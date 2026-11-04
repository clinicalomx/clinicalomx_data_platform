#!/usr/bin/env python3
"""Fail if the health fixtures' key shape differs from what the real health endpoint emits.

Runs hub-ops/health/health_server.py and compares key paths, not values, against every health
fixture.

Run: make ui-fixtures-check
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
HEALTH = HERE.parent / "health" / "health_server.py"
PORT = int(os.environ.get("UI_FIXTURE_PORT", "18099"))

# Keys present only in some states. Every other key must appear in both.
OPTIONAL = {
    "checks.connections.detail[].hint",
    "checks.connections.detail[].error",
    "checks.connections.detail[].established_age_hours",
    "checks.connections.detail[].control_persist_idle_timeout_hours",
    "checks.connections.detail[].pool_status",
    # Present only when a unit has a problem.
    "checks.systemd_units.detail[].hint",
    "checks.systemd_units.detail[].error",
    "checks.systemd_units.detail[].note",
    # Present only for a loaded, active unit.
    "checks.systemd_units.detail[].active_for_seconds",
    "checks.systemd_units.detail[].restarts_in_window",
    "checks.systemd_units.detail[].window_seconds",
    "checks.pg_backup.detail.sentinel",
    "checks.pg_backup.detail.newest",
    "checks.pg_backup.detail.age_hours",
    "checks.pg_backup.detail.size_mb",
    "checks.pg_backup.detail.daily_dumps",
    "checks.prefect.detail[].paused",
    "checks.lamin.detail.id",
    "checks.lamin.detail.schema",
}


def shape(value: Any, prefix: str = "") -> set[str]:
    """The set of key paths in a payload. Lists collapse to one representative element."""
    out: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            path = f"{prefix}.{k}" if prefix else k
            out.add(path)
            out |= shape(v, path)
    elif isinstance(value, list) and value:
        for item in value:
            out |= shape(item, prefix + "[]")
    return out


def real_payload() -> dict[str, Any]:
    """Run the real endpoint against a throwaway socket directory and capture one response."""
    with tempfile.TemporaryDirectory() as tmp:
        # A live-looking socket, so the connections check returns a populated row.
        keep = socket.socket(socket.AF_UNIX)
        keep.bind(str(Path(tmp) / "cm-alice@hpc-login.example.edu:22"))
        env = {
            **os.environ,
            "HEALTH_HPC_HOST": "hpc-login.example.edu",
            "HEALTH_SOCKET_PATH": "",
            "HEALTH_BIND_PORT": str(PORT),
            "HEALTH_CACHE_SECONDS": "1",
            "HEALTH_CONTROL_PATH_TEMPLATE": f"{tmp}/cm-%r@%h:%p",
        }
        proc = subprocess.Popen(
            [sys.executable, str(HEALTH)], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(40):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as r:
                        return json.loads(r.read())
                except (urllib.error.URLError, OSError):
                    time.sleep(0.25)
            raise SystemExit("[fixtures] the real health endpoint did not start")
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            keep.close()


def main() -> None:
    real = real_payload()
    failures = 0
    skipped_forms: set[str] = set()

    for path in sorted(FIXTURES.glob("health-*.json")):
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict):
            continue  # health-offline.json is a sentinel, not a payload
        if "hpc_sockets" in payload.get("checks", {}):
            print(f"  {path.name}: skipped (deliberately models the OLD payload)")
            continue

        problems: list[str] = []

        # Everything outside checks must match exactly.
        env_real = {k for k in shape(real) if not k.startswith("checks.")}
        env_got = {k for k in shape(payload) if not k.startswith("checks.")}
        problems += [f"missing from fixture: {k}" for k in sorted(env_real - env_got)]
        problems += [f"not emitted by the endpoint: {k}" for k in sorted(env_got - env_real)]

        # A check's `detail` can be a list or a dict depending on state. Mismatched forms are
        # reported as uncompared, not passed.
        for name, real_check in real.get("checks", {}).items():
            got_check = payload.get("checks", {}).get(name)
            if got_check is None:
                problems.append(f"check missing entirely: {name}")
                continue
            real_detail, got_detail = real_check.get("detail"), got_check.get("detail")
            if type(real_detail) is not type(got_detail):
                skipped_forms.add(name)
                continue
            # An empty list has nothing to compare.
            if isinstance(got_detail, list) and (not got_detail or not real_detail):
                r = {k for k in shape(real_check, f"checks.{name}") if "[]" not in k}
                g = {k for k in shape(got_check, f"checks.{name}") if "[]" not in k}
                problems += [f"missing from fixture: {k}" for k in sorted(r - g) if k not in OPTIONAL]
                problems += [f"not emitted by the endpoint: {k}" for k in sorted(g - r) if k not in OPTIONAL]
                continue
            r = shape(real_check, f"checks.{name}")
            g = shape(got_check, f"checks.{name}")
            problems += [f"missing from fixture: {k}" for k in sorted(r - g) if k not in OPTIONAL]
            problems += [f"not emitted by the endpoint: {k}" for k in sorted(g - r) if k not in OPTIONAL]

        if problems:
            failures += 1
            print(f"  {path.name}: DRIFT")
            for p in problems:
                print(f"      {p}")
        else:
            print(f"  {path.name}: ok")

    if skipped_forms:
        print(
            f"\n[fixtures] not compared, because the capture exercised a different form: "
            f"{', '.join(sorted(skipped_forms))}."
        )
        print("[fixtures] To cover those, run the check with the real service reachable.")

    if failures:
        print(f"\n[fixtures] {failures} fixture(s) have drifted from the real payload shape.")
        print("[fixtures] Update them, or add the key to OPTIONAL if it is genuinely state-dependent.")
        raise SystemExit(1)
    print("\n[fixtures] all comparable fixtures match the real payload shape")


if __name__ == "__main__":
    main()
