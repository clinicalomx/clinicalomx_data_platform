"""`TmaCoreDataset` behaviour through a fake source, and the split hash.

Covers uuid-prefixed label pairing, per-slide leakage and orphan labels. Fake artifacts carry
only ``key``, ``suffix`` and ``path``; files are real, images real PNGs.
"""
from __future__ import annotations

import importlib
import json
import pathlib
import sys
import types

import pytest
import yaml
from pngs import png_bytes

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "roi_detection/src"

CATEGORIES = ("full_core", "partial_core")

BOX_FULL = "0 0.5 0.5 0.2 0.2\n"
BOX_PARTIAL = "1 0.3 0.3 0.1 0.1\n"
BOX_BOTH = BOX_FULL + BOX_PARTIAL
IMAGE = png_bytes(64, 48)


def _core_module(monkeypatch, name: str, stubs: dict | None = None):
    """A `croi.*` module imported from `src/`. ``stubs`` are installed after the cleanup."""
    monkeypatch.syspath_prepend(str(SRC))
    importlib.invalidate_caches()
    for existing in [n for n in sys.modules if n.split(".")[0] == "croi"]:
        monkeypatch.delitem(sys.modules, existing)
    for dotted, module in (stubs or {}).items():
        monkeypatch.setitem(sys.modules, dotted, module)
    return importlib.import_module(name)


@pytest.fixture
def dataset_module(monkeypatch):
    return _core_module(monkeypatch, "croi.objectives.tma_core.dataset")


class _FakeArtifact:
    def __init__(self, key: str, path: pathlib.Path):
        self.key = key
        self.suffix = pathlib.Path(key).suffix
        self.path = str(path)


class _All:
    def __init__(self, items):
        self._items = items

    def all(self):
        return list(self._items)


class _FakeCollection:
    def __init__(self, artifacts):
        self.artifacts = _All(artifacts)


class _FakeSource:
    """Resolves an `InputRef`-shaped mapping by its ``key``."""

    def __init__(self, by_key):
        self._by_key = by_key

    def get(self, ref):
        return self._by_key[ref["key"]]


def _build(tmp_path, splits, *, categories=CATEGORIES, name="ds", release_version=None,
           orphans=None):
    """``(config, source)`` for splits shaped ``{split: {image stem: label | None}}``.

    Image stems are used verbatim as keys. Labels are keyed ``core_labels/<uuid>-<normalised stem>.txt``.
    ``orphans`` adds labels with no image.
    """
    files = tmp_path / "files"
    files.mkdir(exist_ok=True)

    notes = files / "notes.json"
    notes.write_text(json.dumps(
        {"categories": [{"id": i, "name": n} for i, n in enumerate(categories)]}
    ))

    def _ls(stem: str) -> str:
        return stem.replace(" ", "_").replace("&", "")

    by_key = {"tax": _FakeArtifact("categories/notes.json", notes)}
    for n, (split, stems) in enumerate(splits.items()):
        artifacts = []
        for i, (stem, label) in enumerate(stems.items()):
            image = files / f"{split}__{i}.png"
            image.write_bytes(IMAGE)
            artifacts.append(_FakeArtifact(f"shared/tma_crops/{stem}.png", image))
            if label is not None:
                text = files / f"{split}__{i}.txt"
                text.write_text(label)
                artifacts.append(
                    _FakeArtifact(f"core_labels/uuid{n}{i}-{_ls(stem)}.txt", text))
        for j, stem in enumerate(orphans.get(split, []) if orphans else []):
            text = files / f"{split}__orphan{j}.txt"
            text.write_text(BOX_FULL)
            artifacts.append(_FakeArtifact(f"core_labels/uuidx{n}{j}-{stem}.txt", text))
        by_key[f"{split}_split"] = _FakeCollection(artifacts)

    config = {
        "parameters": {"name": name, "release_version": release_version},
        "inputs": {
            "categories": {"key": "tax"},
            **{f"{s}_split": {"key": f"{s}_split"} for s in ("train", "val", "test")},
        },
    }
    return config, _FakeSource(by_key)



def test_a_label_pairs_through_its_uuid_prefix_and_the_ls_normalisation(dataset_module, tmp_path):
    """Image and label stems differ (raw vs uuid-prefixed and normalised) and still pair."""
    config, source = _build(tmp_path, {
        "train": {"SYN01 X123 & 45_tma_0": BOX_FULL},
        "val": {}, "test": {},
    })
    ds = dataset_module.TmaCoreDataset.from_config(config, source=source)

    (pair,) = ds.of("train")
    # " & " becomes "__": each space becomes "_" and "&" is dropped.
    assert pair.stem == "SYN01_X123__45_tma_0", "the pair is keyed on the normalised image stem"
    assert pair.label is not None
    assert ds.counts()["unlabelled"] == 0


def test_to_yolo_writes_both_files_under_the_shared_normalised_stem(dataset_module, tmp_path,
                                                                    monkeypatch):
    """Image and label are written under one stem, or Ultralytics trains on nothing."""
    config, source = _build(tmp_path, {
        "train": {"SYN01 X123 & 45_tma_0": BOX_FULL},
        "val": {"slide_b_tma_0": BOX_PARTIAL},
        "test": {"slide_c_tma_0": BOX_FULL},
    })
    ds = dataset_module.TmaCoreDataset.from_config(config, source=source)
    # The ultralytics backend package imports lamindb.
    monkeypatch.setitem(sys.modules, "lamindb", types.ModuleType("lamindb"))

    dest = tmp_path / "yolo"
    data_yaml = ds.to_yolo(dest)

    assert yaml.safe_load(data_yaml.read_text())["names"] == {0: "full_core", 1: "partial_core"}
    stem = "SYN01_X123__45_tma_0"
    image = dest / "images/train" / f"{stem}.png"
    label = dest / "labels/train" / f"{stem}.txt"
    assert image.is_symlink() and label.is_file()
    assert image.stem == label.stem, "YOLO pairs by stem; a uuid prefix here trains on nothing"
    assert image.read_bytes() == IMAGE



def test_two_cores_of_one_slide_in_two_splits_is_a_finding(dataset_module, tmp_path):
    """No stem repeats, so only the per-slide check catches it."""
    config, source = _build(tmp_path, {
        "train": {"slide_a_tma_0": BOX_FULL},
        "val": {"slide_a_tma_1": BOX_FULL},
        "test": {"slide_b_tma_0": BOX_FULL},
    })
    ds = dataset_module.TmaCoreDataset.from_config(config, source=source)

    findings = ds.validate()
    assert any("slide slide_a has cores in ['train', 'val']" in f for f in findings), findings
    assert not any("the same image in two splits" in f for f in findings)
    assert ds.slides() == {"train": 1, "val": 1, "test": 1}


def test_several_cores_of_one_slide_in_one_split_are_not_a_finding(dataset_module, tmp_path):
    config, source = _build(tmp_path, {
        "train": {"slide_a_tma_0": BOX_FULL, "slide_a_tma_1": BOX_PARTIAL,
                  "slide_a_tma_2": BOX_FULL},
        "val": {"slide_b_tma_0": BOX_FULL},
        "test": {"slide_c_tma_0": BOX_FULL},
    })
    ds = dataset_module.TmaCoreDataset.from_config(config, source=source)

    assert ds.validate() == []
    assert ds.counts() == {"train": 3, "val": 1, "test": 1, "unlabelled": 0}
    assert ds.slides() == {"train": 1, "val": 1, "test": 1}



def test_a_label_matching_no_image_is_reported_rather_than_dropped(dataset_module, tmp_path):
    config, source = _build(
        tmp_path,
        {"train": {"slide_a_tma_0": BOX_FULL}, "val": {}, "test": {}},
        orphans={"train": ["slide_z_tma_9"]},
    )
    ds = dataset_module.TmaCoreDataset.from_config(config, source=source)

    assert ds.orphan_labels == (("train", "uuidx00-slide_z_tma_9"),)
    assert any("matched no image" in f for f in ds.validate())
    assert ds.counts()["train"] == 1, "an orphan label is not a pair"


def test_an_image_with_no_label_is_still_a_pair_and_a_finding(dataset_module, tmp_path):
    config, source = _build(tmp_path, {
        "train": {"slide_a_tma_0": BOX_FULL, "slide_a_tma_1": None},
        "val": {}, "test": {},
    })
    ds = dataset_module.TmaCoreDataset.from_config(config, source=source)

    assert ds.counts() == {"train": 2, "val": 0, "test": 0, "unlabelled": 1}
    assert any("has no label and is dropped from training" in f for f in ds.validate())
    assert [p.stem for p in ds.of("train")] == ["slide_a_tma_0"]


def test_co_occurrence_takes_only_labels_naming_two_classes(dataset_module, tmp_path):
    """A crop holding both a full_core and a partial_core."""
    config, source = _build(tmp_path, {
        "train": {"slide_a_tma_0": BOX_BOTH, "slide_a_tma_1": BOX_FULL},
        "val": {"slide_b_tma_0": BOX_PARTIAL},
        "test": {"slide_c_tma_0": BOX_BOTH},
    })
    ds = dataset_module.TmaCoreDataset.from_config(config, source=source)

    assert sorted(p.stem for p in ds.of("co_occurrence")) == [
        "slide_a_tma_0", "slide_c_tma_0",
    ]


def test_validate_raises_nothing_even_when_everything_is_wrong(dataset_module, tmp_path):
    config, source = _build(
        tmp_path,
        {"train": {}, "val": {"slide_a_tma_0": "9 0.5 0.5 0.2 0.2\n"},
         "test": {"slide_a_tma_1": None}},
        orphans={"test": ["nothing_here"]},
    )
    ds = dataset_module.TmaCoreDataset.from_config(config, source=source)

    findings = ds.validate()
    assert "train: no labelled image: training cannot start" in findings
    assert any("names category [9]" in f for f in findings)
    assert any("has no label" in f for f in findings)
    assert any("matched no image" in f for f in findings)
    assert any("slide slide_a has cores in" in f for f in findings)



@pytest.fixture
def split_module(monkeypatch):
    """`tma_core.split` with lamindb and `environment` stubbed; `assign_split` is real."""
    import types

    class _Stub(types.ModuleType):
        def __getattr__(self, name):
            return lambda *a, **k: (lambda f: f)

    return _core_module(monkeypatch, "croi.objectives.tma_core.split", stubs={
        "lamindb": _Stub("lamindb"),
        "clinicalomx": _Stub("clinicalomx"),
        "croi.objectives.tma_core.environment": types.ModuleType("croi.objectives.tma_core.environment"),
    })


def test_slide_identity_collapses_every_crop_of_one_slide(dataset_module):
    """Only the `_tma_<i>` suffix separates a crop from its slide, after case and punctuation normalise."""
    identity = dataset_module.slide_identity
    assert identity("SYN01X12345_Scan1_tma_0") == identity("SYN01X12345_Scan1_tma_7")
    assert identity("SYN01X12345_Scan1") == identity("SYN01X12345_Scan1_tma_0")
    assert identity("SYN01 X123&45") == identity("syn01_x12345")
    assert identity("slide_a_tma_10") != identity("slide_b_tma_10")
    assert identity("slide_tma_x") == "slide_tma_x", "only a NUMERIC tma suffix is stripped"


def test_assign_split_is_the_hash_it_has_always_been(split_module):
    """Regression pin: the current assignment. Changing the salt, fractions or prefix reshuffles every split."""
    assign = split_module.assign_split
    got = {s: assign(s, 0.15, 0.15, "40")
           for s in ("slide_a", "slide_b", "slide_c", "slide_d", "slide_e")}
    assert got == {
        "slide_a": "train",
        "slide_b": "train",
        "slide_c": "val",
        "slide_d": "train",
        "slide_e": "train",
    }


def test_assign_split_is_stable_under_a_growing_corpus(split_module):
    assign = split_module.assign_split
    before = {f"slide_{i}": assign(f"slide_{i}", 0.15, 0.15, "40") for i in range(40)}
    after = {f"slide_{i}": assign(f"slide_{i}", 0.15, 0.15, "40") for i in range(200)}
    assert all(after[k] == v for k, v in before.items())


def test_the_fractions_land_roughly_where_they_are_declared(split_module):
    """Sanity bound: 15/15/70 over 2000 identities."""
    assign = split_module.assign_split
    counts = {"train": 0, "val": 0, "test": 0}
    for i in range(2000):
        counts[assign(f"slide_{i}", 0.15, 0.15, "40")] += 1
    assert 0.12 < counts["test"] / 2000 < 0.18
    assert 0.12 < counts["val"] / 2000 < 0.18
    assert 0.66 < counts["train"] / 2000 < 0.74
