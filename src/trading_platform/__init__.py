"""Trading platform: backtesting and paper trading for systematic strategies.

Public surface is re-exported here so strategy code can import from one place.
"""

from trading_platform.analytics.metrics import PerformanceSummary, summarise
from trading_platform.analytics.tearsheet import render_tearsheet, write_tearsheet
from trading_platform.analytics.trades import RoundTrip, TradeStats, summarise_trades
from trading_platform.analytics.walkforward import (
    SweepSurface,
    WalkForwardReport,
    sweep,
    walk_forward,
)
from trading_platform.analytics.windows import (
    Fold,
    TestWindow,
    TrainWindow,
    split_walk_forward,
)
from trading_platform.core.events import Bar, Fill, Order, Quote
from trading_platform.core.fx import (
    MixedCurrencyError,
    QuoteRates,
    RateObservation,
    StaleQuoteRate,
    UnknownQuoteRate,
    assume_pegged,
)
from trading_platform.core.instruments import (
    Instrument,
    Option,
    Perpetual,
    Spot,
    crypto_option,
    crypto_perpetual,
    crypto_spot,
    equity,
    equity_option,
)
from trading_platform.core.types import (
    AssetClass,
    OptionRight,
    OrderType,
    Side,
    TimeInForce,
)
from trading_platform.data.cache import CachedBarFeed, cache_path, read_bars, write_bars
from trading_platform.data.feed import CSVBarFeed, InMemoryBarFeed, synthetic_walk
from trading_platform.data.intervals import (
    interval_to_timedelta,
    timedelta_to_interval,
)
from trading_platform.data.panel import Panel
from trading_platform.engine.backtester import Backtester, BacktestResult
from trading_platform.engine.execution import (
    BINANCE_PERP_VIP0,
    BINANCE_SPOT_BNB,
    BINANCE_SPOT_VIP0,
    RETAIL_STRESSED,
    ZERO_COST,
    CostModel,
)
from trading_platform.engine.portfolio import Portfolio, Position, translate_curve
from trading_platform.risk.limits import RiskLimits, RiskManager
from trading_platform.strategy.base import Strategy, StrategyContext

__version__ = "0.1.0"

__all__ = [
    "BINANCE_PERP_VIP0",
    "BINANCE_SPOT_BNB",
    "BINANCE_SPOT_VIP0",
    "RETAIL_STRESSED",
    "ZERO_COST",
    "AssetClass",
    "BacktestResult",
    "Backtester",
    "Bar",
    "CSVBarFeed",
    "CachedBarFeed",
    "CostModel",
    "Fill",
    "Fold",
    "InMemoryBarFeed",
    "Instrument",
    "MixedCurrencyError",
    "Option",
    "OptionRight",
    "Order",
    "OrderType",
    "Panel",
    "PerformanceSummary",
    "Perpetual",
    "Portfolio",
    "Position",
    "Quote",
    "QuoteRates",
    "RateObservation",
    "RiskLimits",
    "RiskManager",
    "RoundTrip",
    "Side",
    "Spot",
    "StaleQuoteRate",
    "Strategy",
    "StrategyContext",
    "SweepSurface",
    "TestWindow",
    "TimeInForce",
    "TradeStats",
    "TrainWindow",
    "UnknownQuoteRate",
    "WalkForwardReport",
    "assume_pegged",
    "cache_path",
    "crypto_option",
    "crypto_perpetual",
    "crypto_spot",
    "equity",
    "equity_option",
    "interval_to_timedelta",
    "read_bars",
    "render_tearsheet",
    "split_walk_forward",
    "summarise",
    "summarise_trades",
    "sweep",
    "synthetic_walk",
    "timedelta_to_interval",
    "translate_curve",
    "walk_forward",
    "write_bars",
    "write_tearsheet",
]
