"""AI triage: rank each deterministic finding real vs false-positive.

The linters are tuned for precision already (error/warning are high-confidence;
info is advisory). Triage is the *discriminator* half of the GAN: it reads the
finding plus the actual source around each `where` location and returns a
structured verdict. Two things make it safe to trust:

  * It never invents findings -- it can only judge the deterministic ones it is
    handed. The graph stays ground truth; the model only labels.
  * Every verdict carries a `precision_rule` suggestion: if the model says
    false_positive, it proposes the guard the linter should add so the SAME
    false positive never recurs. That feedback sharpens the deterministic layer
    over time instead of routing around it.

Degrades to nothing useful only if AI is unavailable -- callers gate on that.
"""
import os

_SYS = (
    "You are a precise static-analysis triager. You are given ONE finding from a "
    "deterministic code auditor plus the source code around each cited location. "
    "Decide whether the finding is a REAL defect or a FALSE_POSITIVE. Be "
    "conservative: only call it real if the code actually exhibits the described "
    "seam bug. Reply with STRICT JSON only, no prose, of the form: "
    '{"verdict": "real"|"false_positive", "confidence": 0.0-1.0, '
    '"reason": "<one sentence grounded in the shown code>", '
    '"fix_hint": "<concrete fix, or empty>", '
    '"precision_rule": "<if false_positive: the guard the linter should add to '
    'stop flagging this pattern; else empty>"}')


def _read_context(cfg, where, radius=8):
    """Given a 'file:line' (or bare 'file') location, return a short code excerpt
    centered on the line. Paths are project-root relative; resolve via cfg."""
    raw = where
    line = None
    # split on the LAST colon so windows-y paths or 'file:line=val' still work
    if ":" in where:
        head, _, tail = where.rpartition(":")
        digits = tail.split("=", 1)[0].strip()
        if head and digits.isdigit():
            raw, line = head, int(digits)
    path = cfg.abspath(raw)
    if not os.path.isfile(path):
        return "%s: (source not found)" % where
    try:
        with open(path, "r", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return "%s: (unreadable: %s)" % (where, e)
    if line is None:
        snippet = "".join(lines[:radius * 2])
        return "%s (head):\n%s" % (raw, snippet.rstrip())
    lo = max(0, line - radius - 1)
    hi = min(len(lines), line + radius)
    out = []
    for i in range(lo, hi):
        mark = ">>" if (i + 1) == line else "  "
        out.append("%s %5d  %s" % (mark, i + 1, lines[i].rstrip("\n")))
    return "%s:%d\n%s" % (raw, line, "\n".join(out))


def _finding_context(cfg, f, max_locs=4):
    parts = []
    for w in f.where[:max_locs]:
        parts.append(_read_context(cfg, w))
    return "\n\n".join(parts) or "(no source locations)"


def triage(provider, cfg, findings, limit=20):
    """Triage up to `limit` findings. Returns a list of result dicts that merge
    the finding's own fields with the model verdict. Findings the model can't be
    scored on (AI failure) get verdict='unknown' so nothing is silently dropped."""
    results = []
    for f in findings[:limit]:
        ctx = _finding_context(cfg, f)
        user = (
            "FINDING:\n"
            "  linter:   %s\n"
            "  severity: %s\n"
            "  title:    %s\n"
            "  detail:   %s\n"
            "  where:    %s\n\n"
            "SOURCE CONTEXT:\n%s\n" % (
                f.linter, f.severity, f.title, f.detail,
                ", ".join(f.where) or "(none)", ctx))
        verdict = provider.complete_json(_SYS, user)
        base = {
            "linter": f.linter, "severity": f.severity, "title": f.title,
            "where": f.where, "ids": f.ids,
        }
        if not isinstance(verdict, dict):
            base.update(verdict="unknown", confidence=0.0,
                        reason="ai triage failed: %s" % (provider.last_error or "no/invalid JSON"),
                        fix_hint="", precision_rule="")
        else:
            v = str(verdict.get("verdict", "unknown")).lower()
            if v not in ("real", "false_positive", "unknown"):
                v = "unknown"
            try:
                conf = float(verdict.get("confidence", 0.0))
            except (TypeError, ValueError):
                conf = 0.0
            base.update(
                verdict=v, confidence=max(0.0, min(1.0, conf)),
                reason=str(verdict.get("reason", "")),
                fix_hint=str(verdict.get("fix_hint", "")),
                precision_rule=str(verdict.get("precision_rule", "")))
        results.append(base)
    return results
