#!/usr/bin/env python3
"""Development server for the operator portal: live preview, fixtures, style writeback.

Serves hub-ops/site/index.html against canned /health and /login responses, so every state,
failures included, is reachable. The dev client is injected as the file is served; the
committed file is never modified. A state is chosen by query parameter, so each state is a
plain URL (see shots.sh). Standard library only.

Run it with make ui-dev, on your own machine; it refuses to start on the VM. Health states are
fixtures/health-*.json and login sequences are fixtures/login-*.json. make ui-fixtures-check
compares the fixtures' key shape with the real health_server.py. The style inspector writes only
between the page's style markers and keeps a .bak; make ui-notes lists the notes it queued.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
SITE = HERE.parent / "site"
INDEX = SITE / "index.html"
FIXTURES = HERE / "fixtures"
NOTES = HERE / "notes.jsonl"
INSPECTOR = HERE / "inspector.js"

HOST = os.environ.get("UI_DEV_HOST", "127.0.0.1")
PORT = int(os.environ.get("UI_DEV_PORT", "8090"))
POLL = 0.3

STYLE_OPEN, STYLE_CLOSE = "<style>", "</style>"
# Braces and angle brackets are refused, so a POST cannot escape the rule it edits.
DECL_FORBIDDEN = re.compile(r"[{}<>]")
SELECTOR_OK = re.compile(r"^[A-Za-z0-9 .#:,>\[\]=\"'()_-]{1,200}$")


def log(msg: str) -> None:
    sys.stderr.write(f"[ui-dev] {msg}\n")
    sys.stderr.flush()


class Watcher(threading.Thread):
    """Polls mtimes and tells connected browsers to reload.

    A change confined to the style block hot-swaps the stylesheet, keeping page state.
    """

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.clients: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._seen: dict[Path, float] = {}
        self._last_style = _style_block(INDEX.read_text()) if INDEX.exists() else ""

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self.clients:
                self.clients.remove(q)

    def publish(self, event: str, data: str = "") -> None:
        with self._lock:
            for q in list(self.clients):
                q.put((event, data))

    def run(self) -> None:
        watched = [INDEX, INSPECTOR]
        while True:
            for path in watched + sorted(FIXTURES.glob("*.json")):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if self._seen.get(path) is None:
                    self._seen[path] = mtime
                    continue
                if mtime != self._seen[path]:
                    self._seen[path] = mtime
                    self._on_change(path)
            time.sleep(POLL)

    def _on_change(self, path: Path) -> None:
        if path == INDEX:
            style = _style_block(INDEX.read_text())
            body_changed = _without_style(INDEX.read_text()) != _without_style(
                _last_full.get("html", "")
            )
            _last_full["html"] = INDEX.read_text()
            if style != self._last_style and not body_changed:
                self._last_style = style
                log("style changed, hot-swapping")
                self.publish("style", style)
                return
            self._last_style = style
        log(f"{path.name} changed, reloading")
        self.publish("reload")


_last_full: dict[str, str] = {}


def _style_block(html: str) -> str:
    try:
        return html[html.index(STYLE_OPEN) + len(STYLE_OPEN) : html.index(STYLE_CLOSE)]
    except ValueError:
        return ""


def _without_style(html: str) -> str:
    if not html:
        return ""
    try:
        return html[: html.index(STYLE_OPEN)] + html[html.index(STYLE_CLOSE) :]
    except ValueError:
        return html


WATCHER = Watcher()


def fixture_names() -> list[str]:
    return sorted(p.stem[len("health-") :] for p in FIXTURES.glob("health-*.json"))


def load_fixture(kind: str, name: str) -> Any:
    path = FIXTURES / f"{kind}-{name}.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text())
    if kind == "health" and isinstance(payload, dict):
        # Fresh timestamp for the page's "as of" line.
        payload["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return payload


class MockLogin:
    """Walks a scripted prompt sequence through the real relay's four routes. Each response
    advances one step."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._n = 0

    def start(self, user: str, script_name: str) -> dict[str, Any]:
        script = load_fixture("login", script_name) or load_fixture("login", "password-prompt")
        with self._lock:
            self._n += 1
            sid = f"dev{self._n:04d}"
            self._sessions[sid] = {"user": user, "script": script, "step": 0}
        return self._state(sid)

    def respond(self, sid: str) -> dict[str, Any] | None:
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                return None
            s["step"] = min(s["step"] + 1, len(s["script"]) - 1)
        return self._state(sid)

    def get(self, sid: str) -> dict[str, Any] | None:
        with self._lock:
            if sid not in self._sessions:
                return None
        return self._state(sid)

    def kill(self, sid: str) -> dict[str, Any] | None:
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                return None
            s["step"] = len(s["script"]) - 1
        state = self._state(sid)
        state["state"] = "expired"
        return state

    def _state(self, sid: str) -> dict[str, Any]:
        with self._lock:
            s = self._sessions[sid]
            step = dict(s["script"][s["step"]])
            transcript = "".join(x.get("emits", "") for x in s["script"][: s["step"] + 1])
            step.pop("emits", None)
            step.update(id=sid, user=s["user"], transcript=transcript)
            return step


LOGIN = MockLogin()


def _parse_decls(text: str) -> dict[str, str]:
    """Declaration text to an ordered property map."""
    out: dict[str, str] = {}
    for part in text.split(";"):
        prop, sep, value = part.partition(":")
        if sep and prop.strip():
            out[prop.strip()] = value.strip()
    return out


def _format_decls(decls: dict[str, str]) -> str:
    """One line for a short rule, one declaration per line for a long one."""
    parts = [f"{k}: {v}" for k, v in decls.items()]
    if len(parts) <= 3 and sum(len(p) for p in parts) < 70:
        return " " + "; ".join(parts) + "; "
    body = "".join(f"\n      {p};" for p in parts)
    return body + "\n    "


def write_style(selector: str, declarations: str) -> tuple[bool, str]:
    """Replace or merge one rule inside the <style> block of index.html, keeping a .bak.

    Refuses declaration text containing braces or angle brackets.
    """
    selector = selector.strip()
    declarations = declarations.strip().rstrip(";")
    if not SELECTOR_OK.match(selector):
        return False, "selector rejected"
    if DECL_FORBIDDEN.search(declarations):
        return False, "declaration rejected: braces and angle brackets are not allowed"

    html = INDEX.read_text()
    try:
        start = html.index(STYLE_OPEN) + len(STYLE_OPEN)
        end = html.index(STYLE_CLOSE)
    except ValueError:
        return False, "no style block found"

    block = html[start:end]
    pattern = re.compile(
        r"(^[ \t]*" + re.escape(selector) + r"[ \t]*\{)([^}]*)(\})", re.M
    )
    incoming = _parse_decls(declarations)
    match = pattern.search(block)

    if match:
        # Merge, never replace: the inspector sends only some properties. Existing order is kept.
        merged = _parse_decls(match.group(2))
        merged.update(incoming)
        new_block = pattern.sub(
            lambda m: m.group(1) + _format_decls(merged) + m.group(3), block, count=1
        )
        action = f"updated ({len(incoming)} of {len(merged)} declarations)"
    else:
        new_block = block.rstrip() + f"\n    {selector} {{{_format_decls(incoming)}}}\n  "
        action = "added"

    shutil.copy2(INDEX, INDEX.with_suffix(".html.bak"))
    INDEX.write_text(html[:start] + new_block + html[end:])
    log(f"{action} rule {selector!r}")
    return True, action


class Handler(BaseHTTPRequestHandler):
    server_version = "clinicalomxdb-ui-dev"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        q = parse_qs(url.query)
        path = url.path

        if path in ("/", "/index.html"):
            self._serve_index(q)
        elif path == "/health":
            # /health has no query string; the state comes from the cookie set with the document.
            name = (q.get("state") or [self._cookie("ui_state") or "healthy"])[0]
            payload = load_fixture("health", name)
            if payload is None:
                self._json(404, {"error": f"no fixture health-{name}.json"})
            elif payload == "OFFLINE":
                # The endpoint itself down.
                self.send_error(502, "fixture: health endpoint offline")
            else:
                self._json(200, payload)
        elif path == "/__dev/states":
            self._json(200, {"states": fixture_names(), "current": (q.get("state") or [""])[0]})
        elif path == "/__dev/events":
            self._events()
        elif path == "/__dev/inspector.js":
            self._raw(200, INSPECTOR.read_bytes(), "application/javascript")
        elif path == "/login/config":
            self._json(200, {"host": "hpc-login.example.edu (dev fixture)", "session_ttl": 300})
        elif path.startswith("/login/session/"):
            state = LOGIN.get(path.rsplit("/", 1)[-1])
            self._json(404, {"error": "no such session"}) if state is None else self._json(200, state)
        else:
            self._json(404, {"error": "not found", "hint": "dev server serves / and /health only"})

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        body = self._body()
        path = url.path

        if path == "/__dev/style":
            ok, message = write_style(str(body.get("selector", "")), str(body.get("declarations", "")))
            self._json(200 if ok else 400, {"ok": ok, "message": message})
        elif path == "/__dev/note":
            entry = {
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "selector": str(body.get("selector", ""))[:300],
                "state": str(body.get("state", ""))[:100],
                "note": str(body.get("note", ""))[:2000],
            }
            with NOTES.open("a") as fh:
                fh.write(json.dumps(entry) + "\n")
            log(f"note queued for {entry['selector']!r}")
            self._json(200, {"ok": True})
        elif path == "/login/session":
            script = str(body.get("script") or self._cookie("ui_login_script") or "password-prompt")
            self._json(201, LOGIN.start(str(body.get("user", "dev")), script))
        elif path.endswith("/respond"):
            sid = path.split("/")[3]
            state = LOGIN.respond(sid)
            self._json(404, {"error": "no such session"}) if state is None else self._json(200, state)
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        sid = urlparse(self.path).path.rsplit("/", 1)[-1]
        state = LOGIN.kill(sid)
        self._json(404, {"error": "no such session"}) if state is None else self._json(200, state)


    def _serve_index(self, q: dict[str, list[str]]) -> None:
        html = INDEX.read_text()
        _last_full["html"] = html
        state = (q.get("state") or ["healthy"])[0]
        login_script = (q.get("login") or ["password-prompt"])[0]
        auto_open = (q.get("open") or [""])[0]
        client = INSPECTOR.read_text() if INSPECTOR.is_file() else "// inspector.js missing"
        # shot mode drops the live-reload stream, which keeps headless Chrome from finishing the
        # page load, and keeps the dev toolbar out of screenshots.
        shot = (q.get("shot") or ["0"])[0] == "1"
        config = json.dumps(
            {"state": state, "login": login_script, "open": auto_open,
             "states": fixture_names(), "shot": shot}
        )
        # Contiguous from the marker: deleting from it to </body> must restore the committed file.
        injected = (
            f"<!-- injected by hub-ops/devserver: never present in the committed file -->\n"
            f"<script>window.__dev = {config};</script>\n<script>\n{client}\n</script>\n"
        )
        html = html.replace("</body>", injected + "</body>", 1)
        self._raw(
            200, html.encode(), "text/html; charset=utf-8",
            cookies={"ui_state": state, "ui_login_script": login_script},
        )

    def _events(self) -> None:
        q = WATCHER.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                try:
                    event, data = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                payload = json.dumps({"data": data})
                self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            WATCHER.unsubscribe(q)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}") if length else {}
        except (ValueError, json.JSONDecodeError):
            return {}

    def _cookie(self, name: str) -> str | None:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def _json(self, code: int, payload: Any) -> None:
        self._raw(code, json.dumps(payload, indent=2).encode(), "application/json")

    def _raw(
        self, code: int, body: bytes, content_type: str,
        cookies: dict[str, str] | None = None,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (cookies or {}).items():
            self.send_header("Set-Cookie", f"{name}={value}; Path=/; SameSite=Lax")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        if "__dev/events" not in self.path:
            sys.stderr.write(f"[ui-dev] {fmt % args}\n")


def main() -> None:
    # Writes to the working tree: never run on the VM.
    if Path("/run/ops-health").exists():
        raise SystemExit("[ui-dev] refusing to run on the control-plane VM: this edits the repo")
    if not INDEX.is_file():
        raise SystemExit(f"[ui-dev] portal not found at {INDEX}")
    FIXTURES.mkdir(exist_ok=True)

    WATCHER.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    states = ", ".join(fixture_names()) or "none yet"
    log(f"serving {INDEX} on http://{HOST}:{PORT}/")
    log(f"states: {states}")
    log(f"try:    http://{HOST}:{PORT}/?state=dead-socket")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
