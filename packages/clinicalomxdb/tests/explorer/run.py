#!/usr/bin/env python3
"""Run the Explore tab's and schema review's tests, each file in its own process.

lamindb connects one instance per interpreter, and each test builds its own SQLite instance
under /tmp. Run with `make explorer-test`, which uses the service's pinned lamindb.

These files import no lamindb: test_schema_review.py, test_lamin_home_guard.py,
test_schema_write_guard.py, test_schema_review_server.py, test_ops_reload_signal.py,
test_pg_backup_env_path.py, test_ops_bootstrap_paths.py and test_alert_rules.py. The two
test_lamin_local_* files import lamindb_setup but need no instance.

Write a test as a plain script that runs at module level and exits non-zero on failure: no main,
no __main__ guard, no pytest import.
"""
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
TESTS = sorted(p.name for p in HERE.glob("test_*.py"))

failed = []
for name in TESTS:
    print(f"\n{'=' * 70}\n{name}\n{'=' * 70}", flush=True)
    result = subprocess.run([sys.executable, str(HERE / name)], cwd="/tmp")
    if result.returncode != 0:
        failed.append(name)

print(f"\n{'=' * 70}")
print("FAILED:", ", ".join(failed) if failed else "nothing")
sys.exit(1 if failed else 0)
