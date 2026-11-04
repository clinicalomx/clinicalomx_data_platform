"""The LAMIN_HOME guard, run as the real script against throwaway directories.

make lamin-connect chmods and chowns LAMIN_HOME recursively, so the guard refuses a value
resolving to a home directory. The project default must still pass. HOME is redirected into
a temp tree.
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

# As root, chown the fake home to an otherwise unused uid so a root-owned parent is detectable.
OWNER = (12345, 12345) if os.geteuid() == 0 else (os.getuid(), os.getgid())

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"OK   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label}{': ' + detail if detail else ''}")


def guard(stack, home, value):
    (stack / ".env").write_text(f"LAMIN_HOME={value}\n")
    env = dict(os.environ, HOME=str(home))
    return subprocess.run(
        ["sh", str(stack / "scripts" / "lamin_home_owner.sh")],
        capture_output=True, text=True, env=env,
    )


print("=" * 70)

base = pathlib.Path(tempfile.mkdtemp(prefix="lamin_home_guard_"))
try:
    home = base / "home"
    default = home / ".clinicalomxdb" / "lamin"
    default.mkdir(parents=True)
    for path in (home, home / ".clinicalomxdb", default):
        os.chown(path, *OWNER)

    stack = base / "stack"
    (stack / "scripts").mkdir(parents=True)
    for name in ("host_path.sh", "lamin_home_owner.sh"):
        shutil.copy(SCRIPTS / name, stack / "scripts" / name)

    expected = f"{OWNER[0]}:{OWNER[1]}"
    good = guard(stack, home, "~/.clinicalomxdb/lamin")
    check("the project-scoped default is accepted",
          good.returncode == 0, f"rc={good.returncode} {good.stderr.strip()}")
    check("and it prints the owner of the directory above it",
          good.stdout.strip() == expected, f"printed {good.stdout.strip()!r}, wanted {expected!r}")

    # Each would chmod and chown a tree that is not this project's. The refusal names the resolved
    # path.
    for label, value, resolved in [
        ("a bare tilde", "~", str(home)),
        ("a tilde with a trailing slash", "~/", str(home)),
        ("the home directory by absolute path", str(home), str(home)),
        ("the filesystem root", "/", "/"),
        ("a directory whose parent is owned by root", str(base), str(base)),
    ]:
        result = guard(stack, home, value)
        check(f"{label} is refused",
              result.returncode != 0, f"rc={result.returncode} stdout={result.stdout.strip()!r}")
        check(f"{label} prints nothing an owner could be read from",
              result.stdout.strip() == "", result.stdout.strip())
        check(f"{label} names the resolved path in the refusal",
              resolved in result.stderr, result.stderr.strip())

    # A refusal changes nothing in the tree.
    check("a refusal changes no mode in the tree it refused",
          oct(default.stat().st_mode)[-3:] == "755"
          and (default.stat().st_uid, default.stat().st_gid) == OWNER,
          f"{oct(default.stat().st_mode)[-3:]} {default.stat().st_uid}:{default.stat().st_gid}")
finally:
    shutil.rmtree(base, ignore_errors=True)

print("=" * 70)
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
