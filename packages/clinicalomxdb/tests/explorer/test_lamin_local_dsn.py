"""lamin_local.dsn(), checked through the code that consumes it.

dj_database_url.parse, which lamindb_setup uses, must give back the original credentials, and
check_is_instance_remote must still answer local, so make lamin-local never registers on
LaminHub.
"""

import importlib.util
import os
import pathlib
import sys
import tempfile

import dj_database_url
from lamindb_setup.core._settings_instance import check_is_instance_remote

HERE = pathlib.Path(__file__).resolve().parent

# Off-container the checkout supplies what make explorer-test mounts at /scripts.
SCRIPTS = pathlib.Path("/scripts")
if not SCRIPTS.is_dir():
    SCRIPTS = HERE.parent.parent / "hub-database" / "scripts"

spec = importlib.util.spec_from_file_location("lamin_local", SCRIPTS / "lamin_local.py")
lamin_local = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lamin_local)

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"OK   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label}{': ' + detail if detail else ''}")


def credentials(user, password):
    os.environ["POSTGRES_USER"] = user
    os.environ["POSTGRES_PASSWORD"] = password
    return dj_database_url.parse(lamin_local.dsn())


print("=" * 70)

# Every character here is a URL delimiter.
NASTY_USER = "us:er@x"
NASTY_PASSWORD = "p@ss:w/rd%1?#[]"

cfg = credentials(NASTY_USER, NASTY_PASSWORD)
check("a user holding URL delimiters reaches the consumer intact",
      cfg["USER"] == NASTY_USER, f"got {cfg['USER']!r}, wanted {NASTY_USER!r}")
check("a password holding URL delimiters reaches the consumer intact",
      cfg["PASSWORD"] == NASTY_PASSWORD, f"got {cfg['PASSWORD']!r}, wanted {NASTY_PASSWORD!r}")

# The credential must not change the host.
check("the host is still localhost", cfg["HOST"] == "localhost", repr(cfg["HOST"]))
check("the port is still 5432", str(cfg["PORT"]) == "5432", repr(cfg["PORT"]))
check("the database is still the dedicated review one",
      cfg["NAME"] == lamin_local.DATABASE, f"got {cfg['NAME']!r}, wanted {lamin_local.DATABASE!r}")

# An ordinary credential is unchanged.
plain = credentials("clinicalomx", "s3cret")
check("an ordinary credential is unchanged by the round trip",
      (plain["USER"], plain["PASSWORD"]) == ("clinicalomx", "s3cret"),
      f"{plain['USER']!r} {plain['PASSWORD']!r}")

# Without quoting, the parse raises or returns the wrong credential and host.
naive_url = f"postgresql://{NASTY_USER}:{NASTY_PASSWORD}@localhost:5432/{lamin_local.DATABASE}"
try:
    naive = dj_database_url.parse(naive_url)
    naive_broken = (naive["USER"], naive["PASSWORD"], naive["HOST"]) != (
        NASTY_USER, NASTY_PASSWORD, "localhost")
    naive_detail = f"{naive['USER']!r} {naive['PASSWORD']!r} {naive['HOST']!r}"
except ValueError as exc:
    naive_broken = True
    naive_detail = f"raised {exc}"
check("an unquoted credential does NOT survive the same parse", naive_broken, naive_detail)

# The URL must read as local, so init never registers on LaminHub.
with tempfile.TemporaryDirectory(prefix="lamin_local_dsn_") as storage:
    os.environ["POSTGRES_USER"] = NASTY_USER
    os.environ["POSTGRES_PASSWORD"] = NASTY_PASSWORD
    check("a local storage root and this URL are judged local, so nothing registers on the hub",
          check_is_instance_remote(storage, lamin_local.dsn()) is False)

print("=" * 70)
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
