#!/usr/bin/env bash
# Continuous discriminator: re-derive the system graph and lint the seams.
# Read-only -- it never modifies code, only reports. New error/warning findings
# are the signal for the next co-evolution round (a regression to fix).
#
# Cron: */30 * * * * /data/seamlens/configs/seamlens_periodic.sh >> /tmp/seamlens_periodic.log 2>&1
set -euo pipefail

SEAMLENS_DIR=/data/seamlens
PROJECT=/data/research-agent
CONFIG="$PROJECT/seamlens.yaml"
REPORT_DIR="$PROJECT/logs"
REPORT="$REPORT_DIR/seamlens_findings.json"
WATCH="$REPORT_DIR/seamlens_watch.log"
mkdir -p "$REPORT_DIR"

cd "$SEAMLENS_DIR"
python3 -m seamlens scan "$PROJECT" --config "$CONFIG" --quiet >/dev/null
python3 -m seamlens lint "$PROJECT" --config "$CONFIG" --json > "$REPORT" 2>/dev/null

# Count actionable (error/warning) findings; INFO is advisory and not alerted on.
read -r NERR NWARN <<<"$(python3 - "$REPORT" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(sum(f["severity"] == "error" for f in d),
      sum(f["severity"] == "warning" for f in d))
PY
)"

TS="$(date '+%Y-%m-%dT%H:%M:%S')"
if [ "$NERR" -gt 0 ] || [ "$NWARN" -gt 0 ]; then
  echo "$TS REGRESSION: $NERR error(s), $NWARN warning(s) -- see $REPORT" >> "$WATCH"
  python3 - "$REPORT" <<'PY' >> "$WATCH"
import json, sys
for f in json.load(open(sys.argv[1])):
    if f["severity"] in ("error", "warning"):
        print("  [%s] %s | %s" % (f["severity"], f["linter"], f["title"]))
PY
else
  echo "$TS clean (0 error, 0 warning)" >> "$WATCH"
fi
