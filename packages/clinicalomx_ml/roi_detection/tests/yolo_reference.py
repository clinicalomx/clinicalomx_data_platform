"""The pre-COCO YOLO transport, copied verbatim as the reference for the equivalence tests.

Never edit it to make a test pass.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import yaml

SPLITS = ("train", "val", "test")


def to_yolo(pairs, categories, dest: Path) -> Path:
    """The former ``DetectionDataset.to_yolo``."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    for split in SPLITS:
        (dest / "images" / split).mkdir(parents=True, exist_ok=True)
        (dest / "labels" / split).mkdir(parents=True, exist_ok=True)
    for p in pairs:
        if p.label is None:
            continue
        for art, sub, suffix in ((p.image, "images", ".png"), (p.label, "labels", ".txt")):
            dst = dest / sub / p.split / f"{p.stem}{suffix}"
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            dst.symlink_to(Path(art.path))

    data_yaml = dest / "data.yaml"
    data_yaml.write_text(yaml.safe_dump({
        "path": str(dest),
        "train": "images/train",
        "val": "images/val",
        "test": "images/test",
        "names": dict(enumerate(categories)),
    }, sort_keys=False))
    return data_yaml


SPLIT_DIRS = {"train": "train", "val": "valid", "test": "test"}


def roboflow_view(data_yaml: Path, dest: Path) -> Path:
    """The former ``rfdetr.Trainer.roboflow_view``."""
    spec = yaml.safe_load(Path(data_yaml).read_text())
    root = Path(spec.get("path") or Path(data_yaml).parent)
    names = spec["names"]
    names = [names[i] for i in sorted(names)] if isinstance(names, dict) else list(names)
    if dest.exists():
        shutil.rmtree(dest)
    for ours, theirs in SPLIT_DIRS.items():
        images = root / spec[ours]
        labels = root / Path(*("labels" if part == "images" else part
                               for part in Path(spec[ours]).parts))
        (dest / theirs).mkdir(parents=True)
        (dest / theirs / "images").symlink_to(images.resolve(), target_is_directory=True)
        (dest / theirs / "labels").symlink_to(labels.resolve(), target_is_directory=True)
    (dest / "data.yaml").write_text(yaml.safe_dump({"names": names, "nc": len(names)}))
    return dest
