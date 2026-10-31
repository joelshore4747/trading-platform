#!/usr/bin/env bash
# Announce that a unit failed. Invoked by trading-alert@.service via OnFailure=.
#
# Two deliberate choices. It logs at error level unconditionally, so the failure
# is in the journal whether or not anything else is configured — `journalctl -p
# err` finds it. And the webhook is optional and best-effort: an alerter that
# fails because its notification channel is down is an alerter that tells you
# nothing twice.
set -uo pipefail

unit="${1:-unknown}"
host="$(uname -n)"
message="trading_platform: ${unit} FAILED on ${host} at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Level 3 is err. systemd-cat routes it to the journal with that priority.
if command -v systemd-cat >/dev/null 2>&1; then
    printf '%s\n' "$message" | systemd-cat -t trading-alert -p err
else
    printf '%s\n' "$message" >&2
fi

if [ -n "${TRADING_PLATFORM_ALERT_WEBHOOK:-}" ] && command -v curl >/dev/null 2>&1; then
    # --max-time so a hanging endpoint cannot wedge the alert path. Failure here
    # is reported and never fatal: the journal line above already landed.
    if ! curl -fsS --max-time 10 \
        -H 'Content-Type: application/json' \
        -d "$(printf '{"text":%s}' "$(printf '%s' "$message" | sed 's/\\/\\\\/g; s/"/\\"/g; s/^/"/; s/$/"/')")" \
        "$TRADING_PLATFORM_ALERT_WEBHOOK" >/dev/null; then
        printf 'trading-alert: webhook POST failed; the journal line above still stands\n' >&2
    fi
fi

# Always succeed. A non-zero exit here would make systemd treat the *alerter*
# as failed, which is noise about the messenger rather than the message.
exit 0
