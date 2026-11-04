"""Every review app stage renders on fake data without exceptions, through Streamlit's AppTest.

Skipped when streamlit is not installed (``./dev_review_app.sh`` creates the dev venv).
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")

from clinicalomx import STATE_FEATURE
from streamlit.testing.v1 import AppTest

from qptiff_pipeline.domain import keys

# Absolute path: streamlit versions resolve relative paths differently.
APP = str(Path(__file__).resolve().parents[1] / "src/qptiff_pipeline/review/dev/app.py")
STAGES = ["general", "core", "triage", "bgnorm"]


def _run(stage: str) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=90)
    at.session_state["stage"] = stage             # what stage_nav.render() reads
    at.run()
    return at


def _assert_clean(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]
    # main() reports panel errors through st.error; treat those as failures too.
    assert not [e.value for e in at.error if "Unexpected error" in e.value], [e.value for e in at.error]


def _nav(at: AppTest) -> list:
    return [b for b in at.button if b.key and b.key.startswith("nav-")]


@pytest.mark.parametrize("stage", STAGES)
def test_stage_renders_without_exception(stage: str) -> None:
    at = _run(stage)
    _assert_clean(at)
    # the requested panel rendered
    assert at.session_state["stage"] == stage


def test_selector_is_one_numbered_row_in_pipeline_order() -> None:
    from qptiff_pipeline.review import stage_nav

    assert [s["stage"] for s in stage_nav.STEPS] == STAGES
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()  # no stage set: the container's first paint
    _assert_clean(at)
    nav = _nav(at)
    assert [b.key for b in nav] == [f"nav-{s}" for s in STAGES]
    assert [b.label.split(".")[0] for b in nav] == ["1", "2", "3", "4"]


@pytest.mark.parametrize("stage", STAGES)
def test_only_the_current_stage_is_marked(stage: str) -> None:
    at = _run(stage)
    assert [b.key for b in _nav(at) if b.proto.type == "primary"] == [f"nav-{stage}"]


def test_each_label_carries_its_queue() -> None:
    at = _run("general")
    labels = {b.key: b.label for b in _nav(at)}
    assert "pending" in labels["nav-general"] and "waiting" in labels["nav-triage"], labels


def test_nav_buttons_switch_stage() -> None:
    at = _run("general")
    at.button(key="nav-bgnorm").click().run()
    _assert_clean(at)
    assert at.session_state["stage"] == "bgnorm"


def test_accept_stores_a_typed_context_alongside_the_legacy_features() -> None:
    from qptiff_pipeline.domain import hitl, states
    from qptiff_pipeline.lamin import context_io
    from qptiff_pipeline.review import app
    from qptiff_pipeline.review.dev import fakes, fixtures

    fakes.install_modules()
    fakes.patch_app(app)
    uid, name = next(
        (u, r["name"]) for u, r in fixtures.RECORDS.items()
        if r["values"].get(STATE_FEATURE) == states.S.CORE_PENDING
    )
    import lamindb as ln  # the fake, installed above

    bundle = app.load_bundle(hitl.EDIT_PREFIX_BY_STAGE["core"], name)
    rec = ln.Record.get(uid=uid)
    boxes = [  # canvas shape after a dearray: one gridded cell and one inferred cell
        {"class": "full_core", "xyxy": [10, 10, 60, 60], "occupied": True, "rc": [0, 0], "bid": 0},
        {"class": "inferred_core", "xyxy": [70, 10, 120, 60], "occupied": False, "rc": [0, 1], "bid": 1},
    ]
    app._write_accepted_context(rec, "core", name, uid, boxes, bundle, modified=True)

    stored = rec.features.get_values()[context_io.context_feature("core")]
    assert isinstance(stored, str), "must be serialised: lamindb stores a string, not a dict"
    ctx = context_io.read_context(rec, "core", bundle=bundle)
    assert ctx.accepted is not None and ctx.accepted.modified is True
    assert [b.detected for b in ctx.accepted.boxes] == [True, False]
    assert [b.rc for b in ctx.accepted.boxes] == [(0, 0), (0, 1)]
    assert ctx.accepted.fully_gridded and not ctx.accepted.duplicate_cells()
    # the feature was declared
    assert context_io.context_feature("core") in fakes.declared_features()


def test_selector_counts_reflect_the_queues() -> None:
    from qptiff_pipeline.domain import states
    from qptiff_pipeline.review import app
    from qptiff_pipeline.review.dev import fakes, fixtures

    fakes.install_modules()
    fakes.patch_app(app)
    app._unit_states.clear()
    counts = app._stage_counts()
    assert counts["general"] == len(app.pending_units("general"))
    assert counts["bgnorm"] == len(app.pending_units("bgnorm"))
    assert counts["triage"] == sum(
        1 for r in fixtures.RECORDS.values()
        if r["type"] == states.TMA_TYPE and r["values"].get(STATE_FEATURE) in app.TRIAGE_STATES
    )


def test_title_and_no_log_console() -> None:
    at = _run("general")
    assert at.title[0].value == "qptiff pipeline"
    assert not [e for e in at.expander if "Log console" in e.label]
    assert not [t for t in at.get("text") if "Log console" in str(getattr(t, "value", ""))]


@pytest.mark.parametrize("module", [
    "app.py",             # the panels
    "stage_nav.py",  # the selector
    "dev/fixtures.py",    # the fake data
    "grid_links.py",  # a helper app.py imports by name
])
def test_edit_to_any_module_is_picked_up_on_the_next_rerun(module: str) -> None:
    import os
    from pathlib import Path

    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    assert not [c for c in at.caption if "reloaded" in c.value]  # first run: nothing to reload
    src = Path(__file__).resolve().parents[1] / "src/qptiff_pipeline/review" / module
    assert src.is_file(), src
    os.utime(src, None)  # same content, new mtime
    at.run()
    assert [c for c in at.caption if "reloaded" in c.value], f"{module} did not trigger a reload"
    assert not at.exception, [e.value for e in at.exception]


def test_triage_shows_the_approved_dearray() -> None:
    from qptiff_pipeline.domain import hitl, states
    from qptiff_pipeline.review import app
    from qptiff_pipeline.review.dev import fakes, fixtures

    fakes.install_modules()
    fakes.patch_app(app)
    uid, name = next(
        (u, r["name"]) for u, r in fixtures.RECORDS.items()
        if r["values"].get(STATE_FEATURE) == states.S.CORE_APPROVED
    )
    # The fixture stores the older `inferred` spelling; the reader converts it to `occupied`.
    boxes = app._accepted_or_raw_boxes(
        uid, app.load_bundle(hitl.EDIT_PREFIX_BY_STAGE["core"], name), "core")
    assert all(b.get("rc") is not None for b in boxes), "accepted boxes must carry grid row,col"
    assert any(b.get("occupied") is False for b in boxes), "fixture should include an inferred cell"
    assert not any("inferred" in b for b in boxes), "callers speak the canvas shape now"
    png = app._overlay_thumb_png(hitl.EDIT_PREFIX_BY_STAGE["core"], "core", name, uid)
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("has_context", [True, False])
def test_both_bundle_vintages_read_the_same_way(has_context: bool) -> None:
    from qptiff_pipeline.domain import hitl, states
    from qptiff_pipeline.lamin import context_io
    from qptiff_pipeline.review import app
    from qptiff_pipeline.review.dev import fakes, fixtures

    fakes.install_modules()
    fakes.patch_app(app)
    prefix = hitl.EDIT_PREFIX_BY_STAGE["core"]
    picked = None
    for uid, row in fixtures.RECORDS.items():
        if row["type"] != states.TMA_TYPE:
            continue
        bundle = fixtures.BUNDLES.get((prefix, keys.slug(row["name"])))
        if bundle and bool(bundle.get("context")) is has_context:
            picked = (uid, row["name"], bundle)
            break
    assert picked, f"no fixture bundle with context={has_context}"
    uid, _name, bundle = picked

    import lamindb as ln

    ctx = context_io.read_context(ln.Record.get(uid=uid), "core", bundle=bundle)
    assert ctx is not None
    assert len(ctx.raw.boxes) == len(bundle["boxes"])
    assert ctx.raw.space.width == bundle["width"]
    assert ctx.raw.space.scale_factor == bundle["scale_factor"]
    # model provenance exists only on the typed side
    assert (ctx.raw.model_uid is not None) is has_context


# The source-folder and governance filters on the general queue.
def test_the_general_queue_offers_a_folder_and_a_governance_filter() -> None:
    at = _run("general")
    _assert_clean(at)
    labels = {m.label for m in at.multiselect}
    assert {"Project folder", "Governance"} <= labels, labels

    folders = next(m for m in at.multiselect if m.label == "Project folder")
    assert set(folders.options) == {"projects", "external_projects"}, folders.options

    gov = next(m for m in at.multiselect if m.label == "Governance")
    # The level is the top step of the ladder: projects permits training, external_projects derive.
    assert set(gov.options) == {"train", "derive"}, gov.options


def test_deselecting_a_folder_narrows_the_queue() -> None:
    at = _run("general")
    before = [o for o in next(m for m in at.selectbox if "review" in m.label).options]

    folders = next(m for m in at.multiselect if m.label == "Project folder")
    folders.set_value(["projects"]).run()
    _assert_clean(at)

    after = [o for o in next(m for m in at.selectbox if "review" in m.label).options]
    assert len(after) < len(before), (before, after)
    assert all("Echo" not in o for o in after), after   # Echo is the external_projects slide


def test_the_filters_also_reach_the_core_queue() -> None:
    at = _run("core")
    _assert_clean(at)
    labels = {m.label for m in at.multiselect}
    assert {"Project folder", "Governance"} <= labels, labels
