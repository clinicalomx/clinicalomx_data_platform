"""The Ultralytics view of a COCO dataset: ``images/<split>``, ``labels/<split>``, ``data.yaml``.

A label row is ``class cx cy w h`` normalised by image size. Values round-trip exactly at
float32, not byte for byte.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from croi.io.coco import CocoSplits

SPLITS = ("train", "val", "test")


def write(dataset: CocoSplits, dest: Path) -> Path:
    """Write ``dataset`` as an Ultralytics tree under ``dest``; return its ``data.yaml``.

    ``dest`` is rebuilt. Images are symlinks. An image and its label share a stem.
    """
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    for split in SPLITS:
        coco = getattr(dataset, split)
        images, labels = dest / "images" / split, dest / "labels" / split
        images.mkdir(parents=True)
        labels.mkdir(parents=True)
        rows: dict[int, list[str]] = {i.id: [] for i in coco.images}
        sizes = {i.id: (i.width, i.height) for i in coco.images}
        for a in coco.annotations:
            w, h = sizes[a.image_id]
            x, y, bw, bh = a.bbox
            rows[a.image_id].append(
                f"{a.category_id} {(x + bw / 2) / w!r} {(y + bh / 2) / h!r} {bw / w!r} {bh / h!r}\n")
        for image in coco.images:
            (images / image.file_name).symlink_to(Path(image.path))
            (labels / f"{Path(image.file_name).stem}.txt").write_text("".join(rows[image.id]))

    data_yaml = dest / "data.yaml"
    data_yaml.write_text(yaml.safe_dump({
        "path": str(dest),
        "train": "images/train",
        "val": "images/val",
        "test": "images/test",
        "names": {c.id: c.name for c in dataset.categories},
    }, sort_keys=False))
    return data_yaml
