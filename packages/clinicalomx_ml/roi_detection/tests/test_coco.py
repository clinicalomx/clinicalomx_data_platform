"""What `croi.io.coco` refuses at construction."""
from __future__ import annotations

import importlib
import json
import pathlib
import sys

import pydantic
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "roi_detection/src"


@pytest.fixture
def coco(monkeypatch):
    monkeypatch.syspath_prepend(str(SRC))
    importlib.invalidate_caches()
    for name in [n for n in sys.modules if n.split(".")[0] == "croi"]:
        monkeypatch.delitem(sys.modules, name)
    return importlib.import_module("croi.io.coco")


CATEGORIES = ({"id": 0, "name": "TMA"}, {"id": 1, "name": "tissue_section"})


def _split(images=1, annotations=(), categories=CATEGORIES):
    return {
        "images": tuple({"id": i, "file_name": f"s{i}.png", "width": 64, "height": 48,
                         "path": f"/store/s{i}.png"} for i in range(images)),
        "annotations": tuple(annotations),
        "categories": categories,
    }


def _box(id=0, image_id=0, category_id=0, bbox=(1.0, 2.0, 10.0, 20.0)):
    return {"id": id, "image_id": image_id, "category_id": category_id, "bbox": bbox}


def _refusal(coco, model, data) -> list[str]:
    with pytest.raises(pydantic.ValidationError) as e:
        getattr(coco, model).model_validate(data)
    return [str(err["ctx"]["error"]) if "error" in err.get("ctx", {}) else err["msg"]
            for err in e.value.errors()]


def test_a_well_formed_split_builds_and_dumps_as_coco(coco):
    """The JSON holds COCO's three keys; the image path is not among them."""
    ds = coco.CocoDataset.model_validate(_split(annotations=[_box()]))

    assert json.loads(ds.model_dump_json()) == {
        "images": [{"id": 0, "file_name": "s0.png", "width": 64, "height": 48}],
        "annotations": [{"id": 0, "image_id": 0, "category_id": 0,
                         "bbox": [1.0, 2.0, 10.0, 20.0]}],
        "categories": [{"id": 0, "name": "TMA"}, {"id": 1, "name": "tissue_section"}],
    }
    assert ds.images[0].path == "/store/s0.png"


def test_an_annotation_naming_no_category_is_refused_and_named(coco):
    reasons = _refusal(coco, "CocoDataset", _split(images=2, annotations=[
        _box(0, 0, 7), _box(1, 0, 9), _box(2, 1, 1)]))
    assert reasons == ["s0.png names category [7, 9], not one of 0..1"]


def test_category_ids_must_be_their_class_indices(coco):
    """A category's id is its class index."""
    reasons = _refusal(coco, "CocoDataset", _split(categories=(
        {"id": 1, "name": "TMA"}, {"id": 2, "name": "tissue_section"})))
    assert reasons == ["category ids are [1, 2], not 0..1: a category's id is its class index"]


def test_every_problem_is_reported_not_only_the_first(coco):
    reasons = _refusal(coco, "CocoDataset", {
        "images": (
            {"id": 0, "file_name": "a.png", "width": 9, "height": 9, "path": "/a"},
            {"id": 0, "file_name": "a.png", "width": 9, "height": 9, "path": "/b"},
        ),
        "annotations": (_box(3, 0, 0), _box(3, 5, 0)),
        "categories": CATEGORIES,
    })
    [reason] = reasons
    assert reason.splitlines() == [
        "image id 0 is used 2 times",
        "file name a.png is used 2 times",
        "annotation id 3 is used 2 times",
        "annotation 3 is on image 5, which does not exist",
    ]


@pytest.mark.parametrize(("field", "value"), [
    ("bbox", (0.0, 0.0, -1.0, 4.0)),
    ("bbox", (0.0, float("nan"), 1.0, 4.0)),
    ("bbox", (0.0, 0.0, 1.0)),
    ("category_id", -1),
])
def test_a_box_that_is_not_one_is_refused(coco, field, value):
    with pytest.raises(pydantic.ValidationError):
        coco.Annotation.model_validate({**_box(), field: value})


def test_an_empty_taxonomy_is_refused(coco):
    with pytest.raises(pydantic.ValidationError, match="at least 1 item"):
        coco.CocoDataset.model_validate(_split(images=0, categories=()))


def test_a_field_coco_does_not_model_here_is_refused(coco):
    """Segmentation, keypoints and crowd regions are refused."""
    with pytest.raises(pydantic.ValidationError, match="Extra inputs"):
        coco.Annotation.model_validate({**_box(), "segmentation": []})


def test_a_dataset_is_frozen(coco):
    ds = coco.CocoDataset.model_validate(_split())
    with pytest.raises(pydantic.ValidationError):
        ds.images = ()


def test_splits_share_one_taxonomy(coco):
    other = ({"id": 0, "name": "full_core"}, {"id": 1, "name": "partial_core"})
    reasons = _refusal(coco, "CocoSplits", {
        "train": _split(), "val": _split(), "test": _split(categories=other)})
    assert reasons == [("train, val and test do not share one category list, so a class index "
                        "would mean different things in different splits")]


def test_an_empty_train_split_is_refused_beside_any_other_problem(coco):
    """A field check, so a broken val split does not hide it."""
    with pytest.raises(pydantic.ValidationError) as e:
        coco.CocoSplits.model_validate({
            "train": _split(images=0), "val": _split(annotations=[_box(0, 0, 4)]),
            "test": _split()})
    assert [(err["loc"], str(err["ctx"]["error"])) for err in e.value.errors()] == [
        (("train",), "no labelled image: training cannot start"),
        (("val",), "s0.png names category [4], not one of 0..1"),
    ]


def test_every_split_is_checked_in_one_pass(coco):
    """A dataset broken in two splits names both, rather than stopping at the first."""
    with pytest.raises(pydantic.ValidationError) as e:
        coco.CocoSplits.model_validate({
            "train": _split(annotations=[_box(0, 0, 4)]),
            "val": _split(),
            "test": _split(annotations=[_box(0, 0, 5)]),
        })
    assert [err["loc"] for err in e.value.errors()] == [("train",), ("test",)]
