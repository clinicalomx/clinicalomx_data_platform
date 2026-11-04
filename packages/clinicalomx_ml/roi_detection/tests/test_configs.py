"""Every packaged stage yaml loads through its stage's model, and a misspelt key is refused.

Imports only pydantic and yaml, so it runs without a database.
"""
from __future__ import annotations

import importlib
import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "roi_detection/src"
OBJECTIVES_DIR = SRC / "croi/objectives"
OBJECTIVES = ("tissue_objects", "tma_core")

#: Not a stage config: `croi.environment.InstanceManifest` validates it.
NOT_STAGE_CONFIGS = {"manifest.yaml"}


def _stage_configs() -> list[tuple[str, str]]:
    return [(obj, p.name) for obj in OBJECTIVES
            for p in sorted((OBJECTIVES_DIR / obj / "configs").glob("*.yaml"))
            if p.name not in NOT_STAGE_CONFIGS]


@pytest.fixture(autouse=True)
def _fresh_source_tree(monkeypatch):
    """Import `croi` from this checkout and remove it afterwards."""
    monkeypatch.syspath_prepend(str(SRC))
    for name in [n for n in sys.modules if n.split(".")[0] == "croi"]:
        monkeypatch.delitem(sys.modules, name)
    importlib.invalidate_caches()
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.split(".")[0] == "croi":
            del sys.modules[name]


def _config():
    return importlib.import_module("croi.config")


def _subcommands(obj: str) -> dict:
    """Every stage `python -m croi <obj>` offers: its own and the ones `croi.cli` shares."""
    return importlib.import_module("croi.cli").subcommands(obj)


def _schema_for(obj: str, filename: str):
    """The model of the stage whose default config shares ``filename``'s stage number."""
    number = filename.split("_", 1)[0]
    schemas = {s.schema for s in _subcommands(obj).values()
               if s.config and s.config.split("_", 1)[0] == number}
    assert len(schemas) == 1, f"{obj}/{filename}: stage {number} has models {schemas}"
    return schemas.pop()


def _path(obj: str, filename: str) -> pathlib.Path:
    return OBJECTIVES_DIR / obj / "configs" / filename


@pytest.mark.parametrize(("obj", "filename"), _stage_configs())
def test_every_packaged_stage_config_loads_through_its_model(obj: str, filename: str):
    path = _path(obj, filename)
    loaded = _config().load(path, _schema_for(obj, filename))
    assert loaded == yaml.safe_load(path.read_text()), "load must hand the stage the yaml itself"


def test_the_stage_configs_are_all_found():
    """The glob finds every stage config."""
    assert len(_stage_configs()) == 12


@pytest.mark.parametrize("obj", OBJECTIVES)
def test_every_stage_that_reads_a_config_names_its_model(obj: str):
    for name, stage in _subcommands(obj).items():
        assert (stage.config is None) == (stage.schema is None), name
        if stage.config:
            assert _path(obj, stage.config).is_file(), f"{name}: {stage.config} does not exist"
            assert issubclass(stage.schema, _config().StageConfig), name


@pytest.mark.parametrize(("obj", "filename", "overrides", "where"), [
    ("tma_core", "3_train_and_eval.yaml", {"parameters": {"epochz": 200}}, "epochz"),
    ("tma_core", "3_train_and_eval.yaml", {"parameters": {"eval": {"conff": 0.3}}}, "eval.conff"),
    ("tissue_objects", "3_train_and_eval_rfdetr.yaml",
     {"outputs": {"metrics": {"descripton": "x"}}}, "outputs.metrics.descripton"),
    ("tma_core", "2_split.yaml", {"parameters": {"split": {"tset_fraction": 0.2}}},
     "parameters.split.tset_fraction"),
    ("tissue_objects", "2_split.yaml", {"parameters": {"split": {"seed": "40"}}},
     "parameters.split.seed"),
    ("tma_core", "4_extract_tma_cores.yaml", {"inputs": {"core_model": {"alais": "challenger"}}},
     "inputs.core_model.alais"),
    ("tissue_objects", "4_extract_tma_crops.yaml", {"parameters": {"backend": {"ttaa": True}}},
     "parameters.backend.ttaa"),
    ("tissue_objects", "1_ingest_labels.yaml",
     {"parameters": {"label_import": {"task_json": "x.json"}}}, "label_import.task_json"),
    ("tma_core", "1_ingest_labels.yaml", {"paramaters": {}}, "paramaters"),
])
def test_a_misspelt_or_mistyped_key_is_refused_at_load_naming_it(obj, filename, overrides, where):
    config = _config()
    with pytest.raises(config.InvalidConfig) as caught:
        config.load(_path(obj, filename), _schema_for(obj, filename), overrides)
    assert filename in str(caught.value) and where in str(caught.value)


@pytest.mark.parametrize(("obj", "filename", "model"), [
    ("tma_core", "3_train_and_eval.yaml", "UltralyticsTrainParameters"),
    ("tma_core", "3_train_and_eval_rfdetr.yaml", "RfdetrTrainParameters"),
    ("tissue_objects", "3_train_and_eval.yaml", "UltralyticsTrainParameters"),
    ("tissue_objects", "3_train_and_eval_rfdetr.yaml", "RfdetrTrainParameters"),
])
def test_the_framework_key_chooses_the_parameter_model(obj, filename, model):
    config = _config()
    raw = config.load(_path(obj, filename), config.TrainConfig)
    parsed = config.validate(config.TrainConfig, raw, source=filename)
    assert type(parsed.parameters).__name__ == model


@pytest.mark.parametrize(("filename", "overrides"), [
    ("3_train_and_eval_rfdetr.yaml", {"parameters": {"ultralytics_model_name": "yolo26x.pt"}}),
    ("3_train_and_eval_rfdetr.yaml", {"parameters": {"mlflow_log_period": 5}}),
    ("3_train_and_eval.yaml", {"parameters": {"rfdetr_model_name": "rf-detr-small.pth"}}),
    ("3_train_and_eval.yaml", {"parameters": {"framework": "detectron2"}}),
])
def test_a_key_of_another_framework_or_an_unknown_framework_is_refused(filename, overrides):
    config = _config()
    with pytest.raises(config.InvalidConfig):
        config.load(_path("tma_core", filename), config.TrainConfig, overrides)


def test_the_train_block_belongs_to_the_framework():
    """`train:` passes verbatim to the framework, so an unlisted keyword loads."""
    config = _config()
    loaded = config.load(_path("tma_core", "3_train_and_eval.yaml"), config.TrainConfig,
                         {"parameters": {"train": {"cos_lr": True}}})
    assert loaded["parameters"]["train"]["cos_lr"] is True


def test_overrides_merge_over_the_yaml_before_it_is_validated():
    """Prefect `overrides` are validated with the file."""
    config = _config()
    path = _path("tissue_objects", "3_train_and_eval.yaml")
    loaded = config.load(path, config.TrainConfig,
                         {"parameters": {"name": "run-2", "train": {"epochs": 3}}})
    assert loaded["parameters"]["name"] == "run-2"
    assert loaded["parameters"]["train"]["epochs"] == 3
    assert loaded["parameters"]["train"]["imgsz"] == 640, "a merge keeps the keys it does not name"
    with pytest.raises(config.InvalidConfig):
        config.load(path, config.TrainConfig, {"parameters": {"nmae": "run-2"}})


def _stub_clinicalomx(monkeypatch) -> None:
    """Enough of `clinicalomx` and `pydantic_settings` for the stage environments to import."""
    import types

    cx = types.ModuleType("clinicalomx")
    cx.Manifest = object
    cx.load_manifest = lambda *a, **k: None
    cx.resolve_config = lambda name, default_dir, env_var: pathlib.Path(default_dir) / name
    cx.stage_config_dir = lambda file, fallback: pathlib.Path(file).parent / "configs"
    cx.register_model_resolver = lambda fn: None
    settings = types.ModuleType("pydantic_settings")
    settings.SettingsConfigDict = dict
    monkeypatch.setitem(sys.modules, "clinicalomx", cx)
    monkeypatch.setitem(sys.modules, "pydantic_settings", settings)


@pytest.mark.parametrize(("argv", "filename"), [
    (["tma_core", "train"], "3_train_and_eval.yaml"),
    (["tissue_objects", "validate"], "3_train_and_eval_rfdetr.yaml"),
    (["tissue_objects", "split"], "2_split.yaml"),
    (["tma_core", "extract-tma-cores"], "4_extract_tma_cores.yaml"),
])
def test_the_cli_refuses_a_bad_config_before_preflight_connects_anything(
        monkeypatch, tmp_path, argv, filename):
    """`--config`, then the stage's model, then preflight."""
    _stub_clinicalomx(monkeypatch)
    raw = yaml.safe_load(_path(argv[0], filename).read_text())
    bad = tmp_path / filename
    bad.write_text(yaml.safe_dump({**raw, "paramaters": raw.get("parameters", {})}))

    environment = importlib.import_module("croi.environment")

    def preflight(*args, **kwargs):
        raise AssertionError("preflight ran before the config was validated")

    monkeypatch.setattr(environment, "preflight", preflight)
    with pytest.raises(_config().InvalidConfig, match="paramaters"):
        importlib.import_module("croi.cli").main([*argv, "--config", str(bad)])
