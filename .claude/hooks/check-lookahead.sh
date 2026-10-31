#!/usr/bin/env bash
# Stop hook: refuse to end a turn while the lookahead guards are failing.
#
# tests/test_lookahead.py encodes the one property that makes every other
# number in this repository meaningful. If it breaks, silently finishing the
# turn would leave a backtest engine that reports fiction.
#
# Exit 2 = block the stop and hand stderr back to Claude.

set -uo pipefail

payload=$(cat)
project_dir="${CLAUDE_PROJECT_DIR:-$(pwd)}"

# Never block twice in a row — this is the documented guard against a Claude
# that cannot fix the failure getting stuck in a stop loop.
already_active=$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("stop_hook_active", False))
except Exception:
    print(True)
' 2>/dev/null)
[ "$already_active" = "True" ] && exit 0

python="$project_dir/.venv/bin/python"
[ -x "$python" ] && [ -f "$project_dir/tests/test_lookahead.py" ] || exit 0

output=$(cd "$project_dir" && "$python" -m pytest tests/test_lookahead.py -q 2>&1)
status=$?

if [ $status -ne 0 ]; then
  echo "Lookahead guards are FAILING. Do not finish here." >&2
  echo "" >&2
  echo "$output" | tail -30 >&2
  echo "" >&2
  echo "These tests protect the one-bar delay between signal and fill." >&2
  echo "Fix the engine — do not edit the tests to match new behaviour." >&2
  exit 2
fi

exit 0
