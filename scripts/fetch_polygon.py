#!/usr/bin/env python
"""Download Polygon history into the local Parquet cache.

    scripts/fetch_polygon.py AAPL --market equity --interval 1d --start 2024-01-01
    scripts/fetch_polygon.py SPY  --market etf    --interval 1m --start 2026-09-01
    scripts/fetch_polygon.py AAPL --market equity --interval 1d --start 2024-01-01 --dry-run

The equity counterpart to `fetch_binance.py`, and the reason it exists: both
Polygon modules were VERIFIED on the wire on 2026-09-08 and there was still no
way to get a single equity bar into this platform. The only Polygon entry point
was `run_collector.py`, which is live capture, and a free-tier key answers 403
to every live poll (T-73a2d1bc). So the verified adapter had no user.

Needs `POLYGON_API_KEY` in the environment. Nothing here can place an order.

Three things measured against a real free-tier key on 2026-09-08, which this
script is shaped by rather than merely documenting:

**The free tier refuses recent data.** A window ending five minutes ago returns
403; the identical window ending on a historical date returns bars. `--end`
therefore defaults to `RECENCY_GUARD_MIN` minutes ago rather than to now, and
says so when it moves. Asking for data the tier will not serve produces a 403
that looks like a broken key, and that is a bad half-hour to hand somebody.

**The free tier allows about 5 requests a minute.** Above that, 429. Requests
are paced at `MIN_REQUEST_INTERVAL_S` apart. Pacing rather than retrying on 429
is deliberate: a retry storm against a rate limit is how a key gets banned, and
the delay is the honest cost of the tier.

**A daily US equity bar is stamped midnight exchange-local, and that moves with
DST** — 05:00Z under EST, 04:00Z under EDT. Not a lookahead risk, since the
stamp always trails the session's real 16:00 ET close rather than preceding it,
but it means a daily equity bar is not the same window as a 24h crypto bar.
Intraday intervals carry none of this. Nothing here corrects it: the stamp is
what the venue said, and the place to reconcile calendars is a calendar, not a
download script.

`equity` and `etf` are separate markets in `cache.MARKETS` and this script will
not guess between them. SPY is an ETF; AAPL is not. They are stored apart
because a universe built by scanning the cache would otherwise silently mix
funds with the companies they hold.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_platform.core.events import Bar
from trading_platform.data.cache import cache_path, ensure_layout, merge_bars, read_bars, write_bars
from trading_platform.live.alpaca import TransportError, urllib_transport
from trading_platform.live.polygon import PolygonMarketData

#: Markets this script will write. `cache.MARKETS` also contains the crypto
#: keys and `option`; Polygon aggregates here are equities and funds, and a
#: `--market spot` typo should fail rather than file US stocks beside BTCUSDT.
MARKETS = ("equity", "etf")

#: How far back `--end` defaults to. The free tier's cutoff is documented as 15
#: minutes; 20 leaves room for clock skew between here and the venue, and the
#: cost of overshooting is one missing bar rather than a 403 that reads as a
#: broken key.
RECENCY_GUARD_MIN = 20

#: 5 requests/minute on the free tier, so 12s plus a margin. Paced, never
#: retried: a retry storm against a rate limit is how a key gets banned.
MIN_REQUEST_INTERVAL_S = 13.0

#: How far the first bar may trail the requested start before it is treated as
#: truncation rather than as the market being shut. A long weekend is 4 days and
#: the longest US market closure in living memory was 6 (September 2001), so 10
#: is comfortably clear of a calendar reason while still catching a plan limit,
#: which costs years rather than days.
TRUNCATION_GRACE_DAYS = 10


def _parse_date(value: str) -> datetime:
    """An ISO date or datetime, always tz-aware UTC. Naive input is refused."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"--start/--end must be ISO dates, got {value!r}") from None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _paced(transport_calls: list[float]) -> None:
    """Sleep until the next request would not exceed the free tier's rate."""
    if not transport_calls:
        return
    elapsed = time.monotonic() - transport_calls[-1]
    if elapsed < MIN_REQUEST_INTERVAL_S:
        time.sleep(MIN_REQUEST_INTERVAL_S - elapsed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Polygon history into the local Parquet cache.",
        epilog="Needs POLYGON_API_KEY. Read-only: this script cannot place an order.",
    )
    parser.add_argument("symbol", help="e.g. AAPL")
    parser.add_argument(
        "--market",
        choices=MARKETS,
        required=True,
        help="equity or etf. Required and never inferred: SPY is an ETF, AAPL is not, "
        "and they are stored apart so a universe scan cannot mix funds with companies.",
    )
    parser.add_argument("--interval", default="1d", help="1m, 1h, 1d, ...")
    parser.add_argument("--start", required=True, help="ISO date, inclusive")
    parser.add_argument(
        "--end",
        default=None,
        help=f"ISO date, inclusive. Default: {RECENCY_GUARD_MIN} minutes ago, because the "
        "free tier answers 403 for anything more recent.",
    )
    parser.add_argument("--cache", default="data/cache", help="cache root")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the request that would be made and touch no network",
    )
    parser.add_argument(
        "--allow-short",
        action="store_true",
        help="write the data even if the venue returned less history than asked for. "
        "Say this deliberately: the default refuses, because a silently shortened "
        "window is a backtest scored over a dataset nobody named.",
    )
    args = parser.parse_args(argv)

    start = _parse_date(args.start)
    guard = datetime.now(UTC) - timedelta(minutes=RECENCY_GUARD_MIN)
    if args.end is None:
        end = guard
        print(
            f"--end defaults to {end:%Y-%m-%d %H:%M}Z ({RECENCY_GUARD_MIN} min ago): the free "
            "tier refuses newer data with a 403.",
            file=sys.stderr,
        )
    else:
        end = _parse_date(args.end)
        if end > guard:
            print(
                f"warning: --end {end:%Y-%m-%d %H:%M}Z is within {RECENCY_GUARD_MIN} minutes of "
                "now. A free-tier key answers 403 to that; a paid one will not.",
                file=sys.stderr,
            )
    if end < start:
        raise SystemExit(f"--end {end.isoformat()} is before --start {start.isoformat()}")

    calls: list[float] = []
    inner = urllib_transport()

    def paced_transport(url: str, headers: object) -> bytes:
        _paced(calls)
        calls.append(time.monotonic())
        return inner(url, headers)  # type: ignore[arg-type]

    client = PolygonMarketData(transport=paced_transport)

    if args.dry_run:
        print(f"would GET  {client.bars_url(args.symbol, args.interval, start, end)}")
        print(f"would write {cache_path(args.cache, args.market, args.symbol, args.interval)}")
        print("no network touched")
        return 0

    print(
        f"Fetching {args.symbol} {args.interval} ({args.market}) "
        f"{start:%Y-%m-%d} -> {end:%Y-%m-%d}",
        file=sys.stderr,
    )
    try:
        bars: list[Bar] = client.fetch_symbol(args.symbol, args.interval, start, end)
    except TransportError as exc:
        status = getattr(exc, "status", None)
        print(f"\n{exc}", file=sys.stderr)
        if status == 403:
            print(
                "403 is this tier refusing the data, not a bad key: either the window is too "
                "recent, or the product (Indices, for one) is not on your plan.",
                file=sys.stderr,
            )
        elif status == 429:
            print(
                f"429 is the rate limit. Requests here are already paced "
                f"{MIN_REQUEST_INTERVAL_S:.0f}s apart; something else is using the same key.",
                file=sys.stderr,
            )
        return 1

    if not bars:
        print(
            "No bars returned. The window may be entirely weekends and holidays — "
            "US markets were shut 2026-09-05 to 2026-09-07, for instance.",
            file=sys.stderr,
        )
        return 1

    # A plan limit truncates the window and says nothing: HTTP 200, no warning,
    # no marker in the payload. Asking for five years and being handed two,
    # silently, is how a walk-forward comes to be scored over a different
    # dataset than the one its experiment record names (T-594716a0).
    #
    # Compared against the first bar rather than against a documented limit,
    # because the limit is a property of the account and changes when the plan
    # does. TRUNCATION_GRACE_DAYS absorbs the ordinary reasons a first bar
    # trails the request -- weekends, holidays, and an instrument that simply
    # did not trade that early.
    shortfall = (bars[0].timestamp - start).days
    if shortfall > TRUNCATION_GRACE_DAYS:
        print(
            f"\nREFUSED: asked for {start:%Y-%m-%d}, earliest bar is "
            f"{bars[0].timestamp:%Y-%m-%d} — {shortfall} days short.",
            file=sys.stderr,
        )
        print(
            "  The response was HTTP 200 with no warning, which is what makes this "
            "worth stopping for: a free Polygon plan caps history at about 2 years "
            "and truncates silently.",
            file=sys.stderr,
        )
        print(
            f"  Nothing was written. Re-run with --start {bars[0].timestamp:%Y-%m-%d} to "
            "accept the shorter window deliberately, or --allow-short to keep it as is.",
            file=sys.stderr,
        )
        if not args.allow_short:
            return 2
        print("  --allow-short given: keeping it.", file=sys.stderr)

    ensure_layout(args.cache)
    target = cache_path(args.cache, args.market, args.symbol, args.interval)
    try:
        bars = merge_bars(read_bars(target, args.symbol), bars)
        print("  merged with existing cache", file=sys.stderr)
    except FileNotFoundError:
        pass

    write_bars(target, bars)
    print(f"\n{len(bars)} bars -> {target}")
    print(f"  {bars[0].timestamp.isoformat()} -> {bars[-1].timestamp.isoformat()}")
    print(f"  {len(calls)} request(s), paced for the free tier")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
