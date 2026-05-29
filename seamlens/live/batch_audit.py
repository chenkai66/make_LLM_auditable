"""Headless batch auditor for the periodic discriminator.

Runs the live companion's AUDITOR (and the ARCHITECT on brand-new modules) over
the nodes that changed since the previous scan, reading the REAL source via a
Claude Code chain, and sinks each conclusion into the run-independent `node_meta`
table -- so the system graph accumulates "what the AI already learned" across
scans and restarts instead of re-reading cold every time.

Why reuse live/: cc_chain.CCChain + narrator.audit_change/narrate_change +
graphview.change_brief already do exactly the per-node read-source-and-conclude
work the live view does on each edit. Here we drive them in a BATCH from cron:
  * synthetic event (there is no hook firing) -- audit_change/narrate_change only
    need it for a one-line action label and tolerate delta=None;
  * incremental skip -- a node already carrying an auditor/architect verdict for
    the CURRENT git_rev is left alone, so an idle run (no code change) costs zero
    LLM calls;
  * bounded -- at most `max_nodes` per run;
  * bootstrap -- on a cold graph (or to slowly enrich a stable one) the remaining
    budget is filled with modules that have no meta yet, so coverage grows run by
    run until the whole graph is "known".

Optional/lazy exactly like atlas/triage: the 5 core commands never import this.
"""
import time

from seamlens.live import cc_chain as _cc
from seamlens.live import narrator as _narr
from seamlens.live import graphview as _gv

# A batch run has no real hook event; the roles only use it for an action label.
_AUDIT_EVENT = {"tool_name": "Audit", "hook_event_name": "Periodic"}
_INTENT = "(periodic discriminator audit -- no human goal stated)"
_META_KEYS = ("risk", "audited_clean", "reading")


def _already_audited(meta, git_rev):
    """True if this node already carries a verdict for the current code revision,
    so we don't re-spend an LLM call on source that hasn't changed since."""
    if not git_rev:
        return False
    for k in _META_KEYS:
        v = meta.get(k)
        if v and v.get("git_rev") == git_rev:
            return True
    return False


def _select(store, findings, max_nodes, bootstrap=False):
    """Candidate module nodes to audit this run, in priority order:
      1. modules ADDED since the previous scan (where new bugs enter),
      2. modules cited by any error/warning finding,
      3. (bootstrap only) modules with no meta yet -- backfill the cold graph.
    Returns (candidates[:max_nodes], added_set)."""
    module_ids = [n["id"] for n in store.nodes(kind="module")]
    mset = set(module_ids)
    added, _removed = store.diff_nodes()
    cand = [i for i in added if i in mset]
    seen = set(cand)
    for f in findings:
        if f.severity in ("error", "warning"):
            for i in f.ids:
                if i in mset and i not in seen:
                    seen.add(i)
                    cand.append(i)
    if bootstrap and len(cand) < max_nodes:
        known = store.all_meta()
        for i in module_ids:
            if len(cand) >= max_nodes:
                break
            if i not in seen and i not in known:
                seen.add(i)
                cand.append(i)
    return cand[:max_nodes], set(added)


def run(cfg, store, findings, git_rev="", max_nodes=6, with_reading=True,
        bootstrap=False, lang="zh"):
    """Audit up to max_nodes nodes, persisting verdicts into node_meta. Returns a
    summary dict; never raises on AI failure (degrades to ok=False)."""
    prov = _cc.CCChain.from_config(cfg, kind="qa")
    if not (prov and prov.available):
        return {"ok": False,
                "reason": prov.explain_unavailable() if prov else "no chain",
                "audited": 0, "skipped": 0, "new_risks": [], "clean": []}
    arch = _cc.CCChain.from_config(cfg, kind="narrate") if with_reading else None

    candidates, added = _select(store, findings, max_nodes, bootstrap=bootstrap)
    audited, skipped, failed, new_risks, clean = 0, 0, 0, [], []
    for node in candidates:
        if _already_audited(store.get_meta(node), git_rev):
            skipped += 1
            continue
        brief = _gv.change_brief(store, node)
        # `available` only proves `claude` is on PATH + enabled -- NOT that auth
        # works (e.g. on a server `claude` exists but isn't logged in until the
        # token env is injected). audit_change returns None for BOTH a genuine
        # NO_RISK and a failed call, so we must distinguish via last_error, or we
        # would falsely stamp `audited_clean` on a node the auditor never read.
        prov.last_error = None
        risks = _narr.audit_change(prov, _AUDIT_EVENT, brief, None, findings,
                                   lang=lang, intent=_INTENT)
        if risks is None and prov.last_error:
            failed += 1
            # first node already fails => the chain is down; abort without writing
            # anything (don't burn time, don't half-fill meta).
            if audited == 0:
                return {"ok": False, "reason": prov.last_error,
                        "audited": 0, "skipped": skipped, "failed": failed,
                        "new_risks": [], "clean": []}
            continue
        if risks:
            store.set_meta(node, "risk", risks, git_rev, "auditor")
            new_risks.append({"node": node, "risk": risks})
        else:
            store.set_meta(node, "audited_clean",
                           time.strftime("%Y-%m-%dT%H:%M:%S"), git_rev, "auditor")
            clean.append(node)
        # architect "reading" only for brand-new modules -- cheap, high signal.
        if arch and arch.available and node in added:
            reading = _narr.narrate_change(arch, _AUDIT_EVENT, None, findings,
                                           lang=lang, intent=_INTENT, story=None)
            if reading:
                store.set_meta(node, "reading", reading, git_rev, "architect")
        audited += 1
    return {"ok": True, "audited": audited, "skipped": skipped, "failed": failed,
            "new_risks": new_risks, "clean": clean,
            "candidates": len(candidates), "last_error": prov.last_error}
