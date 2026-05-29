# seamlens — make a complex system auditable

A complex codebase fails most often **in the seams between components**, not
inside a single function: a producer writes `results_summary.json` while the
validator only reads `done.json` (so the job loops forever); a worker cap is `6`
in one module and `9` in another (so batches get rejected early); a daemon calls
`logging.basicConfig()` after something installed a root handler (so 16 days of
logs vanish into the void). These are invisible to ordinary linters because each
file is locally correct — the bug is the *relationship*.

seamlens makes those relationships **queryable and lint-able**. It is a generic,
portable engine: the core has zero project-specific code, and everything a given
project needs lives in one `seamlens.yaml`. Point it at any Python repo to adopt.

## How it works — three layers

1. **Auto-derived structural skeleton** (`extractors/`): from the source AST it
   builds a *system graph* — `module --writes/reads--> artifact`,
   `module --imports--> module`, `config_const` definitions, `logger_install`
   seams, FSM transition tables. No annotation required; this layer is free.
2. **Thin curated semantic layer** (`*.semantic.yaml`): the handful of facts the
   code can't tell you — which filenames are *equivalent* completion signals,
   which constants *must* agree, which orphan reads are legitimately external.
   Small, human-owned, high-signal.
3. **Graph linters** (`linters/`): each encodes a real bug-class and is named for
   the incident it catches. They run over the graph + semantic overlay and emit
   findings at `error` / `warning` / `info` severity.

## The bug-classes it catches

| Linter | Catches | Severity |
|---|---|---|
| `completion_signal_mismatch` | a completion signal produced but no validator reads it → redispatch loop | error |
| `test_writes_prod` | a test module writes a prod-path artifact (e.g. periodic test polluting `errors.db`) | error |
| `divergent_constant` | one constant (or declared synonym-set) with different values across modules | warning / info |
| `logger_trap` | a library calls `basicConfig` after the root logger already has a handler → silent logs | warning |
| `orphan_artifact` | a data artifact read with no in-repo producer (removed upstream) or written with no reader | info |
| `dead_module` | a module nobody imports and with no `__main__` guard → a legacy copy that silently rots | info |

## Quickstart

```bash
# 1. write a starter config in your repo
python3 -m seamlens init /path/to/your/project
$EDITOR /path/to/your/project/seamlens.yaml      # set roots, io_helpers, logger.install_fn, ...

# 2. build the system graph
python3 -m seamlens scan /path/to/your/project

# 3. run the linters
python3 -m seamlens lint /path/to/your/project            # human-readable
python3 -m seamlens lint /path/to/your/project --json     # machine-readable
python3 -m seamlens lint /path/to/your/project --strict   # exit 1 on any error (for CI)

# extras
python3 -m seamlens query /path/to/your/project --kind artifact   # dump graph nodes
python3 -m seamlens diff  /path/to/your/project                   # node delta vs previous scan (blast radius)
```

## Live companion — a god-view for Claude Code

`seamlens live` turns the batch auditor into an always-on browser sidebar beside
Claude Code. On every meaningful edit it narrates *what changed and why it
matters* in plain language, pulses the edited node inside the full system graph,
and animates the new edges the change introduced — and you can ask it questions in
your language of choice. It reuses the same graph, linters, and `ai:` config; the
core commands above never import it.

```bash
python3 -m seamlens live --install /path/to/project   # wire CC hooks (idempotent)
python3 -m seamlens live /path/to/project              # start; opens the browser
```

See `docs/LIVE.md` for the full walkthrough (languages, ports, the meaningful-event
filter, and the graceful no-LLM fallback).

## Adopting on a new project

The only host-aware surface is `seamlens.yaml`. The fields that matter most:

- `roots` / `exclude` — where your source lives (exclude matches *path segments*,
  not substrings, so excluding `data` never breaks a repo rooted under `/data`).
- `io_helpers.read` / `.write` — your project's JSON/file helper functions, so the
  file-I/O extractor sees reads/writes that don't go through bare `open()`.
- `logger.install_fn` — the function that installs a root log handler (the thing
  that turns a later `basicConfig` into a no-op). Leave null if you don't have one.
- `prod_path_markers` / `test_file_markers` — for the `test_writes_prod` linter.
- `fsm_sources` — `"relpath.py:TRANSITIONS_DICT"` to lint state-machine tables.

The curated semantic file (`*.semantic.yaml`) is optional but sharpens precision:
`artifact_groups` (equivalent completion signals), `constant_groups` (synonyms
that must agree), `allow_orphan_reads/writes`, `allow_dead_modules`.

See `configs/research-agent.seamlens.yaml` and `configs/research-agent.semantic.yaml`
for a complete worked example, and `docs/DESIGN.md` for the rationale behind the
three-layer split.

## Design principle

> The core (`seamlens/`) contains zero project-specific code. A project is
> onboarded entirely by writing `seamlens.yaml` + an optional `*.semantic.yaml`.

This is what makes it portable across teams and projects. Findings are
heuristics, tiered by confidence: `error`/`warning` are high-precision (act on
them); `info` is an advisory hint (static analysis is blind to dynamic paths, so
these surface candidates for human review, not assertions).
