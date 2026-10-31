#!/usr/bin/env python3
"""Download Binance history into the local Parquet cache.

    python scripts/fetch_binance.py BTCUSDT --interval 1d --start 2020-01-01
    python scripts/fetch_binance.py BTCUSDT --market um --interval 1h --start 2023-01-01
    python scripts/fetch_binance.py BTCUSDT --market um --funding --start 2023-01-01

⚠️  **This script has never been run against the live Binance API.** It was
    written in an environment whose egress policy blocks ``api.binance.com``
    and ``fapi.binance.com`` (403 at CONNECT), so only its parsing half —
    ``trading_platform.data.binance`` — is covered by tests. The request and
    paging logic is written from Binance's published documentation and is
    unverified against a real response.

    If a response shape differs from what this expects, that is the thing to
    report back. The parser is the part that has been proven; this is the part
    that has not.

No API key is required: klines and funding history are public endpoints. This
script deliberately accepts no credentials, and there is no code path here that
could place an order — see docs/PLAN.md on why live trading is absent.

Only the standard library is used for HTTP, so running this needs no extra
dependency beyond what the package already installs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trading_platform.data.binance import (
    interval_ms,
    klines_to_bars,
    observed_funding_interval_hours,
    parse_funding_rates,
)
from trading_platform.data.cache import (
    cache_path,
    ensure_layout,
    funding_path,
    merge_bars,
    read_bars,
    write_bars,
)

# data-api.binance.vision is Binance's documented market-data-only host. It
# carries no account endpoints at all, which is the right blast radius for a
# script that should never be able to trade.
HOSTS = {
    "spot": "https://data-api.binance.vision",
    "um": "https://fapi.binance.com",
    "cm": "https://dapi.binance.com",
}
KLINE_PATHS = {
    "spot": "/api/v3/klines",
    "um": "/fapi/v1/klines",
    "cm": "/dapi/v1/klines",
}
FUNDING_PATH = "/fapi/v1/fundingRate"

#: Spot caps at 1000. Futures documentation disagrees with itself (1000 vs
#: 1500), so we use the value both sources agree is safe.
PAGE_LIMIT = 1000

USER_AGENT = "trading-platform/0.1 (backtest data loader)"
MAX_RETRIES = 5


class RateLimitBan(RuntimeError):
    """HTTP 418: the IP is banned. Not retryable — stop immediately."""


def _get(url: str, params: dict[str, Any]) -> Any:
    """One GET with Binance's documented backoff behaviour.

    429 means back off and honour ``Retry-After``. 418 means the IP is already
    banned and further requests extend the ban — bans scale from 2 minutes to
    3 days for repeat offenders, so this raises rather than retrying.
    """
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    full = f"{url}?{query}"

    for attempt in range(MAX_RETRIES):
        request = urllib.request.Request(full, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                used = response.headers.get("X-MBX-USED-WEIGHT-1M")
                if used and int(used) > 4000:
                    print(f"    weight {used}/min — easing off", file=sys.stderr)
                    time.sleep(5)
                return json.loads(response.read())

        except urllib.error.HTTPError as exc:
            if exc.code == 418:
                retry_after = exc.headers.get("Retry-After", "unknown")
                raise RateLimitBan(
                    f"HTTP 418: IP banned by Binance, Retry-After={retry_after}s. "
                    "Stop and wait it out; retrying now extends the ban."
                ) from None
            if exc.code == 429:
                delay = int(exc.headers.get("Retry-After", 2 ** (attempt + 1)))
                print(f"    429 rate limited, sleeping {delay}s", file=sys.stderr)
                time.sleep(delay)
                continue
            if exc.code >= 500:
                delay = 2 ** (attempt + 1)
                print(f"    HTTP {exc.code}, retrying in {delay}s", file=sys.stderr)
                time.sleep(delay)
                continue
            raise RuntimeError(f"HTTP {exc.code} from {full}: {exc.read()[:400]!r}") from None

        except urllib.error.URLError as exc:
            delay = 2 ** (attempt + 1)
            print(f"    network error ({exc.reason}), retrying in {delay}s", file=sys.stderr)
            time.sleep(delay)

    raise RuntimeError(f"Giving up on {full} after {MAX_RETRIES} attempts")


def fetch_klines(
    symbol: str, market: str, interval: str, start_ms: int, end_ms: int | None
) -> list[list[Any]]:
    """Page forward through kline history.

    Advances by ``last_open + interval`` rather than ``+1ms`` so the boundary
    bar is not re-fetched on every page.
    """
    url = HOSTS[market] + KLINE_PATHS[market]
    step = interval_ms(interval)
    cursor = start_ms
    rows: list[list[Any]] = []

    while True:
        page = _get(
            url,
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": PAGE_LIMIT,
            },
        )
        if not page:
            break

        rows.extend(page)
        last_open = int(page[-1][0])
        print(
            f"    {len(rows):>7} rows, through {_iso(last_open)}",
            file=sys.stderr,
        )

        if len(page) < PAGE_LIMIT:
            break
        cursor = last_open + step
        if end_ms is not None and cursor > end_ms:
            break
        time.sleep(0.25)  # stay well inside the weight budget

    return rows


def fetch_funding(symbol: str, start_ms: int, end_ms: int | None) -> list[dict[str, Any]]:
    """Page forward through funding settlement history."""
    url = HOSTS["um"] + FUNDING_PATH
    cursor = start_ms
    rows: list[dict[str, Any]] = []

    while True:
        page = _get(
            url,
            {"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": PAGE_LIMIT},
        )
        if not page:
            break
        rows.extend(page)
        last = int(page[-1]["fundingTime"])
        print(f"    {len(rows):>7} settlements, through {_iso(last)}", file=sys.stderr)
        if len(page) < PAGE_LIMIT:
            break
        cursor = last + 1
        if end_ms is not None and cursor > end_ms:
            break
        time.sleep(0.25)

    return rows


def _iso(epoch_ms: int) -> str:
    # Tolerate the microsecond-era archives here too.
    if epoch_ms > 100_000_000_000_000:
        epoch_ms //= 1000
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


def _parse_date(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Binance history into the local Parquet cache.",
        epilog="No API key needed — these are public endpoints. This script cannot trade.",
    )
    parser.add_argument("symbol", help="e.g. BTCUSDT")
    parser.add_argument(
        "--market", choices=sorted(HOSTS), default="spot", help="spot, um (USD-M), or cm (coin-M)"
    )
    parser.add_argument("--interval", default="1d", help="1m, 1h, 1d, ... (case-sensitive)")
    parser.add_argument("--start", default="2020-01-01", help="ISO date, inclusive")
    parser.add_argument("--end", default=None, help="ISO date, inclusive. Default: now")
    parser.add_argument("--cache", default="data/cache", help="cache root")
    parser.add_argument(
        "--funding",
        action="store_true",
        help="fetch funding settlements instead of klines (requires --market um)",
    )
    args = parser.parse_args()

    start_ms = _parse_date(args.start)
    end_ms = _parse_date(args.end) if args.end else None
    now_ms = int(time.time() * 1000)

    try:
        if args.funding:
            if args.market != "um":
                parser.error("--funding requires --market um")
            print(f"Fetching funding for {args.symbol} from {args.start}", file=sys.stderr)
            rows = fetch_funding(args.symbol, start_ms, end_ms)
            rates = parse_funding_rates(rows)
            if not rates:
                print("No settlements returned.", file=sys.stderr)
                return 1

            ensure_layout(args.cache)
            target = funding_path(args.cache, args.symbol)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(rows, indent=1))

            observed = observed_funding_interval_hours(rates)
            print(f"\n{len(rates)} settlements -> {target}")
            print(f"  {rates[0].timestamp.date()} -> {rates[-1].timestamp.date()}")
            print(f"  observed interval: {observed}h (derived, not assumed)")
            return 0

        print(
            f"Fetching {args.symbol} {args.interval} ({args.market}) from {args.start}",
            file=sys.stderr,
        )
        rows = fetch_klines(args.symbol, args.market, args.interval, start_ms, end_ms)
        if not rows:
            print("No bars returned. Check the symbol and start date.", file=sys.stderr)
            return 1

        bars = klines_to_bars(rows, args.symbol, args.interval, now_ms=now_ms)
        target = cache_path(args.cache, args.market, args.symbol, args.interval)

        try:
            bars = merge_bars(read_bars(target, args.symbol), bars)
            print("  merged with existing cache", file=sys.stderr)
        except FileNotFoundError:
            pass

        write_bars(target, bars)
        print(f"\n{len(bars)} bars -> {target}")
        print(f"  {bars[0].timestamp.date()} -> {bars[-1].timestamp.date()}")
        return 0

    except RateLimitBan as exc:
        print(f"\nSTOPPED: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted; cache left unchanged.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
