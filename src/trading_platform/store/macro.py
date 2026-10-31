"""Macro series in the store, read the way a backtest must read them.

Two rules, and the module exists for both.

## 1. A backtest reads what was known then

Macro data is revised. Reading today's figure for a period three years ago is
lookahead in its most insidious form, because it looks like ordinary data rather
than like a bug. `read_as_of` returns, for each period, the most recent vintage
that was **available** at the given instant — and available is not the same as
dated: a figure with vintage 2026-02-01 was released at some hour of that day,
usually 08:30 New York, so a run at 00:00 UTC that reads it has a working day of
hindsight. Where the provider states a release time it is used; where it does
not, the value becomes available at the END of the vintage day, which is the
conservative reading and is stated rather than assumed.

`read_latest` exists and is named to be hard to misuse: it is what a chart shows
and what a person means by "the CPI figure", and it must never reach a backtest.

## 2. Collected freely, used only when named

`harness/ROADMAP.md` defers external data until a strategy hypothesis names what
it needs. The risk that guards is a warehouse of data nobody has a question for,
mined until something correlates — and with a few thousand government series and
a few hundred strategies, something always will.

The deferral is lifted for **collection** and kept for **use**. Ingest anything;
a strategy may only read a series its pre-registered `Trial` named in advance.
`SeriesGrant` is that permission, `read_as_of` requires one, and there is no
argument that turns it off. The registry (`analytics/trials.py`) is where the
naming happens and it is append-only, so a series cannot be added to a
hypothesis after the result is known.

`store/` is the persistence adapter and the top layer: it imports the domain
packages and none of them import it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from trading_platform.analytics.trials import Trial
from trading_platform.data.fred import Observation

__all__ = [
    "MacroSeries",
    "SeriesGrant",
    "SeriesNotGranted",
    "VintageConflict",
    "ensure_series",
    "read_as_of",
    "read_latest",
    "revisions",
    "write_observations",
]

PROVIDERS = ("fred", "sec", "treasury", "bls", "derived")


class SeriesNotGranted(PermissionError):
    """A strategy tried to read a series its hypothesis never named."""


class VintageConflict(ValueError):
    """The provider gave two different values for one period at one vintage.

    Never resolved automatically. One of them is wrong, and which one is not a
    question a writer should answer silently.
    """


@dataclass(frozen=True, slots=True)
class MacroSeries:
    id: int
    code: str
    provider: str
    title: str
    units: str
    frequency: str


@dataclass(frozen=True, slots=True)
class SeriesGrant:
    """Permission to read particular series, derived from a recorded hypothesis.

    Deliberately awkward to construct from nothing. `for_research` exists for
    exploration and says so in its name and in what it records; everything that
    produces a number a decision might rest on goes through `from_trial`.
    """

    #: Which trial named these. Empty only for `for_research`.
    trial_id: str
    codes: frozenset[str]
    #: Why this grant exists, carried so a refusal can explain itself.
    reason: str

    @classmethod
    def from_trial(cls, trial: Trial) -> SeriesGrant:
        if not trial.series:
            raise SeriesNotGranted(
                f"trial {trial.trial_id} names no series, so it may read none. Name them "
                "when the trial is recorded — before the run — or the naming is a "
                "record of what happened to work."
            )
        return cls(
            trial_id=trial.trial_id,
            codes=frozenset(trial.series),
            reason=f"pre-registered by trial {trial.trial_id}",
        )

    @classmethod
    def for_research(cls, *codes: str, because: str) -> SeriesGrant:
        """An exploratory grant. **Produces no result anyone may act on.**

        For looking at a series before forming a hypothesis about it, which is
        legitimate and necessary. What it is not is a way around
        pre-registration: a number obtained under this grant cannot appear in an
        experiment record, because the record requires a trial and the trial
        requires the series to have been named first.
        """
        if not because.strip():
            raise ValueError(
                "an exploratory grant needs a reason. 'Because I wanted to look' is a "
                "fine reason and takes two seconds to write; no reason at all is how "
                "this becomes the default path."
            )
        if not codes:
            raise ValueError("an exploratory grant with no series grants nothing")
        return cls(trial_id="", codes=frozenset(codes), reason=f"exploratory: {because}")

    def check(self, code: str) -> None:
        if code not in self.codes:
            raise SeriesNotGranted(
                f"{code!r} is not among the series this grant covers "
                f"({sorted(self.codes)}; {self.reason}). Government data is collected "
                "freely and used only when named: a hypothesis that reads a series it "
                "did not name in advance is mining, and with a few thousand series "
                "something always correlates."
            )


def ensure_series(
    connection: Any,
    code: str,
    *,
    provider: str,
    title: str = "",
    units: str = "",
    frequency: str = "",
    revision_policy: str = "",
    release_schedule: str = "",
    source_url: str = "",
) -> MacroSeries:
    """Get the series by code, creating it if new. Refuses to change its provider.

    A series's provider is part of what its rows mean — the same code can exist
    at two providers with different definitions — so changing it would silently
    restate every observation already attributed to it.
    """
    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {PROVIDERS}, got {provider!r}")
    if not code.strip() or code != code.strip():
        raise ValueError(f"series code {code!r} is empty or padded")

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, code, provider, title, units, frequency FROM macro_series WHERE code = %s",
            (code,),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                "INSERT INTO macro_series (code, provider, title, units, frequency, "
                "revision_policy, release_schedule, source_url) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id, code, provider, title, units, frequency",
                (
                    code,
                    provider,
                    title,
                    units,
                    frequency,
                    revision_policy,
                    release_schedule,
                    source_url,
                ),
            )
            row = cursor.fetchone()
            connection.commit()
            return MacroSeries(*row)
    existing = MacroSeries(*row)
    if existing.provider != provider:
        raise ValueError(
            f"series {code!r} is already recorded at provider {existing.provider!r}; "
            f"asked for {provider!r}. The same code can mean different things at two "
            "providers — use a distinct code rather than restating every observation."
        )
    return existing


def write_observations(
    connection: Any,
    series: MacroSeries,
    observations: Sequence[Observation],
    *,
    run_id: int | None = None,
    released_at: dict[date, datetime] | None = None,
) -> int:
    """Store observations. Returns how many rows were new.

    A revision is a **new row** with a later vintage, never an update: the whole
    value of this table is that it records what was believed, and an UPDATE
    destroys exactly that.

    The same (period, vintage) arriving twice with the same value is a re-fetch
    and is skipped. With a *different* value it raises `VintageConflict`: the
    provider has contradicted itself about what it published on a given day, and
    which figure is right is not a question a writer answers silently.
    """
    if not observations:
        return 0
    releases = released_at or {}

    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMPORARY TABLE incoming_macro "
            "(LIKE macro_observations INCLUDING DEFAULTS) ON COMMIT DROP"
        )
        with cursor.copy(
            "COPY incoming_macro (series_id, obs_date, vintage, value, released_at, "
            "ingest_run_id) FROM STDIN"
        ) as copy:
            for obs in observations:
                copy.write_row(
                    (
                        series.id,
                        obs.obs_date,
                        obs.vintage,
                        obs.value,
                        releases.get(obs.vintage),
                        run_id,
                    )
                )

        cursor.execute(
            "SELECT i.obs_date, i.vintage, m.value, i.value FROM incoming_macro i "
            "JOIN macro_observations m ON m.series_id = i.series_id "
            "AND m.obs_date = i.obs_date AND m.vintage = i.vintage "
            "WHERE m.value IS DISTINCT FROM i.value LIMIT 5"
        )
        clashes = cursor.fetchall()
        if clashes:
            obs_date, vintage, stored, incoming = clashes[0]
            connection.rollback()
            raise VintageConflict(
                f"{series.code}: the value published on {vintage} for {obs_date} is "
                f"recorded as {stored} and now arrives as {incoming}. A provider cannot "
                "have published two figures on one day; one of these is wrong. "
                f"{len(clashes)} clash(es); nothing was written."
            )

        cursor.execute(
            "INSERT INTO macro_observations (series_id, obs_date, vintage, value, "
            "released_at, ingest_run_id) "
            "SELECT series_id, obs_date, vintage, value, released_at, ingest_run_id "
            "FROM incoming_macro ON CONFLICT DO NOTHING"
        )
        written = cursor.rowcount
    connection.commit()
    return int(written)


def _require_series(connection: Any, code: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM macro_series WHERE code = %s", (code,))
        if cursor.fetchone() is None:
            cursor.execute("SELECT code FROM macro_series ORDER BY code LIMIT 40")
            known = [r[0] for r in cursor.fetchall()]
            raise ValueError(
                f"no series {code!r} in the store; known: {known}. Refusing rather than "
                "returning an empty series, which is indistinguishable from a period "
                "nobody published."
            )


def read_as_of(
    connection: Any,
    code: str,
    *,
    at: datetime,
    grant: SeriesGrant,
    start: date | None = None,
    end: date | None = None,
) -> list[tuple[date, Decimal | None]]:
    """What this series said, as it was known at `at`. The backtest reader.

    For each period, the most recent vintage **available** at `at` — where a
    value with no stated release time becomes available at the end of its
    vintage day. Periods whose first vintage had not yet been published are
    absent, which is correct: nobody had them.

    `grant` is not optional and there is no argument that turns it off.
    """
    grant.check(code)
    if at.tzinfo is None:
        raise ValueError(
            "`at` must be timezone-aware. This function's entire job is deciding what "
            "had been published by a moment, and a naive timestamp does not name one."
        )
    _require_series(connection, code)

    clauses = ["code = %s", "available_from <= %s"]
    params: list[Any] = [code, at]
    if start is not None:
        clauses.append("obs_date >= %s")
        params.append(start)
    if end is not None:
        clauses.append("obs_date <= %s")
        params.append(end)

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT ON (obs_date) obs_date, value FROM macro_available "
            "WHERE " + " AND ".join(clauses) + " ORDER BY obs_date, vintage DESC",
            params,
        )
        return [(row[0], row[1]) for row in cursor.fetchall()]


def read_latest(connection: Any, code: str) -> list[tuple[date, Decimal | None]]:
    """The most recent vintage of every period — **today's** view.

    What a chart shows and what a person means by "the CPI figure". **Never
    give this to a backtest**: reading it as of any past date is the revision
    trap, and it is the reason `read_as_of` exists. No grant is required
    precisely because this is a reporting call and not a research one; if you
    find yourself wanting it inside a strategy, you want `read_as_of`.
    """
    _require_series(connection, code)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT ON (obs_date) obs_date, value FROM macro_available "
            "WHERE code = %s ORDER BY obs_date, vintage DESC",
            (code,),
        )
        return [(row[0], row[1]) for row in cursor.fetchall()]


def revisions(connection: Any, code: str) -> list[dict[str, Any]]:
    """Periods that were revised, and by how much.

    `spread` is the size of the lookahead a single-vintage backtest would have
    granted itself on that period — the number that says whether ignoring
    vintages on this series is imprecise or wrong.
    """
    _require_series(connection, code)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT obs_date, vintages, first_published, last_revised, lowest, highest, "
            "spread FROM macro_revisions WHERE code = %s ORDER BY spread DESC NULLS LAST",
            (code,),
        )
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
