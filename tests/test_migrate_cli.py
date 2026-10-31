"""The migration CLI: the gate an installer runs before it enables anything.

The claim under test is not "it applies migrations" — `store/migrate.py` is
tested for that in `tests/test_store.py`. It is that the CLI's *exit codes* mean
what `deploy/install.sh` assumes they mean, because the installer refuses to
enable a unit on a non-zero one and a wrong code there installs a collector
against a schema that is not the one this checkout describes.

Four codes, and each is asserted against a database in the state that produces
it: 0 nothing pending, 1 something pending or a refusal, 2 unusable environment.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_DSN_VAR = "TRADING_PLATFORM_TEST_DSN"

psycopg = pytest.importorskip("psycopg", reason="the store needs psycopg")


def _dsn() -> str | None:
    return os.environ.get(TEST_DSN_VAR) or None


def _reachable(dsn: str) -> bool:
    try:
        psycopg.connect(dsn).close()
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    _dsn() is None or not _reachable(_dsn() or ""),
    reason=f'set {TEST_DSN_VAR} — scripts/dev_postgres.sh start && eval "$(scripts/dev_postgres.sh env)"',
)


def load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "migrate_cli_under_test", ROOT / "scripts" / "migrate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cli = load()


@pytest.fixture
def schema_dsn(request: pytest.FixtureRequest) -> Iterator[str]:
    """A DSN pinned to a schema of its own, dropped after.

    Per-schema because migration bookkeeping is per-`search_path` (T-cc294dae):
    two tests sharing one schema would see each other's applied migrations and
    the second would find nothing pending.
    """
    base = _dsn()
    assert base is not None
    schema = "m_" + str(abs(hash(request.node.nodeid)) % 10**12)
    admin = psycopg.connect(base)
    with admin.cursor() as cursor:
        cursor.execute(f'CREATE SCHEMA "{schema}"')
    admin.commit()
    sep = "&" if "?" in base else "?"
    try:
        yield f"{base}{sep}options={quote(f'-csearch_path={schema}')}"
    finally:
        with admin.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.commit()
        admin.close()


def _tables(dsn: str) -> set[str]:
    conn = psycopg.connect(dsn)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            )
            return {str(row[0]) for row in cursor.fetchall()}
    finally:
        conn.close()


def test_an_unset_dsn_is_a_usage_error_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """2, not 1: nothing was wrong with the schema, the environment was unusable.

    An installer distinguishes them — a pending migration is something to apply,
    an unset DSN is something to go and set.
    """
    monkeypatch.delenv("TRADING_PLATFORM_DSN", raising=False)
    assert cli.main([]) == 2
    assert "TRADING_PLATFORM_DSN" in capsys.readouterr().err


def test_check_exits_one_while_anything_is_pending(
    monkeypatch: pytest.MonkeyPatch, schema_dsn: str
) -> None:
    monkeypatch.setenv("TRADING_PLATFORM_DSN", schema_dsn)
    assert cli.main(["--check"]) == 1


def test_check_applies_nothing(monkeypatch: pytest.MonkeyPatch, schema_dsn: str) -> None:
    """The honesty claim in the script's docstring, asserted rather than promised.

    `--check` creates only `schema_migrations`, because a database that has run
    nothing needs that table to record the fact. No domain table may appear: an
    installer runs this against production before deciding anything, and a check
    that migrates is not a check.
    """
    monkeypatch.setenv("TRADING_PLATFORM_DSN", schema_dsn)
    cli.main(["--check"])
    assert _tables(schema_dsn) <= {"schema_migrations"}
    assert "bars" not in _tables(schema_dsn)


def test_apply_then_check_is_clean_and_applying_twice_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, schema_dsn: str
) -> None:
    monkeypatch.setenv("TRADING_PLATFORM_DSN", schema_dsn)
    assert cli.main(["--apply"]) == 0
    after_first = _tables(schema_dsn)
    assert "bars" in after_first and "schema_migrations" in after_first

    assert cli.main(["--check"]) == 0
    assert cli.main(["--apply"]) == 0
    assert _tables(schema_dsn) == after_first


def test_a_report_with_something_pending_is_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, schema_dsn: str
) -> None:
    """Without --check this is a report, and reporting accurately is success.

    The distinction matters because the installer calls `--check` for the gate
    and a human calls it bare to look; a bare run that exited 1 would make
    `set -e` scripts abort on a working database.
    """
    monkeypatch.setenv("TRADING_PLATFORM_DSN", schema_dsn)
    assert cli.main([]) == 0


def test_an_edited_migration_is_refused_rather_than_stacked_on(
    monkeypatch: pytest.MonkeyPatch, schema_dsn: str
) -> None:
    """A checksum that has drifted means the schema is not the one this checkout
    describes. The CLI must surface that as a refusal, not apply the rest on top."""
    monkeypatch.setenv("TRADING_PLATFORM_DSN", schema_dsn)
    assert cli.main(["--apply"]) == 0

    conn = psycopg.connect(schema_dsn)
    with conn.cursor() as cursor:
        cursor.execute("UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 1")
    conn.commit()
    conn.close()

    assert cli.main(["--check"]) == 1
    assert cli.main(["--apply"]) == 1
