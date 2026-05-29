"""The GAN loop: AI proposes a fix, the DETERMINISTIC graph referees it.

Roles:
  * generator  = the LLM. For one real finding it proposes a minimal patch as a
    set of exact search/replace edits.
  * discriminator / referee = seamlens itself, run deterministically. The patch
    is applied to a *throwaway copy* of the project, the graph is re-scanned and
    re-linted, and the patch is ACCEPTED only if:
        (a) the targeted finding disappears, AND
        (b) no NEW error/warning finding appears.
    Because acceptance is decided by re-deriving the graph from source -- not by
    asking the model -- the generator cannot reward-hack the discriminator. The
    graph is ground truth.

Safety: default is DRY-RUN. Nothing touches the real tree unless --apply is
passed, and even then only patches the referee already accepted. Every attempt
(accepted or rejected, with the referee's reason) is appended to
`.seamlens/evolve_log.jsonl` for audit.
"""
import json
import os
import shutil
import sys
import tempfile
import time

from seamlens.core.config import Config
from seamlens.core.graph import GraphStore
from seamlens.semantic.loader import load_semantic


_GEN_SYS = (
    "You are a senior engineer fixing a verified static-analysis defect. You are "
    "given the finding and the source around each location. Propose the MINIMAL "
    "patch that removes the defect without changing unrelated behavior. Reply with "
    "STRICT JSON only: "
    '{"rationale": "<why this fixes it>", "edits": [{"file": "<repo-relative path>", '
    '"find": "<exact substring currently in the file>", "replace": "<replacement>"}]}. '
    "Each `find` MUST be an exact, unique substring of the current file (include "
    "enough surrounding text to be unique). Do not reformat unrelated code.")


def _scan_and_lint(cfg, extractors, linters):
    """Run a full scan + lint against `cfg` and return the findings list. Used for
    both the live baseline and the sandboxed referee re-check."""
    store = GraphStore(cfg.db_path)
    store.start_run(note="evolve")
    for Ex in extractors:
        Ex(cfg, store).run()
    store.commit_run()
    semantic = load_semantic(cfg)
    findings = []
    for L in linters:
        try:
            findings.extend(L(cfg, store, semantic).run())
        except Exception as e:
            print("  ! linter %s failed: %s" % (L.name, e), file=sys.stderr)
    store.close()
    return findings


def _key(f):
    """Stable identity of a finding across scans: linter + its graph ids + title.
    Independent of file:line churn from the patch itself."""
    return (f.linter, tuple(sorted(f.ids)), f.title)


def _copy_tree(src, dst, exclude):
    excl = set(exclude or [])

    def ignore(_dir, names):
        return [n for n in names if n in excl or n == ".seamlens"]

    shutil.copytree(src, dst, ignore=ignore, symlinks=True)


def _apply_edits(root, edits):
    """Apply search/replace edits under `root`. Returns (ok, message). Refuses
    unless every `find` matches exactly once -- ambiguous or missing matches abort
    the whole patch (no partial application)."""
    if not edits:
        return False, "no edits proposed"
    plan = []
    for ed in edits:
        rel = ed.get("file", "")
        find = ed.get("find", "")
        repl = ed.get("replace", "")
        if not rel or find == "":
            return False, "edit missing file/find"
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            return False, "file not found: %s" % rel
        with open(path, "r", errors="replace") as f:
            text = f.read()
        n = text.count(find)
        if n == 0:
            return False, "find not present in %s" % rel
        if n > 1:
            return False, "find ambiguous (%dx) in %s" % (n, rel)
        plan.append((path, text, text.replace(find, repl, 1)))
    for path, _old, new in plan:
        with open(path, "w") as f:
            f.write(new)
    return True, "applied %d edit(s)" % len(plan)


def _propose(provider, cfg, finding):
    from seamlens.ai.triage import _finding_context
    user = (
        "FINDING:\n  linter: %s\n  severity: %s\n  title: %s\n  detail: %s\n"
        "  where: %s\n\nSOURCE CONTEXT:\n%s\n" % (
            finding.linter, finding.severity, finding.title, finding.detail,
            ", ".join(finding.where) or "(none)",
            _finding_context(cfg, finding)))
    return provider.complete_json(_GEN_SYS, user)


def _referee(cfg, extractors, linters, target_key, base_ew_keys, edits):
    """Apply edits to a throwaway copy, re-derive the graph, and decide. Accepts
    only if the targeted finding disappears AND no new error/warning appears.
    Returns (accepted, reason, sandbox_findings_summary)."""
    root = cfg.project_root
    tmp = tempfile.mkdtemp(prefix="seamlens_evolve_")
    sand_root = os.path.join(tmp, "proj")
    try:
        _copy_tree(root, sand_root, cfg.get("exclude"))
        ok, msg = _apply_edits(sand_root, edits)
        if not ok:
            return False, "patch did not apply cleanly: %s" % msg, None
        # referee db lives inside the sandbox, never the real .seamlens
        sand_cfg = Config(sand_root, dict(cfg.data))
        sand_cfg.data["db_path"] = ".seamlens_evolve/graph.db"
        after = _scan_and_lint(sand_cfg, extractors, linters)
        after_keys = {_key(f) for f in after}
        if target_key in after_keys:
            return False, "target finding still present after patch", _summ(after)
        new_ew = [f for f in after
                  if f.severity in ("error", "warning") and _key(f) not in base_ew_keys]
        if new_ew:
            return (False,
                    "patch introduced %d new error/warning finding(s), e.g. %s" % (
                        len(new_ew), new_ew[0].title),
                    _summ(after))
        return True, "target closed; no new error/warning", _summ(after)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _summ(findings):
    sev = {"error": 0, "warning": 0, "info": 0}
    for f in findings:
        sev[f.severity] = sev.get(f.severity, 0) + 1
    return sev


def run(cfg, provider, rounds=1, apply=False, severity="error,warning",
        extractors=None, linters=None):
    from seamlens.ai import triage as _triage
    sev_filter = set(s for s in (severity or "").split(",") if s)
    log_path = cfg.abspath(".seamlens/evolve_log.jsonl")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    accepted_total = 0

    for rnd in range(1, rounds + 1):
        baseline = _scan_and_lint(cfg, extractors, linters)
        base_ew_keys = {_key(f) for f in baseline
                        if f.severity in ("error", "warning")}
        cand = [f for f in baseline if (not sev_filter or f.severity in sev_filter)]
        if not cand:
            print("round %d: no findings in scope (%s)" % (rnd, severity))
            break

        verdicts = _triage.triage(provider, cfg, cand, limit=len(cand))
        # pick the highest-severity, highest-confidence REAL finding
        order = {"error": 0, "warning": 1, "info": 2}
        reals = [(v, f) for v, f in zip(verdicts, cand) if v["verdict"] == "real"]
        reals.sort(key=lambda vf: (order.get(vf[1].severity, 9), -vf[0]["confidence"]))
        if not reals:
            print("round %d: triage found no real defects to fix" % rnd)
            break

        verdict, finding = reals[0]
        print("round %d: targeting [%s] %s (conf=%.2f)" % (
            rnd, finding.severity, finding.title, verdict["confidence"]))

        patch = _propose(provider, cfg, finding)
        edits = (patch or {}).get("edits") if isinstance(patch, dict) else None
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "round": rnd,
            "finding": {"linter": finding.linter, "severity": finding.severity,
                        "title": finding.title, "where": finding.where},
            "triage": {"confidence": verdict["confidence"], "reason": verdict["reason"]},
            "rationale": (patch or {}).get("rationale", "") if isinstance(patch, dict) else "",
            "n_edits": len(edits or []),
        }
        if not edits:
            rec.update(accepted=False, reason="generator produced no edits",
                       last_error=provider.last_error)
            _append(log_path, rec)
            print("  rejected: no edits (%s)" % (provider.last_error or "empty patch"))
            continue

        accepted, reason, after_summ = _referee(
            cfg, extractors, linters, _key(finding), base_ew_keys, edits)
        rec.update(accepted=accepted, reason=reason, after=after_summ)

        if accepted and apply:
            ok, msg = _apply_edits(cfg.project_root, edits)
            rec.update(applied=ok, apply_msg=msg)
            print("  ACCEPTED + APPLIED: %s" % msg)
            accepted_total += 1
        elif accepted:
            rec.update(applied=False, apply_msg="dry-run (use --apply to write)")
            print("  ACCEPTED (dry-run): %s" % reason)
            accepted_total += 1
        else:
            print("  rejected by referee: %s" % reason)
        _append(log_path, rec)

        # if dry-run, the real tree is unchanged so the next round would retarget
        # the same finding -- stop after the first to avoid a loop.
        if accepted and not apply:
            print("  (dry-run: stopping; re-run with --apply to commit fixes)")
            break

    print("\nevolve: %d patch(es) accepted across %d round(s). log -> %s" % (
        accepted_total, rounds, log_path))
    return 0


def _append(path, rec):
    with open(path, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
