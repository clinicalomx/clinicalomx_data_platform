"""The detector and trainer dispatch in `croi.detection`, with every framework stubbed.

Each test imports `croi` fresh from source under its own stubs and removes it afterwards.
"""
from __future__ import annotations

import importlib
import json
import pathlib
import sys
import types
import typing
from dataclasses import dataclass

import numpy as np
import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2] / "roi_detection"
SRC = ROOT / "src"

#: Each objective, by its directory, and the word its stored names still use.
OBJECTIVES = {"tissue_objects": "general", "tma_core": "core"}
OBJECTIVES_DIR = SRC / "croi" / "objectives"


@pytest.fixture(autouse=True)
def _fresh_source_tree(monkeypatch):
    """Import `croi` from this checkout and remove it afterwards."""
    monkeypatch.syspath_prepend(str(SRC))
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.split(".")[0] == "croi":
            del sys.modules[name]


def _import(monkeypatch, dotted: str) -> types.ModuleType:
    """Import `dotted` afresh, so it binds the frameworks this test stubbed."""
    for name in [n for n in sys.modules if n.split(".")[0] == "croi"]:
        monkeypatch.delitem(sys.modules, name)
    importlib.invalidate_caches()
    return importlib.import_module(dotted)


@dataclass
class FakeDetection:
    """What an `ObjectDetector` returns. Not the real class."""

    class_id: int
    class_name: str
    confidence: float
    xyxy: tuple[float, float, float, float]


class FakeDetector:
    """An `ObjectDetector` with no framework: only `names` and `predict`."""

    def __init__(self, dets: list[FakeDetection]):
        self.names = {0: "TMA", 1: "tissue_section"}
        self._dets = dets
        self.calls: list[dict] = []

    def predict(self, image, conf: float = 0.25, imgsz: int | None = None):
        self.calls.append({"image": image, "conf": conf, "imgsz": imgsz})
        return list(self._dets)


def _resolve_alias(alias: str) -> pathlib.Path:
    """Stand-in for an objective registry's `resolve_weights`."""
    return pathlib.Path(f"/weights/{alias}/best.pt")


#: Every framework either backend or trainer may import, by top-level module name.
FRAMEWORKS = {"ultralytics", "rfdetr", "supervision", "torch"}


def _stub_frameworks(monkeypatch):
    """Recording stand-ins for both frameworks. Returns the call list."""
    calls: list[tuple] = []

    class FakeYolo:
        def __init__(self, path):
            self.names = {0: "full_core", 1: "partial_core"}
            calls.append(("YOLO", path))

        def predict(self, **kw):
            calls.append(("YOLO.predict", kw))
            box = types.SimpleNamespace(
                cls=types.SimpleNamespace(item=lambda: 1),
                conf=types.SimpleNamespace(item=lambda: 0.5),
                xyxy=types.SimpleNamespace(cpu=lambda: types.SimpleNamespace(
                    numpy=lambda: np.array([[1.0, 2.0, 3.0, 4.0]]))),
            )
            return [types.SimpleNamespace(boxes=[box])]

    class FakeRfDetr:
        def __init__(self, path, resolution):
            self.class_names = ["full_core", "partial_core"]
            self.resolution = resolution
            calls.append(("from_checkpoint", path, resolution))

        def predict(self, image, threshold, **kw):
            calls.append(("RFDETR.predict", image, threshold, self.resolution))
            return types.SimpleNamespace(
                xyxy=np.array([[1.0, 2.0, 3.0, 4.0]]), confidence=np.array([0.5]),
                class_id=np.array([0]),
            )

    ultralytics = types.ModuleType("ultralytics")
    ultralytics.YOLO = FakeYolo
    rfdetr = types.ModuleType("rfdetr")
    rfdetr.from_checkpoint = lambda path, resolution: FakeRfDetr(path, resolution)
    monkeypatch.setitem(sys.modules, "ultralytics", ultralytics)
    monkeypatch.setitem(sys.modules, "rfdetr", rfdetr)
    monkeypatch.setitem(sys.modules, "lamindb", types.ModuleType("lamindb"))
    return calls


def _backends(monkeypatch):
    """`croi.detection.backends` over recording frameworks. Returns it and the call list."""
    calls = _stub_frameworks(monkeypatch)
    return _import(monkeypatch, "croi.detection.backends"), calls


def test_detector_for_dispatches_on_the_framework_key(monkeypatch):
    backends, _ = _backends(monkeypatch)
    detector = sys.modules["croi.detection.detector"]

    assert set(backends.DETECTORS) == {"ultralytics", "rfdetr"}
    yolo = backends.DETECTORS["ultralytics"]
    assert isinstance(backends.detector_for({"weights": "production"},
                                             resolve_alias=_resolve_alias), yolo)
    assert isinstance(backends.detector_for(None), yolo)
    assert isinstance(
        backends.detector_for({"framework": "ultralytics", "weights": "production"},
                               resolve_alias=_resolve_alias),
        yolo,
    )
    with pytest.raises(ValueError, match="unknown framework 'detectron2'"):
        backends.detector_for({"framework": "detectron2", "weights": "production"})

    backends.DETECTORS["fake"] = lambda weights, **kw: FakeDetector([])
    built = backends.detector_for({"framework": "fake", "weights": "production"})
    assert isinstance(built, detector.ObjectDetector) and isinstance(built, FakeDetector)


def test_an_alias_is_read_by_the_objective_that_passes_a_resolver(monkeypatch):
    """Every ensemble member resolves its alias through the caller's resolver."""
    backends, calls = _backends(monkeypatch)

    backends.detector_for({"weights": "production", "ensemble": [{"alias": "challenger"}]},
                           resolve_alias=_resolve_alias)
    assert [c[1] for c in calls if c[0] == "YOLO"] == [
        "/weights/production/best.pt", "/weights/challenger/best.pt"]

    with pytest.raises(ValueError, match="no objective registry was given"):
        backends.detector_for({"weights": "production"})


def test_suppress_overlaps_deduplicates_across_classes(monkeypatch):
    """One object boxed under two class names collapses to one box."""
    monkeypatch.setitem(sys.modules, "lamindb", types.ModuleType("lamindb"))
    detector = _import(monkeypatch, "croi.detection.detector")

    D = detector.DetectedObject
    same_object_two_classes = [
        D(0, "full_core", 0.90, (0.0, 0.0, 10.0, 10.0)),
        D(1, "partial_core", 0.80, (0.2, 0.2, 10.2, 10.2)),
    ]
    kept = detector.suppress_overlaps(same_object_two_classes)
    assert [d.class_name for d in kept] == ["full_core"]

    nested = [
        D(0, "full_core", 0.90, (0.0, 0.0, 10.0, 10.0)),
        D(0, "full_core", 0.70, (1.0, 1.0, 3.0, 3.0)),  # fully inside: low IoU, high IoS
    ]
    assert len(detector.suppress_overlaps(nested)) == 1

    distinct = [
        D(0, "full_core", 0.90, (0.0, 0.0, 10.0, 10.0)),
        D(0, "full_core", 0.80, (40.0, 40.0, 50.0, 50.0)),
    ]
    assert len(detector.suppress_overlaps(distinct)) == 2




@pytest.mark.parametrize("cfg", [None, {}, {"weights": "yolo11n.pt"}])
def test_a_config_naming_no_framework_builds_exactly_what_it_did(monkeypatch, cfg):
    """A keyless config loads the default checkpoint and predicts once at 640 px without augmentation."""
    backends, calls = _backends(monkeypatch)

    backend = backends.detector_for(cfg)
    dets = backend.predict("slide.png", conf=0.25)

    assert type(backend) is backends.DETECTORS["ultralytics"]
    assert calls == [
        ("YOLO", "yolo11n.pt"),
        ("YOLO.predict", {"source": "slide.png", "imgsz": 640, "conf": 0.25,
                          "augment": False, "verbose": False}),
    ]
    DetectedObject = sys.modules["croi.detection.detector"].DetectedObject
    assert dets == [DetectedObject(1, "partial_core", 0.5, (1.0, 2.0, 3.0, 4.0))]


def test_rfdetr_backend_returns_the_same_detection_shape(monkeypatch):
    backends, calls = _backends(monkeypatch)

    backend = backends.detector_for({"framework": "rfdetr", "weights": "/w/best.pt"})
    dets = backend.predict("slide.png", conf=0.3)

    assert type(backend) is backends.DETECTORS["rfdetr"]
    assert backend.names == {0: "full_core", 1: "partial_core"}
    DetectedObject = sys.modules["croi.detection.detector"].DetectedObject
    assert dets == [DetectedObject(0, "full_core", 0.5, (1.0, 2.0, 3.0, 4.0))]
    assert calls == [("from_checkpoint", "/w/best.pt", 640),
                     ("RFDETR.predict", "slide.png", 0.3, 640)]


def test_rfdetr_backend_drops_the_no_object_class_slot(monkeypatch):
    """An id past `class_names` names no class and is dropped."""
    backends, _ = _backends(monkeypatch)

    class OneClass:
        class_names = ("core",)

        def predict(self, image, threshold, **kw):
            return types.SimpleNamespace(
                xyxy=np.array([[1.0, 2.0, 3.0, 4.0], [5.0, 6.0, 7.0, 8.0]]),
                confidence=np.array([0.9, 0.8]), class_id=np.array([0, 1]),
            )

    sys.modules["rfdetr"].from_checkpoint = lambda path, resolution: OneClass()
    backend = backends.detector_for({"framework": "rfdetr", "weights": "/w/best.pt"})

    DetectedObject = sys.modules["croi.detection.detector"].DetectedObject
    assert backend.names == {0: "core"}
    assert backend.predict("slide.png", conf=0.0) == [
        DetectedObject(0, "core", 0.9, (1.0, 2.0, 3.0, 4.0))]


def test_rfdetr_backend_runs_at_the_resolution_the_caller_asks_for(monkeypatch):
    backends, calls = _backends(monkeypatch)
    backend = backends.detector_for({"framework": "rfdetr", "weights": "/w/best.pt", "imgsz": 320})

    backend.predict("a.png", conf=0.25)
    backend.predict("a.png", conf=0.25, imgsz=640)
    backend.predict("a.png", conf=0.25, imgsz=640)

    built = [c for c in calls if c[0] == "from_checkpoint"]
    assert built == [("from_checkpoint", "/w/best.pt", 320), ("from_checkpoint", "/w/best.pt", 640)]
    assert [c[3] for c in calls if c[0] == "RFDETR.predict"] == [320, 640, 640]


def test_rfdetr_backend_reads_an_array_as_bgr(monkeypatch):
    backends, calls = _backends(monkeypatch)
    backend = backends.detector_for({"framework": "rfdetr", "weights": "/w/best.pt"})
    bgr = np.zeros((2, 2, 3), dtype=np.uint8)
    bgr[..., 0] = 255  # blue

    backend.predict(bgr, conf=0.25)

    sent = next(c[1] for c in calls if c[0] == "RFDETR.predict")
    assert sent[..., 2].min() == 255 and sent[..., 0].max() == 0
    assert sent.flags["C_CONTIGUOUS"]


def test_rfdetr_backend_refuses_what_it_cannot_honour(monkeypatch):
    backends, _ = _backends(monkeypatch)
    with pytest.raises(ValueError, match="no test-time augmentation"):
        backends.detector_for({"framework": "rfdetr", "weights": "/w/best.pt", "tta": True})
    with pytest.raises(ValueError, match="needs `weights`"):
        backends.detector_for({"framework": "rfdetr"})


def test_each_framework_is_imported_by_its_own_backend_alone(monkeypatch):
    """A `None` in `sys.modules` makes that import raise."""
    for name in FRAMEWORKS:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "lamindb", types.ModuleType("lamindb"))
    backends = _import(monkeypatch, "croi.detection.backends")

    for framework, other in (("ultralytics", "rfdetr"), ("rfdetr", "ultralytics")):
        _stub_frameworks(monkeypatch)
        monkeypatch.setitem(sys.modules, other, None)
        backend = backends.detector_for({"framework": framework, "weights": "/w/best.pt"})
        assert type(backend) is backends.DETECTORS[framework]
        assert backend.predict("slide.png", conf=0.25)




def _training(monkeypatch):
    """`croi.detection.backends.training` over stub frameworks and a stub mlflow."""
    _stub_frameworks(monkeypatch)
    monkeypatch.setitem(sys.modules, "mlflow", types.ModuleType("mlflow"))
    return _import(monkeypatch, "croi.detection.backends.training")


def test_every_readable_framework_is_trainable_and_the_reverse(monkeypatch):
    """`DETECTORS`, `TRAINERS` and `croi.config.Framework` name the same frameworks."""
    training = _training(monkeypatch)
    backends = sys.modules["croi.detection.backends"]
    framework = typing.get_args(sys.modules["croi.config"].Framework)
    assert set(training.TRAINERS) == set(backends.DETECTORS) == set(framework)
    for name, cls in training.TRAINERS.items():
        assert cls.framework == name


def test_trainer_for_follows_the_config(monkeypatch):
    training = _training(monkeypatch)
    legacy = training.trainer_for({"ultralytics_model_name": "yolo26n.pt"})
    assert type(legacy) is training.TRAINERS["ultralytics"] and legacy.arch == "yolo26n"
    v2 = training.trainer_for({"framework": "rfdetr", "rfdetr_model_name": "rf-detr-small.pth"})
    assert type(v2) is training.TRAINERS["rfdetr"] and v2.arch == "rf-detr-small"
    with pytest.raises(ValueError, match="unknown framework 'detectron2'"):
        training.trainer_for({"framework": "detectron2"})


@pytest.mark.parametrize(("mode", "exists", "why_not", "expected"), [
    ("auto", True, None, True),
    ("auto", True, "is a COMPLETED run", False),
    ("auto", False, None, False),
    (False, True, None, False),
    (True, True, None, True),
])
def test_the_resume_policy_both_trainers_share(monkeypatch, tmp_path, mode, exists, why_not,
                                               expected):
    trainer = _import(monkeypatch, "croi.detection.trainer")
    ckpt = tmp_path / "last.ckpt"
    if exists:
        ckpt.write_text("")
    asked = []
    assert trainer.decide_resume(mode, ckpt, lambda: asked.append(1) or why_not) is expected
    assert bool(asked) == (exists and mode in (True, "auto"))


@pytest.mark.parametrize(("exists", "why_not", "error"), [
    (True, "is a COMPLETED run", RuntimeError), (False, None, FileNotFoundError),
])
def test_resume_true_never_silently_starts_over(monkeypatch, tmp_path, exists, why_not, error):
    trainer = _import(monkeypatch, "croi.detection.trainer")
    ckpt = tmp_path / "last.ckpt"
    if exists:
        ckpt.write_text("")
    with pytest.raises(error):
        trainer.decide_resume(True, ckpt, lambda: why_not)


CATEGORIES = [{"id": 0, "name": "full_core"}, {"id": 1, "name": "partial_core"}]


def _coco_dataset(tmp_path: pathlib.Path):
    """A three-split `CocoSplits`, one image and two boxes per split."""
    coco = importlib.import_module("croi.io.coco")
    store = tmp_path / "store"
    store.mkdir()
    splits = {}
    for split in ("train", "val", "test"):
        (store / f"s{split}.png").write_text(split)
        splits[split] = {
            "images": ({"id": 0, "file_name": f"s{split}.png", "width": 64, "height": 48,
                        "path": str(store / f"s{split}.png")},),
            "annotations": ({"id": 0, "image_id": 0, "category_id": 1, "bbox": (1.5, 2.0, 30.0, 4.25)},
                            {"id": 1, "image_id": 0, "category_id": 0, "bbox": (0.0, 0.0, 64.0, 48.0)}),
            "categories": tuple(CATEGORIES),
        }
    return coco.CocoSplits.model_validate(splits)


def test_the_roboflow_view_is_the_coco_dataset_as_rfdetr_reads_it(monkeypatch, tmp_path):
    """RF-DETR reads `<split>/_annotations.coco.json` beside the images, with `area` and `iscrowd`."""
    rfdetr = _training(monkeypatch).TRAINERS["rfdetr"]
    dataset = _coco_dataset(tmp_path)

    view = rfdetr.roboflow_view(dataset, tmp_path / "view")

    for ours, theirs in (("train", "train"), ("val", "valid"), ("test", "test")):
        written = json.loads((view / theirs / "_annotations.coco.json").read_text())
        assert written == {
            "images": [{"id": 0, "file_name": f"s{ours}.png", "width": 64, "height": 48}],
            "annotations": [
                {"id": 0, "image_id": 0, "category_id": 1, "bbox": [1.5, 2.0, 30.0, 4.25],
                 "area": 127.5, "iscrowd": 0},
                {"id": 1, "image_id": 0, "category_id": 0, "bbox": [0.0, 0.0, 64.0, 48.0],
                 "area": 3072.0, "iscrowd": 0},
            ],
            "categories": CATEGORIES,
        }
        assert (view / theirs / f"s{ours}.png").read_text() == ours, "the image, by symlink"


@pytest.mark.parametrize("obj", OBJECTIVES)
@pytest.mark.parametrize(("config", "framework"), [
    ("3_train_and_eval.yaml", "ultralytics"), ("3_train_and_eval_rfdetr.yaml", "rfdetr"),
])
def test_each_training_config_trains_the_framework_it_is_named_for(monkeypatch, obj, config,
                                                                  framework):
    training = _training(monkeypatch)
    params = yaml.safe_load((OBJECTIVES_DIR / obj / "configs" / config).read_text())["parameters"]
    assert training.trainer_for(params).framework == framework
    if framework == "rfdetr":
        assert params["train"]["resolution"] == params["eval"]["imgsz"]


def _stub_rfdetr_training(monkeypatch, epochs_run: int):
    """An RF-DETR that trains `epochs_run` epochs, and a recording mlflow. Returns the fit kwargs."""
    fits: list[dict] = []

    class FakeModel:
        def train(self, **kw):
            fits.append(kw)
            out = pathlib.Path(kw["output_dir"])
            (out / "last.ckpt").write_text(f"epoch {epochs_run - 1}")
            (out / "checkpoint_best_total.pth").write_text("best")
            (out / "metrics.csv").write_text(
                "epoch,step,val/mAP_50,test/mAP_50\n"
                + "".join(f"{e},{e},0.5,\n" for e in range(epochs_run)) + f"{epochs_run - 1},0,,0.6\n")

    rfdetr = types.ModuleType("rfdetr")
    rfdetr.from_checkpoint = lambda path: FakeModel()
    monkeypatch.setitem(sys.modules, "rfdetr", rfdetr)
    torch = types.ModuleType("torch")
    torch.load = lambda *a, **kw: {"epoch": epochs_run - 1}
    monkeypatch.setitem(sys.modules, "torch", torch)

    class Run:
        info = types.SimpleNamespace(run_id="run-1")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    mlflow = sys.modules["mlflow"]
    mlflow.start_run = lambda **kw: Run()
    mlflow.log_params = lambda params: None
    mlflow.log_metric = lambda *a, **kw: None
    return fits


def _packaged_rfdetr_fit(monkeypatch, tmp_path, obj: str) -> tuple[dict, dict]:
    """Fit an objective's packaged RF-DETR config through the stub. Returns ``(params, train kwargs)``."""
    rfdetr = _training(monkeypatch).TRAINERS["rfdetr"]
    fits = _stub_rfdetr_training(monkeypatch, epochs_run=1)
    config = OBJECTIVES_DIR / obj / "configs" / "3_train_and_eval_rfdetr.yaml"
    params = yaml.safe_load(config.read_text())["parameters"]
    rfdetr(params).fit(_coco_dataset(tmp_path), tmp_path / "runs" / params["name"],
                       tmp_path / "rf-detr-nano.pth")
    return params, fits[-1]


@pytest.mark.parametrize("obj", OBJECTIVES)
def test_rfdetr_trains_at_the_resolution_it_is_evaluated_at(monkeypatch, tmp_path, obj):
    """`RFDETR.train` trains at one scale, the evaluator's.

    rfdetr defaults `multi_scale` and `expanded_scales` to True, which resizes a 640 px nano to 800.
    """
    params, kw = _packaged_rfdetr_fit(monkeypatch, tmp_path, obj)
    assert kw.get("multi_scale", True) is False   # rfdetr's default is True
    assert kw["resolution"] == params["eval"]["imgsz"]


@pytest.mark.parametrize("obj", OBJECTIVES)
def test_rfdetr_loads_its_data_in_the_training_process(monkeypatch, tmp_path, obj):
    """`RFDETR.train` gets `num_workers: 0`, so no persistent loader worker can die in teardown."""
    _, kw = _packaged_rfdetr_fit(monkeypatch, tmp_path, obj)
    assert kw.get("num_workers", 2) == 0   # rfdetr's default is 2
    assert "persistent_workers" not in kw


def test_an_early_stopped_rfdetr_run_is_never_resumed(monkeypatch, tmp_path):
    """A run that early-stopped short of `epochs` is finished, so a re-run starts fresh."""
    rfdetr = _training(monkeypatch).TRAINERS["rfdetr"]
    fits = _stub_rfdetr_training(monkeypatch, epochs_run=60)
    dataset = _coco_dataset(tmp_path)
    run_dir = tmp_path / "runs" / "v2"
    trainer = rfdetr({"name": "v2", "resume": "auto", "train": {"epochs": 150}})

    first = trainer.fit(dataset, run_dir, tmp_path / "rf-detr-nano.pth")
    second = trainer.fit(dataset, run_dir, tmp_path / "rf-detr-nano.pth")

    assert first.test_metrics == {"mAP_50": 0.6}
    assert [f.get("resume") for f in fits] == [None, None]
    assert second.resumed_from is None
    with pytest.raises(RuntimeError, match="COMPLETED"):
        rfdetr({"name": "v2", "resume": True, "train": {"epochs": 150}}).fit(
            dataset, run_dir, tmp_path / "rf-detr-nano.pth")

    (run_dir / rfdetr.FIT_COMPLETE).unlink()
    trainer.fit(dataset, run_dir, tmp_path / "rf-detr-nano.pth")
    assert fits[-1]["resume"] == str(run_dir / "last.ckpt")


def test_a_fresh_restart_preempted_before_its_first_checkpoint_is_not_resumed(monkeypatch,
                                                                             tmp_path):
    """A fresh start over a finished run deletes that run's checkpoint with its marker."""
    rfdetr = _training(monkeypatch).TRAINERS["rfdetr"]
    fits = _stub_rfdetr_training(monkeypatch, epochs_run=60)
    dataset = _coco_dataset(tmp_path)
    run_dir = tmp_path / "runs" / "v2"
    base = tmp_path / "rf-detr-nano.pth"
    trainer = rfdetr({"name": "v2", "resume": "auto", "train": {"epochs": 150}})
    trainer.fit(dataset, run_dir, base)

    class Preempted(Exception):
        pass

    class PreemptedModel:
        def train(self, **kw):
            raise Preempted

    trained = sys.modules["rfdetr"].from_checkpoint
    monkeypatch.setattr(sys.modules["rfdetr"], "from_checkpoint", lambda path: PreemptedModel())
    with pytest.raises(Preempted):
        trainer.fit(dataset, run_dir, base)
    monkeypatch.setattr(sys.modules["rfdetr"], "from_checkpoint", trained)

    trainer.fit(dataset, run_dir, base)
    assert [f.get("resume") for f in fits] == [None, None]




def _registry(monkeypatch, artifact):
    """`croi.io.registries` over a stub clinicalomx, recording what `detector_for` receives."""
    handed: list[tuple[dict, object]] = []
    clinicalomx = types.ModuleType("clinicalomx")
    clinicalomx.DEFAULT_ALIAS = "production"
    clinicalomx.resolve_weights = lambda alias, *_: pathlib.Path(f"/weights/{alias}.pt")
    clinicalomx.resolve_model_artifact = lambda alias, *_: artifact
    monkeypatch.setitem(sys.modules, "clinicalomx", clinicalomx)

    registry = _import(monkeypatch, "croi.io.registries")
    backends = types.ModuleType("croi.detection.backends")
    backends.DEFAULT_FRAMEWORK = "ultralytics"
    backends.detector_for = lambda cfg, *, resolve_alias=None: (
        handed.append((cfg, resolve_alias)) or cfg)
    monkeypatch.setitem(sys.modules, "croi.detection.backends", backends)
    return registry, handed


def _artifact(features: dict):
    return types.SimpleNamespace(key="models/core/roi-core_v9.pt", uid="abc0000",
                                 features=types.SimpleNamespace(get_values=lambda: features))


@pytest.mark.parametrize(("features", "cfg", "expected"), [
    ({"model_framework": "ultralytics"}, {"contained_thr": 0}, "ultralytics"),
    ({"model_framework": "rfdetr"}, {"contained_thr": 0}, "rfdetr"),
    ({"model_framework": "rfdetr"}, {"framework": "rfdetr"}, "rfdetr"),
])
def test_load_champion_builds_the_framework_the_model_records(monkeypatch, features, cfg,
                                                              expected):
    """The champion's recorded framework decides what production builds."""
    registry, handed = _registry(monkeypatch, _artifact(features))
    champions = registry.ChampionRegistry("roi-core", "model_alias_core")

    champions.load_champion("production", cfg)

    assert handed == [({**cfg, "framework": expected,
                        "weights": pathlib.Path("/weights/production.pt")},
                       champions.resolve_weights)]


@pytest.mark.parametrize("cfg", [{"contained_thr": 0}, {"framework": "ultralytics"}])
def test_load_champion_refuses_a_model_that_records_no_framework(monkeypatch, cfg):
    registry, handed = _registry(monkeypatch, _artifact({}))
    champions = registry.ChampionRegistry("roi-core", "model_alias_core")
    with pytest.raises(registry.UnrecordedFramework, match="uid=abc0000.*apply abc0000="):
        champions.load_champion("production", cfg)
    assert handed == []


def test_load_champion_refuses_a_config_that_contradicts_the_model(monkeypatch):
    registry, handed = _registry(monkeypatch, _artifact({"model_framework": "rfdetr"}))
    champions = registry.ChampionRegistry("roi-core", "model_alias_core")
    with pytest.raises(ValueError, match="is a rfdetr model"):
        champions.load_champion("production", {"framework": "ultralytics"})
    assert handed == []


@pytest.mark.parametrize("cfg", [{"framework": "rfdetr"}, {"framework": "ultralytics"}, None])
def test_load_champion_refuses_an_alias_with_no_model_record(monkeypatch, cfg):
    """No lamindb model record raises, whatever the config names."""
    registry, handed = _registry(monkeypatch, None)
    champions = registry.ChampionRegistry("roi-core", "model_alias_core")
    with pytest.raises(registry.UnrecordedFramework,
                       match="roi-core alias 'production' resolves to no lamindb model"):
        champions.load_champion("production", cfg)
    assert handed == []


@pytest.mark.parametrize("obj", OBJECTIVES)
def test_each_objective_binds_its_own_champion_line(monkeypatch, obj):
    """`load_champion` and `resolve_model_artifact` stay bound to each objective's registry."""
    _registry(monkeypatch, None)
    module = importlib.import_module(f"croi.objectives.{obj}.registry")

    word = OBJECTIVES[obj]
    assert module.REGISTERED_MODEL == f"roi-{word}"
    assert module.ALIAS_NAMESPACE == f"model_alias_{word}"
    for name in ("load_champion", "resolve_model_artifact", "resolve_weights", "model_ref",
                 "promote", "resolve_champion"):
        assert getattr(module, name).__self__ is module.REGISTRY, name
