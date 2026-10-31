"""The daily review: one day of one paper account, reduced to what a person decides on.

Built for the loop the platform exists for: the paper trader runs all day, and
the next morning a session (human, or Claude on `/daily-review`) reads this
report and decides — keep, change, retire. The report is written once per day
into `harness/reviews/<account>/<day>.md` and never overwritten: a rerun gets
a new file, so a retracted conclusion stays beside the one that replaced it.

What it contains, in order of how much it should change a decision:

1. **Did the two paths agree?** The session's fills against a plain
   `Backtester` over the bars the feed recorded. A divergence is a bug, and
   nothing below it means anything until the bug is found. Reported only when
   the check is valid — one session that began flat — and otherwise stated
   as not checkable, never silently skipped.
2. **What the risk layer refused.** Intent refused as a share of intent.
   A day of three fills and four hundred refusals is a measurement of the
   risk layer, not of the strategy.
3. **The account.** Equity change, fills by symbol, halts, and the cycle
   table across the account's whole life.
4. **The trial count.** How many experiment records name this strategy. Every
   change during the loop is a trial against the same data; the number is
   printed so the next Sharpe is deflated for it, not for one.

No Sharpe is printed for a single day. The window is far below the month the
annualisation floor requires, and a number that cannot be annualised is not
made meaningful by printing it un-annualised.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trading_platform.engine.backtester import Backtester
from trading_platform.live.accounts import ACCOUNTS_DIRNAME, AccountCycles
from trading_platform.live.journal import (
    DaySummary,
    EventKind,
    JournalEvent,
    decisions_path,
    read_decisions,
    summarise_day,
)
from trading_platform.live.replay import DivergenceReport, compare, replay_feed
from trading_platform.live.setup import (
    PaperSetup,
    build_instruments,
    build_strategy,
    cost_model_for,
    load_setup,
)
from trading_platform.live.supervisor import _fill_from
from trading_platform.risk.limits import RiskLimits, RiskManager
from trading_platform.strategy.versioning import fingerprint

__all__ = ["DailyReview", "review_day", "write_review"]


@dataclass(frozen=True, slots=True)
class DailyReview:
    account: str
    day: str
    setup: PaperSetup
    summary: DaySummary
    divergence: DivergenceReport | None
    divergence_skipped_because: str
    cycles: str
    trial_count: int
    #: The strategy's fingerprint now vs at the account's first start. A
    #: difference means the code changed under a running account: a new
    #: trial that nobody recorded (charter §83).
    fingerprint_now: str = ""
    fingerprint_at_start: str = ""

    @property
    def strategy_changed(self) -> bool:
        return bool(self.fingerprint_at_start) and self.fingerprint_now != self.fingerprint_at_start

    def report(self) -> str:
        lines = [
            f"# {self.account} — {self.day}",
            "",
            f"Strategy `{self.setup.strategy}` {self.setup.strategy_params or '(defaults)'} on "
            f"{', '.join(self.setup.symbols)} ({self.setup.market}, {self.setup.interval}), "
            f"costs `{self.setup.cost_preset}`.",
            "",
            "## 1. Did the two paths agree?",
        ]
        if self.divergence is None:
            lines.append(f"NOT CHECKED — {self.divergence_skipped_because}")
        else:
            lines.append(self.divergence.report())
        lines += ["", "## 2. What the risk layer refused"]
        refused = self.summary.intent_refused
        lines.append(
            "no order intent today"
            if refused is None
            else f"{refused:.1%} of intent refused: {self.summary.rejections_by_reason or 'none'}"
        )
        lines += ["", "## 3. The account", self.summary.report(), "", "### Cycles", self.cycles]
        if self.strategy_changed:
            lines += [
                "",
                f"**STRATEGY SOURCE CHANGED** since this account started "
                f"({self.fingerprint_at_start} -> {self.fingerprint_now}). Every day from the "
                "change is a new trial that has no experiment record. Record it, or start a "
                "new account.",
            ]
        lines += [
            "",
            "## 4. Trial count",
            f"{self.trial_count} experiment record(s) name strategy `{self.setup.strategy}`. "
            "Every change made in this loop is a trial against the same data: the next Sharpe "
            f"is deflated for {max(self.trial_count, 1)} trial(s) (`analytics/multiplicity.py`), "
            "and a parameter change is a new record (`scripts/experiment.py new`), never an edit.",
            "",
            "No Sharpe for one day: the window is below the annualisation floor.",
        ]
        return "\n".join(lines)


def _events_for_day(account_dir: Path, day: datetime) -> list[JournalEvent]:
    path = decisions_path(account_dir, day)
    if not path.is_file():
        return []
    return read_decisions(path)


def _divergence(
    setup: PaperSetup, root: Path, events: list[JournalEvent], day: datetime
) -> tuple[DivergenceReport | None, str]:
    sessions = sorted({e.session for e in events})
    if not sessions:
        return None, "no session ran today"
    if len(sessions) > 1:
        return (
            None,
            f"{len(sessions)} sessions today; replay identity holds per session that began flat",
        )
    first_mark = next((e for e in events if e.kind is EventKind.MARK and e.account), None)
    if first_mark is None or first_mark.account is None:
        return None, "no mark recorded"
    if first_mark.account.positions:
        return None, "the session began with open positions; a replay from flat would not match"
    fills = [_fill_from(e) for e in events if e.kind is EventKind.FILL]
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(hour=23, minute=59, second=59)
    feed = replay_feed(root / "bars", f"alpaca-{setup.market}", setup.symbols, start=start, end=end)
    if not feed.symbols:
        return None, "no bars were recorded for today"
    result = Backtester(
        instruments=build_instruments(setup.market, setup.symbols),
        starting_cash=Decimal(setup.starting_cash),
        cost_model=cost_model_for(setup),
        risk_manager=RiskManager(limits=RiskLimits()),
    ).run(build_strategy(setup), feed)
    return compare(fills, result.fills), ""


def _trial_count(experiments_dir: Path, strategy: str) -> int:
    if not experiments_dir.is_dir():
        return 0
    pattern = re.compile(r'^strategy\s*=\s*"(.*)"', re.M)
    count = 0
    for path in experiments_dir.glob("E-*.toml"):
        m = pattern.search(path.read_text(encoding="utf-8"))
        if m and strategy.lower() in m.group(1).lower():
            count += 1
    return count


def review_day(
    root: Path, account: str, day: datetime, *, experiments_dir: Path | None = None
) -> DailyReview:
    if day.tzinfo is None:
        raise ValueError("day must be timezone-aware")
    account_dir = Path(root) / ACCOUNTS_DIRNAME / account
    setup = load_setup(account_dir)
    events = _events_for_day(account_dir, day)
    summary = summarise_day(events) if events else summarise_day([])
    divergence, why = _divergence(setup, Path(root), events, day)
    cycles = AccountCycles(
        root=root, account=account, starting_cash=Decimal(setup.starting_cash), now=day
    )
    trials = _trial_count(
        experiments_dir if experiments_dir is not None else Path("harness") / "experiments",
        setup.strategy,
    )
    return DailyReview(
        account=account,
        day=f"{day.astimezone(UTC):%Y-%m-%d}",
        setup=setup,
        summary=summary,
        divergence=divergence,
        divergence_skipped_because=why,
        cycles=cycles.report(),
        trial_count=trials,
        fingerprint_now=fingerprint(build_strategy(setup)).digest,
        fingerprint_at_start=setup.strategy_fingerprint,
    )


def write_review(review: DailyReview, reviews_dir: Path) -> Path:
    """Write once; a rerun gets its own file. Nothing here overwrites."""
    directory = Path(reviews_dir) / review.account
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{review.day}.md"
    if path.exists():
        stamp = datetime.now(UTC).strftime("%H%M%S")
        path = directory / f"{review.day}.rerun-{stamp}.md"
    path.write_text(review.report() + "\n", encoding="utf-8")
    return path
