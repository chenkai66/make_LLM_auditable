"""The live companion server: a stdlib HTTP + SSE hub between Claude Code's hooks
and the browser god-view. Zero third-party deps.

Flow per meaningful tool action:
  1. hook.py POSTs the CC hook payload to /event.
  2. We rescan the graph (fast) and diff it against the in-memory snapshot, so we
     know exactly which edges/nodes the change added -- published immediately as a
     `graph_delta` so the UI animates with no LLM latency.
  3. A background thread asks the (fast) narrator to explain the change and
     publishes a `narration` card when it's ready.
The browser subscribes to /stream (SSE) and also gets a replay of the recent ring
buffer on connect, so a late-joining tab catches up.
"""
import json
import os
import queue
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from seamlens.core.graph import GraphStore
from . import graphview, narrator, rescan

_UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
_EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
_RING_MAX = 200


def _finding_key(f):
    return (getattr(f, "title", ""), tuple(sorted(getattr(f, "ids", []) or [])))


class LiveState:
    def __init__(self, cfg, lang="zh"):
        self.cfg = cfg
        self.lang = lang
        self.lock = threading.Lock()
        self._subs = []
        self._subs_lock = threading.Lock()
        self.ring = []
        self.prev_snapshot = (set(), {})
        self.prev_finding_keys = set()
        self.narrate_prov, self.qa_prov = narrator.build_providers(cfg)
        self.event_log = []   # short human strings for Q&A context

    # -- pub/sub --------------------------------------------------------------
    def subscribe(self):
        q = queue.Queue()
        with self._subs_lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._subs_lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, msg):
        self.ring.append(msg)
        if len(self.ring) > _RING_MAX:
            self.ring = self.ring[-_RING_MAX:]
        with self._subs_lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except Exception:
                pass

    def ring_copy(self):
        return list(self.ring)

    # -- store helpers --------------------------------------------------------
    def open_store(self):
        return GraphStore(self.cfg.db_path)

    def prime_snapshot(self):
        st = self.open_store()
        try:
            if st.current_run() is not None:
                self.prev_snapshot = graphview.snapshot(st)
                self.prev_finding_keys = {_finding_key(f) for f in rescan.lint(self.cfg)}
        finally:
            st.close()


STATE = None  # set by serve()


def _relativize(path):
    if not path:
        return None
    root = STATE.cfg.project_root
    ap = os.path.abspath(path)
    if ap.startswith(root + os.sep):
        return os.path.relpath(ap, root)
    return path


def process_event(payload):
    """Synchronous part: rescan + delta + immediate publish. Spawns async
    narration. Serialized by STATE.lock so rescans don't race."""
    tool = payload.get("tool_name") or ""
    evname = payload.get("hook_event_name") or ""
    action_desc = narrator.describe_action(payload)
    delta = {}
    edited_node = None
    new_findings = []

    with STATE.lock:
        edited_rel = None
        if tool in _EDIT_TOOLS:
            ti = payload.get("tool_input") or {}
            edited_rel = _relativize(ti.get("file_path") or ti.get("notebook_path"))
            edited_node = graphview.locate(edited_rel)
        do_rescan = bool(edited_rel and edited_rel.endswith(".py"))
        if do_rescan:
            rescan.rescan(STATE.cfg)
            st = STATE.open_store()
            try:
                after = graphview.snapshot(st)
                delta = graphview.diff(STATE.prev_snapshot, after, st)
                STATE.prev_snapshot = after
                findings = rescan.lint(STATE.cfg)
            finally:
                st.close()
            keys_now = {_finding_key(f) for f in findings}
            added_keys = keys_now - STATE.prev_finding_keys
            STATE.prev_finding_keys = keys_now
            new_findings = [f for f in findings if _finding_key(f) in added_keys]

    STATE.event_log.append(action_desc)
    STATE.publish({
        "type": "graph_delta",
        "action": action_desc,
        "edited_node": edited_node,
        "delta": delta,
        "tool": tool,
        "event": evname,
        "findings": [
            {"severity": f.severity, "title": f.title,
             "where": list(getattr(f, "where", []))[:3]}
            for f in new_findings[:10]
        ],
    })

    # async narration -- skip pure prompt-submit/stop unless there was a change
    threading.Thread(target=_narrate_async,
                     args=(payload, delta, new_findings), daemon=True).start()


def _narrate_async(payload, delta, new_findings):
    try:
        text = narrator.narrate_change(STATE.narrate_prov, payload, delta,
                                       new_findings, lang=STATE.lang)
    except Exception as e:
        text = "(narration error: %s)" % e
    STATE.publish({"type": "narration", "text": text,
                   "action": narrator.describe_action(payload)})


def handle_ask(question, lang):
    st = STATE.open_store()
    try:
        summary = graphview.graph_summary(st) if st.current_run() is not None else {}
    finally:
        st.close()
    ans = narrator.answer(STATE.qa_prov, question, summary,
                          STATE.event_log, lang=lang or STATE.lang)
    STATE.publish({"type": "answer", "question": question, "text": ans})
    return ans


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass  # quiet

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # -- GET ------------------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            return self._serve_ui()
        if path == "/graph":
            return self._serve_graph()
        if path == "/config":
            return self._send_json({
                "project": os.path.basename(STATE.cfg.project_root),
                "project_root": STATE.cfg.project_root,
                "lang": STATE.lang,
                "languages": narrator.LANGUAGES,
                "ai": bool(getattr(STATE.qa_prov, "enabled", False) and STATE.qa_prov.available),
            })
        if path == "/stream":
            return self._serve_stream()
        self._send_json({"error": "not found"}, 404)

    def _serve_ui(self):
        fp = os.path.join(_UI_DIR, "index.html")
        try:
            with open(fp, "rb") as f:
                body = f.read()
        except OSError:
            body = b"<h1>seamlens live</h1><p>ui/index.html missing</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_graph(self):
        st = STATE.open_store()
        try:
            if st.current_run() is None:
                return self._send_json({"nodes": [], "edges": [], "summary": {}})
            els = graphview.cytoscape_elements(st)
            els["summary"] = graphview.graph_summary(st)
        finally:
            st.close()
        self._send_json(els)

    def _serve_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        q = STATE.subscribe()
        try:
            for msg in STATE.ring_copy():       # replay for late joiners
                self._sse(msg)
            while True:
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self._sse(msg)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            STATE.unsubscribe(q)

    def _sse(self, msg):
        data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        self.wfile.write(b"data: " + data + b"\n\n")
        self.wfile.flush()

    # -- POST -----------------------------------------------------------------
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        payload = self._read_json()
        if path == "/event":
            try:
                process_event(payload)
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 200)
            return self._send_json({"ok": True})
        if path == "/ask":
            ans = handle_ask(payload.get("question", ""), payload.get("lang"))
            return self._send_json({"answer": ans})
        if path == "/lang":
            STATE.lang = payload.get("lang") or STATE.lang
            return self._send_json({"ok": True, "lang": STATE.lang})
        self._send_json({"error": "not found"}, 404)


def serve(cfg, port=8722, open_ui=True, lang="zh"):
    global STATE
    STATE = LiveState(cfg, lang=lang)
    STATE.prime_snapshot()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d/" % port
    print("seamlens live -> %s  (project: %s)" % (url, cfg.project_root))
    print("  AI: %s" % ("on" if (getattr(STATE.qa_prov, "enabled", False)
                                  and STATE.qa_prov.available) else "off (template narration)"))
    if open_ui:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        httpd.shutdown()
