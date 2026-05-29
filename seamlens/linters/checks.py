"""The bug-class linters. Each one is named for the real incident it catches.

Run order is independent; the CLI runs all enabled linters and aggregates."""
from collections import defaultdict

from .base import Linter, Finding


def _io_maps(store):
    """Return (writers, readers): fname -> [(module, file, line)]."""
    writers, readers = defaultdict(list), defaultdict(list)
    for e in store.edges("writes"):
        fn = e["dst"].split("artifact:", 1)[-1]
        writers[fn].append((e["src"], e["file"], e["line"]))
    for e in store.edges("reads"):
        fn = e["dst"].split("artifact:", 1)[-1]
        readers[fn].append((e["src"], e["file"], e["line"]))
    return writers, readers


class CompletionSignalLinter(Linter):
    """CATCHES: experimenter writes results_summary.json but invariant_checker
    only reads done.json/results.json -> completed experiments healed back to
    'designed' forever. Rule: within a declared artifact-group (equivalent
    completion signals), any member that is WRITTEN by some producer but READ by
    NO validator is a signal that will never be recognized."""
    name = "completion_signal_mismatch"

    def run(self):
        writers, readers = _io_maps(self.store)
        groups = (self.semantic.get("artifact_groups") or {})
        for gname, g in groups.items():
            members = set(g.get("members", []))
            if not members:
                continue
            read_members = {m for m in members if readers.get(m)}
            written_members = {m for m in members if writers.get(m)}
            # a produced signal nobody validates
            for m in sorted(written_members - read_members):
                prod = writers[m][0]
                yield Finding(
                    self.name, "error",
                    "Produced completion signal '%s' is never read by any validator" % m,
                    "Group '%s': producers write '%s' but no module reads it; sibling "
                    "signals read = %s. A consumer keyed on the siblings will loop." % (
                        gname, m, sorted(read_members) or "none"),
                    where=["%s:%s" % (prod[1], prod[2])],
                    ids=["artifact:" + m],
                )
            # NOTE: the inverse (a validator reads a member nobody produces) is
            # NOT flagged -- a validator that defensively accepts extra completion
            # filenames is correct, not a bug. Only the produced-but-unvalidated
            # direction above causes the redispatch loop.


class OrphanArtifactLinter(Linter):
    """CATCHES: a consumer reads an artifact whose producer was deleted (the
    kg_enrichment.py upstream went missing -> 16 days of empty pipeline). Rule:
    artifact read by some module but written by none (and not allow-listed as an
    external/runtime input)."""
    name = "orphan_artifact"

    # Only real data artifacts -- not /proc pseudo-files, source, or dirs. Static
    # path resolution is blind to dynamic producer paths, so this is a hint
    # (INFO), surfaced mainly to catch a removed upstream producer (kg_enrichment).
    DATA_EXTS = (".json", ".jsonl", ".db", ".sqlite", ".csv", ".tsv", ".pkl",
                 ".pickle", ".npy", ".parquet", ".signal", ".tar.gz")

    def run(self):
        writers, readers = _io_maps(self.store)
        allow_r = set(self.semantic.get("allow_orphan_reads", []))
        allow_w = set(self.semantic.get("allow_orphan_writes", []))

        def is_data(fn):
            return fn.endswith(self.DATA_EXTS)

        for fn, locs in sorted(readers.items()):
            if fn in writers or fn in allow_r or not is_data(fn):
                continue
            yield Finding(
                self.name, "info",
                "Data artifact '%s' is read but never written" % fn,
                "%d reader(s), 0 in-repo producers. Likely a dynamic/remote/"
                "shell producer; flag only if an upstream producer was removed." % len(locs),
                where=["%s:%s" % (l[1], l[2]) for l in locs[:4]],
                ids=["artifact:" + fn],
            )
        for fn, locs in sorted(writers.items()):
            if fn in readers or fn in allow_w or not is_data(fn):
                continue
            yield Finding(
                self.name, "info",
                "Data artifact '%s' is written but never read" % fn,
                "%d writer(s), 0 readers. Dead output or read via a path the "
                "extractor couldn't resolve statically." % len(locs),
                where=["%s:%s" % (l[1], l[2]) for l in locs[:4]],
                ids=["artifact:" + fn],
            )


_DEFAULT_SCRATCH = ["_legacy", "/tests/", "analyze_", "experiment_", "batch_",
                    "latexify_", "run_exp", "run_q", "create_mock", "generate_",
                    "simple_experiment"]


def _trunc(s, n=70):
    s = str(s).replace("\n", " ")
    return s if len(s) <= n else s[:n] + "..."


def _scalar(value_repr):
    """True if the literal repr looks like a config knob worth comparing
    (number, bool, or a short string/path) -- not a giant prompt or a big
    dict/list whose divergence is just different content."""
    if value_repr is None:
        return False
    v = value_repr.strip()
    if v in ("True", "False", "None"):
        return True
    try:
        float(v)
        return True
    except ValueError:
        pass
    if (v[:1] in "'\"") and len(v) <= 80:   # short string/path literal
        return True
    return False


class DivergentConstantLinter(Linter):
    """CATCHES: worker cap = 6 in boss.py vs 9 in claude_runner.py -> boss
    rejects whole batches early (79->0 backpressure); and two divergent copies of
    PROTOCOL_TRANSITIONS in pipeline_state.py vs state_machine.py. Rule: a
    constant NAME (or declared synonym-group) defined in >1 non-scratch module
    with non-equal literal values. Scalar knobs -> warning; structural (dict/list)
    -> info; giant strings (prompts) are ignored."""
    name = "divergent_constant"

    def run(self):
        scratch = self.cfg.get("scratch_markers", _DEFAULT_SCRATCH)
        # [[alias, canonical], ...] -- collapse filesystem-equivalent path
        # fragments (e.g. a `data` symlink to `data.default`) so two literals
        # that resolve to the same location aren't reported as divergent.
        aliases = self.semantic.get("path_aliases", []) or []

        def norm(v):
            if isinstance(v, str):
                for pair in aliases:
                    if len(pair) == 2:
                        v = v.replace(pair[0], pair[1])
            return v

        def is_scratch(d):
            return any(m in (d["file"] or "") for m in scratch)

        by_name = defaultdict(list)
        for n in self.store.nodes("config_const"):
            if not is_scratch(n):
                by_name[n["name"]].append(n)

        def emit(label, defs, synonyms):
            vals = defaultdict(list)
            for d in defs:
                a = d["attrs"] or {}
                if a.get("value") is None or a.get("value_kind") == "dynamic":
                    continue
                vals[norm(a["value"])].append(d)
            if len(vals) < 2:
                return []
            # distinct files only -- same value re-imported isn't divergence
            files = {d["file"] for ds in vals.values() for d in ds}
            if len(files) < 2:
                return []
            all_scalar = all(_scalar(v) for v in vals)
            structural = not all_scalar
            # giant strings (prompts/templates) -> skip unless a curated synonym set
            if structural and not synonyms:
                if any(len(v) > 400 and v[:1] in "'\"" for v in vals):
                    return []
            # Curated synonym groups are human-declared "these MUST agree" -> high
            # precision -> warning. Auto same-name detection is a heuristic hint
            # (DB_PATH=graph.db vs users.db are different DBs that share a name),
            # so it stays INFO to avoid name-collision false positives.
            if synonyms:
                sev = "warning"
            else:
                sev = "info"
            where = ["%s:%s=%s" % (d["file"], d["line"], _trunc(v))
                     for v, ds in vals.items() for d in ds]
            tag = "synonym set" if synonyms else "same name"
            return [Finding(
                self.name, sev,
                "Constant '%s' has divergent values across modules (%s)" % (label, tag),
                "values: %s. If these must agree, route them through one config "
                "source." % ", ".join(_trunc(v, 50) for v in sorted(vals.keys())),
                where=where[:6],
                ids=[d["id"] for ds in vals.values() for d in ds],
            )]

        seen = set()
        for gname, g in (self.semantic.get("constant_groups") or {}).items():
            members = [m for nm in g.get("names", []) for m in by_name.get(nm, [])]
            for f in emit(gname, members, True):
                seen.update(d["id"] for d in members)
                yield f
        for name, defs in by_name.items():
            if len(defs) < 2 or any(d["id"] in seen for d in defs):
                continue
            for f in emit(name, defs, False):
                yield f


class LoggerTrapLinter(Linter):
    """CATCHES: kg_patch_merger imports error_logger (installs root SqliteHandler)
    then relies on basicConfig -> all stdout/stderr silently swallowed for 16
    days. Rule: a module that calls the framework setup (basicConfig) OR installs
    the logger but has NO explicit stream handler is at risk of silent logs."""
    name = "logger_trap"

    def run(self):
        lg = self.cfg.get("logger", {}) or {}
        install_fn = lg.get("install_fn")
        # module relpath -> importer count, from the import-graph extractor.
        indeg = {}
        for m in self.store.nodes("module"):
            a = m["attrs"] or {}
            if "importers" in a:
                indeg[m["name"]] = a["importers"]
        for n in self.store.nodes("logger_install"):
            a = n["attrs"] or {}
            if a.get("has_explicit_handler") or not a.get("calls_setup"):
                continue
            # High precision: a basicConfig that only runs under a __main__ guard
            # of a module that does NOT import the installer is SAFE (standalone
            # CLI -> installer never fired in that process). Flag only when the
            # module imports the installer, or calls basicConfig at import time.
            if a.get("setup_in_main") and not a.get("imports_installer"):
                continue
            # Import-graph precision: a module nobody imports is an entrypoint --
            # it runs as its own process where install() never fired, so a
            # top-level basicConfig is safe (e.g. a subprocess CLI). It is a trap
            # only if it is imported as a library (in-degree > 0) or imports the
            # installer itself (and so triggers install() in its own process).
            importers = indeg.get(n["name"], 0)
            if importers == 0 and not a.get("imports_installer"):
                continue
            if a.get("imports_installer"):
                why = "imports the logger installer"
            else:
                why = ("is imported as a library (%d importer(s)) and calls it at "
                       "import time" % importers)
            yield Finding(
                self.name, "warning",
                "Module '%s' may log into the void" % n["name"],
                "Calls %s with no explicit StreamHandler and %s; if %s ran first "
                "it added a root handler that makes basicConfig a no-op. Add "
                "log.addHandler(StreamHandler())." % (
                    lg.get("framework_setup"), why, install_fn or "the installer"),
                where=["%s:%s" % (n["file"], n["line"])],
                ids=[n["id"]],
            )


class TestWritesProdLinter(Linter):
    """CATCHES: periodic test_critical.py runs every 30min and writes prod
    errors.db -> 3096 fake error rows. Rule: a test module writes an artifact
    whose basename matches a declared prod-path marker."""
    name = "test_writes_prod"

    def run(self):
        markers = [m for m in self.cfg.get("prod_path_markers", [])]
        test_markers = self.cfg.get("test_file_markers", [])
        if not markers:
            return
        for e in self.store.edges("writes"):
            f = (e["file"] or "")
            if not any(tm in f for tm in test_markers):
                continue
            fn = e["dst"].split("artifact:", 1)[-1]
            if any(fn in m or m.endswith(fn) for m in markers):
                yield Finding(
                    self.name, "error",
                    "Test module writes prod artifact '%s'" % fn,
                    "%s writes '%s' which matches a prod-path marker; isolate via "
                    "an env-overridable path so periodic test runs don't pollute "
                    "production data." % (f, fn),
                    where=["%s:%s" % (e["file"], e["line"])],
                    ids=[e["dst"]],
                )


class DeadModuleLinter(Linter):
    """CATCHES: a divergent legacy copy left in the tree (state_machine.py held a
    second PROTOCOL_TRANSITIONS that no longer matched pipeline_state.py;
    lib/dashboard_auth.py held a stale PUBLIC_PATHS auth-bypass set). Rule: a
    module nobody imports (in-degree 0), with no `__main__` guard, AND no
    top-level executable code is a *library nobody uses* -- it can neither be run
    nor imported, so its constants/logic silently rot. A guard-less script (has
    top-level exec, runs via `python3 file.py`) is NOT dead -- excluded."""
    name = "dead_module"

    def run(self):
        scratch = self.cfg.get("scratch_markers", _DEFAULT_SCRATCH)
        allow = set(self.semantic.get("allow_dead_modules", []))
        for m in self.store.nodes("module"):
            a = m["attrs"] or {}
            if "importers" not in a:               # only annotated module nodes
                continue
            rel = m["name"]
            if a["importers"] != 0 or a.get("has_main"):
                continue
            # a guard-less script with top-level work is a runnable entrypoint,
            # not a dead library -- don't flag it.
            if a.get("toplevel_exec"):
                continue
            if rel in allow or any(s in (rel or "") for s in scratch):
                continue
            if rel.endswith("__init__.py") or rel == "setup.py":
                continue
            yield Finding(
                self.name, "info",
                "Module '%s' is an unused library (never imported, not runnable)" % rel,
                "0 importers, no __main__ guard, no top-level code -- it can neither "
                "be imported nor run. Likely an orphan/legacy copy whose constants "
                "drift from the live module unnoticed (the state_machine.py / "
                "dashboard_auth.py trap). Delete it or wire it in.",
                where=[rel],
                ids=[m["id"]],
            )


ALL_LINTERS = [
    CompletionSignalLinter, OrphanArtifactLinter, DivergentConstantLinter,
    LoggerTrapLinter, TestWritesProdLinter, DeadModuleLinter,
]
