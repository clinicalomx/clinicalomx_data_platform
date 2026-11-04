"""lamin_local's redirection: who the init child runs as, what is published, and what is not.

No database, network, instance or credential. Not covered: ensure_database(), the init
itself, and main() end to end, which need Postgres.

1. child_environment() makes the child resolve the anonymous user even when HOME holds a
   stored login. A control probe shows the same HOME yields the stored handle without it.
2. publish_settings() puts the instance settings file where lamindb looks for it.
3. names_to_publish() publishes current_user.env when the shared directory has none, so a
   schema-* container with an empty LAMIN_API_KEY resolves the anonymous user. It never
   overwrites a developer's own login.
"""

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent

# make explorer-test mounts hub-database/scripts at /scripts; off-container the checkout is used.
SCRIPTS = pathlib.Path("/scripts")
if not SCRIPTS.is_dir():
    SCRIPTS = HERE.parent.parent / "hub-database" / "scripts"

spec = importlib.util.spec_from_file_location("lamin_local", SCRIPTS / "lamin_local.py")
lamin_local = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lamin_local)

# As main() does: an empty value sends lamindb to the hub.
os.environ.pop("LAMIN_API_KEY", None)

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"OK   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label}{': ' + detail if detail else ''}")


def run_probe(env, source):
    """Run source in a fresh interpreter under env. Returns the result and its parsed last line.

    The parsed line is None when the interpreter died.
    """
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, env=env, cwd="/tmp",
    )
    if result.returncode != 0:
        return result, None
    return result, json.loads(result.stdout.strip().splitlines()[-1])


def probe(env, source):
    """Run source in a fresh interpreter under env, and return its parsed last line."""
    result, parsed = run_probe(env, source)
    if parsed is None:
        raise AssertionError(f"probe failed: {result.stderr[-800:]}")
    return parsed


# One interpreter launch reports every fact below.
WHO_AND_WHERE = (
    "import json, lamindb_setup as s;"
    "from lamindb_setup.core._settings_store import settings_dir;"
    "print(json.dumps({'handle': s.settings.user.handle, 'settings_dir': str(settings_dir)}))"
)

WHERE_LAMINDB_LOOKS = (
    "import json;"
    "from lamindb_setup.core._settings_store import instance_settings_file;"
    "f = instance_settings_file(%r, %r);"
    "print(json.dumps({'path': str(f), 'exists': f.exists()}))"
)

USER_ENV = (
    "lamin_user_email=dev@example.org\n"
    "lamin_user_password=unused\n"
    "lamin_user_access_token=expired-token-not-a-real-credential\n"
    "lamin_user_uid=devuid00\n"
    "lamin_user_uuid=00000000-0000-0000-0000-000000000001\n"
    "lamin_user_handle=somedeveloper\n"
    "lamin_user_name=Some Developer\n"
)

print("=" * 70)

base = pathlib.Path(tempfile.mkdtemp(prefix="lamin_local_anon_"))
real_home = os.environ.get("HOME")
try:
    # A home holding this project's stored login with an expired token.
    home = base / "home"
    (home / ".lamin").mkdir(parents=True)
    (home / ".lamin" / "current_user.env").write_text(USER_ENV)

    scratch = base / "scratch"
    (scratch / ".lamin").mkdir(parents=True)

    # child_environment() copies os.environ. HOME is restored in the finally below.
    os.environ["HOME"] = str(home)
    stored_login = dict(os.environ)

    # The control: without the redirection the child resolves the stored handle.
    control = probe(stored_login, WHO_AND_WHERE)
    check("the seeded home really does carry a stored login",
          control["handle"] == "somedeveloper", repr(control["handle"]))

    redirected = probe(lamin_local.child_environment(scratch), WHO_AND_WHERE)
    check("lamin_local's child environment resolves the anonymous user instead",
          redirected["handle"] == "anonymous", repr(redirected["handle"]))

    # The child must read the scratch directory while HOME holds a complete lamindb setup.
    check("and it reads its settings from the scratch directory, not from that home",
          redirected["settings_dir"] == str(scratch / ".lamin"),
          f"{redirected['settings_dir']} is not {scratch / '.lamin'}")

    check("lamin_local owns the instance as exactly the user its child resolves",
          lamin_local.OWNER == redirected["handle"],
          f"OWNER={lamin_local.OWNER!r} but the child resolves {redirected['handle']!r}")

    # Half two. The scratch directory stands in for what the init leaves behind.
    instance_env, current_env, user_env = lamin_local.settings_filenames()
    written = base / "written" / ".lamin"
    written.mkdir(parents=True)
    (written / instance_env).write_text("lamindb_instance_name=from-the-init\n")
    (written / current_env).write_text("lamindb_instance_name=from-the-init\n")

    left_by_the_init = scratch / ".lamin" / user_env
    check("the init leaves an anonymous current_user.env in the scratch directory",
          left_by_the_init.is_file(), f"nothing at {left_by_the_init}")
    shutil.copy(left_by_the_init, written / user_env)

    published_home = base / "published"
    clean = lamin_local.names_to_publish(
        published_home / ".lamin", (instance_env, current_env, user_env))
    check("with no stored user in the shared directory, current_user.env is published too",
          set(clean) == {instance_env, current_env, user_env}, str(clean))
    lamin_local.publish_settings(written, published_home / ".lamin", clean)

    # Ask lamindb where it would look.
    looks = probe({**os.environ, "LAMIN_SETTINGS_DIR": str(published_home)},
                  WHERE_LAMINDB_LOOKS % (lamin_local.INSTANCE, lamin_local.OWNER))
    check("publish_settings puts the instance file where lamindb looks for it",
          looks["exists"], f"nothing at {looks['path']}")

    landed = published_home / ".lamin" / current_env
    landed_text = landed.read_text() if landed.is_file() else None
    check("and current_instance.env lands beside it holding what the init wrote",
          landed_text == "lamindb_instance_name=from-the-init\n", repr(landed_text))

    # Half three, as a schema-* container runs it: a new interpreter, the shared directory, and an
    # empty LAMIN_API_KEY.
    schema_verb = {**os.environ, "LAMIN_SETTINGS_DIR": str(published_home), "LAMIN_API_KEY": ""}
    resolved = probe(schema_verb, WHO_AND_WHERE)
    check("a schema-* container reading the published directory resolves the anonymous user",
          resolved["handle"] == "anonymous", repr(resolved["handle"]))

    # A shared directory holding the instance pair and no stored user.
    without = base / "without-a-user"
    (without / ".lamin").mkdir(parents=True)
    for name in (instance_env, current_env):
        shutil.copy(written / name, without / ".lamin" / name)
    _, unresolved = run_probe(
        {**os.environ, "LAMIN_SETTINGS_DIR": str(without), "LAMIN_API_KEY": ""}, WHO_AND_WHERE)
    check("and the same container with no stored user cannot resolve one at all",
          unresolved is None, f"resolved {unresolved} rather than failing")

    # A shared directory that already holds a developer's login keeps it.
    theirs_home = base / "theirs"
    theirs = theirs_home / ".lamin"
    theirs.mkdir(parents=True)
    (theirs / user_env).write_text(USER_ENV)
    kept = lamin_local.names_to_publish(theirs, (instance_env, current_env, user_env))
    check("a shared directory that already holds a login is not given current_user.env",
          user_env not in kept, str(kept))

    lamin_local.publish_settings(written, theirs, kept)
    check("so their login is byte-identical after a publish",
          (theirs / user_env).read_bytes() == USER_ENV.encode(),
          repr((theirs / user_env).read_text()))
    check("while the instance settings still land beside it",
          (theirs / instance_env).is_file() and (theirs / current_env).is_file(),
          str(sorted(p.name for p in theirs.iterdir())))

    kept_resolution = probe(
        {**os.environ, "LAMIN_SETTINGS_DIR": str(theirs_home), "LAMIN_API_KEY": ""}, WHO_AND_WHERE)
    check("and a schema-* container resolves THEIR handle from it rather than logging in",
          kept_resolution["handle"] == "somedeveloper", repr(kept_resolution["handle"]))

    # An init that left only one of the two files publishes nothing.
    (written / current_env).unlink()
    partial = base / "partial" / ".lamin"
    try:
        lamin_local.publish_settings(written, partial, (instance_env, current_env))
        raised = None
    except FileNotFoundError as exc:
        raised = exc
    check("an init that left only one settings file fails loudly",
          raised is not None, "publish_settings returned normally")
    check("and publishes NEITHER file, so the shared directory is untouched",
          not (partial / instance_env).exists(),
          f"{partial / instance_env} was published without {current_env} beside it")
    # The shared directory must not even have been created.
    check("and leaves the shared directory alone entirely, staging files included",
          not partial.exists(),
          str(sorted(p.name for p in partial.iterdir())) if partial.is_dir() else "created")

    # Both sources exist and a copy fails partway: a name too long for NAME_MAX forces it, even as
    # root. The staged file must be cleaned up.
    both = base / "both" / ".lamin"
    both.mkdir(parents=True)
    fine, too_long = instance_env, "n" * 250
    (both / fine).write_text("lamindb_instance_name=from-the-init\n")
    (both / too_long).write_text("staging this one cannot open a file\n")
    destination = base / "batch" / ".lamin"
    try:
        lamin_local.publish_settings(both, destination, (fine, too_long))
        batch_raised = None
    except OSError as exc:
        batch_raised = exc
    check("a copy that fails partway through the batch also raises",
          batch_raised is not None, "publish_settings returned normally")
    check("and the file that did stage is cleaned up rather than left in the shared directory",
          sorted(p.name for p in destination.iterdir()) == [],
          str(sorted(p.name for p in destination.iterdir())))
finally:
    if real_home is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = real_home
    shutil.rmtree(base, ignore_errors=True)

print("=" * 70)
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
