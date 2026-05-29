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
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from seamlens.core.graph import GraphStore
from . import graphview, narrator, rescan

_UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
_EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
_RING_MAX = 200
# control messages (clear/delete) are broadcast to sync tabs but are NOT part of
# the durable record themselves -- they must not be persisted or replayed.
# answer_step is live streaming progress ("reading X..."); the finished answer card
# replaces the placeholder on resolve, so the steps are transient by design.
_EPHEMERAL_TYPES = {"control", "answer_step"}


def _git_rev(root):
    try:
        return subprocess.check_output(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return ""


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
        self.ring_lock = threading.Lock()  # publish() runs from N daemon threads
        self._next_id = 0     # monotonic per-message id, for delete/dedup across tabs
        self.feed_path = cfg.abspath(".seamlens/live_feed.jsonl")
        self.git_rev = ""     # set in serve(); stamped onto every learned meta record
        self.prev_snapshot = (set(), {})
        self.prev_finding_keys = set()
        self.narrate_prov, self.qa_prov = narrator.build_providers(cfg)
        self.event_log = []   # short human strings for Q&A context
        self.intent = None    # latest UserPromptSubmit -- the dev's GOAL for the agent
        self.story = []        # running architect summaries, fed back for continuity
        self.story_lock = threading.Lock()  # _narrate_async runs in N daemon threads
        # One durable claude session for /ask, so follow-ups (--resume) keep memory.
        self.qa_session = {"id": None}

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
        # Every message gets a monotonic id so the UI can delete a single card and
        # all tabs stay in sync. Control messages (clear/delete) are broadcast for
        # cross-tab sync but are NOT part of the durable record -- skip ring+disk.
        ephemeral = msg.get("type") in _EPHEMERAL_TYPES
        with self.ring_lock:
            if "id" not in msg:
                self._next_id += 1
                msg["id"] = self._next_id
            if not ephemeral:
                self.ring.append(msg)
                if len(self.ring) > _RING_MAX:
                    self.ring = self.ring[-_RING_MAX:]
                self._persist(msg)
        with self._subs_lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except Exception:
                pass

    def resolve(self, mid, msg):
        """Replace an already-published card (same id) with its finished form and
        broadcast it -- e.g. a streamed answer whose final text supplants the
        placeholder. Updates ring + disk so a late tab replays the finished card,
        not the spinner; does NOT mint a new id (that would duplicate the card)."""
        msg["id"] = mid
        with self.ring_lock:
            for i, m in enumerate(self.ring):
                if m.get("id") == mid:
                    self.ring[i] = msg
                    break
            else:
                self.ring.append(msg)
            self._rewrite_feed()
        with self._subs_lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except Exception:
                pass

    def _persist(self, msg):
        try:
            os.makedirs(os.path.dirname(self.feed_path), exist_ok=True)
            with open(self.feed_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _rewrite_feed(self):
        """Truncate-and-rewrite the JSONL from the in-memory ring. Called after a
        trim/delete/clear so the file never drifts from what a fresh tab replays."""
        try:
            os.makedirs(os.path.dirname(self.feed_path), exist_ok=True)
            tmp = self.feed_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for m in self.ring:
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")
            os.replace(tmp, self.feed_path)
        except Exception:
            pass

    def delete(self, mid):
        with self.ring_lock:
            self.ring = [m for m in self.ring if m.get("id") != mid]
            self._rewrite_feed()

    def clear(self):
        with self.ring_lock:
            self.ring = []
            try:
                if os.path.exists(self.feed_path):
                    os.remove(self.feed_path)
            except Exception:
                pass

    def load_feed(self):
        """Replay the last _RING_MAX records from disk into the ring on startup, so a
        restart doesn't lose the session's narration history."""
        try:
            with open(self.feed_path, encoding="utf-8") as f:
                rows = [json.loads(ln) for ln in f if ln.strip()]
        except OSError:
            return
        except Exception:
            return
        rows = rows[-_RING_MAX:]
        with self.ring_lock:
            self.ring = rows
            self._next_id = max((m.get("id", 0) for m in rows), default=0)

    def ring_copy(self):
        with self.ring_lock:
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
    brief = {}

    # A new prompt = the dev restating what they want the agent to build. Capture it
    # as the GOAL so architect/auditor/oracle can frame everything against intent.
    if evname == "UserPromptSubmit":
        p = (payload.get("prompt") or "").strip()
        if p:
            STATE.intent = p[:600]
            STATE.publish({"type": "intent", "text": STATE.intent})

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
                brief = graphview.change_brief(st, edited_node)
            finally:
                st.close()
            keys_now = {_finding_key(f) for f in findings}
            added_keys = keys_now - STATE.prev_finding_keys
            STATE.prev_finding_keys = keys_now
            new_findings = [f for f in findings if _finding_key(f) in added_keys]

    # Is there anything worth narrating? A structural delta, a new finding, or an
    # actual file/shell mutation. Pure prompt-submit with no change gets a card but
    # no "nothing changed" narration (that prose is just noise).
    has_delta = bool(delta and (delta.get("added_edges") or delta.get("removed_edges")
                                or delta.get("added_nodes") or delta.get("removed_nodes")))
    will_narrate = bool(has_delta or new_findings or tool in _EDIT_TOOLS or tool == "Bash")

    STATE.event_log.append(action_desc)
    STATE.publish({
        "type": "graph_delta",
        "action": action_desc,
        "edited_node": edited_node,
        "delta": delta,
        "tool": tool,
        "event": evname,
        "narrate": will_narrate,
        "findings": [
            {"severity": f.severity, "title": f.title,
             "where": list(getattr(f, "where", []))[:3]}
            for f in new_findings[:10]
        ],
    })

    if will_narrate:
        threading.Thread(target=_narrate_async,
                         args=(payload, delta, new_findings, edited_node),
                         daemon=True).start()
    # The auditor is a separate, heavier pass: only worth running when the system
    # graph actually shifted (a real code edit), where cross-component breakage lives.
    if do_rescan:
        threading.Thread(target=_audit_async,
                         args=(payload, brief, delta, new_findings, edited_node),
                         daemon=True).start()


def _learn(node, key, value, source):
    """Persist one piece of AI-learned knowledge onto a node (run-independent
    node_meta) and tell the UI so the node's inspector gains an 'AI 已知' entry
    without a page reload. No node (edit on a non-graph file) -> just skip."""
    if not node or not value:
        return
    st = STATE.open_store()
    try:
        st.set_meta(node, key, value, git_rev=STATE.git_rev, source=source)
        meta = st.get_meta(node)
    finally:
        st.close()
    STATE.publish({"type": "node_meta", "node": node, "meta": meta})


def _narrate_async(payload, delta, new_findings, edited_node=None):
    with STATE.story_lock:
        story_txt = "\n".join("  - %s" % s for s in STATE.story[-6:])
    try:
        text = narrator.narrate_change(STATE.narrate_prov, payload, delta,
                                       new_findings, lang=STATE.lang,
                                       intent=STATE.intent, story=story_txt or None)
    except Exception as e:
        text = "(narration error: %s)" % e
    action = narrator.describe_action(payload)
    # Feed the architect's own reading back into the story so the next narration has
    # continuity ("grasp the global logic as it evolves") rather than isolated blurbs.
    with STATE.story_lock:
        STATE.story.append("%s -> %s" % (action, (text or "").replace("\n", " ")[:200]))
        if len(STATE.story) > 40:
            STATE.story = STATE.story[-40:]
    STATE.publish({"type": "narration", "text": text, "action": action})
    # Don't let a read go to waste: sink the architect's reading onto the node so the
    # graph gets richer each scan instead of re-reading cold (oracle reuses it later).
    if text and not text.startswith("(narration error"):
        _learn(edited_node, "reading", text, "architect")


def _audit_async(payload, brief, delta, new_findings, edited_node=None):
    try:
        risks = narrator.audit_change(STATE.qa_prov, payload, brief, delta,
                                      new_findings, lang=STATE.lang, intent=STATE.intent)
    except Exception as e:
        risks = "(audit error: %s)" % e
    node = edited_node or graphview.locate(_relativize(
        (payload.get("tool_input") or {}).get("file_path")))
    if not risks:
        # NO_RISK -> no card, but a clean audit IS a positive memory: record that this
        # node was checked and found sound at this rev. Only when the auditor actually
        # ran -- with the chain unavailable, audit_change returns None without reading,
        # so marking "audited_clean" would be a lie.
        prov = STATE.qa_prov
        if prov and getattr(prov, "available", False):
            _learn(node, "audited_clean", STATE.git_rev or "checked", "auditor")
        return
    STATE.publish({"type": "risk", "text": risks,
                   "action": narrator.describe_action(payload),
                   "node": node})
    _learn(node, "risk", risks, "auditor")


def handle_ask(question, lang):
    """Stream one oracle turn: post a placeholder card immediately, forward each
    file-read / thought as an answer_step so the dev sees the agent working (not a
    blind spinner), then resolve the placeholder to the finished markdown answer.
    Runs in its own thread (see do_POST /ask) so the HTTP request returns at once."""
    lang = lang or STATE.lang
    st = STATE.open_store()
    try:
        summary = graphview.graph_summary(st) if st.current_run() is not None else {}
        knowledge = st.all_meta()
    finally:
        st.close()
    placeholder = {"type": "answer_start", "question": question}
    STATE.publish(placeholder)
    qid = placeholder["id"]

    def on_event(ev):
        STATE.publish({"type": "answer_step", "qid": qid, "step": ev})

    try:
        ans = narrator.answer(STATE.qa_prov, question, summary, STATE.event_log,
                              lang=lang, intent=STATE.intent, knowledge=knowledge,
                              on_event=on_event, session=STATE.qa_session)
    except Exception as e:
        ans = "(ask error: %s)" % e
    STATE.resolve(qid, {"type": "answer", "question": question, "text": ans})
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
            # Fire-and-forget: the answer (placeholder -> steps -> final) is delivered
            # entirely over SSE, so every tab sees the same stream and the POST returns
            # instantly instead of blocking for the full read-and-reason latency.
            q = (payload.get("question") or "").strip()
            if q:
                threading.Thread(target=handle_ask,
                                 args=(q, payload.get("lang")), daemon=True).start()
            return self._send_json({"ok": True})
        if path == "/clear":
            STATE.clear()
            STATE.publish({"type": "control", "action": "clear"})
            return self._send_json({"ok": True})
        if path == "/delete":
            mid = payload.get("id")
            STATE.delete(mid)
            STATE.publish({"type": "control", "action": "delete", "id": mid})
            return self._send_json({"ok": True})
        if path == "/lang":
            STATE.lang = payload.get("lang") or STATE.lang
            return self._send_json({"ok": True, "lang": STATE.lang})
        self._send_json({"error": "not found"}, 404)


def serve(cfg, port=8722, open_ui=True, lang="zh"):
    global STATE
    STATE = LiveState(cfg, lang=lang)
    STATE.git_rev = _git_rev(cfg.project_root)
    STATE.load_feed()        # replay prior session's feed so a restart isn't a blank slate
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
