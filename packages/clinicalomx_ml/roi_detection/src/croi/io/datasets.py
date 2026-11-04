"""The dataset template: which pairs, in which split, whether they are valid, and their COCO form.

An objective implements :meth:`DetectionDataset.pairs_in` and may override
:meth:`DetectionDataset.leakage_findings`. :meth:`DetectionDataset.to_coco` is the form every
trainer receives. ``validate`` returns findings and raises nothing. ``from_config`` takes a
``source=`` so tests can run without a database.
"""
from __future__ import annotations

import abc
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pydantic as pdt

from croi.io import coco
from croi.io.readers import (
    ImageSource,
    classes_in_label,
    default_image_source,
    label_rows,
    png_size,
)

SplitName = Literal["train", "val", "test"]
SPLITS: tuple[SplitName, ...] = ("train", "val", "test")

#: Views `evaluate` scores. ``all`` includes train, so it is a sanity view, not a metric.
#: ``co_occurrence`` is every image with two or more distinct classes, across splits.
VIEWS = (*SPLITS, "all", "co_occurrence")


@dataclass(frozen=True)
class ImageLabelPair:
    """One training unit: an image, its label, and its split. ``stem`` names both files on disk."""

    stem: str
    image: Any                 # ln.Artifact
    label: Any | None          # ln.Artifact, or None when the image is unlabelled
    split: SplitName


@dataclass(frozen=True)
class DetectionDataset(abc.ABC):
    """Every (image, label, split) an objective trains on, plus its taxonomy.

    ``categories`` is indexed by class id; ``from_config`` refuses ids that are not ``0..n-1``.
    """

    name: str
    categories: tuple[str, ...]
    pairs: tuple[ImageLabelPair, ...]
    release_version: str | None = None

    #: Labels that matched no image, as ``(split, label stem)``.
    orphan_labels: tuple[tuple[str, str], ...] = ()

    #: The split Collections and taxonomy Artifact, for lineage. None with a fake source.
    collections: Mapping[str, Any] | None = None
    categories_artifact: Any = None


    @classmethod
    @abc.abstractmethod
    def pairs_in(cls, collection: Any, split: SplitName) -> tuple[list[ImageLabelPair], list[str]]:
        """Pair one split Collection's artifacts. Returns ``(pairs, orphan label stems)``.

        An unlabeled image pairs with ``label=None``.
        """

    def leakage_findings(self) -> list[str]:
        """Leakage through a unit coarser than the image. Empty by default."""
        return []


    @classmethod
    def from_config(cls, config: Mapping[str, Any], *,
                    source: ImageSource | None = None) -> DetectionDataset:
        """Build from a stage yaml's ``inputs`` block: three split Collections and the taxonomy."""
        src = source or default_image_source()
        inputs = config["inputs"]
        categories_artifact = src.get(inputs["categories"])
        collections = {s: src.get(inputs[f"{s}_split"]) for s in SPLITS}
        pairs: list[ImageLabelPair] = []
        orphans: list[tuple[str, str]] = []
        for split, collection in collections.items():
            found, unmatched = cls.pairs_in(collection, split)
            pairs += found
            orphans += [(split, stem) for stem in unmatched]
        return cls(
            name=config.get("parameters", {}).get("name", "dataset"),
            categories=cls._categories(categories_artifact),
            pairs=tuple(sorted(pairs, key=lambda p: (p.split, p.stem))),
            release_version=config.get("parameters", {}).get("release_version"),
            orphan_labels=tuple(sorted(orphans)),
            collections=collections,
            categories_artifact=categories_artifact,
        )

    @staticmethod
    def _categories(notes_artifact: Any) -> tuple[str, ...]:
        """Class names indexed by id, from a registered notes.json."""
        notes = json.loads(Path(notes_artifact.path).read_text())
        by_id = {int(c["id"]): c["name"] for c in notes["categories"]}
        if sorted(by_id) != list(range(len(by_id))):
            raise ValueError(
                f"taxonomy ids are not 0..{len(by_id) - 1}: {sorted(by_id)}. "
                "A sparse id set would make a class index mean one thing as a COCO category "
                "id and another as a position here; renumber notes.json rather than working "
                "around it."
            )
        return tuple(by_id[i] for i in range(len(by_id)))


    def of(self, view: str) -> tuple[ImageLabelPair, ...]:
        """The pairs one view scores. See :data:`VIEWS`."""
        if view in SPLITS:
            return tuple(p for p in self.pairs if p.split == view and p.label is not None)
        if view == "all":
            return tuple(p for p in self.pairs if p.label is not None)
        if view == "co_occurrence":
            return tuple(
                p for p in self.pairs
                if p.label is not None and len(classes_in_label(p.label.path)) >= 2
            )
        raise ValueError(f"unknown view {view!r}; expected one of {VIEWS}")

    def artifacts(self) -> tuple[Any, ...]:
        """Every record in this dataset."""
        out = [a for p in self.pairs for a in (p.image, p.label) if a is not None]
        if self.categories_artifact is not None:
            out.append(self.categories_artifact)
        return tuple(out)

    def counts(self) -> Mapping[str, int]:
        """Pairs per split, plus unlabeled images."""
        out = {s: sum(1 for p in self.pairs if p.split == s) for s in SPLITS}
        out["unlabelled"] = sum(1 for p in self.pairs if p.label is None)
        return out

    def describe(self) -> str:
        """One line for a log: pairs per split."""
        return str(dict(self.counts()))


    def validate(self) -> list[str]:
        """Findings, most serious first. Raises nothing."""
        findings = self._coco_findings()

        seen: dict[str, set[str]] = {}
        for p in self.pairs:
            seen.setdefault(p.stem, set()).add(p.split)
        for stem, splits in sorted(seen.items()):
            if len(splits) > 1:
                findings.append(
                    f"{stem} is in {sorted(splits)}: the same image in two splits is leakage"
                )
        findings += self.leakage_findings()

        for split, stem in self.orphan_labels:
            findings.append(f"label {stem} ({split}) matched no image and is dropped")

        for p in self.pairs:
            if p.label is None:
                findings.append(f"{p.stem} ({p.split}) has no label and is dropped from training")
        return findings

    def _coco_findings(self) -> list[str]:
        """Why :meth:`to_coco` refuses this dataset, one finding per reason."""
        try:
            self.to_coco()
        except pdt.ValidationError as e:
            out = []
            for err in e.errors():
                where = " ".join(str(part) for part in err["loc"]) or "dataset"
                reason = str(err["ctx"]["error"]) if "error" in err.get("ctx", {}) else err["msg"]
                out += [f"{where}: {line}" for line in reason.splitlines()]
            return out
        except ValueError as e:
            return [str(e)]
        return []


    def to_coco(self) -> coco.CocoSplits:
        """Every labeled pair as COCO, one dataset per split.

        Images are named ``<stem>.png``; ids follow file-name order. Boxes are label rows scaled by
        image size: ``x_min = (cx - w/2) * width``. Raises on a malformed row or COCO refusal.
        """
        categories = tuple({"id": i, "name": n} for i, n in enumerate(self.categories))
        return coco.CocoSplits.model_validate(
            {split: self._coco_split(split, categories) for split in SPLITS})

    def _coco_split(self, split: SplitName, categories: tuple[dict, ...]) -> dict:
        pairs = sorted((p for p in self.pairs if p.split == split and p.label is not None),
                       key=lambda p: f"{p.stem}.png")
        images, annotations = [], []
        for image_id, p in enumerate(pairs):
            width, height = png_size(p.image.path)
            images.append({"id": image_id, "file_name": f"{p.stem}.png", "width": width,
                           "height": height, "path": str(p.image.path)})
            try:
                rows = label_rows(p.label.path)
            except ValueError as e:
                raise ValueError(f"{p.stem} ({p.split}) label {e}") from None
            for category, cx, cy, w, h in rows:
                annotations.append({
                    "id": len(annotations), "image_id": image_id, "category_id": category,
                    "bbox": ((cx - w / 2) * width, (cy - h / 2) * height, w * width, h * height),
                })
        return {"images": tuple(images), "annotations": tuple(annotations),
                "categories": categories}

    def to_yolo(self, dest: Path) -> Path:
        """The Ultralytics tree of :meth:`to_coco` under ``dest``; returns its ``data.yaml``."""
        # Deferred: the backends package imports lamindb.
        from croi.detection.backends.ultralytics import yolo

        return yolo.write(self.to_coco(), dest)
