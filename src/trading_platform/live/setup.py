"""What a paper account runs: the strategy, instruments, costs — saved beside the journal.

`scripts/run_paper.py` writes `config.json` into the account directory on
every start and `scripts/daily_review.py` reads it back, so the review
replays the day with the same strategy the session ran, built by the same
code. A review that re-typed the parameters by hand would be measuring a
second program.

The strategy registry is deliberately tiny. Adding a strategy here is a
research decision (a new trial against the same data), not a convenience.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from pathlib import Path

from trading_platform.core.instruments import Instrument, crypto_spot, equity
from trading_platform.engine.execution import BINANCE_SPOT_BNB, RETAIL_STRESSED, CostModel
from trading_platform.strategy.base import Strategy
from trading_platform.strategy.examples.sma_crossover import SMACrossover

__all__ = [
    "COST_PRESETS",
    "STRATEGIES",
    "PaperSetup",
    "build_instruments",
    "build_strategy",
    "cost_model_for",
    "load_setup",
    "save_setup",
]

CONFIG_FILENAME = "config.json"

STRATEGIES: dict[str, type[Strategy]] = {
    "sma": SMACrossover,
}

COST_PRESETS: dict[str, CostModel] = {
    "default": CostModel(),
    "binance-spot-bnb": BINANCE_SPOT_BNB,
    "retail-stressed": RETAIL_STRESSED,
}


@dataclass(frozen=True, slots=True)
class PaperSetup:
    account: str
    market: str
    symbols: tuple[str, ...]
    interval: str
    strategy: str
    starting_cash: str
    cost_preset: str
    daily_loss_pct: str | None
    #: Strategy keyword arguments, as strings, so the review rebuilds the
    #: same object. Empty means the strategy's own defaults.
    strategy_params: dict[str, str]
    #: `strategy.versioning.fingerprint(...).digest` at the account's first
    #: start. Recorded, not enforced: the daily review compares it with the
    #: current source and flags a change as a new trial (charter §83). Empty
    #: for setups written before it existed.
    strategy_fingerprint: str = ""

    def __post_init__(self) -> None:
        if self.market not in ("crypto", "stocks"):
            raise ValueError(f"market must be crypto or stocks, got {self.market!r}")
        if not self.symbols:
            raise ValueError("a setup needs symbols")
        if self.strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy {self.strategy!r}; known: {sorted(STRATEGIES)}")
        if self.cost_preset not in COST_PRESETS:
            raise ValueError(
                f"unknown cost preset {self.cost_preset!r}; known: {sorted(COST_PRESETS)}"
            )
        if Decimal(self.starting_cash) <= 0:
            raise ValueError("starting_cash must be positive")


def build_instruments(market: str, symbols: Sequence[str]) -> dict[str, Instrument]:
    if market == "crypto":
        # Alpaca crypto is USD-quoted spot ("BTC/USD"); the quote is genuinely USD.
        return {s: crypto_spot(s, quote_currency="USD") for s in symbols}
    return {s: equity(s) for s in symbols}


def build_strategy(setup: PaperSetup) -> Strategy:
    cls = STRATEGIES[setup.strategy]
    params: dict[str, object] = {}
    for key, value in setup.strategy_params.items():
        params[key] = Decimal(value) if key == "allocation" else int(value)
    if setup.strategy == "sma":
        return cls(symbol=setup.symbols[0], **params)  # type: ignore[call-arg]
    return cls(**params)


def cost_model_for(setup: PaperSetup) -> CostModel:
    return COST_PRESETS[setup.cost_preset]


def save_setup(account_dir: Path, setup: PaperSetup) -> Path:
    """Write the setup. Refuses to change a setup that already exists: a
    changed strategy or cost on a running account is a new trial, which is a
    new account (or a new experiment record), not an edit.

    The strategy fingerprint is filled in here at the first save and left
    alone afterwards, so the comparison on later starts is on what the user
    chose, not on a hash the code changed under them."""
    path = Path(account_dir) / CONFIG_FILENAME
    if not setup.strategy_fingerprint:
        from trading_platform.strategy.versioning import fingerprint

        setup = replace(setup, strategy_fingerprint=fingerprint(build_strategy(setup)).digest)
    payload = asdict(setup)
    payload["symbols"] = list(setup.symbols)
    if path.exists():
        existing = load_setup(account_dir)
        if replace(existing, strategy_fingerprint="") != replace(setup, strategy_fingerprint=""):
            raise ValueError(
                f"{path} already describes a different setup for {setup.account!r}. "
                "Changing strategy, costs or symbols on an account is a new trial: "
                "start a new account, and record the experiment."
            )
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_setup(account_dir: Path) -> PaperSetup:
    path = Path(account_dir) / CONFIG_FILENAME
    raw = json.loads(path.read_text(encoding="utf-8"))
    return PaperSetup(
        account=raw["account"],
        market=raw["market"],
        symbols=tuple(raw["symbols"]),
        interval=raw["interval"],
        strategy=raw["strategy"],
        starting_cash=raw["starting_cash"],
        cost_preset=raw["cost_preset"],
        daily_loss_pct=raw.get("daily_loss_pct"),
        strategy_params=dict(raw.get("strategy_params", {})),
        strategy_fingerprint=str(raw.get("strategy_fingerprint", "")),
    )
