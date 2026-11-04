#!/usr/bin/env python3
"""The operator portal's tab shell, served on a laptop, with the Build tab live.

Start it with scripts/dev/control-plane.sh and open http://localhost:8088/#build. Stop it with
Ctrl-C. Only the Build tab is real, on demo data; every other tab is a stand-in page. Set
PORTAL_PORT, REFLEX_PORT and REFLEX_BACKEND_PORT to move it off 8088, 3000 and 8000. It reads
packages/clinicalomxdb/hub-ops/site/index.html and writes nothing.
"""
from __future__ import annotations

import argparse
import html
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITE = Path(__file__).resolve().parents[2] / "packages/clinicalomxdb/hub-ops/site/index.html"

# One entry per portal tab this server has to answer for, keyed by the path prefix the tab's
# iframe requests. The Status and HPC tabs are rendered inline by the page itself and appear
# here only through /health below. Build is absent on purpose: it is the one tab with a real
# service behind it, and it is handled before this table is consulted.
#
# This table is a source of BETTER WORDS, never the list of what is answered: a path matching
# nothing here still gets the stand-in page, named after its own first segment. So a tab added
# to the portal renders a stand-in the day it lands, and an entry here is what upgrades that
# stand-in from a bare name to a sentence saying what the tab would have shown.
STANDINS = {
    "/prefect/": ("Prefect",
                  "Flow runs, deployments and work pools, from the hub VM's Prefect server."),
    "/mlflow/": ("MLflow", "Experiment tracking for the detector objectives."),
    "/review/": ("Review",
                 "The ROI triage board. Still a Streamlit app; a separate task owns"
                 " its migration."),
    "/explore/": ("Explore", "The LaminDB instance browsed through its own API rather than SQL."),
    "/schema-review/": ("Schema",
                        "Declared versus landed schema, rendered on demand by the"
                        " ops-schema-review host unit."),
    "/grafana/": ("Grafana", "Container cost and log search over Loki and Prometheus."),
    "/pgadmin/": ("pgAdmin", "Raw SQL against the instance Postgres."),
    "/ssh/": ("HPC terminal",
              "A real ssh session on the cluster, served by the ops-ttyd host unit."),
}

# The Status and HPC tabs both read this. It is FABRICATED, and the summaries say so on the
# page, because the real endpoint reads host systemd units and HPC ControlMaster sockets that
# a laptop does not have. It exists so those two tabs render their real layout instead of the
# "Health endpoint unreachable" box, which is what a missing route would give.
def _health() -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # Long, because nothing here changes: re-polling a fixture is pure noise in the log.
        "cache_seconds": 3600,
        "status": "unknown",
        "checks": {
            "connections": {
                "status": "unknown",
                "summary": "stand-in: no HPC access from a laptop",
                "detail": [{
                    "user": "example.user", "source": "stand-in", "socket_present": True,
                    "master_alive": True, "established_age_hours": 3, "worker_installed": True,
                    "worker_active": True, "pool_present": True, "pool_status": "READY",
                    "submit_ready": True,
                    "hint": "This row is fabricated by scripts/dev/portal.py so the tab renders.",
                }],
            },
            "systemd_units": {"status": "unknown",
                              "summary": "stand-in: no host units on a laptop"},
            "prefect": {"status": "unknown", "summary": "stand-in: no Prefect server reachable"},
            "pg_backup": {"status": "unknown", "summary": "stand-in: no backup directory"},
            "lamin": {"status": "ok", "summary": "stand-in: demo data, no instance connected"},
        },
    }


_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{name}</title>
<style>
  :root {{ --bg:#fff; --fg:#1a1a1a; --muted:#5a5a5a; --line:#d8d8d8; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#1c1c1e; --fg:#e8e8e8; --muted:#a0a0a0; --line:#3a3a3c; }}
  }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         font-family:system-ui,sans-serif; background:var(--bg); color:var(--fg); }}
  main {{ max-width:34rem; padding:2rem; border:1px solid var(--line); border-radius:6px; }}
  h1 {{ font-size:1.1rem; margin:0 0 0.5rem; }}
  p {{ color:var(--muted); font-size:0.9rem; line-height:1.55; margin:0 0 0.75rem; }}
  code {{ font-size:0.85em; }}
</style></head><body><main>
<h1>{name} is not running here</h1>
<p>{what}</p>
<p>This is the local control-plane dev server. It serves the real tab shell and the real
Build app; every other tab needs the hub VM, so it is answered by this page instead of
failing to load. See <code>scripts/dev/portal.py</code>.</p>
</main></body></html>
"""


_UNKNOWN = ("No stand-in is declared for this path, so it is a tab this dev server has not been"
            " told about. Add it to STANDINS in scripts/dev/portal.py to say what it would have"
            " shown.")

# A path's first segment is the service name in every route the portal asks for, so it is the
# heading where it reads as a name at all. Anything else - an empty path, a segment carrying
# punctuation or a URL's worth of length - gets the neutral heading instead of a mangled one.
def _name_for(path: str) -> str:
    segment = path.strip("/").split("/", 1)[0]
    if segment and len(segment) <= 32 and segment.replace("-", "").replace("_", "").isalnum():
        return segment
    return "That service"


class Handler(BaseHTTPRequestHandler):
    server_version = "clinicalomx-dev-portal"

    # One line per request is noise once the page is up; the shell polls /health on a timer.
    def log_message(self, fmt, *args):  # noqa: D102
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            # Read on every request, not once at startup: the shell is a file the operator edits
            # too, and a dev server that needs restarting to show an edit is the thing this is for.
            self._send(200, SITE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/build" or path.startswith("/build/"):
            self._send(302, b"", "text/plain", {"Location": self.server.reflex_url})
        elif path == "/health":
            self._send(200, json.dumps(_health()).encode(), "application/json")
        else:
            self._standin(path)

    do_HEAD = do_GET

    def do_POST(self) -> None:  # noqa: N802
        # The HPC tab's login relay. Its client renders `error` from a non-ok response, which
        # is the one place a stand-in is better served as JSON than as a page.
        body = json.dumps({"error": "The HPC login relay runs on the hub VM, not here."})
        self._send(503, body.encode(), "application/json")

    do_DELETE = do_POST

    def _standin(self, path: str) -> None:
        for prefix, (name, what) in STANDINS.items():
            if path == prefix.rstrip("/") or path.startswith(prefix):
                self._send_standin(name, what)
                return
        self._send_standin(_name_for(path), _UNKNOWN)

    def _send_standin(self, name: str, what: str) -> None:
        page = _PAGE.format(name=html.escape(name), what=html.escape(what))
        self._send(200, page.encode(), "text/html; charset=utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # Not 8080: that is the port `make tunnel` gives the real portal on this same laptop.
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--reflex-url", default="http://localhost:3000/")
    args = parser.parse_args()

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        raise SystemExit(
            f"port {args.port} is not free ({exc}); "
            f"rerun with PORTAL_PORT=<other> ./scripts/dev/control-plane.sh") from exc
    server.reflex_url = args.reflex_url
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        # Ctrl-C is the documented stop path, so it exits quietly.
        pass


if __name__ == "__main__":
    main()
