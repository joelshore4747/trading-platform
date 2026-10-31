#!/usr/bin/env bash
# PostToolUse hook (Write|Edit): caution when a financial or risk DEFAULT moved.
#
# Charter §69: an agent must never silently change fees, starting capital,
# slippage, spread, or a risk limit. Until now that was prose in CLAUDE.md.
# This makes it mechanical — for the four files where such defaults live.
#
# It CAUTIONS, it does not block. The write has already happened when a
# PostToolUse hook runs, and a guard that undoes edits is switched off within a
# day (docs/PROMPTS.md). Exit 2 here feeds stderr back to Claude as context;
# the edit stands, and the caution asks for the reason to be stated.
#
# System python3, not .venv: no third-party deps, and a fresh session may not
# have built the venv yet. Every failure path is silent — a crashed guard must
# never cost a turn.

set -uo pipefail

payload=$(cat)
project_dir="${CLAUDE_PROJECT_DIR:-$(pwd)}"

python3 - "$payload" "$project_dir" <<'PY'
import json
import re
import subprocess
import sys

try:
    data = json.loads(sys.argv[1])
    project = sys.argv[2]
except (json.JSONDecodeError, IndexError):
    sys.exit(0)

path = (data.get("tool_input") or {}).get("file_path", "")
if not path:
    sys.exit(0)

# The files that hold the defaults §69 names, and the field names in each.
WATCHED = {
    "engine/execution.py": ("commission_rate", "slippage_rate", "half_spread_rate", "minimum_commission"),
    "risk/limits.py": (
        "max_order_notional_pct", "max_position_notional_pct", "max_order_notional",
        "max_position_notional", "max_gross_leverage", "max_margin_utilisation",
        "max_drawdown_pct", "max_orders_per_bar",
    ),
    "core/instruments.py": ("initial_margin_rate",),
    "engine/backtester.py": ("starting_cash",),
}

matched = None
for suffix, fields in WATCHED.items():
    if path.replace("\\", "/").endswith(suffix):
        matched = (suffix, fields)
        break
if matched is None:
    sys.exit(0)

suffix, fields = matched
try:
    diff = subprocess.run(
        ["git", "-C", project, "diff", "-U0", "--", path],
        capture_output=True, text=True, timeout=10,
    ).stdout
except (subprocess.TimeoutExpired, FileNotFoundError):
    sys.exit(0)

# A default is a dataclass field with an `=`; only changed lines (+/-) count,
# and only ones naming a watched field.
pattern = re.compile(r"^[+-]\s*(" + "|".join(map(re.escape, fields)) + r")\s*:.*=\s*")
moved = sorted({m.group(1) for m in (pattern.match(line) for line in diff.splitlines()) if m})
if not moved:
    sys.exit(0)

sys.stderr.write(
    f"CAUTION (charter §69): a default in {suffix} changed: {', '.join(moved)}.\n"
    "Fees, capital, slippage and risk limits must never move silently. The edit\n"
    "stands — state the reason in your response and in the commit message, and if\n"
    "it makes a result look better, say so.\n"
)
sys.exit(2)
PY
# Propagate the guard's verdict. With set -u/-o pipefail but no -e, the script
# would otherwise fall through to a bare exit 0 and swallow the 2 — which is
# exactly what happened on first run: the caution was written and never
# delivered. A guard whose result is discarded is a guard that cannot fail.
exit $?
