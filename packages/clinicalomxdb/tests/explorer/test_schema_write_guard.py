"""The schema-apply guard, run as the real script against throwaway settings files.

A connection this stack does not run is refused. One it runs, including the VM's, is
allowed. Nothing printed carries the password. Each case uses its own LAMIN_HOME.
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

# Asserted absent from every byte the guard prints.
PASSWORD = "n0t-in-the-output-9d3f"

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"OK   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label}{': ' + detail if detail else ''}")


def dsn(host, port, database="lamindb", user="root"):
    where = f"{host}:{port}" if port else host
    return f"postgresql://{user}:{PASSWORD}@{where}/{database}"


def guard(home, db=None):
    """Run the real script against a throwaway LAMIN_HOME holding these settings."""
    if db is not None:
        (home / "current_instance.env").write_text(
            "lamindb_instance_owner=example-org\n"
            "lamindb_instance_name=clinicalomx\n"
            f"lamindb_instance_db={db}\n")
    env = dict(os.environ, LAMIN_HOME=str(home))
    return subprocess.run(
        ["sh", str(SCRIPTS / "schema_write_guard.sh"), "make schema-apply"],
        capture_output=True, text=True, env=env,
    )


print("=" * 70)

base = pathlib.Path(tempfile.mkdtemp(prefix="schema_write_guard_"))
try:
    home = base / "lamin"
    home.mkdir()

    # No settings file: the guard allows it and lamindb fails as usual.
    check("no settings file is allowed",
          guard(home).returncode == 0, "a fresh clone would be refused an apply")

    # sqlite instances are never production.
    for label, value in [("a null database URL", "null"),
                         ("an empty database URL", ""),
                         ("a sqlite instance", "sqlite:///tmp/throwaway.db")]:
        check(f"{label} is allowed", guard(home, value).returncode == 0)

    # Local hosts: localhost inside the postgres container's namespace, and postgres and database,
    # the container and service names the VM's LAMIN_DB_HOST_OVERRIDE uses.
    for label, host in [("the namespace-local cluster", "localhost"),
                        ("the loopback address", "127.0.0.1"),
                        ("the IPv6 loopback", "[::1]"),
                        ("the postgres container by name, which is the VM's own shape", "postgres"),
                        ("the compose service by name", "database")]:
        result = guard(home, dsn(host, 5432))
        check(f"{label} is allowed",
              result.returncode == 0, f"rc={result.returncode} {result.stderr.strip()}")

    # No port means Postgres's default. [::1] must split on the closing bracket, not a colon.
    for label, host in [("a URL with no port", "localhost"),
                        ("the IPv6 loopback with no port", "[::1]")]:
        result = guard(home, dsn(host, None))
        check(f"{label} is allowed",
              result.returncode == 0, f"rc={result.returncode} {result.stderr.strip()}")

    # Refused: the VM's public address, and a forwarded port on loopback.
    for label, host, port in [
        ("a public address, which off the VM is production", "203.0.113.10", 5432),
        ("a hostname this stack does not run", "clinicalomx.example.org", 5432),
        ("a forwarded port on loopback", "localhost", 15432),
        ("a forwarded port on the docker bridge", "172.17.0.1", 15432),
        ("a bracketed IPv6 literal this stack does not run", "[2001:db8::1]", 5432),
    ]:
        result = guard(home, dsn(host, port))
        check(f"{label} is refused",
              result.returncode != 0, f"rc={result.returncode}")
        # The refusal names the endpoint.
        check(f"{label} names the endpoint it refused",
              host in result.stderr and str(port) in result.stderr and "lamindb" in result.stderr,
              result.stderr.strip())
        check(f"{label} says the read verbs are still allowed",
              "schema-review" in result.stderr, result.stderr.strip())

    # current_instance.env holds the password; it must never be printed, refusals included.
    for label, value in [("a refusal", dsn("203.0.113.10", 5432)),
                         ("a pass", dsn("localhost", 5432))]:
        result = guard(home, value)
        check(f"{label} prints no password",
              PASSWORD not in result.stdout and PASSWORD not in result.stderr,
              "the credential reached the output")

    # LAMIN_HOME from the environment beats hub-database/.env, as on the VM.
    other = base / "elsewhere"
    other.mkdir()
    (home / "current_instance.env").write_text("lamindb_instance_db=" + dsn("localhost", 5432) + "\n")
    check("the environment's LAMIN_HOME is what is read, not another directory's settings",
          guard(other, dsn("203.0.113.10", 5432)).returncode != 0)

    # A relative LAMIN_HOME resolves against the stack directory, as compose does. Here the
    # caller's directory is local and the stack's is production, so a wrong guard exits 0.
    stack = SCRIPTS.parent
    relative = "schema_write_guard_relative_home"
    from_cwd = base / relative
    from_cwd.mkdir()
    (from_cwd / "current_instance.env").write_text(
        "lamindb_instance_db=" + dsn("localhost", 5432) + "\n")
    from_stack = stack / relative
    from_stack.mkdir(exist_ok=True)
    try:
        (from_stack / "current_instance.env").write_text(
            "lamindb_instance_db=" + dsn("203.0.113.10", 5432) + "\n")
        result = subprocess.run(
            ["sh", str(SCRIPTS / "schema_write_guard.sh"), "make schema-apply"],
            capture_output=True, text=True, cwd=str(base),
            env=dict(os.environ, LAMIN_HOME="./" + relative),
        )
        check("a relative LAMIN_HOME is read from the stack directory, not from the caller's",
              result.returncode != 0 and "203.0.113.10" in result.stderr,
              f"rc={result.returncode} {result.stderr.strip()}")
    finally:
        shutil.rmtree(from_stack, ignore_errors=True)
finally:
    shutil.rmtree(base, ignore_errors=True)

print("=" * 70)
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
