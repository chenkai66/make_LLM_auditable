"""Refresh the system graph after a tool action, and run the linters, so the live
view reflects the change the agent just made.

Why a full rescan rather than re-extracting only the edited file: several node
attributes are GLOBAL -- most importantly a module's importer count (in-degree),
which the dead_module / logger-trap linters key off. A single edit can change
another module's in-degree (add/remove an import), so a correct refresh must
re-run at least the imports extractor across the whole tree. A full scan of a
normal repo is sub-second; the server animates the graph delta off an in-memory
snapshot diff, so the human never waits on this. The expensive part (LLM
narration) is async and never blocks the rescan.
"""
from seamlens.core.graph import GraphStore
from seamlens.extractors.imports import ImportsExtractor
from seamlens.extractors.file_io import FileIOExtractor
from seamlens.extractors.constants import ConstantsExtractor
from seamlens.extractors.logger import LoggerExtractor
from seamlens.extractors.fsm import FSMExtractor
from seamlens.semantic.loader import load_semantic
from seamlens.linters.checks import ALL_LINTERS

# imports first: later linters key off the module in-degree it annotates.
EXTRACTORS = [ImportsExtractor, FileIOExtractor, ConstantsExtractor,
              LoggerExtractor, FSMExtractor]


def rescan(cfg, note="live"):
    """Run all extractors into a fresh run and commit. Returns (nodes, edges)."""
    store = GraphStore(cfg.db_path)
    try:
        store.start_run(note=note)
        for Ex in EXTRACTORS:
            Ex(cfg, store).run()
        store.commit_run()
        nc = sum(1 for _ in store.nodes())
        ec = sum(1 for _ in store.edges())
        return nc, ec
    finally:
        store.close()


def lint(cfg):
    """Run all linters against the current graph. Returns the findings list."""
    store = GraphStore(cfg.db_path)
    try:
        if store.current_run() is None:
            return []
        semantic = load_semantic(cfg)
        findings = []
        for L in ALL_LINTERS:
            try:
                findings.extend(L(cfg, store, semantic).run())
            except Exception:
                pass
        order = {"error": 0, "warning": 1, "info": 2}
        findings.sort(key=lambda f: order.get(f.severity, 9))
        return findings
    finally:
        store.close()
