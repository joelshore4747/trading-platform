"""Prompt guard: catch prompts whose honest answer differs from the one invited.

Not a spell-checker. A bad prompt here is one that is perfectly clear and badly
*aimed* — "make this strategy profitable" is unambiguous, and the only way to
comply is to search until a number appears, which is the failure mode
``CLAUDE.md`` exists to prevent. Until now the only defence was that Claude
remembers to push back, and memory is not a control.

Two rule kinds:

- **Cautions** fire on a prompt that invites a dishonest answer. They do not
  refuse it; they state what the honest version requires.
- **Reminders** fire on a prompt that touches a milestone surface — the event
  loop, a risk limit, an instrument class, a data source. These are not bad
  prompts at all. This is the ``CLAUDE.md`` plan-mode rule made mechanical
  instead of remembered.

**This hook never blocks.** Exit 2 on ``UserPromptSubmit`` erases what the human
typed. A guard that deletes your sentence is switched off within a day and then
guards nothing, so the only lever used here is added context. Everything fails
open: a crashed guard must never cost a turn.

Run ``python scripts/prompt_guard.py --self-test`` to check the rules against
``tests/fixtures/prompt_corpus.jsonl`` without pytest.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

PROJECT_DIR = Path(__file__).resolve().parent.parent
CORPUS = PROJECT_DIR / "tests" / "fixtures" / "prompt_corpus.jsonl"

RuleKind = Literal["caution", "reminder"]

#: Sources whose prompts are not human text worth judging. `/walk-forward` is
#: not a badly aimed question; running the rules over it is pure noise.
SKIP_SOURCES = frozenset({"slash_command"})


@dataclass(frozen=True, slots=True)
class Rule:
    """One detector.

    ``requires`` is a second pattern that must ALSO match somewhere in the
    prompt, for rules where a phrase alone is not enough. ``unless`` vetoes the
    match. Both exist for precision: a guard that cries wolf gets disabled, so
    a false positive costs more than a miss.
    """

    id: str
    kind: RuleKind
    headline: str
    guidance: str
    pattern: re.Pattern[str]
    requires: re.Pattern[str] | None = None
    unless: re.Pattern[str] | None = None

    def matches(self, prompt: str) -> bool:
        if not self.pattern.search(prompt):
            return False
        if self.requires is not None and not self.requires.search(prompt):
            return False
        return not (self.unless is not None and self.unless.search(prompt))


def _p(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


#: Phrasings that ask *about* a thing rather than asking for it to be done.
#: "What does the adjusted-close trap mean?" is a good question about a trap,
#: not a request to walk into one.
_ASKING_ABOUT = _p(
    r"\b(what (is|are|does|do|'s)|why (is|are|does|do|did|isn'?t|doesn'?t there)|"
    r"explain|describe|tell me about|meaning of|documented?|section of|how does .* work)\b"
)

#: A noun that makes "is this good?" a question about a strategy result rather
#: than about infrastructure. Without it, "is the parquet cache working?" would
#: be scolded for asking an unearned question, which is how guards get deleted.
_STRATEGY_NOUN = _p(
    r"\b(strateg\w*|backtest\w*|sharpe|edge|alpha|signal|equity curve|crossover|"
    r"sma|momentum|mean.reversion|p&l|pnl|returns?|trade[sd]?|drawdown|"
    r"profitab\w*|paper trad\w*)\b"
)

RULES: tuple[Rule, ...] = (
    # -- Cautions -----------------------------------------------------------
    Rule(
        id="make-it-profitable",
        kind="caution",
        headline="This asks for a conclusion, not a test",
        guidance=(
            "The only way to comply literally is to search until a good number appears. "
            "Say what result would falsify the idea, run that, and report the figure "
            "whatever it is. If the honest answer is 'it does not work', that is the "
            "deliverable — prefer deleting a bad strategy to fixing it."
        ),
        pattern=_p(
            r"\b(make|get|turn|help)\s+(it|this|that|the|these|those)(\s+\w+)?\s+(more\s+)?"
            r"(profitable|make money|work better)\b"
            r"|\bimprove\s+(the\s+|its\s+)?(returns?|profit\w*|pnl|p&l|performance|equity)\b"
            r"|\bbeat the market\b"
            r"|\bwhy (is|isn'?t|does|doesn'?t)\b[^.?!]{0,40}\b"
            r"(profitable|losing money|make money|making money)\b"
        ),
    ),
    Rule(
        id="find-better-params",
        kind="caution",
        headline="That is a grid search, and a grid always finds something",
        guidance=(
            "Report the whole sweep surface, never the peak: `profitable_fraction`, "
            "`neighbour_ratio`, and fills per fold decide whether a peak is an effect "
            "or a coincidence. Fit on a TrainWindow and score once on a TestWindow — "
            "run `/walk-forward`, and treat efficiency below 0.5 as fitted."
        ),
        pattern=_p(
            r"\b(find|get|search for|look for|pick)\s+(me\s+)?(the\s+|some\s+)?"
            r"(best|better|optimal|winning|good)\s+"
            r"(param\w*|setting\w*|value\w*|window\w*|combination\w*|number\w*)\b"
            r"|\boptimi[sz]e\s+(the\s+)?(strateg\w*|param\w*|window\w*|setting\w*)\b"
            r"|\btune\s+(the\s+|its\s+)?(strateg\w*|param\w*|window\w*|it)\b"
        ),
    ),
    Rule(
        id="weaken-costs",
        kind="caution",
        headline="Do not weaken the cost model",
        guidance=(
            "`ZERO_COST` exists only to isolate an engine bug and is never evidence. "
            "A strategy profitable at `ZERO_COST` and unprofitable at `CostModel()` is "
            "unprofitable. If costs are the question, run `/cost-check` and report the "
            "level at which the edge dies."
        ),
        pattern=_p(
            r"\b(ignore|remove|drop|skip|zero\s*out|turn off|get rid of|without)\s+"
            r"(the\s+)?(fees?|commissions?|costs?|slippage|spread)\b"
            r"|\b(assume|with)\s+no\s+(fees?|commissions?|slippage|costs?)\b"
            r"|\bZERO_COST\b"
        ),
        # Bare ZERO_COST is a legitimate mention; it is only a caution when the
        # prompt treats a frictionless number as a finding.
        requires=_p(
            r"\b(profitab\w*|viable|evidence|result|makes money|worth|"
            r"good|better|works?|fees?|commissions?|costs?|slippage|spread)\b"
        ),
        unless=_ASKING_ABOUT,
    ),
    Rule(
        id="weaken-risk",
        kind="caution",
        headline="Do not relax a risk limit to let a run finish",
        guidance=(
            "A run that halts on drawdown is a result, not a failure. If the halt is "
            "firing too early, the finding is that the strategy's drawdown exceeds the "
            "limit — report that. Changing a limit is itself a milestone: plan first, "
            "and `risk-auditor` should see the diff."
        ),
        pattern=_p(
            r"\b(loosen|relax|raise|increase|widen|bump|disable|turn off|remove|bypass|"
            r"ignore|lift)\s+(the\s+)?[\w\s]{0,24}?"
            r"(drawdown|risk limit|max_drawdown|position limit|leverage limit|halt)\b"
        ),
    ),
    Rule(
        id="weaken-test",
        kind="caution",
        headline="Fix the code, not the test",
        guidance=(
            "If a test in `tests/test_lookahead.py` fails, the change is wrong — those "
            "guard the one-bar delay between signal and fill, the single property that "
            "makes every other number here mean anything. Weakening any guard has to be "
            "stated out loud, in the response, with the reason."
        ),
        pattern=_p(
            r"\b(make|get)\s+(the\s+)?tests?\s+(to\s+)?(pass|go green|green)\b"
            r"|\b(adjust|change|edit|update|relax|weaken|loosen|rewrite)\s+(the\s+)?"
            r"tests?\s+(to|so)\b"
            r"|\b(skip|delete|remove|disable|comment out|xfail|ignore)\s+"
            r"(the\s+|that\s+|those\s+)?[\w\s]{0,20}?tests?\b"
        ),
    ),
    Rule(
        id="lookahead-vector",
        kind="caution",
        headline="That is a named lookahead vector",
        guidance=(
            "Precomputing indicators over the full series, adjusted prices (the "
            "adjustment factor is derived from future events), and a universe chosen "
            "because those symbols exist today are all future information reaching a "
            "decision. Run `lookahead-auditor` before trusting anything built this way."
        ),
        pattern=_p(
            r"\bpre.?comput\w+\b[^.?!]{0,40}\b(indicator\w*|signal\w*|feature\w*|series)\b"
            r"|\badjusted\s+(close|price\w*)\b"
            r"|\b(symbols?|universe|tickers?|coins?)\s+that\s+(still\s+)?"
            r"(exist|are listed|survived|are still around)\b"
            r"|\b(current|today'?s)\s+(constituents|universe|listings?)\b"
            r"|\bvectori[sz]e\s+the\s+(whole|entire|full)\s+series\b"
        ),
        unless=_ASKING_ABOUT,
    ),
    Rule(
        id="unearned-belief",
        kind="caution",
        headline="That question needs evidence that does not exist yet",
        guidance=(
            "An in-sample figure is not a result. Before answering: `/walk-forward` "
            "(efficiency, grid profitability, fills per fold, buy-and-hold), "
            "`lookahead-auditor`, `strategy-reviewer`, `overfitting-auditor`, and "
            "`/cost-check`. The checklist is `.claude/skills/backtest-review/`. "
            "Keep the answer numeric — 'it did well' is not a result."
        ),
        pattern=_p(
            r"\b(is|are|was|were)\s+(this|it|that|the|these|those)(\s+\w+)?\s+(any\s+)?"
            r"(good|ready|profitable|solid|reliable|worth (it|trading))\b"
            r"|\b(should|can|could)\s+(i|we)\s+(trade|deploy|use|run|back)\s+(this|it|that)\b"
            r"|\b(does|do)\s+(this|it|that|the|these)(\s+\w+)?\s+(actually\s+|really\s+)?work\b"
            r"|\bready for (paper|live|production|real)\b"
        ),
        requires=_STRATEGY_NOUN,
    ),
    Rule(
        id="live-trading",
        kind="caution",
        headline="There is no live path here, by design",
        guidance=(
            "This repository has never traded real money and currently cannot: no live "
            "broker adapter, no order routing to a venue. That is a deliberate stage, "
            "not an oversight — `docs/PLAN.md` Phase 6 says why, and the roadmap has no "
            "step 51. Do not add one without an explicit request. Credentials are read "
            "from the environment only and never enter the working tree."
        ),
        pattern=_p(
            r"\b(connect|hook|wire|plug)\s+(it\s+|this\s+|us\s+)?(up\s+)?to\s+"
            r"(binance|deribit|coinbase|kraken|the exchange|a broker|an exchange)\b"
            r"|\bplace\s+(a|an|some)?\s*(real|live|actual)?\s*orders?\b"
            r"|\bgo(ing)? live\b|\blive[\s_-]?trading\b|\breal money\b|\btrade for real\b"
            r"|\b(add|use|put|set|paste|enter)\s+(my|the|your)\s+(api\s+)?"
            r"(key|secret|credential)s?\b"
        ),
        unless=_ASKING_ABOUT,
    ),
    Rule(
        id="vague-scope",
        kind="caution",
        headline="No falsifiable target",
        guidance=(
            "Better where, and how would we know it worked? Name the file, the metric, "
            "or the behaviour that should change. Without a target, 'improve' resolves "
            "to whatever is easiest to show, which is rarely what matters."
        ),
        # Anchored to the whole prompt on purpose: "improve this by adding X"
        # has a target and must not fire.
        pattern=_p(
            r"^\s*(make (it|this) better|improve (this|it)|clean (this|it) up|"
            r"tidy (this|it) up|optimi[sz]e (this|it)|fix (this|it) up|"
            r"make (it|this) nicer|do better)\s*[.!]*\s*$"
        ),
    ),
    # -- Reminders ----------------------------------------------------------
    Rule(
        id="milestone-event-loop",
        kind="reminder",
        headline="Milestone: the event loop",
        guidance=(
            "Any change to the loop in `engine/backtester.py`, the fill logic in "
            "`engine/execution.py`, or the ordering guarantees in `data/feed.py` is a "
            "milestone. Plan first, then run `lookahead-auditor` on the diff."
        ),
        pattern=_p(
            r"\bbacktester\.py\b|\bexecution\.py\b|\bfeed\.py\b"
            r"|\bevent loop\b|\bfill logic\b|\bordering guarantee\w*\b"
        ),
    ),
    Rule(
        id="milestone-risk",
        kind="reminder",
        headline="Milestone: a risk limit",
        guidance=(
            "Adding or relaxing a risk limit is a milestone. Plan first, and remember "
            "the halt must never trap the account: a risk-reducing order stays allowed "
            "while halted."
        ),
        pattern=_p(
            r"\brisk[/\\]limits\.py\b|\bRiskManager\b|\bRiskLimits\b"
            r"|\b(add|new|change|another)\s+(a\s+)?risk limit\b|\bdrawdown halt\b"
        ),
        unless=_ASKING_ABOUT,
    ),
    Rule(
        id="milestone-instrument",
        kind="reminder",
        headline="Milestone: an instrument class",
        guidance=(
            "Adding an instrument class or changing the `Instrument` hierarchy is a "
            "milestone. Options carry their own traps — quoted IV is frequently "
            "unmarketable in size, and a delta-neutral book can be dangerously short "
            "vega."
        ),
        pattern=_p(
            r"\bInstrument hierarchy\b|\bnew instrument\b|\basset class\b"
            r"|\b(crypto|equity) options?\b|\boptions? chain\b|\bgreeks\b"
            r"|\bblack.?76\b|\bvol(atility)? surface\b"
        ),
    ),
    Rule(
        id="milestone-data-source",
        kind="reminder",
        headline="Milestone: a data source",
        guidance=(
            "Adding a venue, vendor, or file format is a milestone. The "
            "`adding-a-data-source` skill has the checklist: bar stamping (we use close "
            "time, computed not read), unit switches, still-forming final bars, and "
            "ordering validated rather than trusted."
        ),
        pattern=_p(
            r"\bnew (venue|vendor|exchange|data source|data feed)\b"
            r"|\badd (a |an )?(new )?(connector|data source|venue|vendor|exchange)\b"
            r"|\bingest\b.{0,20}\b(csv|json|data)\b"
        ),
    ),
)


def evaluate(prompt: str) -> list[Rule]:
    """Rules matching this prompt, in declaration order."""
    return [rule for rule in RULES if rule.matches(prompt)]


def render(rules: list[Rule]) -> str:
    """The context injected back into the session.

    Marked unambiguously as machine-generated. It must never be mistaken for
    something the user said — it is a note about their prompt, not their words.
    """
    if not rules:
        return ""
    lines = [
        "<prompt-guard>",
        "Automated note from .claude/hooks/check-prompt.sh. This is NOT user text.",
        "It is not a refusal: the prompt stands as written and should be answered.",
        "Apply the following while answering it.",
        "",
    ]
    for rule in rules:
        lines.append(f"[{rule.kind.upper()}: {rule.id}] {rule.headline}")
        lines.append(f"  {rule.guidance}")
        lines.append("")
    lines.append("Full wording for each rule: docs/PROMPTS.md")
    lines.append("</prompt-guard>")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Read a UserPromptSubmit payload on stdin; print context; always exit 0."""
    args = sys.argv[1:] if argv is None else argv
    if "--self-test" in args:
        return self_test()

    try:
        payload = json.load(sys.stdin)
        prompt = payload.get("user_prompt") or ""
        source = (payload.get("user_prompt_metadata") or {}).get("source", "user_input")
    except Exception:
        # Fail open. A malformed payload must never cost the user a turn.
        return 0

    if not isinstance(prompt, str) or source in SKIP_SOURCES:
        return 0

    try:
        output = render(evaluate(prompt))
    except Exception:
        return 0

    if output:
        print(output)
    return 0


def self_test() -> int:
    """Run the rules against the corpus without pytest, for CI and for humans.

    Precision is the hard gate: a guard that cries wolf gets switched off, and
    then it guards nothing. Recall is reported rather than enforced.
    """
    if not CORPUS.exists():
        print(f"No corpus at {CORPUS}", file=sys.stderr)
        return 1

    false_positives: list[str] = []
    missed: list[str] = []
    positives = negatives = 0

    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        case = json.loads(line)
        fired = {rule.id for rule in evaluate(case["prompt"])}
        expected = set(case.get("expect", []))

        if expected:
            positives += 1
            if not expected <= fired:
                missed.append(f"{case['prompt'][:60]!r} missed {sorted(expected - fired)}")
        else:
            negatives += 1
            if fired:
                false_positives.append(f"{case['prompt'][:60]!r} fired {sorted(fired)}")

    print(f"corpus: {positives} positive, {negatives} negative")
    print(f"precision: {negatives - len(false_positives)}/{negatives} clean negatives")
    print(f"recall:    {positives - len(missed)}/{positives} positives caught")

    for label, rows in (("FALSE POSITIVE", false_positives), ("MISSED", missed)):
        for row in rows:
            print(f"  {label}: {row}", file=sys.stderr)

    return 1 if false_positives or missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
