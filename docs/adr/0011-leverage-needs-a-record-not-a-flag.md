# ADR 0011 — Leverage needs a record, not a flag

**Status:** Accepted
**Date:** 2026-09-04
**Supersedes:** nothing. Extends ADR 0010 (the queryable store).

## Context

Leverage in this codebase is one field: `Instrument.initial_margin_rate`, where
`None` means fully collateralised and `Decimal("0.1")` means 10x. That is a
number a reviewer can see in a diff, which is good, and it is the whole of the
control, which is not.

Nothing made anyone state, before the fact, what 10x does to an account when the
market moves 20% the wrong way. The answer for a fully deployed account at 10x
is that it loses twice its equity and owes the difference. That is not a number
somebody works out during a drawdown.

The charter's §8 stages keep RESEARCH, SIMULATION, PAPER and LIVE apart, and §69
lists relaxing a risk limit among the things an agent must never do silently.
Neither says anything about leverage being *taken up* in the first place, and
the gap showed: a paper session could register a 10x perpetual by editing one
keyword argument, and every guard in `risk/limits.py` would then bound the size
of the position without anyone having acknowledged what its leverage meant.

## Decision

**An account may not hold an instrument above 1x without a stored
acknowledgement for that account and that symbol.**

1. `risk/leverage.py` holds `LeverageAcknowledgement`, which states the maximum
   leverage accepted, the liquidation price at it, and the cash lost on a 20%
   adverse move. **The stated figures are recomputed at construction and a
   disagreement raises.** A form that can be filled in wrongly is a form that
   gets filled in wrongly, and an acknowledgement nobody had to compute is a
   checkbox.
2. `LeverageGate` refuses an instrument with no acknowledgement, and one whose
   leverage exceeds what was acknowledged — the case that bites in practice,
   where the margin rate is edited later and an acknowledgement written in good
   faith no longer covers it.
3. `RiskManager.check` consults the gate on the order path, beside `halted` and
   `insolvent` rather than among the numeric caps, because it is the same kind
   of rule: whether the account may take a position at all, not how large.
4. **Reducing orders are exempt.** An account that somehow holds an
   unacknowledged leveraged position must still be able to close it. A gate that
   trapped a position would be a risk control that creates risk.
5. `SupervisorConfig` refuses to construct a session that registers a leveraged
   instrument with no gate. Leverage must not arrive because a keyword was
   omitted.
6. The rows live in `leverage_acknowledgements` (migration 0004). They are
   **revoked, never deleted**: "who could take 10x on this account in August"
   has to stay answerable in December.

### Where the gate binds, and where it does not

`RiskManager(leverage_gate=None)` — no gate — is the default, and is correct for
a RESEARCH backtest. A backtest holds no capital; gating it would stop nothing
dangerous and would teach people to switch the gate off, which is how a control
dies. PAPER and above always build one from the account's rows, and the
supervisor refuses to start without it. The four stages are kept apart here as
everywhere: research does not gain execution authority, and it does not need
permission it cannot use.

## What the arithmetic assumes, stated because it is optimistic

`liquidation_move(L) = 1/L` is the adverse move that wipes the **initial** margin
posted. A real venue liquidates at its **maintenance** margin, which is lower
than initial, so the position is closed while some margin remains — at roughly
`initial - maintenance`, not `initial`. This platform does not model maintenance
margin: no venue is connected and no schedule has been read. **The stated
liquidation price is therefore an upper bound on the room available, and the real
figure is worse.** That sentence is in `LeverageAcknowledgement.text()` and a
test asserts it is there, because a leverage disclosure that is quietly
optimistic is worse than none at all.

Funding is excluded too. A perpetual at 10x pays or receives funding on the full
notional, which moves the liquidation price every interval. It is modelled
elsewhere and is not folded into these point-in-time figures.

## Consequences

- Fifteen existing supervisor tests began failing on this change, because those
  sessions registered a 10x perpetual and had acknowledged nothing. That is the
  control working. They now carry an explicit acknowledgement written out in
  full, so the numbers being accepted are visible in the test file.
- Anyone adding a leveraged instrument to a paper account has to write a row and
  get three figures right. That is the cost, and it is the point.
- Nothing here enables leverage or moves anything toward LIVE. Every broker
  adapter still raises, `is_live` is False everywhere, and `SupervisorConfig`
  refuses `stage=LIVE` outright.

## Alternatives rejected

**A configuration flag** (`allow_leverage=True`). Rejected: a flag records that
somebody typed something, not that they understood it, and it would end up
defaulted true in a helper.

**Enforcing at instrument construction.** Rejected: the instrument does not know
which account holds it, and the same 10x perpetual is legitimately used in a
research backtest with no account at all.

**Gating research too.** Rejected on the reasoning above: a control that fires
where it cannot help is a control people learn to disable.
