"""The tearsheet.

The most dangerous artefact in this repository. An equity curve rising left to
right is the most convincing thing in systematic trading, and it is exactly as
convincing when the strategy does not work. So the design question is not which
charts to draw — it is **what this page makes impossible to skip**.

Four answers, all structural rather than advisory:

- **The cost model is a required argument** and prints in the header. A chart
  with unstated costs is not a result, and ``ZERO_COST`` renders a banner
  saying the page is an engine check rather than evidence.
- **Verdicts render above the equity curve.** Caveats placed under a chart are
  caveats nobody reads.
- **Buy-and-hold is drawn on the same axis, always.** A lone strategy curve
  answers "did it go up", which is the wrong question.
- **The equity axis is logarithmic by default**, so equal percentage moves are
  equal distances instead of the last year dominating the picture.

The page is entirely self-contained: no external stylesheet, script, font, or
image. A test asserts there is no ``http`` reference in the output.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from html import escape
from pathlib import Path

from trading_platform.analytics.charts import Box, Scale, histogram, legend, line_chart
from trading_platform.analytics.metrics import (
    CRYPTO_DAYS_PER_YEAR,
    PerformanceSummary,
    sharpe_ratio,
    summarise,
    to_returns,
)
from trading_platform.analytics.trades import TradeStats, summarise_trades
from trading_platform.analytics.walkforward import WalkForwardReport
from trading_platform.core.instruments import Instrument
from trading_platform.engine.backtester import BacktestResult
from trading_platform.engine.execution import CostModel
from trading_platform.engine.portfolio import EquityPoint

#: Default rolling-Sharpe window: a quarter of a crypto trading year.
DEFAULT_ROLLING_WINDOW = 91

_STRATEGY_COLOUR = "--series-1"
_BENCHMARK_COLOUR = "--series-2"

_STANDING_WARNING = (
    "This is a single backtest over one period, with parameters chosen by a human "
    "who has seen this data. It is in-sample by construction. Run "
    "<code>/walk-forward</code> before treating any number on this page as evidence."
)


@dataclass(frozen=True, slots=True)
class _Panel:
    title: str
    body: str
    note: str = ""


def render_tearsheet(
    result: BacktestResult,
    *,
    cost_model: CostModel,
    instruments: Mapping[str, Instrument] | None = None,
    benchmark: Sequence[EquityPoint] | None = None,
    benchmark_label: str = "Buy and hold (cost-free)",
    walk_forward: WalkForwardReport | None = None,
    periods_per_year: int = CRYPTO_DAYS_PER_YEAR,
    log_scale: bool = True,
    rolling_window: int = DEFAULT_ROLLING_WINDOW,
    title: str = "",
) -> str:
    """Render a complete, self-contained HTML page for one backtest run."""
    curve = result.equity_curve
    if not curve:
        raise ValueError(
            "This run produced no equity curve, so there is nothing to draw. A "
            "tearsheet of an empty run would imply a result that does not exist."
        )

    summary = summarise(curve, periods_per_year=periods_per_year)
    trades = summarise_trades(result.fills, instruments)
    heading = title or f"{result.strategy_name} — tearsheet"

    panels = [
        _equity_panel(curve, benchmark, benchmark_label, walk_forward, log_scale),
        _underwater_panel(curve),
        _rolling_sharpe_panel(curve, periods_per_year, rolling_window),
        _trades_panel(trades),
        _stats_panel(summary, trades, result),
    ]

    return _PAGE.format(
        title=escape(heading),
        style=_STYLE,
        header=_header(result, cost_model, summary),
        warnings=_warnings(cost_model, walk_forward, trades),
        panels="\n".join(_render_panel(p) for p in panels),
        script=_SCRIPT,
    )


def write_tearsheet(
    result: BacktestResult,
    path: str | Path,
    **kwargs: object,
) -> Path:
    """Render and write to ``path``. Parent directories are created.

    ``runs/`` is gitignored — a tearsheet is a regenerable artefact, not a
    source file, and committing one invites reading a stale chart as current.
    """
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_tearsheet(result, **kwargs), encoding="utf-8")  # type: ignore[arg-type]
    return destination


# -- Panels ------------------------------------------------------------------


def _equity_panel(
    curve: Sequence[EquityPoint],
    benchmark: Sequence[EquityPoint] | None,
    benchmark_label: str,
    walk_forward: WalkForwardReport | None,
    log_scale: bool,
) -> _Panel:
    box = Box(height=320)
    strategy_points = [(_x(p.timestamp), float(p.equity)) for p in curve]
    all_series = [("Strategy", _STRATEGY_COLOUR, strategy_points)]

    values = [y for _, y in strategy_points]
    if benchmark:
        # Indexed to the strategy's starting equity so both sit on ONE axis.
        # Two y-scales would invent a relationship the data does not contain.
        start = float(curve[0].equity)
        base = float(benchmark[0].equity) or 1.0
        bench_points = [(_x(p.timestamp), float(p.equity) / base * start) for p in benchmark]
        all_series.append((benchmark_label, _BENCHMARK_COLOUR, bench_points))
        values += [y for _, y in bench_points]

    xs = [x for series in all_series for x, _ in series[2]]
    usable = [v for v in values if v > 0] if log_scale else values
    if log_scale and len(usable) < 2:
        log_scale = False
        usable = values

    y_scale = Scale(min(usable), max(usable), box.plot_bottom, box.plot_top, log=log_scale)
    x_scale = Scale(min(xs), max(xs), box.plot_left, box.plot_right)

    shaded = []
    if walk_forward is not None:
        shaded = [(_x(f.test_start), _x(f.test_end)) for f in walk_forward.folds]

    axis_note = (
        "Logarithmic equity axis: equal percentage moves are equal distances. On a "
        "linear axis a late 20% gain draws larger than an early 50% one."
        if log_scale
        else "Linear equity axis — late moves draw larger than equal-percentage early ones."
    )
    if shaded:
        axis_note += " Shaded bands are out-of-sample windows."

    return _Panel(
        title="Equity",
        body=legend([(n, c) for n, c, _ in all_series])
        + line_chart(
            all_series,
            box=box,
            y_scale=y_scale,
            x_scale=x_scale,
            shaded=shaded,
            label="Equity curve against buy and hold",
        ),
        note=axis_note,
    )


def _underwater_panel(curve: Sequence[EquityPoint]) -> _Panel:
    """Drawdown from the running peak.

    A max-drawdown number says how deep. This says how long, which is what
    actually decides whether a strategy gets switched off before it recovers.
    """
    box = Box(height=200)
    peak = float(curve[0].equity)
    points: list[tuple[float, float]] = []
    for point in curve:
        value = float(point.equity)
        peak = max(peak, value)
        points.append((_x(point.timestamp), -((peak - value) / peak) if peak else 0.0))

    depths = [y for _, y in points]
    y_scale = Scale(min([*depths, 0.0]), 0.0, box.plot_bottom, box.plot_top)
    x_scale = Scale(points[0][0], points[-1][0], box.plot_left, box.plot_right)

    return _Panel(
        title="Underwater",
        body=line_chart(
            [("Drawdown", _STRATEGY_COLOUR, points)],
            box=box,
            y_scale=y_scale,
            x_scale=x_scale,
            y_format="{:.1%}",
            fill_first=True,
            label="Drawdown from running peak",
        ),
        note=(
            "Depth below the running peak. The same colour as the equity curve because "
            "it is the same entity, not a separate series."
        ),
    )


def _rolling_sharpe_panel(
    curve: Sequence[EquityPoint], periods_per_year: int, window: int
) -> _Panel:
    """How unstable the single headline Sharpe is.

    A run reported as "Sharpe 1.4" whose rolling Sharpe swings between -2 and 4
    has not measured a property of the strategy. It has averaged some noise.
    """
    box = Box(height=200)
    returns = to_returns(curve)
    if len(returns) <= window:
        return _Panel(
            title=f"Rolling Sharpe ({window} periods)",
            body='<p class="empty">Series too short for this window.</p>',
            note="A rolling figure over a window longer than the run would be one number.",
        )

    points = [
        (
            _x(curve[i + 1].timestamp),
            sharpe_ratio(returns[i + 1 - window : i + 1], periods_per_year),
        )
        for i in range(window - 1, len(returns))
    ]
    values = [y for _, y in points]
    y_scale = Scale(min([*values, 0.0]), max([*values, 0.0]), box.plot_bottom, box.plot_top)
    x_scale = Scale(points[0][0], points[-1][0], box.plot_left, box.plot_right)
    spread = max(values) - min(values)

    return _Panel(
        title=f"Rolling Sharpe ({window} periods)",
        body=line_chart(
            [("Rolling Sharpe", _STRATEGY_COLOUR, points)],
            box=box,
            y_scale=y_scale,
            x_scale=x_scale,
            y_format="{:.1f}",
            zero_rule=True,
            label="Rolling Sharpe ratio",
        ),
        note=(
            f"Range {min(values):.2f} to {max(values):.2f}, a spread of {spread:.2f}. "
            "A headline Sharpe means little when the rolling figure crosses zero."
        ),
    )


def _trades_panel(trades: TradeStats) -> _Panel:
    values = [float(t.net_pnl) for t in trades.trades]
    without_best = trades.pnl_without_best_n(1)
    note = (
        f"{trades.count} closed trade(s). Removing the single best takes net P&amp;L from "
        f"{trades.total_net_pnl:,.2f} to {without_best:,.2f}."
    )
    if trades.open_quantity:
        still_open = ", ".join(f"{s} {q}" for s, q in sorted(trades.open_quantity.items()))
        note += (
            f" Still open and excluded from this distribution: {escape(still_open)} — "
            "which is why these totals do not tie to the equity curve."
        )
    return _Panel(
        title="Trade distribution",
        body=histogram(values, box=Box(height=200), label="Net P&L per closed trade"),
        note=note,
    )


def _stats_panel(summary: PerformanceSummary, trades: TradeStats, result: BacktestResult) -> _Panel:
    performance = [
        ("Total return", f"{summary.total_return:.2%}"),
        ("CAGR", "n/a" if summary.cagr is None else f"{summary.cagr:.2%}"),
        ("Volatility (ann.)", f"{summary.annualised_volatility:.2%}"),
        ("Sharpe", f"{summary.sharpe_ratio:.2f}"),
        ("Sortino", f"{summary.sortino_ratio:.2f}"),
        ("Max drawdown", f"{summary.max_drawdown:.2%}"),
        ("Max DD duration", f"{summary.max_drawdown_duration} periods"),
        ("Calmar", "n/a" if summary.calmar_ratio is None else f"{summary.calmar_ratio:.2f}"),
        ("Win rate (per period)", f"{summary.win_rate:.2%}"),
        ("Periods", f"{summary.num_periods}"),
    ]
    trade_rows = [
        ("Closed trades", f"{trades.count}"),
        ("Win rate (per trade)", f"{trades.win_rate:.2%}"),
        ("Net P&L", f"{trades.total_net_pnl:,.2f}"),
        ("Commission paid", f"{trades.total_commission:,.2f}"),
        ("Average win", f"{trades.average_win:,.2f}"),
        ("Average loss", f"{trades.average_loss:,.2f}"),
        (
            "Profit factor",
            "n/a" if trades.profit_factor is None else f"{trades.profit_factor:.2f}",
        ),
        ("Largest win", f"{trades.largest_win:,.2f}"),
        ("Median hold", str(trades.median_duration)),
        ("Risk rejections", result.rejection_summary_text()),
    ]
    return _Panel(
        title="Statistics",
        body=(
            '<div class="tables">'
            + _table("Per period", performance)
            + _table("Per trade", trade_rows)
            + "</div>"
        ),
        note=(
            "The two win rates are different measurements and are routinely confused. "
            "Per period counts bars that closed up; per trade counts round trips that "
            "made money net of costs."
        ),
    )


# -- Page furniture ----------------------------------------------------------


def _header(result: BacktestResult, cost_model: CostModel, summary: PerformanceSummary) -> str:
    costs = (
        f"commission {_bp(cost_model.commission_rate)} bp, "
        f"slippage {_bp(cost_model.slippage_rate)} bp, "
        f"half-spread {_bp(cost_model.half_spread_rate)} bp"
    )
    facts = [
        ("Period", f"{_date(result.start)} to {_date(result.end)}"),
        ("Bars", f"{result.bars_processed:,}"),
        ("Starting cash", f"{result.starting_cash:,.2f}"),
        ("Ending equity", f"{result.ending_equity:,.2f}"),
        ("Total return", f"{summary.total_return:.2%}"),
        ("Fills", f"{len(result.fills):,}"),
        ("Cost model", costs),
    ]
    if result.halted:
        facts.append(("Status", "HALTED — max drawdown breached"))
    if result.funding_settlements:
        facts.append(("Funding", f"{result.total_funding:,.2f} over {result.funding_settlements}"))
    rows = "".join(
        f'<div class="fact"><dt>{escape(k)}</dt><dd>{escape(v)}</dd></div>' for k, v in facts
    )
    return f'<dl class="facts">{rows}</dl>'


def _warnings(
    cost_model: CostModel,
    walk_forward: WalkForwardReport | None,
    trades: TradeStats,
) -> str:
    """Everything wrong with this page, above the charts rather than below."""
    blocks: list[str] = []

    if cost_model.commission_rate == 0 and cost_model.slippage_rate == 0:
        blocks.append(
            _banner(
                "critical",
                "Frictionless run — not a result",
                "This page was rendered at zero cost. ZERO_COST exists to isolate an "
                "engine bug. Nothing here says whether the strategy is profitable.",
            )
        )

    if walk_forward is None:
        blocks.append(_banner("warning", "In-sample", _STANDING_WARNING))
    else:
        verdicts = walk_forward.verdicts
        if verdicts:
            items = "".join(f"<li>{escape(v)}</li>" for v in verdicts)
            blocks.append(
                _banner(
                    "critical",
                    f"Walk-forward raised {len(verdicts)} objection(s)",
                    f"<ul>{items}</ul>",
                )
            )
        else:
            blocks.append(
                _banner(
                    "good",
                    "Walk-forward raised no objection",
                    "This run failed to falsify an edge, on this data, at this cost "
                    "model. That is not the same as having found one.",
                )
            )

    concentration = trades.concentration_verdict
    if concentration is not None:
        blocks.append(_banner("serious", "Trade concentration", escape(concentration)))

    return "\n".join(blocks)


def _banner(status: str, heading: str, body: str) -> str:
    """Status colour never carries meaning alone — it ships with icon and label."""
    icons = {"good": "✓", "warning": "!", "serious": "▲", "critical": "✕"}
    return (
        f'<div class="banner {status}">'
        f'<span class="icon" aria-hidden="true">{icons[status]}</span>'
        f'<div><p class="banner-title">{escape(heading)}</p><div>{body}</div></div>'
        f"</div>"
    )


def _table(caption: str, rows: Sequence[tuple[str, str]]) -> str:
    body = "".join(
        f'<tr><th scope="row">{escape(k)}</th><td>{escape(v)}</td></tr>' for k, v in rows
    )
    return f"<table><caption>{escape(caption)}</caption><tbody>{body}</tbody></table>"


def _render_panel(panel: _Panel) -> str:
    note = f'<p class="note">{panel.note}</p>' if panel.note else ""
    return f'<section class="panel"><h2>{escape(panel.title)}</h2>{panel.body}{note}</section>'


def _x(timestamp: datetime) -> float:
    return timestamp.timestamp()


def _date(value: datetime | None) -> str:
    return "—" if value is None else value.strftime("%Y-%m-%d")


def _bp(rate: Decimal) -> str:
    return f"{rate * 10000:.1f}"


_STYLE = """
:root {
  color-scheme: light;
  --page: #f9f9f7;  --surface-1: #fcfcfb;
  --text-primary: #0b0b0b;  --text-secondary: #52514e;  --text-muted: #898781;
  --grid: #e1e0d9;  --axis: #c3c2b7;  --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6;  --series-2: #eb6834;  --series-8: #e34948;
  --good: #0ca30c;  --warning: #fab219;  --serious: #ec835a;  --critical: #d03b3b;
  --oos: rgba(42,120,214,0.07);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d;  --surface-1: #1a1a19;
    --text-primary: #ffffff;  --text-secondary: #c3c2b7;  --text-muted: #898781;
    --grid: #2c2c2a;  --axis: #383835;  --border: rgba(255,255,255,0.10);
    --series-1: #3987e5;  --series-2: #d95926;  --series-8: #e66767;
    --oos: rgba(57,135,229,0.10);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d;  --surface-1: #1a1a19;
  --text-primary: #ffffff;  --text-secondary: #c3c2b7;  --text-muted: #898781;
  --grid: #2c2c2a;  --axis: #383835;  --border: rgba(255,255,255,0.10);
  --series-1: #3987e5;  --series-2: #d95926;  --series-8: #e66767;
  --oos: rgba(57,135,229,0.10);
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 20px 64px;
  background: var(--page); color: var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 15px; line-height: 1.55;
}
main { max-width: 1000px; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 4px; }
h2 { font-size: 0.95rem; font-weight: 600; margin: 0 0 12px;
     color: var(--text-secondary); letter-spacing: 0.01em; }
.facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
         gap: 12px 24px; margin: 20px 0 24px; padding: 16px 20px;
         background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; }
.fact dt { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em;
           color: var(--text-muted); }
.fact dd { margin: 2px 0 0; font-variant-numeric: tabular-nums; }
.banner { display: flex; gap: 12px; align-items: flex-start; padding: 14px 18px;
          margin: 0 0 12px; border-radius: 10px; border: 1px solid var(--border);
          background: var(--surface-1); border-left: 4px solid var(--text-muted); }
.banner .icon { flex: none; width: 22px; height: 22px; border-radius: 50%;
                display: grid; place-items: center; font-size: 13px; font-weight: 700;
                color: #fff; background: var(--text-muted); }
.banner-title { margin: 0 0 4px; font-weight: 650; }
.banner p, .banner ul { margin: 0; }
.banner ul { padding-left: 20px; }
.banner li { margin: 4px 0; }
.banner.good { border-left-color: var(--good); }
.banner.good .icon { background: var(--good); }
.banner.warning { border-left-color: var(--warning); }
.banner.warning .icon { background: var(--warning); color: #0b0b0b; }
.banner.serious { border-left-color: var(--serious); }
.banner.serious .icon { background: var(--serious); color: #0b0b0b; }
.banner.critical { border-left-color: var(--critical); }
.banner.critical .icon { background: var(--critical); }
.panel { margin: 24px 0 0; padding: 18px 20px 14px; background: var(--surface-1);
         border: 1px solid var(--border); border-radius: 10px; }
.chart { display: block; width: 100%; height: auto; overflow: visible; }
.chart .grid { stroke: var(--grid); stroke-width: 1; }
.chart .axis { stroke: var(--axis); stroke-width: 1; }
.chart .zero { stroke: var(--axis); stroke-width: 1.5; }
.chart .series { stroke-width: 2; stroke-linejoin: round; stroke-linecap: round;
                 vector-effect: non-scaling-stroke; }
.chart .fill { opacity: 0.14; }
.chart .oos { fill: var(--oos); }
.chart .tick { fill: var(--text-muted); font-size: 11px;
               font-variant-numeric: tabular-nums; }
.legend { display: flex; flex-wrap: wrap; gap: 16px; margin: 0 0 10px;
          font-size: 0.8rem; color: var(--text-secondary); }
.key { display: inline-flex; align-items: center; gap: 7px; }
.swatch { width: 11px; height: 11px; border-radius: 3px; }
.note { margin: 10px 0 0; font-size: 0.8rem; color: var(--text-muted); }
.empty { color: var(--text-muted); font-size: 0.85rem; margin: 8px 0; }
.stat-row { display: flex; flex-wrap: wrap; gap: 32px; margin: 4px 0 0; }
.stat { display: flex; flex-direction: column; }
.stat-value { font-size: 1.6rem; font-weight: 600; line-height: 1.2; }
.stat-label { font-size: 0.75rem; text-transform: uppercase;
              letter-spacing: 0.04em; color: var(--text-muted); }
.tables { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
          gap: 24px; }
table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
caption { text-align: left; font-size: 0.75rem; text-transform: uppercase;
          letter-spacing: 0.04em; color: var(--text-muted); padding-bottom: 6px; }
th, td { text-align: left; padding: 5px 0; border-bottom: 1px solid var(--grid); }
th { font-weight: 400; color: var(--text-secondary); }
td { text-align: right; font-variant-numeric: tabular-nums; }
code { background: var(--grid); padding: 1px 5px; border-radius: 4px; font-size: 0.85em; }
footer { margin: 32px 0 0; font-size: 0.78rem; color: var(--text-muted); }
"""

#: Hover readout. Kept to the crosshair the dataviz skill asks for on line
#: charts, with no dependency and no network.
_SCRIPT = """
for (const svg of document.querySelectorAll('svg.chart')) {
  const paths = svg.querySelectorAll('path.series');
  if (!paths.length) continue;
  const ns = 'http://www.w3.org/2000/svg';
  const rule = document.createElementNS(ns, 'line');
  rule.setAttribute('stroke', 'var(--axis)');
  rule.setAttribute('stroke-width', '1');
  rule.setAttribute('opacity', '0');
  svg.appendChild(rule);
  const box = svg.viewBox.baseVal;
  svg.addEventListener('pointermove', (event) => {
    const bounds = svg.getBoundingClientRect();
    const x = ((event.clientX - bounds.left) / bounds.width) * box.width;
    rule.setAttribute('x1', x); rule.setAttribute('x2', x);
    rule.setAttribute('y1', 0); rule.setAttribute('y2', box.height);
    rule.setAttribute('opacity', '1');
  });
  svg.addEventListener('pointerleave', () => rule.setAttribute('opacity', '0'));
}
"""

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{style}</style>
</head><body><main>
<h1>{title}</h1>
{header}
{warnings}
{panels}
<footer>Generated by trading_platform. A backtest's job is to disappoint you;
this page is built to let it.</footer>
</main><script>{script}</script></body></html>
"""
