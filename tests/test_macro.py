"""Macro series: the revision trap, and the rule about mining.

The parser tests need no database. The store tests do, and the guard in
`tests/test_store_guard.py` polices this module's skip.

Two properties carry the weight:

**A backtest reads what was known then.** `read_as_of` must return the figure
that had actually been published, not the one that exists now — and "published"
means available, which is later than the vintage date when no release time is
known.

**Collected freely, used only when named.** A series a hypothesis did not name
before the run cannot be read, and there is no argument that turns that off.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_platform.analytics.trials import TrialRegistry
from trading_platform.data.fred import (
    FredParseError,
    Observation,
    latest_by_period,
    observations_from_payload,
    parse_observation,
)

JAN = date(2026, 1, 1)


def row(**over: Any) -> dict[str, Any]:
    base = {
        "realtime_start": "2026-02-01",
        "realtime_end": "9999-12-31",
        "date": "2026-01-01",
        "value": "3.5",
    }
    base.update(over)
    return base


# -- the parser ------------------------------------------------------------------------------


def test_realtime_start_is_the_vintage_and_date_is_the_period() -> None:
    """The field the whole design turns on. Swapping them hands a backtest the
    future while looking like ordinary data.

    Mutation: read `date` as the vintage — `__post_init__` raises on the
    reversed pair, which is why that check exists."""
    obs = parse_observation(row())
    assert obs.obs_date == JAN
    assert obs.vintage == date(2026, 2, 1)
    assert obs.value == Decimal("3.5")


def test_a_vintage_before_its_period_is_refused() -> None:
    """FRED publishes no forecasts, so this means the fields were read the wrong
    way round."""
    with pytest.raises(FredParseError, match="precedes the period"):
        Observation(obs_date=date(2026, 2, 1), vintage=JAN, value=Decimal("1"))


def test_a_missing_figure_is_none_and_not_a_gap() -> None:
    """`"."` means the provider published nothing for that period. That is a real
    answer, and storing it keeps "no value" distinguishable from "we never
    fetched it".

    Mutation: skip missing rows instead of storing None — the two become
    indistinguishable and this goes red."""
    assert parse_observation(row(value="."))
    assert parse_observation(row(value=".")).value is None
    assert parse_observation(row(value=" . ")).value is None


def test_a_value_that_is_neither_a_number_nor_the_missing_marker_raises() -> None:
    with pytest.raises(FredParseError, match="not a number"):
        parse_observation(row(value="n/a"))
    with pytest.raises(FredParseError, match="not a string"):
        parse_observation(row(value=3.5))


def test_a_response_with_no_observations_key_raises_rather_than_returning_empty() -> None:
    """ "No data" and "your request was wrong" must not look alike.

    Mutation: `return []` when the key is absent — an API error becomes an empty
    series and a backtest reports no signal instead of failing."""
    with pytest.raises(FredParseError, match="carries no `observations`"):
        observations_from_payload({"error_code": 400, "error_message": "Bad request"})
    assert observations_from_payload({"observations": []}) == []


def test_observations_come_back_sorted_by_period_then_vintage() -> None:
    payload = {
        "observations": [
            row(date="2026-02-01", realtime_start="2026-03-01"),
            row(date="2026-01-01", realtime_start="2026-03-15", value="3.7"),
            row(date="2026-01-01", realtime_start="2026-02-01"),
        ]
    }
    parsed = observations_from_payload(payload)
    assert [(o.obs_date, o.vintage) for o in parsed] == sorted(
        (o.obs_date, o.vintage) for o in parsed
    )


def test_latest_by_period_is_todays_view_and_is_named_to_be_hard_to_misuse() -> None:
    """It is what a chart shows. Giving it to a backtest is the revision trap."""
    payload = {
        "observations": [
            row(realtime_start="2026-02-01", value="3.5"),
            row(realtime_start="2026-03-15", value="3.7"),
        ]
    }
    latest = latest_by_period(observations_from_payload(payload))
    assert latest[JAN].value == Decimal("3.7")
    assert latest[JAN].vintage == date(2026, 3, 15)


# -- the grant ---------------------------------------------------------------------------------


def test_a_trial_that_named_no_series_may_read_none(tmp_path: Path) -> None:
    """Collected freely, used only when named. A hypothesis that named nothing
    reads nothing — not "everything", which is what a permissive default would
    mean with a few thousand government series available.

    Mutation: return an all-permitting grant for an empty tuple — red."""
    from trading_platform.store.macro import SeriesGrant, SeriesNotGranted

    registry = TrialRegistry.open(tmp_path / "trials.jsonl")
    bare = registry.record(
        "unemployment leads equity drawdowns",
        mechanism="labour market weakness precedes earnings revisions, which precede prices",
        parameter_points=1,
    )
    with pytest.raises(SeriesNotGranted, match="names no series"):
        SeriesGrant.from_trial(bare)


def test_a_grant_covers_exactly_the_series_the_trial_named(tmp_path: Path) -> None:
    """Mutation: have `check` pass anything — the whole rule evaporates and the
    second half of this goes red."""
    from trading_platform.store.macro import SeriesGrant, SeriesNotGranted

    registry = TrialRegistry.open(tmp_path / "trials.jsonl")
    trial = registry.record(
        "unemployment leads equity drawdowns",
        mechanism="labour market weakness precedes earnings revisions, which precede prices",
        parameter_points=4,
        series=["UNRATE"],
    )
    grant = SeriesGrant.from_trial(trial)
    grant.check("UNRATE")
    with pytest.raises(SeriesNotGranted, match="CPIAUCSL"):
        grant.check("CPIAUCSL")


def test_the_named_series_survive_the_registry_round_trip(tmp_path: Path) -> None:
    """The registry is append-only on disk, so a series cannot be added to a
    hypothesis after the result is known — but only if it is actually written.

    Mutation: drop `series` from `to_json` — the reloaded trial names nothing
    and this goes red."""
    path = tmp_path / "trials.jsonl"
    registry = TrialRegistry.open(path)
    registry.record(
        "unemployment leads equity drawdowns",
        mechanism="labour market weakness precedes earnings revisions, which precede prices",
        parameter_points=4,
        series=["UNRATE", "PAYEMS", "UNRATE"],
    )
    reloaded = TrialRegistry.open(path)
    assert reloaded.trials[0].series == ("UNRATE", "PAYEMS"), "duplicates collapse, order kept"


def test_an_exploratory_grant_needs_a_reason() -> None:
    """Looking at a series before forming a hypothesis is legitimate. Doing it
    with no reason recorded is how this becomes the default path."""
    from trading_platform.store.macro import SeriesGrant

    grant = SeriesGrant.for_research("UNRATE", because="seeing whether it is even revised")
    grant.check("UNRATE")
    assert grant.trial_id == "", "an exploratory grant belongs to no trial, and says so"
    with pytest.raises(ValueError, match="needs a reason"):
        SeriesGrant.for_research("UNRATE", because="  ")
    with pytest.raises(ValueError, match="grants nothing"):
        SeriesGrant.for_research(because="nothing to grant")


# -- against a real store -----------------------------------------------------------------------

TEST_DSN_VAR = "TRADING_PLATFORM_TEST_DSN"
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
    from trading_platform.store.migrate import migrate

    dsn = _dsn()
    assert dsn is not None
    schema = "m_" + str(abs(hash(request.node.nodeid)) % 10**12)
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


def anything(*codes: str) -> Any:
    from trading_platform.store.macro import SeriesGrant

    return SeriesGrant.for_research(*codes, because="a test")


def seeded(conn: Any) -> Any:
    """CPI for January, published at 3.5 and revised to 3.7 six weeks later."""
    from trading_platform.store.macro import ensure_series, write_observations

    series = ensure_series(
        conn,
        "CPIAUCSL",
        provider="fred",
        title="CPI",
        frequency="monthly",
        revision_policy="revised for two months, then annually in the benchmark",
        release_schedule="monthly, 08:30 New York, about two weeks after the period",
    )
    write_observations(
        conn,
        series,
        [
            Observation(JAN, date(2026, 2, 12), Decimal("3.5")),
            Observation(JAN, date(2026, 3, 15), Decimal("3.7")),
            Observation(date(2026, 2, 1), date(2026, 3, 12), Decimal("3.1")),
        ],
    )
    return series


def test_a_backtest_reads_the_figure_that_had_been_published_not_todays(conn: Any) -> None:
    """The revision trap, end to end. January's CPI was 3.5 when it came out and
    is 3.7 now. A run standing in February must see 3.5.

    Mutation: order by `vintage DESC` without the `available_from <= at` filter,
    or use `read_latest` — February sees 3.7, a figure nobody had, and this goes
    red."""
    from trading_platform.store.macro import read_as_of, read_latest

    seeded(conn)
    grant = anything("CPIAUCSL")

    february = datetime(2026, 2, 20, tzinfo=UTC)
    assert read_as_of(conn, "CPIAUCSL", at=february, grant=grant) == [(JAN, Decimal("3.5"))]

    april = datetime(2026, 4, 1, tzinfo=UTC)
    assert read_as_of(conn, "CPIAUCSL", at=april, grant=grant) == [
        (JAN, Decimal("3.7")),
        (date(2026, 2, 1), Decimal("3.1")),
    ]

    # Today's view — what a chart shows, and what must never reach a backtest.
    assert read_latest(conn, "CPIAUCSL")[0] == (JAN, Decimal("3.7"))


def test_a_period_not_yet_published_is_absent_rather_than_zero(conn: Any) -> None:
    """Nobody had it. An absent period and a period published as zero are
    different facts, and a reader that conflated them would let a strategy trade
    on a number that did not exist."""
    from trading_platform.store.macro import read_as_of

    seeded(conn)
    early = datetime(2026, 1, 15, tzinfo=UTC)
    assert read_as_of(conn, "CPIAUCSL", at=early, grant=anything("CPIAUCSL")) == []


def test_a_value_is_not_available_at_midnight_on_its_vintage_day(conn: Any) -> None:
    """A figure with vintage 2026-02-12 was released at some hour of that day —
    usually 08:30 New York. A run at 00:00 UTC that reads it has a working day
    of hindsight. With no stated release time the value becomes available at the
    END of the vintage day, which is the conservative reading.

    Mutation: make `available_from` the vintage date itself — the first
    assertion goes red, and every macro backtest silently gains up to a day."""
    from trading_platform.store.macro import read_as_of

    seeded(conn)
    grant = anything("CPIAUCSL")
    on_the_day = datetime(2026, 2, 12, 0, 0, tzinfo=UTC)
    assert read_as_of(conn, "CPIAUCSL", at=on_the_day, grant=grant) == []
    next_day = datetime(2026, 2, 13, 0, 0, tzinfo=UTC)
    assert read_as_of(conn, "CPIAUCSL", at=next_day, grant=grant) == [(JAN, Decimal("3.5"))]


def test_a_stated_release_time_is_used_when_the_provider_gives_one(conn: Any) -> None:
    """The conservative default is a fallback, not a policy. Where the provider
    states 08:30, a run at 09:00 that day should see it."""
    from trading_platform.store.macro import ensure_series, read_as_of, write_observations

    series = ensure_series(conn, "PAYEMS", provider="fred", title="Payrolls")
    release = datetime(2026, 2, 6, 13, 30, tzinfo=UTC)  # 08:30 New York
    write_observations(
        conn,
        series,
        [Observation(JAN, date(2026, 2, 6), Decimal("150"))],
        released_at={date(2026, 2, 6): release},
    )
    grant = anything("PAYEMS")
    assert read_as_of(conn, "PAYEMS", at=datetime(2026, 2, 6, 13, 0, tzinfo=UTC), grant=grant) == []
    assert read_as_of(conn, "PAYEMS", at=datetime(2026, 2, 6, 14, 0, tzinfo=UTC), grant=grant) == [
        (JAN, Decimal("150"))
    ]


def test_reading_an_ungranted_series_is_refused_by_the_reader_itself(conn: Any) -> None:
    """Not by convention, and with no argument that turns it off.

    Mutation: make `grant` optional with a permissive default — red."""
    from trading_platform.store.macro import SeriesNotGranted, read_as_of

    seeded(conn)
    with pytest.raises(SeriesNotGranted, match="not among the series"):
        read_as_of(conn, "CPIAUCSL", at=datetime(2026, 4, 1, tzinfo=UTC), grant=anything("UNRATE"))


def test_a_revision_is_a_new_row_and_the_old_value_survives(conn: Any) -> None:
    """The whole value of the table. `revisions` reports the spread, which is the
    size of the lookahead a single-vintage backtest would have granted itself.

    Mutation: UPDATE on conflict instead of inserting a new vintage — the
    revision history collapses and this goes red."""
    from trading_platform.store.macro import revisions

    seeded(conn)
    found = revisions(conn, "CPIAUCSL")
    assert len(found) == 1
    assert found[0]["obs_date"] == JAN
    assert found[0]["vintages"] == 2
    assert found[0]["spread"] == Decimal("0.2")


def test_the_same_vintage_with_a_different_value_is_a_conflict_not_an_update(
    conn: Any,
) -> None:
    """A provider cannot have published two figures on one day; one is wrong,
    and which is not a question a writer answers silently.

    Mutation: `ON CONFLICT DO UPDATE` — the contradiction is swallowed and the
    record of what was believed is destroyed."""
    from trading_platform.store.macro import VintageConflict, write_observations

    series = seeded(conn)
    with pytest.raises(VintageConflict, match="one of these is wrong"):
        write_observations(conn, series, [Observation(JAN, date(2026, 2, 12), Decimal("9.9"))])
    # And nothing was written: the earlier figure stands.
    from trading_platform.store.macro import read_latest

    assert read_latest(conn, "CPIAUCSL")[0] == (JAN, Decimal("3.7"))


def test_refetching_the_same_observations_is_free_and_writes_nothing(conn: Any) -> None:
    from trading_platform.store.macro import write_observations

    series = seeded(conn)
    assert (
        write_observations(
            conn,
            series,
            [
                Observation(JAN, date(2026, 2, 12), Decimal("3.5")),
            ],
        )
        == 0
    )


def test_a_missing_figure_round_trips_as_none(conn: Any) -> None:
    from trading_platform.store.macro import ensure_series, read_latest, write_observations

    series = ensure_series(conn, "GDPC1", provider="fred")
    write_observations(conn, series, [Observation(JAN, date(2026, 2, 1), None)])
    assert read_latest(conn, "GDPC1") == [(JAN, None)]


def test_a_misspelled_series_is_refused_rather_than_returning_nothing(conn: Any) -> None:
    """An empty series and a typo must not look alike."""
    from trading_platform.store.macro import read_as_of

    seeded(conn)
    with pytest.raises(ValueError, match="no series"):
        read_as_of(
            conn, "CPIAUCSLX", at=datetime(2026, 4, 1, tzinfo=UTC), grant=anything("CPIAUCSLX")
        )


def test_a_naive_timestamp_is_refused(conn: Any) -> None:
    from trading_platform.store.macro import read_as_of

    seeded(conn)
    with pytest.raises(ValueError, match="timezone-aware"):
        read_as_of(conn, "CPIAUCSL", at=datetime(2026, 4, 1), grant=anything("CPIAUCSL"))


def test_the_same_code_at_a_different_provider_is_refused(conn: Any) -> None:
    """The same code can mean different things at two providers, and changing it
    would restate every observation already attributed to it."""
    from trading_platform.store.macro import ensure_series

    seeded(conn)
    with pytest.raises(ValueError, match="already recorded at provider"):
        ensure_series(conn, "CPIAUCSL", provider="bls")
