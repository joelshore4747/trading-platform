"""The daily review reads the journal, replays the day, and never overwrites.

Mutations these catch: skipping the divergence check when the session began
flat (it would say NOT CHECKED); overwriting an existing review; saving a
changed setup over a running account; the trial count ignoring records.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_platform.core.events import Bar
from trading_platform.live.recorder import JournalRecorder
from trading_platform.live.review import review_day, write_review
from trading_platform.live.setup import (
    PaperSetup,
    build_instruments,
    build_strategy,
    cost_model_for,
    load_setup,
    save_setup,
)
from trading_platform.live.stream import LiveFeed
from trading_platform.live.supervisor import PaperSupervisor, SupervisorConfig
from trading_platform.risk.limits import RiskLimits

T0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
SYMBOL = "BTC/USD"


def setup_for(account: str = "a1") -> PaperSetup:
    return PaperSetup(
        account=account,
        market="crypto",
        symbols=(SYMBOL,),
        interval="1m",
        strategy="sma",
        starting_cash="10000",
        cost_preset="binance-spot-bnb",
        daily_loss_pct=None,
        strategy_params={"fast": "2", "slow": "3"},
    )


def run_a_day(root: Path, prices: list[str]) -> None:
    setup = setup_for()
    save_setup(root / "accounts" / "a1", setup)
    recorder = JournalRecorder(root / "bars", "alpaca-crypto")
    feed = LiveFeed([SYMBOL], now=lambda: T0 + timedelta(days=30), recorder=recorder)
    for i, p in enumerate(prices):
        feed.publish(
            Bar(
                symbol=SYMBOL,
                timestamp=T0 + timedelta(minutes=i + 1),
                open=Decimal(p),
                high=Decimal(p),
                low=Decimal(p),
                close=Decimal(p),
                volume=Decimal("1"),
            )
        )
    feed.stop()
    recorder.close()
    cfg = SupervisorConfig(
        root=root,
        account="a1",
        starting_cash=Decimal(setup.starting_cash),
        instruments=build_instruments(setup.market, setup.symbols),
        cost_model=cost_model_for(setup),
        limits=RiskLimits(),
    )
    PaperSupervisor(cfg, build_strategy(setup), now=T0 - timedelta(hours=1)).run(feed)


def test_the_review_replays_the_day_and_agrees(tmp_path: Path) -> None:
    run_a_day(tmp_path, ["100", "101", "102", "103", "101", "99", "98", "100", "102", "104"])
    experiments = tmp_path / "experiments"
    experiments.mkdir()
    (experiments / "E-001-x.toml").write_text('id = "E-001"\nstrategy = "SMACrossover v1"\n')
    (experiments / "E-002-y.toml").write_text('id = "E-002"\nstrategy = "CrossSectionalMomentum"\n')

    review = review_day(tmp_path, "a1", T0, experiments_dir=experiments)
    assert review.divergence is not None, review.divergence_skipped_because
    assert review.divergence.agrees, review.divergence.report()
    assert review.divergence.live_fills >= 2, "the scenario must trade"
    assert review.trial_count == 1
    text = review.report()
    assert "AGREES" in text and "Account 1" in text and "1 experiment record(s)" in text
    assert "Sharpe" in text and "below the annualisation floor" in text


def test_a_review_is_written_once_and_a_rerun_gets_its_own_file(tmp_path: Path) -> None:
    run_a_day(tmp_path, ["100", "101", "102"])
    review = review_day(tmp_path, "a1", T0, experiments_dir=tmp_path / "none")
    first = write_review(review, tmp_path / "reviews")
    second = write_review(review, tmp_path / "reviews")
    assert first.name == "2026-09-01.md"
    assert second != first and second.name.startswith("2026-09-01.rerun-")
    assert first.read_text() == second.read_text()


def test_a_day_with_no_session_says_so(tmp_path: Path) -> None:
    run_a_day(tmp_path, ["100", "101", "102"])
    review = review_day(tmp_path, "a1", T0 + timedelta(days=3), experiments_dir=tmp_path / "none")
    assert review.divergence is None
    assert review.divergence_skipped_because == "no session ran today"
    assert "NOT CHECKED" in review.report()


def test_a_changed_setup_cannot_be_saved_over_a_running_account(tmp_path: Path) -> None:
    save_setup(tmp_path / "a1", setup_for())
    loaded = load_setup(tmp_path / "a1")
    assert loaded.strategy_fingerprint and replace(loaded, strategy_fingerprint="") == setup_for()
    changed = replace(setup_for(), strategy_params={"fast": "5", "slow": "9"})
    with pytest.raises(ValueError, match="new trial"):
        save_setup(tmp_path / "a1", changed)
    save_setup(tmp_path / "a1", setup_for())  # identical: fine


def test_setup_validation_refuses_the_unknown() -> None:
    with pytest.raises(ValueError, match="unknown strategy"):
        replace(setup_for(), strategy="magic")
    with pytest.raises(ValueError, match="unknown cost preset"):
        replace(setup_for(), cost_preset="free")
    with pytest.raises(ValueError, match="positive"):
        replace(setup_for(), starting_cash="0")


def test_a_strategy_source_change_is_flagged_as_an_unrecorded_trial(tmp_path: Path) -> None:
    """The fingerprint at first start is kept; a different current fingerprint
    is reported. Recomputing the stored one on every save would hide it."""
    run_a_day(tmp_path, ["100", "101", "102"])
    saved = load_setup(tmp_path / "accounts" / "a1")
    assert saved.strategy_fingerprint, "recorded at the first start"
    review = review_day(tmp_path, "a1", T0, experiments_dir=tmp_path / "none")
    assert review.strategy_changed is False
    assert "STRATEGY SOURCE CHANGED" not in review.report()

    # Simulate a code change: rewrite the stored fingerprint as if the source had moved.
    path = tmp_path / "accounts" / "a1" / "config.json"
    import json

    raw = json.loads(path.read_text())
    raw["strategy_fingerprint"] = "000000000000"
    path.write_text(json.dumps(raw))
    review = review_day(tmp_path, "a1", T0, experiments_dir=tmp_path / "none")
    assert review.strategy_changed is True
    assert "STRATEGY SOURCE CHANGED" in review.report()
    # And a restart with the same declared setup is still accepted: the
    # fingerprint is a report, not a gate. It must also leave the recorded
    # fingerprint alone — a save that rewrote it would hide the change on the
    # very next review (guard-auditor, 2026-09-03: the first version of this
    # test stopped here without looking).
    save_setup(tmp_path / "accounts" / "a1", setup_for())
    assert load_setup(tmp_path / "accounts" / "a1").strategy_fingerprint == "000000000000"
    again = review_day(tmp_path, "a1", T0, experiments_dir=tmp_path / "none")
    assert again.strategy_changed is True
