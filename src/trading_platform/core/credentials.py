"""Credential handling.

**This repository has no live trading path and holds no credentials.** This
module exists so that when one is eventually needed, the safe pattern is
already the obvious one — not so that keys can be added today.

Three rules, all enforced here rather than left to discipline:

1. **Values come from the environment only.** No file reading, no defaults, no
   config lookup. A secret that can be committed eventually is.
2. **A credential never prints itself.** ``__repr__`` and ``__str__`` are
   redacted, so a secret cannot reach a log, a traceback, or a debugger dump
   by accident. This is the failure mode that actually happens.
3. **Absence fails loudly at the point of use**, naming the variable that is
   missing, rather than silently sending an unauthenticated request.

Read-only market-data scopes are the only permissions this project would ever
want. Nothing here can place an order, and no code in this repository can.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

REDACTED = "<redacted>"


class MissingCredentialError(RuntimeError):
    """Raised when a required credential is absent from the environment."""


@dataclass(frozen=True, slots=True)
class Credential:
    """One named secret, resolved from an environment variable.

    Construct with :meth:`from_env`; the value is never logged or displayed.
    """

    name: str
    _value: str | None = None

    @classmethod
    def from_env(cls, name: str) -> Credential:
        """Read ``name`` from the environment. Absent is fine — see ``require``."""
        raw = os.environ.get(name)
        # Treat blank as absent: .env.example ships every variable empty, and a
        # half-configured environment should fail like an unconfigured one
        # rather than sending an empty key and getting a confusing 401.
        return cls(name=name, _value=raw if raw else None)

    @property
    def is_present(self) -> bool:
        return self._value is not None

    def require(self) -> str:
        """Return the value, or raise naming the variable that is missing."""
        if self._value is None:
            raise MissingCredentialError(
                f"Environment variable {self.name} is not set. "
                f"Copy .env.example to .env and fill it in. "
                f"Only read-only market-data scopes are ever needed here."
            )
        return self._value

    def reveal(self) -> str | None:
        """Explicit accessor for the raw value.

        Named to be conspicuous at the call site and in review. Anything that
        reads a secret should be easy to grep for.
        """
        return self._value

    def __repr__(self) -> str:
        state = "set" if self.is_present else "unset"
        return f"Credential(name={self.name!r}, value={REDACTED}, {state})"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class CredentialSet:
    """The credentials one venue would need.

    A venue that needs none — Binance klines are a public endpoint — declares an
    empty set. Saying so explicitly stops someone adding keys that were never
    required, which is the cheapest possible security win.
    """

    venue: str
    credentials: tuple[Credential, ...] = ()

    @classmethod
    def from_env(cls, venue: str, *names: str) -> CredentialSet:
        return cls(venue=venue, credentials=tuple(Credential.from_env(n) for n in names))

    @property
    def required_variables(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.credentials)

    @property
    def is_complete(self) -> bool:
        """True when every declared credential is present. Vacuously true when
        none are declared, which is correct for public endpoints."""
        return all(c.is_present for c in self.credentials)

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.credentials if not c.is_present)

    def require_all(self) -> dict[str, str]:
        if self.missing:
            raise MissingCredentialError(
                f"{self.venue} is missing: {', '.join(self.missing)}. See .env.example."
            )
        return {c.name: c.require() for c in self.credentials}

    def __repr__(self) -> str:
        if not self.credentials:
            return f"CredentialSet(venue={self.venue!r}, public — no credentials needed)"
        state = "complete" if self.is_complete else f"missing {list(self.missing)}"
        return (
            f"CredentialSet(venue={self.venue!r}, "
            f"variables={list(self.required_variables)}, {state})"
        )

    __str__ = __repr__
