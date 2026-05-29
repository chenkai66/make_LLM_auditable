"""Claude Code hook bridge: reads the hook JSON on stdin and POSTs it to the live
server's /event. Invoked as `python3 -m seamlens.live.hook` from a PostToolUse /
UserPromptSubmit / Stop hook.

Two hard rules:
  * NEVER block or slow Claude Code. Short timeout, and ALWAYS exit 0 even if the
    server is down / unreachable -- a missing companion must never break the agent.
  * Forward only MEANINGFUL events. Read-only tools (Read/Grep/Glob/LS) produce no
    system-graph change and would just spam the feed, so they're dropped here.
"""
import json
import os
import sys
import urllib.request

_EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
# Bash is forwarded only if the command looks like it mutates the repo/state.
_BASH_MUTATING = ("git ", "mv ", "rm ", "cp ", "mkdir", "touch", "make",
                  "npm ", "pnpm ", "yarn ", "pip ", "python -m build",
                  ">", ">>", "tee ", "sed -i", "go build", "cargo build")
_PASS_EVENTS = {"UserPromptSubmit", "Stop"}


def _meaningful(payload):
    ev = payload.get("hook_event_name") or ""
    if ev in _PASS_EVENTS:
        return True
    tool = payload.get("tool_name") or ""
    if tool in _EDIT_TOOLS:
        return True
    if tool == "Bash":
        cmd = ((payload.get("tool_input") or {}).get("command") or "")
        return any(tok in cmd for tok in _BASH_MUTATING)
    return False


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0
    if not _meaningful(payload):
        return 0
    port = os.environ.get("SEAMLENS_LIVE_PORT", "8722")
    url = "http://127.0.0.1:%s/event" % port
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass  # server down -> silently no-op; never break the agent
    return 0


if __name__ == "__main__":
    sys.exit(main())
