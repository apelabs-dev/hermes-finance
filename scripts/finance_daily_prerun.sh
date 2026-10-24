#!/usr/bin/env bash
# hermes-finance daily pre-run: sync banks, apply loan/card payments, refresh car/property estimates, snapshot the
# Alpaca live account (TASK-19, read-only),
# build AI classification queue, check alerts.
# Portable, opt-in example for a self-hoster. This file does not install or activate a schedule.
# Run only after reviewing the commands: FINANCE_DAILY_ENABLE=1 bash scripts/finance_daily_prerun.sh
# `finance revalue` asks Carfax (cars) and Redfin (homes)
# only (TASK-13b); the pictures are fetched on the dashboard (a save, the first page view) or by `finance photos`.
# Prints a SHORT summary + file paths only (never the whole queue JSON).
set -uo pipefail
if [[ "${FINANCE_DAILY_ENABLE:-0}" != 1 ]]; then
  echo "Daily pre-run disabled; review this script, then set FINANCE_DAILY_ENABLE=1 to run it."
  exit 0
fi
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd) || exit 1
cd "${FINANCE_APP_DIR:-$ROOT}" || { echo "PRE-RUN ERROR: project dir missing"; exit 1; }
F=${FINANCE_CLI:-.venv/bin/finance}
OUT=${FINANCE_DAILY_OUT:-${XDG_CACHE_HOME:-$HOME/.cache}/hermes-finance/daily}
mkdir -p "$OUT" || exit 1
chmod 700 "$OUT" || exit 1
API=${FINANCE_DAILY_API_URL:-http://127.0.0.1:8799}
D=$(date +%F)
echo "== date: $(date '+%A %F %H:%M %Z')"
echo "== sync"
$F sync 2>&1 | tail -12
echo "== loan payments"
$F match-payments 2>&1 | tail -8
echo "== card autopay suggestions (never saved; ask the owner before saving any)"
$F match-payments --suggest 2>&1 | tail -8
echo "== car / property estimates (monthly; within the free tier, non-fatal)"
$F revalue 2>&1 | tail -8
echo "== Alpaca live account (read-only API snapshot; nothing to do without its key file; non-fatal)"
$F alpaca-sync 2>&1 | tail -3
echo "== classification queue"
if $F ai-queue --max-groups 120 > "$OUT/queue-$D.json" 2> "$OUT/queue-$D.err"; then
  python3 - "$OUT/queue-$D.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
print(f"run_id={d.get('run_id')} groups={len(d.get('groups',[]))} queued_txns={d.get('queued_txns')} truncated={d.get('truncated')}")
PY
  echo "queue_file=$OUT/queue-$D.json"
else
  echo "ai-queue FAILED: $(tail -3 "$OUT/queue-$D.err")"
fi
echo "== alerts (from live dashboard API)"
if curl -sf -m 20 -o "$OUT/overview-$D.json" "$API/api/overview"; then
  python3 - "$OUT/overview-$D.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
al=d.get("alerts") or []
print(f"alerts={len(al)}")
for a in al[:15]: print(" -", json.dumps(a)[:300])
c=d.get("connections") or {}
print(f"connections total={c.get('total')} broken={c.get('broken')}")
PY
else
  echo "dashboard API unreachable at $API — alerts not checked"
fi
