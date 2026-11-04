"""COCO to YOLO conversion loses nothing a framework reads.

1. Round trip: COCO written as a YOLO tree and read back keeps boxes, classes and order, within
   four float64 ulps of the image's larger side.
2. Reference: the tree `to_yolo` writes matches `yolo_reference`. Images, `data.yaml` and classes
   are identical; coordinates are identical at float32, as Ultralytics reads them.
3. The same over many random rows, both stored formats, several image sizes.
4. The frameworks' own loaders, skipped without the `train` extra. RF-DETR boxes agree within one
   float32 ulp of the image size.
"""
from __future__ import annotations

import importlib
import pathlib
import sys
import types

import numpy as np
import pytest
import yaml
import yolo_reference
from pngs import png_bytes

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "roi_detection/src"
EPS = np.finfo(np.float64).eps


@pytest.fixture
def croi(monkeypatch):
    """`croi` from `src/` with lamindb stubbed. Returns `importlib.import_module`."""
    monkeypatch.syspath_prepend(str(SRC))
    importlib.invalidate_caches()
    for name in [n for n in sys.modules if n.split(".")[0] == "croi"]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "lamindb", types.ModuleType("lamindb"))
    return importlib.import_module


# ---- fixtures: a dataset of real files ----------------------------------------------------


def _ultralytics_rows(text: str) -> list[list[str]]:
    """A label as `ultralytics.data.utils.verify_image_label` reads one."""
    return [x.split() for x in text.strip().splitlines() if len(x)]


def _random_rows(rng, n: int, fmt: str) -> str:
    """``n`` rows in a stored format: ``6dp`` as the labels stage writes, ``repr`` as Label Studio does."""
    w = rng.uniform(0.001, 0.6, (n, 2))
    c = w / 2 + rng.uniform(0, 1, (n, 2)) * (1 - w)
    cls = rng.integers(0, 2, n)
    if fmt == "6dp":
        return "\n".join(f"{k} {a:.6f} {b:.6f} {x:.6f} {y:.6f}"
                         for k, (a, b), (x, y) in zip(cls, c, w))
    return "".join(f"{k} {float(a)!r} {float(b)!r} {float(x)!r} {float(y)!r}\n"
                   for k, (a, b), (x, y) in zip(cls, c, w))


#: Edge rows: the whole image, boxes touching every edge, a duplicate row and a blank line.
EDGES = ("0 0.5 0.5 1.0 1.0\n1 0.05 0.05 0.1 0.1\n1 0.95 0.95 0.1 0.1\n\n"
         "0 0.25 0.75 0.5 0.5\n0 0.25 0.75 0.5 0.5\n")


def _dataset(croi, tmp_path, objective: str, images: dict):
    """An objective's dataset over real files.

    ``images`` is ``{(split, stem): (stored name, (width, height), label text | None)}``.
    """
    template = croi("croi.io.datasets")
    cls = {"tissue_objects": "TissueObjectsDataset", "tma_core": "TmaCoreDataset"}[objective]
    dataset_cls = getattr(croi(f"croi.objectives.{objective}.dataset"), cls)
    store = tmp_path / "store"
    store.mkdir(exist_ok=True)
    pairs = []
    for (split, stem), (stored, (w, h), text) in sorted(images.items()):
        image = store / split / f"{stored}.png"
        image.parent.mkdir(exist_ok=True)
        image.write_bytes(png_bytes(w, h))
        label = None
        if text is not None:
            label = store / split / f"uuid-{stored}.txt"
            label.write_text(text)
        pairs.append(template.ImageLabelPair(
            stem, types.SimpleNamespace(path=str(image)),
            label and types.SimpleNamespace(path=str(label)), split))
    return dataset_cls(name="ds", categories=("full_core", "partial_core"), pairs=tuple(pairs))


def _fixture(croi, tmp_path, objective: str):
    rng = np.random.default_rng(7)
    return _dataset(croi, tmp_path, objective, {
        ("train", "a"): ("a", (64, 48), _random_rows(rng, 40, "6dp")),
        ("train", "a-b"): ("a-b", (333, 517), _random_rows(rng, 40, "repr")),
        ("train", "edges"): ("edges", (640, 480), EDGES),
        ("train", "SYN01_X123__45_tma_0"): ("SYN01 X123 & 45_tma_0", (1531, 2047),
                                            _random_rows(rng, 40, "repr")),
        ("train", "unlabelled"): ("unlabelled", (64, 48), None),
        ("val", "background"): ("background", (64, 48), ""),
        ("val", "v"): ("v", (517, 333), _random_rows(rng, 40, "6dp")),
        ("test", "t"): ("t", (1024, 1024), _random_rows(rng, 40, "repr")),
    })


# ---- 1. round trip ------------------------------------------------------------------------


@pytest.mark.parametrize("objective", ["tissue_objects", "tma_core"])
def test_a_coco_dataset_written_as_yolo_reads_back_as_the_same_boxes(croi, tmp_path, objective):
    dataset = _fixture(croi, tmp_path, objective).to_coco()
    yolo = croi("croi.detection.backends.ultralytics.yolo")

    data_yaml = yolo.write(dataset, tmp_path / "yolo")

    assert yaml.safe_load(data_yaml.read_text())["names"] == {0: "full_core", 1: "partial_core"}
    for split in ("train", "val", "test"):
        coco = getattr(dataset, split)
        for image in coco.images:
            label = tmp_path / "yolo/labels" / split / f"{pathlib.Path(image.file_name).stem}.txt"
            rows = _ultralytics_rows(label.read_text())
            boxes = [a for a in coco.annotations if a.image_id == image.id]
            assert [int(r[0]) for r in rows] == [a.category_id for a in boxes], "class and order"
            cx, cy, w, h = np.array([r[1:] for r in rows], dtype=np.float64).reshape(-1, 4).T
            back = np.stack([(cx - w / 2) * image.width, (cy - h / 2) * image.height,
                             w * image.width, h * image.height], axis=-1)
            atol = 4 * np.spacing(float(max(image.width, image.height)))
            np.testing.assert_allclose(back, np.array([a.bbox for a in boxes]).reshape(-1, 4),
                                       rtol=0, atol=atol)


# ---- 2. reference ---------------------------------------------------------------------------


def _files(root: pathlib.Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("objective", ["tissue_objects", "tma_core"])
def test_to_yolo_writes_what_main_wrote_as_ultralytics_reads_it(croi, tmp_path, objective):
    ds = _fixture(croi, tmp_path, objective)
    old, new = tmp_path / "main", tmp_path / "now"

    yolo_reference.to_yolo(ds.pairs, ds.categories, old)
    ds.to_yolo(new)

    assert _files(new) == _files(old), "the same images, labels and data.yaml"
    assert (new / "data.yaml").read_text() == (old / "data.yaml").read_text().replace(
        str(old), str(new))
    rows_compared = text_differs = 0
    for rel in sorted(_files(old)):
        if rel.startswith("images/"):
            assert (new / rel).resolve() == (old / rel).resolve(), rel
        elif rel.startswith("labels/"):
            was, now = (old / rel).read_text(), (new / rel).read_text()
            was_rows, now_rows = _ultralytics_rows(was), _ultralytics_rows(now)
            assert [r[0] for r in now_rows] == [r[0] for r in was_rows], f"{rel}: classes"
            was32 = np.array(was_rows, dtype=np.float32).reshape(-1, 5)
            now32 = np.array(now_rows, dtype=np.float32).reshape(-1, 5)
            assert np.array_equal(now32, was32), f"{rel}: what Ultralytics trains on"
            np.testing.assert_allclose(np.array(now_rows, dtype=np.float64).reshape(-1, 5),
                                       np.array(was_rows, dtype=np.float64).reshape(-1, 5),
                                       rtol=0, atol=4 * EPS)
            rows_compared += len(was_rows)
            text_differs += sum(a != b for a, b in zip(now_rows, was_rows))
    assert rows_compared == 40 * 5 + 5
    assert text_differs, "the text does differ in its last digits; the float32 values do not"


# ---- 3. many rows -------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["6dp", "repr"])
def test_many_rows_read_the_same_to_ultralytics_at_any_image_size(croi, tmp_path, fmt):
    rng = np.random.default_rng(11)
    sizes = {"s640": (640, 480), "s333": (333, 517), "s5099": (5099, 6737)}
    texts = {stem: _random_rows(rng, 5000, fmt) for stem in sizes}
    ds = _dataset(croi, tmp_path, "tissue_objects", {
        ("train", stem): (stem, size, texts[stem]) for stem, size in sizes.items()})

    ds.to_yolo(tmp_path / "yolo")

    for stem, text in texts.items():
        now = (tmp_path / "yolo/labels/train" / f"{stem}.txt").read_text()
        assert np.array_equal(np.array(_ultralytics_rows(now), dtype=np.float32),
                              np.array(_ultralytics_rows(text), dtype=np.float32)), stem


# ---- 4. the frameworks' own loaders -------------------------------------------------------


def test_ultralytics_reads_main_and_the_adapter_identically(croi, tmp_path):
    utils = pytest.importorskip("ultralytics.data.utils")
    ds = _fixture(croi, tmp_path, "tma_core")
    old, new = tmp_path / "main", tmp_path / "now"
    yolo_reference.to_yolo(ds.pairs, ds.categories, old)
    ds.to_yolo(new)

    for label in sorted((old / "labels").rglob("*.txt")):
        rel = label.relative_to(old / "labels")
        image = pathlib.Path("images") / rel.with_suffix(".png")
        was = utils.verify_image_label((str(old / image), str(label), "", False, 2, 0, 0, False))
        now = utils.verify_image_label(
            (str(new / image), str(new / "labels" / rel), "", False, 2, 0, 0, False))
        assert np.array_equal(now[1], was[1]), rel
        assert now[2] == was[2] and now[5:9] == was[5:9], rel
        assert now[9].replace(str(new), "") == was[9].replace(str(old), ""), rel


def test_rfdetr_reads_the_coco_view_as_it_read_mains_yolo_view(croi, tmp_path):
    yolo_loader = pytest.importorskip("rfdetr.datasets.yolo")
    coco_loader = pytest.importorskip("rfdetr.datasets.coco")
    ds = _fixture(croi, tmp_path, "tma_core")
    old_view = yolo_reference.roboflow_view(
        yolo_reference.to_yolo(ds.pairs, ds.categories, tmp_path / "main"), tmp_path / "old_view")
    new_view = croi("croi.detection.backends.rfdetr.trainer").Trainer.roboflow_view(
        ds.to_coco(), tmp_path / "new_view")

    for split in ("train", "valid", "test"):
        was = yolo_loader.YoloDetection(str(old_view / split / "images"),
                                        str(old_view / split / "labels"),
                                        str(old_view / "data.yaml"))
        now = coco_loader.CocoDetection(
            new_view / split, new_view / split / "_annotations.coco.json", transforms=None,
            remap_category_ids=True,
            cat2label=None if split == "train" else coco_loader._train_split_cat2label(new_view))
        assert len(now) == len(was), split
        for i in range(len(was)):
            info = was.sv_dataset.get_image_info(i)
            assert now.coco.loadImgs(now.ids[i])[0]["file_name"] == pathlib.Path(
                info.image_path).name, "the same image at the same index"
            (_, was_target), (_, now_target) = was[i], now[i]
            assert now_target["labels"].tolist() == was_target["labels"].tolist()
            ulp = float(np.spacing(np.float32(max(info.width, info.height))))
            diff = (now_target["boxes"] - was_target["boxes"]).abs().numpy()
            assert float(diff.max(initial=0)) <= ulp, (split, info.image_path)
