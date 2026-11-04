"""general-detect: detect ROIs on each WholeSlide. Writes a candidate only. Runs on the HPC.

Runs the general detector on the slide thumbnail, writes a candidate ROI GeoParquet and parks the
slide at ``general_pending``. TMA Records are created later by general-persist on accept.
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

#: Inference scale, in thumbnail pixels. Fixed here so the champion's config cannot change it.
DETECT_IMGSZ = 640

#: The ``model:`` block the production champion is built with. ``contained_thr: 0`` turns off the
#: backend's own containment pass: this flow already applies ``suppress_overlaps``.
CHAMPION_CFG = {"contained_thr": 0}


def _detect_rois(detector, img, *, conf: float, nms_iou: float):
    """Detect ROIs on the thumbnail and return the candidate GeoDataFrame.

    ``suppress_overlaps`` keeps no two boxes on the same object. A slide with no ROIs still returns
    an empty frame with a geometry column, so a human can draw ROIs in the editor.
    """
    import geopandas as gpd
    from croi.detection.detector import suppress_overlaps
    from shapely.geometry import box

    dets = suppress_overlaps(
        detector.predict(img, conf=conf, imgsz=DETECT_IMGSZ),
        iou_thr=nms_iou,
        class_agnostic=True,
    )
    rows = [
        {"class": d.class_name, "conf": d.confidence, "geometry": box(*d.xyxy)}
        for d in dets
    ]
    return gpd.GeoDataFrame(
        rows, columns=["class", "conf", "geometry"], geometry="geometry"
    ).reset_index(drop=True)


def _overlay_png_bytes(img, gdf) -> bytes:
    """Detected boxes over the thumbnail, as PNG bytes."""
    import io

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    fig, ax = plt.subplots(figsize=(10, 10))
    ax.imshow(img)
    colors = {"TMA": "cyan", "tissue_section": "yellow"}
    for _, r in gdf.iterrows():
        x1, y1, x2, y2 = r.geometry.bounds
        col = colors.get(r["class"], "red")
        ax.add_patch(
            Rectangle((x1, y1), x2 - x1, y2 - y1, fill=False, edgecolor=col, linewidth=1.5)
        )
        ax.text(x1, y1 - 4, f"{r['class']} {float(r['conf']):.2f}", color=col, fontsize=6)
    ax.axis("off")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=100)
    plt.close(fig)
    return buf.getvalue()


def _overlay_datauri(img, gdf) -> str:
    """Detected boxes over the thumbnail, as base64 PNG."""
    import base64

    return base64.b64encode(_overlay_png_bytes(img, gdf)).decode()


def _reconcile_general(
    slide,
    arts: dict,
    *,
    okeys: tuple[str, str, str],
    qptiff,
    model_art,
    model_uid: str,
    model_alias: str,
    conf: float,
    nms_iou: float,
    target_directory: str,
    logger,
) -> dict:
    """Enforce everything that must hold once the three outputs exist.

    Runs on both the computed and the cached path (see :func:`clinicalomx.materialize`). Reads the
    coordinate space from the candidate parquet's ``.attrs``. ``okeys`` is (pred, thumbnail,
    detections).
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
    base = Path(qptiff.key).stem

    gdf = read_roi_parquet(pred_art.path)
    factor = float(gdf.attrs.get("scale_factor", 1.0))
    tw = int(gdf.attrs.get("thumb_w") or 1)
    th = int(gdf.attrs.get("thumb_h") or 1)
    n_tma = int((gdf["class"] == "TMA").sum()) if len(gdf) else 0

    # The unit Record is the ROI, so `annotate`, not `update`. Role features append: readers take
    # newest_artifact. Any of these may be None: an overlay render can fail without failing the
    # stage.
    for fname, val in ((vocab.F_THUMBNAIL, thumb_art),
                       (vocab.F_PRED, pred_art),
                       (vocab.F_DET_OVERLAY, overlay_art)):
        if val is not None:
            annotate(slide, val, fname)

    # Lineage: the source qptiff and the model. model_art may be None; link_input skips it.
    link_input(pred_art, qptiff, model_art)

    ctx = context_io.build_detect_context(
        "general",
        unit_name=slide.name,
        unit_uid=slide.uid,
        gdf=gdf,
        width=tw,
        height=th,
        scale_factor=factor,
        source_image=qptiff,
        model_uid=model_uid,
        model_alias=model_alias,
        params={"conf": conf, "nms_iou": nms_iou},
        images={"clean": thumb_art, "detection_overlay": overlay_art},
    )
    context_io.write_context(slide, ctx, target_directory=target_directory, qptiff_key=qptiff.key)

    # Park for review, but never rewind a gate a human already moved past. Only states still
    # waiting for this stage's review are advanced.
    current = get_state(slide)
    if current in (None, states.S.REGISTERED, states.S.GENERAL_PENDING, states.S.GENERAL_FAILED):
        set_state(slide, states.S.GENERAL_PENDING)
    else:
        logger.info(f"{base}: leaving state {current} untouched (already past general review)")

    # Review transport: the PNGs travel to the VM as data-URIs in a per-slide Prefect artifact.
    slug = "roi-review-" + keys.slug(base)
    try:
        overlay_b64 = base64.b64encode(Path(overlay_art.path).read_bytes()).decode()
        create_markdown_artifact(
            key=slug,
            markdown=(
                f"## general-detect review: {base}\n\n"
                f"WholeSlide uid `{slide.uid}` - {len(gdf)} ROI(s), {n_tma} TMA - "
                f"state general_pending.\n\n"
                f"![overlay](data:image/png;base64,{overlay_b64})"
            ),
            description=f"ROI detection overlay for {base}",
        )
        if len(gdf):
            create_table_artifact(
                key=f"{slug}-table",
                table=[
                    {
                        "class": r["class"],
                        "conf": round(float(r["conf"]), 3),
                        "bbox": [round(float(v), 1) for v in r.geometry.bounds],
                    }
                    for _, r in gdf.iterrows()
                ],
                description=f"Detections for {base}",
            )
        context_io.publish_context(
            ctx,
            image_b64=base64.b64encode(Path(thumb_art.path).read_bytes()).decode(),
            slug=keys.slug(base),
        )
    except Exception as exc:
        logger.warning(f"{base}: could not create review artifact: {exc}")

    logger.info(f"{base}: {len(gdf)} ROI(s) ({n_tma} TMA) -> general_pending")
    return {"slide": slide.name, "n_rois": len(gdf), "n_tma": n_tma, "review_key": slug}


def _thumb_png_bytes(img) -> bytes:
    """The thumbnail as a clean 1:1 PNG: the training image and the editor's background.

    Pixel-exact, so a box in thumbnail px maps to full resolution by one ``scale_factor`` multiply.
    """
    import io

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    buf = io.BytesIO()
    plt.imsave(buf, img, format="png")
    return buf.getvalue()


# retries: the HPC-to-VM postgres connection can time out at `import lamindb`.
@flow(name="general-detect", log_prints=True, retries=3, retry_delay_seconds=60)
def general_detect(
    target_directory: str,
    slide_uids: list[str] | None = None,
    model_alias: str = "production",
    conf: float = 0.3,
    nms_iou: float = 0.6,
    limit: int | None = None,
) -> dict:
    """Detect ROIs per WholeSlide, write a candidate prediction, and park it at general_pending.

    slide_uids: WholeSlide Record uids to detect on, in one job. Empty means batch mode: every
        WholeSlide at ``registered``.
    target_directory: results root on the HPC (a registered lamindb Storage root).
    nms_iou: IoU threshold for the overlap suppression applied to the raw boxes. 0.0 would collapse
        every overlapping box to one.
    limit: cap on slides processed this run (None = all).
    """
    from qptiff_pipeline.domain import qptiff as qptiff_vocab
    logger = get_run_logger()

    from pathlib import Path

    import clinicalomx as cx
    import lamindb as ln
    from bioio import BioImage
    from bioio_qptiff import Reader
    from clinicalomx import set_state
    from croi.objectives.tissue_objects.registry import load_champion, resolve_model_artifact
    from croi.utils.image_preprocess import (
        get_smallest_scale_image,
        grayscale_to_rgb,
        image_to_8bit,
        max_intensity_projection,
    )

    from qptiff_pipeline.domain import keys, states, vocab
    from qptiff_pipeline.domain.roi_io import write_roi_parquet
    from qptiff_pipeline.lamin.queries import select_registered_slide_uids
    from qptiff_pipeline.lamin.registry import get_or_create_artifact
    from qptiff_pipeline.lamin.tracking import track_prefect_run

    track_prefect_run("general-detect")
    try:
        # Batch mode: no uids, so sweep every WholeSlide in the general entry state.
        if not slide_uids:
            _, entry_state = states.STAGE_ENTRY["general"]
            slide_uids = select_registered_slide_uids()
            logger.info(f"batch mode: {len(slide_uids)} WholeSlide(s) in state {entry_state!r}")
        if limit:
            slide_uids = slide_uids[:limit]
        if not slide_uids:
            logger.info("no WholeSlides to detect; nothing to do")
            return {"processed": 0, "results": []}

        vocab.ensure()  # declares this repo's features (see vocab.py)
        # Loaded once and reused across the batch.
        detector = load_champion(model_alias, CHAMPION_CFG)
        model_art = resolve_model_artifact(model_alias)  # lamindb model Artifact the alias points at (or None)
        model_uid = getattr(model_art, "uid", None)  # model-versions the prediction keys + lineage
        logger.info(f"detector alias={model_alias!r} -> model_uid={model_uid}")
        results: list[dict] = []
        failed: list[tuple[str, str]] = []
        for uid in slide_uids:
            slide = ln.Record.get(uid=uid)
            src = slide.features.get_values().get("source_image")
            qptiff = src if isinstance(src, ln.Artifact) else ln.Artifact.get(key=src)
            base = Path(qptiff.key).stem

            # Integrity gate: a file check-qptiffs flagged unreadable marks the slide
            # general_failed.
            if qptiff.features.get_values().get(qptiff_vocab.INTEGRITY_FEATURE) == qptiff_vocab.INTEGRITY_UNREADABLE:
                set_state(slide, states.S.GENERAL_FAILED)
                failed.append((base, "quarantined: qptiff_integrity=unreadable"))
                logger.warning(f"skip {base}: qptiff_integrity=unreadable -> general_failed")
                continue

            # Output keys are model-versioned: re-running under the same champion skips the work.
            pred_key = keys.general_key(qptiff.key, "pred", model_uid)
            thumb_key = keys.general_key(qptiff.key, "thumbnail", model_uid)
            overlay_key = keys.general_key(qptiff.key, "detections", model_uid)
            okeys = (pred_key, thumb_key, overlay_key)

            def _compute(qptiff=qptiff, base=base, okeys=okeys) -> dict:
                """Read the image, detect, then write and register the three outputs."""
                pred_key, thumb_key, overlay_key = okeys
                ds = BioImage(
                    qptiff.path, reader=Reader
                ).reader.xarray_dask_scene_datatree["FullResolution"]
                img, factor = get_smallest_scale_image(ds, return_factor=True)
                img = grayscale_to_rgb(image_to_8bit(max_intensity_projection(img)))

                gdf = _detect_rois(detector, img, conf=conf, nms_iou=nms_iou)
                if not len(gdf):
                    logger.warning(
                        f"{base}: detector found 0 ROIs; parking empty candidate for review")
                # scale_factor maps thumbnail px to full-res px. roi_io keeps it in the parquet.
                gdf.attrs["scale_factor"] = float(factor)
                gdf.attrs["thumb_w"] = int(img.shape[1])
                gdf.attrs["thumb_h"] = int(img.shape[0])

                out = {}
                # Candidate prediction, before review.
                pq = f"{target_directory.rstrip('/')}/{pred_key}"
                Path(pq).parent.mkdir(parents=True, exist_ok=True)
                out[pred_key] = get_or_create_artifact(
                    pq,
                    key=pred_key,
                    description=f"General ROI predictions (candidate, pre-review) for {base}",
                    write=lambda g=gdf, p=pq: write_roi_parquet(g, p),
                )
                # Clean 1:1 thumbnail PNG: the training image and the editor's background.
                tpath = f"{target_directory.rstrip('/')}/{thumb_key}"
                out[thumb_key] = get_or_create_artifact(
                    tpath,
                    key=thumb_key,
                    description=f"Clean detection thumbnail for {base}",
                    # Rendered inside the thunk, so an existing key costs no PNG encode.
                    write=lambda p=tpath, im=img: Path(p).write_bytes(_thumb_png_bytes(im)),
                )
                # Detection overlay (boxes drawn on the thumbnail) as a durable PNG.
                opath = f"{target_directory.rstrip('/')}/{overlay_key}"
                out[overlay_key] = get_or_create_artifact(
                    opath,
                    key=overlay_key,
                    description=f"General detection overlay (boxes on thumbnail) for {base}",
                    write=lambda p=opath, im=img, g=gdf: Path(p).write_bytes(
                        _overlay_png_bytes(im, g)),
                )
                return out

            # A failure marks this slide general_failed and the batch continues. The cached path
            # never reaches compute, so it cannot fail here.
            def _reconcile(arts, slide=slide, qptiff=qptiff, okeys=okeys) -> None:
                results.append(_reconcile_general(
                    slide, arts,
                    okeys=okeys,
                    qptiff=qptiff,
                    model_art=model_art,
                    model_uid=model_uid,
                    model_alias=model_alias,
                    conf=conf,
                    nms_iou=nms_iou,
                    target_directory=target_directory,
                    logger=logger,
                ))

            try:
                cx.materialize(okeys, compute=_compute, reconcile=_reconcile, heal="delete")
            except Exception as exc:
                set_state(slide, states.S.GENERAL_FAILED)
                failed.append((base, f"detect failed: {type(exc).__name__}: {exc}"))
                logger.error(f"{base}: detect failed -> general_failed: {type(exc).__name__}: {exc}")
                continue


        # Failures are marked general_failed and excluded from later batches. No Teams alert.
        logger.info(
            f"general-detect done: {len(results)} -> general_pending, {len(failed)} failed"
        )
        return {
            "processed": len(results),
            "failed": len(failed),
            "results": results,
            "failed_slides": [b for b, _ in failed],
        }
    finally:
        ln.finish()
