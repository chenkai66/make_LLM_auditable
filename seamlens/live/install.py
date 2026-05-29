"""Wire the live companion into a project's Claude Code hooks.

Writes three hooks into <project>/.claude/settings.local.json (the per-project,
not-committed settings file) so that every edit / shell mutation / prompt / stop
in that project gets forwarded to the running live server:

  PostToolUse  (Edit|MultiEdit|Write|NotebookEdit|Bash)  -> bridge
  UserPromptSubmit                                        -> bridge
  Stop                                                    -> bridge

The hook command embeds THIS interpreter and the seamlens package location, so it
resolves no matter what cwd Claude Code runs hooks from (the companion can watch a
project that is not seamlens itself). Idempotent: re-running replaces our entries
(matched by the `seamlens.live.hook` marker) and leaves any other hooks intact.
"""
import json
import os
import sys

_MARKER = "seamlens.live.hook"


def _command(port):
    pkg_parent = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = 'PYTHONPATH="%s" ' % pkg_parent
    if str(port) != "8722":
        env += "SEAMLENS_LIVE_PORT=%s " % port
    return '%s"%s" -m seamlens.live.hook' % (env, sys.executable)


def _entry(cmd, matcher=None):
    e = {"hooks": [{"type": "command", "command": cmd}]}
    if matcher is not None:
        e["matcher"] = matcher
    return e


def _strip_ours(entries):
    """Drop any hook group that contains our bridge command."""
    out = []
    for grp in entries or []:
        hooks = (grp or {}).get("hooks") or []
        if any(_MARKER in (h.get("command") or "") for h in hooks):
            continue
        out.append(grp)
    return out


def install(project_root, port=8722):
    project_root = os.path.abspath(project_root)
    cdir = os.path.join(project_root, ".claude")
    os.makedirs(cdir, exist_ok=True)
    target = os.path.join(cdir, "settings.local.json")

    settings = {}
    if os.path.exists(target):
        try:
            with open(target) as f:
                settings = json.load(f) or {}
        except Exception:
            settings = {}

    hooks = settings.setdefault("hooks", {})
    cmd = _command(port)
    hooks["PostToolUse"] = _strip_ours(hooks.get("PostToolUse")) + [
        _entry(cmd, matcher="Edit|MultiEdit|Write|NotebookEdit|Bash")]
    hooks["UserPromptSubmit"] = _strip_ours(hooks.get("UserPromptSubmit")) + [_entry(cmd)]
    hooks["Stop"] = _strip_ours(hooks.get("Stop")) + [_entry(cmd)]

    with open(target, "w") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print("installed seamlens live hooks -> %s" % target)
    print("  PostToolUse / UserPromptSubmit / Stop -> %s" % cmd)
    print("\nNext:")
    print("  1) start the companion:  python3 -m seamlens live %s --port %d" % (project_root, port))
    print("  2) open Claude Code in this project and start editing.")
    print("  (the browser god-view opens automatically when the server starts)")
    return target
