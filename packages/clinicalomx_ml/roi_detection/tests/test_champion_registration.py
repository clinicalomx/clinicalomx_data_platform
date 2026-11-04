"""A trained champion is registered and evaluated even when MLflow fails afterwards, and the run still fails.

lamindb, MLflow and the framework are recording fakes. `train_model`, `train_and_eval`,
`stamp_mlflow_features` and `log_detector_model` run for real.
"""
from __future__ import annotations

import contextlib
import importlib
import pathlib
import sys
import types
from dataclasses import dataclass, field

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "roi_detection" / "src"

RUN_ID = "run-1"
ARTIFACT_URI = "s3://mlflow/1/run-1/artifacts"


@dataclass
class Instance:
    """What the fakes recorded, and the faults a test asks them to raise."""

    mlflow_has_a_run: bool = True
    mlflow_fails_at: str | None = None  # "log_artifacts", "log_model" or "after_registering"
    mlflow_registers_a_version: bool = True
    mlflow_get_run_fails: bool = False
    evaluate_fails: bool = False
    champions: list = field(default_factory=list)
    evaluated: list = field(default_factory=list)
    logged_models: list = field(default_factory=list)
    model_versions: list = field(default_factory=list)
    features: set = field(default_factory=set)


class FakeFeatures:
    """lamindb semantics: `set_values` replaces every value; `add_values` adds."""

    def __init__(self):
        self.values: dict[str, str] = {}

    def set_values(self, values: dict) -> None:
        self.values = dict(values)

    def add_values(self, values: dict) -> None:
        self.values.update(values)


def _stub_lamindb(instance: Instance) -> types.ModuleType:
    ln = types.ModuleType("lamindb")
    ln.step = ln.flow = lambda *a, **k: (lambda fn: fn)
    ln.setup = types.SimpleNamespace(
        settings=types.SimpleNamespace(instance=types.SimpleNamespace(slug="lab/instance")))

    class Feature:
        def __init__(self, name, dtype):
            self.name = name

        @classmethod
        def filter(cls, name):
            return types.SimpleNamespace(exists=lambda: name in instance.features)

        def save(self):
            instance.features.add(self.name)

    class Artifact:
        def __init__(self, path, *, key, kind, storage, version, description):
            self.path, self.key, self.version = pathlib.Path(path), key, version
            self.uid = f"uid-{len(instance.champions)}"
            self.features = FakeFeatures()
            self.run = types.SimpleNamespace(input_artifacts=set())

        def save(self):
            instance.champions.append(self)
            return self

    ln.Feature, ln.Artifact = Feature, Artifact
    return ln


def _stub_mlflow(instance: Instance) -> types.ModuleType:
    mlflow = types.ModuleType("mlflow")
    for name in ("set_experiment", "set_tag", "log_metric", "log_input"):
        setattr(mlflow, name, lambda *a, **k: None)
    mlflow.start_run = lambda **k: contextlib.nullcontext()
    mlflow.last_active_run = lambda: types.SimpleNamespace(
        info=types.SimpleNamespace(run_id=RUN_ID, run_name="brave-owl")
    ) if instance.mlflow_has_a_run else None

    def log_artifacts(local_dir, artifact_path):
        if instance.mlflow_fails_at == "log_artifacts":
            raise ConnectionError("artifact upload refused")

    mlflow.log_artifacts = log_artifacts

    def get_run(run_id):
        if instance.mlflow_get_run_fails:
            raise ConnectionError("tracking server unreachable")
        return types.SimpleNamespace(info=types.SimpleNamespace(artifact_uri=ARTIFACT_URI))

    mlflow.get_run = get_run

    pyfunc = types.ModuleType("mlflow.pyfunc")
    pyfunc.PythonModel = object

    def log_model(*, name, python_model, artifacts, registered_model_name, pip_requirements):
        if instance.mlflow_fails_at == "log_model":
            raise ConnectionError("model upload refused")
        staged = pathlib.Path(artifacts["weights"])  # gone once log_detector_model returns
        instance.logged_models.append((registered_model_name, staged.name, staged.read_bytes()))
        if instance.mlflow_registers_a_version:
            instance.model_versions.append(types.SimpleNamespace(run_id=RUN_ID, version="3"))
        if instance.mlflow_fails_at == "after_registering":
            raise ConnectionError("model upload refused after registering")

    pyfunc.log_model = log_model

    tracking = types.ModuleType("mlflow.tracking")
    tracking.MlflowClient = lambda: types.SimpleNamespace(
        search_model_versions=lambda query: list(instance.model_versions))

    data = types.ModuleType("mlflow.data")
    http_source = types.ModuleType("mlflow.data.http_dataset_source")
    http_source.HTTPDatasetSource = object
    meta_dataset = types.ModuleType("mlflow.data.meta_dataset")
    meta_dataset.MetaDataset = object

    mlflow.pyfunc, mlflow.tracking, mlflow.data = pyfunc, tracking, data
    return {"mlflow": mlflow, "mlflow.pyfunc": pyfunc, "mlflow.tracking": tracking,
            "mlflow.data": data, "mlflow.data.http_dataset_source": http_source,
            "mlflow.data.meta_dataset": meta_dataset}


def _stub_clinicalomx() -> dict[str, types.ModuleType]:
    cx = types.ModuleType("clinicalomx")
    cx.DEFAULT_ALIAS = "production"
    cx.Manifest = object
    cx.load_manifest = cx.resolve_config = lambda *a, **k: None

    class InputRef:
        def __init__(self, kind, key):
            self.key = key

        def ensure(self, path, storage):
            return self

    cx.InputRef = InputRef
    pydantic_settings = types.ModuleType("pydantic_settings")
    pydantic_settings.SettingsConfigDict = dict
    return {"clinicalomx": cx, "pydantic_settings": pydantic_settings}


def _stub_evaluate(instance: Instance) -> types.ModuleType:
    """Stub evaluate stage: records what it scored and fails on request."""
    evaluate = types.ModuleType("croi.evaluation.evaluate")

    def evaluate_model(objective, config, weights_art, output_storage):
        instance.evaluated.append(weights_art)
        if instance.evaluate_fails:
            raise ValueError("evaluation split is empty")

    evaluate.evaluate_model = evaluate_model
    return evaluate


class FakeTrainer:
    """A trainer that writes a checkpoint without a GPU."""

    framework = "rfdetr"
    arch = "rfdetr-nano"
    pip_requirements = ("rfdetr",)

    def fetch_base(self, dest_dir: pathlib.Path) -> pathlib.Path:
        base = dest_dir / "rf-detr-nano.pth"
        base.write_bytes(b"base")
        return base

    def fit(self, dataset, run_dir: pathlib.Path, base):
        from croi.detection.trainer import TrainingRun

        assert dataset is FakeDataset.COCO, "a trainer is handed what `to_coco` built"
        run_dir.mkdir(parents=True)
        best = run_dir / "checkpoint_best_total.pth"  # RF-DETR's own name for it
        best.write_bytes(b"trained weights")
        return TrainingRun(best=best, save_dir=run_dir)


class FakeDataset:
    collections = None
    COCO = object()

    def validate(self):
        return []

    def to_coco(self):
        return self.COCO

    def describe(self):
        return "3 images"

    def artifacts(self):
        return ()


CONFIG = {
    "data_dir": "clinical-roi/core/training",
    "parameters": {"name": "v2", "mlflow_experiment_name": "roi-core-train"},
    "outputs": {"model": {"key": "models/core/best.pt", "description": "core champion"}},
    "inputs": {"train_split": {"key": "clinical-roi/core/splits/train"}},
}


@pytest.fixture
def instance():
    return Instance()


@pytest.fixture
def train(monkeypatch, instance):
    """`croi.train` imported fresh from `src/` over the fakes, for `tma_core`."""
    monkeypatch.syspath_prepend(str(SRC))
    stubs = {"lamindb": _stub_lamindb(instance), **_stub_mlflow(instance), **_stub_clinicalomx(),
             "croi.evaluation.evaluate": _stub_evaluate(instance)}
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)
    for name in [n for n in sys.modules if n.split(".")[0] == "croi" and n not in stubs]:
        monkeypatch.delitem(sys.modules, name)

    train = importlib.import_module("croi.train")
    from croi.io.objectives import DetectionObjective
    from croi.io.registries import ChampionRegistry

    record = DetectionObjective(
        name="tma_core",
        lamin_key_prefix="clinical-roi/core",
        registry=ChampionRegistry(registered_model="roi-core", alias_namespace="model_alias_core"),
        dataset=types.SimpleNamespace(from_config=lambda config: FakeDataset()),
    )
    monkeypatch.setattr(train.objectives, "get", lambda name: record)
    monkeypatch.setattr(train, "trainer_for", lambda params: FakeTrainer())
    yield train
    for name in [n for n in sys.modules if n.split(".")[0] == "croi"]:
        del sys.modules[name]


def _train_and_eval(train, tmp_path):
    storage = types.SimpleNamespace(path=tmp_path)
    train.train_and_eval(objective="tma_core", config=CONFIG, project=None, output_storage=storage)


def _the_champion(instance: Instance, tmp_path: pathlib.Path):
    """The one champion registered."""
    [champion] = instance.champions
    assert (tmp_path / champion.key).read_bytes() == b"trained weights"
    assert champion.features.values["model_framework"] == "rfdetr"
    assert [base.key for base in champion.run.input_artifacts] == [
        "clinical-roi/core/training/base_models/rf-detr-nano.pth"]
    assert instance.evaluated == [champion]
    return champion


def test_a_clean_run_registers_a_champion_mlflow_can_serve(train, instance, tmp_path):
    """Baseline: every coordinate is stamped."""
    _train_and_eval(train, tmp_path)

    champion = _the_champion(instance, tmp_path)
    assert champion.key == "models/core/roi-core_v3_rfdetr-nano.pt"
    assert champion.version == "3"
    assert champion.features.values["model_uri"] == "models:/roi-core/3"
    assert champion.features.values["mlflow_artifact_uri"] == ARTIFACT_URI
    assert champion.features.values["mlflow_run_id"] == RUN_ID


def test_the_checkpoint_reaches_mlflow_as_best_pt_whatever_the_framework_named_it(
        train, instance, tmp_path):
    """`clinicalomx.models` globs `*.pt`; RF-DETR writes `.pth`."""
    _train_and_eval(train, tmp_path)

    assert instance.logged_models == [("roi-core", "best.pt", b"trained weights")]


def test_no_mlflow_version_still_registers_and_evaluates_then_fails(train, instance, tmp_path):
    instance.mlflow_registers_a_version = False

    with pytest.raises(RuntimeError, match="roi-core has no version from MLflow run run-1"):
        _train_and_eval(train, tmp_path)

    champion = _the_champion(instance, tmp_path)
    assert champion.key == "models/core/roi-core_v2_rfdetr-nano.pt"  # named by its run dir
    assert "model_uri" not in champion.features.values
    assert champion.features.values["mlflow_artifact_uri"] == ARTIFACT_URI


def test_a_failed_mlflow_stamp_still_registers_and_evaluates_then_fails(train, instance, tmp_path):
    instance.mlflow_get_run_fails = True

    with pytest.raises(ConnectionError, match="tracking server unreachable") as raised:
        _train_and_eval(train, tmp_path)

    assert not getattr(raised.value, "__notes__", [])
    champion = _the_champion(instance, tmp_path)
    assert "mlflow_artifact_uri" not in champion.features.values


def test_when_both_fire_the_stamp_failure_names_the_missing_version(train, instance, tmp_path):
    instance.mlflow_registers_a_version = False
    instance.mlflow_get_run_fails = True

    with pytest.raises(ConnectionError) as raised:
        _train_and_eval(train, tmp_path)

    assert raised.value.__notes__ == ["Also: roi-core has no version from MLflow run run-1"]
    _the_champion(instance, tmp_path)


def test_an_evaluation_failure_names_the_deferred_one(train, instance, tmp_path):
    instance.mlflow_registers_a_version = False
    instance.evaluate_fails = True

    with pytest.raises(ValueError, match="evaluation split is empty") as raised:
        _train_and_eval(train, tmp_path)

    assert raised.value.__notes__ == [
        "Also, before evaluation: roi-core has no version from MLflow run run-1"]
    _the_champion(instance, tmp_path)


@pytest.mark.parametrize("call, error", [
    ("log_artifacts", "artifact upload refused"),
    ("log_model", "model upload refused"),
])
def test_a_failed_mlflow_log_still_registers_and_evaluates_then_fails(
        train, instance, tmp_path, call, error):
    instance.mlflow_fails_at = call

    with pytest.raises(ConnectionError, match=error) as raised:
        _train_and_eval(train, tmp_path)

    assert not getattr(raised.value, "__notes__", [])
    champion = _the_champion(instance, tmp_path)
    assert champion.key == "models/core/roi-core_v2_rfdetr-nano.pt"  # no version to name it by
    assert "model_uri" not in champion.features.values


def test_a_log_fault_after_registering_still_names_the_champion_by_its_version(
        train, instance, tmp_path):
    instance.mlflow_fails_at = "after_registering"

    with pytest.raises(ConnectionError, match="model upload refused after registering"):
        _train_and_eval(train, tmp_path)

    champion = _the_champion(instance, tmp_path)
    assert champion.key == "models/core/roi-core_v3_rfdetr-nano.pt"
    assert champion.version == "3"
    assert champion.features.values["model_uri"] == "models:/roi-core/3"


def test_no_mlflow_run_still_registers_and_evaluates_then_fails(train, instance, tmp_path):
    """A champion with no MLflow coordinates cannot be promoted, so the run fails."""
    instance.mlflow_has_a_run = False

    with pytest.raises(RuntimeError, match="training left no MLflow run"):
        _train_and_eval(train, tmp_path)

    champion = _the_champion(instance, tmp_path)
    assert champion.key == "models/core/roi-core_v2_rfdetr-nano.pt"
    assert "mlflow_run_id" not in champion.features.values
