#!/usr/bin/env bash
# AI-layer discriminator: agentic auditor reads REAL source and sinks verdicts
# into the run-independent node_meta table (the discriminator gets smarter over
# time). SEPARATE from the deterministic lint cron (seamlens_periodic.sh) and
# strictly subordinate to it: AI failure NEVER affects the lint signal.
#
# This box runs the research-agent autonomous loop with ~4 concurrent heavy
# `claude -p` experiments at steady state; a 5th agentic claude spikes RAM and
# gets OOM-killed. So we only launch the audit during a memory WINDOW (few
# experiment claudes + ample free RAM), waiting briefly for one, else we skip
# this round. Accumulation is opportunistic -- it fills node_meta during the
# lulls between research cycles.
#
# Cron (every 6h, off the :00 mark):  17 */6 * * * /data/seamlens/configs/seamlens_audit.sh >> /tmp/seamlens_audit_cron.log 2>&1
set -uo pipefail

SEAMLENS_DIR=/data/seamlens
PROJECT=/data/research-agent
CONFIG="$PROJECT/seamlens.yaml"
REPORT_DIR="$PROJECT/logs"
AUDIT_LOG="$REPORT_DIR/seamlens_audit.log"
WATCH="$REPORT_DIR/seamlens_watch.log"
KEYS=/root/openclaw-dashboard/api-keys.json
LOCK=/tmp/seamlens_audit.lock
mkdir -p "$REPORT_DIR"

# --- single-instance: a slow audit must never overlap the next cron tick ---
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date '+%F %T') audit: another run holds the lock -- skip" >> "$AUDIT_LOG"
  exit 0
fi

TS() { date '+%Y-%m-%dT%H:%M:%S'; }

# --- preflight: claude on PATH + a usable key, else this host has no AI layer ---
if ! command -v claude >/dev/null 2>&1; then
  echo "$(TS) audit: no claude on PATH -- skip" >> "$AUDIT_LOG"; exit 0
fi
if [ ! -r "$KEYS" ]; then
  echo "$(TS) audit: no key file ($KEYS) -- skip" >> "$AUDIT_LOG"; exit 0
fi

# --- memory window: wait up to ~5 min for the box to have headroom ---
MIN_AVAIL_MB=5200      # need real free RAM for a 5th agentic claude
MAX_PROCS=2            # at most this many experiment claudes already running
WIN_TRIES=10           # 10 * 30s = 5 min budget
have_window=0
for _ in $(seq "$WIN_TRIES"); do
  procs=$(pgrep -fc 'claude -p' 2>/dev/null || echo 0)
  avail=$(free -m | awk '/Mem:/{print $7}')
  if [ "$procs" -le "$MAX_PROCS" ] && [ "$avail" -ge "$MIN_AVAIL_MB" ]; then
    have_window=1; break
  fi
  sleep 30
done
if [ "$have_window" -ne 1 ]; then
  echo "$(TS) audit: no memory window (procs=$procs avail=${avail}MB) -- skip" >> "$AUDIT_LOG"
  exit 0
fi

# --- credentials: read a random INTL key + the anthropic base_url at run time.
# Exported into THIS process only -- never written to yaml/disk. ---
read -r BURL TOK < <(python3 - "$KEYS" <<'PY'
import json, random, sys
d = json.load(open(sys.argv[1]))
print(d["intl_anthropic_base_url"], random.choice(d["intl_keys"]))
PY
)
if [ -z "${TOK:-}" ] || [ -z "${BURL:-}" ]; then
  echo "$(TS) audit: could not load key/base_url -- skip" >> "$AUDIT_LOG"; exit 0
fi
export ANTHROPIC_BASE_URL="$BURL"
export ANTHROPIC_AUTH_TOKEN="$TOK"
export ANTHROPIC_MODEL="qwen3.7-max"

cd "$SEAMLENS_DIR" || exit 0
echo "$(TS) audit: window ok (procs=$procs avail=${avail}MB) -- running max=1 bootstrap" >> "$AUDIT_LOG"

# --max 1: one node per round keeps the footprint to a single extra claude.
# --bootstrap: backfill un-audited modules so a stable graph still enriches.
# || true: AI failure is not a discriminator failure -- the lint cron stands alone.
OUT=$(python3 -m seamlens audit "$PROJECT" --config "$CONFIG" \
        --bootstrap --max 1 --json 2>>"$AUDIT_LOG") || true
echo "$OUT" >> "$AUDIT_LOG"

# Surface a one-line outcome (and any new risk count) into the shared watch log.
python3 - "$WATCH" <<PY || true
import json, sys, time
ts = time.strftime("%Y-%m-%dT%H:%M:%S")
w = open(sys.argv[1], "a")
try:
    r = json.loads('''$OUT''')
except Exception:
    w.write("%s audit: no parseable result\n" % ts); sys.exit(0)
if not r.get("ok"):
    w.write("%s audit skipped (%s)\n" % (ts, r.get("reason", "?"))); sys.exit(0)
nr = len(r.get("new_risks") or [])
w.write("%s audit: audited=%d skipped=%d new_risks=%d clean=%d\n" % (
    ts, r.get("audited", 0), r.get("skipped", 0), nr, len(r.get("clean") or [])))
for it in (r.get("new_risks") or []):
    head = (it.get("risk") or "").splitlines()[0] if it.get("risk") else ""
    w.write("  AUDIT-RISK %s -- %s\n" % (it.get("node"), head[:160]))
PY
exit 0
