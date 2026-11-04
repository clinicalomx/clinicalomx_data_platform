"""Dearray each QPTIFF into per-core shapes.

Detects TMA grids with the tissue_objects champion and cores with the tma_core champion on the
FullResolution image's smallest pyramid level, fits the grid per TMA, and records every cell,
detected and inferred. Geometry is saved as GeoParquet shape Artifacts
(``rois/<stem>_{rois,cores}.parquet``) in smallest-level pixels. Each TMA and Core is an
``ln.Record`` in the ``WholeSlide -> TMA -> Core`` hierarchy. Full-res core crops come on demand
from ``crops.core_crop``. :func:`run_tma_cores` is the entry point.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

import lamindb as ln

import clinicalomx as cx
from clinicalomx import materialize

from croi.utils import roi_records
from croi.detection.backends import detector_for

from croi.objectives.tma_core import environment, vocab
from croi.objectives.tma_core.dearray import detect_cores
from croi.objectives.tma_core.gridding import estimate_grid
from croi.utils.image_preprocess import (
    get_smallest_scale_image, grayscale_to_rgb, image_to_8bit, max_intensity_projection,
)
from croi.objectives.tma_core import registry as _models

# The general detector's registry names, spelled here so tma_core does not import tissue_objects.
_GEN_REGISTERED, _GEN_ALIAS_NS = "roi-general", "model_alias_general"
_COMPLETENESS = {"full_core": "full", "partial_core": "partial"}
SCENE = "FullResolution"

_OUTPUT_STORAGE = None
_CORE_DETECTOR = _GENERAL_DETECTOR = None   # croi.detection.detector.ObjectDetector
_CORE_REF = _GENERAL_REF = None         # cx.ModelRef, for lineage


def _general_ref(sel: dict) -> "cx.ModelRef":
    """ModelRef for the general champion from a yaml ``inputs.model`` selector."""
    if any(sel.get(k) for k in ("mlflow_run_id", "mlflow_version", "mlflow_alias")):
        return cx.ModelRef.from_mlflow(
            run_id=sel.get("mlflow_run_id"), version=sel.get("mlflow_version"),
            alias=sel.get("mlflow_alias"),
            registered_model=_GEN_REGISTERED, alias_namespace=_GEN_ALIAS_NS,
        )
    return cx.ModelRef.from_lamindb(
        uid=sel.get("uid"), key=sel.get("key"), alias=sel.get("alias") or "production",
        registered_model=_GEN_REGISTERED, alias_namespace=_GEN_ALIAS_NS,
    )


def _detection_image(qptiff_path):
    """The FullResolution datatree, the smallest-level RGB uint8 image, and that level's
    downsample factor from full resolution."""
    from bioio import BioImage
    from bioio_qptiff import Reader

    ds = BioImage(qptiff_path, reader=Reader).reader.xarray_dask_scene_datatree[SCENE]
    img_da, factor = get_smallest_scale_image(ds, return_factor=True)
    arr = max_intensity_projection(img_da).data.compute()
    return ds, grayscale_to_rgb(image_to_8bit(arr)), factor


def _level_yx(node):
    """(H, W) of a DataTree node's ``image`` variable."""
    im = node.ds["image"]
    yd = next(d for d in im.dims if d.lower() == "y")
    xd = next(d for d in im.dims if d.lower() == "x")
    return int(im.sizes[yd]), int(im.sizes[xd])


def _coordinate_space(ds, frame_hw, factor, *, reference: str) -> dict:
    """Coordinate-frame metadata for the shapes parquets.

    Geometry is in pixels of ``SCENE``'s smallest pyramid level, not full resolution.
    ``frame_shape`` is ``crop_datatree_to_box``'s ``box_shape``. To convert:
    ``x_full = x * scale0_W / frame_W``.
    """
    nodes = [n for n in ds.subtree if "image" in n.ds.variables]
    ordered = sorted(nodes, key=lambda n: _level_yx(n)[0] * _level_yx(n)[1], reverse=True)
    H0, W0 = _level_yx(ordered[0])  # largest level is full resolution
    Hs, Ws = int(frame_hw[0]), int(frame_hw[1])
    n_levels = len(ordered)
    level_index = next((i for i, n in enumerate(ordered) if _level_yx(n) == (Hs, Ws)), n_levels - 1)
    return {
        "coordinate_space": "pixel",
        "reference_image": str(reference),
        "scene": SCENE,
        "level_index": level_index,   # 0 is full resolution
        "n_levels": n_levels,
        "frame_shape": [Hs, Ws],      # (H, W) the coordinates are defined in
        "scale0_shape": [H0, W0],     # full-resolution (level-0) (H, W)
        "downsample_to_scale0": float(factor),
        "note": ("geometry/ideal_geometry/bbox_* are pixel coords on pyramid level "
                 f"{level_index}/{n_levels - 1} (smallest) of {SCENE}; multiply by "
                 "scale0_shape/frame_shape to reach full resolution. crop_datatree_to_box "
                 "consumes frame_shape directly as box_shape."),
    }


def _write_geoparquet_with_meta(gdf, path, meta: dict) -> None:
    """Write a GeoParquet with ``meta`` as the ``croi:coordinate_space`` file-level metadata.

    geopandas drops ``DataFrame.attrs``, so the file is re-read, merged and rewritten.
    """
    import json
    import pyarrow.parquet as pq

    gdf.to_parquet(path)
    tbl = pq.read_table(path)
    kv = dict(tbl.schema.metadata or {})
    kv[b"croi:coordinate_space"] = json.dumps(meta).encode()
    pq.write_table(tbl.replace_schema_metadata(kv), path)


def _tma_cells(img, tma_geom, t, params):
    """Detect and grid the cores in one TMA box. Returns a cells gdf in smallest-level coords.

    Falls back to a single row when there are too few cores to fit a grid.
    """
    import geopandas as gpd

    H, W = img.shape[:2]
    x0, y0 = max(0, math.floor(tma_geom.bounds[0])), max(0, math.floor(tma_geom.bounds[1]))
    x1, y1 = min(W, math.ceil(tma_geom.bounds[2])), min(H, math.ceil(tma_geom.bounds[3]))
    core_gdf = detect_cores(
        img[y0:y1, x0:x1], _CORE_DETECTOR,
        conf=float(params.get("core_conf", 0.1)),
    )
    core_main = core_gdf.copy()
    core_main["geometry"] = core_gdf.geometry.translate(x0, y0)  # TMA slice -> full frame
    try:
        grid = estimate_grid(core_main, expected_shape=params.get("expected_shape"))
        cells = grid.to_geodataframe()
    except Exception as e:
        print(f"  TMA{t}: grid fit failed ({type(e).__name__}: {str(e)[:80]}); single-row fallback")
        cells = core_main.assign(
            row=0, col=range(len(core_main)), occupied=True,
            detection_idx=range(len(core_main)), ideal_geometry=core_main.geometry,
        )
        cells = gpd.GeoDataFrame(cells, geometry="geometry")
    cells["tma"] = t
    cells["is_detected"] = cells["conf"] != -1
    return cells


@ln.step()
def step_dearray(qptiff_art: ln.Artifact, config: dict) -> list:
    """Dearray one QPTIFF into TMA and core GeoParquet Artifacts and their ROI Records."""
    import geopandas as gpd
    import pandas as pd

    params = config["parameters"]
    stem = qptiff_art.path.stem
    rois_key = config["outputs"]["tma_shapes"]["key_template"].format(stem=stem)
    cores_key = config["outputs"]["core_shapes"]["key_template"].format(stem=stem)

    def compute():
        ds, img, factor = _detection_image(qptiff_art.path)
        tma_gdf = detect_cores(
            img, _GENERAL_DETECTOR,
            conf=float(params.get("tma_conf", 0.3)),
        )
        tma_gdf = tma_gdf[tma_gdf["class"] == "TMA"].copy()
        # stable TMA index: top-left to bottom-right
        b = tma_gdf.geometry.bounds
        tma_gdf = tma_gdf.loc[b.sort_values(["miny", "minx"]).index].reset_index(drop=True)

        frames = [_tma_cells(img, geom, t, params) for t, geom in enumerate(tma_gdf.geometry)]
        cores_all = (
            gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), geometry="geometry")
            if frames else gpd.GeoDataFrame({"geometry": gpd.GeoSeries([])}, geometry="geometry")
        )
        cores_all = cores_all.reset_index(drop=True)

        out_dir = Path(_OUTPUT_STORAGE.path) / "rois"
        out_dir.mkdir(parents=True, exist_ok=True)
        rois_path, cores_path = out_dir / f"{stem}_rois.parquet", out_dir / f"{stem}_cores.parquet"
        coord = _coordinate_space(ds, img.shape[:2], factor, reference=qptiff_art.key)
        _write_geoparquet_with_meta(tma_gdf, rois_path, coord)
        _write_geoparquet_with_meta(cores_all, cores_path, coord)

        rois_art = ln.Artifact(rois_path, key=rois_key, storage=_OUTPUT_STORAGE,
                               description=f"TMA detections for {stem} ({len(tma_gdf)} TMA(s)).").save()
        cores_art = ln.Artifact(cores_path, key=cores_key, storage=_OUTPUT_STORAGE,
                                description=(f"Dearray grid for {stem} ({len(cores_all)} cells, "
                                            f"{int(cores_all.get('is_detected', pd.Series(dtype=bool)).sum())} detected).")).save()
        print(f"  dearrayed {qptiff_art.key} -> {len(tma_gdf)} TMA(s), {len(cores_all)} core cell(s)")
        return {rois_key: rois_art, cores_key: cores_art}

    # Lineage and ROI graph. Runs on cache hits too, so a re-run backfills an existing parquet.
    def reconcile(arts):
        rois_art, cores_art = arts[rois_key], arts[cores_key]

        # Parquets made by another producer keep that run, since `materialize` skips compute.
        # Re-point both to one fresh run of this transform. No-op once this transform produced them.
        ours = ln.context.run.transform
        foreign = [a for a in (rois_art, cores_art)
                   if a.run is None or a.run.transform is None or a.run.transform.key != ours.key]
        if foreign:
            run = ln.Run(transform=ours).save()
            for a in (rois_art, cores_art):
                a.run = run
                a.save()
            print(f"  re-attributed producer of {rois_key}, {cores_key} -> {ours.key}")

        for art in (rois_art, cores_art):
            art.run.input_artifacts.add(qptiff_art)
        _GENERAL_REF.consume(rois_art, ensure=True, storage=_OUTPUT_STORAGE)
        _GENERAL_REF.consume(cores_art, ensure=True, storage=_OUTPUT_STORAGE)
        _CORE_REF.consume(cores_art, ensure=True, storage=_OUTPUT_STORAGE)

        slide = roi_records.slide_name(stem)
        slide_roi = roi_records.get_or_create_roi(slide, roi_records.TYPE_SLIDE)
        cx.annotate(slide_roi, rois_art, vocab.TMA_SHAPES)
        cx.annotate(slide_roi, cores_art, vocab.CORE_SHAPES)

        # A TMA Record's parquet_row is its positional row in the rois parquet. Older rois
        # parquets hold every ROI class, so TMA rows keep their original position.
        tma_gdf = gpd.read_parquet(rois_art.path)
        has_class = "class" in tma_gdf.columns
        tma_rois = {}
        t = 0
        for pos in range(len(tma_gdf)):
            rec = tma_gdf.iloc[pos]
            if has_class and rec.get("class") != "TMA":
                continue
            tma_roi = roi_records.get_or_create_roi(
                roi_records.tma_name(slide, t), roi_records.TYPE_TMA,
                detection_class_name="TMA",
                detection_confidence=float(rec.get("conf", -1.0)),
                is_detected=True, parquet_row=pos,
                source_image=qptiff_art, source_artifact=rois_art,
            )
            cx.create(tma_roi, parent=slide_roi)
            tma_rois[t] = tma_roi
            t += 1

        cores_gdf = gpd.read_parquet(cores_art.path)
        has_tma = "tma" in cores_gdf.columns
        has_isdet = "is_detected" in cores_gdf.columns
        for row in range(len(cores_gdf)):
            rec = cores_gdf.iloc[row]
            t, rr, cc = (int(rec["tma"]) if has_tma else 0), int(rec["row"]), int(rec["col"])
            cls = rec.get("class")
            detected = bool(rec["is_detected"]) if has_isdet else (float(rec["conf"]) != -1)
            core_roi = roi_records.get_or_create_roi(
                roi_records.core_name(slide, t, rr, cc), roi_records.TYPE_CORE,
                detection_class_name=(cls if detected else None),
                core_completeness=_COMPLETENESS.get(cls),
                detection_confidence=(float(rec["conf"]) if detected else None),
                is_detected=detected, parquet_row=row, row=rr, col=cc,
                source_image=qptiff_art, source_artifact=cores_art,
            )
            cx.create(core_roi, parent=tma_rois.get(t, slide_roi))
        print(f"  ROI graph: WholeSlide {slide} -> {len(tma_rois)} TMA -> {len(cores_gdf)} Core")

        # The dashboard draws lineage only for finished runs; a hand-built Run stays
        # `scheduled` until completed.
        prod = cores_art.run
        if prod.finished_at is None:
            prod.finished_at = datetime.now(timezone.utc)
            prod._status_code = 0  # completed
            prod.save()

    return list(materialize([rois_key, cores_key], compute=compute, reconcile=reconcile).values())


def _qptiffs_to_dearray(config: dict) -> list[ln.Artifact]:
    if config.get("image"):  # single-slide override
        return [ln.Artifact.get(key=config["image"], is_latest=True)]
    q = dict(config["inputs"]["qptiffs"])
    q.pop("query", None)
    suffix = q.pop("suffix", ".qptiff")
    exclude = q.pop("exclude_key_contains", []) or []
    qs = ln.Artifact.filter(suffix=suffix, is_latest=True, **q)
    for term in exclude:
        qs = qs.filter(~ln.Q(key__icontains=term))
    arts = list(qs)
    limit = config.get("limit")
    return arts[:limit] if limit else arts


@ln.flow()
def run_extract_tma_cores(config: dict, project: ln.Project) -> None:
    global _CORE_DETECTOR, _GENERAL_DETECTOR, _CORE_REF, _GENERAL_REF
    _CORE_REF = _models.model_ref(config["inputs"]["core_model"])
    _GENERAL_REF = _general_ref(config["inputs"]["general_model"])
    print(f"core model:    {_CORE_REF}\n  weights: {_CORE_REF.path}")
    print(f"general model: {_GENERAL_REF}\n  weights: {_GENERAL_REF.path}")
    # Both refs are resolved checkpoints. contained_thr 0 keeps raw boxes; detect_cores suppresses.
    _CORE_DETECTOR = detector_for({"weights": str(_CORE_REF.path), "contained_thr": 0})
    _GENERAL_DETECTOR = detector_for({"weights": str(_GENERAL_REF.path), "contained_thr": 0})

    qptiffs = _qptiffs_to_dearray(config)
    print(f"dearraying {len(qptiffs)} qptiff artifact(s)")
    n_done = n_err = 0
    for qp in qptiffs:
        try:
            step_dearray(qp, config)
            n_done += 1
        except Exception as e:
            print(f"  ERROR {qp.key}: {type(e).__name__}: {str(e)[:160]}")
            n_err += 1
    print(f"\n{n_done} dearrayed, {n_err} errored.")


def run_tma_cores(config: dict, *, image: str | None = None,
                  limit: int | None = None) -> None:
    """Stage entry point for the dearray."""
    global _OUTPUT_STORAGE
    stage = environment.preflight(mlflow_env=True)
    _OUTPUT_STORAGE = stage.storage
    if image:
        config["image"] = image
    if limit:
        config["limit"] = limit
    run_extract_tma_cores(config=config, project=stage.project)
