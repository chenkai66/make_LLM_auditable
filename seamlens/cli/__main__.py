"""seamlens CLI.

  python3 -m seamlens scan  <project>   build/refresh the system graph
  python3 -m seamlens lint  <project>   run all linters, print findings
  python3 -m seamlens diff  <project>   show node delta vs previous scan
  python3 -m seamlens query <project> --kind artifact   dump graph nodes
  python3 -m seamlens init  <project>   write a starter seamlens.yaml

  python3 -m seamlens atlas  <project>  architecture atlas: DOT + AI narrative
  python3 -m seamlens triage <project>  AI ranks each finding real vs false-positive
  python3 -m seamlens evolve <project>  GAN loop: AI proposes fixes, graph referees
  python3 -m seamlens audit  <project>  headless batch: AI auditor/architect read
                                        real source, sink verdicts into node_meta

These last four are the OPTIONAL AI layer (seamlens/ai + seamlens/live). They are
only wired in here and import lazily, so the core commands above never touch the
ai/live packages and run with zero AI configured.
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


def _collect_findings(cfg, store, semantic):
    """Run all linters, return findings sorted by severity. Shared by lint and
    the AI commands so they all see the identical deterministic finding set."""
    findings = []
    for L in ALL_LINTERS:
        lint = L(cfg, store, semantic)
        try:
            findings.extend(lint.run())
        except Exception as e:
            print("  ! linter %s failed: %s" % (L.name, e), file=sys.stderr)
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: order.get(f.severity, 9))
    return findings


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
    findings = _collect_findings(cfg, store, semantic)
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


def _require_scan(cfg, store):
    if store.current_run() is None:
        print("no scan yet; run `seamlens scan` first", file=sys.stderr)
        return False
    return True


def cmd_atlas(args):
    # lazy: keeps the ai package off the core import path
    from seamlens.ai.provider import Provider
    from seamlens.ai import atlas as _atlas
    cfg = Config.load(args.project, args.config)
    store = GraphStore(cfg.db_path)
    if not _require_scan(cfg, store):
        store.close(); return 2
    semantic = load_semantic(cfg)
    findings = _collect_findings(cfg, store, semantic)
    st = _atlas.stats(store, findings)
    dot = _atlas.build_dot(store, findings)
    outdir = cfg.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)
    dot_path = os.path.join(outdir, "atlas.dot")
    with open(dot_path, "w") as f:
        f.write(dot)
    narrative = None
    if not args.no_ai:
        prov = Provider.from_config(cfg)
        if prov.enabled and prov.available:
            narrative = _atlas.narrate(prov, st, findings, _atlas._graph_excerpt(store))
            if narrative is None:
                print("  (ai narrative skipped: %s)" % prov.last_error, file=sys.stderr)
        elif prov.enabled:
            print("  (ai enabled but unavailable: %s)" % prov.explain_unavailable(),
                  file=sys.stderr)
    md = _atlas.render_markdown(st, narrative)
    md_path = os.path.join(outdir, "atlas.md")
    with open(md_path, "w") as f:
        f.write(md)
    print("atlas -> %s" % dot_path)
    print("        %s" % md_path)
    print("  modules=%d artifacts=%d findings(e/w/i)=%d/%d/%d%s" % (
        st["modules"], st["artifacts"], st["findings"]["error"],
        st["findings"]["warning"], st["findings"]["info"],
        "  +narrative" if narrative else ""))
    store.close()
    return 0


def cmd_triage(args):
    from seamlens.ai.provider import Provider
    from seamlens.ai import triage as _triage
    cfg = Config.load(args.project, args.config)
    store = GraphStore(cfg.db_path)
    if not _require_scan(cfg, store):
        store.close(); return 2
    semantic = load_semantic(cfg)
    findings = _collect_findings(cfg, store, semantic)
    sev_filter = set(args.severity.split(",")) if args.severity else None
    if sev_filter:
        findings = [f for f in findings if f.severity in sev_filter]
    prov = Provider.from_config(cfg)
    if not (prov.enabled and prov.available):
        print("triage needs ai enabled+configured: %s" % prov.explain_unavailable(),
              file=sys.stderr)
        store.close(); return 2
    results = _triage.triage(prov, cfg, findings, limit=args.limit)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            print("[%s] verdict=%s conf=%.2f  %s" % (
                r["severity"], r["verdict"], r.get("confidence", 0), r["title"]))
            print("    reason: %s" % r.get("reason", ""))
            if r.get("fix_hint"):
                print("    fix:    %s" % r["fix_hint"])
            if r.get("precision_rule"):
                print("    rule:   %s" % r["precision_rule"])
        real = sum(1 for r in results if r["verdict"] == "real")
        fp = sum(1 for r in results if r["verdict"] == "false_positive")
        print("\n%d triaged: %d real, %d false-positive" % (len(results), real, fp))
    store.close()
    return 0


def cmd_evolve(args):
    from seamlens.ai.provider import Provider
    from seamlens.ai import evolve as _evolve
    cfg = Config.load(args.project, args.config)
    store = GraphStore(cfg.db_path)
    if not _require_scan(cfg, store):
        store.close(); return 2
    prov = Provider.from_config(cfg)
    if not (prov.enabled and prov.available):
        print("evolve needs ai enabled+configured: %s" % prov.explain_unavailable(),
              file=sys.stderr)
        store.close(); return 2
    store.close()
    return _evolve.run(cfg, prov, rounds=args.rounds, apply=args.apply,
                       severity=args.severity, extractors=EXTRACTORS,
                       linters=ALL_LINTERS)


def cmd_audit(args):
    # lazy: keeps the live package off the core import path (like atlas/triage)
    from seamlens.live import batch_audit as _audit
    cfg = Config.load(args.project, args.config)
    store = GraphStore(cfg.db_path)
    if not _require_scan(cfg, store):
        store.close(); return 2
    semantic = load_semantic(cfg)
    findings = _collect_findings(cfg, store, semantic)
    res = _audit.run(cfg, store, findings, git_rev=_git_rev(cfg.project_root),
                     max_nodes=args.max, with_reading=not args.no_reading,
                     bootstrap=args.bootstrap, lang=args.lang)
    store.close()
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    if not res.get("ok"):
        # graceful: AI unavailable is not a discriminator failure -- exit 0 so the
        # periodic script's deterministic lint result stands on its own.
        print("audit skipped: %s" % res.get("reason"), file=sys.stderr)
        return 0
    print("audited=%d skipped=%d candidates=%d new_risks=%d clean=%d" % (
        res["audited"], res["skipped"], res.get("candidates", 0),
        len(res["new_risks"]), len(res["clean"])))
    for r in res["new_risks"]:
        head = (r["risk"].splitlines()[0] if r["risk"] else "")
        print("  RISK %s -- %s" % (r["node"], head[:160]))
    return 0


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


def cmd_live(args):
    # lazy: keeps the live package (http server, ai) off the core import path
    from seamlens.live import install as _install
    cfg = Config.load(args.project, args.config)
    if args.install:
        _install.install(cfg.project_root, port=args.port)
        return 0
    # One-click: starting the companion also (idempotently) wires the CC hooks into
    # the watched project, so `seamlens live <project>` is the only command a new
    # user/project needs -- install + scan + serve + open. --no-install opts out.
    if not args.no_install:
        _install.install(cfg.project_root, port=args.port)
    # always (re)scan at startup so the live baseline reflects the CURRENT tree --
    # otherwise the first edit's delta would include everything that changed since
    # an older scan, instead of just that edit.
    print("scanning baseline ...")
    cmd_scan(argparse.Namespace(project=args.project, config=args.config,
                                note="live-baseline", quiet=True))
    from seamlens.live import server as _server
    _server.serve(cfg, port=args.port, open_ui=not args.no_browser, lang=args.lang)
    return 0


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

    # --- optional AI layer (lazy-imports seamlens.ai only when invoked) ---
    s = sub.add_parser("atlas"); add_common(s)
    s.add_argument("--outdir", default=".seamlens")
    s.add_argument("--no-ai", action="store_true")
    s.set_defaults(fn=cmd_atlas)
    s = sub.add_parser("triage"); add_common(s)
    s.add_argument("--severity", default="error,warning")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_triage)
    s = sub.add_parser("evolve"); add_common(s)
    s.add_argument("--rounds", type=int, default=1)
    s.add_argument("--apply", action="store_true")
    s.add_argument("--severity", default="error,warning")
    s.set_defaults(fn=cmd_evolve)
    s = sub.add_parser("audit"); add_common(s)
    s.add_argument("--max", type=int, default=6,
                   help="max nodes to audit this run (cost bound)")
    s.add_argument("--no-reading", action="store_true",
                   help="skip the architect 'reading' pass on brand-new modules")
    s.add_argument("--bootstrap", action="store_true",
                   help="fill the remaining budget with un-audited modules "
                        "(backfill a cold graph / slowly enrich a stable one)")
    s.add_argument("--lang", default="zh")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_audit)

    # --- live companion (browser god-view beside Claude Code) ---
    s = sub.add_parser("live"); add_common(s)
    s.add_argument("--install", action="store_true",
                   help="write the CC hook config into .claude/settings.local.json, then exit")
    s.add_argument("--no-install", action="store_true",
                   help="skip the automatic idempotent hook install done at startup")
    s.add_argument("--port", type=int, default=8722)
    s.add_argument("--lang", default="zh", help="default narration language (zh/en/ja/...)")
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(fn=cmd_live)

    args = p.parse_args(argv)
    rc = args.fn(args)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
