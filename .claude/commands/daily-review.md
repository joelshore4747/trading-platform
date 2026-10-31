---
description: Review yesterday's paper session — divergence first, then refusals, then the account — and decide keep / change / retire, with every change recorded as a new trial.
argument-hint: [account] [YYYY-MM-DD]
---

# Daily review: $ARGUMENTS

The loop this platform exists for. The paper trader ran; you read what it did
and decide. Numbers, not adjectives; every change is a trial.

## 1. Produce the report

```bash
.venv/bin/python scripts/daily_review.py --root ./paper --account <account> [--day <date>]
```

Read the file it wrote under `harness/reviews/<account>/`. Never edit it.

## 2. Read it in this order, and stop at the first failure

1. **Divergence.** If the two paths DIVERGE, nothing else in the report means
   anything. Find the bug (engine or supervisor — it is one of them), fix it
   with a test, and re-run the review. If the check was NOT CHECKED, say why
   in your summary; do not treat "not checked" as "agreed".
2. **Refusals.** If more than a small share of intent was refused, the day
   measured the risk layer, not the strategy. Read `rejections_by_reason`.
   Never relax a limit to make the strategy fit; a refusal is a result.
3. **Halts.** A drawdown halt, daily-loss halt, or failed cycle gets the
   `strategy-autopsy` skill before anything else is decided.
4. **The account.** Equity change and fills. One day is one observation.

## 3. Decide, and record the decision

- **Keep.** Say so, with the reason. Append nothing to the strategy.
- **Change.** A parameter or logic change is a **new trial**: create the record
  first (`scripts/experiment.py new ...` — hypothesis and what would disprove
  it), then start a **new account** for it (`save_setup` refuses to change a
  running account's setup). The old account keeps running or is closed with a
  reason (`AccountCycles.close`). Never edit parameters in place.
- **Retire.** Close the cycle with the reason; set the experiment record's
  status; write the autopsy into `harness/KNOWN_FAILURES.md` if it taught
  something reusable.
- **An idea** that is not a change goes to `harness/CREATIVE_RESEARCH.md` in
  the lab's template, via the `creative-researcher` agent if it needs research.

## 4. Deflate

The report prints the trial count. When a Sharpe is eventually computed for
this strategy (after a month, not a day), it is deflated for that count
(`analytics/multiplicity.py`). Thirty tweaks in thirty days is thirty trials.

## 5. Log it

```bash
.venv/bin/python scripts/agent_log.py --agent final-reviewer --model <model> \
  --request "daily review <account> <day>" --decision "<keep|change|retire>: <why>" \
  --confidence high|medium|low
```
