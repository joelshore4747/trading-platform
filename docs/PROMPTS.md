# Asking well

Every rule in `scripts/prompt_guard.py` has a section here. When the guard fires
it names a rule id and points at this file, so the note stays one line and the
reasoning lives somewhere you can read at leisure.

**None of this is a refusal.** The guard cannot block a prompt — it only adds a
note, and your prompt is answered as written. The point is that the honest
answer to some questions is different from the answer the question invites, and
that difference is worth surfacing before the work rather than after.

A bad prompt here is not a badly written one. It is a badly **aimed** one:
*"make this strategy profitable"* is perfectly clear, and the only way to comply
literally is to search until a good number appears. That is the failure mode
this whole repository is built against.

The table below is the fastest way to use this document.

| Instead of | Ask |
|---|---|
| Make this strategy profitable | What would falsify this idea, and what is the cheapest test of it? |
| Find the best parameters | Sweep this grid and show me the surface — is the peak a plateau or a spike? |
| Is this any good? | Run `/walk-forward` and `/cost-check` and give me the numbers |
| Run it without fees for now | At what cost level does the edge die? |
| Loosen the drawdown limit | Why did the halt fire, and what does that say about the strategy? |
| Make the tests pass | This test fails — is the test wrong or is the code wrong? Show me why |
| Precompute the indicators | Is there a way to speed this up that cannot see the future? |
| Improve this | Improve this *specific thing*, measured *this specific way* |

---

## Cautions

### `make-it-profitable`

**Fires on:** "make this profitable", "improve the returns", "beat the market",
"why isn't this making money".

The request has only one literal compliance path: keep changing things until the
number improves. Whatever comes out the far end is a search result, not a
finding, and it will not survive contact with data it was not chosen on.

The prime directive here is that **a backtest's job is to disappoint you**. If a
strategy does not work, saying so plainly is the deliverable — a recorded
negative result is the most reusable artefact this project produces, and bad
strategies are cheap enough to delete rather than repair.

> **Ask instead:** "What would have to be true for this to work, and what is the
> fastest way to find out it isn't?"

### `find-better-params`

**Fires on:** "find the best parameters", "optimise the strategy", "tune it".

A grid always finds something. With sixteen cells and a noisy series, the best
cell is roughly the luckiest cell, and reporting it as an edge converts noise
into a strategy.

What the sweep should return is the **surface**, not the peak:
`profitable_fraction` (one good cell in forty is noise), `neighbour_ratio` (a
plateau is real, a spike is a coincidence), and fills per fold (a Sharpe over
two trades is arithmetic, not measurement).

> **Ask instead:** "Sweep this grid on the training window and show me the
> surface — how sharp is the peak, and does it hold up out of sample?"

### `weaken-costs`

**Fires on:** `ZERO_COST` alongside a claim about profitability; "ignore the
fees"; "assume no slippage".

`ZERO_COST` exists for exactly one purpose: isolating a bug in the engine. It is
never evidence. **A strategy profitable at `ZERO_COST` and unprofitable at
`CostModel()` is unprofitable** — the costs are not an obstacle between you and
the result, they are part of the result.

> **Ask instead:** "Run `/cost-check` — at what cost level does this stop
> working, and is that level realistic for the venue?"

### `weaken-risk`

**Fires on:** "loosen the drawdown limit", "increase max_drawdown", "disable the
halt".

**A run that halts on drawdown is a result, not a failure.** The halt fired
because the strategy lost more than the limit allowed; raising the limit does
not change that, it just hides it until later.

Changing a risk limit is a milestone in its own right — plan before writing, and
put the diff in front of `risk-auditor`. One property worth knowing: the halt
permits *risk-reducing* orders, so a halted account can still flatten. An
earlier version blocked every order and trapped the position, which inflated one
example run's ending equity from 72,969 to 111,610 — profit that existed only
because the position could not be exited.

> **Ask instead:** "Why did the halt fire here, and what does the drawdown that
> triggered it say about the strategy?"

### `weaken-test`

**Fires on:** "make the tests pass", "adjust the test so…", "skip the lookahead
tests", "xfail it".

A failing test is information. The question is always *which* of the two is
wrong — and in this repository the answer has genuinely gone both ways, so it is
worth actually checking rather than assuming.

`tests/test_lookahead.py` is the exception with no ambiguity: **if a change makes
a test in that file fail, the change is wrong.** Weakening any guard has to be
stated out loud, in the response, with the reason. A `Stop` hook refuses to end
a session while those tests are red.

> **Ask instead:** "This test is failing — is the test encoding the wrong
> expectation, or did the code regress? Show me which and why."

### `lookahead-vector`

**Fires on:** "precompute the indicators", "use the adjusted close", "symbols
that still exist", "vectorise the whole series".

Each of these is a named route by which future information reaches a decision:

- **Precomputed indicators** are computed over the full series, then sliced per
  bar — so bar 40's value knew about bar 900.
- **Adjusted prices** derive their adjustment factor from future events. This is
  where survivorship bias enters equities, below the level any strategy review
  would catch.
- **A universe of symbols that exist today** is a universe chosen by knowing who
  survived.

None of these announces itself. There is no error — only an equity curve that is
quietly fictional.

> **Ask instead:** "Is there a way to make this faster that cannot see past the
> current bar?" Then run `lookahead-auditor` on the result.

### `unearned-belief`

**Fires on:** "is this any good?", "should I trade this?", "does this work?",
"ready for paper trading?" — but only when a strategy noun is nearby, so
*"is the parquet cache working?"* is left alone.

A fair answer needs evidence that usually does not exist yet. The checklist is
`.claude/skills/backtest-review/`, and the short version is: `/walk-forward`
(efficiency, grid profitability, fills per fold, buy-and-hold), then
`lookahead-auditor`, `strategy-reviewer`, `overfitting-auditor`, `/cost-check`.

Note that efficiency alone is not sufficient. On pure noise this project
measured an efficiency of **1.24** — above 1.0, for a strategy with nothing to
find — because out-of-sample scores scatter around zero and their mean can beat
a barely-positive in-sample mean. A run has to survive every check, not the
headline one.

> **Ask instead:** "Run the backtest-review checklist on this and give me the
> numbers." Then expect the honest answer to be no.

### `live-trading`

**Fires on:** "connect it to Binance", "place an order", "go live", "add my API
key".

**This repository has never traded real money and currently cannot.** There is
no live broker adapter, no order-routing path to a venue. That is a deliberate
design stage, not an oversight — `docs/PLAN.md` Phase 6 explains why, and the
roadmap deliberately has no step 51.

Every connector and broker is a skeleton that raises on use. Credentials are
read from the environment only, never from a file, and a `Credential` redacts
itself in `repr` so a secret cannot reach a log by accident. Keys used with this
project should be **read-only market data scopes**, never withdrawal-enabled.

> **Ask instead:** "What would have to be built and proved before a live path
> would be responsible?" The answer is in `docs/PLAN.md` Phase 6.

### `vague-scope`

**Fires on:** a prompt that is *only* "make it better" / "improve this" / "clean
this up". Deliberately anchored to the whole prompt, so *"improve this by adding
a rolling Sharpe panel"* does not fire — that one has a target.

Without a target, "improve" resolves to whatever is easiest to demonstrate,
which is rarely what matters.

> **Ask instead:** name the file, the metric, or the behaviour that should
> change, and how you would know it worked.

---

## Reminders

These are not bad prompts. They are the `CLAUDE.md` milestone rule made
mechanical instead of remembered — a note that the work you just asked for
touches something where the expensive mistake is building the right thing on a
wrong premise.

Plan mode is cheap. A backtest built on a bad premise is not.

### `milestone-event-loop`

The ordering of the loop in `engine/backtester.py` is the single most important
property in the codebase: orders submitted while processing bar T fill against
bar T+1, at that bar's open. It is enforced by the order of the steps, not by a
flag — there is no configuration that turns it off. The same applies to fill
logic in `engine/execution.py` and the ordering guarantees in `data/feed.py`.

### `milestone-risk`

Adding or relaxing a limit changes what the engine is allowed to do with money
it is pretending to have. See `weaken-risk` above for why relaxing one to make a
run complete is not an option.

### `milestone-instrument`

Options are a subclass, not a rewrite — the `Instrument` hierarchy was built
instrument-agnostic from the start so that this stays true. Two traps worth
holding in mind before starting: quoted implied volatility is frequently
unmarketable in size, and a delta-neutral book can still be dangerously short
vega.

### `milestone-data-source`

The `adding-a-data-source` skill has the full checklist. The recurring shape of
these bugs is that they sort correctly and look plausible: Binance's `closeTime`
is `open + interval − 1ms`; its spot archives switch from milliseconds to
microseconds on 2025-01-01, which lands unnormalised 2025 bars roughly fifty
thousand years in the future *while still sorting monotonically*; and the final
bar of an unbounded response is still forming.

---

## Prompts worth keeping

Not rules — just the phrasings that have actually produced good work here.

**Starting a session**
> `/session-brief` — bounded token cost, then it stops.

**Before believing anything**
> "Run `/walk-forward` on this with a coarse grid and a benchmark, then hand the
> report to `overfitting-auditor`. Report its verdict verbatim, including if it
> contradicts the numbers looking good."

**When a result looks good**
> `/challenge` or `/pre-mortem` — assume it has already failed with real money
> and work backwards to why.

**Understanding something properly**
> `/deep-dive <topic>` — mechanism, then implementation, then failure modes.

**When you disagree with a design decision**
> "Argue the other side of this as strongly as you can, then tell me which you
> actually believe and why." The repository's decisions are recorded in
> `docs/adr/` precisely so they can be re-opened with the original reasoning in
> view.
