"""Ingest the core objective's YOLO labels into LaminDB.

Each Label Studio ``<uuid>-<stem>.txt`` is matched to a source image in
``clinical-roi/core/annotated_images``, registered under ``core_labels/<name>.txt`` with that
image as input, and inherits the image's ROI. Pairs bundle into the ``clinical-roi/core``
Collection. ``legacy_*`` features link source images registered before the ROI layer.
"""
from __future__ import annotations

from pathlib import Path

import lamindb as ln

import clinicalomx as cx
from clinicalomx import InputRef, materialize

from croi.objectives.tma_core import environment, vocab
from croi.objectives.tma_core.dataset import normalize

ROI_FEATURE = vocab.DETECTIONS
# Source-image features from before the ROI layer.
_LEGACY_UID = ("source_tma_crop_uid", "source_thumbnail_uid")
_LEGACY_NAME = ("source_tma_crop_key",)


def match_core_labels(labels_dir, image_collections):
    """Match each core label file to its source image. Returns ``[(label_path, image_art)]``.

    Same rule as :func:`croi.objectives.tma_core.dataset.match_pairs`.
    """
    image_arts = []
    for coll in image_collections:
        image_arts.extend(coll.ordered_artifacts)
        print(f"  source images <- {coll.key} v{coll.version} ({coll.artifacts.count()} artifacts)")
    index = {normalize(Path(a.key).stem): a for a in image_arts}

    labels_dir = Path(labels_dir)
    matches, unmatched = [], []
    for lbl in sorted(labels_dir.glob("*.txt")):
        if lbl.name == "classes.txt":
            continue
        stem = lbl.stem.split("-", 1)[-1]  # strip the Label Studio uuid prefix
        img = index.get(stem) or index.get(normalize(stem))
        (matches.append((lbl, img)) if img is not None else unmatched.append(lbl.name))
    if unmatched:
        print(f"WARN: {len(unmatched)} labels had no matching image: {unmatched}")
    print(f"Matched {len(matches)} (image, label) pairs.")
    return matches


@ln.step()
def step_label(label_path: str, image_art: ln.Artifact, config: dict,
               output_storage: ln.Storage) -> ln.Artifact:
    """Register one core label on its source image's ROI."""
    lp = Path(label_path)
    key = config["outputs"]["labels"]["key_template"].format(name=lp.name)

    def compute():
        try:
            art = ln.Artifact.get(key=key, storage=output_storage)
        except ln.Artifact.DoesNotExist:
            art = ln.Artifact(lp, key=key, storage=output_storage).save()
        art.run.input_artifacts.add(image_art)
        print(f"  ingested {key}")
        return {key: art}

    def reconcile(arts):
        roi = cx.update(image_art, arts[key], ROI_FEATURE,
                        legacy_uid_features=_LEGACY_UID, legacy_name_features=_LEGACY_NAME)
        if roi is not None:
            print(f"  {ROI_FEATURE} -> ROI {roi.name}")
        else:
            print(f"  no source ROI on {image_art.key!r}; '{ROI_FEATURE}' not linked")

    return materialize([key], compute=compute, reconcile=reconcile)[key]


@ln.flow()
def run_ingest_core_labels(config: dict, project: ln.Project,
                           output_storage: ln.Storage) -> None:
    data_dir = output_storage.path / config["data_dir"]
    inputs = config["inputs"]
    image_collections = [
        InputRef(**spec).get() for spec in inputs.values() if spec.get("kind") == "collection"
    ]
    notes_ref = InputRef(**inputs["notes"])
    categories = notes_ref.ensure(data_dir / notes_ref.source, storage=output_storage)

    matches = match_core_labels(labels_dir=str(data_dir), image_collections=image_collections)
    pairs = [(img, step_label(str(lbl), img, config, output_storage)) for lbl, img in matches]

    out = config["outputs"]["dataset"]
    artifacts = [a for pair in pairs for a in pair]
    latest = ln.Collection.filter(key=out["key"], is_latest=True).one_or_none()
    new = {a.uid for a in artifacts}
    if latest and {a.uid for a in latest.ordered_artifacts} == new:
        coll = latest
    else:
        coll = ln.Collection(artifacts=artifacts, key=out["key"], revises=latest,
                             description=out["description"]).save()
        coll.run.input_artifacts.add(categories)
    print(f"\nCollection {coll.key!r} saved (uid={coll.uid}) with {len(pairs)} pairs.")


def run(config: dict) -> None:
    """Stage entry point: preflight, then the flow."""
    stage = environment.preflight()
    run_ingest_core_labels(config=config, project=stage.project, output_storage=stage.storage)
