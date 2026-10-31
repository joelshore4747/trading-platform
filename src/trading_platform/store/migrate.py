"""Forward-only schema migrations, with the edited-history case treated as a defect.

Three rules, each there because of a specific way schemas rot.

**Forward only.** There is no down-migration. A down-migration is a plan for
destroying data written since the up-migration ran, and it is written at the one
moment nobody knows what that data will be. Rolling back means restoring a
backup, which is honest about what is being lost.

**Checksummed.** An applied migration's text is hashed and stored. Editing a
migration that has already run makes the database and the repository disagree
about what the schema is, and nothing else would notice: the file says one thing
and the live table says another, forever. That raises here instead.

**One transaction each.** A migration either happens or does not. A half-applied
schema is the state nobody writes recovery code for.
"""

from __future__ import annotations

import hashlib
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"
_FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER     PRIMARY KEY,
    name        TEXT        NOT NULL,
    checksum    TEXT        NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE schema_migrations IS
    'Which migrations have run, and the sha256 of each as it was when it ran. A checksum that no longer matches the file means the migration was edited after being applied, so the schema in this database is not the one the repository describes.';
"""


class MigrationError(RuntimeError):
    """A migration is malformed, out of order, or has been edited after running."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    def __str__(self) -> str:
        return f"{self.version:04d}_{self.name}"


def discover(directory: Path | None = None) -> list[Migration]:
    """Every migration on disk, in version order.

    A gap in the numbering raises. Gaps happen when two branches each add
    `0007_` and one is renamed to `0009_` on merge: the missing 8 is the trace
    of a migration someone dropped, and skipping past it silently is how a
    column goes missing on one machine only.
    """
    directory = directory or MIGRATIONS_DIR
    if not directory.is_dir():
        raise MigrationError(f"no migrations directory at {directory}")

    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if match is None:
            raise MigrationError(
                f"{path.name} is not a migration filename; expected NNNN_lower_snake.sql"
            )
        found.append(
            Migration(
                version=int(match.group(1)),
                name=match.group(2),
                path=path,
                sql=path.read_text(encoding="utf-8"),
            )
        )

    versions = [m.version for m in found]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"migration versions must be 1..n with no gaps, found {versions}")
    return found


def applied_migrations(connection: Any) -> dict[int, tuple[str, str, datetime]]:
    """version -> (name, checksum, applied_at) for everything already run."""
    with connection.cursor() as cursor:
        cursor.execute(_BOOTSTRAP)
        cursor.execute("SELECT version, name, checksum, applied_at FROM schema_migrations")
        rows = cursor.fetchall()
    connection.commit()
    return {int(v): (str(n), str(c), a) for v, n, c, a in rows}


def pending_migrations(connection: Any, directory: Path | None = None) -> list[Migration]:
    """Migrations on disk that this database has not run, checked for tampering.

    Verifies every *applied* migration first. A drifted checksum is raised
    before anything new is applied, because stacking a new migration on a schema
    that is not the one the repository describes compounds the problem.
    """
    on_disk = discover(directory)
    already = applied_migrations(connection)

    for migration in on_disk:
        record = already.get(migration.version)
        if record is None:
            continue
        name, checksum, applied_at = record
        if checksum != migration.checksum:
            raise MigrationError(
                f"{migration} was applied at {applied_at:%Y-%m-%d %H:%M} and its file has "
                "changed since. The schema in this database is not the one this file "
                "describes. Add a new migration; never edit one that has run."
            )
        if name != migration.name:
            raise MigrationError(
                f"migration {migration.version:04d} ran as {name!r} and is now named "
                f"{migration.name!r}. Renaming an applied migration hides which one ran."
            )

    unknown = sorted(set(already) - {m.version for m in on_disk})
    if unknown:
        raise MigrationError(
            f"this database has run migration(s) {unknown} that are not in {directory or MIGRATIONS_DIR}. "
            "It is ahead of this checkout, or the files were deleted."
        )
    return [m for m in on_disk if m.version not in already]


#: One arbitrary but fixed key, so every process migrating this database queues
#: on the same lock. Chosen once; changing it would let an old build migrate
#: alongside a new one, which is the situation the lock exists to prevent.
_MIGRATE_LOCK_KEY = 8_534_119_027_461_733


def migrate(connection: Any, directory: Path | None = None) -> list[Migration]:
    """Apply every pending migration, one transaction each. Returns what ran.

    Serialised across processes with an advisory lock. Several collector
    processes starting together each call this, and without the lock they race
    on the bootstrap `CREATE TABLE IF NOT EXISTS` and fail with a catalogue
    `UniqueViolation` — an error that names neither the cause nor the fix, on a
    path whose whole job is to be safe to call every time.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", (_MIGRATE_LOCK_KEY,))
    connection.commit()
    try:
        return _migrate_locked(connection, directory)
    finally:
        with suppress(Exception):
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATE_LOCK_KEY,))
            connection.commit()


def _migrate_locked(connection: Any, directory: Path | None = None) -> list[Migration]:
    pending = pending_migrations(connection, directory)
    for migration in pending:
        with connection.cursor() as cursor:
            cursor.execute(migration.sql)
            cursor.execute(
                "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                (migration.version, migration.name, migration.checksum),
            )
        connection.commit()
    return pending
