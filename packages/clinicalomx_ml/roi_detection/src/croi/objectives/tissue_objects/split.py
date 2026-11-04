"""Split the labeled dataset into deterministic train/val/test Collections.

Pairs PNGs and labels by stem and assigns each image by ``hash(seed:identity)``, so adding or
re-annotating images never moves an existing image between splits. Writes
``clinical-roi/general/splits/{train,val,test}``.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import lamindb as ln

from clinicalomx import InputRef
from croi.objectives.tissue_objects import environment


def _canon(stem: str) -> str:
    """Canonical image identity for the split hash.

    Must match ``labels._canon``: any change reshuffles every image across splits.
    """
    s = re.sub(r"[^A-Za-z0-9 _.-]", "", stem)
    s = re.sub(r"[ _]+", "_", s)
    return s.lower()


def _classes_in_label(label_artifact) -> frozenset[int]:
    out = set()
    for line in label_artifact.path.read_text().splitlines():
        parts = line.split()
        if parts:
            try:
                out.add(int(parts[0]))
            except ValueError:
                continue
    return frozenset(out)


def assign_split(
    identity: str, 
    validation_fraction: float, 
    test_fraction: float, 
    salt: str
) -> str:
    """Split for one image: ``test`` if u < test_fraction, ``val`` below test+val, else ``train``.

    u is uniform in [0, 1) from sha256(salt:identity).
    """
    h = hashlib.sha256(f"{salt}:{identity}".encode()).hexdigest()
    u = int(h[:16], 16) / float(1 << 64)
    if u < test_fraction:
        return "test"
    if u < test_fraction + validation_fraction:
        return "val"
    return "train"


def pair_by_stem(
    thumbnails: ln.Collection,
    labels: ln.Collection
) -> list[tuple[ln.Artifact, ln.Artifact]]:
    """Re-pair ``(png_artifact, label_artifact)`` by stem across the two Collections."""
    pngs = {Path(a.key).stem: a for a in thumbnails.ordered_artifacts if a.suffix == ".png"}
    lbls = {Path(a.key).stem: a for a in labels.ordered_artifacts if a.suffix == ".txt"}
    pairs = [(pngs[s], lbls[s]) for s in sorted(lbls) if s in pngs]
    for s in sorted(set(lbls) - set(pngs)):
        print(f"  warn: label {s} has no matching png (skipped)")
    return pairs


@ln.step()
def split_pairs(
    thumbnails: ln.Collection,
    labels: ln.Collection,
    categories_artifact: ln.Artifact,
    *,
    validation_fraction: float,
    test_fraction: float,
    seed: int,
    splits_key_prefix: str,
    splits_description: str
) -> dict[str, ln.Collection]:
    """Deterministic train/val/test split keyed on image identity.

    Takes Collections, not pairs, because step params must be JSON-serializable. Class
    composition is reported per split and never constrains assignment.
    """
    pairs = pair_by_stem(thumbnails, labels)
    if not pairs:
        raise ValueError("No (png, label) pairs to split.")

    salt = str(seed)
    splits: dict[str, list[tuple]] = {"train": [], "val": [], "test": []}
    strata: dict[frozenset[int], dict[str, int]] = {}
    for png, label in pairs:
        identity = _canon(Path(png.key).stem)
        name = assign_split(identity, validation_fraction, test_fraction, salt)
        splits[name].append((png, label))
        counts = strata.setdefault(_classes_in_label(label), {"train": 0, "val": 0, "test": 0})
        counts[name] += 1

    for stratum, c in sorted(strata.items(), key=lambda kv: sorted(kv[0])):
        lbl = str(sorted(stratum)) if stratum else "empty"
        print(
            f"  stratum {lbl:<10}  n={sum(c.values()):>4}  "
            f"train={c['train']:>4}  val={c['val']:>3}  test={c['test']:>3}"
        )

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
                f"{splits_description} - {name}: {len(items)} (png, label) pairs, "
                f"deterministic hash(stem) split, salt={seed}, "
                f"val_frac={validation_fraction}, test_frac={test_fraction}"
            ),
            skip_hash_lookup=True,
        ).save()
        # Lineage: the source collections, plus notes.json for the id-to-name mapping.
        coll.run.input_collections.add(thumbnails)
        coll.run.input_collections.add(labels)
        coll.run.input_artifacts.add(categories_artifact)
        out[name] = coll

    return out


@ln.flow()
def split_dataset(config: dict, project: ln.Project):
    inputs = config["inputs"]
    thumbnails = InputRef(**inputs["thumbnails"]).get()
    labels = InputRef(**inputs["labels"]).get()
    categories_art = InputRef(**inputs["categories"]).get()

    splits_out = config["outputs"]["splits"]
    split_pairs(
        thumbnails,
        labels,
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
