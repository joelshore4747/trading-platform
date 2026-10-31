# trading_platform

A market-intelligence and quantitative-research platform, built up from a backtesting
engine whose job is to falsify strategies. The long-range direction is the charter in
`harness/`; the current state is `harness/PROJECT_STATE.md`; the honest score is
`harness/FINAL_AUDIT.md`. Read those before assuming anything about this codebase.

## Ground truth before change

Every conclusion you state is one of **VERIFIED** (shown by code, tests, runtime, or a
measurement you can name), **INFERRED** (strongly implied, not demonstrated), **UNKNOWN**,
or **PROPOSED**. Never present UNKNOWN as VERIFIED or PROPOSED as CURRENT. If the evidence
is insufficient, say UNKNOWN. Do not invent measurements, provider capabilities, or results.

If the implementation drifts from `harness/ARCHITECTURE.md`, flag it. Decide whether the
code is wrong or the document is obsolete. Never silently edit the document to match.

## The financial principle

Historical profitability does not prove future profitability. A backtest does not prove
robustness. A paper result does not prove live execution. A correlation is not a cause. The
strongest claim this repository can make about a strategy is **"failed to falsify, on this
data, at this cost model."** A result that looks good is evidence of a bug until proven
otherwise; try to break it before believing it.

Complexity must earn its place. If a simple strategy performs like a complex one, prefer the
simple one. Do not build a feature because the charter lists it; score it and defer it until
the condition in `harness/ROADMAP.md` is met.

## Four stages, kept apart

**RESEARCH → SIMULATION → PAPER → LIVE.** A research strategy never gains execution
authority by accident. A paper account never places a live order. Nothing moves a strategy
toward LIVE silently. Today every broker adapter raises and `is_live` is False everywhere;
LIVE is a permitted stage with explicit boundaries, not a forbidden one, and reaching it
requires all of: credentials held outside the repository, a kill switch, per-order and
per-day loss limits enforced venue-side as well as locally, full order audit logging, a
written 3am procedure, and an explicit human decision recorded as an ADR.

## Engineering invariants

- **Orders submitted on bar T fill on bar T+1, at its open.** Enforced by the event loop's
  ordering in `engine/backtester.py`, guarded by `tests/test_lookahead.py`. If a change
  fails a test there, the change is wrong. Do not edit those tests to match new behaviour
  without saying, in your response, that you are weakening a lookahead guard and why.
- **Money is `Decimal`**, never `float`, in `core`, `engine`, `risk`, `live`. Analytics
  converts at the boundary in `analytics/metrics.py` — the only place.
- **Validation lives in `__post_init__`.** Impossible values fail at construction, loudly.
- **Strategies see the market only through `StrategyContext`.** Extend the context
  explicitly when a strategy needs more; never reach around it.
- **Never weaken the cost model to make a strategy viable.** `ZERO_COST` isolates engine
  bugs and is never evidence. **Never relax a risk limit to let a run complete** — a halt
  is a result.
- **A gap is a gap.** Nothing interpolates missing data, anywhere.
- **Refuse rather than assume**: an unknown FX rate, a censored listing, a date outside a
  calendar's range, a window too short to annualise — all raise. Silent defaults are how a
  plausible wrong number reaches a decision.
- **New public functions get a test.** Especially in `engine/`, `risk/`, `live/`.
- **A test that cannot fail is worse than no test.** Six have been found here, each
  surviving a green suite. Before trusting a test, name the mutation that would leave it
  green — and if unsure, run `scripts/run_mutation_eval.py` or the `guard-auditor`.
- **Reproducibility.** Never overwrite a historical result, dataset, or experiment record.
  A changed strategy is a new version; changed data is a new dataset; changed fees are a
  new experiment.

## What an agent must never do silently

Change fees, starting capital, slippage or spread defaults · change or delete historical
data · delete experiments, accounts, or journal entries · rewrite a research conclusion ·
change strategy parameters · relax a risk limit · touch the event loop's ordering · enable
live trading · remove a safety control. Each of these needs an explicit request and a
recorded decision. Deterministic checks for some live in `.claude/rules/`; the rest are
this paragraph, which is weaker, so raise them rather than act.

## Milestones: stop and plan before building

Enter plan mode before: starting a new priority tier in `harness/ROADMAP.md`; adding a data
source, venue, or instrument class; any change to the event loop, fill logic, or feed
ordering; adding or relaxing a risk limit; believing a backtest that will inform a decision;
and after finishing a chunk, before the next. Ask what would make the approach wrong and
how you would find out, whether future information could reach a decision, and whether a
smaller version would falsify the idea faster.

## Commands

```bash
uv pip install -e ".[dev]"                               # install
.venv/bin/python -m pytest -q                            # all tests
.venv/bin/python -m pytest tests/test_lookahead.py -v    # the ones that matter
.venv/bin/ruff check . && .venv/bin/ruff format .
.venv/bin/mypy                                           # strict, must stay clean
.venv/bin/python scripts/run_mutation_eval.py            # do the tests actually test?
.venv/bin/python scripts/run_agent_eval.py --dry-run     # do the auditors? (costs money)
python examples/run_walk_forward_btc.py                  # real data, honest result
```

## Layout

```
src/trading_platform/
  core/        types, instruments (margin), events, fx, credentials   — imports nothing
  data/        feeds, cache, vendor, universe, panel, synthetics
  engine/      backtester (the event loop), execution, portfolio (ledger + margin)
  strategy/    Strategy, the narrow StrategyContext, examples
  risk/        pre-trade limits, drawdown halt, margin utilisation
  analytics/   metrics, walk-forward, null harness, multiplicity, tearsheet
  live/        stream bridge, recorder, decision journal, calendars, replay
  brokers/     Broker protocol; every adapter raises
evals/         the corpus of this repo's own defects; mutation + agent eval
harness/       the audit: maps, reviews, protocols, FINAL_AUDIT
docs/          ROADMAP (canonical lab notebook), adr/ (canonical decisions)
```

Dependencies point downward only. `core` imports nothing.

## Working here

- `/session-brief` first. Believe no result until `lookahead-auditor`, `risk-auditor`,
  `/cost-check` and `/walk-forward` have run; the `gauntlet` workflow runs them together
  and attacks the synthesis. Numbers, not adjectives.
- Every strategy change during a review loop is a trial; report Sharpe **deflated** for the
  real trial count (`analytics/multiplicity.py`). Thirty tweaks in thirty days is thirty
  hypotheses.
- Retracted results stay in the record. Hiding a retraction is the dishonesty this
  repository is built against.
- **Read `library/MANIFEST.md` before researching a subject from scratch**, and write
  what you learn back into `library/subjects/<name>/NOTES.md` with a status and a link.
  Rejections belong there most of all: a note saying "tried, here is the record, it
  failed" saves the next session the whole cost. The `library` skill has the shape.
- **File what you find.** `scripts/ticket.py file --kind defect --severity P1 --title ...
  --path ... --repro ... --source ...`. A finding that lives only in a transcript dies
  with it, and `harness/TECHNICAL_DEBT.md` is now generated from
  `harness/tickets/events.jsonl` rather than maintained by hand. A defect is **refused
  without a reproduction**; filing something already filed records a second independent
  source, which is corroboration rather than a duplicate. `scripts/ticket.py list` to see
  what is open, `close` with a resolution and a commit, `debt` to regenerate the table.
- The agents and hooks are measured, not assumed (`/harness`, `evals/README.md`). Adding
  an agent means adding it to the corpus routing table, or the guard fails — which is the
  point.
- Use the specialist agent for the job and the model the task deserves
  (`harness/AGENT_PROTOCOL.md`). Opus for financial correctness and audits; Sonnet for
  checklists; Haiku for validation; Fable for adversarial, market and creative research.
