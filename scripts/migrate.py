#!/usr/bin/env python
"""Apply the store's schema migrations, or say what is pending.

    scripts/migrate.py                 # what is applied, what is pending
    scripts/migrate.py --check         # exit 1 if anything is pending
    scripts/migrate.py --apply         # run them

Until now the only documented way to create the schema was a `python -c` block
pasted out of `docs/SETUP.md`, and five scripts that migrate on startup as a
side effect of doing something else. Neither is a thing an installer can call:
the first cannot be checked for an exit code without re-typing it, and the
second applies a schema change as a side effect of starting a collector. A
deploy that wants the schema up to date before it enables a unit needs to ask
that question on its own.

`--check` is the gate `deploy/install.sh` runs before enabling anything, and is
the honest half of this script: it answers "is this database the one this
checkout describes" without changing it. It is not perfectly read-only, and the
one thing it does write is worth naming — `applied_migrations` creates the
`schema_migrations` bookkeeping table if it is absent, because a database with
no such table has run nothing and that is a fact it needs the table to record.
No migration in `migrations/` is applied.

Everything underneath is `store/migrate.py`, unchanged: forward-only, one
transaction per migration, sha256-checksummed so an edited migration that has
already run raises instead of stacking a new one on a schema nobody can
reproduce, and serialised across processes on a fixed advisory lock.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_platform.store import (
    MigrationError,
    StoreError,
    applied_migrations,
    connect,
    is_configured,
    migrate,
    pending_migrations,
)
from trading_platform.store.connection import DSN_VAR


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply the store's schema migrations, or say what is pending."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if any migration is pending, and apply nothing. What an "
        "installer runs before enabling a unit.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="apply every pending migration. Forward-only; there is no --rollback, "
        "and adding one would be a schema change nobody could reproduce.",
    )
    args = parser.parse_args(argv)

    if not is_configured():
        print(
            f"{DSN_VAR} is not set. It is read from the environment only — never from a "
            f"file in this repository. See docs/SETUP.md section 3.",
            file=sys.stderr,
        )
        return 2

    try:
        with connect() as conn:
            already = applied_migrations(conn)
            outstanding = pending_migrations(conn)

            for version in sorted(already):
                name, _checksum, applied_at = already[version]
                print(f"  applied  {version:04d}_{name}  {applied_at:%Y-%m-%d %H:%M}")

            if not outstanding:
                print(f"{len(already)} migration(s) applied, none pending.")
                return 0

            for migration in outstanding:
                print(f"  PENDING  {migration}")

            if not args.apply:
                print(
                    f"{len(outstanding)} migration(s) pending. "
                    f"Run {Path(__file__).name} --apply to apply them."
                )
                # --check is asking a question, and "pending" is a non-zero
                # answer. Without --check this is a report, and reporting
                # accurately is success.
                return 1 if args.check else 0

            ran = migrate(conn)
            for migration in ran:
                print(f"  applied  {migration}")
            print(f"{len(ran)} migration(s) applied.")
            return 0
    except MigrationError as exc:
        # The schema in the database is not the one this checkout describes.
        # Never repaired automatically: the fix depends on which of the two is
        # wrong, and guessing writes the wrong one over the other.
        print(f"migration refused: {exc}", file=sys.stderr)
        return 1
    except StoreError as exc:
        print(f"store unreachable: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
