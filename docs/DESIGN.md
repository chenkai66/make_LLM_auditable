# seamlens design

## The problem

A system grows past the point where any one person holds it in their head. Each
file is locally correct, reviewed, tested. Yet it fails — and the failures share
a shape: they live in the **seams** where one component's output is another's
input, where two modules each hold "the" value of a constant, where a process's
logging assumptions are violated by a different module loaded into the same
interpreter. Ordinary linters and type-checkers see one file at a time, so they
are structurally blind to this entire class of bug.

seamlens exists to make the seams a first-class, queryable object.

## Three layers, and why the split

### Layer 1 — auto-derived structural skeleton (`extractors/`)

A set of independent AST extractors populate a single SQLite **system graph**:

- `imports` — `module --imports--> module`, annotating each module with its
  in-degree (importer count) and whether it has a `__main__` guard. This is what
  separates a *library* (imported into a long-lived daemon) from an *entrypoint*
  (run as its own process), a distinction several linters depend on.
- `file_io` — `module --writes/reads--> artifact`, keyed by the *filename literal*
  (basename), because cross-component coupling is by filename, not by variable.
  Existence probes (`os.path.exists(p)`) count as reads of a marker; intra-module
  `p = os.path.join(d, 'done.json')` is resolved so `open(p)` is attributed.
- `constants` — module/class-level ALL_CAPS assignments as `config_const` nodes
  with their literal value, so divergence is comparable.
- `logger` — per-module logging seam: does it call the framework setup
  (`basicConfig`), install the root handler, import the installer, have an
  explicit `StreamHandler`, and is its setup confined to a `__main__` guard.
- `fsm` — declared transition tables parsed into `fsm`/`fsm_state` nodes +
  `transitions_to` edges.

This layer requires **no annotation** — it is free, and it refreshes on every
scan. The graph store keeps the two most recent runs so `diff` can show the
blast radius of a change.

### Layer 2 — thin curated semantic overlay (`*.semantic.yaml`)

Some facts cannot be derived from syntax: that `done.json`, `results.json`, and
`results_summary.json` are *equivalent* completion signals; that `WORKER_CAP`
and `MAX_WORKERS` are synonyms that must agree; that `config.json` is read but
legitimately produced by an external process. These are few, they change slowly,
and they are owned by a human who understands the domain. Keeping this layer thin
is a deliberate constraint: if it grows large, the structural layer is too weak.

### Layer 3 — graph linters (`linters/`)

Each linter encodes one real bug-class and is documented with the incident that
motivated it. They consume the graph + overlay and emit tiered findings:

- **error / warning** — high precision; meant to be acted on or to block CI.
- **info** — advisory; static analysis is blind to dynamic paths, so these
  surface candidates for human review rather than asserting a defect.

The tiering is the discipline that keeps the tool trusted: a linter that cries
wolf at warning level gets ignored. When a check cannot be made precise, it is
demoted to info rather than dropped.

## Portability invariant

> `seamlens/` contains zero project-specific code. A project is onboarded
> entirely by writing `seamlens.yaml` (+ optional `*.semantic.yaml`).

This mirrors the host-adapter pattern: the engine is universal, the configuration
is the only host-aware surface. It is what lets one team's seamlens setup transfer
to another team's repo by copying and editing a single YAML file.

## Co-evolution (the GAN loop)

seamlens was hardened against a real, large codebase by treating the two as
adversaries that sharpen each other:

- **discriminator** (seamlens) sharpens until it emits *zero false positives* at
  error/warning severity — every remaining high-severity finding is a true defect.
- **generator** (the target system) fixes each true defect seamlens surfaces.

Convergence = a clean error/warning lint where every finding that remains is
either fixed or a deliberate, allow-listed exception. Each round either fixes a
real bug in the target or teaches the discriminator a new precision rule (e.g.
"a top-level `basicConfig` is only a trap in a library, not an entrypoint" —
which is exactly why the `imports` extractor exists).
