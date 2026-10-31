#!/usr/bin/env bash
# PostToolUse hook (Write|Edit): format and lint the file that was just written.
#
# Keeping this in a hook rather than asking Claude to remember means formatting
# is never a thing anyone has to think about, and never a source of diff noise
# in a review.
#
# Exit 0 always: formatting is housekeeping and must not interrupt the session.
# Real lint failures surface on stderr, which Claude sees.

set -uo pipefail

payload=$(cat)
project_dir="${CLAUDE_PROJECT_DIR:-$(pwd)}"

file_path=$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    print((json.load(sys.stdin).get("tool_input") or {}).get("file_path", ""))
except Exception:
    print("")
' 2>/dev/null)

# Only Python files in this project.
case "$file_path" in
  *.py) ;;
  *) exit 0 ;;
esac
[ -f "$file_path" ] || exit 0

ruff="$project_dir/.venv/bin/ruff"
[ -x "$ruff" ] || exit 0   # Environment not built yet — nothing to do.

"$ruff" format --quiet "$file_path" 2>/dev/null
"$ruff" check --fix --quiet "$file_path" 2>/dev/null

# Report anything auto-fix could not resolve, so it is dealt with now
# rather than discovered at commit time.
remaining=$("$ruff" check --output-format=concise "$file_path" 2>/dev/null)
if [ -n "$remaining" ]; then
  echo "Ruff issues remain in $file_path:" >&2
  echo "$remaining" >&2
fi

exit 0
