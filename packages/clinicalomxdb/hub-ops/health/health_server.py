#!/usr/bin/env python3
"""Read-only health endpoint for the operator portal. Standard library only.

GET /health returns JSON tiles for the HPC ControlMaster sockets, host systemd units and
their restart rate, Prefect work pools, the pg-backup sentinel, and the LaminDB instance.
Runs as a host systemd unit on the host it watches, so it cannot report that host down.

It never opens a database connection or the Docker socket, caches results for the poll
interval, and never returns a credential.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

OK, WARN, FAIL, UNKNOWN = "ok", "warn", "fail", "unknown"
_RANK = {OK: 0, UNKNOWN: 1, WARN: 2, FAIL: 3}


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in _env(name, default).split(",") if part.strip())


@dataclass(frozen=True)
class Config:
    # Served on a unix socket only the portal can open. Set HEALTH_SOCKET_PATH empty for TCP.
    socket_path: str = _env("HEALTH_SOCKET_PATH", "/run/ops-health/health.sock")
    socket_mode: int = int(_env("HEALTH_SOCKET_MODE", "0660"), 8)
    bind_host: str = _env("HEALTH_BIND_HOST", "127.0.0.1")
    bind_port: int = int(_env("HEALTH_BIND_PORT", "8099"))
    cache_seconds: int = int(_env("HEALTH_CACHE_SECONDS", "15"))
    timeout: int = int(_env("HEALTH_TIMEOUT_SECONDS", "10"))

    # HPC connections: sockets found on disk, plus the users declared in users.conf.
    users_file: Path = Path(
        _env(
            "HEALTH_USERS_FILE",
            str(
                Path(__file__).resolve().parents[2]
                / "hub-pipelines/prefect/workers/hpc/users.conf"
            ),
        )
    )
    # Pins the user list and disables discovery.
    hpc_users: tuple[str, ...] = _csv("HEALTH_HPC_USERS", "")
    # hpc.login_host in site.yaml; ops-health.service sets it.
    hpc_host: str = _env("HEALTH_HPC_HOST", "")
    hpc_port: str = _env("HEALTH_HPC_PORT", "22")
    # ssh alias for `ssh -O check`, with "{user}" substituted. Matches the Prefect SSH block.
    hpc_alias_template: str = _env("HEALTH_HPC_ALIAS_TEMPLATE", "hpc-{user}")
    # The ssh_config ControlPath: %r remote user, %h hostname, %p port. Must match ssh_config
    # and the Prefect SSH block's control_master_path.
    control_path_template: str = _env("HEALTH_CONTROL_PATH_TEMPLATE", "~/.ssh/cm-%r@%h:%p")
    control_persist_hours: int = int(_env("HEALTH_CONTROL_PERSIST_HOURS", "96"))

    # Host units that are not per user. Per-user workers are reported per connection.
    # hpc-ssh-pool-worker.service is the shared-pool worker. ops-health is this program.
    units: tuple[str, ...] = _csv(
        "HEALTH_UNITS",
        "hpc-ssh-pool-worker.service,pipeline-pool-worker.service,vm-pool-worker.service,"
        "ops-login.service,ops-ttyd.service,clinicalomxdb.service",
    )
    # Restarts are counted over this window to detect crash loops.
    flap_window_seconds: int = int(_env("HEALTH_FLAP_WINDOW_SECONDS", "900"))
    flap_fail_count: int = int(_env("HEALTH_FLAP_FAIL_COUNT", "3"))
    recent_restart_seconds: int = int(_env("HEALTH_RECENT_RESTART_SECONDS", "120"))
    prefect_api: str = _env("HEALTH_PREFECT_API", "http://localhost:4200/api")
    # Per-user pools are POOL_PREFIX-USER, matching deploy.py's HPC_POOL_PREFIX.
    pool_prefix: str = _env("HEALTH_POOL_PREFIX", "hpc-ssh-pool")

    backup_dir: Path = Path(
        _env("HEALTH_BACKUP_DIR", "/mnt/data/artifacts/backups/postgres")
    )
    # The backup timer runs daily at 02:30.
    backup_max_age_hours: int = int(_env("HEALTH_BACKUP_MAX_AGE_HOURS", "26"))

    lamin_dir: Path = Path(_env("HEALTH_LAMIN_DIR", str(Path.home() / ".lamin")))


CONFIG = Config()


@dataclass
class Check:
    status: str
    summary: str
    detail: Any = field(default=None)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status, "summary": self.summary}
        if self.detail is not None:
            out["detail"] = self.detail
        return out


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Run a command without raising. Returns (rc, stdout, stderr); rc 124 on timeout."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=CONFIG.timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {CONFIG.timeout}s"
    except OSError as exc:
        return 127, "", str(exc)


def _http_json(url: str, payload: dict | None = None) -> tuple[bool, Any]:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=CONFIG.timeout) as resp:
            body = resp.read().decode()
        return True, json.loads(body) if body else None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return False, str(exc)


def _worst(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: _RANK.get(s, 1)) if statuses else UNKNOWN


# Work pools, fetched once per collect().
_pools_cache: tuple[bool, Any] | None = None


def _pools() -> tuple[bool, Any]:
    global _pools_cache
    if _pools_cache is None:
        _pools_cache = _http_json(f"{CONFIG.prefect_api}/work_pools/filter", payload={})
    return _pools_cache


def _declared_users() -> list[str]:
    """Users provisioned for submission, from users.conf. The first is primary."""
    if not CONFIG.users_file.is_file():
        return []
    out = []
    for line in CONFIG.users_file.read_text(errors="replace").splitlines():
        name = line.split("#", 1)[0].strip()
        if name:
            out.append(name)
    return out


def _discovered_users() -> list[str]:
    """Users with a ControlMaster socket on disk, parsed from the socket filenames."""
    prefix, _, suffix = _control_path_parts()
    found = []
    directory = Path(prefix).parent
    if not directory.is_dir():
        return found
    stem = Path(prefix).name
    for path in directory.iterdir():
        name = path.name
        if not path.is_socket():
            continue
        if name.startswith(stem) and name.endswith(suffix):
            user = name[len(stem) : len(name) - len(suffix)]
            if user:
                found.append(user)
    return sorted(found)


def _control_path_parts() -> tuple[str, str, str]:
    """Split the ControlPath template around %r, so a user can be substituted or recovered."""
    template = str(Path(CONFIG.control_path_template).expanduser())
    resolved = template.replace("%h", CONFIG.hpc_host).replace("%p", CONFIG.hpc_port)
    before, _, after = resolved.partition("%r")
    return before, "%r", after


def _connection_users() -> list[tuple[str, str]]:
    """(user, source) for every connection to report, declared and discovered alike."""
    if CONFIG.hpc_users:
        return [(u, "configured") for u in CONFIG.hpc_users]
    declared = _declared_users()
    discovered = _discovered_users()
    rows: list[tuple[str, str]] = [(u, "provisioned") for u in declared]
    rows += [(u, "logged in only") for u in discovered if u not in declared]
    return rows


def _control_path(user: str) -> Path:
    """Resolve the ControlPath template for one user, as ssh would. ~ is this process's HOME."""
    resolved = (
        CONFIG.control_path_template.replace("%r", user)
        .replace("%h", CONFIG.hpc_host)
        .replace("%p", CONFIG.hpc_port)
    )
    return Path(resolved).expanduser()


def check_connections() -> Check:
    """One row per HPC connection: is the SSH session live, and can the account run flows.

    Running flows also needs an ssh alias, a Prefect SSH block, a work pool and a worker.
    """
    pools_ok, pools = _pools()
    pool_status = {}
    if pools_ok and isinstance(pools, list):
        pool_status = {p.get("name"): p for p in pools}

    rows: list[dict[str, Any]] = []
    for user, source in _connection_users():
        alias = CONFIG.hpc_alias_template.format(user=user)
        socket = _control_path(user)
        pool_name = f"{CONFIG.pool_prefix}-{user}"
        unit = f"hpc-ssh-pool-worker@{user}.service"
        row: dict[str, Any] = {
            "user": user,
            "source": source,
            "alias": alias,
            "socket": str(socket),
            "pool": pool_name,
            "unit": unit,
        }

        present = socket.exists()
        row["socket_present"] = present
        if present:
            row["established_age_hours"] = round(
                (time.time() - socket.stat().st_mtime) / 3600, 1
            )
            row["control_persist_idle_timeout_hours"] = CONFIG.control_persist_hours

        # A discovered user has no alias yet, so the socket is checked directly.
        target = alias if source == "provisioned" else None
        if target:
            rc, _, err = _run(["ssh", "-O", "check", target])
        else:
            rc, _, err = _run(
                [
                    "ssh",
                    "-O",
                    "check",
                    "-o",
                    f"ControlPath={CONFIG.control_path_template}",
                    "-o",
                    f"User={user}",
                    CONFIG.hpc_host,
                ]
            )
        row["master_alive"] = rc == 0
        if rc != 0 and err:
            row["error"] = err

        pool = pool_status.get(pool_name)
        row["pool_present"] = pool is not None
        row["pool_status"] = (pool or {}).get("status")
        rc_unit, out_unit, _ = _run(
            ["systemctl", "show", unit, "--property=LoadState", "--property=ActiveState"]
        )
        props = dict(line.split("=", 1) for line in out_unit.splitlines() if "=" in line)
        row["worker_installed"] = rc_unit == 0 and props.get("LoadState") != "not-found"
        row["worker_active"] = props.get("ActiveState") == "active"

        submit_ready = row["master_alive"] and row["worker_active"] and row["pool_present"]
        row["submit_ready"] = submit_ready
        if submit_ready:
            row["status"] = OK
        elif not row["master_alive"]:
            row["status"] = FAIL
            row["hint"] = "no live session: use Log in on this row and complete DUO"
        else:
            row["status"] = WARN
            row["hint"] = (
                f"session live but not submit-ready: add {user} to users.conf and run "
                f"'make hpc-users' on the VM"
            )
        rows.append(row)

    if not rows:
        return Check(WARN, "no HPC connections", [])
    alive = sum(1 for r in rows if r["master_alive"])
    ready = sum(1 for r in rows if r["submit_ready"])
    return Check(
        _worst([r["status"] for r in rows]),
        f"{alive}/{len(rows)} session(s) live, {ready} submit-ready",
        rows,
    )


# Restart counts per unit, sampled on each poll and kept in memory only.
_restart_history: dict[str, list[tuple[float, int]]] = {}

# The command that installs each unit.
UNIT_HINTS = {
    "hpc-ssh-pool-worker.service": "make hpc-users",
    "pipeline-pool-worker.service": "sudo bash hub-pipelines/prefect/workers/hpc/bootstrap.sh",
    "vm-pool-worker.service": "sudo bash hub-pipelines/prefect/workers/vm-process/bootstrap.sh",
    "ops-login.service": "make ops-login",
    "ops-ttyd.service": "make ops-ssh",
    "clinicalomxdb.service": "written by cloud-init; needs a tofu apply -replace to reinstate",
}


def _restarts_in_window(unit: str, count: int) -> tuple[int, bool]:
    """Restarts within the window, and whether there is enough history to judge.

    A decreasing count means systemd reset it, and the history restarts.
    """
    now = time.monotonic()
    history = _restart_history.setdefault(unit, [])
    if history and count < history[-1][1]:
        history.clear()
    history.append((now, count))
    while len(history) > 1 and now - history[0][0] > CONFIG.flap_window_seconds:
        history.pop(0)
    return history[-1][1] - history[0][1], len(history) > 1


def _unit_uptime(props: dict[str, str]) -> float | None:
    """Seconds in the current active state, from the monotonic clock systemd reports against."""
    raw = props.get("ActiveEnterTimestampMonotonic")
    if not raw or raw == "0":
        return None
    try:
        with open("/proc/uptime") as fh:
            since_boot = float(fh.read().split()[0])
        return since_boot - int(raw) / 1_000_000
    except (OSError, ValueError):
        return None


def check_units() -> Check:
    """Host systemd units via `systemctl show`, with restart counts and uptime."""
    rows: list[dict[str, Any]] = []
    for unit in CONFIG.units:
        rc, out, err = _run(
            [
                "systemctl",
                "show",
                unit,
                "--property=LoadState",
                "--property=ActiveState",
                "--property=SubState",
                "--property=NRestarts",
                "--property=ActiveEnterTimestampMonotonic",
            ]
        )
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        try:
            restarts = int(props.get("NRestarts") or 0)
        except ValueError:
            restarts = 0
        uptime = _unit_uptime(props)

        row: dict[str, Any] = {
            "unit": unit,
            "load_state": props.get("LoadState"),
            "active_state": props.get("ActiveState"),
            "sub_state": props.get("SubState"),
            "restarts": restarts,
        }
        if uptime is not None:
            row["active_for_seconds"] = int(uptime)

        if rc != 0:
            row["status"] = UNKNOWN
            row["error"] = err or "systemctl show failed"
            rows.append(row)
            continue
        if props.get("LoadState") == "not-found":
            row["status"] = FAIL
            row["hint"] = f"unit not installed: {UNIT_HINTS.get(unit, 'run its bootstrap')}"
            rows.append(row)
            continue

        churn, have_baseline = _restarts_in_window(unit, restarts)
        row["restarts_in_window"] = churn
        row["window_seconds"] = CONFIG.flap_window_seconds
        if not have_baseline:
            row["note"] = "no restart baseline yet; this is the first poll since ops-health started"

        state = props.get("ActiveState")
        if state == "activating":
            row["status"] = WARN
            row["hint"] = f"restarting now; journalctl -u {unit} -n 50"
        elif state != "active":
            row["status"] = FAIL
            row["hint"] = f"journalctl -u {unit} -n 50"
        elif churn >= CONFIG.flap_fail_count:
            row["status"] = FAIL
            row["hint"] = (
                f"flapping: {churn} restarts in the last {CONFIG.flap_window_seconds // 60}m. "
                f"Active is misleading here; journalctl -u {unit} -n 100"
            )
        elif churn > 0:
            row["status"] = WARN
            row["hint"] = (
                f"restarted {churn} time(s) in the last {CONFIG.flap_window_seconds // 60}m; "
                f"journalctl -u {unit} -n 50"
            )
        elif uptime is not None and uptime < CONFIG.recent_restart_seconds and restarts > 0:
            row["status"] = WARN
            row["hint"] = f"started {int(uptime)}s ago after {restarts} restart(s)"
        else:
            row["status"] = OK
        rows.append(row)

    active = sum(1 for r in rows if r["active_state"] == "active")
    unstable = sum(1 for r in rows if r.get("restarts_in_window", 0) > 0)
    summary = f"{active}/{len(rows)} units active"
    if unstable:
        summary += f", {unstable} restarting"
    return Check(_worst([r["status"] for r in rows]), summary, rows)


def check_prefect() -> Check:
    """Prefect API liveness and work pool readiness, over HTTP only.

    A pool reports NOT_READY when no worker is polling it.
    """
    ok, body = _http_json(f"{CONFIG.prefect_api}/health")
    if not ok:
        return Check(
            FAIL, "Prefect API unreachable", {"api": CONFIG.prefect_api, "error": body}
        )

    pools_ok, pools = _pools()
    if not pools_ok or not isinstance(pools, list):
        return Check(
            WARN,
            "Prefect API up, work pools unreadable",
            {"api": CONFIG.prefect_api, "error": pools},
        )

    rows = [
        {
            "name": pool.get("name"),
            "type": pool.get("type"),
            "pool_status": (pool.get("status") or "UNKNOWN").upper(),
            "paused": pool.get("is_paused"),
            "status": OK if (pool.get("status") or "").upper() == "READY" else WARN,
        }
        for pool in pools
    ]
    ready = sum(1 for r in rows if r["pool_status"] == "READY")
    return Check(
        _worst([r["status"] for r in rows]) if rows else WARN,
        f"Prefect up, {ready}/{len(rows)} work pools ready",
        rows,
    )


def check_pg_backup() -> Check:
    """The pg-backup failure sentinel, and the age of the newest dump."""
    detail: dict[str, Any] = {"dir": str(CONFIG.backup_dir)}
    if not CONFIG.backup_dir.is_dir():
        return Check(UNKNOWN, "backup directory not present", detail)

    sentinel = CONFIG.backup_dir / "LAST_BACKUP_FAILED"
    if sentinel.exists():
        detail["sentinel"] = sentinel.read_text(errors="replace").strip()[:200]
        return Check(FAIL, "last pg-backup FAILED (sentinel present)", detail)

    dumps = sorted(
        CONFIG.backup_dir.glob("daily-*.sql.gz"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not dumps:
        return Check(FAIL, "no daily dump found", detail)

    newest = dumps[0]
    stat = newest.stat()
    age_hours = (time.time() - stat.st_mtime) / 3600
    detail.update(
        newest=newest.name,
        age_hours=round(age_hours, 1),
        size_mb=round(stat.st_size / 1_048_576, 1),
        daily_dumps=len(dumps),
    )
    if age_hours > CONFIG.backup_max_age_hours:
        return Check(
            WARN,
            f"newest dump is {age_hours:.0f}h old; check pg-backup.timer",
            detail,
        )
    return Check(OK, f"backup {age_hours:.0f}h old", detail)


def check_lamin() -> Check:
    """Connected LaminDB instance identity, read off disk.

    Identity fields only. The same file holds the Postgres password, which must never leave
    this host.
    """
    path = CONFIG.lamin_dir / "current_instance.env"
    if not path.is_file():
        return Check(
            FAIL,
            "no connected instance",
            {"path": str(path), "hint": "run lamin-connect.sh"},
        )

    wanted = {
        "lamindb_instance_owner": "owner",
        "lamindb_instance_name": "name",
        "lamindb_instance_id": "id",
        "lamindb_instance_schema_str": "schema",
    }
    detail: dict[str, Any] = {}
    for line in path.read_text(errors="replace").splitlines():
        key, _, value = line.partition("=")
        if (name := wanted.get(key.strip())) is not None:
            detail[name] = value.strip()

    if "name" not in detail:
        return Check(WARN, "instance file unparseable", {"path": str(path)})
    return Check(OK, f"{detail.get('owner', '?')}/{detail['name']}", detail)


CHECKS: dict[str, Callable[[], Check]] = {
    "connections": check_connections,
    "systemd_units": check_units,
    "prefect": check_prefect,
    "pg_backup": check_pg_backup,
    "lamin": check_lamin,
}


def collect() -> dict[str, Any]:
    global _pools_cache
    _pools_cache = None
    checks: dict[str, Any] = {}
    for name, fn in CHECKS.items():
        try:
            checks[name] = fn().as_dict()
        except Exception as exc:  # one broken check is reported, not raised
            checks[name] = {
                "status": UNKNOWN,
                "summary": f"check raised {type(exc).__name__}",
                "detail": {"error": str(exc)},
            }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cache_seconds": CONFIG.cache_seconds,
        "status": _worst([c["status"] for c in checks.values()]),
        "checks": checks,
    }


class _Cache:
    """Single shared result, refreshed at most once per cache_seconds. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._payload: dict[str, Any] | None = None
        self._at = 0.0

    def get(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if self._payload is None or now - self._at >= CONFIG.cache_seconds:
                self._payload = collect()
                self._at = now
            return self._payload


CACHE = _Cache()


class Handler(BaseHTTPRequestHandler):
    server_version = "clinicalomxdb-health"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.rstrip("/") not in ("/health", ""):
            self._respond(404, {"error": "not found", "routes": ["/health"]})
            return
        payload = CACHE.get()
        # Always 200; status is in the payload.
        self._respond(200, payload)

    def _respond(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def address_string(self) -> str:
        # client_address is an empty string on an AF_UNIX socket.
        peer = self.client_address
        return peer[0] if isinstance(peer, tuple) and peer else "unix"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[health] {self.address_string()} {fmt % args}\n")


class UnixHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer over AF_UNIX."""

    address_family = socket.AF_UNIX
    allow_reuse_address = False

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = 0


# Bytes. The kernel caps AF_UNIX paths just above this.
_SOCKET_PATH_MAX = 100


def _serve_unix(path: str) -> ThreadingHTTPServer:
    target = Path(path)
    if len(str(target).encode()) > _SOCKET_PATH_MAX:
        raise SystemExit(
            f"[health] socket path is {len(str(target).encode())} bytes, over the "
            f"~{_SOCKET_PATH_MAX} byte AF_UNIX limit: {target}\n"
            f"[health] use a shorter HEALTH_SOCKET_PATH, or set it empty to serve on TCP"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    # Remove a socket left by an unclean stop.
    if target.is_socket():
        target.unlink()
    elif target.exists():
        raise SystemExit(f"[health] refusing to replace non-socket path {target}")
    server = UnixHTTPServer(str(target), Handler)
    os.chmod(target, CONFIG.socket_mode)
    return server


def main() -> None:
    if not CONFIG.hpc_host:
        raise SystemExit("[health] HEALTH_HPC_HOST is unset: ops-health.service sets it from "
                         "hpc.login_host in site.yaml")
    if CONFIG.socket_path:
        server = _serve_unix(CONFIG.socket_path)
        where = f"unix:{CONFIG.socket_path} (mode {CONFIG.socket_mode:04o})"
    else:
        server = ThreadingHTTPServer((CONFIG.bind_host, CONFIG.bind_port), Handler)
        where = f"{CONFIG.bind_host}:{CONFIG.bind_port}"

    server.daemon_threads = True
    sys.stderr.write(
        f"[health] serving /health on {where} "
        f"(cache {CONFIG.cache_seconds}s, users {','.join(CONFIG.hpc_users)})\n"
    )
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
