"""Quote-currency rates, and the refusal to guess one.

``BTCUSDT`` is not ``BTC/USD``. It is BTC priced in Tether, and Tether is a
security with its own price. Treating the two as the same thing is the default
behaviour of almost every crypto backtest, including this one until now — every
``Instrument`` has carried a ``quote_currency`` field that nothing read.

What that costs, measured on the real 32-day BTCUSDT sample in this repository
with a simulated depeg (see ``docs/adr/0002-quote-currency.md``):

===========================  ==============  ==============
metric                       USDT figures    USD figures
===========================  ==============  ==============
total return (round trip)          -23.75%        -23.75%
max drawdown                        25.12%         27.64%
annualised volatility               20.07%         54.61%
===========================  ==============  ==============

The trap is in the first row. A depeg that recovers inside the sample leaves
**total return exactly unchanged** — so the number quoted first is the one
number that hides the problem, while reported volatility is overstated by
2.7x. A depeg that does *not* recover moves terminal wealth by
``(1 + return) x depeg_size``: on buy-and-hold over this sample that is
1.195306 x 0.1211 = **14.48 percentage points**.

So the rule here is that a rate is either known or it is an error. There is no
default of 1.0. If you want to assume the peg held, ``assume_pegged`` makes you
type it.

**A rate series coarser than the bars understates the risk.** The free daily
sources are the ones reachable from this sandbox, and daily is an average. In
the real Coin Metrics series USDC's 2023-03-11 figure is **0.9706** — a 2.94%
dip, against intraday venue prints around 13%, understating the event 4.4x.
USDT on 2022-05-12 reads **0.9972** and the depeg does not appear at all. So a
daily series is better than assuming the peg and still not the truth; whichever
you use, say which, and do not read a drawdown off it as though it were
complete.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

__all__ = [
    "MixedCurrencyError",
    "QuoteRates",
    "RateObservation",
    "StaleQuoteRate",
    "UnknownQuoteRate",
    "assume_pegged",
]


class UnknownQuoteRate(LookupError):
    """No rate is known for an asset at a time. Never fall back to 1.0."""


class StaleQuoteRate(UnknownQuoteRate):
    """A rate exists but is too old to stand in for the requested instant."""


class MixedCurrencyError(ValueError):
    """A run combined instruments quoted in different currencies.

    The portfolio holds one cash number, so such a run has been adding
    unlike units together. The figures describe nothing.
    """


@dataclass(frozen=True, slots=True)
class RateObservation:
    """One asset's price in the reporting currency, at an instant.

    Stamped at the close of the interval it summarises, matching ``Bar``. A
    rate stamped T is therefore known at T and may be used at T.
    """

    timestamp: datetime
    rate: Decimal

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError(
                f"rate observation at {self.timestamp!r} has no timezone; a naive "
                "stamp cannot be aligned with UTC bars"
            )
        if self.rate <= 0:
            raise ValueError(f"rate must be positive, got {self.rate}")


@dataclass(slots=True)
class QuoteRates:
    """Rates from quote assets into one reporting currency.

    Lookup is **step-back only**: the most recent observation at or before the
    requested instant. Interpolating toward the next observation would let a
    Tuesday rate inform a Monday mark, which is the same defect as filling an
    order on the bar that generated its signal.

    ``max_staleness`` bounds how long one observation may stand in. A daily
    series carried across three years of minute bars is not a rate, it is a
    guess wearing a timestamp. ``None`` carries forward indefinitely and is
    the right choice only when the series is at least as frequent as the bars.

    **Keyed on asset alone, which is a known limit.** USDT genuinely traded at
    different prices on different venues at the same instant — below $1 on
    Kraken and above it on Bitfinex on 2022-05-12. A multi-venue run therefore
    needs an ``(asset, venue)`` key. Nothing here is multi-venue yet, so the
    simpler key stands; it is recorded rather than discovered later.
    """

    reporting_currency: str = "USD"
    max_staleness: timedelta | None = None
    #: Assets asserted to be worth exactly 1.0 of the reporting currency at
    #: every instant. Always a claim about the world, never a fallback — see
    #: `assume_pegged`, which is the only intended way to populate it.
    pegged: frozenset[str] = frozenset()
    _series: dict[str, list[RateObservation]] = field(default_factory=dict)
    _stamps: dict[str, list[datetime]] = field(default_factory=dict)

    def add(self, asset: str, observations: list[RateObservation]) -> None:
        """Register (or extend) one asset's series. Sorted and deduped here."""
        if asset == self.reporting_currency:
            raise ValueError(
                f"{asset!r} is the reporting currency; its rate is 1 by definition "
                "and must not be overridden"
            )
        merged = {o.timestamp: o for o in self._series.get(asset, ())}
        merged.update({o.timestamp: o for o in observations})
        ordered = [merged[t] for t in sorted(merged)]
        self._series[asset] = ordered
        self._stamps[asset] = [o.timestamp for o in ordered]

    @property
    def assets(self) -> tuple[str, ...]:
        return tuple(sorted(self._series))

    def covers(self, asset: str) -> bool:
        """True when ``rate`` could return something for this asset."""
        return asset == self.reporting_currency or asset in self._series or asset in self.pegged

    def rate(self, asset: str, at: datetime) -> Decimal:
        """The asset's price in the reporting currency, as known at ``at``.

        Raises rather than guessing. That is the entire point of this class.
        """
        if asset == self.reporting_currency or asset in self.pegged:
            return Decimal("1")

        stamps = self._stamps.get(asset)
        if stamps is None:
            raise UnknownQuoteRate(
                f"no rate series for {asset!r} into {self.reporting_currency}. "
                f"Known: {list(self.assets) or 'none'}. Supply one, or call "
                f"assume_pegged({asset!r}) if you mean to assert the peg held."
            )

        index = bisect_right(stamps, at) - 1
        if index < 0:
            raise UnknownQuoteRate(
                f"{asset!r} has no observation at or before {at.isoformat()}; the "
                f"series starts at {stamps[0].isoformat()}. Extending it backwards "
                "would assume a rate nobody observed."
            )

        observation = self._series[asset][index]
        if self.max_staleness is not None and at - observation.timestamp > self.max_staleness:
            raise StaleQuoteRate(
                f"{asset!r} rate at {at.isoformat()} would rely on an observation "
                f"from {observation.timestamp.isoformat()}, "
                f"{at - observation.timestamp} stale (limit {self.max_staleness})."
            )
        return observation.rate


def assume_pegged(*assets: str, reporting_currency: str = "USD") -> QuoteRates:
    """Rates that are 1.0 for the named assets, for all time.

    An assumption, and a false one during exactly the episodes that matter.
    USDT traded around $0.94-0.95 on 2022-05-12 during the Terra/LUNA
    collapse, and USDC around $0.87-0.88 on 2023-03-11 after Circle disclosed
    roughly $3.3bn of reserves at Silicon Valley Bank.

    Those are ranges rather than prices because **there was no single price**.
    Reported venue lows on 2023-03-11 ran from about $0.87 on Kraken to about
    $0.74 on Binance, and on 2022-05-12 USDT was below $1 on Kraken and above
    it on Bitfinex at the same moment — that spread was the arbitrage. Which
    is the strongest argument for this whole module: if you cannot say what the
    rate was, an object that refuses to guess is not pedantry.

    It exists so that asserting the peg is a line of code someone wrote and a
    reviewer can see, rather than the silent default it used to be.

    Combines with real series — peg one asset, measure another — because
    ``pegged`` is a field on the same object rather than a separate mode.
    """
    return QuoteRates(
        reporting_currency=reporting_currency,
        pegged=frozenset(a for a in assets if a != reporting_currency),
    )
