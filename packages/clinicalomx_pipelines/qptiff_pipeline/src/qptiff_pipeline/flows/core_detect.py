"""core-detect: deep-learning core detection for each TMA. Writes a candidate only. Runs on the HPC.

Crops the TMA out of the slide's low-res image, runs the core detector, writes a candidate
detections GeoParquet, and parks the TMA at ``core_pending``. The dearray grid fit runs in the
review
app, not here.

Review payload, as Prefect artifacts:
  - ``core-review-<slug>``        markdown overlay (boxes on the TMA crop, data-URI PNG)
  - ``core-review-<slug>-table``  detections table
  - ``core-edit-<slug>``          editor bundle: clean crop PNG b64, boxes in crop px, and
                                  ``crop_x``/``crop_y``/``scale_factor`` to map back to full-res
"""

from __future__ import annotations

# Put the clone's src/ on sys.path: the HPC runs a git clone, not an install.
import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)


from prefect import flow, get_run_logger
from prefect.artifacts import create_markdown_artifact, create_table_artifact

STAGE = "core"

#: The ``model:`` block the production champion is built with. ``contained_thr: 0`` turns off the
#: backend's own containment pass: ``detect_cores`` already applies ``suppress_overlaps``.
CHAMPION_CFG = {"contained_thr": 0}


def _detect_cores(detector, crop, *, conf: float):
    """Detect cores on the TMA crop through ``croi``'s ObjectDetector seam. Returns a boxes gdf."""
    from croi.objectives.tma_core.dearray import detect_cores

    return detect_cores(crop, detector, conf=conf).reset_index(drop=True)


def _overlay_png_bytes(img, gdf) -> bytes:
    """Boxes over the TMA crop, as PNG bytes."""
    import io

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    fig, ax = plt.subplots(figsize=(10, 10))
    ax.imshow(img)
    colors = {"full_core": "cyan", "partial_core": "yellow"}
    for _, r in gdf.iterrows():
        if r.geometry is None:
            continue
        x1, y1, x2, y2 = r.geometry.bounds
        col = colors.get(r["class"], "red")
        ax.add_patch(
            Rectangle((x1, y1), x2 - x1, y2 - y1, fill=False, edgecolor=col, linewidth=1.2)
        )
    ax.axis("off")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=100)
    plt.close(fig)
    return buf.getvalue()


def _overlay_datauri(img, gdf) -> str:
    """Boxes over the TMA crop, as base64 PNG."""
    import base64

    return base64.b64encode(_overlay_png_bytes(img, gdf)).decode()


def _reconcile_core(
    tma,
    arts: dict,
    *,
    okeys: tuple[str, str, str],
    qptiff,
    roi_art,
    model_art,
    model_uid: str,
    model_alias: str,
    conf: float,
    review_prefix: str,
    target_directory: str,
    logger,
) -> dict:
    """Enforce everything that must hold once the three outputs exist (see general_detect).

    Also runs on the cached path, so the crop space comes from the candidate parquet's ``.attrs``.
    ``okeys`` is (pred, thumbnail, detections).
    """
    import base64
    from pathlib import Path

    from clinicalomx import annotate, get_state, set_state

    from qptiff_pipeline.domain import keys, states, vocab
    from qptiff_pipeline.domain.roi_io import read_roi_parquet
    from qptiff_pipeline.lamin import context_io
    from qptiff_pipeline.lamin.linking import link_input

    pred_key, thumb_key, overlay_key = okeys
    pred_art, thumb_art, overlay_art = arts[pred_key], arts[thumb_key], arts[overlay_key]
    slug = keys.slug(tma.name)

    det_gdf = read_roi_parquet(pred_art.path)
    a = det_gdf.attrs
    factor = float(a.get("scale_factor", 1.0))
    crop_x, crop_y = int(a.get("crop_x") or 0), int(a.get("crop_y") or 0)
    tw, th = int(a.get("thumb_w") or 1), int(a.get("thumb_h") or 1)
    n_dets = len(det_gdf)

    # Any of these may be None: an overlay render can fail without failing the stage.
    for fname, val in ((vocab.F_THUMBNAIL, thumb_art),
                       (vocab.F_PRED, pred_art),
                       (vocab.F_DET_OVERLAY, overlay_art)):
        if val is not None:
            annotate(tma, val, fname)

    # Inputs: the source qptiff, the accepted general ROI parquet that locates this TMA, the model.
    link_input(pred_art, qptiff, roi_art, model_art)

    ctx = context_io.build_detect_context(
        "core",
        unit_name=tma.name,
        unit_uid=tma.uid,
        gdf=det_gdf,
        width=tw,
        height=th,
        scale_factor=factor,
        crop_x=crop_x,
        crop_y=crop_y,
        source_image=qptiff,
        model_uid=model_uid,
        model_alias=model_alias,
        params={"conf": conf},
        images={"clean": thumb_art, "detection_overlay": overlay_art},
    )
    context_io.write_context(tma, ctx, target_directory=target_directory, qptiff_key=qptiff.key)

    # Park for review, but never rewind a gate a human already moved past.
    current = get_state(tma)
    if current in (None, states.S.REGISTERED, states.S.CORE_PENDING, states.S.CORE_FAILED):
        set_state(tma, states.S.CORE_PENDING)
    else:
        logger.info(f"{tma.name}: leaving state {current} untouched (already past core review)")

    rslug = review_prefix + slug
    try:
        overlay_b64 = base64.b64encode(Path(overlay_art.path).read_bytes()).decode()
        create_markdown_artifact(
            key=rslug,
            markdown=(
                f"## core-detect review: {tma.name}\n\n"
                f"TMA uid `{tma.uid}` - {n_dets} core detection(s) - state core_pending.\n\n"
                f"![overlay](data:image/png;base64,{overlay_b64})"
            ),
            description=f"Core detection overlay for {tma.name}",
        )
        boxes = [
            {
                "class": r["class"],
                "conf": round(float(r["conf"]), 3),
                "xyxy": [round(float(v), 1) for v in r.geometry.bounds],
            }
            for _, r in det_gdf.iterrows()
            if r.geometry is not None
        ]
        if boxes:
            create_table_artifact(
                key=f"{rslug}-table",
                table=[{"class": b["class"], "conf": b["conf"], "bbox": b["xyxy"]} for b in boxes],
                description=f"Core detections for {tma.name}",
            )
        context_io.publish_context(
            ctx,
            image_b64=base64.b64encode(Path(thumb_art.path).read_bytes()).decode(),
            slug=slug,
        )
    except Exception as exc:
        logger.warning(f"{tma.name}: could not create review artifact: {exc}")

    logger.info(f"{tma.name}: {n_dets} core detection(s) -> core_pending")
    return {"tma": tma.name, "n_dets": n_dets, "review_key": rslug}


def _thumb_png_bytes(img) -> bytes:
    """Clean 1:1 PNG of the TMA crop: the editor's background and the training image."""
    import io

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    buf = io.BytesIO()
    plt.imsave(buf, img, format="png")
    return buf.getvalue()


@flow(name="core-detect", log_prints=True, retries=3, retry_delay_seconds=60)
def core_detect(
    target_directory: str,
    tma_uids: list[str] | None = None,
    model_alias: str = "production",
    conf: float = 0.1,
    limit: int | None = None,
) -> dict:
    """Detect cores per TMA, write a candidate grid, and park each TMA at core_pending.

    tma_uids: TMA Record uids to detect on. Empty means batch mode: every TMA at ``registered``,
        capped by ``limit``.
    target_directory: results root on the HPC (a registered lamindb Storage root).
    """
    logger = get_run_logger()

    import math
    from pathlib import Path

    import clinicalomx as cx
    import geopandas as gpd
    import lamindb as ln
    from bioio import BioImage
    from bioio_qptiff import Reader
    from clinicalomx import geometry_row, get_state, set_state
    from croi.objectives.tma_core.registry import load_champion, resolve_model_artifact
    from croi.utils.image_preprocess import (
        get_smallest_scale_image,
        grayscale_to_rgb,
        image_to_8bit,
        max_intensity_projection,
    )

    from qptiff_pipeline.domain import hitl, keys, states, vocab
    from qptiff_pipeline.domain.roi_io import write_roi_parquet
    from qptiff_pipeline.lamin.registry import get_or_create_artifact
    from qptiff_pipeline.lamin.tracking import track_prefect_run

    review_prefix = hitl.REVIEW_PREFIX_BY_STAGE[STAGE]

    track_prefect_run("core-detect")
    try:
        # Batch mode: no uids, so sweep every TMA in the core entry state.
        if not tma_uids:
            _unit_type, entry_state = states.STAGE_ENTRY["core"]
            tma_type = ln.Record.filter(name=states.TMA_TYPE, is_type=True).one_or_none()
            pool = list(ln.Record.filter(type=tma_type).all()) if tma_type else []
            tma_uids = [t.uid for t in pool if get_state(t) == entry_state]
            logger.info(f"batch mode: {len(tma_uids)} TMA(s) in state {entry_state!r}")
        if limit:
            tma_uids = tma_uids[:limit]
        if not tma_uids:
            logger.info("no TMAs to detect; nothing to do")
            return {"processed": 0, "results": [], "failed": 0}

        vocab.ensure()  # declares this repo's features (see vocab.py)
        # Loaded once and reused across the batch.
        detector = load_champion(model_alias, CHAMPION_CFG)
        model_art = resolve_model_artifact(model_alias)  # lamindb model Artifact the alias points at (or None)
        model_uid = getattr(model_art, "uid", None)  # model-versions the prediction keys + lineage
        logger.info(f"core detector alias={model_alias!r} -> model_uid={model_uid}")
        img_cache: dict[
            str, tuple
        ] = {}  # qptiff key -> (rgb_img, scale_factor); TMAs share a slide

        results: list[dict] = []
        failed: list[tuple[str, str]] = []
        for uid in tma_uids:
            tma = ln.Record.get(uid=uid)
            tvals = tma.features.get_values()
            src = tvals.get("source_image")
            qptiff = src if isinstance(src, ln.Artifact) else ln.Artifact.get(key=src)
            roi_src = tvals.get("source_artifact")
            roi_art = roi_src if isinstance(roi_src, ln.Artifact) else ln.Artifact.get(key=roi_src)

            # Output keys are model-versioned: re-running under the same champion skips the work.
            pred_key = keys.core_key(qptiff.key, tma.name, "pred", model_uid)
            thumb_key = keys.core_key(qptiff.key, tma.name, "thumbnail", model_uid)
            overlay_key = keys.core_key(qptiff.key, tma.name, "detections", model_uid)
            okeys = (pred_key, thumb_key, overlay_key)

            def _compute(tma=tma, tvals=tvals, qptiff=qptiff, roi_art=roi_art, okeys=okeys) -> dict:
                """Crop the TMA out of the slide, detect, write and register."""
                pred_key, thumb_key, overlay_key = okeys
                # Slide low-res RGB, cached per slide.
                if qptiff.key not in img_cache:
                    ds = BioImage(qptiff.path, reader=Reader).reader.xarray_dask_scene_datatree[
                        "FullResolution"
                    ]
                    _img, factor = get_smallest_scale_image(ds, return_factor=True)
                    img_cache[qptiff.key] = (
                        grayscale_to_rgb(image_to_8bit(max_intensity_projection(_img))),
                        float(factor),
                    )
                img, factor = img_cache[qptiff.key]
                h_img, w_img = int(img.shape[0]), int(img.shape[1])

                # TMA box from the general ROI parquet, stored at full resolution. Map it to the
                # low-res image (/factor) and clamp. Match on roi_id, else on parquet_row.
                roi_row = geometry_row(
                    gpd.read_parquet(roi_art.path),
                    roi_id=tvals.get("roi_id"),
                    parquet_row=tvals.get("parquet_row"),
                )
                minx, miny, maxx, maxy = (b / factor for b in roi_row.geometry.bounds)
                xmin, ymin = max(0, math.floor(minx)), max(0, math.floor(miny))
                xmax, ymax = min(w_img, math.ceil(maxx)), min(h_img, math.ceil(maxy))
                if xmax - xmin < 2 or ymax - ymin < 2:
                    raise ValueError(
                        f"degenerate TMA crop [{xmin},{ymin},{xmax},{ymax}] on {w_img}x{h_img} low-res image"
                    )
                crop = img[ymin:ymax, xmin:xmax]

                # Detection only, in crop-local coords.
                det_gdf = _detect_cores(detector, crop, conf=conf)
                th, tw = int(crop.shape[0]), int(crop.shape[1])
                # These attrs define the crop space. Reconcile rebuilds it from them.
                det_gdf.attrs.update(
                    {
                        "scale_factor": factor,
                        "crop_x": xmin,
                        "crop_y": ymin,
                        "thumb_w": tw,
                        "thumb_h": th,
                    }
                )

                out = {}
                pq = f"{target_directory.rstrip('/')}/{pred_key}"
                Path(pq).parent.mkdir(parents=True, exist_ok=True)
                out[pred_key] = get_or_create_artifact(
                    pq,
                    key=pred_key,
                    description=f"Candidate core detections (pre-review) for {tma.name}",
                    write=lambda g=det_gdf, p=pq: write_roi_parquet(g, p),
                )
                tpath = f"{target_directory.rstrip('/')}/{thumb_key}"
                out[thumb_key] = get_or_create_artifact(
                    tpath,
                    key=thumb_key,
                    description=f"Clean TMA crop for {tma.name}",
                    # Rendered inside the thunk, so an existing key costs no PNG encode.
                    write=lambda p=tpath, im=crop: Path(p).write_bytes(_thumb_png_bytes(im)),
                )
                opath = f"{target_directory.rstrip('/')}/{overlay_key}"
                out[overlay_key] = get_or_create_artifact(
                    opath,
                    key=overlay_key,
                    description=f"Core detection overlay (boxes on image) for {tma.name}",
                    write=lambda p=opath, im=crop, g=det_gdf: Path(p).write_bytes(
                        _overlay_png_bytes(im, g)),
                )
                return out

            def _reconcile(arts, tma=tma, qptiff=qptiff, roi_art=roi_art, okeys=okeys) -> None:
                results.append(_reconcile_core(
                    tma, arts,
                    okeys=okeys,
                    qptiff=qptiff,
                    roi_art=roi_art,
                    model_art=model_art,
                    model_uid=model_uid,
                    model_alias=model_alias,
                    conf=conf,
                    review_prefix=review_prefix,
                    target_directory=target_directory,
                    logger=logger,
                ))

            # core_failed is retryable. The cached path never reaches compute, so it cannot fail
            # here.
            try:
                cx.materialize(okeys, compute=_compute, reconcile=_reconcile, heal="delete")
            except Exception as exc:
                set_state(tma, states.S.CORE_FAILED)
                failed.append((tma.name, f"{type(exc).__name__}: {exc}"))
                logger.error(
                    f"{tma.name}: core-detect failed -> core_failed: {type(exc).__name__}: {exc}"
                )
                continue

        logger.info(f"core-detect done: {len(results)} -> core_pending, {len(failed)} failed")
        return {
            "processed": len(results),
            "failed": len(failed),
            "results": results,
            "failed_tmas": [t for t, _ in failed],
        }
    finally:
        ln.finish()
