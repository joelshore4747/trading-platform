"""Documented market statistics the synthetic generator is calibrated to.

Every constant here has a source and a date. They live in one file rather than
inline in the generator because they are *claims about the world*, and claims
about the world go stale — BTC's annualised volatility fell from over 180% in
2013 to the mid-thirties by 2024. When that shifts again, one file changes and
the tests that assert the generator still matches reality fail loudly.

**Nothing here is a price.** These are distributional properties. The generator
produces synthetic data calibrated to them, which is a different thing from
real data and is labelled as such everywhere it surfaces. Calibrated synthetic
is honest; synthetic dressed as real is the dishonesty this repository exists
to prevent.

Why this file exists at all: the previous generator was measured against these
figures and handed out an annualised Sharpe of **6.36** in its trending regime
and **12.75** in recovery, against a real BTC buy-and-hold Sharpe of roughly
0.5-1.0 — a six- to thirteen-fold free lunch. Its returns also had a kurtosis of
2.82, *thinner*-tailed than a Gaussian, in an asset famous for fat tails. Every
number this project produced before that was fixed had been measured on a market
substantially easier than the real one.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

# -- Volatility ---------------------------------------------------------------
# Fidelity Digital Assets and iShares, retrieved 2026-08: BTC 30-day annualised
# realised volatility ranged 23-45% across 2025-2026, with the 2024 twelve-month
# figure around 35%. K33 Research put 2025's average daily volatility at 2.24%,
# the lowest annual reading on record (~43% annualised).

BTC_ANNUAL_VOL_LOW: Final = 0.23
BTC_ANNUAL_VOL_HIGH: Final = 0.45
BTC_ANNUAL_VOL_CENTRAL: Final = 0.40

#: Daily standard deviation of returns. 2.0-2.4% covers 2024 and 2025.
BTC_DAILY_SIGMA_LOW: Final = 0.020
BTC_DAILY_SIGMA_HIGH: Final = 0.024

# -- Distribution shape -------------------------------------------------------
# arXiv:1707.07618 (statistical properties and multifractality of Bitcoin) and
# arXiv:1909.04903 (GARCH estimation of Bitcoin volatility): returns are
# leptokurtic with short-ranged autocorrelation, a power-law ACF of absolute
# returns, and pronounced volatility clustering. Student-t innovations fit
# materially better than Gaussian.

#: Degrees of freedom for the Student-t innovations. Kurtosis of a t is
#: 3 + 6/(nu - 4), so nu=5 gives 9 — fat, and finite. nu=4 has infinite
#: kurtosis, which makes the generator's own tests unstable.
STUDENT_T_DF: Final = 5.0

#: Excess kurtosis a calibrated daily series must clear. Well above the
#: Gaussian 3.0, well below what a single outlier could fake.
MIN_DAILY_KURTOSIS: Final = 4.0

# -- Volatility clustering (GARCH(1,1)) ---------------------------------------
# sigma^2_t = omega + alpha * r^2_{t-1} + beta * sigma^2_{t-1}
# alpha + beta is the persistence: crypto studies routinely report 0.94-0.99.
# Long-run variance is omega / (1 - alpha - beta), so omega is derived from the
# target volatility rather than set by hand.

GARCH_ALPHA: Final = 0.09
GARCH_BETA: Final = 0.86

#: Lag-1 autocorrelation of squared returns a clustered series must exceed.
#: An i.i.d. series sits at zero; this is the check the old generator failed.
MIN_SQUARED_RETURN_AUTOCORR: Final = 0.02

# -- Drift --------------------------------------------------------------------
# The parameter that was wrong before. Buy-and-hold BTC has delivered a
# full-sample annualised Sharpe of roughly 0.5-1.0 over the last several years,
# depending on the window. A generator paying more than this is handing every
# long-biased strategy an edge that does not exist.

BTC_BUY_AND_HOLD_SHARPE_LOW: Final = 0.3
BTC_BUY_AND_HOLD_SHARPE_HIGH: Final = 1.2

#: Hard ceiling for any single regime's *conditional* Sharpe. A crash or a
#: recovery genuinely does run at an extreme rate while it lasts — but the old
#: generator's steady 6.36 in an ordinary uptrend was not a market, it was a
#: gift. Enforced by test.
MAX_REGIME_SHARPE: Final = 3.5

# -- Jumps --------------------------------------------------------------------
# Crashes are not the edge of a regime, they are a fat tail. A Poisson jump
# component produces days a GARCH process alone will not.

#: Probability per bar of a jump, in a calm regime. Roughly one a year daily.
JUMP_PROBABILITY_CALM: Final = 0.003
#: In a crashing regime. Roughly one bar in twenty-five.
JUMP_PROBABILITY_STRESSED: Final = 0.04

# -- Perpetual funding --------------------------------------------------------
# Binance funding history and CoinGlass, retrieved 2026-08: funding settles at
# 00:00, 08:00 and 16:00 UTC and is capped at +/-0.05% per 8-hour window for
# most contracts. Sustained rates above 0.01% per window mark crowded longs.

FUNDING_INTERVAL_HOURS: Final = 8
FUNDING_RATE_CAP: Final = Decimal("0.0005")
FUNDING_RATE_TYPICAL: Final = Decimal("0.0001")

#: Binance moved selected perpetuals from 8-hourly to 4-hourly in October 2023.
#: A multi-year series really does contain both, so code assuming a fixed
#: interval must be tested against one that is not.
FUNDING_INTERVAL_HOURS_ALTERNATE: Final = 4

# -- Fees ---------------------------------------------------------------------
# Binance published fee schedule, retrieved 2026-08. These are the numbers that
# showed the previous CostModel default (5 bp commission) to be optimistic by
# 2x for a retail spot trader.

#: Spot, VIP 0 — the tier essentially every individual is on.
BINANCE_SPOT_VIP0_MAKER: Final = Decimal("0.0010")
BINANCE_SPOT_VIP0_TAKER: Final = Decimal("0.0010")

#: Spot, VIP 0, paying fees in BNB: a 25% discount.
BINANCE_SPOT_BNB_TAKER: Final = Decimal("0.00075")

#: USD-margined perpetuals, VIP 0.
BINANCE_PERP_VIP0_MAKER: Final = Decimal("0.0002")
BINANCE_PERP_VIP0_TAKER: Final = Decimal("0.0005")

#: Half the bid-ask spread on a top-of-book pair such as BTCUSDT in normal
#: conditions. Wider in stress, and much wider on anything illiquid — treat
#: this as the optimistic end, which is why /cost-check exists.
BTC_HALF_SPREAD: Final = Decimal("0.0001")

# -- Option fees --------------------------------------------------------------
# Deribit published schedule, retrieved 2026-08. An option's commission is
# charged on the UNDERLYING, not on the premium, and that single fact is what
# made the spot model wrong by up to 125x when handed an option: at BTC 30,000
# and 55% IV a 36,000 strike seven days out costs 0.0069 under a 10bp premium
# charge and 0.87 under Deribit's schedule.

#: Per contract, as a fraction of the underlying's value. Both sides pay it;
#: there is no maker rebate on Deribit options.
DERIBIT_OPTION_UNDERLYING_RATE: Final = Decimal("0.0003")

#: ...but never more than this fraction of the option's own premium. The cap is
#: not a detail. Without it a far OTM option worth 6.94 would pay 9.00 to
#: trade — more than the contract — and the model would reject strategies for
#: the wrong reason. It is what turns a 1300x understatement into a 125x one.
DERIBIT_OPTION_PREMIUM_CAP: Final = Decimal("0.125")

#: Delivery at expiry, on the underlying, under the same 12.5% cap. Charged
#: once on settlement and easy to forget, which is how a strategy that holds to
#: expiry looks cheaper than one that closes early.
DERIBIT_OPTION_SETTLEMENT_RATE: Final = Decimal("0.00015")

#: Half the bid-ask spread as a fraction of PREMIUM, not of the underlying.
#: 5% is a tight quote on a liquid near-dated strike; deep OTM and long-dated
#: contracts routinely quote 20-50% wide, and in size the touch is not the
#: price. The spot model charged 1bp of premium here — optimistic by 500x —
#: and this figure is still the optimistic end of what is real.
CRYPTO_OPTION_HALF_SPREAD: Final = Decimal("0.05")

SOURCES: Final = (
    "https://arxiv.org/pdf/1707.07618",
    "https://arxiv.org/pdf/1909.04903",
    "https://link.springer.com/article/10.1186/s43093-025-00568-w",
    "https://www.fidelitydigitalassets.com/research-and-insights/closer-look-bitcoins-volatility",
    "https://www.ishares.com/us/insights/bitcoin-volatility-trends",
    "https://www.binance.com/en/futures/funding-history/perpetual/funding-fee-history",
    "https://www.coinglass.com/FundingRate/BTC",
    "https://binancemakertakerfee.org/",
    "https://www.deribit.com/kb/fees",
)
