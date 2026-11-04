"""Session setup: a throwaway postgres:18 and a disposable lamin instance with the ledger
module, connected at import time. Synthetic data only; no live instance is touched."""

from __future__ import annotations

import atexit
import os
import pathlib
import subprocess
import sys
import tempfile

from testcontainers.postgres import PostgresContainer

_pg = PostgresContainer("postgres:18")
_pg.start()
atexit.register(_pg.stop)

_work = pathlib.Path(tempfile.mkdtemp(prefix="omop-tests-"))
(_work / "settings").mkdir()
(_work / "home").mkdir()
os.environ["LAMIN_SETTINGS_DIR"] = str(_work / "settings")
os.environ["HOME"] = str(_work / "home")

_lamin = pathlib.Path(sys.executable).parent / "lamin"
_db = _pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
subprocess.run([str(_lamin), "init", "--storage", "./store", "--db", _db,
                "--modules", "clinicalomx_ledger"], cwd=_work, check=True)
subprocess.run([str(_lamin), "migrate", "deploy"], cwd=_work, check=True)

import lamindb as ln  # noqa: E402  (connects to the throwaway instance)
from clinicalomx.names import FEATURES, TYPES  # noqa: E402

# The foundation vocabulary this package needs: the ROI types and the `patient` feature.
_patient = ln.Record(name=TYPES.PATIENT, is_type=True).save()
_roi = ln.Record(name=TYPES.ROI, is_type=True).save()
for _name in (TYPES.CORE, TYPES.TMA, TYPES.WHOLE_SLIDE, TYPES.TILE):
    ln.Record(name=_name, is_type=True, type=_roi).save()
ln.Feature(name=FEATURES.PATIENT, dtype=_patient).save()
