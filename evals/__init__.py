"""The harness that measures this repository's own defences.

Everything under ``.claude/`` — seven agents, seven commands, two skills, five
hooks — is prose. Prose is not a control. ``scripts/prompt_guard.py`` is the
only part of the AI layer with a corpus behind it, and writing that corpus
found four bugs in rules that read perfectly well.

This package extends that treatment to the rest. It holds a corpus of defects
that **actually occurred in this repository**, each reintroducible into a
scratch copy of the tree, and runs two questions over it:

1. **Does the test suite catch it?** (:mod:`evals.suite`) A mutant that
   survives is a guard that cannot fail — the single most repeated defect in
   this project's history, found mechanically instead of by luck.
2. **Does the auditor catch it?** (:mod:`evals.agent`) ``lookahead-auditor``
   has never been measured. A missed leak does not crash; it produces a
   beautiful fictional equity curve. Its false-negative rate is the most
   important unknown number in the repository.

The methodology is ADR 0003's, pointed at the reviewers instead of the
strategies. ADR 0003 put data with no predictability through the pipeline and
counted false discoveries. This puts code with known defects through the
defences and counts false negatives — and clean code through them and counts
false positives, because a check that fires on everything says nothing.
"""

from evals.corpus import Defect, DefectError, Mutation, load_corpus, load_defect
from evals.mutate import MutantTree, materialise

__all__ = [
    "Defect",
    "DefectError",
    "MutantTree",
    "Mutation",
    "load_corpus",
    "load_defect",
    "materialise",
]
