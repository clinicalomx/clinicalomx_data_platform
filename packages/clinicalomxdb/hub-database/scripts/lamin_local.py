#!/usr/bin/env python3
"""Create and connect a local lamindb instance on this stack's own Postgres.

    make lamin-local

For a machine that cannot reach the hub instance's database. Gives `make schema-apply` and
`make schema-review` an instance to run against.

Never touches production. The storage root is local and the database URL is @localhost:, so
lamindb does not register the instance on LaminHub, and it gets its own dedicated database.

Needs no credential. The instance is owned by lamindb's anonymous user: LAMIN_API_KEY is
removed and the init runs with LAMIN_SETTINGS_DIR pointing at an empty temporary directory.

The first build makes one unauthenticated query to lamindb's hub. Later runs are offline.

Runs inside the python-lamindb container, where Postgres is at localhost:5432.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import quote

# The instance, its dedicated database, and its storage root under LAMIN_HOME.
# The database is never POSTGRES_DB, which on a misconfigured VM is the production lamindb.
INSTANCE = "clinicalomxdb-local-review"
DATABASE = "clinicalomxdb_local_review"
STORAGE = os.path.join(os.path.expanduser("~"), ".lamin", "local-review-storage")

# lamindb_setup skips its hub request when the owner is this literal.
OWNER = "anonymous"

# The init runs in a child process: lamindb_setup reads LAMIN_SETTINGS_DIR once, at import.
INIT_PHASE = "--init-in-temporary-settings-dir"

# Needed by consumer vocabularies, as in `make schema-apply WITH_CONSUMERS=1`.
MODULES = "bionty"


def ensure_database() -> None:
    """Create the review database if missing. psycopg2 quotes the name; it is never interpolated."""
    import psycopg2
    from psycopg2 import sql

    conn = psycopg2.connect(
        host="localhost",
        port=5432,
        dbname="postgres",
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )
    # CREATE DATABASE cannot run inside a transaction block.
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE,))
            if cur.fetchone() is None:
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE)))
                print(f"created database {DATABASE}", file=sys.stderr)
    finally:
        conn.close()


def dsn() -> str:
    """The connection URL, with user and password percent-encoded."""
    user = quote(os.environ["POSTGRES_USER"], safe="")
    password = quote(os.environ["POSTGRES_PASSWORD"], safe="")
    return f"postgresql://{user}:{password}@localhost:5432/{DATABASE}"


def settings_filenames() -> tuple[str, str, str]:
    """The three settings filenames the init leaves behind, as this lamindb names them."""
    from lamindb_setup.core._settings_store import (
        current_instance_settings_file,
        current_user_settings_file,
        instance_settings_file,
    )

    return (
        instance_settings_file(INSTANCE, OWNER).name,
        current_instance_settings_file().name,
        current_user_settings_file().name,
    )


def names_to_publish(shared: pathlib.Path, names: tuple[str, str, str]) -> tuple[str, ...]:
    """Which of the init's three files to copy out.

    The instance pair always. current_user.env only when the shared directory has none, so a
    developer's own login is never replaced.
    """
    instance_env, current_env, user_env = names
    if (shared / user_env).exists():
        return (instance_env, current_env)
    return (instance_env, current_env, user_env)


def child_environment(scratch: pathlib.Path) -> dict[str, str]:
    """The init child's environment, with LAMIN_SETTINGS_DIR at the temporary directory."""
    return {**os.environ, "LAMIN_SETTINGS_DIR": str(scratch)}


def publish_settings(temporary: pathlib.Path, shared: pathlib.Path, names) -> None:
    """Copy the init's settings files into the shared directory every container reads.

    Every source is checked and staged beside its destination before any is moved into place, so
    a missing source or failed copy leaves the shared directory unchanged.
    """
    missing = [name for name in names if not (temporary / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f"the init left no {', '.join(missing)} in {temporary}, so {shared} was left alone")

    shared.mkdir(parents=True, exist_ok=True)
    incoming = [shared / f".{name}.incoming" for name in names]
    try:
        for name, staged in zip(names, incoming):
            shutil.copy(temporary / name, staged)
        # A failure between these renames publishes some files without the rest.
        for name, staged in zip(names, incoming):
            os.replace(staged, shared / name)
    finally:
        for staged in incoming:
            staged.unlink(missing_ok=True)


def init_in_temporary_settings_dir() -> None:
    """The child phase: init the instance as the anonymous user. A repeat run reconnects."""
    os.environ.pop("LAMIN_API_KEY", None)

    import lamindb_setup

    lamindb_setup.init(storage=STORAGE, name=INSTANCE, db=dsn(), modules=MODULES)


def main() -> int:
    # Before importing lamindb.
    os.environ.pop("LAMIN_API_KEY", None)

    ensure_database()
    os.makedirs(STORAGE, exist_ok=True)

    names = settings_filenames()
    instance_env = names[0]
    shared = pathlib.Path(os.path.expanduser("~")) / ".lamin"
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="lamin-local-"))
    try:
        temporary = scratch / ".lamin"
        temporary.mkdir()
        # A repeat run resolves the instance from the settings file the last run wrote.
        if (shared / instance_env).exists():
            shutil.copy(shared / instance_env, temporary / instance_env)

        subprocess.run(
            [sys.executable, os.path.abspath(__file__), INIT_PHASE],
            env=child_environment(scratch),
            check=True,
        )

        publish_settings(temporary, shared, names_to_publish(shared, names))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    print(f"connected {OWNER}/{INSTANCE}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == INIT_PHASE:
        init_in_temporary_settings_dir()
        sys.exit(0)
    sys.exit(main())
