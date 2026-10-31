# ADR 0007 — The charter governs, and what that does and does not change

**Status:** Accepted
**Date:** 2026-09-01

## Problem

A charter arrived ("Trading Intelligence Platform — Master Foundation, Audit, Research &
Claude Code Harness v1.0") describing a platform far wider than this repository: a web
terminal, ML, knowledge graphs, 100+ equities, news and government-data ingestion, monitors,
paper-account cycles. It conflicted with `CLAUDE.md` on scope ("no step 51", prefer deleting a
strategy to fixing it), on base currency (§28: £1,000 accounts; the ledger is USD/USDT with
ADR 0002 refusing to assume a rate), and on where decisions and the roadmap live (§4 wants
`/harness/ROADMAP.md`; `docs/ROADMAP.md` is a 1,000-line lab notebook that records
retractions). Its §124 asked for the desired architecture itself to be evaluated.

## Evidence

- Ground truth contradicted the charter's premise: 63 of 64 commits Claude-co-authored under
  a human owner, none by Codex; the harness it asks for is the repository's most rigorous part
  and is itself measured (`evals/`, ADR 0006).
- No strategy has survived out-of-sample on real data. SMACrossover's honest result on the
  BTCUSDT archive is a **negative** in-sample Sharpe (−2.52, roadmap 35).
- The charter's own §81 (complexity penalty) and §105 ("do not automatically build every
  feature") argue against building its upper layers before that condition is met.
- ADR 0002 measured currency mixing inside a ledger as the dangerous case (vol 20%→55%
  through a depeg while total return was unchanged).
- Charter §112 forbids two sources of truth; duplicating the roadmap and ADRs would create them.

## Options

**Governance.** (A) `CLAUDE.md` governs, charter is a map. (B) Charter governs, `CLAUDE.md`
rewritten. (C) Decide after the audit.
**Currency.** (A) USD ledger, GBP display via `core/fx.py`. (B) GBP ledger with FX on entry.
(C) Multi-currency portfolio.
**Location.** (A) `/harness/` for new audit files, pointers for roadmap and decisions.
(B) Move `docs/` into `/harness/`. (C) Everything in `docs/`.

## Decision

**B / A / A.** The charter governs and `CLAUDE.md` was rewritten (131 lines). The ledger stays
USD with GBP as a labelled display conversion. New audit documents live in `/harness/`;
`docs/ROADMAP.md` and `docs/adr/` remain canonical, with pointer files beside the audit.

The rewrite **preserved**, as charter-compatible principles rather than as a competing
philosophy: the one-bar fill delay and its guards; `Decimal` money; validation at
construction; the narrow `StrategyContext`; refuse-rather-than-assume; "a test that cannot
fail is worse than no test"; and falsification-first, restated through the charter's own §81,
§103 and §121 — the strongest claim about a strategy remains "failed to falsify".

The rewrite **replaced** "no step 51 / no live path" with §8's four stages. LIVE became a
permitted stage with explicit boundaries (credentials outside the repo, kill switch,
venue-side and local loss limits, full audit logging, a 3am procedure, an ADR) rather than a
forbidden one. Today every adapter still raises; nothing in the approved work reaches LIVE.

The rewrite **added** §2 classification (VERIFIED / INFERRED / UNKNOWN / PROPOSED) as a
standing rule, §112 no-silent-drift, and `.claude/rules/safety.md` carrying the §69 list —
with the fee / capital / slippage / risk-default prohibitions made mechanical by a PostToolUse
hook (`check-defaults.sh`) that cautions and never blocks.

## Reason

The owner's call, made with the loss stated: the falsification culture is what found this
session's retraction, two fake guards and the flip-through-zero. It is kept because the
charter independently demands it; it no longer sets the scope.

## Consequences

- The build order is inverted relative to the charter's top-down reading. `harness/ROADMAP.md`
  defers P6 (frontend), P8 (ML), P9 (information), P13 (advanced) with the condition that
  lifts each — chiefly, one strategy surviving out-of-sample and 30 days of paper divergence.
- `CLAUDE.md` is paid for on every turn; the rewrite invalidated the prompt cache once.
- Widening scope before a validated strategy is now a permitted risk. The mitigation is the
  ladder and its conditions, not a rule.
- The §28 £1,000 account becomes a USD ledger with a GBP figure that is always labelled as
  a conversion, through `core/fx.py`, which will refuse without a GBPUSD series.

## Revisit when

Scope widening has preceded a validated strategy for more than two quarters; a GBP venue or
UK tax reporting makes GBP the natural ledger; or a frontend needs the roadmap in a shape
`docs/ROADMAP.md` cannot provide.
