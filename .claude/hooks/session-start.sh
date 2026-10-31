#!/usr/bin/env bash
# SessionStart hook: make a fresh session immediately able to run the checks,
# and say what state the repository is in.
#
# Without this, a new web session starts with no .venv, so the first thing that
# happens is always the same handful of setup commands. Worse, the Stop hook
# that guards the lookahead tests silently does nothing when .venv is absent —
# so an unprepared session loses its most important safety net without saying so.
#
# Runs synchronously on purpose. Async would start the session sooner, but it
# races: Claude could reach for pytest while the install is still running, and
# a guard that is sometimes there is worse than one that always is.
#
# Not gated on $CLAUDE_CODE_REMOTE. The install is guarded by "does .venv
# already exist", which makes it a no-op on a machine that is already set up —
# and the command it runs is exactly the one README.md tells a human to run.
#
# Cannot block a session. Exit 0 always; failures are reported as context so
# Claude knows the environment is not ready rather than discovering it later.

set -uo pipefail

project_dir="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$project_dir" || exit 0

notes=()

# -- 1. Environment ----------------------------------------------------------
if [ -x "$project_dir/.venv/bin/python" ]; then
  notes+=("Environment: .venv ready ($("$project_dir/.venv/bin/python" --version 2>&1))")
elif command -v uv >/dev/null 2>&1; then
  if install_log=$(uv venv 2>&1 && uv pip install -e ".[dev]" 2>&1); then
    notes+=("Environment: built .venv with uv (was absent).")
  else
    notes+=("Environment: FAILED to build .venv. Tests and both guard hooks are")
    notes+=("  unavailable until this is fixed. Last lines:")
    notes+=("$(printf '%s' "$install_log" | tail -5 | sed 's/^/  /')")
  fi
else
  notes+=("Environment: no .venv and uv is not installed. Run: uv pip install -e \".[dev]\"")
fi

# -- 2. Where the work is ----------------------------------------------------
if git rev-parse --git-dir >/dev/null 2>&1; then
  branch=$(git branch --show-current 2>/dev/null || echo "detached")
  last=$(git log -1 --format='%h %s' 2>/dev/null || echo "no commits")
  dirty=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  notes+=("Branch: $branch — last commit: $last")
  [ "$dirty" != "0" ] && notes+=("Working tree: $dirty uncommitted change(s).")
fi

# -- 3. What is done and what is next ----------------------------------------
state=$(grep -m1 '^\*\*Current state:' docs/ROADMAP.md 2>/dev/null | tr -d '*')
[ -n "$state" ] && notes+=("$state")

next=$(grep -m1 '^[0-9]\+\. ▶' docs/ROADMAP.md 2>/dev/null | cut -c1-160)
[ -n "$next" ] && notes+=("Next up: $next")

# -- 4. Emit -----------------------------------------------------------------
# Plain stdout is added to Claude's context. Kept short: this is paid for on
# every single session, so it earns its place by being a few lines, not a brief.
printf '%s\n' "${notes[@]}"
echo "House rules are in CLAUDE.md. Run /session-brief for the full picture."

exit 0
