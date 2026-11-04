#!/usr/bin/env python3
"""HPC login relay: drives real ssh on a PTY and exposes its prompts as JSON widgets.

The ssh child becomes an OpenSSH ControlMaster that the Prefect workers reuse. Each DUO or
password prompt is forwarded as it arrives. A prompt is secret when the pty has echo off.

Credential handling:

1. A response is written to the pty and dropped: never stored, returned or written to disk.
2. Logs carry prompt text and outcomes only, never a response.
3. The transcript redacts responses of four characters or more.
4. Sessions live in memory, are capped, and expire with their ssh child.
5. The server binds a unix socket reachable only through the portal.

Runs as a host unit, sharing the workers' home directory. Standard library only.
"""

from __future__ import annotations

import errno
import json
import os
import pty
import re
import select
import signal
import socket
import socketserver
import sys
import termios
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

USERNAME_RE = re.compile(r"^[a-z][a-z0-9._-]{0,31}$")
# Shorter responses, such as a menu choice, are not redacted.
REDACT_MIN = 4


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Config:
    socket_path: str = _env("LOGIN_SOCKET_PATH", "/run/ops-login/login.sock")
    socket_mode: int = int(_env("LOGIN_SOCKET_MODE", "0660"), 8)
    bind_host: str = _env("LOGIN_BIND_HOST", "127.0.0.1")
    bind_port: int = int(_env("LOGIN_BIND_PORT", "8098"))

    # hpc.login_host in site.yaml; ops-login.service sets it.
    hpc_host: str = _env("LOGIN_HPC_HOST", "")
    control_path: str = _env("LOGIN_CONTROL_PATH", "~/.ssh/cm-%r@%h:%p")
    control_persist: str = _env("LOGIN_CONTROL_PERSIST", "96h")

    # Bounds on live ssh children waiting on a human.
    max_sessions: int = int(_env("LOGIN_MAX_SESSIONS", "4"))
    session_ttl: int = int(_env("LOGIN_SESSION_TTL", "300"))
    # Seconds of quiet output before the tail is treated as a prompt.
    idle_gap: float = float(_env("LOGIN_IDLE_GAP", "0.5"))
    # Seconds to wait for output after a response; a DUO push can take a while.
    read_timeout: int = int(_env("LOGIN_READ_TIMEOUT", "120"))
    transcript_max: int = int(_env("LOGIN_TRANSCRIPT_MAX", "4000"))


CONFIG = Config()


def log(message: str) -> None:
    """Prompt text and outcomes only. Never a response value."""
    sys.stderr.write(f"[login] {message}\n")
    sys.stderr.flush()


@dataclass
class Session:
    id: str
    user: str
    pid: int = 0
    fd: int = -1
    state: str = "starting"  # starting | prompt | waiting | done | failed | expired
    prompt: str = ""
    secret: bool = False
    message: str = ""
    transcript: str = ""
    created: float = field(default_factory=time.monotonic)
    _response: str | None = None
    _has_response: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # Responses sent, so an echoed one can be scrubbed from output.
    _sent: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "user": self.user,
                "state": self.state,
                "prompt": self.prompt,
                "secret": self.secret,
                "message": self.message,
                "transcript": self.transcript[-CONFIG.transcript_max :],
            }

    def _redact(self, text: str) -> str:
        for value in self._sent:
            if len(value) >= REDACT_MIN:
                text = text.replace(value, "[redacted]")
        return text

    def add_output(self, text: str) -> None:
        with self._lock:
            self.transcript = (self.transcript + self._redact(text))[
                -CONFIG.transcript_max :
            ]

    def respond(self, value: str) -> bool:
        """Hand a response to the driver thread. The value is not stored on the session."""
        with self._lock:
            if self.state != "prompt":
                return False
            self._response = value
            self.state = "waiting"
            self.prompt = ""
        self._has_response.set()
        return True

    def take_response(self) -> str | None:
        with self._lock:
            value = self._response
            self._response = None
            if value is not None and len(value) >= REDACT_MIN:
                self._sent.append(value)
        self._has_response.clear()
        return value

    def kill(self, why: str) -> None:
        with self._lock:
            if self.state in ("done", "failed", "expired"):
                return
            self.state = "expired"
            self.message = why
        _terminate(self.pid, self.fd)


def _terminate(pid: int, fd: int) -> None:
    if pid > 0:
        try:
            os.kill(pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        else:
            for _ in range(20):
                try:
                    if os.waitpid(pid, os.WNOHANG)[0] == pid:
                        break
                except ChildProcessError:
                    break
                time.sleep(0.05)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                except OSError:
                    pass
    if fd >= 0:
        try:
            os.close(fd)
        except OSError:
            pass


def _echo_off(fd: int) -> bool:
    """True when the pty has echo off, which is how ssh signals a secret prompt."""
    try:
        return not (termios.tcgetattr(fd)[3] & termios.ECHO)
    except (termios.error, OSError):
        return False


def _ssh_argv(user: str) -> list[str]:
    """ssh argv. Only the account is client-controlled."""
    return [
        "ssh",
        "-e",
        "none",
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={CONFIG.control_path}",
        "-o",
        f"ControlPersist={CONFIG.control_persist}",
        "-o",
        "ServerAliveInterval=60",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "NumberOfPasswordPrompts=3",
        "-o",
        f"User={user}",
        CONFIG.hpc_host,
        "true",
    ]


class Driver(threading.Thread):
    """Runs one ssh child on a pty, publishing each prompt and feeding back each response."""

    def __init__(self, session: Session) -> None:
        super().__init__(daemon=True)
        self.session = session

    def run(self) -> None:
        s = self.session
        try:
            pid, fd = pty.fork()
        except OSError as exc:
            self._finish("failed", f"could not start ssh: {exc}")
            return

        if pid == 0:
            try:
                os.execvp("ssh", _ssh_argv(s.user))
            except Exception:
                os._exit(127)

        s.pid, s.fd = pid, fd
        log(f"session {s.id[:8]} started for {s.user} (pid {pid})")

        # kill() may have run before pid was recorded; recheck.
        with s._lock:
            already_over = s.state in ("expired", "done", "failed")
        if already_over:
            log(f"session {s.id[:8]} was cancelled during startup; reaping child")
            _terminate(pid, fd)
            s.pid, s.fd = 0, -1
            return

        try:
            self._pump_loop()
        except Exception as exc:  # never leave a session wedged in "starting"
            log(f"session {s.id[:8]} driver error: {type(exc).__name__}: {exc}")
            self._finish("failed", f"relay error: {type(exc).__name__}")

    def _pump_loop(self) -> None:
        s = self.session
        while True:
            chunk, exited = self._read_until_idle()
            if chunk:
                s.add_output(chunk)

            if exited:
                status = self._reap()
                if status == 0:
                    self._finish("done", f"connected; session established for {s.user}")
                else:
                    self._finish(
                        "failed",
                        "ssh exited without authenticating. Wrong password, an expired or "
                        "declined second factor, or no access to this host.",
                    )
                return

            prompt, secret = self._classify(chunk)
            if prompt is None:
                # No prompt yet; the session deadline stops this loop.
                if time.monotonic() - s.created > CONFIG.session_ttl:
                    s.kill("timed out waiting for ssh")
                    return
                continue

            with s._lock:
                s.state = "prompt"
                s.prompt = prompt
                s.secret = secret
            log(f"session {s.id[:8]} prompt ({'secret' if secret else 'plain'}): {prompt!r}")

            if not s._has_response.wait(timeout=CONFIG.session_ttl):
                s.kill("timed out waiting for a response")
                return

            value = s.take_response()
            if value is None:
                s.kill("no response")
                return
            try:
                os.write(s.fd, value.encode() + b"\n")
            except OSError as exc:
                self._finish("failed", f"could not send response: {exc}")
                return
            finally:
                del value  # not retained anywhere

    def _read_until_idle(self) -> tuple[str, bool]:
        """Collect output until it goes quiet or the child closes the pty.

        Output that stops without a trailing newline is a prompt.
        """
        s = self.session
        buf = bytearray()
        deadline = time.monotonic() + CONFIG.read_timeout
        last = None
        while True:
            if time.monotonic() > deadline:
                return buf.decode(errors="replace"), False
            gap = CONFIG.idle_gap if buf else 1.0
            try:
                ready, _, _ = select.select([s.fd], [], [], gap)
            except OSError:
                return buf.decode(errors="replace"), True
            if not ready:
                if buf and last is not None and time.monotonic() - last >= CONFIG.idle_gap:
                    return buf.decode(errors="replace"), False
                if not buf and self._child_gone():
                    return "", True
                continue
            try:
                data = os.read(s.fd, 4096)
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EBADF):  # pty closed: child exited
                    return buf.decode(errors="replace"), True
                raise
            if not data:
                return buf.decode(errors="replace"), True
            buf.extend(data)
            last = time.monotonic()

    def _child_gone(self) -> bool:
        try:
            return os.waitpid(self.session.pid, os.WNOHANG)[0] == self.session.pid
        except ChildProcessError:
            return True
        except OSError:
            return False

    def _classify(self, chunk: str) -> tuple[str | None, bool]:
        """The prompt is the trailing text after the last newline, if any."""
        text = chunk.replace("\r\n", "\n").replace("\r", "")
        tail = text.rsplit("\n", 1)[-1].strip() if text else ""
        if not tail:
            return None, False
        return tail, _echo_off(self.session.fd)

    def _reap(self) -> int:
        try:
            _, status = os.waitpid(self.session.pid, 0)
        except (ChildProcessError, OSError):
            return 0
        return os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else status

    def _finish(self, state: str, message: str) -> None:
        s = self.session
        with s._lock:
            s.state = state
            s.prompt = ""
            s.message = message
        if s.fd >= 0:
            try:
                os.close(s.fd)
            except OSError:
                pass
            s.fd = -1
        log(f"session {s.id[:8]} {state}: {message}")


class Sessions:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, Session] = {}

    def reap(self) -> None:
        with self._lock:
            for sid, s in list(self._items.items()):
                age = time.monotonic() - s.created
                if age > CONFIG.session_ttl:
                    s.kill("expired")
                if age > CONFIG.session_ttl * 2:
                    del self._items[sid]

    def create(self, user: str) -> Session | None:
        self.reap()
        with self._lock:
            live = [s for s in self._items.values() if s.state in ("starting", "prompt", "waiting")]
            if len(live) >= CONFIG.max_sessions:
                return None
            s = Session(id=uuid.uuid4().hex, user=user)
            self._items[s.id] = s
        Driver(s).start()
        return s

    def get(self, sid: str) -> Session | None:
        self.reap()
        with self._lock:
            return self._items.get(sid)


SESSIONS = Sessions()


class Handler(BaseHTTPRequestHandler):
    server_version = "clinicalomxdb-login"
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        parts = self._parts()
        if parts == ["login", "session"]:
            self._start()
        elif len(parts) == 4 and parts[:2] == ["login", "session"] and parts[3] == "respond":
            self._respond(parts[2])
        else:
            self._json(404, {"error": "not found"})

    def do_GET(self) -> None:  # noqa: N802
        parts = self._parts()
        if len(parts) == 3 and parts[:2] == ["login", "session"]:
            s = SESSIONS.get(parts[2])
            self._json(404, {"error": "no such session"}) if s is None else self._json(200, s.public())
        elif parts == ["login", "config"]:
            self._json(200, {"host": CONFIG.hpc_host, "session_ttl": CONFIG.session_ttl})
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        parts = self._parts()
        if len(parts) == 3 and parts[:2] == ["login", "session"]:
            s = SESSIONS.get(parts[2])
            if s is None:
                self._json(404, {"error": "no such session"})
                return
            s.kill("cancelled")
            self._json(200, s.public())
        else:
            self._json(404, {"error": "not found"})


    def _parts(self) -> list[str]:
        return [p for p in self.path.split("?")[0].split("/") if p]

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw or b"{}")
        except (ValueError, json.JSONDecodeError):
            return {}

    def _start(self) -> None:
        user = str(self._body().get("user", "")).strip()
        if not USERNAME_RE.match(user):
            self._json(400, {"error": "expected a lowercase account name such as alice"})
            return
        s = SESSIONS.create(user)
        if s is None:
            self._json(429, {"error": "too many logins in progress; try again shortly"})
            return
        for _ in range(40):
            if s.state != "starting":
                break
            time.sleep(0.1)
        self._json(201, s.public())

    def _respond(self, sid: str) -> None:
        s = SESSIONS.get(sid)
        if s is None:
            self._json(404, {"error": "no such session"})
            return
        body = self._body()
        if "response" not in body:
            self._json(400, {"error": "missing response"})
            return
        if not s.respond(str(body["response"])):
            self._json(409, {"error": f"session is {s.state}, not awaiting a response"})
            return
        for _ in range(50):
            if s.state not in ("waiting",):
                break
            time.sleep(0.1)
        self._json(200, s.public())

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def address_string(self) -> str:
        peer = self.client_address
        return peer[0] if isinstance(peer, tuple) and peer else "unix"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Request lines only. Bodies carry responses and are never logged.
        sys.stderr.write(f"[login] {self.address_string()} {fmt % args}\n")


class UnixHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX
    allow_reuse_address = False

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = 0


_SOCKET_PATH_MAX = 100


def main() -> None:
    if not CONFIG.hpc_host:
        raise SystemExit("[login] LOGIN_HPC_HOST is unset: ops-login.service sets it from "
                         "hpc.login_host in site.yaml")
    if CONFIG.socket_path:
        target = Path(CONFIG.socket_path)
        if len(str(target).encode()) > _SOCKET_PATH_MAX:
            raise SystemExit(
                f"[login] socket path is over the ~{_SOCKET_PATH_MAX} byte AF_UNIX limit: {target}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_socket():
            target.unlink()
        elif target.exists():
            raise SystemExit(f"[login] refusing to replace non-socket path {target}")
        server: ThreadingHTTPServer = UnixHTTPServer(str(target), Handler)
        os.chmod(target, CONFIG.socket_mode)
        where = f"unix:{target}"
    else:
        server = ThreadingHTTPServer((CONFIG.bind_host, CONFIG.bind_port), Handler)
        where = f"{CONFIG.bind_host}:{CONFIG.bind_port}"

    server.daemon_threads = True
    log(f"serving /login on {where} (host {CONFIG.hpc_host}, ttl {CONFIG.session_ttl}s)")
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
