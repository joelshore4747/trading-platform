"""Connecting to the store, without ever holding the password in the repository.

The connection string is a credential: it usually carries one. So it is read
through `core.credentials.Credential`, which reads the environment and nothing
else, redacts itself in every repr and traceback, and names the missing variable
when it is absent rather than failing with a connection error three frames later.

There is no default DSN and no localhost fallback. A fallback here would mean a
script that silently wrote to the wrong database, and "the wrong database"
includes "an empty one that looks like everything is fine".
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from trading_platform.core.credentials import Credential

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

#: The one environment variable that says where the store is.
DSN_VAR = "TRADING_PLATFORM_DSN"


class StoreError(RuntimeError):
    """The store could not be reached, or was reached and disagreed with us."""


def is_configured(var: str = DSN_VAR) -> bool:
    """True when a DSN is present in the environment.

    For callers that must degrade rather than fail: the paper supervisor keeps
    journalling to disk whether or not the database is configured, because a
    missing database must never cost a bar.
    """
    return Credential.from_env(var).is_present


def dsn(var: str = DSN_VAR) -> str:
    """The connection string, or a `MissingCredentialError` naming the variable."""
    return Credential.from_env(var).require()


@contextmanager
def connect(target: str | None = None, *, autocommit: bool = False) -> Iterator[Any]:
    """A connection, closed on the way out.

    `target` is for tests and for tools that already hold a DSN; leaving it
    None reads the environment. The import is deferred so that importing
    `trading_platform.store` costs nothing and works on a machine with no
    driver installed — the failure then happens at the point of use, naming
    what to install.
    """
    try:
        import psycopg
    except ModuleNotFoundError as exc:  # pragma: no cover - environment-dependent
        raise StoreError(
            "psycopg is not installed. `uv pip install -e '.[store]'` — the store is "
            "optional, and everything that does not touch it keeps working without it."
        ) from exc

    try:
        connection = psycopg.connect(target or dsn(), autocommit=autocommit)
    except psycopg.OperationalError as exc:
        # The DSN is a credential: `str(exc)` from the driver can echo it back,
        # password included, into a log or a traceback.
        raise StoreError(f"could not connect to the store: {type(exc).__name__}") from None
    try:
        yield connection
    finally:
        connection.close()
