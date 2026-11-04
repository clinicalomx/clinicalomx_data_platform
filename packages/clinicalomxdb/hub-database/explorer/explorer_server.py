#!/usr/bin/env python3
"""The Explore tab: a LaminDB query surface for the operator portal.

Runs from the python-lamindb image as its own service on clinicalomxdb_net. Caddy reaches it
at clinicalomxdb_explorer:8600 and serves it under /explore/ with the prefix stripped, so
every URL the page requests is relative. The trailing slash is required.

Requests are threaded. lamin_api runs one database operation at a time and closes each
connection when done. The page never polls.

The app code is bind-mounted: a change needs a container restart, not an image rebuild.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
PORT = int(os.environ.get("EXPLORER_PORT", "8600"))

# The console evaluates Python. See _check_expression before changing this default.
CONSOLE_ENABLED = os.environ.get("EXPLORER_CONSOLE", "on").lower() not in ("off", "0", "false")

# Cancels a runaway query at the database.
STATEMENT_TIMEOUT_MS = int(os.environ.get("EXPLORER_STATEMENT_TIMEOUT_MS", "30000"))

sys.path.insert(0, str(HERE))
import lamin_api as api  # noqa: E402  (after sys.path so the bind mount is importable)


def _set_statement_timeout() -> None:
    """Cap query time at the database. No-op on SQLite."""
    try:
        from django.db import connection
        if connection.vendor != "postgresql":
            return
        with connection.cursor() as cur:
            cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
    except Exception:
        pass


class Denied(Exception):
    """Raised for a request the caller is not permitted to make."""


def _require_admin(headers) -> None:
    """Refuse the console to a caller whose X-Remote-Groups header lacks the admin group.

    With no header the request is allowed: the portal is then reachable only through an SSH tunnel.
    """
    groups = headers.get("X-Remote-Groups")
    if groups is None:
        return
    if "admins" not in [g.strip() for g in groups.split(",")]:
        raise Denied("the console is limited to admins")


def _check_expression(expr: str) -> ast.Expression:
    """Parse console input as a single expression and reject a few obvious escapes.

    This is not a sandbox. Anything that can call lamindb reaches the filesystem and the database
    as root. The real control is that the console is admin-only.
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"not a single expression: {e}") from None
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError(f"attribute {node.attr!r} is not available here")
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise ValueError(f"name {node.id!r} is not available here")
    return tree


def _console_namespace() -> dict:
    import lamindb as ln
    ns = {"ln": ln}
    try:
        import bionty as bt
        ns["bt"] = bt
    except Exception:
        pass
    return ns


def _result_payload(value, expression: str, elapsed: float, limit: int) -> dict:
    """Whatever the console returned, in the builder's table shape."""
    import pandas as pd
    from lamindb.models import SQLRecord

    if hasattr(value, "to_dataframe") and not isinstance(value, pd.DataFrame):
        value = value[:limit].to_dataframe() if hasattr(value, "__getitem__") else value.to_dataframe()
    if isinstance(value, pd.DataFrame):
        return api._frame_to_payload(value.head(limit), elapsed, expression, limit)
    if isinstance(value, SQLRecord):
        fields = {f.name: api._render(getattr(value, f.name, None))
                  for f in value._meta.concrete_fields if not f.name.startswith("_")}
        return {"columns": list(fields), "rows": [list(fields.values())], "count": 1,
                "truncated": False, "elapsed": round(elapsed, 3), "expression": expression}
    if isinstance(value, (list, tuple)):
        rows = [[api._render(v)] for v in list(value)[:limit]]
        return {"columns": ["value"], "rows": rows, "count": len(rows),
                "truncated": len(rows) >= limit, "elapsed": round(elapsed, 3),
                "expression": expression}
    return {"columns": ["value"], "rows": [[api._render(value)]], "count": 1, "truncated": False,
            "elapsed": round(elapsed, 3), "expression": expression}


def _console(expression: str, limit: int) -> dict:
    from django.db import close_old_connections
    tree = _check_expression(expression)
    try:
        _set_statement_timeout()
        started = time.monotonic()
        value = eval(compile(tree, "<console>", "eval"), _console_namespace())  # noqa: S307
        return _result_payload(value, expression, time.monotonic() - started, limit)
    finally:
        close_old_connections()


def _agent_available() -> bool:
    """Whether the Agent tab's dependencies are in the image."""
    try:
        import langgraph  # noqa: F401
        import openai  # noqa: F401
        return True
    except Exception:
        return False


ROUTES_GET = {
    "/api/registries": lambda h, q: api.list_registries(),
    "/api/features": lambda h, q: api.list_features(),
    "/api/entities": lambda h, q: api.list_entity_types(),
    "/api/config": lambda h, q: {"console": CONSOLE_ENABLED,
                                 "max_rows": api.MAX_ROWS,
                                 "default_rows": api.DEFAULT_ROWS,
                                 "instance": api.instance_slug(),
                                 "aggregates": api.AGGREGATES,
                                 "export_max": api.EXPORT_MAX_ROWS,
                                 "agent": _agent_available()},
}


class Handler(BaseHTTPRequestHandler):
    server_version = "clinicalomx-explorer"

    def log_message(self, fmt, *args):
        sys.stderr.write("explorer %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload, default=str).encode(), "application/json")

    def _error(self, exc: Exception) -> None:
        code = {LookupError: 404, ValueError: 400, Denied: 403}.get(type(exc), 500)
        if code == 500:
            traceback.print_exc()
        # Shown verbatim in the UI; lamindb's InvalidArgument lists every valid field.
        self._json({"error": f"{type(exc).__name__}: {exc}"}, code)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def _export_get(self, query: str) -> None:
        """CSV as a plain GET, so the control is a real link.

        Row count and truncation go in the filename. The query string matches a linkable query URL.
        """
        params = parse_qs(query, keep_blank_values=True)
        one = lambda k, d="": (params.get(k) or [d])[0]
        registry = one("registry") or "Artifact"
        # f=<group>:<field>:<lookup>:<value>. Without a numeric first segment, the group is 0.
        by_group: dict[int, list] = {}
        for raw in params.get("f", []):
            head, _, rest = raw.partition(":")
            if head.isdigit():
                index = int(head)
            else:
                index, rest = 0, raw
            field, _, tail = rest.partition(":")
            lookup, _, value = tail.partition(":")
            by_group.setdefault(index, []).append(
                {"field": field, "lookup": lookup or "exact", "value": value})
        modes = {}
        for raw in params.get("gm", []):           # gm=<group>:<all|any>
            index, _, mode = raw.partition(":")
            if index.isdigit():
                modes[int(index)] = mode or "all"
        groups = [{"match": modes.get(i, "all"), "filters": by_group[i]}
                  for i in sorted(by_group)] or None
        columns = [c for c in one("cols").split(",") if c] or None

        merge = [c for c in one("merge").split(",") if c] or None
        text, rows, truncated, dropped = api.export_csv(
            registry, None, one("features") == "1", one("match", "all") or "all", columns,
            merge, groups)
        payload = text.encode("utf-8")
        name = f"{registry.lower()}-{rows}rows{'-TRUNCATED' if truncated else ''}.csv"
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("X-Rows", str(rows))
        self.send_header("X-Truncated", "1" if truncated else "0")
        if dropped:
            self.send_header("X-Dropped-Columns", ",".join(dropped))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/export.csv":
                _set_statement_timeout()
                return self._export_get(parsed.query)
            if path in ROUTES_GET:
                return self._json(ROUTES_GET[path](self.headers, None))
            if path.startswith("/api/registry/") and path.endswith("/fields"):
                name = unquote(path[len("/api/registry/"):-len("/fields")])
                return self._json(api.registry_fields(name))
            if path.startswith("/api/record/"):
                rest = path[len("/api/record/"):].split("/", 1)
                if len(rest) != 2:
                    raise ValueError("expected /api/record/<registry>/<uid>")
                return self._json(api.record_detail(unquote(rest[0]), unquote(rest[1])))
            return self._static(path)
        except Exception as e:
            self._error(e)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/query":
                _set_statement_timeout()
                return self._json(api.run_query(
                    body["registry"], body.get("filters", []),
                    bool(body.get("features")), int(body.get("limit", api.DEFAULT_ROWS)),
                    body.get("match", "all"), body.get("feature_columns") or None,
                    body.get("groups") or None))
            if path == "/api/export":
                _set_statement_timeout()
                text, rows, truncated, dropped = api.export_csv(
                    body["registry"], body.get("filters", []), bool(body.get("features")),
                    body.get("match", "all"), body.get("columns") or None)
                payload = text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{body["registry"].lower()}.csv"')
                # Read by the page to report the row count and the cap.
                self.send_header("X-Rows", str(rows))
                self.send_header("X-Truncated", "1" if truncated else "0")
                if dropped:
                    self.send_header("X-Dropped-Columns", ",".join(dropped))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return self.wfile.write(payload)
            if path == "/api/values":
                # Distinct values of one field within the current result, via the unique aggregate.
                _set_statement_timeout()
                result = api.aggregate(
                    body["registry"], body.get("filters", []), body.get("field"), "unique",
                    body.get("match", "all"), body.get("groups") or None)
                return self._json({"values": [r[0] for r in result["rows"]],
                                   "truncated": result["truncated"],
                                   "elapsed": result["elapsed"]})
            if path == "/api/aggregate":
                _set_statement_timeout()
                return self._json(api.aggregate(
                    body["registry"], body.get("filters", []),
                    body.get("target") or None, body.get("op", "count"),
                    body.get("match", "all"), body.get("groups") or None))
            if path == "/api/agent":
                # One Agent turn. The API key is never stored, logged or written to disk.
                # Not admin-only: the agent calls only the Build tab's validated functions.
                import agent
                return self._json(agent.run(
                    body.get("messages") or [],
                    (body.get("base_url") or "").strip(),
                    body.get("api_key") or "",
                    (body.get("model") or "").strip(),
                    body.get("query") or None))
            if path == "/api/agent/stream":
                # Server-sent events over POST, keeping the API key out of URLs and access logs.
                import agent
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()
                for event in agent.run_stream(
                        body.get("messages") or [],
                        (body.get("base_url") or "").strip(),
                        body.get("api_key") or "",
                        (body.get("model") or "").strip(),
                        body.get("query") or None):
                    try:
                        self.wfile.write(
                            f"data: {json.dumps(event, default=str)}\n\n".encode())
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        # The tab was closed mid-turn; stop.
                        return
                return
            if path == "/api/console":
                if not CONSOLE_ENABLED:
                    raise Denied("the console is disabled on this deployment")
                _require_admin(self.headers)
                return self._json(_console(body.get("expression", ""),
                                           int(body.get("limit", api.DEFAULT_ROWS))))
            self._json({"error": "not found"}, 404)
        except Exception as e:
            self._error(e)

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC / name).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            return self._json({"error": "not found"}, 404)
        ctype = {".html": "text/html; charset=utf-8", ".css": "text/css",
                 ".js": "text/javascript"}.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)


def main() -> None:
    import lamindb as ln
    print(f"explorer: lamindb {ln.__version__}, console={'on' if CONSOLE_ENABLED else 'off'}",
          file=sys.stderr, flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
