"""Extract thumbnail PNGs from QPTIFF artifacts and mint their WholeSlide ROIs.

    QPTIFF Artifact
        --[ step_ingest ]-->  thumbnail PNG Artifact  --annotates-->  WholeSlide ROI

Re-runs skip the PNG but re-attach the ROI.
"""
from __future__ import annotations

from pathlib import Path

import lamindb as ln

import clinicalomx as cx
from clinicalomx import InputRef, materialize

from croi.utils import roi_records

from croi.objectives.tissue_objects import environment, vocab

ROI_FEATURE = vocab.DETECTIONS

# Set by `run`: an @ln.step takes only JSON-serializable args.
_OUTPUT_STORAGE = None


@ln.step()
def step_ingest(qptiff_art: ln.Artifact, config: dict) -> ln.Artifact:
    """Ingest one QPTIFF into a thumbnail PNG Artifact on a WholeSlide ROI."""
    import bioio_qptiff
    import imageio
    from bioio import BioImage

    stem = qptiff_art.path.stem
    out = config["outputs"]["thumbnails"]
    png_key = out["key_template"].format(stem=stem)

    def compute():
        image = BioImage(qptiff_art.path, reader=bioio_qptiff.Reader)
        thumbnail = image.reader.xarray_dask_scene_datatree["Thumbnail"].image
        out_dir = Path(_OUTPUT_STORAGE.path) / Path(png_key).parent
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{stem}.png"
        imageio.v3.imwrite(out_path, thumbnail)
        png_art = InputRef(kind="artifact", key=png_key).ensure(
            out_path, storage=_OUTPUT_STORAGE, force=config.get("parameters", {}).get("force", False)
        )
        png_art.run.input_artifacts.add(qptiff_art)  # only after save
        print(f"  ingested {qptiff_art.key} -> {png_key}")
        return {png_key: png_art}

    # Runs on fresh and skipped thumbnails alike, so a re-run backfills the ROI.
    def reconcile(arts):
        slide = roi_records.get_or_create_roi(roi_records.slide_name(stem), roi_records.TYPE_SLIDE)
        cx.create(slide, arts[png_key], ROI_FEATURE)
        print(f"  {ROI_FEATURE} -> WholeSlide ROI {slide.name}")

    return materialize([png_key], compute=compute, reconcile=reconcile)[png_key]


def _resolve_inputs(config: dict) -> list[ln.Artifact]:
    """QPTIFF artifacts matching the ``inputs.qptiffs`` filter, capped by ``config['limit']``."""
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
def run_ingest(config: dict, project: ln.Project) -> None:
    qptiffs = _resolve_inputs(config)
    print(f"ingesting thumbnails for {len(qptiffs)} qptiff artifact(s)")
    png_arts, n_done, n_err = [], 0, 0
    for qp in qptiffs:
        try:
            png_arts.append(step_ingest(qp, config))
            n_done += 1
        except Exception as e:  # corrupt QPTIFF: stamp data_failure, skip
            print(f"  ERROR {qp.key}: {type(e).__name__}: {str(e)[:160]}")
            qp.features.set_values({vocab.DATA_FAILURE: str(e)})
            n_err += 1

    thumbnails = config["outputs"]["thumbnails"]
    key = thumbnails["key"]
    latest = ln.Collection.filter(key=key, is_latest=True).one_or_none()
    new = {a.uid for a in png_arts}
    if not (latest and {a.uid for a in latest.ordered_artifacts} == new):
        ln.Collection(artifacts=png_arts, key=key, revises=latest,
                      description=thumbnails["description"], skip_hash_lookup=True).save()
    print(f"\n{n_done} ingested, {n_err} errored.")


def run(config: dict, *, limit: int | None = None) -> None:
    """Stage entry point: preflight, then the flow."""
    global _OUTPUT_STORAGE
    stage = environment.preflight()
    _OUTPUT_STORAGE = stage.storage
    if limit:
        config["limit"] = limit
    run_ingest(config=config, project=stage.project)
