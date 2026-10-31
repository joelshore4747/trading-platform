"""Run N independent captures of the same markets, writing every bar to the store.

    .venv/bin/python scripts/run_collector.py --config collector.toml --check
    .venv/bin/python scripts/run_collector.py --config collector.toml

`--check` validates the configuration and the credentials and touches no
network: run it first, and run it in the systemd unit's `ExecStartPre`.

Configuration is a TOML file so the unit has something to point at. An example
lives at `config/collector.example.toml`.

**This does not run here.** Every market-data host answers 403 in the sandbox
this was written in, so nothing below has met a real response. It is written to
run on your machine or a VPS, and `docs/RUNBOOK.md` is the procedure. The
offline half — that N captures run concurrently, keep their sources apart, land
in Postgres and reconcile to a stated rate — is proved by
`tests/test_reconcile.py::test_two_captures_run_concurrently_land_separately_and_reconcile_to_a_number`.

**Nothing here can place an order.** The collector reads market data and writes
rows. Every broker adapter still raises.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import tomllib
from collections.abc import Mapping
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from types import FrameType
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_platform.collect.capture import Collector, SourceCapture
from trading_platform.core.credentials import MissingCredentialError
from trading_platform.data.cache import MARKETS
from trading_platform.data.intervals import UnknownIntervalError, interval_ms
from trading_platform.live.alpaca import AlpacaMarketData, urllib_transport
from trading_platform.live.coinbase import CoinbaseMarketData
from trading_platform.live.polygon import PolygonMarketData
from trading_platform.live.recorder import JournalRecorder
from trading_platform.store.bars import ensure_source, heartbeat, ingest_run
from trading_platform.store.connection import connect, is_configured
from trading_platform.store.migrate import migrate
from trading_platform.store.sink import FanOutSink, StoreSink

ROOT = Path(__file__).resolve().parents[1]

#: Which client to build. NOT the same thing as the venue — see below.
VENDORS = ("alpaca", "coinbase", "polygon")


class ConfigError(SystemExit):
    """The configuration cannot be run. Named, and refused before any network."""


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"no configuration at {path}")
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    if not config.get("source"):
        raise ConfigError(f"{path} declares no [[source]]; a collector with none runs silently")
    return config


def build_client(spec: dict[str, Any]) -> Any:
    """One vendor client. Credentials are read from the environment, never here."""
    vendor = spec.get("vendor")
    if vendor not in VENDORS:
        raise ConfigError(
            f"source {spec.get('name')!r}: vendor must be one of {VENDORS}, got {vendor!r}"
        )
    transport = urllib_transport(timeout_s=float(spec.get("timeout_s", 30.0)))
    if vendor == "alpaca":
        return AlpacaMarketData(
            market=spec.get("asset_class", "stocks"),
            transport=transport,
            stocks_feed=spec.get("feed", "iex"),
        )
    if vendor == "coinbase":
        # No credentials: the candles endpoint is public. See
        # live/coinbase.py on why a key is deliberately not read.
        return CoinbaseMarketData(transport=transport)
    return PolygonMarketData(transport=transport)


def validate(config: dict[str, Any]) -> list[tuple[dict[str, Any], Any]]:
    """Check every source and build its client. No database, no network.

    Separate from `build` so `--check` can say what is wrong without opening a
    connection or writing an `ingest_runs` row — a check that leaves rows behind
    is not a check.
    """
    # The store's market vocabulary, checked HERE rather than at the first
    # flush. `market = "stocks"` shipped in the example config and is not one of
    # them, so every write failed with the collector reporting `ok`, both runs
    # closing with rows_written = 0, and the reconciler comparing nothing
    # forever (adversarial review, 2026-09-04). A --check that does not check
    # this is not a check.
    market = config.get("market")
    if market not in MARKETS:
        raise ConfigError(
            f"market {market!r} is not one of the store's markets {sorted(MARKETS)}. "
            "US equities are 'equity'; 'stocks' is Alpaca's own path segment and belongs "
            "in a source's asset_class, not here."
        )
    interval = config.get("interval", "1m")
    try:
        interval_ms(interval)
    except UnknownIntervalError as exc:
        raise ConfigError(f"interval: {exc}") from None

    checked: list[tuple[dict[str, Any], Any]] = []
    venues: dict[str, str] = {}
    for spec in config["source"]:
        name = spec.get("name")
        if not name:
            raise ConfigError(
                "every [[source]] needs a name; it is what its rows are attributed to"
            )
        if not spec.get("symbols"):
            raise ConfigError(f"source {name!r} declares no symbols")
        if int(spec.get("replicas", 1)) < 1:
            raise ConfigError(f"source {name!r}: replicas must be at least 1")

        # The venue is the TAPE, not the vendor. `sources.venue` decides whether
        # a disagreement is an error (same venue: one of them is a bad print) or
        # the basis (different venues: a market fact). Stamping it with the
        # vendor made every Alpaca-vs-Polygon gap read as basis — turning every
        # error into a shrug, which is the exact failure the reconciler says it
        # refuses. So it is required, never defaulted from the vendor.
        venue = spec.get("venue")
        if not isinstance(venue, str) or not venue:
            raise ConfigError(
                f"source {name!r} declares no venue. The venue is which tape this data "
                "describes ('iex', 'sip', 'nasdaq', 'binance'), not which vendor delivers "
                "it. Two sources sharing a venue are claiming to report the same market, "
                "so any difference between them is an error; two with different venues are "
                "expected to differ and the gap is the basis."
            )
        if venue != venue.strip().lower():
            raise ConfigError(
                f"source {name!r}: venue {venue!r} must be lower case with no padding, or "
                f"{venue.strip().lower()!r} and {venue!r} become two different venues and "
                "their disagreements are misread as basis."
            )
        _refuse_a_mislabelled_tape(name, spec, venue)
        venues[name] = venue
        client = build_client(spec)
        try:
            client.credentials.require_all()  # names the variable, before any network
        except MissingCredentialError as exc:
            raise ConfigError(f"source {name!r}: {exc}") from None
        checked.append((spec, client))

    if len(checked) > 1 and len(set(venues.values())) == len(venues):
        print(
            "  note: no two sources share a venue, so every disagreement this "
            "configuration finds will be recorded as basis rather than as an error. "
            "That is correct if they really are different tapes.",
            file=sys.stderr,
        )
    return checked


#: Vendor feed tiers whose `venue` is not a matter of opinion. The key is
#: (vendor, feed); the value is the only tape that feed can honestly claim.
#: Alpaca's free tier serves IEX alone — a small share of volume — so calling it
#: `sip` makes every Alpaca-vs-Polygon difference a same-venue disagreement,
#: which the reconciler reports as an error rate and the operations page alarms
#: on. The multi-source check then measures nothing except the IEX-to-
#: consolidated spread, mislabelled (adversarial-reviewer, T-c9da9b75).
TAPE_BY_FEED = {
    ("alpaca", "iex"): "iex",
    ("alpaca", "sip"): "sip",
}


def _refuse_a_mislabelled_tape(name: str, spec: dict[str, Any], venue: str) -> None:
    """A venue that contradicts the feed tier is refused at `--check`.

    Prose in the example config already said this and prose does not run. The
    whole point of two sources is that a difference between them means
    something, and a mislabelled tape is the one input that makes every
    difference mean the wrong thing — silently, and in the direction of "the
    data is fine" once somebody decides a 40% error rate is just how it is.
    """
    feed = spec.get("feed")
    if not isinstance(feed, str):
        return
    expected = TAPE_BY_FEED.get((str(spec.get("vendor", "")).lower(), feed.lower()))
    if expected is None or expected == venue:
        return
    raise ConfigError(
        f"source {name!r}: feed {feed!r} is the {expected!r} tape, but venue is {venue!r}. "
        f"Set venue = {expected!r}. Alpaca's free tier serves IEX alone, a small share of "
        "volume, so calling it the consolidated tape makes every difference against a "
        "consolidated source a same-venue disagreement — reported as an error rate, alarmed "
        "on, and measuring nothing but the IEX-to-consolidated spread under the wrong name. "
        "The spread is itself worth having; it is not data validation."
    )


def build(config: dict[str, Any], stack: ExitStack) -> tuple[Collector, list[StoreSink]]:
    """Wire every configured source to its own connection, run, journal and sink.

    One store connection per capture, deliberately. Two captures sharing one
    connection would interleave their transactions, so one source's contradiction
    would roll back another source's batch — coupling the very things this whole
    workstream exists to keep apart.
    """
    market = config["market"]
    interval = config.get("interval", "1m")
    journal_root = Path(config.get("journal_root", ROOT / "data" / "journal"))

    captures: list[SourceCapture] = []
    sinks: list[StoreSink] = []
    for spec, client in validate(config):
        name = spec["name"]
        symbols = spec["symbols"]
        replicas = int(spec.get("replicas", 1))
        for index in range(replicas):
            # Replicas are separate sources on purpose: two connections to one
            # vendor that report the same instant differently have found the
            # vendor contradicting itself, and that only shows up if their rows
            # are kept apart. It also multiplies rate-limit consumption for
            # little information, so the default is 1.
            capture_name = name if replicas == 1 else f"{name}-{index + 1}"
            connection = stack.enter_context(connect())
            migrate(connection)
            source = ensure_source(
                connection,
                capture_name,
                kind="vendor",
                venue=spec["venue"],
                description=spec.get("description", f"live capture via {spec['venue']}"),
            )
            run_id = stack.enter_context(
                ingest_run(
                    connection,
                    source,
                    kind="capture",
                    params={
                        "market": market,
                        "interval": interval,
                        "symbols": list(symbols),
                        "replica": index + 1,
                    },
                )
            )
            store = StoreSink(
                connection=connection,
                source=source,
                run_id=run_id,
                market=market,
                interval=interval,
                batch_size=int(spec.get("batch_size", 500)),
            )
            sinks.append(store)
            journal = stack.enter_context(JournalRecorder(journal_root / capture_name, market))

            def beat(
                counters: Mapping[str, int],
                *,
                connection: Any = connection,
                run_id: int = run_id,
                store: StoreSink = store,
            ) -> None:
                """Write the live counters onto the open run, every poll.

                Bound as defaults because this closes over the loop variables of
                a `for` that builds several captures; without them every capture
                would heartbeat onto the last one's run, which is one row that
                looks healthy and N-1 sources that look dead.

                The sink's own dropped count is merged in: `sink_failures` on the
                capture counts a fan-out sink raising, and `dropped` counts bars
                the store gave up on after retrying. They are different numbers
                and the second is the one that means bars are only in the journal.
                """
                heartbeat(connection, run_id, {**counters, "dropped": store.stats.dropped})

            # The journal FIRST: it needs only a filesystem, and it is what
            # backfills the store after an outage.
            captures.append(
                SourceCapture(
                    name=capture_name,
                    fetcher=client,
                    sink=FanOutSink([journal, store]),
                    symbols=list(symbols),
                    interval=interval,
                    poll_every=timedelta(seconds=float(spec.get("poll_every_s", 15))),
                    lookback=timedelta(minutes=float(spec.get("lookback_min", 120))),
                    heartbeat=beat,
                )
            )
    return Collector(captures), sinks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--config", type=Path, default=ROOT / "collector.toml")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the config and credentials, touch no network, exit",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if not is_configured():
        raise ConfigError(
            "TRADING_PLATFORM_DSN is unset. The collector writes to the store; "
            'scripts/dev_postgres.sh start && eval "$(scripts/dev_postgres.sh env)" '
            "for a local one."
        )

    stop = threading.Event()

    def handle(signum: int, frame: FrameType | None) -> None:
        print(f"\nsignal {signal.Signals(signum).name}: stopping", file=sys.stderr)
        stop.set()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)

    if args.check:
        checked = validate(config)
        for spec, client in checked:
            replicas = int(spec.get("replicas", 1))
            print(
                f"  {spec['name']:<16} venue {spec['venue']:<10} via {client.venue:<16} "
                f"{len(spec['symbols'])} symbol(s)"
                + (f", {replicas} replicas" if replicas > 1 else "")
            )
        print(
            f"{len(checked)} source(s) into market {config['market']!r} at "
            f"{config.get('interval', '1m')}: configuration and credentials OK, "
            "no network touched"
        )
        return 0

    with ExitStack() as stack:
        collector, sinks = build(config, stack)
        names = ", ".join(c.name for c in collector.captures)
        print(f"{len(collector.captures)} capture(s): {names}")
        try:
            collector.run(stop)
        finally:
            for sink in sinks:
                sink.close()
            print("\n" + collector.report())
            for sink in sinks:
                if not sink.stats.healthy:
                    print(f"  store sink {sink.source.name}: {sink.stats.report()}")

    degraded = [c for c in collector.captures if not c.stats.healthy]
    lost = sum(s.stats.dropped for s in sinks)
    if lost:
        print(
            f"\n{lost:,} bar(s) never reached the store. They are in the journal; "
            "put them back with scripts/journal_to_store.py --source <name>.",
            file=sys.stderr,
        )
    return 1 if degraded or lost else 0


if __name__ == "__main__":
    raise SystemExit(main())
