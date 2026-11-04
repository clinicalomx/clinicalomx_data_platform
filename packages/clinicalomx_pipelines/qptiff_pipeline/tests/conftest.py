"""Shared test scaffolding.

The ``load_*_module`` helpers load one source file under its real dotted name, behind bare stand-in
packages, so a test never runs the real package ``__init__``.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

# Give every session an empty PREFECT_HOME, set before any test imports prefect, so no machine
# state leaks in.
_PREFECT_HOME = tempfile.TemporaryDirectory(prefix="prefect-home-", ignore_cleanup_errors=True)
os.environ["PREFECT_HOME"] = _PREFECT_HOME.name

SRC = Path(__file__).resolve().parents[1] / "src"
_REVIEW = SRC / "qptiff_pipeline" / "review"
_FLOWS = SRC / "qptiff_pipeline" / "flows"


def _stub_packages() -> None:
    for name, path in (
        ("qptiff_pipeline", SRC / "qptiff_pipeline"),
        ("qptiff_pipeline.review", _REVIEW),
        ("qptiff_pipeline.flows", _FLOWS),
        ("qptiff_pipeline.domain", SRC / "qptiff_pipeline" / "domain"),
        ("qptiff_pipeline.lamin", SRC / "qptiff_pipeline" / "lamin"),
    ):
        mod = sys.modules.get(name)
        if mod is None:
            mod = types.ModuleType(name)
            mod.__path__ = [str(path)]  # a package, so submodule imports resolve
            sys.modules[name] = mod


def _load(dotted: str, path: Path):
    _stub_packages()
    if dotted in sys.modules:
        return sys.modules[dotted]
    spec = importlib.util.spec_from_file_location(dotted, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod  # register before exec, so sibling imports resolve
    spec.loader.exec_module(mod)
    return mod


def load_review_module(name: str):
    """Load ``review/<name>.py`` under its real dotted name, bypassing the package __init__."""
    return _load(f"qptiff_pipeline.review.{name}", _REVIEW / f"{name}.py")


def load_pipeline_module(name: str):
    """Load ``qptiff_pipeline/domain/<name>.py``, bypassing the package __init__.

    Raises in a bare checkout if a domain module imports lamindb.
    """
    _stub_packages()
    for pkg in ("domain", "lamin"):
        path = SRC / "qptiff_pipeline" / pkg / f"{name}.py"
        if path.exists():
            return _load(f"qptiff_pipeline.{pkg}.{name}", path)
    raise FileNotFoundError(f"no domain/ or lamin/ module named {name!r}")


def _stub_prefect() -> None:
    """Register a minimal ``prefect`` stand-in when the real one is not installed."""
    try:
        import prefect  # noqa: F401 - imported to PROBE availability, not to use
    except ImportError:
        pass
    else:
        return

    import logging

    mod = types.ModuleType("prefect")

    def flow(*args, **kwargs):
        if args and callable(args[0]) and not kwargs:
            return args[0]  # bare @flow
        return lambda fn: fn  # @flow(...)

    mod.flow = flow
    mod.task = flow
    mod.get_run_logger = lambda: logging.getLogger("qptiff_pipeline.test")
    mod.__path__ = []  # a package, so `from prefect.artifacts import ...` resolves
    sys.modules["prefect"] = mod

    # Flows import these at module level, so they must exist before a flow loads. To assert on one,
    # monkeypatch the attribute on the flow module.
    tasks = types.ModuleType("prefect.tasks")
    tasks.exponential_backoff = lambda backoff_factor=1: backoff_factor
    mod.tasks = tasks
    sys.modules["prefect.tasks"] = tasks

    artifacts = types.ModuleType("prefect.artifacts")
    artifacts.create_markdown_artifact = lambda **kw: None
    artifacts.create_table_artifact = lambda **kw: None
    artifacts.create_link_artifact = lambda **kw: None
    mod.artifacts = artifacts
    sys.modules["prefect.artifacts"] = artifacts

    # No orchestrator, so there is never a run context.
    context = types.ModuleType("prefect.context")
    context.FlowRunContext = type("FlowRunContext", (), {"get": staticmethod(lambda: None)})
    mod.context = context
    sys.modules["prefect.context"] = context


def load_flow_module(name: str):
    """Load ``flows/<name>.py`` under its real dotted name, bypassing the package __init__."""
    _stub_prefect()
    return _load(f"qptiff_pipeline.flows.{name}", _FLOWS / f"{name}.py")
