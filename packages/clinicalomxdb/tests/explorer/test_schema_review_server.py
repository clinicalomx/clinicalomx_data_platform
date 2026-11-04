"""The schema review renderer behind the portal's Schema tab.

Each render runs a fake make on PATH that records its argv; nothing outside a temp directory
is written, and lamindb is never imported. Checks: no request value reaches the command,
triggers do not stack, a reader never gets half a file, a failed or empty render keeps the
previous page, and no command output reaches a response.
"""

import http.client
import json
import os
import pathlib
import shutil
import socket
import sys
import tempfile
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent

# Off-container the checkout supplies what make explorer-test mounts at /ops-schema-review.
SERVER_DIR = pathlib.Path("/ops-schema-review")
if not SERVER_DIR.is_dir():
    SERVER_DIR = HERE.parent.parent / "hub-ops" / "schema-review"

# Asserted absent from every response byte.
NOISE = "SENSITIVE-COMMAND-OUTPUT-must-not-reach-the-browser-7c1f"

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"OK   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label}{': ' + detail if detail else ''}")


# --- load the server as a module -------------------------------------------------

sys.path.insert(0, str(SERVER_DIR))
import review_server as rs  # noqa: E402


# --- a fake make, so no render is ever real ---------------------------------------

FAKE_MAKE = """#!/bin/sh
for a in "$@"; do printf '%s\\n' "$a" >> "$FAKE_LOG"; done
printf -- '--\\n' >> "$FAKE_LOG"
echo "$FAKE_NOISE"
echo "$FAKE_NOISE" >&2
if [ -n "$FAKE_SLEEP" ]; then sleep "$FAKE_SLEEP"; fi
case "$FAKE_MODE" in
  ok)     mkdir -p "$(dirname "$FAKE_OUT")"; printf '%s' "$FAKE_PAGE" > "$FAKE_OUT" ;;
  empty)  mkdir -p "$(dirname "$FAKE_OUT")"; : > "$FAKE_OUT" ;;
  nofile) rm -f "$FAKE_OUT" ;;
  fail)   exit 3 ;;
esac
exit 0
"""


class UnixConnection(http.client.HTTPConnection):
    """HTTP over the unit's AF_UNIX socket."""

    def __init__(self, path):
        super().__init__("localhost")
        self._unix_path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(20)
        sock.connect(self._unix_path)
        self.sock = sock


def request(sock_path, method, path, body=None):
    conn = UnixConnection(sock_path)
    try:
        conn.request(method, path, body=body)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def status(sock_path):
    code, body = request(sock_path, "GET", "/schema-review/status")
    return code, json.loads(body)


def wait_idle(sock_path, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, state = status(sock_path)
        if not state["running"]:
            return state
        time.sleep(0.05)
    raise AssertionError("a fake render never finished")


def log_lines(log_path):
    return log_path.read_text().splitlines() if log_path.exists() else []


print("=" * 70)

base = pathlib.Path(tempfile.mkdtemp(prefix="schema_review_server_"))
server = None
try:
    repo = base / "repo"
    (repo / "hub-database" / ".lavish").mkdir(parents=True)
    bin_dir = base / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "make"
    fake.write_text(FAKE_MAKE)
    fake.chmod(0o755)

    log = base / "argv.log"
    out = repo / "hub-database" / rs.RENDER_OUT_REL
    os.environ.update({
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "FAKE_LOG": str(log),
        "FAKE_OUT": str(out),
        "FAKE_NOISE": NOISE,
        "FAKE_MODE": "ok",
        "FAKE_PAGE": "<html>page one</html>",
        "FAKE_SLEEP": "",
    })

    sock_path = str(base / "review.sock")
    cfg = rs.Config(
        socket_path=sock_path,
        socket_mode=0o600,
        repo_dir=str(repo),
        state_dir=str(base / "state"),
        lamin_home="/home/ubuntu/.lamin",
        timeout=20,
        stale_after=3600,
    )
    rs.CONFIG = cfg
    rs.RENDERER = rs.Renderer(cfg)
    server = rs.UnixHTTPServer(sock_path, rs.Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # --- the command is built from configuration alone ---------------------------

    check("the command is the make target and nothing else",
          rs.render_command(cfg) == ["make", "schema-review",
                                     "SCHEMA_REVIEW_OUT=" + rs.RENDER_OUT_REL,
                                     "LAMIN_HOME=/home/ubuntu/.lamin"],
          str(rs.render_command(cfg)))

    check("with no LAMIN_HOME configured the argument is absent rather than empty",
          rs.render_command(rs.Config(lamin_home="")) == [
              "make", "schema-review", "SCHEMA_REVIEW_OUT=" + rs.RENDER_OUT_REL])

    # --- nothing from the request reaches it -------------------------------------

    code, _ = status(sock_path)
    check("a fresh unit answers its status route", code == 200)

    _, before = status(sock_path)
    check("a fresh unit has no page and claims none",
          before["have_page"] is False and before["rendered_at"] is None, str(before))

    injected = json.dumps({
        "target": "schema-apply",
        "args": ["--default-storage", "/etc"],
        "LAMIN_HOME": "/tmp/somewhere-else",
    }).encode()
    code, _ = request(sock_path, "POST",
                      "/schema-review/render?target=schema-apply&rows=99", injected)
    check("a trigger carrying a body and a query is accepted", code == 202, str(code))
    wait_idle(sock_path)

    recorded = log_lines(log)
    check("the command run is exactly the configured one, whatever the request said",
          recorded == ["schema-review", "SCHEMA_REVIEW_OUT=" + rs.RENDER_OUT_REL,
                       "LAMIN_HOME=/home/ubuntu/.lamin", "--"],
          str(recorded))

    _, after = status(sock_path)
    check("a successful render is served and stamped",
          after["have_page"] and after["rendered_at"] and after["age_seconds"] is not None,
          str(after))

    code, page = request(sock_path, "GET", "/schema-review/page")
    check("the page served is the one the render wrote",
          code == 200 and page == b"<html>page one</html>", f"{code} {page[:80]!r}")

    # --- nothing the command printed reaches a response --------------------------

    code, wrapper = request(sock_path, "GET", "/schema-review/")
    _, state_body = request(sock_path, "GET", "/schema-review/status")
    check("no response carries what the command printed",
          NOISE.encode() not in wrapper and NOISE.encode() not in state_body
          and NOISE.encode() not in page)

    check("the wrapper states the render time and offers the trigger",
          b"Rendered " in wrapper and b"Re-render" in wrapper,
          "the wrapper is missing its stamp or its button")

    # --- triggers do not stack ---------------------------------------------------

    log.unlink()
    os.environ["FAKE_SLEEP"] = "2"
    os.environ["FAKE_PAGE"] = "<html>page two</html>"
    first, _ = request(sock_path, "POST", "/schema-review/render")
    refused = [request(sock_path, "POST", "/schema-review/render")[0] for _ in range(5)]
    check("a second trigger while one runs is refused rather than queued",
          first == 202 and refused == [409] * 5, f"{first} {refused}")

    _, during = status(sock_path)
    check("the status says a render is in progress", during["running"] is True, str(during))
    check("the previous page is still served while a render runs",
          request(sock_path, "GET", "/schema-review/page")[1] == b"<html>page one</html>")

    wait_idle(sock_path)
    check("exactly one render ran for six triggers",
          log_lines(log).count("--") == 1, str(log_lines(log)))
    check("the finished render replaced the page",
          request(sock_path, "GET", "/schema-review/page")[1] == b"<html>page two</html>")

    # --- a reader is never served half a file ------------------------------------
    # --- a reader is never served half a file ------------------------------------
    # One thread installs alternating multi-megabyte pages while readers fetch over the socket.
    # Every read must be one whole version.

    os.environ["FAKE_SLEEP"] = ""
    big_a = ("<html>" + "a" * (2 * 1024 * 1024) + "</html>").encode()
    big_b = ("<html>" + "b" * (2 * 1024 * 1024) + "</html>").encode()
    seen = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                _, body = request(sock_path, "GET", "/schema-review/page")
            except (OSError, http.client.HTTPException) as exc:
                seen.append(("error", str(exc)))
                continue
            seen.append(("whole" if body in (big_a, big_b) else "torn", len(body)))

    # Installed first, so any read of a third length is torn.
    rs.RENDERER._write(rs.RENDERER.page, big_a)
    readers = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
    for thread in readers:
        thread.start()
    for i in range(40):
        rs.RENDERER._write(rs.RENDERER.page, big_a if i % 2 else big_b)
        time.sleep(0.01)
    stop.set()
    for thread in readers:
        thread.join(timeout=20)

    torn = [entry for entry in seen if entry[0] != "whole"]
    check("no reader ever saw a partial page during a swap",
          len(seen) > 20 and not torn, f"{len(seen)} reads, {torn[:3]}")

    rs.RENDERER._write(rs.RENDERER.page, b"<html>page two</html>")

    # --- a failed render leaves the previous page --------------------------------

    for mode, label in [("fail", "a command that exits non-zero"),
                        ("empty", "a command that reports success and writes an empty page"),
                        ("nofile", "a command that reports success and writes no page at all")]:
        os.environ["FAKE_MODE"] = mode
        os.environ["FAKE_PAGE"] = "<html>never served</html>"
        request(sock_path, "POST", "/schema-review/render")
        wait_idle(sock_path)
        _, state = status(sock_path)
        code, body = request(sock_path, "GET", "/schema-review/page")
        check(f"{label} leaves the previous page untouched",
              body == b"<html>page two</html>", f"{code} {body[:80]!r}")
        check(f"{label} is reported as a failure",
              state["failure"] and state["have_page"] and state["rendered_at"],
              str(state))
        check(f"{label} reports no command output",
              NOISE not in json.dumps(state), str(state))

    # A later success clears the failure.
    os.environ["FAKE_MODE"] = "ok"
    os.environ["FAKE_PAGE"] = "<html>page three</html>"
    request(sock_path, "POST", "/schema-review/render")
    wait_idle(sock_path)
    _, recovered = status(sock_path)
    check("a success after a failure clears the failure",
          recovered["failure"] is None and recovered["have_page"], str(recovered))

    # --- the surface is three routes and no more ---------------------------------

    check("a GET on the render route is not a trigger",
          request(sock_path, "GET", "/schema-review/render")[0] == 404)
    check("an unknown route is refused",
          request(sock_path, "GET", "/schema-review/../etc/passwd")[0] == 404)
    check("an unknown POST route is refused",
          request(sock_path, "POST", "/schema-review/anything")[0] == 404)
    check("no route ran a further render",
          log_lines(log).count("--") == 5, str(log_lines(log).count("--")))

    # --- what survives a restart -------------------------------------------------

    rs.RENDERER = rs.Renderer(cfg)
    _, restarted = status(sock_path)
    check("a restarted unit still serves the page it rendered before",
          restarted["have_page"] and restarted["rendered_at"] == recovered["rendered_at"],
          str(restarted))
    check("a restarted unit is not stuck reporting a render in progress",
          restarted["running"] is False, str(restarted))

finally:
    if server is not None:
        server.shutdown()
        server.server_close()
    shutil.rmtree(base, ignore_errors=True)

print("=" * 70)
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
