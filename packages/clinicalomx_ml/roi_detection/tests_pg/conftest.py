"""A disposable lamin instance on a throwaway postgres:18, started at conftest import.

HOME and LAMIN_SETTINGS_DIR point at a temp dir before `lamin init`, so no configured instance
is reachable. Needs docker. Run from the package root:

    uv pip install --python .venv/bin/python pytest "testcontainers[postgres]"
    .venv/bin/python -m pytest roi_detection/tests_pg -q
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

# Ryuk's docker-socket mount fails on Docker Desktop for macOS; atexit stops the container.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

from testcontainers.postgres import PostgresContainer

_pg = PostgresContainer("postgres:18")
_pg.start()
atexit.register(_pg.stop)

DB_URL = _pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
WORK = pathlib.Path(tempfile.mkdtemp(prefix="croi-recovery-tests-"))
(WORK / "settings").mkdir()
(WORK / "home").mkdir()
os.environ["LAMIN_SETTINGS_DIR"] = str(WORK / "settings")
os.environ["HOME"] = str(WORK / "home")

_lamin = pathlib.Path(sys.executable).parent / "lamin"
for cmd in ([str(_lamin), "init", "--storage", "./store", "--db", DB_URL],
            [str(_lamin), "migrate", "deploy"]):
    done = subprocess.run(cmd, cwd=WORK, capture_output=True, text=True, check=False)
    if done.returncode:
        raise RuntimeError(f"{cmd[1]} failed\n{done.stdout}\n{done.stderr}")

import lamindb as ln

STORE = pathlib.Path(ln.setup.settings.storage.root_as_str)
INSTANCE = ln.setup.settings.instance.slug

# Point the objective manifest's instance and storage root here.
os.environ["CROI_INSTANCE"] = INSTANCE
os.environ["CROI_STORAGE__STORAGE_ROOT"] = str(STORE)
