"""Fetch macro series into the store, **with their vintages**.

    export FRED_API_KEY=...
    .venv/bin/python scripts/fetch_macro.py --series CPIAUCSL UNRATE PAYEMS
    .venv/bin/python scripts/fetch_macro.py --series CPIAUCSL --check

`--check` validates the configuration and the credential and touches no network.

**It asks ALFRED, not FRED.** The difference is the whole point: FRED serves the
current value of a series, ALFRED serves every value it has ever had. A backtest
reading the current figure for a period three years ago has read a number nobody
had at the time. The request therefore carries a realtime range covering all of
history, and the response comes back one row per (period, revision).

That costs more rows — a monthly series revised twice is three times the size —
and it is not optional. `store/macro.py` has no way to read a single-vintage
series correctly, on purpose.

**UNVERIFIED.** `api.stlouisfed.org` answers 403 from the sandbox this was
written in, so the URL construction and the parser are pinned by
`tests/test_macro.py` against the published reference and nothing here has met a
real response. The first real run is the verification.

The API key goes in a header where the provider allows one and otherwise in the
query string, which FRED requires — and where it does, the URL is never logged.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_platform.core.credentials import CredentialSet, MissingCredentialError
from trading_platform.data.fred import observations_from_payload
from trading_platform.live.alpaca import Transport, TransportError, urllib_transport
from trading_platform.store.bars import ensure_source, ingest_run
from trading_platform.store.connection import connect, is_configured
from trading_platform.store.macro import ensure_series, write_observations
from trading_platform.store.migrate import migrate

FRED_HOST = "https://api.stlouisfed.org"
#: The full realtime range. Anything narrower asks for a subset of history and
#: silently gives a series that looks complete.
ALL_HISTORY = ("1776-07-04", "9999-12-31")


def observations_url(series: str, key: str, *, host: str = FRED_HOST) -> str:
    """ALFRED's observations endpoint over the entire realtime range."""
    params = {
        "series_id": series,
        "api_key": key,
        "file_type": "json",
        "realtime_start": ALL_HISTORY[0],
        "realtime_end": ALL_HISTORY[1],
        "sort_order": "asc",
    }
    return f"{host}/fred/series/observations?{urllib.parse.urlencode(params)}"


def series_url(series: str, key: str, *, host: str = FRED_HOST) -> str:
    params = {"series_id": series, "api_key": key, "file_type": "json"}
    return f"{host}/fred/series?{urllib.parse.urlencode(params)}"


def _redacted(url: str, key: str) -> str:
    """The URL with the key removed, for anything that might be printed."""
    return url.replace(key, "<FRED_API_KEY>") if key else url


def fetch(transport: Transport, url: str, key: str) -> dict[str, object]:
    raw = transport(url, {"Accept": "application/json"})
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TransportError(f"response from {_redacted(url, key)} is not JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise TransportError(f"response from {_redacted(url, key)} is not an object")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--series", nargs="+", required=True)
    parser.add_argument("--host", default=FRED_HOST)
    parser.add_argument("--check", action="store_true", help="validate, touch no network")
    args = parser.parse_args(argv)

    credentials = CredentialSet.from_env("fred", "FRED_API_KEY")
    try:
        key = credentials.require_all()["FRED_API_KEY"]
    except MissingCredentialError as exc:
        raise SystemExit(str(exc)) from None

    if args.check:
        for code in args.series:
            print(f"  {code:<12} {_redacted(observations_url(code, key, host=args.host), key)}")
        print(f"{len(args.series)} series: credential present, no network touched")
        return 0

    if not is_configured():
        raise SystemExit(
            "TRADING_PLATFORM_DSN is unset. Vintages have nowhere to go; "
            'scripts/dev_postgres.sh start && eval "$(scripts/dev_postgres.sh env)"'
        )

    transport = urllib_transport()
    written_total = 0
    with connect() as conn:
        migrate(conn)
        # The PROVIDER is the source, registered like any other. This used to
        # convert each MacroSeries into a Source and keep its id, on the
        # assumption that "a macro series carries the same identity" -- but
        # macro_series and sources are different tables with independent id
        # sequences, and ingest_runs.source_id is a foreign key into sources.
        # So the id was a coincidence: with fewer bar sources than macro series
        # it raised, and with as many or more it silently attributed macro runs
        # to market-data sources and wrote anyway. GDP landed under
        # "binance-equity" that way. T-c5a8f437.
        source = ensure_source(
            conn,
            "fred",
            kind="vendor",
            description="Federal Reserve Economic Data / ALFRED, read by vintage",
        )
        for code in args.series:
            meta = fetch(transport, series_url(code, key, host=args.host), key)
            listed = meta.get("seriess")
            described: dict[str, object] = {}
            if isinstance(listed, list) and listed and isinstance(listed[0], dict):
                described = listed[0]
            series = ensure_series(
                conn,
                code,
                provider="fred",
                title=str(described.get("title", "")),
                units=str(described.get("units", "")),
                frequency=str(described.get("frequency", "")),
                revision_policy=str(described.get("notes", ""))[:2000],
                source_url=f"{args.host}/fred/series?series_id={code}",
            )
            payload = fetch(transport, observations_url(code, key, host=args.host), key)
            observations = observations_from_payload(payload)
            with ingest_run(
                conn,
                source,
                kind="fetch-fred",
                params={"series": code, "realtime": list(ALL_HISTORY)},
            ) as run_id:
                written = write_observations(conn, series, observations, run_id=run_id)
            vintages = len({o.vintage for o in observations})
            periods = len({o.obs_date for o in observations})
            print(
                f"{code}: {len(observations):,} observation(s) over {periods:,} period(s) "
                f"and {vintages:,} vintage(s) — {written:,} new"
            )
            written_total += written
    print(f"\n{written_total:,} new row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
