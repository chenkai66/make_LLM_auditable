"""seamlens CLI.

  python3 -m seamlens scan  <project>   build/refresh the system graph
  python3 -m seamlens lint  <project>   run all linters, print findings
  python3 -m seamlens diff  <project>   show node delta vs previous scan
  python3 -m seamlens query <project> --kind artifact   dump graph nodes
  python3 -m seamlens init  <project>   write a starter seamlens.yaml
"""
import argparse
import json
import os
import subprocess
import sys

from seamlens.core.config import Config
from seamlens.core.graph import GraphStore
from seamlens.extractors.file_io import FileIOExtractor
from seamlens.extractors.constants import ConstantsExtractor
from seamlens.extractors.logger import LoggerExtractor
from seamlens.extractors.fsm import FSMExtractor
from seamlens.extractors.imports import ImportsExtractor
from seamlens.semantic.loader import load_semantic
from seamlens.linters.checks import ALL_LINTERS

# imports runs first: later linters key off the module in-degree it annotates.
EXTRACTORS = [ImportsExtractor, FileIOExtractor, ConstantsExtractor,
              LoggerExtractor, FSMExtractor]


def _git_rev(root):
    try:
        return subprocess.check_output(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return ""


def cmd_scan(args):
    cfg = Config.load(args.project, args.config)
    store = GraphStore(cfg.db_path)
    store.start_run(git_rev=_git_rev(cfg.project_root), note=args.note or "")
    for Ex in EXTRACTORS:
        ex = Ex(cfg, store)
        ex.run()
        if not args.quiet:
            print("  + extractor:", ex.name)
    store.commit_run()
    rid = store.current_run()
    nc = sum(1 for _ in store.nodes())
    print("scanned -> %s (run %s): %d nodes, %d edges" % (
        cfg.db_path, rid, nc, sum(1 for _ in store.edges())))
    store.close()


def cmd_lint(args):
    cfg = Config.load(args.project, args.config)
    store = GraphStore(cfg.db_path)
    if store.current_run() is None:
        print("no scan yet; run `seamlens scan` first", file=sys.stderr)
        return 2
    semantic = load_semantic(cfg)
    findings = []
    for L in ALL_LINTERS:
        lint = L(cfg, store, semantic)
        try:
            findings.extend(lint.run())
        except Exception as e:
            print("  ! linter %s failed: %s" % (L.name, e), file=sys.stderr)
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: order.get(f.severity, 9))
    if args.json:
        print(json.dumps([f.as_dict() for f in findings], ensure_ascii=False, indent=2))
    else:
        if not findings:
            print("clean: no findings")
        for f in findings:
            print(f)
        n_err = sum(1 for f in findings if f.severity == "error")
        n_warn = sum(1 for f in findings if f.severity == "warning")
        print("\n%d findings (%d error, %d warning)" % (len(findings), n_err, n_warn))
    store.close()
    return 1 if any(f.severity == "error" for f in findings) and args.strict else 0


def cmd_diff(args):
    cfg = Config.load(args.project, args.config)
    store = GraphStore(cfg.db_path)
    added, removed = store.diff_nodes()
    print("=== blast radius vs previous scan ===")
    print("added   (%d):" % len(added))
    for i in sorted(added)[:60]:
        print("  + " + i)
    print("removed (%d):" % len(removed))
    for i in sorted(removed)[:60]:
        print("  - " + i)
    store.close()


def cmd_query(args):
    cfg = Config.load(args.project, args.config)
    store = GraphStore(cfg.db_path)
    for n in store.nodes(args.kind):
        print("%-16s %-40s %s:%s" % (n["kind"], n["name"], n["file"], n["line"]))
    store.close()


def cmd_init(args):
    dst = os.path.join(os.path.abspath(args.project), "seamlens.yaml")
    if os.path.exists(dst) and not args.force:
        print("exists (use --force):", dst); return
    with open(dst, "w") as f:
        f.write(_STARTER_YAML)
    print("wrote", dst)


_STARTER_YAML = """# seamlens project config -- the only host-aware surface.
roots: ["."]
exclude: ["__pycache__", ".git", "node_modules", ".seamlens", "venv"]
io_helpers:
  read:  ["read_json", "load_json", "json.load"]
  write: ["write_json", "save_json", "dump_json", "atomic_write"]
logger:
  install_fn: null              # e.g. "error_logger.install"
  framework_setup: "logging.basicConfig"
prod_path_markers: []           # e.g. ["errors.db", "graph.db"]
fsm_sources: []                 # e.g. ["state.py:TRANSITIONS"]
semantic_overlay: "seamlens.semantic.yaml"
db_path: ".seamlens/system_graph.db"
"""


def main(argv=None):
    p = argparse.ArgumentParser(prog="seamlens")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp):
        sp.add_argument("project")
        sp.add_argument("--config", default=None)

    s = sub.add_parser("scan"); add_common(s)
    s.add_argument("--note", default=""); s.add_argument("--quiet", action="store_true")
    s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("lint"); add_common(s)
    s.add_argument("--json", action="store_true"); s.add_argument("--strict", action="store_true")
    s.set_defaults(fn=cmd_lint)
    s = sub.add_parser("diff"); add_common(s); s.set_defaults(fn=cmd_diff)
    s = sub.add_parser("query"); add_common(s)
    s.add_argument("--kind", default=None); s.set_defaults(fn=cmd_query)
    s = sub.add_parser("init"); add_common(s)
    s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_init)

    args = p.parse_args(argv)
    rc = args.fn(args)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
