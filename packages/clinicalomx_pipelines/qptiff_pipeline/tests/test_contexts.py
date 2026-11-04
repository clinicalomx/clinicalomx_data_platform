"""The typed detect, review and persist payloads in ``contexts``."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import load_pipeline_module

contexts = load_pipeline_module("contexts")

Box = contexts.Box
Space = contexts.Space
DetectionSet = contexts.DetectionSet
AcceptedSet = contexts.AcceptedSet
CoreDetectContext = contexts.CoreDetectContext
GeneralDetectContext = contexts.GeneralDetectContext

THUMB = Space(name="thumbnail", width=1000, height=800, scale_factor=16.0)
CROP = Space(name="crop", width=900, height=900, scale_factor=16.0, origin_x=120.0, origin_y=64.0)


def _core_ctx(**kw):
    raw = DetectionSet(boxes=[Box(cls="full_core", xyxy=(10, 10, 60, 60), conf=0.9)], space=CROP)
    return CoreDetectContext(unit_name="slide:TMA0", raw=raw, **kw)


# The occupancy bit.
@pytest.mark.parametrize(("legacy", "expected"), [
    ({"class": "full_core", "xyxy": [0, 0, 1, 1], "is_detected": True}, True),
    ({"class": "full_core", "xyxy": [0, 0, 1, 1], "is_detected": False}, False),
    ({"class": "full_core", "xyxy": [0, 0, 1, 1], "inferred": True}, False),
    ({"class": "full_core", "xyxy": [0, 0, 1, 1], "inferred": False}, True),
    ({"class": "full_core", "xyxy": [0, 0, 1, 1], "occupied": True}, True),
    ({"class": "full_core", "xyxy": [0, 0, 1, 1], "occupied": False}, False),
    ({"class": "inferred_core", "xyxy": [0, 0, 1, 1]}, False),          # class as the last resort
    ({"class": "full_core", "xyxy": [0, 0, 1, 1]}, True),               # nothing said -> detected
])
def test_legacy_occupancy_spellings_collapse_to_detected(legacy, expected) -> None:
    assert Box.from_legacy(legacy).detected is expected


def test_explicit_flag_beats_the_class_name() -> None:
    assert Box.from_legacy({"class": "inferred_core", "xyxy": [0, 0, 1, 1], "is_detected": True}).detected
    assert not Box.from_legacy({"class": "full_core", "xyxy": [0, 0, 1, 1], "inferred": True}).detected


def test_row_col_columns_become_rc() -> None:
    assert Box.from_legacy({"class": "c", "xyxy": [0, 0, 1, 1], "row": 3, "col": 4}).rc == (3, 4)
    assert Box.from_legacy({"class": "c", "xyxy": [0, 0, 1, 1], "rc": [3, 4]}).rc == (3, 4)
    assert Box.from_legacy({"class": "c", "xyxy": [0, 0, 1, 1]}).rc is None


def test_legacy_round_trip_back_out() -> None:
    b = Box.from_legacy({"class": "inferred_core", "xyxy": [1, 2, 3, 4], "inferred": True, "rc": [0, 5]})
    assert b.to_corrected_json() == {
        "class": "inferred_core", "xyxy": [1.0, 2.0, 3.0, 4.0], "inferred": True, "rc": [0, 5]}
    assert b.to_canvas() == {
        "class": "inferred_core", "xyxy": [1.0, 2.0, 3.0, 4.0], "occupied": False, "rc": [0, 5],
        "bid": None}


# Coordinate spaces.
def test_general_transform_is_a_plain_multiply() -> None:
    assert THUMB.to_full_res((10, 20, 30, 40)) == (160.0, 320.0, 480.0, 640.0)


def test_core_transform_offsets_then_scales() -> None:
    assert CROP.to_full_res((10, 20, 30, 40)) == (
        (10 + 120) * 16.0, (20 + 64) * 16.0, (30 + 120) * 16.0, (40 + 64) * 16.0)


def test_degenerate_space_is_rejected() -> None:
    with pytest.raises(ValueError):
        Space(name="crop", width=0, height=10)
    with pytest.raises(ValueError):
        Space(name="crop", width=10, height=10, scale_factor=0)


def test_reversed_box_is_normalised() -> None:
    assert Box(cls="c", xyxy=(60, 60, 10, 20)).xyxy == (10.0, 20.0, 60.0, 60.0)


# The envelope.
def test_json_round_trip_preserves_everything() -> None:
    ctx = _core_ctx(
        accepted=AcceptedSet(
            boxes=[Box(cls="full_core", xyxy=(10, 10, 60, 60), detected=True, rc=(0, 0), bid=1),
                   Box(cls="inferred_core", xyxy=(70, 10, 120, 60), detected=False, rc=(0, 1), bid=2)],
            space=CROP, modified=True),
        grid=contexts.GridShape(n_rows=1, n_cols=2),
        links=[contexts.GridLink(a=1, b=2, axis="row")],
    )
    again = contexts.parse_context("core", ctx.to_json())
    assert again == ctx


def test_class_keeps_its_json_key() -> None:
    payload = json.loads(_core_ctx().to_json())
    assert payload["raw"]["boxes"][0]["class"] == "full_core"
    assert "cls" not in payload["raw"]["boxes"][0]
    # parsing accepts the wire name
    assert contexts.parse_context("core", payload).raw.boxes[0].cls == "full_core"


def test_never_accepted_sentinel_survives() -> None:
    ctx = _core_ctx()
    assert ctx.accepted is None and not ctx.is_accepted
    assert contexts.parse_context("core", ctx.to_json()).accepted is None
    assert ctx.boxes == ctx.raw.boxes  # display falls back to the raw detections


def test_image_bytes_only_travel_in_the_transport_dump() -> None:
    ctx = _core_ctx()
    ctx.images.append(contexts.ImageRef(kind="clean", b64="QUJD", ref=contexts.ArtifactRef(key="a/b.png")))
    durable = json.loads(ctx.to_json())
    assert "b64" not in durable["images"][0]  # dropped entirely, not nulled
    assert durable["images"][0]["ref"]["key"] == "a/b.png"  # the pointer stays
    assert json.loads(ctx.to_json(with_image_bytes=True))["images"][0]["b64"] == "QUJD"
    # the durable form still parses; b64 falls back to its default
    assert contexts.parse_context("core", durable).images[0].b64 is None


def test_accepted_space_must_match_raw() -> None:
    with pytest.raises(ValueError, match="space"):
        _core_ctx(accepted=AcceptedSet(boxes=[], space=THUMB))


def test_duplicate_cells_are_reported_not_rejected() -> None:
    acc = AcceptedSet(space=CROP, boxes=[
        Box(cls="full_core", xyxy=(0, 0, 1, 1), rc=(0, 0)),
        Box(cls="full_core", xyxy=(2, 2, 3, 3), rc=(0, 0)),
        Box(cls="full_core", xyxy=(4, 4, 5, 5), rc=(0, 1)),
    ])
    assert acc.duplicate_cells() == [(0, 0)]


def test_fully_gridded_drives_the_legacy_refit() -> None:
    gridded = AcceptedSet(space=CROP, boxes=[Box(cls="c", xyxy=(0, 0, 1, 1), rc=(0, 0))])
    partial = AcceptedSet(space=CROP, boxes=[Box(cls="c", xyxy=(0, 0, 1, 1))])
    assert gridded.fully_gridded and not partial.fully_gridded
    assert not AcceptedSet(space=CROP).fully_gridded  # empty is not "gridded"


def test_unknown_fields_are_ignored_not_fatal() -> None:
    future = json.loads(_core_ctx().to_json())
    future["some_field_from_a_newer_build"] = {"nested": 1}
    future["raw"]["boxes"][0]["new_box_field"] = "x"
    ctx = contexts.parse_context("core", future)   # must not raise
    assert ctx.raw.boxes[0].cls == "full_core"

    # A legacy key on Box itself does nothing: from_legacy is the only way in.
    assert Box(cls="c", xyxy=(0, 0, 1, 1), occupied=False).detected is True
    assert Box.from_legacy({"class": "c", "xyxy": [0, 0, 1, 1], "occupied": False}).detected is False


def test_accepted_default_must_stay_none() -> None:
    field = contexts.CoreDetectContext.model_fields["accepted"]
    assert field.default is None and field.default_factory is None
    assert contexts.Box.model_fields["detected"].default is True


def test_general_and_core_stages_are_tagged() -> None:
    assert GeneralDetectContext(unit_name="s", raw=DetectionSet(space=THUMB)).stage == "general"
    assert _core_ctx().stage == "core"
    assert json.loads(_core_ctx().to_json())["schema_version"] == contexts.SCHEMA_VERSION


def test_artifact_ref_needs_a_selector() -> None:
    with pytest.raises(ValueError):
        contexts.ArtifactRef()
    assert contexts.ArtifactRef(key="a/b.parquet").kind == "artifact"


# Import discipline.
def test_module_imports_with_only_pydantic() -> None:
    path = Path(contexts.__file__)
    probe = (
        "import importlib.util, sys;"
        f"spec = importlib.util.spec_from_file_location('probe_contexts', r'{path}');"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
        "banned = [n for n in ('lamindb', 'prefect', 'geopandas', 'shapely', 'pandas', 'numpy',"
        " 'matplotlib', 'streamlit') if n in sys.modules];"
        "print('BANNED:' + ','.join(banned)); sys.exit(1 if banned else 0)"
    )
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=False)
    assert r.returncode == 0, f"{r.stdout.strip()} {r.stderr.strip()}"


# The review transport (context_io.transport_payload).
def _load_context_io():
    """Load context_io through conftest's stub packages."""
    return load_pipeline_module("context_io")


def test_transport_payload_keeps_every_legacy_key() -> None:
    io_ = _load_context_io()
    ctx = _core_ctx()
    payload = io_.transport_payload(ctx, image_b64="QUJD")

    assert set(payload) >= {"scale_factor", "width", "height", "boxes", "image_b64",
                            "crop_x", "crop_y"}
    assert payload["scale_factor"] == 16.0
    assert payload["width"] == 900 and payload["height"] == 900
    assert payload["crop_x"] == 120.0 and payload["crop_y"] == 64.0
    assert payload["image_b64"] == "QUJD"
    assert payload["boxes"] == [{"class": "full_core", "conf": 0.9, "xyxy": [10.0, 10.0, 60.0, 60.0]}]
    # the typed context rides alongside, without a second copy of the image bytes
    assert payload["context"]["stage"] == "core"
    assert "b64" not in payload["context"]["images"][0] if payload["context"]["images"] else True


def test_general_payload_has_no_crop_offset() -> None:
    io_ = _load_context_io()
    ctx = GeneralDetectContext(
        unit_name="slide", raw=DetectionSet(boxes=[Box(cls="TMA", xyxy=(1, 2, 3, 4))], space=THUMB))
    payload = io_.transport_payload(ctx, image_b64="x")
    assert "crop_x" not in payload and "crop_y" not in payload
    assert payload["boxes"] == [{"class": "TMA", "xyxy": [1.0, 2.0, 3.0, 4.0]}]  # conf absent, as before


def test_payload_round_trips_back_into_a_context() -> None:
    io_ = _load_context_io()
    payload = io_.transport_payload(_core_ctx(), image_b64="x")
    again = contexts.parse_context("core", payload["context"])
    assert [b.xyxy for b in again.raw.boxes] == [(10.0, 10.0, 60.0, 60.0)]
    assert again.raw.space.origin_x == 120.0  # the crop offset travels typed, not as a loose key
