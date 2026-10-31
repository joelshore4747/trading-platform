"""A strategy's version is its code and its parameters, hashed (charter §83).

"SMACrossover" names a family. A result belongs to one member of it: the
source that ran and the parameters it ran with. Thirty tweaks in thirty days
is thirty versions, and a result recorded against the family name alone is a
result nobody can reproduce or count.

`fingerprint` derives the version rather than asking for it: the SHA-256 of
the strategy class's source file plus the strategy's public attributes at the
time of the call. Derived, so it cannot be forgotten or left stale; from the
file rather than the class body, so a helper function edited two screens
above the class changes the version too. What it does not capture is stated:
a parameter held somewhere other than on the instance, and the version of
every module the strategy imports — the commit hash beside it covers those.
"""

from __future__ import annotations

import hashlib
import inspect
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from trading_platform.strategy.base import Strategy

__all__ = ["StrategyVersion", "fingerprint"]


@dataclass(frozen=True, slots=True)
class StrategyVersion:
    family: str
    #: SHA-256 of the file the class was defined in, first 12 hex digits.
    source: str
    #: Public instance attributes that look like parameters, rendered stably.
    params: tuple[tuple[str, str], ...]
    #: SHA-256 over source and params together, first 12 hex digits.
    digest: str

    @property
    def label(self) -> str:
        return f"{self.family}@{self.digest}"

    def __str__(self) -> str:
        shown = ", ".join(f"{k}={v}" for k, v in self.params) or "-"
        return f"{self.family} src={self.source} params[{shown}] -> {self.digest}"


def _param_like(value: object) -> bool:
    return isinstance(value, bool | int | float | str | Decimal | tuple)


def fingerprint(strategy: Strategy) -> StrategyVersion:
    """Derive the version of a strategy instance. Raises if its source cannot be read."""
    cls = type(strategy)
    try:
        located = inspect.getsourcefile(cls)
    except TypeError:
        located = None
    if located is None:
        module = sys.modules.get(cls.__module__)
        located = getattr(module, "__file__", None)
    path = Path(located or "")
    if not path.is_file():
        raise ValueError(
            f"cannot version {cls.__name__}: its source file is not on disk "
            "(a strategy defined in a REPL or a test body has no reproducible version)"
        )
    source = hashlib.sha256(path.read_bytes()).hexdigest()[:12]

    params: list[tuple[str, str]] = []
    for key in sorted(vars(strategy)):
        if key.startswith("_") or key in ("name",):
            continue
        value = vars(strategy)[key]
        if _param_like(value):
            params.append((key, repr(value)))

    material = f"{cls.__module__}.{cls.__qualname__}|{source}|{params!r}".encode()
    digest = hashlib.sha256(material).hexdigest()[:12]
    return StrategyVersion(family=cls.__name__, source=source, params=tuple(params), digest=digest)
