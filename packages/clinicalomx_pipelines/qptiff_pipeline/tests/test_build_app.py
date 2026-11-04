"""Build app: the Extract Explorer imported, live, with the orchestrator wired, from this package.

The page is checked by evaluating its Reflex component tree, as ``reflex export`` does. The compiled
address is checked in ``test_build_app_address.py``. Uses the dev fakes, so it shares the review
modules' fake-prefect collision.
"""
from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("reflex", reason="the Build app needs clinicalomx[app]")

from qptiff_pipeline.review.dev import fakes

fakes.install_modules()  # before the app module is imported

from qptiff_pipeline import build_app

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "qptiff_pipeline"
PKG = pathlib.Path(__file__).resolve().parents[1]

#: Module-level names the Explorer defines. A copy would bring them; an import does not.
EXPLORER_NAMES = ("MODEL_SLOTS", "ExplorerState", "declared_row", "blueprint", "here_panel")


def _built():
    """The app's whole component tree, evaluated. Catches structural faults the source hides."""
    page = next(iter(build_app.app._unevaluated_pages.values()))
    return page.component()


def _page() -> str:
    return str(_built().render())


def _walk(component):
    yield component
    for child in getattr(component, "children", []):
        yield from _walk(child)


def _controls(root, css_class: str) -> list:
    """Every control under `root` with a contract class and an on_change handler."""
    return [c for c in _walk(root)
            if css_class in str(getattr(c, "class_name", ""))
            and getattr(c, "event_triggers", {}).get("on_change") is not None]


# The app is the real Explorer, against the live instance.
def test_the_page_evaluates_without_raising() -> None:
    built = _built()
    assert str(built.render())

    aside = [c for c in _walk(built) if getattr(c, "tag", None) == "aside"]
    assert len(aside) == 1 and _controls(aside[0], "cx-select"), \
        "the Explorer's sidebar did not draw"
    assert _controls(built, "cx-select--key"), "the build panel did not draw"


def test_the_app_asks_the_explorer_for_the_live_instance() -> None:
    from clinicalomx.extract.app.web import state

    assert state.FORCED_LIVE is True


def test_the_app_registers_an_orchestrator_at_import() -> None:
    from clinicalomx.extract import submit as hop

    assert hop.can_submit() is True


def test_the_page_chrome_is_all_this_module_adds() -> None:
    composed = _page()
    assert "Build" in composed
    assert "Builds run on the HPC" in composed


# Nothing was copied.
def test_the_app_imports_the_explorer() -> None:
    source = (SRC / "build_app.py").read_text()
    assert "from clinicalomx.extract.app.web import make_app" in source
    assert build_app.app is not None


def test_no_explorer_code_is_copied_into_this_package() -> None:
    from clinicalomx.extract.app.web import state as explorer_state
    from clinicalomx.extract.app.web import view as explorer_view

    for name in EXPLORER_NAMES:
        assert hasattr(explorer_state, name) or hasattr(explorer_view, name), \
            f"{name} is no longer an Explorer name; fix this list"

    ours = "\n".join(p.read_text() for p in SRC.rglob("*.py"))
    for name in EXPLORER_NAMES:
        assert name not in ours, f"{name} looks copied from the Extract Explorer"


# The image carries the governance table, so it is built from this package.
def test_the_image_installs_this_distribution_and_not_only_clinicalomx() -> None:
    dockerfile = (PKG / "Dockerfile.build").read_text()
    assert 'uv pip install ".[build]"' in dockerfile
    assert "clinicalomx[app]" not in dockerfile, (
        "name the extra, not the package: the floor belongs in pyproject.toml alone")


def test_the_image_installs_graphviz_because_the_diagrams_need_a_binary() -> None:
    dockerfile = (PKG / "Dockerfile.build").read_text()
    assert "graphviz" in dockerfile


def test_the_image_compiles_the_frontend_at_build_time() -> None:
    dockerfile = (PKG / "Dockerfile.build").read_text()
    assert "reflex export --frontend-only" in dockerfile
    assert "AS builder" in dockerfile, "the toolchain must not reach the runtime stage"
    assert "build_serve:asgi" in dockerfile, "one process, one port, serving the built bundle"


def test_a_pipeline_output_key_is_readable_through_the_entry_point() -> None:
    from clinicalomx import governance as gov

    from qptiff_pipeline.domain import keys

    gov.reset_cache(registrations=True)
    assert keys.PIPELINE_ROOT in gov.discover()
    assert gov.permits_read(f"{keys.PIPELINE_ROOT}/slide/core.zarr")


# The panel is gone from the review app.
def test_the_review_app_no_longer_hosts_the_explorer() -> None:
    pytest.importorskip("streamlit")
    from qptiff_pipeline.review import app

    assert not hasattr(app, "_extract_panel")
    assert "clinicalomx.extract" not in (SRC / "review/app.py").read_text()


def test_the_selector_describes_stages_only() -> None:
    pytest.importorskip("streamlit")
    from qptiff_pipeline.review import stage_nav

    assert not hasattr(stage_nav, "TOOLS")
    assert not hasattr(stage_nav, "_render_tools")
    assert [s["stage"] for s in stage_nav.STEPS] == ["general", "core", "triage", "bgnorm"]


def test_an_unknown_stage_still_falls_back_to_general() -> None:
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(SRC / "review/dev/app.py"), default_timeout=180)
    at.session_state["stage"] = "extract"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["stage"] == "general"
