r"""Stage 2 parity driver: disposable postgres:18 via testcontainers, disposable
lamin settings dir, then pg_probe.py in a child interpreter. Never touches any
live or operator instance.

Run from the repository root, with Docker running:

    uv venv --python 3.12 /tmp/pgparity-venv
    uv pip install --python /tmp/pgparity-venv/bin/python \
      "lamindb==2.8.1" "testcontainers[postgres]" psycopg2-binary ./packages/clinicalomx
    /tmp/pgparity-venv/bin/python tools/parity/pg_driver.py

Exit 0 means every required check passed.
"""
import os
import pathlib
import subprocess
import sys
import tempfile

from testcontainers.postgres import PostgresContainer

HERE = pathlib.Path(__file__).resolve().parent
VENV_BIN = pathlib.Path(sys.executable).parent

with PostgresContainer("postgres:18") as pg:
    url = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    work = pathlib.Path(tempfile.mkdtemp(prefix="pgparity-"))
    settings = work / "settings"
    settings.mkdir()
    env = dict(os.environ)
    env["LAMIN_SETTINGS_DIR"] = str(settings)
    env["HOME"] = str(work / "home")
    (work / "home").mkdir()
    init = subprocess.run(
        [str(VENV_BIN / "lamin"), "init", "--storage", "./lab", "--db", url],
        cwd=work, env=env, capture_output=True, text=True,
    )
    print(init.stdout[-500:], init.stderr[-500:])
    if init.returncode != 0:
        sys.exit(f"lamin init failed: {init.returncode}")
    probe = subprocess.run([sys.executable, str(HERE / "pg_probe.py")], cwd=work, env=env)
    sys.exit(probe.returncode)
