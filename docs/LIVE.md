# seamlens live — a god-view companion for Claude Code

`seamlens live` is an always-on browser sidebar that sits beside Claude Code (or
any editor) and, on every meaningful change, (1) **narrates in plain language**
what just changed and why it matters, (2) **lights up the edited node** inside
the full system graph, and (3) **animates the new edges** the change introduced.
You can also **ask it questions** about the change, in your language of choice.

It is the real-time counterpart to the batch `lint` / `atlas` commands: instead
of a verdict after the fact, you watch the seams move as the code is written.

```
Claude Code ──PostToolUse / UserPromptSubmit / Stop hook──▶ POST :8722/event
                                                                 │
                                                  seamlens/live/server.py
                                                  ├─ rescan → recompute graph delta
                                                  ├─ run the linters on the new graph
                                                  ├─ narrate the delta (LLM, your language)
                                                  └─ push to the browser via SSE /stream
                                                                 │
                                              ui/index.html  (cytoscape god-view
                                              + narration feed + chat + language picker)
```

## Quickstart

```bash
# 1. (optional) point an OpenAI-compatible LLM at it for narration + Q&A.
#    Without these, narration falls back to a deterministic template — still useful.
export SEAMLENS_AI_BASE_URL="https://your-endpoint/v1"
export SEAMLENS_AI_KEY="sk-..."

# 2. wire the Claude Code hooks into the project you want to watch (idempotent):
python3 -m seamlens live --install /path/to/project

# 3. start the companion (auto-scans a fresh baseline, opens the browser):
python3 -m seamlens live /path/to/project

# 4. open Claude Code in that same project and start editing.
#    Each meaningful edit appears in the feed within ~1s; the edited module
#    pulses in the graph and any new edge animates in.
```

Flags:

- `--install` — write the three hooks into `<project>/.claude/settings.local.json`
  and exit. Re-running replaces only our entries (matched by the `seamlens.live.hook`
  marker); any other hooks you have are left intact.
- `--port N` — listen on a different port (default `8722`). The hook bridge reads
  the same port from `SEAMLENS_LIVE_PORT`, which `--install` bakes into the command
  when the port is non-default.
- `--lang CODE` — default narration language (`zh` default; also `en`, `ja`, `es`,
  `fr`, `de`). Switchable live from the dropdown in the UI.
- `--no-browser` — don't auto-open the browser (for headless / remote runs).

## What counts as a "meaningful" change

The hook bridge (`seamlens.live.hook`) forwards only events that can move the
system graph, so the feed never fills with noise:

- `Edit` / `MultiEdit` / `Write` / `NotebookEdit` — always.
- `Bash` — only when the command looks mutating (`git`, `mv`, `rm`, `mkdir`,
  build tools, redirects, `sed -i`, …). Read-only `Read` / `Grep` / `Glob` / `LS`
  are dropped.
- `UserPromptSubmit` / `Stop` — forwarded as conversation markers.

The bridge has two hard rules: **never block or slow Claude Code** (2-second
timeout, always exits 0 even if the server is down) and **forward only
meaningful events**.

## The god-view

The left pane is a [cytoscape.js](https://js.cytoscape.org/) rendering of the
whole system graph, color-coded by node kind:

| Color | Node |
|---|---|
| green | entrypoint module (`__main__` guard or known entry) |
| blue | module |
| grey | dead module (nobody imports it, not runnable) |
| yellow | data artifact |
| purple | config constant |
| teal | FSM state |

On each change the edited node grows a **red pulsing ring** and the view centers
on it; genuinely new edges flash green and fade to steady over ~4s. This is what
makes "where did this edit land, and what did it newly couple?" answerable at a
glance.

## Asking questions

The chat box (bottom-right) sends your question plus a fresh graph summary and the
recent change log to the LLM, and prints a graph-grounded answer. Because it sees
the *current* graph, answers reflect the edits you just made — e.g. "这次改动会不会
引入新的孤儿 artifact?" or "is the new module reachable from any entrypoint?".

Narration uses a fast model (`ai.narrate_model`, default short-token) so cards
fill quickly; Q&A uses the considered model (`ai.model`) for deeper reasoning.
Both are configured in the `ai:` block of `seamlens.yaml`:

```yaml
ai:
  enabled: true
  model: "qwen3.7-max"           # considered model for /ask Q&A
  narrate_model: "qwen3.6-flash" # fast model for proactive per-change narration
  narrate_max_tokens: 300
  narrate_timeout: 30
  max_tokens: 1024
  timeout: 60
```

Credentials are **never** read from config — only from the `SEAMLENS_AI_BASE_URL`
/ `SEAMLENS_AI_KEY` environment variables (overridable via `ai.base_url_env` /
`ai.key_env`). If they're unset or the endpoint is unreachable, narration degrades
to a deterministic template and the companion keeps working.

## Portability

Like the rest of seamlens, the live companion is project-agnostic. It lives
entirely in `seamlens/live/` and only *reads* the existing `GraphStore`, `Config`,
extractors, and linters — it adds no project-specific code, and the five core
commands (`scan` / `lint` / `diff` / `query` / `init`) never import it. Everything
is Python stdlib (`http.server`, `sqlite3`, `urllib`) plus one CDN script tag for
cytoscape, so the demo runs with zero install on any machine that already has
seamlens.
