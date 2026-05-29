"""Guards the load-bearing invariant: the seamlens CORE never depends on the
optional AI layer, and works with zero AI configured.

If someone adds `from seamlens.ai import ...` at the top of a core module, the
tool stops being installable/runnable AI-off -- which breaks the whole "the graph
is the asset, AI is an advisory lens" contract. This test fails loudly if that
happens.

Run: python3 -m pytest tests/test_core_independence.py   (or run directly)
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# every package EXCEPT seamlens/ai must be importable and AI-free.
CORE_DIRS = ["core", "extractors", "linters", "semantic"]


def _module_level_imports(path):
    """Return the set of module names imported at MODULE TOP LEVEL only.
    Imports nested inside a function/method body are deliberately ignored --
    that is exactly how the CLI is allowed to reach the ai layer lazily."""
    with open(path, "r", errors="replace") as f:
        tree = ast.parse(f.read(), filename=path)
    names = set()
    for node in tree.body:                     # top level only, not walk()
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    return names


def _iter_py(rel):
    base = os.path.join(ROOT, "seamlens", rel)
    for dirpath, _dirs, files in os.walk(base):
        for fn in files:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def test_core_has_no_module_level_ai_import():
    offenders = []
    for d in CORE_DIRS:
        for path in _iter_py(d):
            for name in _module_level_imports(path):
                if name == "seamlens.ai" or name.startswith("seamlens.ai."):
                    offenders.append((os.path.relpath(path, ROOT), name))
    assert not offenders, (
        "core modules must not import the AI layer at module level: %s" % offenders)


def test_cli_lazy_imports_ai_only_inside_functions():
    """The CLI MAY use the ai layer, but only lazily (inside command functions),
    so `python3 -m seamlens scan/lint` never touches the ai package."""
    path = os.path.join(ROOT, "seamlens", "cli", "__main__.py")
    for name in _module_level_imports(path):
        assert not (name == "seamlens.ai" or name.startswith("seamlens.ai.")), (
            "cli imports %s at module level; it must be lazy-imported inside the "
            "atlas/triage/evolve command functions" % name)


def test_scan_and_lint_work_with_ai_disabled(tmp_path=None):
    """Functional proof: a full scan + lint completes with no AI configured."""
    import tempfile
    import shutil
    from seamlens.core.config import Config
    from seamlens.core.graph import GraphStore
    from seamlens.semantic.loader import load_semantic
    from seamlens.cli.__main__ import EXTRACTORS, _collect_findings

    work = tempfile.mkdtemp(prefix="sl_indep_")
    try:
        # tiny fixture project: one module that writes a json another reads
        with open(os.path.join(work, "producer.py"), "w") as f:
            f.write("import json\n"
                    "def go():\n"
                    "    with open('out.json','w') as fh: json.dump({}, fh)\n")
        with open(os.path.join(work, "consumer.py"), "w") as f:
            f.write("import json\n"
                    "def go():\n"
                    "    with open('out.json') as fh: return json.load(fh)\n")
        cfg = Config(work, {
            "roots": ["."], "exclude": ["__pycache__", ".seamlens"],
            "io_helpers": {"read": ["json.load"], "write": ["json.dump"]},
            "logger": {"install_fn": None, "framework_setup": "logging.basicConfig",
                       "explicit_handler_markers": ["StreamHandler", "addHandler"]},
            "prod_path_markers": [], "test_file_markers": ["test_"],
            "fsm_sources": [], "scratch_markers": [],
            "semantic_overlay": "seamlens.semantic.yaml",
            "db_path": ".seamlens/system_graph.db",
        })
        store = GraphStore(cfg.db_path)
        store.start_run()
        for Ex in EXTRACTORS:
            Ex(cfg, store).run()
        store.commit_run()
        semantic = load_semantic(cfg)
        findings = _collect_findings(cfg, store, semantic)
        store.close()
        # the call simply must complete and return a list (AI never consulted)
        assert isinstance(findings, list)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_provider_degrades_gracefully_without_env():
    """With no env vars set, the provider reports unavailable and complete()
    returns None rather than raising -- so AI commands fall back cleanly."""
    from seamlens.core.config import Config
    from seamlens.ai.provider import Provider
    # config with ai enabled but env vars absent
    os.environ.pop("SEAMLENS_AI_BASE_URL", None)
    os.environ.pop("SEAMLENS_AI_KEY", None)
    cfg = Config(".", {"ai": {"enabled": True}})
    prov = Provider.from_config(cfg)
    assert prov.enabled is True
    assert prov.available is False
    assert prov.complete("sys", "user") is None
    assert "not set" in prov.explain_unavailable()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, "--", e)
        except Exception as e:
            failed += 1
            print("ERROR", fn.__name__, "--", type(e).__name__, e)
    sys.exit(1 if failed else 0)
