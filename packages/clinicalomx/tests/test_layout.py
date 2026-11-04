"""Tests for the layout preview: no pixel is read, and every number matches the builder's.

Driven by the demo Snapshot with real files on disk. Skipped without the `extract` extra.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("xarray", reason="the layout preview needs clinicalomx[extract]")
pytest.importorskip("geopandas", reason="the layout preview needs clinicalomx[extract]")

import dask.array as da  # noqa: E402
import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402
import tifffile  # noqa: E402
import xarray as xr  # noqa: E402
from dask.callbacks import Callback  # noqa: E402
from shapely.geometry import box  # noqa: E402

from clinicalomx.extract import Element, Recipe, register_recipes, reset_registrations  # noqa: E402
from clinicalomx.extract.app import demo  # noqa: E402
from clinicalomx.extract.builders.imaging import reset_cache  # noqa: E402
from clinicalomx.extract.layout import layout_of  # noqa: E402
from clinicalomx.extract.plan import plan as make_plan  # noqa: E402
from clinicalomx.extract.request import (  # noqa: E402
    Destination,
    ElementSpec,
    ExtractRequest,
    ROISelector,
)
from clinicalomx.extract.source import Artifact  # noqa: E402

C, Y, X = 4, 600, 700
#: The box the fixture parquet gives every core, unpadded.
CORE_BOX = (100.0, 200.0, 300.0, 400.0)


class _Watch(Callback):
    """The largest array dask materialises, in elements. A coordinate read is allowed."""

    def __init__(self):
        self.biggest = 0

    def _posttask(self, key, result, dsk, state, worker_id):
        self.biggest = max(self.biggest, int(getattr(result, "size", 0) or 0))


class _Files:
    """A Source over a dict of key -> real path."""

    def __init__(self, paths):
        self.paths = {k: str(v) for k, v in paths.items()}
        self.asked: list[str] = []

    def slug(self):
        return "fixture"

    def entities(self, selection):
        return []

    def entity(self, **_kw):
        return None

    def artifacts(self, artifact_ids):
        out = []
        for key in artifact_ids:
            self.asked.append(key)
            if key in self.paths:
                out.append(Artifact(artifact_id=key, uri=self.paths[key], stored=True))
        return out


def _bgnormed(path):
    """A bgnormed zarr: two float64 adjusted images, plus `labels` and `probs`."""
    ds = xr.Dataset(
        {
            "adjusted_image": (("c", "y", "x"), da.zeros((C, Y, X), chunks=(1, 128, 128),
                                                         dtype="f8")),
            "adjusted_image_post_hoc": (("c", "y", "x"),
                                        da.ones((C, Y, X), chunks=(1, 128, 128), dtype="f8")),
            "labels": (("c", "y", "x"), da.zeros((C, Y, X), chunks=(1, 128, 128), dtype="u1")),
            "probs": (("c", "c_probs", "y", "x"),
                      da.zeros((C, 3, Y, X), chunks=(1, 3, 128, 128), dtype="f4")),
        },
        coords={
            "c": [f"CD{i}" for i in range(C)],
            "color_rgb": ("c", [f"#{i:02x}00ff" for i in range(C)]),
            "emission_wavelength_nm": ("c", [520.0 + i for i in range(C)]),
            "fluorophore": ("c", [f"Opal{500 + i}" for i in range(C)]),
        },
    )
    ds["adjusted_image"].attrs["image_info"] = {"scale_factor": 0.4977}
    ds.to_zarr(path, mode="w")
    return path


def _grid(path):
    """The dearray parquet, stamped with frame metadata."""
    gpd.GeoDataFrame(
        {"roi_id": [f"R0C{i}" for i in range(demo.N_CORES)]},
        geometry=[box(*CORE_BOX) for _ in range(demo.N_CORES)],
    ).to_parquet(path)
    table = pq.read_table(path)
    meta = dict(table.schema.metadata or {})
    meta[b"roi_attrs"] = json.dumps({"scale_factor": 1.0}).encode()
    pq.write_table(table.replace_schema_metadata(meta), path)
    return path


@pytest.fixture
def source(tmp_path):
    """Every demo artifact key this fixture can put real bytes behind."""
    raw = tmp_path / "slide.qptiff"
    tifffile.imwrite(raw, np.zeros((C, Y, X), dtype="uint16"))
    paths = {
        f"raw/{demo.SLIDE_NAME}.qptiff": raw,
        f"detections/{demo.TMA_NAME}-grid.parquet": _grid(tmp_path / "grid.parquet"),
        "restricted/denied-core.zarr": tmp_path / "never-opened.zarr",
    }
    for i in demo.BGNORMED_CORES:
        paths[f"bgnorm/{demo.TMA_NAME}:R0C{i}.zarr"] = _bgnormed(tmp_path / f"c{i}.zarr")
    reset_cache()
    return _Files(paths)


@pytest.fixture
def snap():
    demo.install_governance()
    return demo.build_snapshot()


def _plan(snap, roi, recipe, **kw):
    request = ExtractRequest(
        roi=ROISelector(uid=roi.uid), recipe=recipe, target=_target_of(recipe),
        destination=Destination(kind="path", path="./out"), **kw)
    return make_plan(request, snapshot=snap)


def _target_of(recipe: str) -> str:
    return "OME-TIFF" if recipe.startswith("ometiff") else "SpatialData"


def _core(snap, index=0):
    return snap.record_by_name("Core", f"{demo.TMA_NAME}:R0C{index}")


def _tma(snap):
    return snap.record_by_name("TMA", demo.TMA_NAME)


def _piece(layout, key):
    return next(p for p in layout.pieces if p.key == key)


#: `crop_box` of `CORE_BOX`, computed so the float padding artifact is not hardcoded.
def _expected_extent():
    from clinicalomx.extract.coords import crop_box

    x0, y0, x1, y1 = crop_box(CORE_BOX, width=X, height=Y)
    return (y1 - y0, x1 - x0)


# No pixel is read.
def test_no_pixel_is_read(snap, source):
    from clinicalomx.extract.builders.imaging import _MAX_COORD_VALUES

    watch = _Watch()
    with watch:
        layout = layout_of(_plan(snap, _core(snap), "ometiff.core"), snapshot=snap, source=source)
    assert layout.measured, "the test is vacuous unless it actually measured something"
    assert watch.biggest <= _MAX_COORD_VALUES


def test_the_watch_would_have_caught_a_real_read(snap, source):
    from clinicalomx.extract.builders.imaging import _MAX_COORD_VALUES, crop, open_image

    watch = _Watch()
    with watch:
        crop(open_image(source.paths[f"bgnorm/{demo.TMA_NAME}:R0C0.zarr"],
                        variable="adjusted_image"), CORE_BOX)
    assert watch.biggest > _MAX_COORD_VALUES


def test_a_denied_store_is_never_opened(snap, source):
    layout = layout_of(_plan(snap, _core(snap, demo.DENIED_CORE), "ometiff.core"),
                       snapshot=snap, source=source)
    assert "restricted/denied-core.zarr" not in source.asked
    assert {a.state for a in layout.absences if a.element == "bgnorm"} == {"denied"}


# A full plan.
def test_a_full_plan_shows_the_series_it_will_write(snap, source):
    layout = layout_of(_plan(snap, _core(snap), "ometiff.core"), snapshot=snap, source=source)
    assert [p.key for p in layout.pieces] == ["raw", "bgnorm", "bgnormQ"]
    assert all(p.slot == "series" for p in layout.pieces)


def test_every_series_carries_its_channels_dimensions_and_dtype(snap, source):
    layout = layout_of(_plan(snap, _core(snap), "ometiff.core"), snapshot=snap, source=source)
    piece = _piece(layout, "bgnorm")
    assert piece.shape == (C, *_expected_extent())
    assert piece.channels == tuple(f"CD{i}" for i in range(C))
    assert piece.px_um == 0.4977
    assert piece.dims == ("c", "y", "x")


def test_the_dtype_is_what_the_writer_stores_not_what_the_source_holds(snap, source):
    layout = layout_of(_plan(snap, _core(snap), "ometiff.core"), snapshot=snap, source=source)
    assert _piece(layout, "bgnorm").dtype == "float32"
    assert _piece(layout, "raw").dtype == "uint16"


def test_every_piece_carries_what_it_will_cost(snap, source):
    layout = layout_of(_plan(snap, _core(snap), "ometiff.core"), snapshot=snap, source=source)
    piece = _piece(layout, "bgnorm")
    assert piece.size_bytes == int(np.prod(piece.shape)) * np.dtype(piece.dtype).itemsize
    assert _piece(layout, "raw").size_bytes < piece.size_bytes, "uint16 is half of float32"


def test_a_spatialdata_keeps_the_source_dtype_that_an_ome_tiff_halves(snap, source):
    ome = layout_of(_plan(snap, _core(snap), "ometiff.core"), snapshot=snap, source=source)
    sdata = layout_of(_plan(snap, _core(snap), "spatialdata.core"), snapshot=snap, source=source)
    assert _piece(ome, "bgnorm").dtype == "float32"
    assert _piece(sdata, "bgnorm").dtype == "float64"


def test_each_element_is_visibly_placed_in_the_layout(snap, source):
    piece = _piece(layout_of(_plan(snap, _core(snap), "ometiff.core"), snapshot=snap,
                             source=source), "bgnormQ")
    assert piece.element == "bgnormQ"
    assert piece.role == "bgnormed"
    assert piece.variable == "adjusted_image_post_hoc"
    assert piece.source == f"bgnorm/{demo.TMA_NAME}:R0C0.zarr"


def test_two_series_from_one_store_name_the_same_artifact(snap, source):
    layout = layout_of(_plan(snap, _core(snap), "ometiff.core"), snapshot=snap, source=source)
    assert _piece(layout, "bgnorm").source == _piece(layout, "bgnormQ").source
    assert _piece(layout, "bgnorm").variable != _piece(layout, "bgnormQ").variable


def test_the_ome_model_is_the_builders_own(snap, source):
    from ome_types import to_xml

    from clinicalomx.extract.builders.imaging import ChannelMeta
    from clinicalomx.extract.builders.ometiff import to_ome

    layout = layout_of(_plan(snap, _core(snap), "ometiff.core"), snapshot=snap, source=source)
    arrays = [(p.key, np.zeros(p.shape, dtype=p.dtype), p.dtype) for p in layout.pieces]
    meta = ChannelMeta(names=_piece(layout, "bgnorm").channels,
                       colors=tuple(f"#{i:02x}00ff" for i in range(C)),
                       emission_nm=tuple(520.0 + i for i in range(C)),
                       fluorophore=tuple(f"Opal{500 + i}" for i in range(C)),
                       px_um=0.4977, source="zarr:adjusted_image")
    assert layout.ome_xml == to_xml(to_ome(arrays, meta))


def test_the_ome_model_tells_a_reader_where_each_series_starts(snap, source):
    layout = layout_of(_plan(snap, _core(snap), "ometiff.core"), snapshot=snap, source=source)
    assert 'IFD="0"' in layout.ome_xml and f'IFD="{C}"' in layout.ome_xml
    assert 'Name="bgnormQ"' in layout.ome_xml
    assert 'Name="CD0"' in layout.ome_xml


def test_the_reads_it_cost_are_reported(snap, source):
    layout = layout_of(_plan(snap, _core(snap), "ometiff.core"), snapshot=snap, source=source)
    assert layout.reads > 0
    assert layout.measured_on == f"{demo.TMA_NAME}:R0C0"


# A degraded plan.
def test_an_absence_is_described_as_an_effect_on_the_output(snap, source):
    layout = layout_of(_plan(snap, _tma(snap), "ometiff.tma.per-core"), snapshot=snap,
                       source=source)
    absent = {a.element: a for a in layout.absences}
    assert "series" in absent["bgnorm"].effect
    assert "outputs will carry" in absent["bgnorm"].effect


def test_a_partial_absence_carries_both_halves_of_the_fraction(snap, source):
    layout = layout_of(_plan(snap, _tma(snap), "ometiff.tma.per-core"), snapshot=snap,
                       source=source)
    absent = {a.element: a for a in layout.absences}
    assert absent["bgnorm"].partial == (len(demo.BGNORMED_CORES), demo.N_CORES)


def test_the_resolvers_own_word_is_kept_beside_the_effect(snap, source):
    layout = layout_of(_plan(snap, _tma(snap), "spatialdata.tma.by-core"), snapshot=snap,
                       source=source)
    assert {a.state for a in layout.absences} & {"unlinked", "undeclared", "present"}


def test_a_degraded_element_still_appears_with_its_measurement(snap, source):
    layout = layout_of(_plan(snap, _core(snap, 5), "ometiff.core"), snapshot=snap, source=source)
    assert [p.key for p in layout.pieces] == ["raw"]
    assert _piece(layout, "raw").shape == (C, *_expected_extent())


def test_a_blocked_plan_says_nothing_will_be_built(snap, source):
    request = ExtractRequest(
        roi=ROISelector(uid=_core(snap, demo.EMPTY_CORE).uid), recipe="spatialdata.core",
        target="SpatialData", strictness="strict",
        elements=ElementSpec(include=["labels"]),
        destination=Destination(kind="path", path="./out"))
    layout = layout_of(make_plan(request, snapshot=snap), snapshot=snap, source=source)
    assert layout.n_outputs == 0
    assert "builds nothing" in " ".join(layout.notes)
    assert all("blocks the whole plan" in a.effect for a in layout.absences)


def test_an_unreadable_geometry_reports_the_source_size_and_says_so(snap, source):
    twin = snap.record_by_name("Core", f"{demo.TMA1_NAME}:R0C0")
    layout = layout_of(_plan(snap, twin, "ometiff.core"), snapshot=snap, source=source)
    assert _piece(layout, "raw").shape == (C, Y, X)
    assert any("crop box is unknown" in note for note in layout.notes)


# A target that is not an OME-TIFF.
def test_a_spatialdata_is_rendered_as_elements_not_series(snap, source):
    layout = layout_of(_plan(snap, _core(snap), "spatialdata.core"), snapshot=snap, source=source)
    assert layout.target == "SpatialData"
    assert {p.slot for p in layout.pieces} == {"images", "labels", "tables"}
    assert layout.ome_xml == ""


def test_a_spatialdata_image_carries_the_transformation_that_places_it(snap, source):
    from clinicalomx.extract.coords import crop_box

    layout = layout_of(_plan(snap, _core(snap), "spatialdata.core"), snapshot=snap, source=source)
    x0, y0, _x1, _y1 = crop_box(CORE_BOX, width=X, height=Y)
    assert _piece(layout, "bgnorm").transform == f"Translation(x={x0}, y={y0})"
    assert _piece(layout, "bgnorm").space == "global"


def test_a_merged_object_lists_the_keys_the_builder_will_write(snap, source):
    from clinicalomx.extract.keys import key_for

    layout = layout_of(_plan(snap, _tma(snap), "spatialdata.tma.by-core"), snapshot=snap,
                       source=source)
    expected = key_for(demo.TMA_NAME, f"{demo.TMA_NAME}:R0C0", "Core", "bgnorm")
    assert expected in {p.key for p in layout.pieces}
    assert _piece(layout, expected).element == "bgnorm"


def test_a_merged_object_keeps_the_anchors_own_elements_once(snap, source):
    layout = layout_of(_plan(snap, _tma(snap), "spatialdata.tma.by-core"), snapshot=snap,
                       source=source)
    assert [p.key for p in layout.pieces if p.slot == "shapes"] == ["cores"]
    assert "bgnorm" not in {p.key for p in layout.pieces}


def test_a_table_says_what_it_annotates(snap, source):
    layout = layout_of(_plan(snap, _core(snap, demo.SEGMENTED_TRACED), "spatialdata.core"),
                       snapshot=snap, source=source)
    table = next(p for p in layout.pieces if p.slot == "tables")
    assert table.annotates == "labels"
    assert table.instance_key == "cell_id"


def test_an_unknown_target_says_so_rather_than_inventing_a_structure(snap, source):
    reset_registrations()
    register_recipes([Recipe(
        name="fixture.zarrish", target="Zarrish", roi_types=("Core",), layout="single",
        elements=(Element("image", "image", roles=("source_image",)),),
        builder="clinicalomx.extract.builders.ometiff:build_core",
        placement="clinicalomx.extract.coords:GeometryPlacement")], provider="fixture")
    try:
        layout = layout_of(_plan(snap, _core(snap), "fixture.zarrish"), snapshot=snap,
                           source=source)
    finally:
        reset_registrations()
    assert layout.target == "Zarrish"
    assert "no layout is known" in " ".join(layout.notes)
    assert [p.element for p in layout.pieces] == ["image"]


# The mosaic: one canvas rather than one series per core.
def test_a_mosaic_canvas_is_the_union_of_the_padded_boxes(snap, source):
    from clinicalomx.extract.builders.mosaic import canvas_shape, union_bounds
    from clinicalomx.extract.coords import crop_box

    layout = layout_of(_plan(snap, _tma(snap), "ometiff.tma.mosaic"), snapshot=snap,
                       source=source)
    expected = canvas_shape(union_bounds([crop_box(CORE_BOX)]), channels=C)
    assert _piece(layout, "bgnorm").shape == expected
    assert _piece(layout, "bgnorm").n_from > 1


def test_a_mosaic_canvas_holds_every_core_it_counts(snap, source):
    from clinicalomx.extract.builders.mosaic import union_bounds
    from clinicalomx.extract.coords import crop_box

    layout = layout_of(_plan(snap, _tma(snap), "ometiff.tma.mosaic"), snapshot=snap,
                       source=source)
    x0, y0, x1, y1 = union_bounds([crop_box(CORE_BOX)])
    px0, py0, px1, py1 = crop_box(CORE_BOX)
    assert (px0 >= x0 and py0 >= y0 and px1 <= x1 and py1 <= y1), "a core falls outside the canvas"
    assert not any("SKIPPED by the compositor" in note for note in layout.notes)


def _tiled_source(tmp_path):
    """A source whose core grid tiles its canvas and earns a pyramid."""
    side, across = 600.0, 4
    raw = tmp_path / "slide.qptiff"
    tifffile.imwrite(raw, np.zeros((C, Y, X), dtype="uint16"))
    grid = tmp_path / "tiled.parquet"
    gpd.GeoDataFrame(
        {"roi_id": [f"R0C{i}" for i in range(demo.N_CORES)]},
        geometry=[box(100.0 + (i % across) * side, 200.0 + (i // across) * side,
                      100.0 + (i % across + 1) * side, 200.0 + (i // across + 1) * side)
                  for i in range(demo.N_CORES)],
    ).to_parquet(grid)
    table = pq.read_table(grid)
    meta = dict(table.schema.metadata or {})
    meta[b"roi_attrs"] = json.dumps({"scale_factor": 1.0}).encode()
    pq.write_table(table.replace_schema_metadata(meta), grid)

    paths = {f"raw/{demo.SLIDE_NAME}.qptiff": raw,
             f"detections/{demo.TMA_NAME}-grid.parquet": grid,
             "restricted/denied-core.zarr": tmp_path / "never-opened.zarr"}
    for i in demo.BGNORMED_CORES:
        paths[f"bgnorm/{demo.TMA_NAME}:R0C{i}.zarr"] = _bgnormed(tmp_path / f"c{i}.zarr")
    reset_cache()
    return _Files(paths)


def test_a_mosaic_states_the_base_levels_occupancy_and_not_the_whole_pyramids(snap, tmp_path):
    layout = layout_of(_plan(snap, _tma(snap), "ometiff.tma.mosaic"), snapshot=snap,
                       source=_tiled_source(tmp_path))
    note = next(n for n in layout.notes if "PADDED crop boxes" in n)
    shown = float(note.split("BASE level are ")[1].split("%")[0])
    piece = next(p for p in layout.pieces if p.shape and p.stored_bytes)

    assert piece.stored_bytes > piece.size_bytes, (
        "the whole file does not exceed the canvas here, so a whole-pyramid percentage would "
        "still come out under 100 and this asserts nothing")
    assert 90 <= shown <= 100, f"the page reports {shown}% occupancy of its own canvas: {note}"
    assert "the second is what the whole file holds" in note, note
    assert any("PYRAMIDAL" in n for n in layout.notes), "the fixture grew no overview levels"


def test_a_mosaic_whose_series_cannot_be_measured_states_no_occupancy(snap, source, monkeypatch):
    from clinicalomx.extract import layout as layout_module

    monkeypatch.setattr(layout_module._Stores, "open",
                        lambda self, key, variable: (None, "the store could not be opened"))
    layout = layout_of(_plan(snap, _tma(snap), "ometiff.tma.mosaic"), snapshot=snap,
                       source=source)
    note = next(n for n in layout.notes if "PADDED crop boxes" in n)
    assert "%" not in note, note
    assert "ABSENT tiles" in note
    assert "second" not in note, note


def test_a_mosaic_says_where_the_canvas_sits_on_the_slide(snap, source):
    layout = layout_of(_plan(snap, _tma(snap), "ometiff.tma.mosaic"), snapshot=snap,
                       source=source)
    assert any("origin" in note for note in layout.notes)


# The structure without the bytes.
def test_the_structure_alone_needs_no_read_at_all(snap):
    layout = layout_of(_plan(snap, _core(snap), "ometiff.core"), snapshot=snap, measure=False)
    assert [p.key for p in layout.pieces] == ["raw", "bgnorm", "bgnormQ"]
    assert layout.reads == 0
    assert not layout.measured


def test_the_layout_round_trips_through_to_dict(snap, source):
    layout = layout_of(_plan(snap, _tma(snap), "spatialdata.tma.by-core"), snapshot=snap,
                       source=source)
    got = layout.to_dict()
    assert len(got["pieces"]) == len(layout.pieces)
    assert {a["element"] for a in got["absences"]} == {a.element for a in layout.absences}
    json.dumps(got)


def test_the_tree_truncates_its_keys_and_says_by_how_many(snap, source):
    layout = layout_of(_plan(snap, _tma(snap), "spatialdata.tma.by-core"), snapshot=snap,
                       source=source)
    tree = layout.tree(max_keys=2)
    assert "more" in tree
    assert tree.count("Core_R0C") < len(layout.pieces)


# What a job reports about scratch.
def test_a_job_quotes_the_scratch_its_layout_measured(snap, source):
    from clinicalomx.extract.jobs import Job

    plan = _plan(snap, _core(snap), "ometiff.core")
    layout = layout_of(plan, snapshot=snap, source=source)
    est = Job(intent="", request={}, plans=[plan]).estimate(layouts={plan.token: layout})
    assert est["scratch_bytes"] == sum(p.size_bytes for p in layout.pieces) * layout.n_outputs
    assert "Scratch:" in est["note"]


def test_a_mosaic_is_quoted_at_what_it_stores(snap, source):
    from clinicalomx.extract.jobs import Job

    plan = _plan(snap, _tma(snap), "ometiff.tma.mosaic")
    layout = layout_of(plan, snapshot=snap, source=source)
    est = Job(intent="", request={}, plans=[plan]).estimate(layouts={plan.token: layout})
    assert est["scratch_bytes"] == sum(p.stored_bytes or p.size_bytes for p in layout.pieces
                                       if p.measured)


def test_an_unmeasured_layout_says_scratch_is_unknown_rather_than_small(snap, source):
    from clinicalomx.extract.jobs import Job

    plan = _plan(snap, _tma(snap), "ometiff.tma.mosaic")
    layout = layout_of(plan, snapshot=snap, measure=False)
    est = Job(intent="", request={}, plans=[plan]).estimate(layouts={plan.token: layout})
    assert est["scratch_bytes"] is None
    assert "NOT measured" in est["note"]
    assert "without store metadata" in est["scratch_basis"], "the reason must say what to do"


def test_a_plate_says_which_renderer_is_missing(snap, source):
    from clinicalomx.extract.jobs import Job

    plan = make_plan(ExtractRequest(
        roi=ROISelector(uid=_tma(snap).uid), recipe="omezarr.tma.plate", target="OME-Zarr",
        destination=Destination(kind="path", path="./out")), snapshot=snap)
    layout = layout_of(plan, snapshot=snap, source=source)
    est = Job(intent="", request={}, plans=[plan]).estimate(layouts={plan.token: layout})
    assert est["scratch_bytes"] is None
    assert "no renderer for OME-Zarr" in est["scratch_basis"], est["scratch_basis"]
