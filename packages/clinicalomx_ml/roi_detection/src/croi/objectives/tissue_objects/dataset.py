"""The tissue_objects dataset: a thumbnail and its label pair by identical stem."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from croi.io.datasets import DetectionDataset, ImageLabelPair, SplitName


class TissueObjectsDataset(DetectionDataset):
    """Every thumbnail tissue_objects trains on, each with its label and split."""

    @classmethod
    def pairs_in(cls, collection: Any, split: SplitName) -> tuple[list[ImageLabelPair], list[str]]:
        """Re-pair one split Collection's artifacts by stem. An image with no label still pairs."""
        artifacts = list(collection.artifacts.all())
        images = {Path(a.key).stem: a for a in artifacts if a.suffix == ".png"}
        labels = {Path(a.key).stem: a for a in artifacts if a.suffix == ".txt"}
        pairs = [ImageLabelPair(stem, art, labels.get(stem), split)
                 for stem, art in sorted(images.items())]
        return pairs, sorted(stem for stem in labels if stem not in images)
