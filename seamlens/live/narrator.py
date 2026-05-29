"""Live narration + Q&A over the system graph.

Backed by a Claude Code *chain* (seamlens.live.cc_chain.CCChain), not a raw LLM HTTP
call: each narration/answer spawns a real `claude -p` instance whose cwd is the
watched project, so it can READ the actual source + system graph to ground its reply
-- "restart a Claude Code beside you to read along". Same CC integration the rest of
the ecosystem uses. Two jobs with different latency budgets:

  * narrate_change -- fires on every meaningful tool action, so it gets a tight
    timeout (and an optional faster cc.narrate_model). The graph delta animates
    instantly without the chain; this prose streams into the card a few seconds later.
  * answer -- the human asked a question and expects a considered reply, so it gets
    the heavier model + a longer timeout, and is encouraged to read the real files.

If the chain is disabled/unavailable (e.g. `claude` not on PATH) the narrator falls
back to a deterministic template (narration) or an off message (Q&A), so the live
view never hard-fails.
"""
import re

from seamlens.live.cc_chain import CCChain

# language code -> how we name it to the model. Order also drives the UI dropdown.
LANGUAGES = [
    ("zh", "简体中文"),
    ("en", "English"),
    ("ja", "日本語"),
    ("es", "Español"),
    ("fr", "Français"),
    ("de", "Deutsch"),
]
_LANG_INSTRUCT = {
    "zh": "用简体中文回答。",
    "en": "Answer in English.",
    "ja": "日本語で答えてください。",
    "es": "Responde en español.",
    "fr": "Réponds en français.",
    "de": "Antworte auf Deutsch.",
}


def lang_instruction(lang):
    return _LANG_INSTRUCT.get(lang, _LANG_INSTRUCT["en"])


def build_providers(cfg):
    """(narrate_chain, qa_chain). Both spawn `claude -p` in the watched project;
    narration gets the tighter timeout (+ optional faster cc.narrate_model), Q&A
    gets the longer budget. See cc_chain.CCChain for auth/portability."""
    narrate = CCChain.from_config(cfg, kind="narrate")
    qa = CCChain.from_config(cfg, kind="qa")
    return narrate, qa


# -- describing the tool action in words ---------------------------------------
def describe_action(event):
    """One-line human description of the raw CC hook event."""
    tool = event.get("tool_name") or "?"
    ti = event.get("tool_input") or {}
    if tool in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        fp = ti.get("file_path") or ti.get("notebook_path") or "?"
        verb = "created" if tool == "Write" else "edited"
        return "%s %s" % (verb, fp)
    if tool == "Bash":
        return "ran shell: %s" % (ti.get("command") or "")[:200]
    if event.get("hook_event_name") == "UserPromptSubmit":
        return "you asked: %s" % (event.get("prompt") or "")[:200]
    return "%s" % tool


def _delta_text(delta):
    if not delta:
        return "no structural change to the system graph"
    parts = []
    ae, re_ = delta.get("added_edges", []), delta.get("removed_edges", [])
    an, rn = delta.get("added_nodes", []), delta.get("removed_nodes", [])
    for e in ae[:8]:
        d = e["data"]
        parts.append("+ %s %s -> %s" % (d["kind"], d["source"], d["target"]))
    for e in re_[:8]:
        d = e["data"]
        parts.append("- %s %s -> %s" % (d["kind"], d["source"], d["target"]))
    for n in an[:8]:
        parts.append("+ node %s" % n["data"]["id"])
    for nid in rn[:8]:
        parts.append("- node %s" % nid)
    return "\n".join(parts) if parts else "no new edges or nodes"


_ARCHITECT_SYS = (
    "You are a senior software architect sitting beside a developer who is watching "
    "an AI coding agent (Claude Code) build their codebase in real time. Your cwd IS "
    "that project -- Read any file to ground yourself. The developer's hard problem is "
    "keeping a MENTAL MODEL of the whole system while the agent changes it under them.\n"
    "You are given: the developer's current GOAL (what they asked the agent to do), the "
    "agent's latest ACTION, the exact SYSTEM-GRAPH DELTA it caused (modules, data "
    "artifacts, and who writes/reads each), and the STORY SO FAR.\n"
    "In 1-3 sentences, explain how this change ADVANCES THE GOAL and how it RESHAPES "
    "the system's wiring -- name the concrete modules/artifacts and call out any NEW "
    "producer->consumer coupling or cross-subsystem dependency it creates. Speak at the "
    "level of components and data flow, not lines of code. If the change appears to "
    "DRIFT from the stated goal, say so plainly. Be concrete, use the node names, never "
    "dump file contents. %s")


def narrate_change(provider, event, delta, findings, lang="zh", intent=None, story=None):
    action = describe_action(event)
    delta_txt = _delta_text(delta)
    if not (provider and provider.available):
        return _template_narration(action, delta, lang)
    find_txt = "\n".join("  [%s] %s" % (f.severity, f.title) for f in (findings or [])[:6]) or "  (none new)"
    user = ("GOAL (what the developer asked for):\n  %s\n\n"
            "STORY SO FAR:\n%s\n\n"
            "AGENT ACTION:\n  %s\n\nSYSTEM-GRAPH DELTA:\n%s\n\nNEW SEAM FINDINGS:\n%s\n"
            % (intent or "(not stated yet)",
               story or "  (this is the first tracked change)",
               action, delta_txt, find_txt))
    out = provider.run(user, system=_ARCHITECT_SYS % lang_instruction(lang))
    return out or _template_narration(action, delta, lang)


_AUDITOR_SYS = (
    "You are a paranoid systems auditor sitting beside a developer, watching an AI "
    "coding agent edit their codebase. Your cwd IS that project -- you MUST Read/Grep "
    "the real source to VERIFY before you claim a bug; never speculate about code you "
    "can open. Your single job: find the most likely REAL bug or regression THIS change "
    "introduces ACROSS THE WHOLE SYSTEM -- the cross-component kind that hides in the "
    "seams and that a local diff review misses.\n"
    "You are given the GOAL, the ACTION, a CHANGE-IMPACT BRIEF (the edited module's "
    "graph neighborhood: who imports it, the artifacts it reads/writes and who else "
    "produces/consumes them, related constants), and any new linter findings.\n"
    "Hunt specifically for: (1) CONTRACT DRIFT -- the change altered a function "
    "signature / return shape / artifact format / schema, but a CONSUMER in the brief "
    "still expects the old shape; (2) STALE CALLER -- a name/path/signature changed but "
    "an importer was not updated; (3) PRODUCER/CONSUMER MISMATCH -- an artifact written "
    "but never read, or read before it is written; (4) DIVERGENT CONSTANT -- the same "
    "config value defined differently in two places; (5) RIPPLE -- walk each "
    "imported_by / also_read_by / also_written_by entry in the brief and check it still "
    "holds after this change.\n"
    "VERIFY by reading the actual files named in the brief. Output ONLY real, specific "
    "risks, ranked, each on its own line as:\n"
    "`**[high|med|low]** <one-line risk> -- <file:line> -- <why it breaks>`\n"
    "If, after reading, you find no real cross-component risk, reply with EXACTLY this "
    "and nothing else: NO_RISK. Never pad with generic advice or restate the change. %s")


def format_brief(brief):
    """Render a change_brief dict (graphview.change_brief) for the auditor prompt."""
    if not brief:
        return "  (no graph neighborhood -- the edited file is not a graph node yet)"
    lines = ["  edited module: %s" % brief.get("node", "?")]
    if brief.get("imported_by"):
        lines.append("  imported by (break if its interface changed): %s"
                     % ", ".join(brief["imported_by"][:12]))
    if brief.get("imports_out"):
        lines.append("  imports: %s" % ", ".join(brief["imports_out"][:12]))
    for w in brief.get("writes", [])[:8]:
        also = w["also_read_by"]
        lines.append("  WRITES artifact `%s`%s" % (
            w["artifact"],
            (" -- also read by: " + ", ".join(also[:8])) if also else " -- (no other reader: possible orphan)"))
    for r in brief.get("reads", [])[:8]:
        also = r["also_written_by"]
        lines.append("  READS artifact `%s`%s" % (
            r["artifact"],
            (" -- written by: " + ", ".join(also[:8])) if also else " -- (no writer: possible read-before-write)"))
    if brief.get("constants"):
        lines.append("  related constants: %s" % ", ".join(brief["constants"][:12]))
    return "\n".join(lines)


def audit_change(provider, event, brief, delta, findings, lang="zh", intent=None):
    """The bug-hunt pass. Returns auditor prose (ranked risks) or None when it
    finds nothing real / the chain is unavailable -- caller drops empty audits."""
    if not (provider and provider.available):
        return None
    action = describe_action(event)
    find_txt = "\n".join("  [%s] %s" % (f.severity, f.title) for f in (findings or [])[:8]) or "  (none new)"
    user = ("GOAL:\n  %s\n\nAGENT ACTION:\n  %s\n\nCHANGE-IMPACT BRIEF:\n%s\n\n"
            "SYSTEM-GRAPH DELTA:\n%s\n\nNEW LINTER FINDINGS:\n%s\n"
            % (intent or "(not stated)", action, format_brief(brief),
               _delta_text(delta), find_txt))
    out = provider.run(user, system=_AUDITOR_SYS % lang_instruction(lang))
    if not out:
        return None
    t = out.strip()
    # The auditor is told to reply EXACTLY "NO_RISK" when it finds nothing, but models
    # often narrate their verification first and only append NO_RISK at the end. Treat
    # any NO_RISK token -- or any output with no ranked [high|med|low] severity tag --
    # as "nothing to surface", so a clean audit never leaks a noise card into the feed.
    if "NO_RISK" in t.upper():
        return None
    if not re.search(r"\[(high|med|low)\]", t, re.I):
        return None
    return t


def _template_narration(action, delta, lang):
    ae = (delta or {}).get("added_edges", [])
    if lang == "zh":
        s = "Claude Code %s。" % _zh_action(action)
        if ae:
            d = ae[0]["data"]
            s += "系统图新增 %d 条边，例如 %s：%s → %s。" % (
                len(ae), d["kind"], d["source"], d["target"])
        else:
            s += "本次改动未改变系统图的接缝结构。"
        return s
    s = "Claude Code %s. " % action
    if ae:
        d = ae[0]["data"]
        s += "%d new edge(s) in the system graph, e.g. %s: %s -> %s." % (
            len(ae), d["kind"], d["source"], d["target"])
    else:
        s += "No change to the graph's seams."
    return s


def _zh_action(action):
    return (action
            .replace("edited ", "修改了 ")
            .replace("created ", "新建了 ")
            .replace("ran shell: ", "执行了命令：")
            .replace("you asked: ", "你提问：")) if action else "执行了一个操作"


_ORACLE_SYS = (
    "You are the god-view oracle for a codebase, running as a Claude Code chain whose "
    "cwd IS the project under review. Answer the developer's question about the system's "
    "GLOBAL wiring and behavior. You are given the developer's current GOAL, a SYSTEM-"
    "GRAPH SUMMARY (modules, data artifacts, and who writes/reads each -- where cross-"
    "component bugs live: producer/consumer mismatches, orphaned artifacts, divergent "
    "constants) and the recent change history. Use the summary as your MAP, but "
    "READ/GREP/GLOB the real source to verify specifics -- never guess at code you can "
    "open. When the question touches a component, also state its BLAST RADIUS: what "
    "else imports it / reads its outputs / shares its constants and would be affected. "
    "Be concise and cite concrete module / file:line names. %s")


def answer(provider, question, summary, recent_events, lang="zh", intent=None):
    if not (provider and provider.available):
        why = provider.explain_unavailable() if provider else "no chain"
        if lang == "zh":
            return ("（CC 链未就绪：%s。请确保 Claude Code CLI（`claude`）已安装且可用，"
                    "或在 seamlens.yaml 设置 cc.enabled=true。）" % why)
        return ("(CC chain not ready: %s. Install the Claude Code CLI (`claude`) or "
                "set cc.enabled=true in seamlens.yaml.)" % why)
    recent = "\n".join("  - %s" % e for e in (recent_events or [])[-8:]) or "  (none yet)"
    user = ("GOAL:\n  %s\n\nSYSTEM-GRAPH SUMMARY:\n%s\n\nRECENT CHANGES THIS SESSION:\n%s\n\n"
            "QUESTION:\n%s\n" % (intent or "(not stated)", _fmt_summary(summary), recent, question))
    out = provider.run(user, system=_ORACLE_SYS % lang_instruction(lang))
    if out:
        return out
    if lang == "zh":
        return "（CC 链调用失败：%s）" % (provider.last_error or "unknown")
    return "(CC chain failed: %s)" % (provider.last_error or "unknown")


def _fmt_summary(st):
    if not st:
        return "  (graph not scanned yet)"
    f = st.get("findings", {})
    return ("  modules=%s libraries=%s artifacts=%s imports_edges=%s constants=%s\n"
            "  entrypoints=%s\n  dead_modules=%s\n"
            "  shared_artifacts=%s\n  write_only=%s\n  read_only=%s\n"
            "  findings: error=%s warning=%s info=%s" % (
                st.get("modules"), st.get("libraries"), st.get("artifacts"),
                st.get("imports_edges"), st.get("constants"),
                st.get("entrypoints", [])[:12], st.get("dead_modules", [])[:12],
                st.get("shared_artifacts", [])[:12], st.get("write_only_artifacts", [])[:8],
                st.get("read_only_artifacts", [])[:8],
                f.get("error"), f.get("warning"), f.get("info")))
