"""The guard on the store suite. Lives here because it must never be skipped.

It used to live in `tests/test_store.py` under that module's `pytestmark`, which
made it skip in exactly the cases it was written to catch: with no
`TRADING_PLATFORM_TEST_DSN` it skipped, with an unreachable one it skipped, and
with a reachable one it asserted the predicate the mark had already required —
trivially green in all three. An adversarial review of the store found it, and
it is corpus entry G02: a test that cannot fail is worse than no test.

This file has no module-level skip. It runs on every invocation of the suite,
including the one where psycopg is not installed.

Two obligations:

**When the store tests are declared required** — `TRADING_PLATFORM_STORE_TESTS_REQUIRED`
set, which CI and `scripts/dev_postgres.sh env` both do — the driver must be
installed, the DSN set, and the database reachable. Failing any of those is a
failure, not a skip. This is what stops a CI that quietly lost its database from
reporting green over 20-odd skips.

**When a database IS reachable** the suite must actually be running against it.
That is checked by reading the skip condition `tests/test_store.py` computed at
import time, not by recomputing it here: recomputing is what made the old guard
vacuous.
"""

from __future__ import annotations

import importlib
import importlib.util
import os

import pytest

DSN_VAR = "TRADING_PLATFORM_TEST_DSN"
REQUIRED_VAR = "TRADING_PLATFORM_STORE_TESTS_REQUIRED"
#: Every module that skips itself without a database. A new one added here and
#: nowhere else is a suite that can go quiet unnoticed.
STORE_BACKED_MODULES = (
    "tests.test_store",
    "tests.test_reconcile",
    "tests.test_account_store",
    "tests.test_ticket_sync",
    "tests.test_macro",
)
_FALSE = ("", "0", "false", "no", "off")


def store_tests_required() -> bool:
    return os.environ.get(REQUIRED_VAR, "").strip().lower() not in _FALSE


def _reachable(dsn: str) -> bool:
    import psycopg

    try:
        psycopg.connect(dsn).close()
    except Exception:
        return False
    return True


def test_the_store_suite_is_not_quietly_disabled() -> None:
    """Mutation: set the skip condition in `tests/test_store.py` to a constant
    `True` while a database is reachable. The suite goes to 20-odd skips and
    this test goes red, which is the whole point of it. Second mutation: unset
    the DSN in a run where `TRADING_PLATFORM_STORE_TESTS_REQUIRED` is set —
    also red, where the old guard skipped."""
    required = store_tests_required()
    driver = importlib.util.find_spec("psycopg") is not None
    dsn = os.environ.get(DSN_VAR) or None

    if required:
        assert driver, (
            f"{REQUIRED_VAR} is set but psycopg is not installed: pip install -e '.[store]'"
        )
        assert dsn is not None, f"{REQUIRED_VAR} is set but {DSN_VAR} is unset: the store never ran"
        assert _reachable(dsn), f"{REQUIRED_VAR} is set but {dsn.split('@')[-1]} is unreachable"

    if not driver or dsn is None or not _reachable(dsn):
        pytest.skip(
            f"no reachable store: there is nothing for this guard to police. "
            f"Set {REQUIRED_VAR}=1 to make that a failure instead."
        )

    for name in STORE_BACKED_MODULES:
        module = importlib.import_module(name)
        mark = getattr(module, "pytestmark", None)
        assert mark is not None, (
            f"{name.replace('.', '/')}.py no longer declares a pytestmark. If the skip was "
            "removed deliberately, drop it from STORE_BACKED_MODULES; if it was renamed, "
            "this guard is now blind to it."
        )
        assert not bool(mark.mark.args[0]), (
            f"{DSN_VAR} points at a reachable database, yet {name} computed its skip "
            "condition as True. That suite is being skipped against a live database, and "
            "a skipped suite looks exactly like a passing one."
        )


def test_the_guard_itself_has_no_module_level_skip() -> None:
    """Mutation: add a `pytestmark` skip to this file — the defect that made the
    original guard vacuous. This test is what notices."""
    module = importlib.import_module("tests.test_store_guard")
    assert not hasattr(module, "pytestmark"), (
        "this file must not carry a module-level skip: it is the guard on a suite "
        "that skips, and a guard that skips with its subject guards nothing."
    )
