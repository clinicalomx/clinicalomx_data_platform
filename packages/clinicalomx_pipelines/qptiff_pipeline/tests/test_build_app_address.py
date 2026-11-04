"""The three values the Build app's bundle is compiled with, and the service that must not restate
them.

Runs without reflex: ``rxconfig.py`` is parsed, not executed. Reflex compiles ``frontend_path``,
``backend_path`` and ``api_url`` into the bundle at build time. A wrong port loads the page but
never
connects. This is the only test of these values.
"""
from __future__ import annotations

import ast
import pathlib
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[3] / "clinicalomx_pipelines"


def _rxconfig_kwargs() -> dict:
    """The literal keywords `rxconfig.py` passes to `rx.Config`, read without running it."""
    tree = ast.parse((ROOT / "rxconfig.py").read_text())
    call = next(n for n in ast.walk(tree)
                if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "Config")
    return {k.arg: ast.literal_eval(k.value) for k in call.keywords
            if k.arg and isinstance(k.value, ast.Constant)}


def _declared_portal_port() -> int:
    """``OPS_PORTAL_PORT`` from the ops environment template. ``OPS_PORTAL_URL`` must agree."""
    env = (ROOT.parents[0] / "clinicalomxdb" / "hub-ops" / "environment.example").read_text()
    declared = {k: v.strip() for k, _, v in
                (ln.partition("=") for ln in env.splitlines() if not ln.startswith("#"))}
    port = int(declared["OPS_PORTAL_PORT"])
    url_port = urllib.parse.urlsplit(declared["OPS_PORTAL_URL"]).port
    assert url_port == port, (
        f"OPS_PORTAL_URL names port {url_port} while the portal is published on {port}")
    return port


def _service_environment(service: str) -> dict[str, str]:
    """One compose service's environment, with the mapping and list forms normalised."""
    import yaml  # a base dependency of this package

    path = ROOT.parents[0] / "clinicalomxdb" / "hub-pipelines" / "docker-compose.yml"
    declared = yaml.safe_load(path.read_text())["services"][service].get("environment", {})
    if isinstance(declared, dict):
        return {str(k): str(v) for k, v in declared.items()}
    pairs = (entry.split("=", 1) for entry in declared)
    return {p[0]: p[1] if len(p) == 2 else "" for p in pairs}


def test_the_websocket_port_is_the_portals_and_not_reflexs_default() -> None:
    api_url = _rxconfig_kwargs().get("api_url")
    assert api_url, (
        "rxconfig.py must set api_url: reflex's default bakes ws://<host>:8000 into the bundle, "
        "so the deployed app never reaches its own backend")

    ours = urllib.parse.urlsplit(api_url)
    portal_port = _declared_portal_port()
    assert ours.port == portal_port, (
        f"the bundle would ask for port {ours.port}; the portal serves this app on {portal_port}")
    assert ours.hostname == "localhost", (
        "only a localhost api_url is rewritten to the page's host by reflex's getBackendURL")


def test_the_reflex_project_names_this_app_and_the_build_prefix() -> None:
    kwargs = _rxconfig_kwargs()
    assert kwargs.get("app_module_import") == "qptiff_pipeline.build_app"
    assert kwargs.get("frontend_path") == "/build"
    assert kwargs.get("backend_path") == "/build", (
        "the websocket must land under the same prefix, or the forwarding Caddy handle covers "
        "the page and not the events")


def test_the_compose_service_sets_the_prefect_api_and_no_bundle_knob() -> None:
    env = _service_environment("build-app")
    for knob in ("REFLEX_FRONTEND_PATH", "REFLEX_API_URL", "STREAMLIT_SERVER_BASE_URL_PATH"):
        assert knob not in env, f"{knob} on the service cannot move a compiled bundle"
    assert env.get("PREFECT_API_URL") == "http://clinicalomxdb_prefect:4200/api", (
        "the app reads a deployment to size a job; with no address the client falls back to its "
        "default and every read fails as if nothing were registered")
