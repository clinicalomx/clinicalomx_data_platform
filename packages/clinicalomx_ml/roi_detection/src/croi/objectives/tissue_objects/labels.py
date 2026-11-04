"""Label ingestion: model Label Studio as a lamindb Transform, and register its exports.

Each Label Studio export is one ``Run`` of a long-lived ``Transform`` (kind ``pipeline``)::

    PNG collection  --input-->  [ Label Studio export Run ]  --output-->  labels
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

import lamindb as ln

import clinicalomx as cx
from clinicalomx import InputRef

from croi.objectives.tissue_objects import environment, vocab


LS_TRANSFORM_KEY = "label-studio"
LS_REFERENCE_TYPE = "labelstudio-export"


def get_ls_transform(
    key: str = LS_TRANSFORM_KEY,
    version: str | None = None,
    description: str | None = None,
):
    """Get-or-create the Transform for the Label Studio app. Idempotent."""
    import lamindb as ln

    t = ln.Transform.filter(key=key, version=version).one_or_none()
    if t is None:
        t = ln.Transform(
            key=key,
            kind="pipeline",
            version=version,
            description=description or "Label Studio annotation app (external).",
        ).save()
    return t


def open_export_run(
    png_collection,
    *,
    transform=None,
    reference: str | None = None,
    reference_type: str = LS_REFERENCE_TYPE,
):
    """Open a Run of the Label Studio Transform for one export event.

    ``png_collection`` is recorded as a collection-level input. ``reference`` is the Label Studio
    project or export id or URL.
    """
    import lamindb as ln

    transform = transform or get_ls_transform()
    run = ln.Run(
        transform=transform,
        reference=reference,
        reference_type=reference_type,
    ).save()
    run.input_collections.add(png_collection)
    return run


def register_label(label_path, *, key: str, storage, run, source_image=None):
    """Register one label as an output of the export run. Idempotent on ``key``.

    ``source_image`` adds a per-label image input on top of the collection input.
    """
    import lamindb as ln

    try:
        art = ln.Artifact.get(key=key, storage=storage)
    except ln.Artifact.DoesNotExist:
        art = ln.Artifact(label_path, key=key, storage=storage, run=run).save()
    if source_image is not None:
        run.input_artifacts.add(source_image)
    return art


def record_export(
    png_collection,
    label_specs: Iterable[tuple[Any, str, Any]],
    *,
    storage,
    reference: str | None = None,
    transform=None,
):
    """Record one export as a completed Run. Returns ``(run, label_artifacts)``.

    ``label_specs`` yields ``(label_path, artifact_key, source_image)``; ``source_image`` may be None.
    """
    run = open_export_run(
        png_collection, transform=transform, reference=reference
    )
    labels = [
        register_label(path, key=key, storage=storage, run=run, source_image=src)
        for (path, key, src) in label_specs
    ]
    return run, labels


# Provenance comes from the full JSON export; the YOLO export drops model_version and region ids.

_RECT_TYPES = ("rectanglelabels", "rectangle")


def load_tasks(json_path) -> list[dict]:
    """Load a Label Studio full JSON export as a list of task dicts."""
    return json.loads(Path(json_path).read_text())


def task_image_stem(task: dict) -> str | None:
    """Source-image filename stem from a task's ``data`` block, or None. Best effort."""
    for v in (task.get("data") or {}).values():
        if isinstance(v, str):
            return Path(v.split("?")[0]).stem
    return None


def latest_annotation(task: dict) -> dict | None:
    """The last non-cancelled human annotation on a task (None if unlabeled)."""
    anns = [a for a in task.get("annotations", []) if not a.get("was_cancelled")]
    return anns[-1] if anns else None


def prediction(task: dict, model_version: str | None = None) -> dict | None:
    """The prediction matching ``model_version``, else the latest, else None."""
    preds = task.get("predictions", []) or []
    if model_version is not None:
        preds = [p for p in preds if p.get("model_version") == model_version] or preds
    return preds[-1] if preds else None


def _region_box(r: dict):
    """``(class_name, (cx, cy, w, h))`` normalized 0-1 from a Label Studio rectangle.

    Label Studio stores percent of the image. Rotation is ignored.
    """
    v = r["value"]
    cx = (v["x"] + v["width"] / 2) / 100.0
    cy = (v["y"] + v["height"] / 2) / 100.0
    w = v["width"] / 100.0
    h = v["height"] / 100.0
    names = v.get("rectanglelabels") or v.get("labels") or []
    return (names[0] if names else None), (cx, cy, w, h)


def result_to_yolo(result: list[dict], class_to_id: dict[str, int]) -> list[str]:
    """Label Studio rectangles to normalized YOLO lines ``"cls cx cy w h"``."""
    lines = []
    for r in result:
        if r.get("type") not in _RECT_TYPES:
            continue
        name, (cx, cy, w, h) = _region_box(r)
        cid = class_to_id.get(name)
        if cid is None:
            continue  # unknown class
        lines.append(f"{cid} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
    return lines


def _iou(a, b) -> float:
    """IoU of two normalized (cx, cy, w, h) boxes."""
    ax1, ay1, ax2, ay2 = a[0] - a[2] / 2, a[1] - a[3] / 2, a[0] + a[2] / 2, a[1] + a[3] / 2
    bx1, by1, bx2, by2 = b[0] - b[2] / 2, b[1] - b[3] / 2, b[0] + b[2] / 2, b[1] + b[3] / 2
    iw, ih = max(0.0, min(ax2, bx2) - max(ax1, bx1)), max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def classify_origin(ann_result: list[dict], pred_result: list[dict], *, accept_iou: float = 0.99) -> dict:
    """Per-region provenance by region-id match and geometry.

      - id in both, IoU >= accept_iou -> accepted
      - id in both, IoU <  accept_iou -> modified
      - id only in annotation         -> from_scratch
      - id only in prediction         -> rejected
    Returns features for :func:`stamp_provenance`.
    """
    preds = {r["id"]: _region_box(r)[1] for r in pred_result if r.get("type") in _RECT_TYPES and "id" in r}
    n_acc = n_mod = n_add = 0
    ious: list[float] = []
    seen: set = set()
    for r in ann_result:
        if r.get("type") not in _RECT_TYPES:
            continue
        rid, box = r.get("id"), _region_box(r)[1]
        if rid in preds:
            seen.add(rid)
            iou = _iou(box, preds[rid])
            ious.append(iou)
            n_acc, n_mod = (n_acc + 1, n_mod) if iou >= accept_iou else (n_acc, n_mod + 1)
        else:
            n_add += 1
    n_rej = len(preds) - len(seen)

    if not preds:
        origin = "from_scratch" if (n_add or n_acc or n_mod) else "none"
    elif n_mod == 0 and n_add == 0:
        origin = "accepted"
    elif n_acc == 0 and n_mod == 0:
        origin = "from_scratch"
    else:
        origin = "modified"

    return {
        "annotation_origin": origin,
        "n_accepted": n_acc,
        "n_modified": n_mod,
        "n_added": n_add,
        "n_rejected": n_rej,
        "prelabel_iou": (sum(ious) / len(ious)) if ious else 0.0,
    }


def resolve_prelabel_model(pred: dict | None):
    """The model Artifact whose uid is ``prediction.model_version``, or None.

    Nothing in this tree currently writes ``model_version``.
    """
    if not pred or not pred.get("model_version"):
        return None
    import lamindb as ln
    try:
        return ln.Artifact.get(uid=pred["model_version"])
    except ln.Artifact.DoesNotExist:
        return None


# Dtypes for the local get-or-create fallback. Declared in `croi.objectives.tissue_objects.vocab`.
_PROVENANCE_FEATURES = {
    vocab.ANNOTATION_ORIGIN: "str",      # accepted | modified | from_scratch | none
    vocab.PRELABEL_IOU: "float",         # mean IoU of kept boxes vs their prelabel
    vocab.N_ACCEPTED: "int",
    vocab.N_MODIFIED: "int",
    vocab.N_ADDED: "int",
    vocab.N_REJECTED: "int",
}


def ensure_provenance_features() -> None:
    """Get-or-create the provenance features :func:`stamp_provenance` writes.

    Fallback for the JSON-export path, which can run before ``roi_records.ensure_roi_features()``.
    """
    import lamindb as ln
    # Categorical over the Artifact registry: pass the class, not "cat[Artifact]".
    specs = {vocab.PRELABEL_MODEL: ln.Artifact, **_PROVENANCE_FEATURES}
    for name, dtype in specs.items():
        if ln.Feature.filter(name=name).one_or_none() is None:
            ln.Feature(name=name, dtype=dtype).save()


def stamp_provenance(label_art, run, *, origin: dict, prelabel_model=None) -> None:
    """Stamp provenance features on a label and add the prelabel model as a run input."""
    ensure_provenance_features()
    values = dict(origin)
    if prelabel_model is not None:
        values[vocab.PRELABEL_MODEL] = prelabel_model
        run.input_artifacts.add(prelabel_model)
    label_art.features.set_values(values)


# --- the stage --------------------------------------------------------------------------
#
# Each exported .txt is renamed to its thumbnail PNG stem and registered with the PNG as input.
# Each label inherits its PNG's WholeSlide ROI; label boxes are not minted as ROIs here.
# All labels bundle into one Collection per round.

ROI_FEATURE = vocab.DETECTIONS


def _canon(stem: str) -> str:
    s = re.sub(r"[^A-Za-z0-9 _.-]", "", stem)
    s = re.sub(r"[ _]+", "_", s)
    return s.lower()


def _strip_ls_prefix(stem: str) -> str:
    return re.compile(r"^[0-9a-f]{6,}-", re.IGNORECASE).sub("", stem, count=1)


def load_categories(notes_artifact: ln.Artifact) -> dict[int, str]:
    """{id: name} parsed from a registered notes.json artifact."""
    notes = json.loads(notes_artifact.path.read_text())
    return {c["id"]: c["name"] for c in notes["categories"]}


def match_labels(thumbnails: ln.Collection, labels_dir: os.PathLike) -> list[tuple[ln.Artifact, Path]]:
    """Match Label Studio YOLO labels to their source PNG by stem, renaming on disk.

    Returns ``[(png_artifact, label_path)]``.
    """
    png_arts = list(thumbnails.artifacts.all())
    png_by_canon = {_canon(Path(art.key).stem): art for art in png_arts}

    labels_dir = Path(labels_dir)
    label_files = [p for p in sorted(labels_dir.glob("*.txt")) if p.name != "classes.txt"]
    for path in label_files:  # first pass: rename to match the PNG stem
        png = png_by_canon.get(_canon(_strip_ls_prefix(path.stem)))
        if png is None:
            continue
        target_stem = Path(png.key).stem
        if path.stem == target_stem:
            continue
        new_path = path.with_name(f"{target_stem}.txt")
        if new_path.exists():
            print(f"  skip rename {path.name} -> {new_path.name} (target exists)")
            continue
        path.rename(new_path)
        print(f"  renamed {path.name} -> {new_path.name}")

    label_stems = {p.stem: p for p in labels_dir.glob("*.txt") if p.name != "classes.txt"}
    matches = []
    for png in sorted(png_arts, key=lambda a: a.key):
        label_path = label_stems.get(Path(png.key).stem)
        if label_path is None:
            print(f"  warn: no label for {png.key} (skipped)")
            continue
        matches.append((png, label_path))
    for stem in sorted(label_stems.keys() - {Path(lp).stem for _, lp in matches}):
        print(f"  warn: label {stem}.txt has no matching image (skipped)")
    return matches


@ln.step()
def link_annotations(thumbnails_collection: ln.Collection, label_artifacts: list[ln.Artifact],
                     *, key: str, description: str) -> ln.Collection:
    """Collection of one export round's label artifacts, adding only new members."""
    new = {a.uid for a in label_artifacts}
    latest = ln.Collection.filter(key=key, is_latest=True).one_or_none()
    if latest and {a.uid for a in latest.ordered_artifacts} == new:
        return latest
    labels = ln.Collection(label_artifacts, key=key, revises=latest,
                           description=description, skip_hash_lookup=True).save()
    labels.run.input_collections.add(thumbnails_collection)
    return labels


def _match_task_png(task: dict, png_by_canon: dict):
    stem = task_image_stem(task)
    if stem is None:
        return None
    return png_by_canon.get(_canon(_strip_ls_prefix(stem))) or png_by_canon.get(_canon(stem))


def _ingest_from_json(thumbnails, tasks_json, categories_artifact, output_storage, *, yolo_dir):
    """JSON-export path: write YOLO labels and stamp provenance. Returns ``[(png, label)]``."""
    tasks = load_tasks(tasks_json)
    class_to_id = {name: cid for cid, name in load_categories(categories_artifact).items()}
    png_by_canon = {_canon(Path(a.key).stem): a for a in thumbnails.ordered_artifacts}
    yolo_dir = Path(yolo_dir)
    yolo_dir.mkdir(parents=True, exist_ok=True)

    run = open_export_run(thumbnails, reference=str(tasks_json))
    pairs, skipped = [], 0
    for task in tasks:
        png = _match_task_png(task, png_by_canon)
        ann = latest_annotation(task)
        if png is None or ann is None:
            skipped += 1
            continue
        txt = yolo_dir / f"{Path(png.key).stem}.txt"
        txt.write_text("\n".join(result_to_yolo(ann.get("result", []), class_to_id)))
        label_art = register_label(
            str(txt), key=f"general_labels/{txt.name}", storage=output_storage, run=run)
        pred = prediction(task)
        origin = classify_origin(ann.get("result", []), pred.get("result", []) if pred else [])
        model_art = resolve_prelabel_model(pred)
        stamp_provenance(label_art, run, origin=origin, prelabel_model=model_art)
        pairs.append((png, label_art))
    print(f"Ingested {len(pairs)} labels from JSON export ({skipped} tasks skipped).")
    return pairs


@ln.flow()
def run_ingest_labels(config: dict, project: ln.Project) -> None:
    manifest = environment.load_manifest(environment.resolve_config("manifest.yaml"))
    output_storage = manifest.storage.provision()
    data_dir = output_storage.path / config["data_dir"]
    os.makedirs(data_dir, exist_ok=True)

    thumbnails = InputRef(**config["inputs"]["thumbnails"]).get()
    notes_ref = InputRef(**config["inputs"]["notes"])
    categories = notes_ref.ensure(data_dir / notes_ref.source, storage=output_storage)

    li = config["parameters"]["label_import"]
    if li.get("tasks_json"):
        pairs = _ingest_from_json(thumbnails, data_dir / li["tasks_json"], categories,
                                  output_storage, yolo_dir=data_dir / "_yolo")
    else:
        matches = match_labels(thumbnails, str(data_dir))
        _run, label_arts = record_export(
            thumbnails, [(lp, f"general_labels/{lp.name}", None) for _, lp in matches],
            storage=output_storage, reference=str(data_dir))
        pairs = [(png, lbl) for (png, _), lbl in zip(matches, label_arts)]

    # Each label inherits its PNG's WholeSlide ROI. Idempotent.
    n_linked = 0
    for png, label in pairs:
        if cx.update(png, label, ROI_FEATURE) is not None:
            n_linked += 1
    print(f"  linked {n_linked}/{len(pairs)} labels to their WholeSlide ROI")

    labels_out = config["outputs"]["labels"]
    link_annotations(thumbnails, [label for _, label in pairs], key=labels_out["key"],
                     description=f'{labels_out["description"]} ({len(pairs)} images, thumbnails@{thumbnails.uid})')


def run(config: dict) -> None:
    """Stage entry point: preflight, then the flow."""
    stage = environment.preflight()
    run_ingest_labels(config=config, project=stage.project)
