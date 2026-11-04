"""The two offline extractors that cut the substrates the `tma_core` objective trains on.

``tma_crops`` runs the served detector on each thumbnail PNG and crops every ``TMA`` detection
above ``min_conf`` into a frozen crop-release Collection, versioned by the model that cut it.
Each crop becomes a child ``TMA`` ROI (``<slide>:TMA<i>``) of the thumbnail's ``WholeSlide`` ROI.
``pyramid_low_scale`` renders a channel projection from each QPTIFF's smallest pyramid level.
It does not depend on any model.
"""
from __future__ import annotations

from pathlib import Path

import lamindb as ln

import clinicalomx as cx
from clinicalomx import InputRef, materialize

from croi.utils import roi_records
from croi.detection.backends import detector_for

from croi.objectives.tissue_objects import environment, vocab
from croi.objectives.tissue_objects.tma_crops import extract_tma_crop_from_thumbnail
from croi.objectives.tissue_objects import registry as _models

ROI_FEATURE = vocab.DETECTIONS

# Set by the `run_*` entry points: an @ln.step takes only JSON-serializable args.
_OUTPUT_STORAGE = None
_MODEL = None       # the inference backend (detector_for)
_MODEL_REF = None   # the cx.ModelRef, for lineage and release versioning


def _resolve_model(sel: dict):
    """ModelRef for the general champion from the yaml ``inputs.model`` selector."""
    return _models.model_ref(sel)


def _detect_tmas(thumb_art: ln.Artifact, params: dict) -> list:
    """Wanted TMA detections on a thumbnail, sorted top-left to bottom-right.

    The stable order keeps ``TMA<i>`` indices deterministic across re-runs.
    """
    target = params.get("target_class", "TMA")
    conf = float(params.get("conf", 0.25))
    min_conf = float(params.get("min_conf", 0.75))
    dets = _MODEL.predict(str(thumb_art.path), conf=conf, imgsz=None)
    wanted = [d for d in dets if d.class_name == target and d.confidence >= min_conf]
    return sorted(wanted, key=lambda d: (round(float(d.xyxy[1]), 1), round(float(d.xyxy[0]), 1)))


@ln.step()
def step_crop(thumb_art: ln.Artifact, config: dict) -> list:
    """Crop every TMA detection out of one thumbnail into Artifacts and TMA ROIs."""
    params = config["parameters"]
    out = config["outputs"]["crops"]
    stem = Path(thumb_art.key).stem
    slide = roi_records.slide_name(stem)

    wanted = _detect_tmas(thumb_art, params)
    if not wanted:
        return []
    crop_keys = [out["key_template"].format(slide=slide, i=i) for i in range(len(wanted))]

    def compute():
        out_dir = Path(_OUTPUT_STORAGE.path) / out["key_prefix"] / stem
        out_dir.mkdir(parents=True, exist_ok=True)
        arts = {}
        for i, det in enumerate(wanted):
            crop, meta = extract_tma_crop_from_thumbnail(thumb_art.path, tuple(det.xyxy))
            out_path = out_dir / f"tma_{i}.png"
            crop.save(out_path, "PNG", optimize=True)
            art = ln.Artifact(
                out_path, key=crop_keys[i], storage=_OUTPUT_STORAGE,
                description=(f"TMA thumbnail crop from {thumb_art.key} "
                            f"(conf={det.confidence:.3f})."),
            ).save()
            art.run.input_artifacts.add(thumb_art)
            _MODEL_REF.consume(art, ensure=True, storage=_OUTPUT_STORAGE)
            arts[crop_keys[i]] = art
            print(f"  {crop_keys[i]}  conf={det.confidence:.2f}")
        return arts

    # WholeSlide ROI -> one TMA child ROI per crop.
    def reconcile(arts):
        parent = cx.resolve_roi(thumb_art) or roi_records.get_or_create_roi(
            slide, roi_records.TYPE_SLIDE)
        children = []
        for i, det in enumerate(wanted):
            x1, y1, x2, y2 = (float(v) for v in det.xyxy)
            tma_roi = roi_records.get_or_create_roi(
                roi_records.tma_name(slide, i), roi_records.TYPE_TMA,
                detection_class_name=det.class_name, detection_confidence=float(det.confidence),
                bbox_x1=x1, bbox_y1=y1, bbox_x2=x2, bbox_y2=y2,
            )
            children.append((tma_roi, arts[crop_keys[i]]))
        cx.split(thumb_art, children, ROI_FEATURE, parent=parent)
        print(f"  split WholeSlide {parent.name} -> {len(children)} TMA ROI(s)")

    return list(materialize(crop_keys, compute=compute, reconcile=reconcile).values())


def _resolve_thumbnails(config: dict) -> list[ln.Artifact]:
    coll = InputRef(**config["inputs"]["thumbnails"]).get()
    pngs = [a for a in coll.ordered_artifacts if a.suffix == ".png"]
    limit = config.get("limit")
    return pngs[:limit] if limit else pngs


@ln.flow()
def run_extract_tma_crops(config: dict, project: ln.Project) -> None:
    global _MODEL, _MODEL_REF
    _MODEL_REF = _resolve_model(config["inputs"]["model"])
    print(f"Cropping with model: {_MODEL_REF}\n  weights: {_MODEL_REF.path}")
    backend_cfg = dict(config["parameters"].get("backend") or {})
    backend_cfg["weights"] = {"path": str(_MODEL_REF.path)}
    backend_cfg.setdefault("imgsz", config["parameters"].get("imgsz", 640))
    # An `ensemble` member may name an alias of this objective.
    _MODEL = detector_for(backend_cfg, resolve_alias=_models.resolve_weights)

    thumbnails = _resolve_thumbnails(config)
    print(f"Detecting TMAs over {len(thumbnails)} thumbnails")
    crop_arts, n_err = [], 0
    for thumb in thumbnails:
        try:
            crop_arts.extend(step_crop(thumb, config))
        except Exception as e:
            print(f"  ERROR {thumb.key}: {type(e).__name__}: {str(e)[:160]}")
            n_err += 1

    if not crop_arts:
        print("No crops produced; skipping release.")
        return

    # Frozen crop release, attributed to and versioned by the model.
    rel = config["outputs"]["release"]
    key = rel["key"]
    version = str(config["parameters"].get("release_version") or _MODEL_REF.version)
    new = {a.uid for a in crop_arts}
    latest = ln.Collection.filter(key=key, is_latest=True).one_or_none()
    if latest and {a.uid for a in latest.ordered_artifacts} == new:
        print(f"  Release {key} v{latest.version} unchanged (membership identical).")
    else:
        coll = ln.Collection(
            artifacts=crop_arts, key=key, version=version, revises=latest,
            description=f"{rel['description']} Cut by {_MODEL_REF.key} v{_MODEL_REF.version}.",
            skip_hash_lookup=True,
        ).save()
        _MODEL_REF.consume(coll, ensure=True, storage=_OUTPUT_STORAGE)
        print(f"  Release: {coll.key} v{coll.version}  <- model {_MODEL_REF.key}")
    print(f"\n{len(crop_arts)} crops, {n_err} thumbnails errored.")


def run_tma_crops(config: dict, *, limit: int | None = None) -> None:
    """Stage entry point for the TMA crop release."""
    global _OUTPUT_STORAGE
    stage = environment.preflight(mlflow_env=True)
    _OUTPUT_STORAGE = stage.storage
    if limit:
        config["limit"] = limit
    run_extract_tma_crops(config=config, project=stage.project)


# --- pyramid_low_scale ------------------------------------------------------------------
#
# Per QPTIFF: read scene 0's lowest-resolution level, project channels (max for multiplex,
# mean for H&E), downsample to target_max_edge, percentile-normalise to uint8.
# The projection annotates the slide's WholeSlide ROI, get-or-created by its deterministic name.

@ln.step()
def step_project(qptiff_art: ln.Artifact, config: dict) -> ln.Artifact:
    """Render one QPTIFF to a low-res projection PNG Artifact on its WholeSlide ROI."""
    from croi.objectives.tissue_objects.pyramid_projection import qptiff_to_grayscale_image

    stem = Path(qptiff_art.key).stem
    out = config["outputs"]["projections"]
    png_key = out["key_template"].format(stem=stem)
    params = config["parameters"]

    def compute():
        img, meta = qptiff_to_grayscale_image(
            qptiff_art.path,
            target_max_edge=int(params.get("target_max_edge", 1024)),
            mode=params.get("mode", "auto"),
        )
        out_dir = Path(_OUTPUT_STORAGE.path) / Path(png_key).parent
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{stem}.png"
        img.save(out_path, "PNG", optimize=True)
        art = ln.Artifact(
            out_path, key=png_key, storage=_OUTPUT_STORAGE,
            description=(
                f"Low-res pyramid {meta['projection_mode']}-projection of {qptiff_art.key} "
                f"({meta['n_channels']} channels, "
                f"output {meta['output_size_yx'][1]}x{meta['output_size_yx'][0]})."
            ),
        ).save()
        art.run.input_artifacts.add(qptiff_art)
        # Rendering params stay on the projection Artifact; declared in tissue_objects.vocab.
        art.features.set_values({
            vocab.SMALLEST_PYRAMID_H: int(meta["smallest_pyramid_size_yx"][0]),
            vocab.SMALLEST_PYRAMID_W: int(meta["smallest_pyramid_size_yx"][1]),
            vocab.N_CHANNELS: int(meta["n_channels"]),
            vocab.PROJECTION_MODE: meta["projection_mode"],
        })
        print(f"  projected {qptiff_art.key} -> {png_key}")
        return {png_key: art}

    def reconcile(arts):
        slide = roi_records.get_or_create_roi(roi_records.slide_name(stem), roi_records.TYPE_SLIDE)
        cx.annotate(slide, arts[png_key], ROI_FEATURE)
        print(f"  {ROI_FEATURE} -> WholeSlide ROI {slide.name}")

    return materialize([png_key], compute=compute, reconcile=reconcile)[png_key]


def _resolve_qptiffs(config: dict) -> list[ln.Artifact]:
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
def run_extract_pyramid_low_scale(config: dict, project: ln.Project) -> None:
    qptiffs = _resolve_qptiffs(config)
    print(f"projecting {len(qptiffs)} qptiff artifact(s)")
    arts, n_done, n_err = [], 0, 0
    for qp in qptiffs:
        try:
            arts.append(step_project(qp, config))
            n_done += 1
        except Exception as e:
            print(f"  ERROR {qp.key}: {type(e).__name__}: {str(e)[:160]}")
            n_err += 1

    if not arts:
        print("No projections produced; skipping release.")
        return

    # Frozen handoff Collection; adds a version only on membership change.
    rel = config["outputs"]["release"]
    key = rel["key"]
    version = str(config["parameters"].get("release_version", "1"))
    new = {a.uid for a in arts}
    latest = ln.Collection.filter(key=key, is_latest=True).one_or_none()
    if latest and {a.uid for a in latest.ordered_artifacts} == new:
        print(f"  Release {key} v{latest.version} unchanged (membership identical).")
    else:
        coll = ln.Collection(artifacts=arts, key=key, version=version, revises=latest,
                             description=rel["description"], skip_hash_lookup=True).save()
        print(f"  Release: {coll.key} v{coll.version}  uid={coll.uid}")
    print(f"\n{n_done} projected, {n_err} errored.")


def run_pyramid_low_scale(config: dict, *, limit: int | None = None) -> None:
    """Stage entry point for the pyramid projection release."""
    global _OUTPUT_STORAGE
    stage = environment.preflight()
    _OUTPUT_STORAGE = stage.storage
    if limit:
        config["limit"] = limit
    run_extract_pyramid_low_scale(config=config, project=stage.project)
