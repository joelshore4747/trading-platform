"""The reconciler: what several feeds disagree about, and why that is the point.

Needs a real Postgres, like `tests/test_store.py`. The guard in
`tests/test_store_guard.py` polices this module's skip too.

The distinction every test here turns on: **two sources on the same venue that
differ have found an error; two sources on different venues that differ have
measured the basis.** A reconciler that conflates them either buries real bad
prints under thousands of expected cross-venue rows, or reports normal market
structure as a fault. Both make the table useless, which makes running several
feeds pointless, which is the whole of workstream 2.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_platform.collect.reconcile import (
    ReconcileError,
    compare_bars,
    reconcile,
)
from trading_platform.core.events import Bar
from trading_platform.store.bars import Source, ensure_source, ingest_run, write_bars
from trading_platform.store.migrate import migrate

TEST_DSN_VAR = "TRADING_PLATFORM_TEST_DSN"
T0 = datetime(2026, 8, 1, tzinfo=UTC)
WINDOW = (T0 - timedelta(days=1), T0 + timedelta(days=1))

psycopg = pytest.importorskip("psycopg", reason="the store needs psycopg")


def _dsn() -> str | None:
    return os.environ.get(TEST_DSN_VAR) or None


def _reachable(dsn: str) -> bool:
    try:
        psycopg.connect(dsn).close()
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    _dsn() is None or not _reachable(_dsn() or ""),
    reason=f'set {TEST_DSN_VAR} — scripts/dev_postgres.sh start && eval "$(scripts/dev_postgres.sh env)"',
)


@pytest.fixture
def conn(request: pytest.FixtureRequest) -> Iterator[Any]:
    dsn = _dsn()
    assert dsn is not None
    schema = "r_" + str(abs(hash(request.node.nodeid)) % 10**12)
    connection = psycopg.connect(dsn)
    with connection.cursor() as cursor:
        cursor.execute(f'CREATE SCHEMA "{schema}"')
        cursor.execute(f'SET search_path TO "{schema}"')
    connection.commit()
    migrate(connection)
    try:
        yield connection
    finally:
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA "{schema}" CASCADE')
        connection.commit()
        connection.close()


def bar(minute: int, close: str = "100.50", **over: Any) -> Bar:
    values: dict[str, Any] = {
        "open": Decimal("100.00"),
        "high": Decimal("101.00"),
        "low": Decimal("99.00"),
        "volume": Decimal("12.5"),
    }
    values.update(over)
    return Bar(
        symbol="BTCUSDT", timestamp=T0 + timedelta(minutes=minute), close=Decimal(close), **values
    )


def feed(conn: Any, name: str, venue: str | None, bars: list[Bar]) -> Source:
    """One source with its bars in the store."""
    source = ensure_source(conn, name, kind="venue", venue=venue)
    if bars:
        with ingest_run(conn, source, kind="test") as run_id:
            write_bars(conn, source, run_id, market="um", interval="1m", bars=bars)
    return source


def run(conn: Any, **over: Any) -> Any:
    params: dict[str, Any] = {
        "market": "um",
        "interval": "1m",
        "window_start": WINDOW[0],
        "window_end": WINDOW[1],
    }
    params.update(over)
    return reconcile(conn, **params)


# -- the comparison itself, no database ---------------------------------------------------


def test_identical_bars_do_not_disagree() -> None:
    assert compare_bars(bar(1), bar(1)) is None


def test_the_same_price_quoted_to_different_precision_is_not_a_disagreement() -> None:
    """`100.5` and `100.50` are one price written two ways. Calling that a
    disagreement would fill the table with vendors' formatting choices and
    train everyone to ignore it.

    Mutation: compare `str(left) != str(right)` instead of `left == right` —
    this goes red and every cent-quoting vendor becomes a permanent alarm."""
    assert compare_bars(bar(1, "100.5"), bar(1, "100.50")) is None


def test_the_worst_field_is_the_largest_gap_not_always_the_close() -> None:
    """A bad print usually shows in the high or the low — a fat-fingered wick —
    while the close agrees. Reporting only the close would miss it.

    Mutation: measure the close alone. Red: the pair below agrees on close."""
    found = compare_bars(bar(1, high=Decimal("101.00")), bar(1, high=Decimal("110.00")))
    assert found is not None
    gap, field = found
    assert field == "high"
    assert gap == (Decimal("110.00") - Decimal("101.00")) / Decimal("101.00")


def test_a_volume_difference_alone_is_not_a_price_disagreement() -> None:
    """Two venues genuinely trade different amounts, and one venue reported by
    two vendors can differ on volume through trade-condition filtering. Volume
    is context for a price gap, never evidence on its own.

    Mutation: add "volume" to `PRICE_FIELDS` — red, and every cross-vendor pair
    on earth becomes a disagreement."""
    assert compare_bars(bar(1, volume=Decimal("1")), bar(1, volume=Decimal("9999"))) is None


# -- against the store --------------------------------------------------------------------


def test_two_sources_that_agree_produce_no_rows_and_a_stated_rate(conn: Any) -> None:
    feed(conn, "alpaca", "nasdaq", [bar(1), bar(2)])
    feed(conn, "polygon", "nasdaq", [bar(1), bar(2)])
    result = run(conn)
    assert result.instants == 2
    assert result.compared == 2
    assert result.disagreements == []
    assert result.agreement_rate == 1


def test_two_sources_on_one_venue_that_differ_are_an_error_and_are_recorded(
    conn: Any,
) -> None:
    """Same venue: the prices SHOULD be identical, so a gap is a bad print in
    one of them. Mutation: compute `same_venue` as a constant False — the
    `same_venue_disagreements` list empties and this goes red."""
    feed(conn, "alpaca", "nasdaq", [bar(1, "100.50")])
    feed(conn, "polygon", "nasdaq", [bar(1, "100.75")])
    result = run(conn)

    assert len(result.disagreements) == 1
    found = result.disagreements[0]
    assert found.same_venue is True
    assert found.worst_field == "close"
    assert found.close_a == Decimal("100.50") and found.close_b == Decimal("100.75")
    assert result.same_venue_disagreements == [found]
    assert "1 error(s)" in result.report()
    assert result.same_venue_agreement_rate == 0
    assert result.same_venue_compared == 1

    with conn.cursor() as cursor:
        cursor.execute("SELECT same_venue, worst_field, worst_gap FROM source_disagreements")
        rows = cursor.fetchall()
    assert rows == [(True, "close", found.worst_gap)]


def test_two_sources_on_different_venues_that_differ_are_basis_not_an_error(
    conn: Any,
) -> None:
    """Binance and Coinbase SHOULD differ on BTC. That gap is the basis: a
    market fact. Recording it as an error would bury the real errors.

    Mutation: `same_venue=True` unconditionally — the error count becomes 1
    and the report's two lines swap, red."""
    feed(conn, "binance", "binance", [bar(1, "100.50")])
    feed(conn, "coinbase", "coinbase", [bar(1, "100.75")])
    result = run(conn)

    assert len(result.disagreements) == 1
    assert result.disagreements[0].same_venue is False
    assert result.same_venue_disagreements == []
    assert "cross venue: 1 of 1 comparisons differ" in result.report()
    assert "NOT an error" in result.report()
    # No two sources share a venue, so no error rate can be stated at all.
    assert result.same_venue_compared == 0
    assert result.same_venue_agreement_rate is None


def test_a_source_with_no_recorded_venue_refuses_to_be_classified(conn: Any) -> None:
    """A wrong `same_venue` turns a basis into a false alarm or an error into a
    shrug. Guessing which is worse than raising.

    Mutation: default the venue to the source name, or to None==None being
    "same" — this goes green while silently deciding something unknowable."""
    feed(conn, "alpaca", "nasdaq", [bar(1, "100.50")])
    feed(conn, "mystery", None, [bar(1, "100.75")])
    with pytest.raises(ReconcileError, match="no recorded venue"):
        run(conn)


def test_instants_only_one_source_saw_are_not_evidence_of_agreement(conn: Any) -> None:
    """A source that captured 10,000 bars alone and 2 alongside another has not
    agreed 10,002 times. Counting them would make the agreement rate a function
    of how much data one feed has, not of whether they agree.

    Mutation: drop the `HAVING count(DISTINCT source_id) > 1` — `instants`
    jumps to 5 and this goes red."""
    feed(conn, "alpaca", "nasdaq", [bar(1), bar(2), bar(3), bar(4), bar(5)])
    feed(conn, "polygon", "nasdaq", [bar(1), bar(2)])
    result = run(conn)
    assert result.instants == 2
    assert result.compared == 2


def test_an_empty_window_states_that_nothing_was_compared_rather_than_100_percent(
    conn: Any,
) -> None:
    """No comparisons is not perfect agreement. A caller printing 100% over an
    empty window has told a lie, so the rate is None and the report says so.

    Mutation: `return Decimal(1)` when `compared == 0` — red, twice."""
    feed(conn, "alpaca", "nasdaq", [bar(1)])
    result = run(conn)
    assert result.compared == 0
    assert result.agreement_rate is None
    assert "NOTHING COMPARED" in result.report()


def test_a_pair_is_recorded_once_and_not_again_in_mirror_image(conn: Any) -> None:
    """`source_a_id < source_b_id` in the query and as a table constraint, so
    (alpaca, polygon) and (polygon, alpaca) are one row.

    Mutation: drop `a.source_id < b.source_id` from the join — the count
    doubles and the CHECK constraint rejects the insert."""
    feed(conn, "alpaca", "nasdaq", [bar(1, "100.50")])
    feed(conn, "polygon", "nasdaq", [bar(1, "100.75")])
    result = run(conn)
    assert len(result.disagreements) == 1
    assert result.disagreements[0].source_a_id < result.disagreements[0].source_b_id


def test_three_sources_compare_as_three_pairs(conn: Any) -> None:
    feed(conn, "a", "nasdaq", [bar(1)])
    feed(conn, "b", "nasdaq", [bar(1)])
    feed(conn, "c", "nasdaq", [bar(1)])
    result = run(conn)
    assert result.instants == 1
    assert result.compared == 3


def test_the_run_row_records_what_the_pass_did(conn: Any) -> None:
    """The agreement rate only means something with its denominator beside it,
    so both live on the run row. Mutation: stop updating `compared` — the
    stored 0 would make every stored rate unreadable."""
    feed(conn, "alpaca", "nasdaq", [bar(1, "100.50"), bar(2)])
    feed(conn, "polygon", "nasdaq", [bar(1, "100.75"), bar(2)])
    result = run(conn)

    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT instants, compared, disagreements, finished_at FROM reconcile_runs WHERE id = %s",
            (result.run_id,),
        )
        instants, compared, disagreements, finished = cursor.fetchone()
    assert (instants, compared, disagreements) == (2, 2, 1)
    assert finished is not None


def test_a_dry_run_compares_and_writes_nothing(conn: Any) -> None:
    feed(conn, "alpaca", "nasdaq", [bar(1, "100.50")])
    feed(conn, "polygon", "nasdaq", [bar(1, "100.75")])
    result = run(conn, record=False)
    assert len(result.disagreements) == 1
    assert result.run_id is None
    with conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM source_disagreements")
        assert cursor.fetchone()[0] == 0
        cursor.execute("SELECT count(*) FROM reconcile_runs")
        assert cursor.fetchone()[0] == 0


def test_min_gap_filters_but_still_counts_the_comparison(conn: Any) -> None:
    """A threshold is how a systematic error becomes invisible, so it defaults
    to zero and the comparison is counted whether or not a row is written.

    Mutation: skip filtered pairs before incrementing `compared` — the
    agreement rate would then be computed over a denominator that excludes
    exactly the cases it was asked to tolerate."""
    feed(conn, "alpaca", "nasdaq", [bar(1, "100.50")])
    feed(conn, "polygon", "nasdaq", [bar(1, "100.51")])
    result = run(conn, min_gap=Decimal("0.01"))  # 1%, far above the ~0.01% gap
    assert result.disagreements == []
    assert result.compared == 1
    assert result.agreement_rate == 1


def test_a_backwards_or_naive_window_is_refused(conn: Any) -> None:
    with pytest.raises(ValueError, match="is before"):
        run(conn, window_start=WINDOW[1], window_end=WINDOW[0])
    with pytest.raises(ValueError, match="timezone-aware"):
        run(conn, window_start=datetime(2026, 8, 1))  # naive on purpose


def test_symbols_can_be_narrowed_but_not_emptied(conn: Any) -> None:
    feed(conn, "alpaca", "nasdaq", [bar(1, "100.50")])
    feed(conn, "polygon", "nasdaq", [bar(1, "100.75")])
    assert run(conn, symbols=["ETHUSDT"]).compared == 0
    assert run(conn, symbols=["BTCUSDT"]).compared == 1
    with pytest.raises(ValueError, match="omit it"):
        run(conn, symbols=[])


# -- end to end: the phase-1 acceptance criterion, minus the network ----------------------


def test_two_captures_run_concurrently_land_separately_and_reconcile_to_a_number(
    conn: Any,
) -> None:
    """The whole of workstream 2 phase 1 in one test, with the venues faked.

    Two independent captures of the SAME symbol, running on their own threads,
    each with its own store connection, its own source row and its own ingest
    run. They disagree about one bar out of five. What must come out the far
    end: ten rows in `bars` (five per source, never merged), and an agreement
    rate of 4/5 stated as a number with its denominator.

    What this does NOT verify: that any real vendor's response parses, or that
    two real feeds agree in the field. No venue is reachable from this sandbox.
    `docs/RUNBOOK.md` says how to run the same thing against real feeds; until
    that has been done, the network half of this claim is UNVERIFIED.

    Mutation: have both captures write under one source name. The bars collapse
    to five rows by primary key, `compared` falls to 0, and the agreement rate
    becomes None — which is exactly the failure "never merge two sources"
    exists to prevent."""

    from trading_platform.collect.capture import Collector, SourceCapture
    from trading_platform.store.sink import StoreSink

    now = T0 + timedelta(minutes=10)
    alpaca_bars = [bar(m) for m in range(1, 6)]
    polygon_bars = [bar(m) if m != 3 else bar(3, "100.75") for m in range(1, 6)]

    # A barrier makes the concurrency a REQUIREMENT rather than a hope. Each
    # capture's fetch waits for the other before returning, so a collector that
    # ran its captures one after another on one thread — which an earlier
    # version of this test could not tell apart — deadlocks here and fails.
    gate = threading.Barrier(2, timeout=15)

    class Feed:
        venue = "nasdaq"

        def __init__(self, bars: list[Bar]) -> None:
            self._bars = bars
            self._waited = False

        def fetch_bars(self, symbols: Any, interval: str, start: Any, end: Any = None) -> list[Bar]:
            if not self._waited:
                self._waited = True
                gate.wait()  # BrokenBarrierError if the other capture never arrives
            return list(self._bars)

    dsn = _dsn()
    assert dsn is not None
    with conn.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        schema = cursor.fetchone()[0]

    captures = []
    connections = []
    for name, bars in (("alpaca", alpaca_bars), ("polygon", polygon_bars)):
        own = psycopg.connect(dsn)  # one connection per capture, as the collector does
        with own.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
        own.commit()
        connections.append(own)
        source = ensure_source(own, name, kind="venue", venue="nasdaq")
        run_id = (
            own.cursor()
            .execute(
                "INSERT INTO ingest_runs (source_id, kind, params) VALUES (%s, 'capture', '{}') "
                "RETURNING id",
                (source.id,),
            )
            .fetchone()[0]
        )
        own.commit()
        captures.append(
            SourceCapture(
                name=name,
                fetcher=Feed(bars),
                sink=StoreSink(
                    connection=own,
                    source=source,
                    run_id=int(run_id),
                    market="um",
                    interval="1m",
                    batch_size=5,
                ),
                symbols=["BTCUSDT"],
                interval="1m",
                poll_every=timedelta(seconds=0.001),
                clock=lambda: now,
                sleep=lambda _s: None,
            )
        )

    collector = Collector(captures)
    stop = threading.Event()
    runner = threading.Thread(target=lambda: collector.run(stop), daemon=True)
    runner.start()
    tick = threading.Event()
    for _ in range(1000):
        if all(c.stats.recorded >= 5 for c in captures):
            break
        tick.wait(0.01)
    stop.set()
    runner.join(timeout=10)
    for capture in captures:
        capture.sink.close()  # type: ignore[attr-defined]
    for own in connections:
        own.close()

    with conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM bars")
        stored = cursor.fetchone()[0]
    assert stored == 10, "five bars per source, kept apart — a merge would give five"
    assert all(c.stats.healthy for c in captures), [c.stats.report() for c in captures]
    assert gate.n_waiting == 0 and not gate.broken, (
        "the two captures did not overlap: the barrier was never cleared by both, so this "
        "ran serially and proves nothing about concurrency"
    )

    result = run(conn)
    assert result.instants == 5
    assert result.compared == 5
    assert len(result.disagreements) == 1
    assert result.disagreements[0].ts == T0 + timedelta(minutes=3)
    assert result.disagreements[0].same_venue is True
    assert result.agreement_rate == Decimal(4) / Decimal(5)
    assert "5 comparisons over 5 shared instants" in result.report()


def test_the_headline_rate_does_not_count_basis_as_disagreement(conn: Any) -> None:
    """Three sources across two venues, all reporting honestly. The all-in rate
    reads 33% because every cross-venue pair differs — and nothing is wrong.
    Leading with that number would train everyone to ignore the reconciler.

    Mutation: make `report()` lead with `agreement_rate`, or compute
    `same_venue_agreement_rate` over `compared` rather than
    `same_venue_compared` — the same-venue rate stops being 100% and this goes
    red."""
    feed(conn, "coinbase-a", "coinbase", [bar(1, "100.50")])
    feed(conn, "coinbase-b", "coinbase", [bar(1, "100.50")])  # same venue, agrees
    feed(conn, "binance", "binance", [bar(1, "100.75")])  # other venue, differs
    result = run(conn)

    assert result.compared == 3
    assert result.same_venue_compared == 1
    assert result.same_venue_agreement_rate == 1, "nothing on one venue disagreed"
    assert result.agreement_rate == Decimal(1) / Decimal(3)
    assert "same venue: 100.0000% agreement" in result.report()
    assert "not an error rate" in result.report()

    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT compared, disagreements, same_venue_compared, same_venue_disagreements "
            "FROM reconcile_runs WHERE id = %s",
            (result.run_id,),
        )
        assert cursor.fetchone() == (3, 2, 1, 0)


def test_a_bar_exactly_on_the_window_end_is_compared(conn: Any) -> None:
    """The window is inclusive at both ends. An exclusive upper bound would drop
    the most recent instant from every run — the one an operator is looking at.

    Mutation: `a.ts < %s` instead of `<=` in the reconcile query — `compared`
    falls to 0, red."""
    edge = T0 + timedelta(minutes=7)
    feed(conn, "alpaca", "nasdaq", [bar(7, "100.50")])
    feed(conn, "polygon", "nasdaq", [bar(7, "100.75")])
    result = run(conn, window_start=T0, window_end=edge)
    assert result.compared == 1
    assert len(result.disagreements) == 1


def test_the_worst_gap_is_relative_to_the_lower_numbered_source(conn: Any) -> None:
    """Deliberately asymmetric — 100 against 200 is 1.0, the other way round it
    is 0.5 — and made deterministic by `source_a_id < source_b_id`, so a given
    pair always reports the same number and rows stay sortable.

    Mutation: drop the ordering from the join and the value depends on which row
    the planner returns first."""
    feed(conn, "aaa", "nasdaq", [bar(1, "100.00", low=Decimal("99.00"), high=Decimal("201.00"))])
    feed(conn, "bbb", "nasdaq", [bar(1, "200.00", low=Decimal("99.00"), high=Decimal("201.00"))])
    result = run(conn)
    found = result.disagreements[0]
    assert found.source_a_id < found.source_b_id
    assert found.worst_field == "close"
    assert found.worst_gap == Decimal("1")  # |100 - 200| / 100


def test_the_collector_script_wires_a_config_that_actually_writes_to_the_store(
    conn: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    """The test that would have caught the P0.

    `scripts/run_collector.py` defaulted its store market to `"stocks"`, which
    is not one of the store's markets, so every flush raised
    `unknown market 'stocks'`, both sources dropped 100% of their bars, both
    ingest runs closed `ok` with `rows_written = 0`, and `--check` passed. The
    library was fine; nothing tested `build()`, which is the only thing systemd
    ever runs.

    So: build from a config, poll once, and require rows.

    Mutation: put `market = "stocks"` back in the config below, or drop the
    `market not in MARKETS` check from `validate()` — red either way."""
    import contextlib
    import importlib.util
    import sys as _sys

    spec = importlib.util.spec_from_file_location(
        "run_collector_e2e", Path(__file__).resolve().parents[1] / "scripts" / "run_collector.py"
    )
    assert spec is not None and spec.loader is not None
    collector_script = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = collector_script
    spec.loader.exec_module(collector_script)

    with conn.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        schema = cursor.fetchone()[0]

    opened: list[Any] = []

    @contextlib.contextmanager
    def fake_connect() -> Any:
        dsn = _dsn()
        assert dsn is not None
        own = psycopg.connect(dsn)
        with own.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
        own.commit()
        opened.append(own)
        try:
            yield own
        finally:
            own.close()

    class FakeClient:
        venue = "fake-vendor"

        def __init__(self) -> None:
            self.credentials = type("C", (), {"require_all": staticmethod(lambda: {})})()

        def fetch_bars(self, symbols: Any, interval: str, start: Any, end: Any = None) -> list[Bar]:
            return [bar(1), bar(2)]

    monkeypatch.setattr(collector_script, "connect", fake_connect)
    monkeypatch.setattr(collector_script, "build_client", lambda spec: FakeClient())

    # The SHIPPED example config, with only the symbols and the journal path
    # changed. The market, the vendors and the venues are read from the file an
    # operator is told to copy, so a market key the store rejects fails here.
    root = Path(__file__).resolve().parents[1]
    config = collector_script.load_config(root / "config" / "collector.example.toml")
    config["journal_root"] = str(tmp_path / "journal")
    for source in config["source"]:
        source["symbols"] = ["BTCUSDT"]
    collector_script.validate(config)  # must not raise

    from contextlib import ExitStack

    with ExitStack() as stack:
        built, sinks = collector_script.build(config, stack)
        for capture in built.captures:
            capture.clock = lambda: T0 + timedelta(minutes=10)
            capture.poll_once()
        for sink in sinks:
            sink.close()
        assert all(s.stats.healthy for s in sinks), [s.stats.report() for s in sinks]
        assert all(c.stats.healthy for c in built.captures), built.report()

    with conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM bars")
        assert cursor.fetchone()[0] == 4, "two sources x two bars, kept apart"
        cursor.execute("SELECT DISTINCT market FROM bars")
        assert cursor.fetchall() == [(config["market"],)]
        cursor.execute("SELECT status, rows_written FROM ingest_runs ORDER BY id")
        assert cursor.fetchall() == [("ok", 2), ("ok", 2)], (
            "an ingest run that closes ok with rows_written 0 is what the market bug looked "
            "like from the outside"
        )

    # And the journal, which is the durable half, got them too.
    journalled = list((tmp_path / "journal").rglob("*.jsonl"))
    assert len(journalled) == 2, journalled


def test_the_shipped_example_config_names_a_market_the_store_accepts() -> None:
    """The example is followed. One that cannot write is worse than none.

    Mutation: change `market` in `config/collector.example.toml` to anything
    outside MARKETS — red."""
    import importlib.util
    import sys as _sys
    import tomllib

    spec = importlib.util.spec_from_file_location(
        "run_collector_cfg", Path(__file__).resolve().parents[1] / "scripts" / "run_collector.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    root = Path(__file__).resolve().parents[1]
    with (root / "config" / "collector.example.toml").open("rb") as handle:
        example = tomllib.load(handle)

    from trading_platform.data.cache import MARKETS

    assert example["market"] in MARKETS
    assert all("venue" in source for source in example["source"])
