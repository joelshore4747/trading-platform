#!/usr/bin/env bash
# Install trading_platform's systemd units. One script, two layouts.
#
#   deploy/install.sh --layout user                    # print the plan
#   deploy/install.sh --layout user --apply            # do it
#   deploy/install.sh --layout system --apply          # /opt, a service user, sudo
#
# It prints everything it will do and changes nothing without --apply, because
# the alternative is a script whose first run is also its first surprise.
#
# It never writes a credential. It creates the environment file empty, with
# variable names and no values, at 0600, outside the repository — and it never
# echoes the contents of that file, so a --apply run pasted into an issue does
# not leak anything.
#
# It refuses to enable a unit whose preflight fails. `scripts/migrate.py
# --check` and `run_collector.py --check` are the same checks the units run in
# ExecStartPre; running them here means a broken install fails in front of you
# rather than in a restart loop at 3am.
#
# Run it twice on a fresh machine. The first run has no credentials to find, so
# it creates the files, installs the units and stops before enabling; you fill
# in the environment file; the second run passes the gates and enables.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

LAYOUT=""
PREFIX=""
SVC_USER="trading"
CONFIG=""
ENVFILE=""
ONFAILURE="trading-alert@%n.service"
PYTHON=""
APPLY=0

die() { printf 'install.sh: %s\n' "$*" >&2; exit 2; }

usage() {
    sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<'USAGE'

Options:
  --layout user|system   REQUIRED. user: a --user session, no sudo, prefix is
                         this checkout. system: /opt, a service user, sudo.
  --prefix PATH          where the checkout lives on the target
  --user NAME            service account for the system layout (default: trading)
  --config PATH          collector config (default: <confdir>/collector.toml)
  --env-file PATH        credentials, 0600 (default: <confdir>/env)
  --on-failure UNIT      what to notify (default: trading-alert@%n.service)
  --python PATH          interpreter (default: <prefix>/.venv/bin/python)
  --apply                actually do it. Without this, nothing is written.
  -h, --help             this
USAGE
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --layout) LAYOUT="${2:-}"; shift 2 ;;
        --prefix) PREFIX="${2:-}"; shift 2 ;;
        --user) SVC_USER="${2:-}"; shift 2 ;;
        --config) CONFIG="${2:-}"; shift 2 ;;
        --env-file) ENVFILE="${2:-}"; shift 2 ;;
        --on-failure) ONFAILURE="${2:-}"; shift 2 ;;
        --python) PYTHON="${2:-}"; shift 2 ;;
        --apply) APPLY=1; shift ;;
        -h|--help) usage ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
done

case "$LAYOUT" in
    user)
        : "${PREFIX:=$REPO}"
        CONFDIR="${XDG_CONFIG_HOME:-$HOME/.config}/trading_platform"
        UNITDIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
        SYSTEMCTL=(systemctl --user)
        INSTALL_AS=()
        USERLINES=""
        # ProtectHome=true would hide the checkout, the config and the
        # environment file, all of which live under $HOME in this layout.
        PROTECTHOME=""
        WANTEDBY="default.target"
        AFTERDB=""
        ;;
    system)
        : "${PREFIX:=/opt/trading_platform}"
        CONFDIR="/etc/trading_platform"
        UNITDIR="/etc/systemd/system"
        SYSTEMCTL=(sudo systemctl)
        INSTALL_AS=(sudo)
        USERLINES="User=${SVC_USER}
Group=${SVC_USER}
"
        PROTECTHOME="ProtectHome=true
"
        WANTEDBY="multi-user.target"
        AFTERDB=" postgresql.service"
        ;;
    "") die "--layout is required (user or system). See --help." ;;
    *)  die "unknown layout: ${LAYOUT}. Use user or system." ;;
esac

: "${CONFIG:=$CONFDIR/collector.toml}"
: "${ENVFILE:=$CONFDIR/env}"
# Defaults to the venv inside the prefix, which is the normal case. Overridable
# because the interpreter is not always there: a tree checked out without its
# venv is a real configuration, and the units need an interpreter that exists
# rather than one that ought to.
: "${PYTHON:=$PREFIX/.venv/bin/python}"

UNITS=(trading-collector.service trading-reconcile.service trading-health.service "trading-alert@.service")
TIMERS=(trading-reconcile.timer trading-health.timer)
ENABLE=(trading-collector.service trading-reconcile.timer trading-health.timer)

say()  { printf '  %s\n' "$*"; }
head2() { printf '\n%s\n' "$*"; }

head2 "Plan (${LAYOUT} layout)"
say "checkout           ${PREFIX}"
say "interpreter        ${PYTHON}"
say "unit directory     ${UNITDIR}"
say "collector config   ${CONFIG}"
say "environment file   ${ENVFILE}   (0600, created empty, never printed)"
say "on failure         ${ONFAILURE}"
[ "$LAYOUT" = system ] && say "service account    ${SVC_USER}"
say "units              ${UNITS[*]} ${TIMERS[*]}"
say "enabled            ${ENABLE[*]}"

# -- Preflight: things that must already be true --------------------------------
head2 "Preflight"
fail=0
check() {
    if eval "$2"; then say "ok      $1"; else say "MISSING $1"; fail=1; fi
}
check "checkout at ${PREFIX}"        "[ -d '$PREFIX' ]"
check "interpreter ${PYTHON}"        "[ -x '$PYTHON' ]"
check "scripts/run_collector.py"     "[ -f '$PREFIX/scripts/run_collector.py' ]"
check "scripts/run_reconcile.py"     "[ -f '$PREFIX/scripts/run_reconcile.py' ]"
check "scripts/check_health.py"      "[ -f '$PREFIX/scripts/check_health.py' ]"
check "scripts/migrate.py"           "[ -f '$PREFIX/scripts/migrate.py' ]"
check "deploy/alert.sh executable"   "[ -x '$PREFIX/deploy/alert.sh' ]"
check "systemctl present"            "command -v systemctl >/dev/null"
if [ "$LAYOUT" = user ]; then
    check "user session bus"         "[ -n \"\${XDG_RUNTIME_DIR:-}\" ] && [ -S \"\${XDG_RUNTIME_DIR:-}/bus\" ]"
fi
[ "$fail" -eq 0 ] || die "preflight failed; nothing was changed"

# -- Render ---------------------------------------------------------------------
STAGE="$(mktemp -d)"
render() {
    local src="$1" dst="$2" c
    c="$(cat "$src")"
    c="${c//@PREFIX@/$PREFIX}"
    c="${c//@PYTHON@/$PYTHON}"
    c="${c//@CONFIG@/$CONFIG}"
    c="${c//@ENVFILE@/$ENVFILE}"
    c="${c//@ONFAILURE@/$ONFAILURE}"
    c="${c//@USERLINES@/$USERLINES}"
    c="${c//@PROTECTHOME@/$PROTECTHOME}"
    c="${c//@WANTEDBY@/$WANTEDBY}"
    c="${c//@AFTERDB@/$AFTERDB}"
    printf '%s\n' "$c" > "$dst"
    if grep -q '@[A-Z_]*@' "$dst"; then
        die "unresolved placeholder in $(basename "$dst"): $(grep -o '@[A-Z_]*@' "$dst" | sort -u | tr '\n' ' ')"
    fi
}
for u in "${UNITS[@]}"; do render "$REPO/deploy/${u}.in" "$STAGE/$u"; done
for t in "${TIMERS[@]}"; do cp "$REPO/deploy/$t" "$STAGE/$t"; done
say "rendered ${#UNITS[@]} unit(s) and ${#TIMERS[@]} timer(s) into ${STAGE}"

if [ "$APPLY" -eq 0 ]; then
    head2 "Dry run — nothing was written"
    say "rendered units are in ${STAGE} if you want to read them first:"
    say "  cat ${STAGE}/trading-collector.service"
    say "re-run with --apply to install."
    exit 0
fi

# -- Write ----------------------------------------------------------------------
head2 "Installing"
"${INSTALL_AS[@]}" mkdir -p "$CONFDIR" "$UNITDIR"

if [ -e "$ENVFILE" ]; then
    say "kept    ${ENVFILE} (already exists; not touched)"
else
    # Names only. A value written here by a script is a value in a shell
    # history, a scrollback and possibly a pasted log.
    "${INSTALL_AS[@]}" tee "$ENVFILE" >/dev/null <<'ENVSKEL'
# trading_platform credentials. 0600. Read by the systemd units.
# Values are read from the environment only; nothing loads this file from
# inside the repository. Fill these in, then re-run deploy/install.sh --apply.
TRADING_PLATFORM_DSN=
APCA_API_KEY_ID=
APCA_API_SECRET_KEY=
POLYGON_API_KEY=
FRED_API_KEY=
# Optional: POSTed by deploy/alert.sh when a unit fails.
TRADING_PLATFORM_ALERT_WEBHOOK=
ENVSKEL
    "${INSTALL_AS[@]}" chmod 600 "$ENVFILE"
    [ "$LAYOUT" = system ] && "${INSTALL_AS[@]}" chown "$SVC_USER:$SVC_USER" "$ENVFILE"
    say "created ${ENVFILE} (0600, empty values)"
fi

if [ -e "$CONFIG" ]; then
    say "kept    ${CONFIG} (already exists; not touched)"
else
    "${INSTALL_AS[@]}" cp "$PREFIX/config/collector.example.toml" "$CONFIG"
    say "created ${CONFIG} from config/collector.example.toml — EDIT THE SYMBOLS AND THE VENUE"
fi

for f in "${UNITS[@]}" "${TIMERS[@]}"; do
    "${INSTALL_AS[@]}" cp "$STAGE/$f" "$UNITDIR/$f"
    say "installed ${UNITDIR}/${f}"
done
"${SYSTEMCTL[@]}" daemon-reload
say "daemon-reload done"

# -- Gates: the same checks the units run, before anything is enabled -----------
head2 "Preflight against the real configuration"
gate=0
# shellcheck disable=SC1090
set -a; . "$ENVFILE"; set +a
if [ -z "${TRADING_PLATFORM_DSN:-}" ]; then
    say "SKIP    TRADING_PLATFORM_DSN is empty in ${ENVFILE}"
    gate=1
else
    if (cd "$PREFIX" && "$PYTHON" scripts/migrate.py --check >/dev/null 2>&1); then
        say "ok      schema is up to date"
    else
        say "FAIL    migrations pending or store unreachable — run: ${PYTHON} scripts/migrate.py --apply"
        gate=1
    fi
fi
if (cd "$PREFIX" && "$PYTHON" scripts/run_collector.py --config "$CONFIG" --check >/dev/null 2>&1); then
    say "ok      collector config and credentials validate"
else
    say "FAIL    collector --check failed — run it yourself to see why:"
    say "        cd ${PREFIX} && ${PYTHON} scripts/run_collector.py --config ${CONFIG} --check"
    gate=1
fi

if [ "$gate" -ne 0 ]; then
    head2 "Installed, NOT enabled"
    say "The units are in place and the preflight did not pass, so nothing was started."
    say "Fill in ${ENVFILE}, fix what is reported above, then re-run this script with --apply."
    exit 1
fi

head2 "Enabling"
"${SYSTEMCTL[@]}" enable --now "${ENABLE[@]}"
for u in "${ENABLE[@]}"; do
    say "$(${SYSTEMCTL[*]} is-active "$u" 2>/dev/null || true)  ${u}"
done

head2 "Done"
if [ "$LAYOUT" = user ]; then
    say "journalctl --user -u trading-collector -f"
    say "loginctl enable-linger \$USER   # so the units survive logout"
else
    say "sudo journalctl -u trading-collector -f"
fi
say "operations page: cd ${PREFIX} && ${PYTHON} scripts/run_ops.py"
