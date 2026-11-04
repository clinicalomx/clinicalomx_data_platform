"""core-persist: the accept side of the core gate, per TMA. Runs on the HPC.

Maps the reviewed boxes from crop px to full-resolution px, writes the accepted core grid, registers
one ``Core`` Record per cell (named ``<tma>:R<r>C<c>``) and moves the TMA to ``core_approved``.
The reviewer's row,col per box is used as given. Only a payload without ``rc`` re-fits the grid.
"""
from __future__ import annotations

# Put the clone's src/ on sys.path: the HPC runs a git clone, not an install.
import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)


from prefect import flow, get_run_logger

STAGE = "core"


def _store_accepted_context(tma, cand_gdf, factor: float, crop_x: float, crop_y: float, logger) -> None:
    """Store the accepted state as a typed context on the TMA, rebuilt from ``F_CORRECTED``.

    Best effort: a TMA that has registered its Cores never fails on this write.
    """
    import json

    from qptiff_pipeline.domain import contexts as ctxmod
    from qptiff_pipeline.domain import vocab
    from qptiff_pipeline.lamin import context_io

    try:
        vals = tma.features.get_values()
        corrected = vals.get(vocab.F_CORRECTED)
        if not corrected:
            return  # nothing a human accepted (candidate-as-is path), so no context to write
        space = ctxmod.Space(
            name="crop",
            width=int(cand_gdf.attrs.get("thumb_w") or 1),
            height=int(cand_gdf.attrs.get("thumb_h") or 1),
            scale_factor=float(factor),
            origin_x=float(crop_x),
            origin_y=float(crop_y),
        )
        ctx = ctxmod.CoreDetectContext(
            unit_name=tma.name,
            unit_uid=getattr(tma, "uid", None),
            source_image=context_io.to_artifact_ref(vals.get("source_image")),
            raw=ctxmod.DetectionSet(
                boxes=[
                    ctxmod.Box.from_legacy({
                        "class": r["class"], "conf": r.get("conf"), "xyxy": list(r.geometry.bounds),
                    })
                    for _i, r in cand_gdf.iterrows()
                    if r.geometry is not None
                ],
                space=space,
            ),
            accepted=ctxmod.AcceptedSet(
                boxes=[ctxmod.Box.from_legacy(b) for b in json.loads(corrected)],
                space=space,
                modified=bool(vals.get(vocab.F_MODIFIED)),
            ),
        )
        context_io.write_context(tma, ctx)
        logger.info(f"{tma.name}: stored accepted context ({len(ctx.accepted.boxes)} cell(s))")
    except Exception as exc:
        logger.warning(f"{tma.name}: could not store the accepted context: {exc}")


def _dearray_overlay_png(img, cells) -> bytes:
    """The accepted dearray grid drawn on the TMA crop, as PNG bytes.

    Detected cells solid, inferred cells dashed, coloured by class, labelled ``row,col``.
    """
    import io

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    colors = {"full_core": "cyan", "partial_core": "yellow", "inferred_core": "magenta"}
    has_det = "is_detected" in cells.columns
    has_rc = "row" in cells.columns and "col" in cells.columns

    fig, ax = plt.subplots(figsize=(12, 12))
    ax.imshow(img)
    for _, r in cells.iterrows():
        geom = r.get("geometry")
        if geom is None:
            continue
        x1, y1, x2, y2 = geom.bounds
        cls = str(r.get("class", "full_core"))
        col = colors.get(cls, "red")
        detected = bool(r["is_detected"]) if has_det else bool(r.get("occupied", True))
        ax.add_patch(Rectangle(
            (x1, y1), x2 - x1, y2 - y1, fill=False, edgecolor=col,
            linewidth=1.4, linestyle="-" if detected else "--"))
        if has_rc and r.get("row") is not None:
            try:
                ax.text(x1, max(0, y1 - 3), f"{int(r['row'])},{int(r['col'])}", color=col,
                        fontsize=7, va="bottom",
                        bbox={"facecolor": "black", "alpha": 0.35, "pad": 0.5, "edgecolor": "none"})
            except (TypeError, ValueError):
                pass  # a non-numeric row/col just goes unlabelled; the box is already drawn
    ax.axis("off")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=110)
    plt.close(fig)
    return buf.getvalue()


@flow(name="core-persist", log_prints=True, retries=3, retry_delay_seconds=60)
def core_persist(
    tma_uids: list[str],
    target_directory: str,
    detected_only: bool = False,
) -> dict:
    """Register accepted Core Records for each TMA and advance it to core_approved.

    tma_uids: TMA Record uids whose core detection was accepted.
    detected_only: skip inferred cells. False registers the full grid.
    """
    logger = get_run_logger()

    import json
    from pathlib import Path

    import clinicalomx as cx
    import geopandas as gpd
    import lamindb as ln
    from clinicalomx import ROI_ID_COLUMN, annotate, newest_artifact, set_state
    from clinicalomx.names import FEATURES
    from croi.objectives.tma_core.gridding import estimate_grid
    from shapely.geometry import box

    from qptiff_pipeline.domain import contexts as ctxmod
    from qptiff_pipeline.domain import keys, states, vocab
    from qptiff_pipeline.domain.roi_io import read_roi_parquet, write_roi_parquet
    from qptiff_pipeline.lamin.linking import link_input
    from qptiff_pipeline.lamin.registry import (
        get_or_create_artifact,
        get_or_create_record,
        get_or_create_type,
    )
    from qptiff_pipeline.lamin.tracking import track_prefect_run

    track_prefect_run("core-persist")
    try:
        core_type = get_or_create_type(states.CORE_TYPE)
        vocab.ensure()  # this repo's declared features (see vocab.py)

        # The persist-run uid makes each accepted key unique.
        run_uid = getattr(getattr(ln.context, "run", None), "uid", None) or "unknown"

        out: list[dict] = []
        for uid in tma_uids:
            tma = ln.Record.get(uid=uid)
            tvals = tma.features.get_values()
            src = tvals.get("source_image")
            qptiff = src if isinstance(src, ln.Artifact) else ln.Artifact.get(key=src)

            # `roi_pred` is multi-valued, one value per model. `newest_artifact` picks the current
            # one. Never rebuild the key: it needs the model uid.
            cand = newest_artifact(tvals.get(vocab.F_PRED))
            if cand is None:
                logger.warning(f"{tma.name}: no candidate core grid found; skipping")
                continue
            cand_gdf = read_roi_parquet(cand.path)
            factor = float(cand_gdf.attrs.get("scale_factor", 1.0))
            crop_x = float(cand_gdf.attrs.get("crop_x", 0.0))
            crop_y = float(cand_gdf.attrs.get("crop_y", 0.0))

            corrected_json = tvals.get(vocab.F_CORRECTED)
            modified = bool(tvals.get(vocab.F_MODIFIED))
            # Reviewer-edited boxes (CROP px): [{class, xyxy, inferred?, rc?}]
            src_boxes = json.loads(corrected_json) if corrected_json else []
            have_rc = bool(src_boxes) and all(b.get("rc") for b in src_boxes)
            if have_rc:
                # The reviewer's grid is authoritative: one Core per accepted box, at the row,col
                # the reviewer approved. Never re-fit here: it would undo their corrections.
                # contexts.accepted_cells is the one place that decides Core identity.
                cells = ctxmod.accepted_cells(
                    ctxmod.AcceptedSet(
                        boxes=[ctxmod.Box.from_legacy(b) for b in src_boxes],
                        space=ctxmod.Space(name="crop", width=1, height=1,
                                          scale_factor=factor, origin_x=crop_x, origin_y=crop_y),
                        modified=modified,
                    )
                )
                grid_gdf = gpd.GeoDataFrame(
                    [{**{k: v for k, v in c.items() if k != "xyxy"},
                      "geometry": box(*c["xyxy"])} for c in cells],
                    geometry="geometry",
                ).reset_index(drop=True)
                dupes = grid_gdf.duplicated(subset=["row", "col"], keep=False)
                if dupes.any():
                    # Two boxes on one cell collapse to a single Core and one is lost. Log it.
                    clashing = sorted(
                        {f"R{r}C{c}" for r, c in
                         grid_gdf.loc[dupes, ["row", "col"]].itertuples(index=False)}
                    )
                    logger.warning(
                        f"{tma.name}: accepted grid has {int(dupes.sum())} box(es) sharing "
                        f"{len(clashing)} cell(s) ({', '.join(clashing)}); each cell yields ONE Core"
                    )
                logger.info(f"{tma.name}: using the reviewer's accepted grid ({len(grid_gdf)} cell(s), no re-fit)")
            elif src_boxes:
                # Payload without rc: re-fit the grid so row,col naming holds.
                logger.info(f"{tma.name}: accepted boxes carry no row,col; re-fitting the grid")
                det = gpd.GeoDataFrame(
                    [{"class": b["class"], "conf": 1.0, "geometry": box(*b["xyxy"])} for b in src_boxes],
                    geometry="geometry",
                ).reset_index(drop=True)
                grid_gdf = estimate_grid(det, expected_shape=None).to_geodataframe().reset_index(drop=True)
                grid_gdf["geometry"] = gpd.GeoSeries(
                    [g if g is not None else ig for g, ig in zip(grid_gdf["geometry"], grid_gdf["ideal_geometry"])]
                )
                # is_detected follows the reviewer's flag. A grid-only cell the re-fit added
                # (detection_idx == -1) is inferred. Payloads without the flag use occupancy.
                reviewer_inferred = [bool(b.get("inferred", False)) for b in src_boxes]
                has_flag = any("inferred" in b for b in src_boxes)

                # Defaults bind this TMA's values now, not at call time.
                def _detected(didx: int, occupied: bool,
                              has_flag=has_flag, reviewer_inferred=reviewer_inferred) -> bool:
                    if not has_flag:
                        return occupied
                    if 0 <= didx < len(reviewer_inferred):
                        return not reviewer_inferred[didx]
                    return False  # grid-only cell (no reviewed box landed here)

                grid_gdf["is_detected"] = [
                    _detected(int(di), bool(oc))
                    for di, oc in zip(grid_gdf["detection_idx"], grid_gdf["occupied"])
                ]
            else:
                grid_gdf = cand_gdf

            # The accepted dearray overlay, drawn on the clean crop in crop px, saved and linked to
            # the TMA as F_DEARRAY_OVERLAY.
            try:
                thumb_art = newest_artifact(tvals.get(vocab.F_THUMBNAIL))
                if thumb_art is not None:
                    import matplotlib
                    matplotlib.use("Agg")
                    import matplotlib.pyplot as plt

                    crop_img = plt.imread(str(thumb_art.path))
                    okey = keys.core_accepted_key(qptiff.key, tma.name, run_uid, "dearray")
                    opath = f"{target_directory.rstrip('/')}/{okey}"
                    Path(opath).parent.mkdir(parents=True, exist_ok=True)
                    ov_bytes = _dearray_overlay_png(crop_img, grid_gdf)
                    ov_art = get_or_create_artifact(
                        opath, key=okey,
                        description=f"Accepted dearray grid overlay for {tma.name}",
                        write=lambda b=ov_bytes, p=opath: Path(p).write_bytes(b),
                    )
                    annotate(tma, ov_art, vocab.F_DEARRAY_OVERLAY)
            except Exception as exc:
                logger.warning(f"{tma.name}: could not render/save dearray overlay: {exc}")

            # Accepted core grid in full-res px. The crop offset and scale are bound as defaults,
            # so this TMA can never pick up the next TMA's crop origin.
            def _to_full(geom, crop_x=crop_x, crop_y=crop_y, factor=factor):
                x1, y1, x2, y2 = geom.bounds
                return box((x1 + crop_x) * factor, (y1 + crop_y) * factor,
                           (x2 + crop_x) * factor, (y2 + crop_y) * factor)

            accepted = grid_gdf.copy()
            accepted["geometry"] = gpd.GeoSeries([_to_full(g) for g in grid_gdf["geometry"]])
            accepted = accepted.drop(columns=[c for c in ("ideal_geometry",) if c in accepted]).reset_index(drop=True)
            # Stable row id that consumers match on, built from the same row,col that names the
            # Core.
            accepted[ROI_ID_COLUMN] = [
                f"R{int(accepted.loc[i, 'row'])}C{int(accepted.loc[i, 'col'])}"
                if "row" in accepted.columns else f"R{i}C0"
                for i in accepted.index
            ]
            accepted.attrs["scale_factor"] = 1.0
            accepted.attrs["source_scale_factor"] = factor

            key = keys.core_accepted_key(qptiff.key, tma.name, run_uid, "cores")
            pq = f"{target_directory.rstrip('/')}/{key}"
            Path(pq).parent.mkdir(parents=True, exist_ok=True)
            cores_art = get_or_create_artifact(
                pq, key=key, description=f"Accepted core grid (full-res) for {tma.name}",
                write=lambda g=accepted, p=pq: write_roi_parquet(g, p),
            )
            # The accepted grid parquet holds every cell of the TMA, so it is annotated on the TMA.
            # The Cores reference it by `parquet_row`.
            annotate(tma, cores_art, vocab.F_CORES_ACCEPTED)
            # Lineage: the candidate grid is the input of this persist run.
            link_input(cores_art, cand)

            n_cores = 0

            minted: list[tuple] = []  # (core_roi, artifact) pairs for cx.split
            for r in accepted.index:
                cell = accepted.loc[r]
                # The reviewer's is_detected where set, else grid occupancy.
                detected = bool(cell.get("is_detected", cell.get("occupied", True)))
                if detected_only and not detected:
                    continue
                row_i = int(cell["row"]) if "row" in cell else r
                col_i = int(cell["col"]) if "col" in cell else 0
                cls = str(cell.get("class", "full_core"))
                core = get_or_create_record(
                    name=f"{tma.name}:R{row_i}C{col_i}",
                    type=core_type,
                    feature_values={
                        "tma": tma, "source_image": qptiff, "source_artifact": cores_art,
                        # parquet_row serves older parquets. New readers match on roi_id.
                        "parquet_row": int(r), FEATURES.ROI_ID: f"R{row_i}C{col_i}",
                        "row": row_i, "col": col_i,
                        "is_detected": detected, "core_completeness": cls,
                    },
                )
                minted.append((core, None))
                n_cores += 1

            # core_approved waits for human triage. A reviewer queues multiplex TMAs for bgnorm.
            # `split` sets `parent_roi` on each Core.
            if minted:
                cx.split(cand, minted, vocab.F_CORES_ACCEPTED, parent=tma)

            set_state(tma, states.S.CORE_APPROVED)
            # Keep the reviewer's accepted boxes on the record. The review app needs them in review
            # px to redraw what was accepted.
            _store_accepted_context(tma, cand_gdf, factor, crop_x, crop_y, logger)
            logger.info(f"{tma.name}: registered {n_cores} Core(s) -> core_approved (modified={modified})")
            out.append({"tma": tma.name, "cores": n_cores, "modified": modified})

        logger.info(f"core-persist done: {len(out)} TMA(s) -> core_approved")
        return {"processed": len(out), "results": out}
    finally:
        ln.finish()
