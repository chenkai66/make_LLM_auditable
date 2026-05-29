"""Live narration + Q&A over the system graph.

Wraps the existing seamlens.ai.provider.Provider (config-driven, key-free, graceful
degradation). Two jobs with different latency budgets:

  * narrate_change -- fires on every meaningful tool action, so it wants a FAST
    model (default qwen3.6-flash). The graph delta animates instantly without the
    LLM; this prose streams into the card a few seconds later.
  * answer -- the human asked a question and expects a considered reply, so it can
    use the heavier configured model (e.g. qwen3.7-max).

If the provider is disabled/unavailable the narrator falls back to a deterministic
template so the demo never hard-fails (mirrors atlas.narrate returning None, but
here we always return *something* human-readable).
"""
from seamlens.ai.provider import Provider

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
    """(narrate_provider, qa_provider). Both share creds/base_url from the ai:
    config; narration overrides to a fast model + tight budget."""
    qa = Provider.from_config(cfg)
    ai = dict((cfg.get("ai") or {}))
    fast_model = ai.get("narrate_model") or "qwen3.6-flash"
    narrate = Provider(qa.base_url, qa.api_key, fast_model,
                       max_tokens=ai.get("narrate_max_tokens", 300),
                       temperature=0.3,
                       timeout=ai.get("narrate_timeout", 30))
    narrate.enabled = getattr(qa, "enabled", False)
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


_NARRATE_SYS = (
    "You are an always-on companion sitting beside a developer who is watching an "
    "AI coding agent (Claude Code) edit their codebase. You are given the agent's "
    "latest action and the exact change it caused in the project's SYSTEM GRAPH "
    "(modules, data artifacts, and which module writes/reads each). Explain, in 1-3 "
    "short sentences, what just changed and why it matters for the system's wiring "
    "-- especially any new producer/consumer coupling or risky seam. Be concrete and "
    "use the node names given. Do not invent facts beyond the graph delta. %s")


def narrate_change(provider, event, delta, findings, lang="zh"):
    action = describe_action(event)
    delta_txt = _delta_text(delta)
    if not (provider and getattr(provider, "enabled", False) and provider.available):
        return _template_narration(action, delta, lang)
    find_txt = "\n".join("  [%s] %s" % (f.severity, f.title) for f in (findings or [])[:6]) or "  (none new)"
    user = ("AGENT ACTION:\n  %s\n\nSYSTEM-GRAPH DELTA:\n%s\n\nNEW SEAM FINDINGS:\n%s\n"
            % (action, delta_txt, find_txt))
    out = provider.complete(_NARRATE_SYS % lang_instruction(lang), user)
    return out or _template_narration(action, delta, lang)


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


_QA_SYS = (
    "You are the god-view companion for a codebase. Answer the developer's question "
    "ONLY from the system-graph facts and recent change history provided. The graph "
    "captures modules, data artifacts, and which module writes/reads each -- this is "
    "where cross-component bugs live (producer/consumer mismatches, orphaned "
    "artifacts, divergent constants). If the facts don't cover the question, say so "
    "rather than guessing. Be concise. %s")


def answer(provider, question, summary, recent_events, lang="zh"):
    if not (provider and getattr(provider, "enabled", False) and provider.available):
        if lang == "zh":
            return ("（AI 未启用：请在 seamlens.yaml 的 ai.enabled 设为 true 并配置 "
                    "SEAMLENS_AI_BASE_URL / SEAMLENS_AI_KEY 环境变量后重试。）")
        return ("(AI is off: set ai.enabled=true in seamlens.yaml and export "
                "SEAMLENS_AI_BASE_URL / SEAMLENS_AI_KEY, then retry.)")
    recent = "\n".join("  - %s" % e for e in (recent_events or [])[-8:]) or "  (none yet)"
    user = ("SYSTEM-GRAPH SUMMARY:\n%s\n\nRECENT CHANGES THIS SESSION:\n%s\n\n"
            "QUESTION:\n%s\n" % (_fmt_summary(summary), recent, question))
    out = provider.complete(_QA_SYS % lang_instruction(lang), user)
    if out:
        return out
    if lang == "zh":
        return "（调用失败：%s）" % (provider.last_error or "unknown")
    return "(request failed: %s)" % (provider.last_error or "unknown")


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
