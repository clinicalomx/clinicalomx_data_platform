"""A disposable lamin instance on a throwaway postgres:18, and connections onto it.

Used by `tests/conftest.py` and `scripts/dev/curation_demo.py`. Postgres only.
Call `start()` before anything imports lamindb; it refuses otherwise. HOME and
LAMIN_SETTINGS_DIR point at a fresh temporary directory, so no configured instance is reachable.
"""

from __future__ import annotations

import atexit
import os
import pathlib
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

#: Roles migration 0003 creates, NOLOGIN and without a password.
ROLES = (
    "clinicalomx_ledger_agent",
    "clinicalomx_ledger_curator",
    "clinicalomx_ledger_projector",
)


@dataclass(frozen=True)
class Disposable:
    db_url: str
    container_id: str
    workdir: pathlib.Path


_started: Disposable | None = None


def start() -> Disposable:
    """Stand up the container and the instance, once per process."""
    global _started
    if _started is not None:
        return _started

    if "lamindb" in sys.modules:
        raise RuntimeError(
            "lamindb was imported before the disposable instance was started, so it is "
            "already connected to whatever this machine has configured. Nothing here may "
            "touch a configured instance; start() has to run first"
        )

    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer("postgres:18")
    container.start()
    atexit.register(container.stop)

    db_url = container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="ledger-disposable-"))
    (workdir / "settings").mkdir()
    (workdir / "home").mkdir()
    os.environ["LAMIN_SETTINGS_DIR"] = str(workdir / "settings")
    os.environ["HOME"] = str(workdir / "home")

    lamin = pathlib.Path(sys.executable).parent / "lamin"
    _run([str(lamin), "init", "--storage", "./store", "--db", db_url,
          "--modules", "clinicalomx_ledger"], workdir)
    _run([str(lamin), "migrate", "deploy"], workdir)

    _started = Disposable(db_url=db_url, container_id=container.get_wrapped_container().id,
                          workdir=workdir)
    return _started


def _run(cmd: list[str], cwd: pathlib.Path) -> None:
    """Run ``cmd``; print its output only on failure."""
    done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(
            f"{' '.join(cmd[-3:])} failed with {done.returncode}\n{done.stdout}\n{done.stderr}"
        )


def quiet_lamindb_import():
    """Context manager that swallows lamindb's connection banner on import."""
    import contextlib
    import io

    stack = contextlib.ExitStack()
    sink = io.StringIO()
    stack.enter_context(contextlib.redirect_stdout(sink))
    stack.enter_context(contextlib.redirect_stderr(sink))
    return stack


def verify_isolated() -> None:
    """Refuse to continue unless Django is connected to the container `start()` made."""
    from django.conf import settings

    live = settings.DATABASES["default"]
    wanted = urlsplit(start().db_url)
    if (live.get("HOST"), str(live.get("PORT")), live.get("NAME")) != (
        wanted.hostname, str(wanted.port), wanted.path.lstrip("/")
    ):
        raise RuntimeError(
            f"connected to {live.get('HOST')}:{live.get('PORT')}/{live.get('NAME')}, which is "
            "not the throwaway container. Refusing to run against anything else"
        )


def superuser(dbname: str | None = None):
    """A psycopg2 connection as the container's superuser, for the raw-SQL evidence."""
    import psycopg2

    db_url = start().db_url
    if dbname is None:
        return psycopg2.connect(db_url)
    parts = urlsplit(db_url)
    return psycopg2.connect(
        urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))
    )


def activate_role_logins() -> None:
    """Grant LOGIN and a throwaway password to the NOLOGIN roles. Container-only values."""
    with superuser() as conn:
        with conn.cursor() as cur:
            for role in ROLES:
                cur.execute(f"ALTER ROLE {role} LOGIN PASSWORD '{role}'")
        conn.commit()


def as_role(role: str):
    import psycopg2

    parts = urlsplit(start().db_url)
    host = parts.hostname or "localhost"
    port = f":{parts.port}" if parts.port else ""
    return psycopg2.connect(
        urlunsplit((parts.scheme, f"{role}:{role}@{host}{port}", parts.path,
                    parts.query, parts.fragment))
    )


def table(model) -> str:
    return model._meta.db_table
