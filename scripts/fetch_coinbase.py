#!/usr/bin/env python
"""Download Coinbase Exchange history into the local Parquet cache.

    scripts/fetch_coinbase.py BTC-USD --interval 1d --start 2026-06-01
    scripts/fetch_coinbase.py ETH-USD --interval 1h --start 2026-09-01 --dry-run

No API key: `/products/{id}/candles` is public. This script accepts no
credentials and has no code path that could place an order.

**The product id is stored verbatim.** `BTC-USD` is quoted in dollars;
Binance's `BTCUSDT` is quoted in Tether, and the gap between them is the peg.
They are different instruments, they are cached under different symbols, and
nothing here normalises one to the other — see `live/coinbase.py`.

Coinbase's history depth is UNMEASURED. The truncation guard below is inherited
from `fetch_polygon.py`, where a free plan silently returned two years for a
six-year request (T-594716a0): a vendor that quietly shortens a window is worse
than one that refuses, because it succeeds. If Coinbase does the same, this
refuses and says by how much rather than writing a series nobody named.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_platform.core.events import Bar
from trading_platform.data.cache import cache_path, ensure_layout, merge_bars, read_bars, write_bars
from trading_platform.data.coinbase import CoinbaseParseError
from trading_platform.live.alpaca import TransportError, urllib_transport
from trading_platform.live.coinbase import CoinbaseMarketData

#: Crypto spot. Coinbase's derivatives are a different product and a different
#: adapter; filing them here would put them beside spot under one market key.
MARKET = "spot"

#: Inherited from fetch_polygon.py and set against calendar reasons rather than
#: a documented limit: a long weekend is 4 days, crypto never closes at all, and
#: a plan or retention limit costs years.
TRUNCATION_GRACE_DAYS = 10


def _parse_date(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"--start/--end must be ISO dates, got {value!r}") from None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Coinbase Exchange history into the local Parquet cache.",
        epilog="No API key needed — public endpoint. This script cannot trade.",
    )
    parser.add_argument("product", help="Coinbase product id, e.g. BTC-USD (NOT BTCUSDT)")
    parser.add_argument("--interval", default="1d", help="1m, 5m, 15m, 1h, 6h or 1d")
    parser.add_argument("--start", required=True, help="ISO date, inclusive")
    parser.add_argument("--end", default=None, help="ISO date, inclusive. Default: now")
    parser.add_argument("--cache", default="data/cache", help="cache root")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the request, touch no network"
    )
    parser.add_argument(
        "--allow-short",
        action="store_true",
        help="write even if the venue returned less history than asked for. Say it "
        "deliberately: a silently shortened window is a backtest nobody named.",
    )
    args = parser.parse_args(argv)

    start = _parse_date(args.start)
    end = _parse_date(args.end) if args.end else datetime.now(UTC)
    if end < start:
        raise SystemExit(f"--end {end.isoformat()} is before --start {start.isoformat()}")

    client = CoinbaseMarketData(transport=urllib_transport())

    if args.dry_run:
        try:
            print(f"would GET  {client.candles_url(args.product, args.interval, start, end)}")
        except CoinbaseParseError as exc:
            raise SystemExit(str(exc)) from None
        print(f"would write {cache_path(args.cache, MARKET, args.product, args.interval)}")
        print("no network touched")
        return 0

    print(
        f"Fetching {args.product} {args.interval} {start:%Y-%m-%d} -> {end:%Y-%m-%d}",
        file=sys.stderr,
    )
    try:
        bars: list[Bar] = client.fetch_symbol(args.product, args.interval, start, end)
    except CoinbaseParseError as exc:
        raise SystemExit(str(exc)) from None
    except TransportError as exc:
        status = getattr(exc, "status", None)
        print(f"\n{exc}", file=sys.stderr)
        if status == 403:
            print(
                "403 from Coinbase usually means the User-Agent was not sent. This client "
                "always sends one, so check for a proxy stripping headers.",
                file=sys.stderr,
            )
        elif status == 404:
            print(
                f"404: no product {args.product!r}. Coinbase ids look like BTC-USD, not BTCUSDT.",
                file=sys.stderr,
            )
        return 1

    if not bars:
        print("No candles returned. Check the product id and the window.", file=sys.stderr)
        return 1

    shortfall = (bars[0].timestamp - start).days
    if shortfall > TRUNCATION_GRACE_DAYS:
        print(
            f"\nREFUSED: asked for {start:%Y-%m-%d}, earliest candle is "
            f"{bars[0].timestamp:%Y-%m-%d} — {shortfall} days short.",
            file=sys.stderr,
        )
        print(
            "  Crypto trades every day, so this is retention or paging, not a market closure. "
            "Nothing was written.",
            file=sys.stderr,
        )
        if not args.allow_short:
            return 2
        print("  --allow-short given: keeping it.", file=sys.stderr)

    ensure_layout(args.cache)
    target = cache_path(args.cache, MARKET, args.product, args.interval)
    try:
        bars = merge_bars(read_bars(target, args.product), bars)
        print("  merged with existing cache", file=sys.stderr)
    except FileNotFoundError:
        pass

    write_bars(target, bars)
    print(f"\n{len(bars)} bars -> {target}")
    print(f"  {bars[0].timestamp.isoformat()} -> {bars[-1].timestamp.isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
