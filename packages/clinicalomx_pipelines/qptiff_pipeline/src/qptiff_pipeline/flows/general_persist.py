"""general-persist: the accept side of the general gate. Runs on the HPC.

Writes the accepted ROI parquet, registers a TMA Record per accepted TMA at ``registered``, and
moves the WholeSlide to ``general_approved``. tissue_section boxes get no Record.
"""
from __future__ import annotations

# Put the clone's src/ on sys.path: the HPC runs a git clone, not an install.
import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from prefect import flow, get_run_logger

# event a per-TMA registration emits; the core-detect Automation matches it.
TMA_REGISTERED_EVENT = "clinical-roi.tma.registered"


# retries: the HPC-to-VM postgres connection can time out at `import lamindb`.
@flow(name="general-persist", log_prints=True, retries=3, retry_delay_seconds=60)
def general_persist(
    slide_uids: list[str],
    target_directory: str,
) -> dict:
    """Register accepted TMA Records for each WholeSlide and advance it to general_approved.

    slide_uids: WholeSlide Record uids whose general detection was accepted.
    target_directory: results root on the HPC (a registered lamindb Storage root).
    """
    logger = get_run_logger()

    import json
    from pathlib import Path

    import clinicalomx as cx
    import geopandas as gpd
    import lamindb as ln
    from clinicalomx import ROI_ID_COLUMN, annotate, newest_artifact, set_state
    from clinicalomx.names import FEATURES
    from shapely.geometry import box

    from qptiff_pipeline.domain import keys, states, vocab
    from qptiff_pipeline.domain import qptiff as qptiff_vocab
    from qptiff_pipeline.domain.roi_io import read_roi_parquet, write_roi_parquet
    from qptiff_pipeline.lamin.linking import link_input
    from qptiff_pipeline.lamin.registry import (
        get_or_create_artifact,
        get_or_create_record,
        get_or_create_type,
    )
    from qptiff_pipeline.lamin.tracking import track_prefect_run
    from qptiff_pipeline.orchestration.events import emit

    track_prefect_run("general-persist")
    try:
        tma_type = get_or_create_type(states.TMA_TYPE)
        vocab.ensure()  # this repo's declared features (see vocab.py)

        # The persist-run uid makes each accepted key unique.
        run_uid = getattr(getattr(ln.context, "run", None), "uid", None) or "unknown"

        out: list[dict] = []
        for uid in slide_uids:
            slide = ln.Record.get(uid=uid)
            src = slide.features.get_values().get("source_image")
            qptiff = src if isinstance(src, ln.Artifact) else ln.Artifact.get(key=src)
            base = Path(qptiff.key).stem

            # `roi_pred` is multi-valued, one value per model. `newest_artifact` picks the current
            # one. Never rebuild the key: it needs the model uid.
            cand = newest_artifact(slide.features.get_values().get(vocab.F_PRED))
            if cand is None:
                logger.warning(f"{base}: no candidate prediction found; skipping")
                continue

            # scale_factor (thumbnail px -> full-res px) is in the candidate parquet's attrs. The
            # reviewed boxes come from F_CORRECTED. Without an edit, the candidate boxes are used.
            pred_gdf = read_roi_parquet(cand.path)
            factor = float(pred_gdf.attrs.get("scale_factor", 1.0))
            vals = slide.features.get_values()
            corrected_json = vals.get(vocab.F_CORRECTED)
            modified = bool(vals.get(vocab.F_MODIFIED))
            if corrected_json:
                src_boxes = json.loads(corrected_json)  # [{class, xyxy}] thumbnail px
            else:
                src_boxes = [
                    {"class": r["class"], "xyxy": list(r.geometry.bounds)}
                    for _, r in pred_gdf.iterrows()
                ]

            # Accepted ROIs in full-resolution px, the space core-detect crops from.
            accepted = gpd.GeoDataFrame(
                [
                    {
                        "class": b["class"],
                        "geometry": box(*[float(v) * factor for v in b["xyxy"]]),
                    }
                    for b in src_boxes
                ],
                geometry="geometry",
            ).reset_index(drop=True)
            # Stable row id that core-detect matches on. A TMA row gets `TMA<i>`, matching its child
            # Record's name. A tissue_section row gets `TS<i>`.
            accepted[ROI_ID_COLUMN] = [
                (f"TMA{i}" if accepted.loc[i, "class"] == "TMA" else f"TS{i}")
                for i in accepted.index
            ]
            accepted.attrs["scale_factor"] = 1.0
            accepted.attrs["source_scale_factor"] = factor
            key = keys.general_accepted_key(qptiff.key, run_uid)
            pq = f"{target_directory.rstrip('/')}/{key}"
            Path(pq).parent.mkdir(parents=True, exist_ok=True)
            roi_art = get_or_create_artifact(
                pq,
                key=key,
                description=f"Accepted general ROIs (full-res) for {base}",
                write=lambda g=accepted, p=pq: write_roi_parquet(g, p),
            )
            # The accepted parquet holds every ROI of the slide, so it is annotated on the slide.
            # The children reference it by `parquet_row`.
            annotate(slide, roi_art, vocab.F_ROI_ACCEPTED)
            # Lineage: the candidate prediction is the input of this persist run.
            link_input(roi_art, cand)

            # Modality copied from the slide onto each TMA, for the triage gate.
            tma_modality = {
                f: vals[f]
                for f in (vocab.IMAGE_TYPE, qptiff_vocab.QPTIFF_N_CHANNELS)
                if vals.get(f) is not None
            }
            tma_names: list[str] = []
            minted: list[tuple] = []  # (tma_roi, artifact) pairs for cx.split
            for row in accepted[accepted["class"] == "TMA"].index:
                tma = get_or_create_record(
                    name=f"{base}:TMA{row}",
                    type=tma_type,
                    feature_values={
                        "slide": slide,
                        "source_image": qptiff,
                        "source_artifact": roi_art,
                        # parquet_row serves older parquets. New readers match on roi_id.
                        "parquet_row": int(row),
                        FEATURES.ROI_ID: f"TMA{row}",
                        **tma_modality,
                    },
                )
                minted.append((tma, None))
                set_state(tma, states.S.REGISTERED)
                try:
                    emit(
                        TMA_REGISTERED_EVENT,
                        resource={"prefect.resource.id": f"clinicalomx.tma.{tma.uid}"},
                        payload={"tma_uid": tma.uid, "slide": slide.name},
                    )
                except Exception as exc:  # emission is best-effort (no-op w/o Automations)
                    logger.warning(f"could not emit {TMA_REGISTERED_EVENT}: {exc}")
                tma_names.append(tma.name)

            # `split` writes `parent_roi` on each child. The child artifact is None: the geometry
            # is the shared parquet annotated on the slide.
            if minted:
                cx.split(cand, minted, vocab.F_ROI_ACCEPTED, parent=slide)

            # No training data here: harvest-feedback collects it from the accepted artifacts.
            set_state(slide, states.S.GENERAL_APPROVED)
            logger.info(
                f"{base}: registered {len(tma_names)} TMA(s) -> general_approved"
            )
            out.append(
                {"slide": slide.name, "tmas": tma_names, "modified": modified}
            )

        logger.info(f"general-persist done: {len(out)} slide(s) -> general_approved")
        return {"processed": len(out), "results": out}
    finally:
        ln.finish()
