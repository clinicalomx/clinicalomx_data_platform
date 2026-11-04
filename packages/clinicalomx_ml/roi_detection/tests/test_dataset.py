"""`TissueObjectsDataset` behaviour through a fake source, with no lamindb.

Fake artifacts carry only ``key``, ``suffix`` and ``path``; files are real, images real PNGs.
"""
from __future__ import annotations

import importlib
import json
import pathlib
import sys
import types

import numpy as np
import pytest
import yaml
from pngs import png_bytes

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "roi_detection/src"

CATEGORIES = ("TMA", "tissue_section")


@pytest.fixture
def dataset_module(monkeypatch):
    """`croi.objectives.tissue_objects.dataset`, imported from `src/`."""
    monkeypatch.syspath_prepend(str(SRC))
    importlib.invalidate_caches()
    for name in [n for n in sys.modules if n.split(".")[0] == "croi"]:
        monkeypatch.delitem(sys.modules, name)
    return importlib.import_module("croi.objectives.tissue_objects.dataset")


class _FakeArtifact:
    def __init__(self, key: str, path: pathlib.Path):
        self.key = key
        self.suffix = pathlib.Path(key).suffix
        self.path = str(path)


class _FakeCollection:
    def __init__(self, artifacts: list[_FakeArtifact]):
        self.artifacts = _All(artifacts)


class _All:
    def __init__(self, items):
        self._items = items

    def all(self):
        return list(self._items)


class _FakeSource:
    """Resolves an `InputRef`-shaped mapping by its ``key``."""

    def __init__(self, by_key: dict[str, object]):
        self._by_key = by_key

    def get(self, ref):
        return self._by_key[ref["key"]]


def _build(tmp_path, splits, *, categories=CATEGORIES, name="ds", release_version=None):
    """``(config, source)`` for splits shaped ``{split: {stem: label text | None}}``."""
    files = tmp_path / "files"
    files.mkdir(exist_ok=True)

    notes = files / "notes.json"
    notes.write_text(json.dumps(
        {"categories": [{"id": i, "name": n} for i, n in enumerate(categories)]}
    ))

    by_key: dict[str, object] = {"tax": _FakeArtifact("categories/notes.json", notes)}
    for split, stems in splits.items():
        artifacts = []
        for stem, label in stems.items():
            image = files / f"{split}__{stem}.png"
            image.write_bytes(IMAGE)
            artifacts.append(_FakeArtifact(f"thumbnails/{stem}.png", image))
            if label is not None:
                text = files / f"{split}__{stem}.txt"
                text.write_text(label)
                artifacts.append(_FakeArtifact(f"general_labels/{stem}.txt", text))
        by_key[f"{split}_split"] = _FakeCollection(artifacts)

    config = {
        "parameters": {"name": name, "release_version": release_version},
        "inputs": {
            "categories": {"key": "tax"},
            **{f"{s}_split": {"key": f"{s}_split"} for s in ("train", "val", "test")},
        },
    }
    return config, _FakeSource(by_key)


IMAGE = png_bytes(64, 48)
BOX_TMA = "0 0.5 0.5 0.2 0.2\n"
BOX_TISSUE = "1 0.3 0.3 0.1 0.1\n"


def _clean(tmp_path, **kwargs):
    return _build(tmp_path, {
        "train": {"a": BOX_TMA, "b": BOX_TISSUE},
        "val": {"c": BOX_TMA},
        "test": {"d": BOX_TISSUE},
    }, **kwargs)


def test_counts_reports_each_split_and_the_unlabelled_images(dataset_module, tmp_path):
    config, source = _build(tmp_path, {
        "train": {"a": BOX_TMA, "b": BOX_TMA, "c": None},
        "val": {"d": BOX_TISSUE},
        "test": {},
    })
    ds = dataset_module.TissueObjectsDataset.from_config(config, source=source)

    assert ds.counts() == {"train": 3, "val": 1, "test": 0, "unlabelled": 1}
    assert ds.categories == CATEGORIES
    assert [p.stem for p in ds.of("train")] == ["a", "b"]


def test_from_config_carries_the_name_and_the_release_version(dataset_module, tmp_path):
    config, source = _clean(tmp_path, name="general-round-3", release_version="7")
    ds = dataset_module.TissueObjectsDataset.from_config(config, source=source)

    assert ds.name == "general-round-3"
    assert ds.release_version == "7"


def test_a_clean_dataset_validates_with_no_findings(dataset_module, tmp_path):
    config, source = _clean(tmp_path)
    ds = dataset_module.TissueObjectsDataset.from_config(config, source=source)

    assert ds.validate() == []


def test_an_image_with_no_label_is_a_finding_and_not_an_exception(dataset_module, tmp_path):
    config, source = _build(tmp_path, {
        "train": {"a": BOX_TMA, "orphan": None},
        "val": {"c": BOX_TMA},
        "test": {"d": BOX_TMA},
    })
    ds = dataset_module.TissueObjectsDataset.from_config(config, source=source)

    findings = ds.validate()
    assert [f for f in findings if f.startswith("orphan (train) has no label")]
    assert len(findings) == 1


def test_a_class_index_outside_the_taxonomy_is_a_finding(dataset_module, tmp_path):
    """Two categories means ids 0 and 1. `to_coco` refuses what `validate` reports."""
    config, source = _build(tmp_path, {
        "train": {"a": BOX_TMA, "bad": "7 0.5 0.5 0.2 0.2\n"},
        "val": {"c": BOX_TMA},
        "test": {"d": BOX_TMA},
    })
    ds = dataset_module.TissueObjectsDataset.from_config(config, source=source)

    assert ds.validate() == ["train: bad.png names category [7], not one of 0..1"]
    with pytest.raises(ValueError, match=r"bad.png names category \[7\]"):
        ds.to_coco()


def test_a_label_row_that_is_not_a_box_is_a_finding(dataset_module, tmp_path):
    config, source = _build(tmp_path, {
        "train": {"a": BOX_TMA, "bad": BOX_TMA + "0 0.5 0.5\n"},
        "val": {"c": BOX_TMA},
        "test": {"d": BOX_TMA},
    })
    ds = dataset_module.TissueObjectsDataset.from_config(config, source=source)

    assert ds.validate() == ["bad (train) label line 2 is not `class cx cy w h` (3 fields)"]


def test_one_stem_in_two_splits_is_reported_as_leakage(dataset_module, tmp_path):
    config, source = _build(tmp_path, {
        "train": {"shared": BOX_TMA, "a": BOX_TMA},
        "val": {"shared": BOX_TMA},
        "test": {"d": BOX_TMA},
    })
    ds = dataset_module.TissueObjectsDataset.from_config(config, source=source)

    assert ds.validate() == [
        "shared is in ['train', 'val']: the same image in two splits is leakage"
    ]


def test_an_empty_train_split_is_a_finding(dataset_module, tmp_path):
    config, source = _build(tmp_path, {"train": {}, "val": {"c": BOX_TMA}, "test": {}})
    ds = dataset_module.TissueObjectsDataset.from_config(config, source=source)

    assert "train: no labelled image: training cannot start" in ds.validate()


def test_validate_raises_nothing_even_when_everything_is_wrong(dataset_module, tmp_path):
    """Findings are returned, never raised."""
    config, source = _build(tmp_path, {
        "train": {"shared": "9 0.5 0.5 0.2 0.2\n", "orphan": None},
        "val": {"shared": BOX_TMA},
        "test": {},
    })
    ds = dataset_module.TissueObjectsDataset.from_config(config, source=source)

    findings = ds.validate()
    assert len(findings) == 3
    assert any("is leakage" in f for f in findings)
    assert any("has no label" in f for f in findings)
    assert any("names category [9], not one of 0..1" in f for f in findings)


def test_co_occurrence_takes_only_labels_naming_two_or_more_classes(dataset_module, tmp_path):
    """A stress view that crosses split boundaries."""
    config, source = _build(tmp_path, {
        "train": {"both": BOX_TMA + BOX_TISSUE, "one": BOX_TMA},
        "val": {"repeated": BOX_TMA + BOX_TMA, "mixed": BOX_TISSUE + BOX_TMA},
        "test": {"none": None},
    })
    ds = dataset_module.TissueObjectsDataset.from_config(config, source=source)

    assert sorted(p.stem for p in ds.of("co_occurrence")) == ["both", "mixed"]
    assert sorted(p.stem for p in ds.of("all")) == ["both", "mixed", "one", "repeated"]


def test_an_unknown_view_is_refused(dataset_module, tmp_path):
    config, source = _clean(tmp_path)
    ds = dataset_module.TissueObjectsDataset.from_config(config, source=source)

    with pytest.raises(ValueError, match="unknown view"):
        ds.of("holdout")


def test_a_sparse_taxonomy_is_refused_at_construction(dataset_module, tmp_path):
    """Positions are class ids, so the taxonomy may have no gaps."""
    config, source = _clean(tmp_path)
    notes = json.loads(pathlib.Path(source.get({"key": "tax"}).path).read_text())
    notes["categories"] = [{"id": 0, "name": "TMA"}, {"id": 4, "name": "tissue_section"}]
    pathlib.Path(source.get({"key": "tax"}).path).write_text(json.dumps(notes))

    with pytest.raises(ValueError, match="taxonomy ids are not"):
        dataset_module.TissueObjectsDataset.from_config(config, source=source)


@pytest.fixture
def no_lamindb(monkeypatch):
    """Stub lamindb: the ultralytics backend package imports it."""
    monkeypatch.setitem(sys.modules, "lamindb", types.ModuleType("lamindb"))


def test_to_yolo_writes_the_tree_and_an_id_indexed_data_yaml(dataset_module, tmp_path, no_lamindb):
    """`images/<split>`, `labels/<split>`, and a `data.yaml` with id-indexed `names`."""
    config, source = _build(tmp_path, {
        "train": {"a": BOX_TMA, "dropped": None},
        "val": {"c": BOX_TISSUE},
        "test": {"d": BOX_TMA},
    })
    ds = dataset_module.TissueObjectsDataset.from_config(config, source=source)

    data_yaml = ds.to_yolo(tmp_path / "yolo")
    dest = tmp_path / "yolo"

    assert data_yaml == dest / "data.yaml"
    assert yaml.safe_load(data_yaml.read_text()) == {
        "path": str(dest),
        "train": "images/train",
        "val": "images/val",
        "test": "images/test",
        "names": {0: "TMA", 1: "tissue_section"},
    }

    for split, stem, box in (("train", "a", BOX_TMA), ("val", "c", BOX_TISSUE),
                             ("test", "d", BOX_TMA)):
        image = dest / "images" / split / f"{stem}.png"
        label = dest / "labels" / split / f"{stem}.txt"
        assert image.is_symlink() and image.read_bytes() == IMAGE
        # Compared as Ultralytics reads it, float32.
        assert np.array_equal(np.array(label.read_text().split(), dtype=np.float32),
                              np.array(box.split(), dtype=np.float32))
    assert not (dest / "images/train/dropped.png").exists(), "an unlabelled image is not staged"
    assert sorted(p.name for p in (dest / "labels/train").iterdir()) == ["a.txt"]


def test_to_yolo_rebuilds_the_destination(dataset_module, tmp_path, no_lamindb):
    config, source = _clean(tmp_path)
    ds = dataset_module.TissueObjectsDataset.from_config(config, source=source)
    dest = tmp_path / "yolo"

    ds.to_yolo(dest)
    stale = dest / "images/train/gone.png"
    stale.write_bytes(IMAGE)
    ds.to_yolo(dest)

    assert not stale.exists()
    assert sorted(p.name for p in (dest / "images/train").iterdir()) == ["a.png", "b.png"]


def test_a_label_matching_no_image_is_reported_rather_than_dropped(dataset_module, tmp_path):
    config, source = _clean(tmp_path)
    ghost = tmp_path / "files" / "ghost.txt"
    ghost.write_text(BOX_TMA)
    source._by_key["val_split"].artifacts._items.append(
        _FakeArtifact("general_labels/ghost.txt", ghost))
    ds = dataset_module.TissueObjectsDataset.from_config(config, source=source)

    assert ds.orphan_labels == (("val", "ghost"),)
    assert "label ghost (val) matched no image and is dropped" in ds.validate()
    assert [p.stem for p in ds.of("val")] == ["c"]


def test_an_objective_dataset_is_the_template_with_its_pairing_implemented(dataset_module):
    """A subclass without `pairs_in` cannot be instantiated."""
    template = importlib.import_module("croi.io.datasets")
    assert issubclass(dataset_module.TissueObjectsDataset, template.DetectionDataset)

    class Unpaired(template.DetectionDataset):
        pass

    with pytest.raises(TypeError, match="pairs_in"):
        Unpaired(name="ds", categories=("TMA",), pairs=())
