"""The defect corpus: real bugs from this repository's history, reintroducible.

Every defect here **actually happened**. The provenance field names the commit
or ADR that records it, and that is not decoration — a corpus of invented bugs
measures a reviewer against the imagination of whoever wrote the corpus, which
is the same person whose blind spots the reviewer exists to cover. Bugs that
got past a real review, into a real commit, are the only ones known to be
capable of getting past a real review.

Anchor-based rather than patch-based, deliberately. A unified diff applies with
fuzz and can land somewhere subtly wrong after the surrounding code moves; an
anchor that no longer appears **exactly once** raises :class:`DefectError`. The
corpus therefore reports its own staleness instead of silently mutating the
wrong line, which is the failure mode this repository names at the boundary of
every other module.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFECTS_DIR = Path(__file__).resolve().parent / "defects"

#: Defect categories. These mirror the audit surfaces the agents claim to
#: cover, so a category with no defect in it is itself a finding: it means an
#: agent asserts a capability nothing has ever tested.
CATEGORIES = frozenset(
    {
        "lookahead",
        "risk",
        "survivorship",
        "accounting",
        "evaluation",
        "data-integrity",
        "guard",
    }
)

SEVERITIES = frozenset({"critical", "high", "medium"})

#: Agents that a defect may be routed to. Read from `.claude/agents/` at
#: validation time would be better still, but a frozen list keeps the corpus
#: loadable without the agent files present (CI checks the two agree).
KNOWN_AGENTS = frozenset(
    {
        "lookahead-auditor",
        "risk-auditor",
        "overfitting-auditor",
        "strategy-reviewer",
        "data-validator",
        "guard-auditor",
        "quant-researcher",
        "test-writer",
        # Charter §63 roles, added 2026-09-01 (Phase C). Routing a corpus
        # entry to one of these is allowed but none is measured yet; see
        # harness/AGENT_PROTOCOL.md for which have been run against a mutant.
        "architect",
        "code-investigator",
        "market-researcher",
        "adversarial-reviewer",
        "creative-researcher",
        "implementer",
        "mechanical-engineer",
        "final-reviewer",
    }
)


class DefectError(RuntimeError):
    """A defect definition is malformed, or no longer matches the tree.

    Raised rather than warned. A corpus entry that cannot be applied is a
    silently missing test case, and a harness quietly measuring eleven defects
    while reporting twelve is worse than no harness.
    """


@dataclass(frozen=True, slots=True)
class Mutation:
    """One exact-text substitution against one file.

    `find` must appear **exactly once** in the target. Not "at least once":
    an anchor matching twice would mutate a site the author never inspected,
    and the ground-truth line number would then point at the wrong one of them.
    """

    file: str
    find: str
    replace: str

    def apply_to(self, text: str, *, defect_id: str) -> str:
        occurrences = text.count(self.find)
        if occurrences != 1:
            raise DefectError(
                f"{defect_id}: anchor for {self.file} appears {occurrences} times, "
                f"expected exactly 1. The corpus is stale — the source moved under it. "
                f"Anchor begins: {self.find.strip().splitlines()[0][:80]!r}"
            )
        return text.replace(self.find, self.replace, 1)

    def line_in(self, text: str) -> int:
        """1-indexed line where the anchor starts, for localisation scoring."""
        index = text.find(self.find)
        if index < 0:
            raise DefectError(f"anchor not present in {self.file}")
        return text.count("\n", 0, index) + 1


@dataclass(frozen=True, slots=True)
class Defect:
    """One reintroducible defect, with the ground truth needed to score a find."""

    id: str
    title: str
    category: str
    severity: str
    provenance: str
    expected_agent: str
    #: What a reviewer would have to say to have actually found this, in
    #: reviewer-facing words. Used as the rubric for scoring a free-text audit.
    tell: str
    mutations: tuple[Mutation, ...]
    #: Test files expected to fail when this is applied. Empty tuple means
    #: "unknown, and that is the question" — never means "none".
    expected_killers: tuple[str, ...] = ()
    #: Set when the defect is known to survive the suite. A documented hole is
    #: a finding, not a corpus error, but it must be declared rather than
    #: discovered so a NEW hole is still a failure.
    known_survivor: bool = False

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(m.file for m in self.mutations))


def _require(raw: dict[str, Any], key: str, source: Path) -> Any:
    if key not in raw:
        raise DefectError(f"{source.name}: missing required key {key!r}")
    return raw[key]


def load_defect(path: Path) -> Defect:
    """Parse and validate one defect file.

    Validation is strict and total: an unknown category or an unknown agent
    raises. The alternative — accepting a typo'd category and silently filing
    the defect nowhere — would let a corpus entry exist while never being
    routed to any reviewer, which reads as a passing eval.
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - malformed file
        raise DefectError(f"{path.name}: not valid TOML: {exc}") from exc

    defect_id = str(_require(raw, "id", path))
    if defect_id != path.stem.split("-", 1)[0]:
        raise DefectError(
            f"{path.name}: id {defect_id!r} does not match the filename prefix. "
            "The filename is how a human finds the file from a report."
        )

    category = str(_require(raw, "category", path))
    if category not in CATEGORIES:
        raise DefectError(
            f"{path.name}: unknown category {category!r}; known: {sorted(CATEGORIES)}"
        )

    severity = str(_require(raw, "severity", path))
    if severity not in SEVERITIES:
        raise DefectError(f"{path.name}: unknown severity {severity!r}")

    agent = str(_require(raw, "expected_agent", path))
    if agent not in KNOWN_AGENTS:
        raise DefectError(f"{path.name}: unknown agent {agent!r}; known: {sorted(KNOWN_AGENTS)}")

    raw_mutations = _require(raw, "mutation", path)
    if not isinstance(raw_mutations, list) or not raw_mutations:
        raise DefectError(f"{path.name}: needs at least one [[mutation]] block")

    mutations = []
    for entry in raw_mutations:
        for key in ("file", "find", "replace"):
            if key not in entry:
                raise DefectError(f"{path.name}: [[mutation]] missing {key!r}")
        if entry["find"] == entry["replace"]:
            raise DefectError(
                f"{path.name}: find and replace are identical — this mutation "
                "changes nothing, so the defect could never be detected or missed. "
                "That is a guard that cannot fail."
            )
        mutations.append(
            Mutation(
                file=str(entry["file"]), find=str(entry["find"]), replace=str(entry["replace"])
            )
        )

    tell = str(_require(raw, "tell", path)).strip()
    if len(tell) < 20:
        raise DefectError(
            f"{path.name}: 'tell' is too short to score against. It must state what a "
            "reviewer has to say to have genuinely found this."
        )

    return Defect(
        id=defect_id,
        title=str(_require(raw, "title", path)),
        category=category,
        severity=severity,
        provenance=str(_require(raw, "provenance", path)),
        expected_agent=agent,
        tell=tell,
        mutations=tuple(mutations),
        expected_killers=tuple(str(k) for k in raw.get("expected_killers", ())),
        known_survivor=bool(raw.get("known_survivor", False)),
    )


def load_corpus(directory: Path | None = None) -> tuple[Defect, ...]:
    """Load every defect, sorted by id. Raises on the first malformed entry."""
    directory = directory or DEFECTS_DIR
    paths = sorted(directory.glob("*.toml"))
    if not paths:
        raise DefectError(f"no defect files in {directory}")

    defects = [load_defect(p) for p in paths]

    seen: dict[str, str] = {}
    for defect in defects:
        if defect.id in seen:
            raise DefectError(f"duplicate defect id {defect.id!r}")
        seen[defect.id] = defect.title

    return tuple(sorted(defects, key=lambda d: d.id))
