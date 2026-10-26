#!/bin/bash
# The weekly static demo: rebuild the 7 weekday variants, run the full demo pipeline on each and on the combined dist,
# and exit 0 only if every stage is green, printing the dist path. No deploy, no cron install.
#
#   scripts/demo_weekly.sh [START]      START: the first day the build serves (YYYY-MM-DD, default today)
#
# Day START+i (i = 0..6) is built as the variant of its weekday k (Monday 0) with --seed k --today <that day>, so a
# visitor on that day sees it unshifted (demo/shim.js shifts any other day's dates to the visitor's today).
# Per variant: 01-build, 02-tests, 03-parity, 04-canary-synth, 05-canary-selftest, 06-canary-private, 07-verify.
# Then the combined dist: 08-combine, 09-tests-shared, 10-canary-synth, 11-canary-selftest, 12-canary-private,
# 13-verify (today's weekday variant, as a visitor gets it).
#
# Environment: HF_WEEKLY_OUT (default dist/weekly), HF_WEEKLY_LOGS (default $HF_WEEKLY_OUT/logs),
# HF_WEEKLY_EVIDENCE (demo_verify's screenshots, outside the repo; default $TMPDIR/hf-demo-weekly-evidence),
# HF_PRIVATE_WORDS (the owner's private-word list for the private canary; required, never printed).
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
OUT=${HF_WEEKLY_OUT:-$ROOT/dist/weekly}
LOGS=${HF_WEEKLY_LOGS:-$OUT/logs}
EVIDENCE=${HF_WEEKLY_EVIDENCE:-${TMPDIR:-/tmp}/hf-demo-weekly-evidence}
PRIV=${HF_PRIVATE_WORDS:-}
START=${1:-}

# The safe environment (as scripts/check.py): no provider keys, no Plaid/Alpaca, no real Hermes env file.
S=(env -u PLAID_CLIENT_ID -u PLAID_SECRET -u PLAID_ENV -u APCA_API_KEY_ID -u APCA_API_SECRET_KEY -u MARKETCHECK_API_KEY
   -u RENTCAST_API_KEY -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u OPENROUTER_API_KEY -u OPENROUTER_KEY
   -u PYTEST_ADDOPTS HERMES_PLAID=off HERMES_ALPACA=off HERMES_CHROME_DEVTOOLS=off HERMES_ENV_FILE=/nonexistent)
PY=(uv run --frozen python)

fail() { echo "demo_weekly: FAIL $*"; exit 1; }
[ -n "$PRIV" ] && [ -r "$PRIV" ] || fail "HF_PRIVATE_WORDS must name the readable private-word list"
case "$OUT" in "$ROOT"|"$ROOT/src"*|"$ROOT/demo"*|"$ROOT/scripts"*|"$ROOT/tests"*) fail "refusing to write into $OUT";; esac
case "$EVIDENCE" in "$ROOT"|"$ROOT"/*) fail "HF_WEEKLY_EVIDENCE must be outside the repo";; esac

# day i -> "k day" lines (weekday, date)
PLAN=$("${PY[@]}" - "$START" <<'EOF'
import sys
from datetime import date, timedelta
start = date.fromisoformat(sys.argv[1]) if sys.argv[1] else date.today()
for i in range(7):
    d = start + timedelta(days=i)
    print(d.weekday(), d.isoformat())
EOF
) || fail "bad START ${START:-}"
TODAY=$(date +%F)

for d in "$OUT/variants" "$OUT/demo" "$LOGS"/v[0-6] "$LOGS/all" "$EVIDENCE"/v[0-6] "$EVIDENCE/all"; do rm -rf "$d"; done   # (only its own)
mkdir -p "$OUT/variants" "$LOGS" "$EVIDENCE"

step() {   # step DIR NAME CMD... : one stage, its log in $LOGS/DIR/NAME.log, a line with its exit code and last line
  local dir=$1 n=$2; shift 2
  mkdir -p "$LOGS/$dir"
  "${S[@]}" "$@" > "$LOGS/$dir/$n.log" 2>&1
  local rc=$?
  echo "$dir $n exit $rc :: $(tail -1 "$LOGS/$dir/$n.log" | cut -c1-220)"
  return $rc
}

while read -r K DAY; do
  V=v$K D=$OUT/variants/v$K
  echo "== $V seed $K today $DAY"
  step $V 01-build "${PY[@]}" scripts/build_static_demo.py --synthetic-only --seed "$K" --today "$DAY" --out "$D" || fail "$V build"
  step $V 02-tests env HF_DEMO_SEED="$K" HF_DEMO_TODAY="$DAY" "${PY[@]}" -m pytest -q -p no:cacheprovider \
    tests/test_static_demo.py tests/test_demo_data.py tests/test_demo_ask.py || fail "$V tests"
  step $V 03-parity "${PY[@]}" scripts/demo_parity.py --seed "$K" --today "$DAY" || fail "$V parity"
  step $V 04-canary-synth "${PY[@]}" scripts/demo_canary.py "$D" --synthetic-only || fail "$V synthetic canary"
  step $V 05-canary-selftest "${PY[@]}" scripts/demo_canary_selftest.py "$D" --synthetic-markers || fail "$V canary selftest"
  step $V 06-canary-private "${PY[@]}" scripts/demo_canary.py "$D" --private "$PRIV" || fail "$V private canary"
  step $V 07-verify "${PY[@]}" scripts/demo_verify.py "$D" --out "$EVIDENCE/$V" || fail "$V verify"
done <<< "$PLAN"

C=all
step $C 08-combine "${PY[@]}" scripts/demo_combine.py "$OUT/demo" $(for k in 0 1 2 3 4 5 6; do printf '%s=%s ' $k "$OUT/variants/v$k"; done) \
  || fail "combine"
step $C 09-tests-shared "${PY[@]}" -m pytest -q -p no:cacheprovider tests/test_demo_variants.py tests/test_demo_worker.py \
  || fail "shared tests"
step $C 10-canary-synth "${PY[@]}" scripts/demo_canary.py "$OUT/demo" --synthetic-only || fail "combined synthetic canary"
step $C 11-canary-selftest "${PY[@]}" scripts/demo_canary_selftest.py "$OUT/demo" --synthetic-markers || fail "combined canary selftest"
step $C 12-canary-private "${PY[@]}" scripts/demo_canary.py "$OUT/demo" --private "$PRIV" || fail "combined private canary"
TODAYS=$("${PY[@]}" -c "from datetime import date; print(date.fromisoformat('$TODAY').weekday())")
step $C 13-verify "${PY[@]}" scripts/demo_verify.py "$OUT/demo" --variant "$TODAYS" --out "$EVIDENCE/all" || fail "combined verify"

echo "files $(find "$OUT/demo" -type f | wc -l | tr -d ' ') sha $(cd "$OUT/demo" && find . -type f -print0 | sort -z | xargs -0 shasum -a 256 | shasum -a 256 | cut -c1-16)"
echo "evidence $EVIDENCE"
echo "DEMO_WEEKLY_OK $OUT/demo"
