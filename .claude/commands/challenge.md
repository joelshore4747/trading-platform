---
description: Adversarially attack a result, claim, or design decision. Try hard to prove it wrong.
argument-hint: [claim, result, or file to attack]
---

# Challenge: $ARGUMENTS

Your job is to **break** this, not to evaluate it fairly. Argue the opposing
case as strongly as the evidence permits. If it survives you, it has earned
something; if it does not, better now than with money on it.

Do not soften findings to be agreeable, and do not manufacture objections to
seem rigorous. Every attack below must be grounded in something you actually
checked.

## Attack in this order

**1. The claim itself.** State precisely what is being asserted, in falsifiable
terms. Vague claims cannot be attacked and usually should not be trusted —
if you cannot make it precise, that is the finding.

**2. The evidence.** What would have to be true for this claim to hold? Check
each. Where you cannot check something, say so explicitly rather than assuming
it holds.

**3. The alternative explanation.** What else would produce exactly this
observation? Be specific. "It could be noise" is weak; "with 14 trades, a
Sharpe of 1.2 is within the range you would expect from a coin flip" is strong.

**4. The hidden assumption.** What is being taken for granted? Common ones
here: that the cost model reflects reality, that historical liquidity would
have absorbed the size, that the data is point-in-time, that the regime
persists.

**5. The strongest counterargument to your own attack.** State the best case
*for* the claim. Then say whether it changes your conclusion.

## Run the numbers

Where the claim is quantitative, check it computationally rather than arguing
about it. Use `Bash` and `.venv/bin/python`. Report actual output, not
expectations.

## Deliverable

- **Verdict**: does the claim survive? `SURVIVES` / `SURVIVES WITH CAVEATS` /
  `DOES NOT SURVIVE`.
- **The strongest objection**, in one paragraph.
- **What would settle it** — the specific test or data that would resolve the
  remaining doubt.

If the claim survives a genuine attack, say so plainly. A challenge that always
finds fatal flaws is as useless as one that never does.
