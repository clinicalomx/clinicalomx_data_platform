"""A champion's framework survives registration and decides the evaluator's backend.

Needs a real instance: lamindb's `set_values` replaces every external feature value.
"""
from __future__ import annotations

import types

import lamindb as ln
import pytest
from conftest import STORE
from croi.detection import packaging
from croi.evaluation import evaluate
from croi.io.registries import UnrecordedFramework, framework_of
from croi.train import stamp_framework
from croi.utils import backfill_model_framework as backfill


class Built(Exception):
    """Raised by the recording `detector_for` once the backend is chosen."""


def _champion(name: str) -> ln.Artifact:
    path = STORE / "models" / "core" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(name.encode())
    return ln.Artifact(path, key=f"models/core/{name}", kind="model").save()


def _register(art: ln.Artifact, framework: str, monkeypatch) -> None:
    """The two stamps `croi.train.train_model` writes on a champion, in order."""
    monkeypatch.setattr(packaging.mlflow, "get_run", lambda run_id: types.SimpleNamespace(
        info=types.SimpleNamespace(artifact_uri=f"s3://mlflow/{run_id}/artifacts")))
    stamp_framework(art, framework)
    packaging.stamp_mlflow_features(
        art, registered_model="roi-core", model_version="9", run_id="run-9",
        run_name="mysterious-leech", experiment_name="clinical-roi/core-train")


@pytest.mark.parametrize("framework", ["rfdetr", "ultralytics"])
def test_registration_keeps_the_framework_beside_the_mlflow_coordinates(framework, monkeypatch):
    art = _champion(f"roi-core_v9_{framework}.pt")
    _register(art, framework, monkeypatch)

    values = ln.Artifact.get(art.id).features.get_values()
    assert values["model_framework"] == framework
    assert values["model_uri"] == "models:/roi-core/9"
    assert values["mlflow_run_id"] == "run-9"


def test_evaluating_an_rfdetr_champion_builds_an_rfdetr_detector(monkeypatch):
    art = _champion("roi-core_v9_rf-detr-nano.pt")
    _register(art, "rfdetr", monkeypatch)

    dataset = types.SimpleNamespace(categories=["full_core", "partial_core"], of=lambda view: [])
    monkeypatch.setattr(evaluate.objectives, "get", lambda name: types.SimpleNamespace(
        load_dataset=lambda config: dataset))
    built = []

    def detector_for(cfg):
        built.append(cfg)
        raise Built

    monkeypatch.setattr(evaluate, "detector_for", detector_for)
    config = {
        "data_dir": "clinical-roi/core/training",
        "parameters": {"name": "v2", "eval": {"split": "test", "mlflow_experiment": "e"}},
        "outputs": {},
    }

    @ln.flow()
    def train_and_eval():  # a step needs a flow around it
        evaluate.evaluate_model("tma_core", config, ln.Artifact.get(art.id),
                                types.SimpleNamespace(path=STORE))

    with pytest.raises(Built):
        train_and_eval()
    assert [cfg["framework"] for cfg in built] == ["rfdetr"]


def test_a_model_recording_no_framework_is_refused_not_defaulted():
    art = _champion("roi-core_v7_yolo26x.pt")
    with pytest.raises(UnrecordedFramework, match=f"uid={art.uid}.*backfill_model_framework"):
        framework_of(ln.Artifact.get(art.id))


def test_the_backfill_lists_every_unstamped_model_and_stamps_only_what_it_is_told(
        monkeypatch, capsys):
    legacy = _champion("best.pt")
    (STORE / "models/core/best.pt").write_bytes(b"a later version")
    newer = ln.Artifact(STORE / "models/core/best.pt", key="models/core/best.pt", kind="model",
                        revises=legacy).save()
    stamped = _champion("roi-core_v8_yolo26x.pt")
    _register(stamped, "ultralytics", monkeypatch)
    lost = _champion("roi-core_v9_rf-detr-nano.pt")
    _register(lost, "rfdetr", monkeypatch)
    lost.features.remove_values(["model_framework"])

    missing = {art.uid for art in backfill.unstamped()}
    assert {legacy.uid, newer.uid, lost.uid} <= missing
    assert stamped.uid not in missing
    assert backfill.check() == 1
    out = capsys.readouterr().out
    assert f"{lost.uid}  models/core/roi-core_v9_rf-detr-nano.pt  version=" in out
    assert out.count("hint=rfdetr") == 1

    backfill.apply({lost.uid: "rfdetr", stamped.uid: "rfdetr"})
    assert framework_of(ln.Artifact.get(lost.id)) == "rfdetr"
    assert framework_of(ln.Artifact.get(stamped.id)) == "ultralytics"  # never overwritten
    assert ln.Artifact.get(lost.id).features.get_values()["model_uri"] == "models:/roi-core/9"
    assert lost.uid not in {art.uid for art in backfill.unstamped()}


def test_the_backfill_refuses_a_framework_croi_cannot_load():
    with pytest.raises(SystemExit, match="UID=FRAMEWORK"):
        backfill.parse(["abc=yolo"])
