"""An HPC job gets the external MLflow address, not the worker's local one."""
from __future__ import annotations

import worker


def test_external_variable_wins(monkeypatch):
    monkeypatch.setenv("MLFLOW_EXTERNAL_TRACKING_URI", "http://203.0.113.9:5000")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://localhost:5000")
    assert worker._external_mlflow_uri() == "http://203.0.113.9:5000"


def test_never_injects_a_local_address_into_a_hpc_job(monkeypatch):
    """localhost is the VM, not the compute node."""
    monkeypatch.delenv("MLFLOW_EXTERNAL_TRACKING_URI", raising=False)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://localhost:5000")
    assert worker._external_mlflow_uri() is None

    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:5000")
    assert worker._external_mlflow_uri() is None


def test_falls_back_for_a_worker_env_written_before_the_split(monkeypatch):
    """An older worker.env holds the external address under MLFLOW_TRACKING_URI; it still works."""
    monkeypatch.delenv("MLFLOW_EXTERNAL_TRACKING_URI", raising=False)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://203.0.113.9:5000")
    assert worker._external_mlflow_uri() == "http://203.0.113.9:5000"


def test_absent_means_absent(monkeypatch):
    monkeypatch.delenv("MLFLOW_EXTERNAL_TRACKING_URI", raising=False)
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    assert worker._external_mlflow_uri() is None
