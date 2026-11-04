"""pg-backup.sh's env-file resolution, run as the real script.

Copies the script into a temp tree beside a fixture .env, with a stub docker that records its
argv. The dump role comes from the .env of the release holding the script; a missing env file
is a refusal naming the path.
"""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent

# Off-container the checkout supplies what make explorer-test mounts at /scripts.
SCRIPTS = pathlib.Path("/scripts")
if not SCRIPTS.is_dir():
    SCRIPTS = HERE.parent.parent / "hub-database" / "scripts"

DOCKER_STUB = """#!/bin/sh
echo "$*" >> "$STUB_RECORD"
printf 'fake dump bytes\\n'
"""

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"OK   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label}{': ' + detail if detail else ''}")


def stack(base, name, role):
    """A throwaway copy of the package shape: <name>/.env beside <name>/scripts/."""
    root = base / name
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPTS / "pg-backup.sh", root / "scripts" / "pg-backup.sh")
    if role is not None:
        (root / ".env").write_text(f"POSTGRES_DB=fixture_db\nPOSTGRES_USER={role}\n")
    return root


def run(base, script, env_file=None):
    """Run the real script through a stub docker. Returns (process, docker argv lines)."""
    backups = base / "backups"
    record = base / "docker-argv"
    # Seed one weekly copy: the retention step's `ls weekly-*.sql.gz` fails under pipefail
    # when there is none.
    backups.mkdir(exist_ok=True)
    (backups / "weekly-19700101-000000.sql.gz").touch()
    env = dict(
        os.environ,
        PATH=f"{base / 'bin'}:{os.environ['PATH']}",
        BACKUP_DIR=str(backups),
        STUB_RECORD=str(record),
    )
    env.pop("ENV_FILE", None)
    if env_file is not None:
        env["ENV_FILE"] = str(env_file)
    result = subprocess.run(
        [str(script)], capture_output=True, text=True, env=env, cwd=str(base))
    argv = record.read_text().splitlines() if record.is_file() else []
    return result, argv, backups


def dumped_role(argv):
    """The role pg_dumpall was given, or None if it was never called."""
    for line in argv:
        parts = line.split()
        if "pg_dumpall" in parts and "-U" in parts:
            return parts[parts.index("-U") + 1]
    return None


print("=" * 70)

base = pathlib.Path(tempfile.mkdtemp(prefix="pg_backup_env_"))
try:
    bin_dir = base / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(DOCKER_STUB)
    stub.chmod(0o755)

    # 1. Invoked directly: the env beside the script's own directory is the one read.
    hub = stack(base, "hub", "hub_role")
    result, argv, backups = run(base, hub / "scripts" / "pg-backup.sh")
    check("a direct invocation succeeds", result.returncode == 0, result.stderr.strip())
    check("and dumps with the role from its own stack's env",
          dumped_role(argv) == "hub_role", f"argv={argv}")
    check("and the dump command is unchanged",
          argv == ["exec postgres pg_dumpall -U hub_role"], f"argv={argv}")
    check("and it writes one daily dump into BACKUP_DIR",
          len(list(backups.glob("daily-*.sql.gz"))) == 1,
          str(sorted(p.name for p in backups.glob("*"))))

    # 2. Through a symlink to the script: the real location wins over a decoy env beside it.
    (base / "decoy").mkdir()
    (base / ".env").write_text("POSTGRES_USER=decoy_role\n")
    link = base / "decoy" / "pg-backup.sh"
    link.symlink_to(hub / "scripts" / "pg-backup.sh")
    shutil.rmtree(base / "backups")
    (base / "docker-argv").unlink()
    result, argv, backups = run(base, link)
    check("a symlinked invocation succeeds", result.returncode == 0, result.stderr.strip())
    check("and reads the real script's env, not the symlink's neighbour",
          dumped_role(argv) == "hub_role", f"argv={argv}")

    # 3. The deployed shape: a release directory reached through the `current` symlink.
    release = stack(base, "releases-v9", "release_role")
    current = base / "current"
    current.symlink_to(release)
    shutil.rmtree(base / "backups")
    (base / "docker-argv").unlink()
    result, argv, backups = run(base, current / "scripts" / "pg-backup.sh")
    check("an invocation through a release symlink succeeds",
          result.returncode == 0, result.stderr.strip())
    check("and reads that release's env",
          dumped_role(argv) == "release_role", f"argv={argv}")

    # 4. An explicit ENV_FILE still wins, so the operator can point it elsewhere.
    elsewhere = base / "elsewhere.env"
    elsewhere.write_text("POSTGRES_USER=override_role\n")
    shutil.rmtree(base / "backups")
    (base / "docker-argv").unlink()
    result, argv, backups = run(base, hub / "scripts" / "pg-backup.sh", env_file=elsewhere)
    check("an ENV_FILE override succeeds", result.returncode == 0, result.stderr.strip())
    check("and overrides the sibling env",
          dumped_role(argv) == "override_role", f"argv={argv}")

    # 5. No env file: a refusal naming the path, never a fallback role.
    bare = stack(base, "bare", None)
    shutil.rmtree(base / "backups")
    (base / "docker-argv").unlink()
    result, argv, backups = run(base, bare / "scripts" / "pg-backup.sh")
    check("a missing env file fails", result.returncode != 0, result.stdout.strip())
    check("and names the exact path it looked for",
          str((bare / ".env").resolve()) in result.stderr, result.stderr.strip())
    check("and dumps nothing", argv == [], f"argv={argv}")
    check("and never falls back to root", "root" not in " ".join(argv), f"argv={argv}")

    # 6. An env file with no POSTGRES_USER is refused the same way.
    empty = stack(base, "empty", None)
    (empty / ".env").write_text("POSTGRES_DB=fixture_db\n")
    result, argv, backups = run(base, empty / "scripts" / "pg-backup.sh")
    check("an env file with no POSTGRES_USER fails",
          result.returncode != 0, result.stdout.strip())
    check("and names the file", str((empty / ".env").resolve()) in result.stderr,
          result.stderr.strip())
    check("and dumps nothing", argv == [], f"argv={argv}")
finally:
    shutil.rmtree(base, ignore_errors=True)

print("=" * 70)
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
