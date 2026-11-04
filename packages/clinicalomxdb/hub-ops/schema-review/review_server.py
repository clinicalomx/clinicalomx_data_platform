#!/usr/bin/env python3
"""Renders the schema review on this host and serves the result through the portal.

Runs `make schema-review` as a host unit. Standard library only.

The render trigger is a command-execution surface:

1. Nothing from the request reaches the command. render_command() builds the argv from
   configuration alone; the POST body is discarded and no route takes a parameter.
2. One render at a time. A second trigger gets 409.
3. The page is installed with os.replace, so a reader never gets half a file.
4. A failed render leaves the previous page in place.

Command output goes to the journal. Only the exit code reaches the browser: a lamindb
traceback can quote connection settings, which hold a password.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# The route prefix Caddy forwards intact, stripped here.
PREFIX = "schema-review"

# Where the render writes, relative to hub-database/. Never the hand-run default path.
RENDER_OUT_REL = ".lavish/ops-schema-review.html"

# The package root holding the Makefile. abspath, not resolve: keep the current/ symlink path.
PACKAGE_ROOT = Path(os.path.abspath(__file__)).parents[2]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def log(msg: str) -> None:
    sys.stderr.write(f"[schema-review] {msg}\n")
    sys.stderr.flush()


@dataclass(frozen=True)
class Config:
    socket_path: str = _env("REVIEW_SOCKET_PATH", "/run/ops-schema-review/review.sock")
    socket_mode: int = int(_env("REVIEW_SOCKET_MODE", "0660"), 8)
    # Used only when the socket path is empty, as in the tests.
    bind_host: str = _env("REVIEW_BIND_HOST", "127.0.0.1")
    bind_port: int = int(_env("REVIEW_BIND_PORT", "8610"))
    repo_dir: str = _env("REVIEW_REPO_DIR", str(PACKAGE_ROOT))
    state_dir: str = _env("REVIEW_STATE_DIR", "/var/lib/ops-schema-review")
    # Passed on the command line: hub-database/.env may predate this key.
    lamin_home: str = _env("REVIEW_LAMIN_HOME", "")
    timeout: int = int(_env("REVIEW_TIMEOUT", "900"))
    # Seconds before the banner marks the page stale. Nothing re-renders on its own.
    stale_after: int = int(_env("REVIEW_STALE_AFTER", "3600"))


CONFIG = Config()


def render_command(cfg: Config) -> list[str]:
    """The whole argv, built from configuration alone."""
    argv = ["make", "schema-review", f"SCHEMA_REVIEW_OUT={RENDER_OUT_REL}"]
    if cfg.lamin_home:
        argv.append(f"LAMIN_HOME={cfg.lamin_home}")
    return argv


def _utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class Renderer:
    """Owns the served page: one render at a time, installed atomically."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.state = Path(cfg.state_dir)
        self.page = self.state / "page.html"
        self.meta_path = self.state / "meta.json"
        self.source = Path(cfg.repo_dir) / "hub-database" / RENDER_OUT_REL
        self._lock = threading.Lock()
        self._running = False
        self._started: float | None = None
        self.state.mkdir(parents=True, exist_ok=True)
        self._meta = self._load_meta()


    def _load_meta(self) -> dict[str, Any]:
        try:
            loaded = json.loads(self.meta_path.read_text())
        except (OSError, ValueError):
            return {"success": None, "failure": None}
        return loaded if isinstance(loaded, dict) else {"success": None, "failure": None}

    def _write(self, target: Path, data: bytes) -> None:
        """Write, fsync, then rename, so a reader sees the whole old file or the whole new one."""
        tmp = target.with_name(target.name + ".new")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)

    def _save_meta(self) -> None:
        self._write(self.meta_path, json.dumps(self._meta, indent=1).encode())


    def status(self) -> dict[str, Any]:
        with self._lock:
            running, started = self._running, self._started
            meta = json.loads(json.dumps(self._meta))
        success = meta.get("success")
        now = time.time()
        age = int(now - success["at"]) if success else None
        return {
            "running": running,
            "running_for": int(now - started) if running and started else None,
            "have_page": bool(success) and self.page.exists(),
            "rendered_at": _utc(success["at"]) if success else None,
            "rendered_epoch": success["at"] if success else None,
            "age_seconds": age,
            "stale_after": self.cfg.stale_after,
            "failure": meta.get("failure"),
        }


    def trigger(self) -> tuple[bool, str]:
        with self._lock:
            if self._running:
                return False, "a render is already in progress"
            self._running = True
            self._started = time.time()
        threading.Thread(target=self._run, daemon=True).start()
        return True, "render started"

    def _run(self) -> None:
        try:
            code, reason = self._render()
            if code == 0 and reason is None:
                log("render succeeded")
            else:
                log(f"render FAILED: {reason} (exit {code}); the served page is unchanged")
        finally:
            with self._lock:
                self._running = False
                self._started = None

    def _render(self) -> tuple[int, str | None]:
        argv = render_command(self.cfg)
        log(f"running: {' '.join(argv)} (cwd {self.cfg.repo_dir})")
        # A new session lets a timeout kill the whole process group. Output goes to the journal.
        try:
            proc = subprocess.Popen(argv, cwd=self.cfg.repo_dir, start_new_session=True)
        except OSError as exc:
            return self._failed(-1, f"could not start make: {exc}")
        try:
            code = proc.wait(timeout=self.cfg.timeout)
        except subprocess.TimeoutExpired:
            self._kill(proc)
            return self._failed(-1, f"timed out after {self.cfg.timeout}s")
        if code != 0:
            return self._failed(code, f"make schema-review exited {code}")
        try:
            data = self.source.read_bytes()
        except OSError as exc:
            return self._failed(code, f"the command reported success but wrote no page: {exc}")
        if not data:
            return self._failed(code, "the command reported success but wrote an empty page")
        self._write(self.page, data)
        with self._lock:
            self._meta["success"] = {"at": time.time(), "bytes": len(data)}
            self._meta["failure"] = None
            self._save_meta()
        return 0, None

    def _kill(self, proc: subprocess.Popen) -> None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                proc.wait(timeout=10)
                return
            except subprocess.TimeoutExpired:
                continue

    def _failed(self, code: int, reason: str) -> tuple[int, str]:
        with self._lock:
            self._meta["failure"] = {"at": time.time(), "exit_code": code, "reason": reason}
            self._save_meta()
        return code, reason


RENDERER: Renderer | None = None

WRAPPER = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Schema review</title>
<style>
  :root { --bg:#fff; --fg:#1a1a1a; --muted:#5a5a5a; --line:#d8d8d8; --bar:#f4f4f4;
          --accent:#2a5db0; --warn:#8a5a00; --warnbg:#fdf3e0; --bad:#a11; --badbg:#fdeaea; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#1c1c1e; --fg:#e8e8e8; --muted:#a0a0a0; --line:#3a3a3c; --bar:#252527;
            --accent:#7aa5ec; --warn:#e0b060; --warnbg:#332a16; --bad:#f08c8c; --badbg:#3a1e1e; }
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body { margin:0; display:flex; flex-direction:column; font-family:system-ui, sans-serif;
         background:var(--bg); color:var(--fg); }
  header { flex:0 0 auto; background:var(--bar); border-bottom:1px solid var(--line);
           padding:.7rem 1rem; display:flex; align-items:center; gap:1rem; flex-wrap:wrap; }
  #stamp { font-size:1.15rem; font-weight:700; }
  #stamp .age { font-weight:400; color:var(--muted); }
  #stamp.stale { color:var(--warn); }
  #stamp.stale .age { color:var(--warn); }
  #stamp.none { color:var(--bad); }
  .spacer { flex:1 1 auto; }
  button { font:inherit; font-size:.9rem; padding:.45rem .9rem; border-radius:4px;
           border:1px solid var(--line); background:var(--bg); color:var(--fg); cursor:pointer; }
  button:hover:not(:disabled) { border-color:var(--accent); color:var(--accent); }
  button:disabled { opacity:.55; cursor:default; }
  .notice { flex:0 0 auto; padding:.55rem 1rem; font-size:.85rem; border-bottom:1px solid var(--line); }
  .notice.bad { background:var(--badbg); color:var(--bad); }
  .notice.info { background:var(--warnbg); color:var(--warn); }
  main { flex:1 1 auto; position:relative; overflow:hidden; }
  iframe { width:100%; height:100%; border:0; display:block; }
  .empty { padding:2rem; max-width:40rem; line-height:1.6; color:var(--muted); }
  code { font-size:.85em; background:var(--bar); padding:.1rem .3rem; border-radius:3px; }
</style>
</head>
<body>
  <header>
    <div id="stamp">reading status</div>
    <span class="spacer"></span>
    <button id="go">Re-render</button>
  </header>
  <div id="notice" class="notice" hidden></div>
  <main>
    <iframe id="page" title="schema review page" hidden></iframe>
    <div id="none" class="empty" hidden>
      <p>No page has been rendered yet.</p>
      <p>Press <strong>Re-render</strong>. It runs <code>make schema-review</code> on this host,
         against the instance this host already has connected, and takes a minute or two.</p>
    </div>
  </main>
<script>
const stamp = document.getElementById('stamp');
const notice = document.getElementById('notice');
const go = document.getElementById('go');
const page = document.getElementById('page');
const none = document.getElementById('none');
let shown = null;
let timer = null;

function ago(s) {
  if (s < 90) return s + 's ago';
  if (s < 5400) return Math.round(s / 60) + ' min ago';
  if (s < 172800) return Math.round(s / 3600) + ' h ago';
  return Math.round(s / 86400) + ' days ago';
}

function apply(s) {
  if (s.have_page) {
    stamp.className = s.age_seconds > s.stale_after ? 'stale' : '';
    stamp.innerHTML = 'Rendered ' + s.rendered_at
                    + ' <span class="age">(' + ago(s.age_seconds) + ')</span>';
    if (shown !== s.rendered_epoch) {
      shown = s.rendered_epoch;
      page.src = 'page?t=' + Math.floor(s.rendered_epoch);
    }
    page.hidden = false;
    none.hidden = true;
  } else {
    stamp.className = 'none';
    stamp.textContent = 'No page rendered yet';
    page.hidden = true;
    none.hidden = false;
  }

  go.disabled = s.running;
  go.textContent = s.running ? 'Rendering...' : 'Re-render';

  if (s.running) {
    show('info', 'Rendering now. The page below, if any, is still the previous one.');
  } else if (s.failure) {
    show('bad', 'The last re-render FAILED at ' + s.failure.at_text + ': ' + s.failure.reason
              + '. The page below is unchanged. Read the reason on the VM with '
              + 'journalctl -u ops-schema-review.');
  } else {
    notice.hidden = true;
  }

  clearTimeout(timer);
  timer = setTimeout(poll, s.running ? 2000 : 20000);
}

function show(kind, text) {
  notice.className = 'notice ' + kind;
  notice.textContent = text;
  notice.hidden = false;
}

async function poll() {
  try {
    const r = await fetch('status', { cache: 'no-store' });
    apply(await r.json());
  } catch (e) {
    show('bad', 'Cannot reach the render service. On the VM: systemctl status ops-schema-review.');
    clearTimeout(timer);
    timer = setTimeout(poll, 10000);
  }
}

go.addEventListener('click', async () => {
  go.disabled = true;
  go.textContent = 'Rendering...';
  try {
    // No body and no query: the render takes nothing from this request.
    const r = await fetch('render', { method: 'POST' });
    if (r.status === 409) show('info', 'A render is already in progress.');
  } catch (e) {
    show('bad', 'Could not start a render. On the VM: systemctl status ops-schema-review.');
  }
  poll();
});

poll();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "clinicalomxdb-schema-review"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        parts = self._parts()
        if parts == []:
            if not self.path.split("?")[0].endswith("/"):
                self._redirect(f"/{PREFIX}/")
            else:
                self._send(200, "text/html; charset=utf-8", WRAPPER.encode())
        elif parts == ["status"]:
            self._send(200, "application/json", json.dumps(self._status()).encode())
        elif parts == ["page"]:
            self._page()
        else:
            self._send(404, "application/json", b'{"error":"not found"}')

    def do_POST(self) -> None:  # noqa: N802
        # Read and discard the body.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self._parts() != ["render"]:
            self._send(404, "application/json", b'{"error":"not found"}')
            return
        started, message = RENDERER.trigger()
        payload = json.dumps({"started": started, "message": message}).encode()
        self._send(202 if started else 409, "application/json", payload)


    def _parts(self) -> list[str]:
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        return parts[1:] if parts[:1] == [PREFIX] else parts

    def _status(self) -> dict[str, Any]:
        status = RENDERER.status()
        failure = status.get("failure")
        if failure:
            failure["at_text"] = _utc(failure["at"])
        return status

    def _page(self) -> None:
        try:
            # os.replace swaps the directory entry, so this handle keeps the complete previous file.
            data = RENDERER.page.read_bytes()
        except OSError:
            self._send(404, "text/html; charset=utf-8", b"<p>no page has been rendered yet</p>")
            return
        self._send(200, "text/html; charset=utf-8", data)

    def _redirect(self, location: str) -> None:
        self.send_response(301)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def address_string(self) -> str:
        peer = self.client_address
        return peer[0] if isinstance(peer, tuple) and peer else "unix"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[schema-review] {self.address_string()} {fmt % args}\n")


class UnixHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX
    allow_reuse_address = False

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = 0


_SOCKET_PATH_MAX = 100


def main() -> None:
    global RENDERER
    RENDERER = Renderer(CONFIG)
    if CONFIG.socket_path:
        target = Path(CONFIG.socket_path)
        if len(str(target).encode()) > _SOCKET_PATH_MAX:
            raise SystemExit(
                f"[schema-review] socket path is over the ~{_SOCKET_PATH_MAX} byte AF_UNIX limit:"
                f" {target}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_socket():
            target.unlink()
        elif target.exists():
            raise SystemExit(f"[schema-review] refusing to replace non-socket path {target}")
        server: ThreadingHTTPServer = UnixHTTPServer(str(target), Handler)
        os.chmod(target, CONFIG.socket_mode)
        where = f"unix:{target}"
    else:
        server = ThreadingHTTPServer((CONFIG.bind_host, CONFIG.bind_port), Handler)
        where = f"{CONFIG.bind_host}:{CONFIG.bind_port}"

    server.daemon_threads = True
    log(f"serving /{PREFIX} on {where} (repo {CONFIG.repo_dir}, state {CONFIG.state_dir})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if CONFIG.socket_path:
            Path(CONFIG.socket_path).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
