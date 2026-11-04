"""The signal reload's age gate, run as the real Makefile macro with a stub docker.

A container that only just started is not sent HUP: ops-up runs compose up -d then
ops-reload, and an early HUP kills the process. Long-running containers are signalled as
before, a rejected reload is an error, a stopped container takes the existing path, and an
unreadable age signals.
"""

import datetime as dt
import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent

# Off-container the checkout is two directories up.
ROOT = pathlib.Path("/workspace")
if not (ROOT / "Makefile").is_file():
    ROOT = HERE.parent.parent

CONTAINER = "clinicalomxdb_alloy"
REJECTION = "failed to reload config"

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"OK   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label}{': ' + detail if detail else ''}")


DOCKER_STUB = """#!/bin/sh
case "$1 $2" in
"ps --format")
    [ -n "$STUB_RUNNING" ] && echo "$STUB_NAME"
    ;;
"inspect -f")
    [ -z "$STUB_STARTED_AT" ] && exit 1
    echo "$STUB_STARTED_AT"
    ;;
"kill -s")
    echo "$3 $4" >> "$STUB_RECORD"
    ;;
"logs --since")
    printf '%s' "$STUB_LOG"
    ;;
*)
    echo "stub docker: unexpected $*" >&2
    exit 99
    ;;
esac
"""

HARNESS = """include {root}/Makefile

reload-under-test:
\t@$(call reload_signal_or_fail,{container},alloy,{rejection},recreate it)
"""


def run(age_seconds, running=True, log=""):
    """Run the real macro with a stub docker. Returns (completed process, HUP lines)."""
    base = pathlib.Path(tempfile.mkdtemp(prefix="reload_signal_"))
    bin_dir = base / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(DOCKER_STUB)
    stub.chmod(0o755)

    makefile = base / "Makefile"
    makefile.write_text(
        HARNESS.format(root=ROOT, container=CONTAINER, rejection=REJECTION))

    if age_seconds is None:
        started_at = ""
    else:
        started = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age_seconds)
        started_at = started.strftime("%Y-%m-%dT%H:%M:%S.%f000Z")

    record = base / "hup"
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        STUB_NAME=CONTAINER,
        STUB_RUNNING="1" if running else "",
        STUB_STARTED_AT=started_at,
        STUB_RECORD=str(record),
        STUB_LOG=log,
    )
    result = subprocess.run(
        ["make", "-f", str(makefile), "reload-under-test"],
        capture_output=True, text=True, env=env, cwd=str(base),
    )
    hups = record.read_text().splitlines() if record.is_file() else []
    return result, hups


print("=" * 70)

# A just-recreated container already runs the config on disk, so it is not signalled.
result, hups = run(2)
check("a container two seconds old is not signalled", hups == [], f"sent {hups}")
check("and says why", "already on the current config" in result.stdout, result.stdout)
check("and succeeds", result.returncode == 0, result.stderr)

result, hups = run(14)
check("one second under the threshold is not signalled", hups == [], f"sent {hups}")

# A long-running container is signalled.
result, hups = run(3600)
check("a long-running container is still signalled",
      hups == [f"HUP {CONTAINER}"], f"sent {hups}")
check("and still reports the reload", "alloy config reloaded" in result.stdout, result.stdout)
check("and succeeds", result.returncode == 0, result.stderr)

result, hups = run(16)
check("one second over the threshold is signalled",
      hups == [f"HUP {CONTAINER}"], f"sent {hups}")

# A rejected reload is an error naming the old config as running.
result, hups = run(3600, log=f"level=error msg=\"{REJECTION}\" err=parse\n")
check("a rejected reload is still an error", result.returncode != 0, result.stdout)
check("and still names the old config",
      "still running the OLD config" in result.stdout, result.stdout)
check("and still prints the hint", "HINT: recreate it" in result.stdout, result.stdout)

# A stopped container.
result, hups = run(3600, running=False)
check("a container that is not running is unchanged",
      result.returncode == 0 and "not running, nothing to reload" in result.stdout,
      result.stdout)
check("and is not signalled", hups == [], f"sent {hups}")

# An unreadable age signals.
result, hups = run(None)
check("an unreadable age falls back to signalling",
      hups == [f"HUP {CONTAINER}"], f"sent {hups}")

print("=" * 70)
print("FAILED:", ", ".join(failures) if failures else "nothing")
sys.exit(1 if failures else 0)
