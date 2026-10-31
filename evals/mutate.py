"""Build a scratch copy of the repository with one defect reintroduced.

Two properties matter more than anything else here.

**The working tree is never touched.** A harness that mutates the real source
and reverts it in a ``finally`` block is one interrupted process away from
leaving a deliberately broken engine on disk, and this repository's whole
premise is that silent wrongness is the expensive kind. Everything happens in a
throwaway directory; the real tree is opened read-only.

**The mutant tree does not contain the corpus.** ``evals/`` is excluded from
the copy, and a test enforces it. An auditor that can read the answer key would
score perfectly while detecting nothing — future information reaching a
decision, which is the exact bug class this harness exists to measure. It would
be a poor joke to build a lookahead detector with a lookahead in it.

The mutant imports ahead of the editable install via ``PYTHONPATH``: the
scratch ``src`` sits earlier on ``sys.path`` than ``site-packages``, so tests
run against the mutated code while using the real virtualenv's dependencies.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from evals.corpus import Defect, DefectError

PROJECT_DIR = Path(__file__).resolve().parent.parent

#: Never copied into a mutant tree.
#:
#: ``evals`` is the answer key and its exclusion is load-bearing, not tidiness.
#: The rest is either large and irrelevant (``historical.zip`` is 10 MB of
#: market data no unit test reads), regenerable, or would make the copy slower
#: than the test run it exists to serve.
EXCLUDED = frozenset(
    {
        ".git",
        ".venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "evals",
        # The harness's own tests import `evals`, which is excluded above. Left
        # in place they raise ImportError at collection inside every mutant,
        # which the suite runner correctly classifies as ERROR rather than
        # KILLED — poisoning every full-suite run instead of failing quietly.
        # They are the harness's guards, not the platform's, so they have no
        # business running against a mutant in the first place.
        "test_evals.py",
        "runs",
        "historical.zip",
        "node_modules",
    }
)


@dataclass(slots=True)
class MutantTree:
    """A materialised mutant, plus where the defect actually landed."""

    root: Path
    defect: Defect | None
    #: `file -> 1-indexed line` where each mutation landed, for localisation
    #: scoring. Empty for a clean control.
    sites: dict[str, int] = field(default_factory=dict)

    @property
    def is_control(self) -> bool:
        """True when nothing was mutated — the false-positive arm."""
        return self.defect is None

    @property
    def label(self) -> str:
        return "CONTROL" if self.defect is None else self.defect.id

    def env(self) -> dict[str, str]:
        """Environment that makes this tree's source win over the real one."""
        env = dict(os.environ)
        src = str(self.root / "src")
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{src}{os.pathsep}{existing}" if existing else src
        return env

    def cleanup(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)


def _ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in EXCLUDED}


def copy_tree(dest: Path, *, source: Path | None = None) -> Path:
    """Copy the repository to `dest`, minus :data:`EXCLUDED`."""
    source = source or PROJECT_DIR
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest, ignore=_ignore, symlinks=True)
    return dest


def materialise(
    defect: Defect | None,
    dest: Path,
    *,
    source: Path | None = None,
) -> MutantTree:
    """Copy the tree and apply `defect`, or none of it for a control.

    Raises :class:`DefectError` if an anchor no longer matches exactly once, or
    if applying the mutation left the file unchanged — the second check exists
    because a substitution that silently no-ops would produce a mutant
    indistinguishable from a control, scored as a detection failure, and
    quietly lower every reported rate.
    """
    source = source or PROJECT_DIR
    root = copy_tree(dest, source=source)
    tree = MutantTree(root=root, defect=defect)

    if defect is None:
        return tree

    for mutation in defect.mutations:
        target = root / mutation.file
        if not target.is_file():
            raise DefectError(f"{defect.id}: {mutation.file} does not exist in the tree")

        original = target.read_text(encoding="utf-8")
        tree.sites[mutation.file] = mutation.line_in(original)
        mutated = mutation.apply_to(original, defect_id=defect.id)

        if mutated == original:  # pragma: no cover - guarded by apply_to
            raise DefectError(f"{defect.id}: applying the mutation changed nothing")

        target.write_text(mutated, encoding="utf-8")

    return tree


def verify_corpus_applies(defects: tuple[Defect, ...], scratch: Path) -> list[str]:
    """Apply every defect once and return the ids that failed.

    The cheap staleness check. It costs one tree copy per defect and no model
    calls, so it can run in CI on every push while the agent evaluation cannot.
    """
    failures: list[str] = []
    for defect in defects:
        tree = None
        try:
            tree = materialise(defect, scratch / f"verify-{defect.id}")
        except DefectError:
            failures.append(defect.id)
        finally:
            if tree is not None:
                tree.cleanup()
    return failures
