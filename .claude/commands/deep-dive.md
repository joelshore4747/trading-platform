---
description: Structured deep exploration of a concept, from mechanism through implementation to failure modes.
argument-hint: [topic or question]
---

# Deep dive: $ARGUMENTS

Give me a genuine understanding of this, not a summary. Assume I want to be
able to reason about it independently afterwards and to spot when someone else
is wrong about it.

Work through these layers. Skip any that genuinely does not apply, and say why
rather than padding.

## 1. The core mechanism

What is actually happening, at the level of cause and effect? Strip out the
jargon and name the moving parts. If there is a single sentence that carries
most of the understanding, lead with it.

## 2. Why it exists

What problem does this solve, and what was done before? Understanding the
predecessor usually explains the design better than the design documents do.

## 3. The mathematics, if any

Derive rather than assert. Show where each term comes from and what it means
dimensionally. State the assumptions the derivation requires — those
assumptions are where real systems break.

## 4. Concrete worked example

Use realistic numbers from this domain (crypto, options, or this codebase).
Show the arithmetic. An example with made-up round numbers hides exactly the
scale effects that matter.

## 5. How it applies here

Tie it to `trading_platform` specifically: which module, which decision, what
would change. If it does not apply to this codebase, say so — that is worth
knowing.

## 6. Failure modes

Where does this break? What are the edge cases, the regimes where it inverts,
the implementation mistakes people reliably make? Be specific about
consequences.

## 7. What people get wrong

The common misconception, and why it is seductive. This is often the most
useful section.

## 8. Open questions

What is genuinely unsettled or contested? What could you not establish?

## Rules

- **Distinguish fact from inference.** Mark clearly what you know versus what
  you are reasoning to.
- **No false confidence.** If the honest answer is "this depends on details I
  do not have", say that and name the details.
- Prefer one concrete example over three abstract ones.
- If a diagram or table would carry the idea better than prose, use one.
