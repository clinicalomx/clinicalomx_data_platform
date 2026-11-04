"""The tma_core dataset: crops paired with Label Studio labels, and leakage checked per slide.

Label keys carry a ``<uuid>-`` prefix and image keys do not, so pairing strips it and normalises
(:func:`match_pairs`). A pair's ``stem`` is the image's normalised stem. Two crops of one slide in
two splits is leakage even when no stem repeats. :func:`slide_identity` lives here so
``validate()`` runs without a database.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from croi.io.datasets import SPLITS, DetectionDataset, ImageLabelPair, SplitName


def normalize(stem: str) -> str:
    """Apply Label Studio's export convention: spaces to ``_``, drop ``&``."""
    return stem.replace(" ", "_").replace("&", "")


def _canon(stem: str) -> str:
    """Canonical identity token: strip odd chars, collapse separators, lowercase."""
    s = re.sub(r"[^A-Za-z0-9 _.-]", "", stem)
    s = re.sub(r"[ _]+", "_", s)
    return s.lower()


def slide_identity(image_stem: str) -> str:
    """Per-slide identity for the split hash: the stem with its ``_tma_<i>`` suffix stripped."""
    return re.sub(r"_tma_\d+$", "", _canon(image_stem))


def match_pairs(artifacts: list[Any]) -> tuple[list[tuple[str, Any, Any | None]], list[str]]:
    """Match images and labels. Returns ``([(stem, image, label | None)], orphan_labels)``.

    Images are indexed by normalised stem. A label's stem loses its ``<uuid>-`` prefix and is
    looked up raw, then normalised. Unlabeled images still pair; labels with no image are orphans.
    """
    images = {normalize(Path(a.key).stem): a for a in artifacts if a.suffix == ".png"}
    labels: dict[str, Any] = {}
    orphans: list[str] = []
    for a in artifacts:
        if a.suffix != ".txt":
            continue
        stem = Path(a.key).stem.split("-", 1)[-1]  # strip the Label Studio uuid prefix
        key = stem if stem in images else normalize(stem)
        if key in images:
            labels[key] = a
        else:
            orphans.append(Path(a.key).stem)
    return [(s, art, labels.get(s)) for s, art in sorted(images.items())], sorted(orphans)


class TmaCoreDataset(DetectionDataset):
    """Every crop tma_core trains on, each with its label and split, and the slide it came from."""

    @classmethod
    def pairs_in(cls, collection: Any, split: SplitName) -> tuple[list[ImageLabelPair], list[str]]:
        """:func:`match_pairs` over one split Collection's artifacts."""
        matched, orphans = match_pairs(list(collection.artifacts.all()))
        return [ImageLabelPair(stem, image, label, split) for stem, image, label in matched], orphans

    def describe(self) -> str:
        """One line for a log: pairs per split and slides per split."""
        return f"{dict(self.counts())} over {dict(self.slides())} slides"

    def slides(self) -> Mapping[str, int]:
        """Distinct source slides per split."""
        out: dict[str, set[str]] = {s: set() for s in SPLITS}
        for p in self.pairs:
            out[p.split].add(slide_identity(p.stem))
        return {s: len(v) for s, v in out.items()}

    def leakage_findings(self) -> list[str]:
        """A SLIDE in two splits is leakage even when no stem repeats."""
        slides: dict[str, set[str]] = {}
        for p in self.pairs:
            slides.setdefault(slide_identity(p.stem), set()).add(p.split)
        return [
            f"slide {slide} has cores in {sorted(splits)}: "
            "cores from one slide in two splits is leakage"
            for slide, splits in sorted(slides.items()) if len(splits) > 1
        ]
