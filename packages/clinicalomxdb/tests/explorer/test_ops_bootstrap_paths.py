"""The path each portal widget bootstrap writes into the unit it renders.

Runs each bootstrap against a throwaway release layout (releases/platform-v9 plus a current
symlink), with stubs for systemctl, curl, ssh, sudo, getent and others on PATH, and SYSTEMD_DIR
in a temp directory. Nothing outside it is written.

A unit rendered through current/ must name current/, not the release path. A second run
replaces its unit, and a widget already running the old tree is reported.

The three socket-served widgets exit 1 here: their last step curls a socket only systemd
creates. The rendered unit and the script's output are what is checked.
"""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent

# Off-container the checkout supplies the files make explorer-test mounts.
PKG = HERE.parent.parent


def source_dir(mount, relative):
    mounted = pathlib.Path(mount)
    return mounted if mounted.is_dir() else PKG / relative


HEALTH_SRC = source_dir("/ops-health", "hub-ops/health")
LOGIN_SRC = source_dir("/ops-login", "hub-ops/login")
TTYD_SRC = source_dir("/ops-ttyd", "hub-ops/ttyd")
REVIEW_SRC = source_dir("/ops-schema-review", "hub-ops/schema-review")
HPC_SRC = source_dir("/ops-hpc", "hub-pipelines/prefect/workers/hpc")

TAG = "platform-v9"

STUBS = {
    # Root, and docker group membership, which the schema review bootstrap requires.
    "id": """#!/bin/sh
case "$1" in
  -u)  echo 0 ;;
  -nG) echo "docker" ;;
  *)   exec /usr/bin/id "$@" ;;
esac
""",
    # Records every call. State files say which unit is up and which tree it runs.
    "systemctl": """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_RECORD"
case "$1 $2" in
  "is-active "*)
      unit="$2"
      if [ -f "$STUB_STATE/active.$unit" ]; then cat "$STUB_STATE/active.$unit"; exit 0; fi
      echo inactive; exit 3 ;;
  "show -p")
      for unit in "$@"; do :; done
      [ -f "$STUB_STATE/exec.$unit" ] && cat "$STUB_STATE/exec.$unit"
      exit 0 ;;
esac
exit 0
""",
    # Answers in the shape health_server.py serves.
    "curl": ('#!/bin/sh\n'
             'printf \'{"status": "ok", "checks": {}}\'\n'),
    "sleep": '#!/bin/sh\nexit 0\n',
    "journalctl": '#!/bin/sh\nexit 0\n',
    # The fake home keeps the HPC bootstrap's ssh config out of the real home.
    "getent": '#!/bin/sh\necho "runner:x:1000:1000::$STUB_HOME:/bin/bash"\n',
    "chown": '#!/bin/sh\nexit 0\n',
    # sudo -u USER <command>: drop the -u and run it as this user.
    "sudo": """#!/bin/sh
[ "$1" = "-u" ] && shift 2
exec "$@"
""",
    "ssh": """#!/bin/sh
case "$1" in
  -G) echo "user $(echo "$2" | sed 's/^hpc-//')" ;;
  -O) echo "Master running" ;;
esac
exit 0
""",
    "ttyd": """#!/bin/sh
case "$1" in
  --version) echo "ttyd version 1.6.3" ;;
  --help)    echo "--base-path --interface --credential" ;;
esac
exit 0
""",
    "make": '#!/bin/sh\nexit 0\n',
    "docker": '#!/bin/sh\nexit 0\n',
}

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"OK   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label}{': ' + detail if detail else ''}")


def build(base):
    """A throwaway release layout: releases/<tag>/packages/clinicalomxdb, plus current -> it."""
    release = base / "releases" / TAG / "packages" / "clinicalomxdb"
    for src, rel in ((HEALTH_SRC, "hub-ops/health"), (LOGIN_SRC, "hub-ops/login"),
                     (TTYD_SRC, "hub-ops/ttyd"), (REVIEW_SRC, "hub-ops/schema-review"),
                     (HPC_SRC, "hub-pipelines/prefect/workers/hpc")):
        dst = release / rel
        dst.mkdir(parents=True)
        for name in ("bootstrap.sh", "bootstrap-users.sh", "hpc-login.sh",
                     "health_server.py", "login_server.py", "review_server.py",
                     "ops-health.service", "ops-login.service", "ops-ttyd.service",
                     "ops-schema-review.service", "hpc-ssh-pool-worker@.service"):
            if (src / name).is_file():
                shutil.copy(src / name, dst / name)
                (dst / name).chmod(0o755)
    # The schema review bootstrap needs a Makefile.
    (release / "Makefile").write_text("schema-review:\n\t@true\n")
    # Written by scripts/site/render.py; untracked.
    (release / "hub-pipelines/prefect/workers/hpc/users.conf").write_text("alice\n")
    (release.parent.parent / "site.env").write_text(
        "CLINICALOMX_SITE_HPC_LOGIN_HOST=hpc-login.example.edu\nCLINICALOMX_SITE_HPC_USER=alice\n")

    (base / "current").symlink_to(pathlib.Path("releases") / TAG)
    for name in ("systemd", "bin", "home", "state"):
        (base / name).mkdir()
    for name, body in STUBS.items():
        stub = base / "bin" / name
        stub.write_text(body)
        stub.chmod(0o755)
    return base / "current" / "packages" / "clinicalomxdb"


def run(base, pkg, script, extra_env=None):
    """Run one bootstrap as make does: cwd at the package root, relative script path."""
    record = base / "systemctl-argv"
    record.write_text("")
    env = dict(
        os.environ,
        PATH=f"{base / 'bin'}:{os.environ['PATH']}",
        SYSTEMD_DIR=str(base / "systemd"),
        STUB_RECORD=str(record),
        STUB_STATE=str(base / "state"),
        STUB_HOME=str(base / "home"),
        HOME=str(base / "home"),
        # As make passes it: bash would otherwise derive $PWD from the resolved getcwd().
        PWD=str(pkg),
    )
    env.update(extra_env or {})
    result = subprocess.run([script], capture_output=True, text=True, env=env, cwd=str(pkg))
    return result, record.read_text().splitlines()


def unit(base, name):
    path = base / "systemd" / name
    return path.read_text() if path.is_file() else ""


print("=" * 70)

base = pathlib.Path(tempfile.mkdtemp(prefix="ops_bootstrap_paths_"))
try:
    pkg = build(base)
    logical = str(base / "current" / "packages" / "clinicalomxdb")
    physical = str(base / "releases" / TAG / "packages" / "clinicalomxdb")

    # --- 1. the health endpoint ---------------------------------------------------------------
    # An empty HEALTH_SOCKET_PATH selects TCP, which the stub curl answers.
    result, calls = run(base, pkg, "hub-ops/health/bootstrap.sh",
                        {"HEALTH_SOCKET_PATH": ""})
    text = unit(base, "ops-health.service")
    check("the health bootstrap succeeds", result.returncode == 0, result.stderr.strip())
    check("and renders a unit naming the path the caller used",
          f"ExecStart=/usr/bin/python3 {logical}/hub-ops/health/health_server.py" in text, text)
    check("and never names the release tree underneath it",
          physical not in text, text)
    check("and still writes the overrides drop-in",
          (base / "systemd" / "ops-health.service.d" / "overrides.conf").is_file())

    # A second run replaces the unit.
    result, calls = run(base, pkg, "hub-ops/health/bootstrap.sh", {"HEALTH_SOCKET_PATH": ""})
    installed = sorted(p.name for p in (base / "systemd").glob("*.service"))
    check("a re-run installs no second unit", installed == ["ops-health.service"], str(installed))
    check("and re-points the running process rather than leaving it",
          any(c.startswith("restart ops-health.service") for c in calls), str(calls))

    # --- 2. the schema review renderer -----------------------------------------------------
    result, calls = run(base, pkg, "hub-ops/schema-review/bootstrap.sh",
                        {"REVIEW_LAMIN_HOME": str(base / "home" / ".lamin")})
    text = unit(base, "ops-schema-review.service")
    check("the schema review bootstrap renders its unit", text != "", result.stderr.strip())
    check("and names the caller's path for both the server and the repo",
          f"ExecStart=/usr/bin/python3 {logical}/hub-ops/schema-review/review_server.py" in text
          and f"Environment=REVIEW_REPO_DIR={logical}" in text, text)
    check("and never names the release tree underneath it", physical not in text, text)
    check("and restarts the unit, so a re-run actually re-points it",
          any(c.startswith("restart ops-schema-review.service") for c in calls), str(calls))

    # --- 3. the login relay, which must not restart itself ---------------------------------
    # It was already up on the old tree. `enable --now` leaves it, so the script must say so.
    (base / "state" / "active.ops-login.service").write_text("active\n")
    (base / "state" / "exec.ops-login.service").write_text(
        "{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 /opt/clinicalomxdb/hub-ops/login/"
        "login_server.py ; ignore_errors=no }\n")
    result, calls = run(base, pkg, "hub-ops/login/bootstrap.sh")
    text = unit(base, "ops-login.service")
    check("the login bootstrap renders its unit", text != "", result.stderr.strip())
    check("and names the caller's path",
          f"ExecStart=/usr/bin/python3 {logical}/hub-ops/login/login_server.py" in text, text)
    check("and never names the release tree underneath it", physical not in text, text)
    check("and reports that the running relay is still on the old tree",
          "NOT LIVE YET" in result.stdout and "/opt/clinicalomxdb" in result.stdout,
          result.stdout.strip())
    check("and does not restart it behind the operator's back",
          not any(c.startswith("restart ops-login.service") for c in calls), str(calls))

    # --- 4. the login terminal, same contract plus the DUO cost ----------------------------
    (base / "state" / "active.ops-ttyd.service").write_text("active\n")
    (base / "state" / "exec.ops-ttyd.service").write_text(
        "{ path=/usr/bin/ttyd ; argv[]=/usr/bin/ttyd --interface /run/ops-ttyd/login.sock "
        "/opt/clinicalomxdb/hub-ops/ttyd/hpc-login.sh ; ignore_errors=no }\n")
    result, calls = run(base, pkg, "hub-ops/ttyd/bootstrap.sh")
    text = unit(base, "ops-ttyd.service")
    check("the ttyd bootstrap renders its unit", text != "", result.stderr.strip())
    check("and names the caller's path for hpc-login.sh",
          f"{logical}/hub-ops/ttyd/hpc-login.sh" in text, text)
    check("and never names the release tree underneath it", physical not in text, text)
    check("and reports the stale terminal with what a restart costs",
          "NOT LIVE YET" in result.stdout and "ControlMaster" in result.stdout,
          result.stdout.strip())
    check("and does not restart it behind the operator's back",
          not any(c.startswith("restart ops-ttyd.service") for c in calls), str(calls))

    # A credential goes to a drop-in, and that copy of the command carries the same path.
    result, calls = run(base, pkg, "hub-ops/ttyd/bootstrap.sh",
                        {"TTYD_CREDENTIAL": "fixture:not-a-real-secret"})
    dropin = base / "systemd" / "ops-ttyd.service.d" / "credential.conf"
    check("a credential drop-in names the caller's path too",
          dropin.is_file() and f"{logical}/hub-ops/ttyd/hpc-login.sh" in dropin.read_text(),
          dropin.read_text() if dropin.is_file() else "no drop-in")

    # --- 5. the per-user HPC workers ----------------------------------------------------------
    (base / "state" / "active.hpc-ssh-pool-worker@alice.service").write_text("active\n")
    (base / "state" / "exec.hpc-ssh-pool-worker@alice.service").write_text(
        "{ path=/opt/clinicalomxdb/hub-pipelines/prefect/.venv/bin/prefect ; ignore_errors=no }\n")
    result, calls = run(base, pkg, "hub-pipelines/prefect/workers/hpc/bootstrap-users.sh")
    text = unit(base, "hpc-ssh-pool-worker@.service")
    prefect = f"{logical}/hub-pipelines/prefect"
    check("the users bootstrap succeeds", result.returncode == 0, result.stderr.strip())
    check("and renders the per-user template at the caller's path",
          f"WorkingDirectory={prefect}" in text
          and f"ExecStart={prefect}/.venv/bin/prefect worker start" in text, text)
    check("and never names the release tree underneath it", physical not in text, text)
    check("and points the worker env file there too, as the tracked render does",
          f"EnvironmentFile={prefect}/worker.env" in text, text)
    check("and the site values beside it, from the release root's site.env",
          f"EnvironmentFile={base}/current/site.env" in text, text)
    check("and reports the instance still running the retired tree",
          "RESTART NEEDED" in result.stdout and "alice" in result.stdout,
          result.stdout.strip())
    check("and does not restart a worker without a drain",
          not any(c.startswith("restart hpc-ssh-pool-worker") for c in calls), str(calls))
    check("and writes the ssh config into the home getent named, nowhere else",
          (base / "home" / ".ssh" / "config").is_file())
    check("and that config carries the managed block for the user",
          "Host hpc hpc-alice" in (base / "home" / ".ssh" / "config").read_text())

    # --- 6. invoked at the release path -------------------------------------------------------
    # Renders what it was given and warns the unit will not survive the next symlink flip.
    result, calls = run(
        base, base / "releases" / TAG / "packages" / "clinicalomxdb",
        str(base / "releases" / TAG / "packages" / "clinicalomxdb" / "hub-ops" / "health"
            / "bootstrap.sh"),
        {"HEALTH_SOCKET_PATH": ""})
    text = unit(base, "ops-health.service")
    check("a release-path invocation still succeeds", result.returncode == 0,
          result.stderr.strip())
    check("and warns that the unit cannot survive the next current/ flip",
          "WARNING" in result.stderr and "current/" in result.stderr, result.stderr.strip())
    check("and renders exactly the path it was given",
          f"ExecStart=/usr/bin/python3 {physical}/hub-ops/health/health_server.py" in text, text)
finally:
    shutil.rmtree(base, ignore_errors=True)

print("=" * 70)
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
