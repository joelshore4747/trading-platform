#!/usr/bin/env python
"""Is the collector working? Exits non-zero when it is not.

    scripts/check_health.py            # one line per alert, 1 if any ALARM
    scripts/check_health.py --quiet    # print only when something is wrong

The operations page shows all of this already. A page is not an alert: nobody
was watching it at 04:00 when the feed stopped, and `CaptureStats` counted the
failures into a report that is printed at exit by a process designed never to
exit (T-f280e8f0). This is the same assessment with an exit code, so systemd,
cron or any monitor that understands "non-zero means broken" can be the one
watching.

Deliberately shares `assess` with the page rather than re-deciding what healthy
means. Two definitions drift, and then the page says BEHIND while the alarm
says ALARM in front of somebody at 3am.

Install it beside the collector:

    systemctl --user enable --now trading-health.timer

`deploy/trading-health.service` runs this; systemd's `OnFailure=` is what turns
the exit code into a message. Set `TRADING_PLATFORM_DSN` in its environment.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_platform.store import StoreError, connect, is_configured
from trading_platform.web.ops import Level, read_snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="print nothing when everything is fine (for cron, which mails any output)",
    )
    parser.add_argument(
        "--warn-is-failure",
        action="store_true",
        help="exit non-zero on a WARN too. Off by default: a WARN that pages somebody "
        "is a pager somebody silences.",
    )
    args = parser.parse_args(argv)

    if not is_configured():
        # Not "healthy". A check that cannot see the thing it checks must say so
        # and fail, or an unset variable reads as silence reads as fine.
        print("TRADING_PLATFORM_DSN is not set; this check cannot see anything.", file=sys.stderr)
        return 2

    try:
        with connect() as connection:
            snapshot = read_snapshot(connection)
    except StoreError as exc:
        print(f"ALARM  store unreachable — {exc}", file=sys.stderr)
        return 1

    alerts = snapshot.alerts
    alarms = [a for a in alerts if a.level is Level.ALARM]
    warns = [a for a in alerts if a.level is Level.WARN]

    if not alerts:
        if not args.quiet:
            feeds = len(snapshot.feeds)
            print(f"ok  {feeds} feed(s) live, {len(snapshot.runs)} run(s) reporting")
        return 0

    stream = sys.stderr if alarms else sys.stdout
    for alert in alerts:
        print(alert.one_line(), file=stream)
    return 1 if alarms or (warns and args.warn_is_failure) else 0


if __name__ == "__main__":
    raise SystemExit(main())
