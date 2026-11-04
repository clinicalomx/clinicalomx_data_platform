"""Split the core dataset into deterministic train/val/test Collections.

Pairs images and labels within ``clinical-roi/core`` and assigns each source slide by
``hash(seed:slide-identity)``, so every core from one slide lands in one split and adding cores
never moves a slide. Writes ``clinical-roi/core/splits/{train,val,test}``. Changing the salt,
the fractions or `dataset.slide_identity` reshuffles every split.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import lamindb as ln
from clinicalomx import InputRef

from croi.io.readers import classes_in_label
from croi.objectives.tma_core import environment
from croi.objectives.tma_core.dataset import match_pairs, slide_identity


def assign_split(
    identity: str,
    validation_fraction: float,
    test_fraction: float,
    salt: str,
) -> str:
    """Split for one slide: ``test`` if u < test_fraction, ``val`` below test+val, else ``train``.

    u is uniform in [0, 1) from sha256(salt:identity).
    """
    h = hashlib.sha256(f"{salt}:{identity}".encode()).hexdigest()
    u = int(h[:16], 16) / float(1 << 64)
    if u < test_fraction:
        return "test"
    if u < test_fraction + validation_fraction:
        return "val"
    return "train"


def pair_by_stem(dataset: ln.Collection) -> list[tuple[ln.Artifact, ln.Artifact]]:
    """Re-pair ``(image_artifact, label_artifact)`` within the core Collection. Drops unlabeled images."""
    matched, orphans = match_pairs(list(dataset.ordered_artifacts))
    for stem in orphans:
        print(f"  warn: label {stem} has no matching image (skipped)")
    return [(image, label) for _, image, label in matched if label is not None]


# Pinned uid: a source change versions this file's Transform instead of forking it.
@ln.step(uid="uvjWNYOTOsev")
def split_pairs(
    dataset: ln.Collection,
    categories_artifact: ln.Artifact,
    *,
    validation_fraction: float,
    test_fraction: float,
    seed: int,
    splits_key_prefix: str,
    splits_description: str,
) -> dict[str, ln.Collection]:
    """Deterministic train/val/test split keyed on slide identity.

    Takes the Collection, not pairs, because step params must be JSON-serializable. Class
    composition is reported per split and never constrains assignment.
    """
    pairs = pair_by_stem(dataset)
    if not pairs:
        raise ValueError("No (image, label) pairs to split.")

    salt = str(seed)
    splits: dict[str, list[tuple]] = {"train": [], "val": [], "test": []}
    strata: dict[frozenset[int], dict[str, int]] = {}
    slides_per_split: dict[str, set[str]] = {"train": set(), "val": set(), "test": set()}
    for img, label in pairs:
        identity = slide_identity(Path(img.key).stem)
        name = assign_split(identity, validation_fraction, test_fraction, salt)
        splits[name].append((img, label))
        slides_per_split[name].add(identity)
        counts = strata.setdefault(classes_in_label(label.path), {"train": 0, "val": 0, "test": 0})
        counts[name] += 1

    for stratum, c in sorted(strata.items(), key=lambda kv: sorted(kv[0])):
        lbl = str(sorted(stratum)) if stratum else "empty"
        print(
            f"  stratum {lbl:<14}  n={sum(c.values()):>4}  "
            f"train={c['train']:>4}  val={c['val']:>3}  test={c['test']:>3}"
        )
    for name in ("train", "val", "test"):
        print(f"  {name:<5} {len(splits[name]):>4} cores across {len(slides_per_split[name]):>3} slides")

    if not splits["train"]:
        raise ValueError("Empty train split.")

    out: dict[str, ln.Collection] = {}
    for name, items in splits.items():
        artifacts = [a for pair in items for a in pair]
        skey = f"{splits_key_prefix}/{name}"
        new = {a.uid for a in artifacts}
        latest = ln.Collection.filter(key=skey, is_latest=True).one_or_none()
        if latest and {a.uid for a in latest.ordered_artifacts} == new:
            out[name] = latest  # unchanged membership: no new version
            continue
        # skip_hash_lookup: version on member uids, not content.
        coll = ln.Collection(
            artifacts=artifacts,
            key=skey,
            revises=latest,
            description=(
                f"{splits_description} - {name}: {len(items)} (image, label) pairs "
                f"across {len(slides_per_split[name])} slides, deterministic "
                f"hash(slide) split, salt={seed}, "
                f"val_frac={validation_fraction}, test_frac={test_fraction}"
            ),
            skip_hash_lookup=True,
        ).save()
        # Lineage: the core dataset, plus notes.json for the id-to-name mapping.
        coll.run.input_collections.add(dataset)
        coll.run.input_artifacts.add(categories_artifact)
        out[name] = coll

    return out


@ln.flow(uid="uvjWNYOTOsev")
def split_dataset(config: dict, project: ln.Project):
    inputs = config["inputs"]
    dataset = InputRef(**inputs["dataset"]).get()
    categories_art = InputRef(**inputs["categories"]).get()

    splits_out = config["outputs"]["splits"]
    split_pairs(
        dataset,
        categories_art,
        splits_key_prefix=splits_out["key_prefix"],
        splits_description=splits_out["description"],
        **config["parameters"]["split"],
    )
    print("\nDone (split).")


def run(config: dict) -> None:
    """Stage entry point: preflight, then the flow."""
    stage = environment.preflight()
    split_dataset(config=config, project=stage.project)
