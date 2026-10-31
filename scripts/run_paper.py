"""Run one paper session: Alpaca bars → LiveFeed → PaperSupervisor → journal.

    .venv/bin/python scripts/run_paper.py --account a1 --market crypto \\
        --symbols BTC/USD ETH/USD --interval 1m --strategy sma \\
        --starting-cash 1000 --daily-loss 0.05 --root ./paper

    .venv/bin/python scripts/run_paper.py ... --dry-run     # everything but the network

**The store is optional and its absence is a stated cost.** With
`TRADING_PLATFORM_DSN` set, every mark and every risk refusal is written to
Postgres and the account's leverage acknowledgements gate its orders. Without
it, the journal remains the only record and nothing gates leverage — so a
session holding a leveraged instrument refuses to start rather than running
ungated. `--no-store` says "I know, and this account is spot only".

This is the VPS entrypoint. It is **UNVERIFIED in the sandbox that wrote it**
(every market-data host answers 403 there); the first real run follows
docs/RUNBOOK.md. Credentials come from APCA_API_KEY_ID and APCA_API_SECRET_KEY
in the environment and nowhere else. Nothing here can place an order at the
venue: fills are simulated locally by the same engine a backtest uses.

Stop it by touching <root>/accounts/<account>/KILL (the strategy stops being
called; the process exits at the next poll) or with SIGINT.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_platform.core.types import Stage
from trading_platform.data.intervals import interval_to_timedelta
from trading_platform.live.alpaca import (
    AlpacaMarketData,
    AlpacaPoller,
    urllib_transport,
)
from trading_platform.live.recorder import JournalRecorder
from trading_platform.live.setup import (
    COST_PRESETS,
    STRATEGIES,
    PaperSetup,
    build_instruments,
    build_strategy,
    cost_model_for,
    save_setup,
)
from trading_platform.live.stream import LiveFeed
from trading_platform.live.supervisor import PaperSupervisor, SupervisorConfig
from trading_platform.risk.leverage import LeverageGate, needs_acknowledgement
from trading_platform.risk.limits import RiskLimits
from trading_platform.store.accounts import StoreTracker, leverage_gate
from trading_platform.store.connection import connect, is_configured
from trading_platform.store.migrate import migrate
from trading_platform.strategy.base import Strategy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--root", type=Path, required=True, help="paper data root (journals, cycles)"
    )
    parser.add_argument("--account", required=True)
    parser.add_argument("--market", choices=("crypto", "stocks"), required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--strategy", default="sma", choices=sorted(STRATEGIES))
    parser.add_argument(
        "--param", action="append", default=[], metavar="KEY=VALUE", help="strategy kwarg"
    )
    parser.add_argument("--cost-preset", default=None, choices=sorted(COST_PRESETS))
    parser.add_argument(
        "--starting-cash", type=Decimal, required=True, help="USD; no default on purpose"
    )
    parser.add_argument(
        "--daily-loss", type=Decimal, default=None, help="fraction of the day's open, e.g. 0.05"
    )
    parser.add_argument("--stocks-feed", default="iex", choices=("iex", "sip"))
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    parser.add_argument("--lookback-hours", type=float, default=2.0)
    parser.add_argument(
        "--dry-run", action="store_true", help="build everything, touch no network, run nothing"
    )
    parser.add_argument(
        "--stage",
        default="paper",
        choices=("research", "simulation", "paper"),
        help="which of the four stages this session is. LIVE is refused: every broker "
        "adapter raises, so no session here can place a live order.",
    )
    parser.add_argument(
        "--no-store",
        action="store_true",
        help="run without Postgres. The journal stays the durable record, but nothing "
        "is queryable and NOTHING GATES LEVERAGE — so this is refused for an account "
        "holding a leveraged instrument.",
    )
    args = parser.parse_args(argv)

    root: Path = args.root
    params: dict[str, str] = {}
    for item in args.param:
        if "=" not in item:
            raise SystemExit(f"--param needs KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        params[k.strip()] = v.strip()
    setup = PaperSetup(
        account=args.account,
        market=args.market,
        symbols=tuple(args.symbols),
        interval=args.interval,
        strategy=args.strategy,
        starting_cash=str(args.starting_cash),
        cost_preset=args.cost_preset
        or ("binance-spot-bnb" if args.market == "crypto" else "default"),
        daily_loss_pct=None if args.daily_loss is None else str(args.daily_loss),
        strategy_params=params,
    )
    save_setup(root / "accounts" / args.account, setup)  # refuses a changed setup
    instruments = build_instruments(args.market, args.symbols)
    strategy = build_strategy(setup)
    cost_model = cost_model_for(setup)
    stage = Stage(args.stage)
    leveraged = sorted(s for s, i in instruments.items() if needs_acknowledgement(i))

    with ExitStack() as stack:
        tracker, gate = _store_wiring(stack, args, stage, leveraged)
        cfg = SupervisorConfig(
            root=root,
            account=args.account,
            starting_cash=args.starting_cash,
            instruments=instruments,
            cost_model=cost_model,
            limits=RiskLimits(),
            kill_switch=root / "accounts" / args.account / "KILL",
            max_daily_loss_pct=args.daily_loss,
            tracker=tracker,
            leverage_gate=gate,
            stage=stage,
        )
        return _run(args, root, cfg, strategy, cost_model, tracker)


def _store_wiring(
    stack: ExitStack, args: argparse.Namespace, stage: Stage, leveraged: list[str]
) -> tuple[StoreTracker | None, LeverageGate | None]:
    """The store, or a stated reason there is none.

    Without a store there is no leverage gate, and an account holding a
    leveraged instrument would then run with nothing checking that anyone had
    acknowledged what 10x does to it. That is refused rather than warned about:
    the gate exists because leverage must not arrive by omission, and "the
    database was not configured" is an omission.
    """
    if args.no_store or not is_configured():
        if leveraged:
            raise SystemExit(
                f"account {args.account!r} holds leveraged instrument(s) "
                f"({', '.join(leveraged)}) and this session has no store, so nothing "
                "would check that anyone acknowledged what that leverage does (ADR "
                "0011). Set TRADING_PLATFORM_DSN and write the acknowledgement, or run "
                "an account that holds none."
            )
        why = "--no-store" if args.no_store else "TRADING_PLATFORM_DSN is unset"
        print(
            f"no store ({why}): the journal is the only record. Nothing is queryable, "
            "the operations page will not see this account, and no leverage is gated."
        )
        return None, None

    connection = stack.enter_context(connect())
    migrate(connection)
    gate = leverage_gate(connection, args.account)
    tracker = StoreTracker(connection=connection, account=args.account, cycle=1, stage=stage)
    acknowledged = sorted(gate.acknowledgements)
    print(
        f"store: connected; {len(acknowledged)} leverage acknowledgement(s)"
        + (f" for {', '.join(acknowledged)}" if acknowledged else "")
    )
    return tracker, gate


def _run(
    args: argparse.Namespace,
    root: Path,
    cfg: SupervisorConfig,
    strategy: Strategy,
    cost_model: object,
    tracker: StoreTracker | None,
) -> int:
    supervisor = PaperSupervisor(cfg, strategy)
    print(
        f"account {args.account!r} at {root}: {args.market} {args.symbols} {args.interval} "
        f"strategy={strategy.name} cost={cost_model!r} stage={cfg.stage} "
        f"kill={cfg.kill_switch}"
    )
    print(supervisor.reconcile().report())

    recorder = JournalRecorder(root / "bars", f"alpaca-{args.market}")
    feed = LiveFeed(
        args.symbols, expected_interval=interval_to_timedelta(args.interval), recorder=recorder
    )
    client = AlpacaMarketData(args.market, urllib_transport(), stocks_feed=args.stocks_feed)
    poller = AlpacaPoller(
        client,
        feed,
        args.symbols,
        args.interval,
        poll_every=timedelta(seconds=args.poll_seconds),
        lookback=timedelta(hours=args.lookback_hours),
    )
    if args.dry_run:
        print(
            "dry run: credentials",
            "complete"
            if client.credentials.is_complete
            else f"MISSING {client.credentials.missing}",
        )
        print(
            "dry run: would poll",
            client.bars_url(args.symbols, args.interval, datetime.now(UTC), None),
        )
        return 0

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    thread = threading.Thread(target=poller.run, args=(stop,), name="alpaca-poller", daemon=True)
    thread.start()
    try:
        outcome = supervisor.run(feed)
    finally:
        stop.set()
        thread.join(timeout=30)
        recorder.close()
    print(
        f"session {outcome.session}: stopped by {outcome.stopped_by}; "
        f"{len(outcome.fills)} fill(s); {outcome.journal_events} journal event(s); "
        f"poller {poller.stats}; feed {feed.stats.report()}"
    )
    if tracker is not None:
        # Said out loud whether it went well or not: a tracker that failed
        # silently is a database that quietly stopped recording the account.
        print(f"store: {tracker.report()}")
    if outcome.cycle_failed is not None:
        print(f"CYCLE {outcome.cycle_failed.number} FAILED at {outcome.cycle_failed.ending_equity}")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
