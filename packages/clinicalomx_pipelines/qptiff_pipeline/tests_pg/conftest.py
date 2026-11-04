"""A disposable lamin instance on a throwaway postgres:18, started at conftest import.

HOME and LAMIN_SETTINGS_DIR are redirected before `lamin init`, so nothing here can reach a
configured instance. lamindb is imported only afterwards. Run from the package root:

    uv pip install --python .venv/bin/python "testcontainers[postgres]"
    .venv/bin/python -m pytest qptiff_pipeline/tests_pg -q
"""
from __future__ import annotations

import atexit
import os
import pathlib
import subprocess
import sys
import tempfile

if "lamindb" in sys.modules:
    raise RuntimeError("lamindb was imported before the disposable instance was started")

# Ryuk does not work with Docker Desktop on macOS; atexit stops the container instead.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

from testcontainers.postgres import PostgresContainer

_pg = PostgresContainer("postgres:18")
_pg.start()
atexit.register(_pg.stop)

WORK = pathlib.Path(tempfile.mkdtemp(prefix="reset-all-roi-tests-"))
for _d in ("settings", "home", "prefect"):
    (WORK / _d).mkdir()
os.environ["LAMIN_SETTINGS_DIR"] = str(WORK / "settings")
os.environ["HOME"] = str(WORK / "home")
os.environ["PREFECT_HOME"] = str(WORK / "prefect")

_lamin = pathlib.Path(sys.executable).parent / "lamin"
_db = _pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
for _cmd in ([str(_lamin), "init", "--storage", "./store", "--db", _db],
             [str(_lamin), "migrate", "deploy"]):
    _done = subprocess.run(_cmd, cwd=WORK, capture_output=True, text=True, check=False)
    if _done.returncode:
        raise RuntimeError(f"{_cmd[1]} failed\n{_done.stdout}\n{_done.stderr}")

import lamindb  # noqa: F401  (connects to the throwaway instance)
