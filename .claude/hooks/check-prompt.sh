#!/usr/bin/env bash
# UserPromptSubmit hook: note what a prompt's honest answer requires.
#
# NEVER blocks. Exit 2 on this event erases what the human typed, and a guard
# that deletes your sentence is switched off within a day — after which it
# guards nothing. The only lever used here is added context: Claude still sees
# the prompt exactly as written, plus a note about what answering it honestly
# demands.
#
# Deliberately uses system python3 rather than .venv: the guard has no
# third-party dependencies, and a fresh session may not have built the venv yet.
# A guard that only works once the environment is ready is not a guard.
#
# Exit 0 always. Every failure path is silent — see scripts/prompt_guard.py.

set -uo pipefail

project_dir="${CLAUDE_PROJECT_DIR:-$(pwd)}"
guard="$project_dir/scripts/prompt_guard.py"

[ -f "$guard" ] || exit 0

python3 "$guard" 2>/dev/null || true
exit 0
