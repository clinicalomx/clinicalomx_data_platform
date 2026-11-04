"""An HPC job receives the site's values from the worker's environment."""
from __future__ import annotations

from config import HpcSlurmJobConfiguration
from worker import HpcSlurmSSHPushWorker


def test_the_job_exports_every_site_value_before_the_container_starts(monkeypatch):
    monkeypatch.setenv("CLINICALOMX_SITE_MODEL_ROOT", "/hpc/models root")
    monkeypatch.setenv("LAMIN_INSTANCE", "owner/name")
    monkeypatch.setenv("UNRELATED_SETTING", "x")
    configuration = HpcSlurmJobConfiguration(
        account="a_project", image="ghcr.io/o/i:t", bind_paths=["/scratch"])
    script = HpcSlurmSSHPushWorker._build_script(
        HpcSlurmSSHPushWorker, None, configuration, registry_env=None,
        sif=("/scratch/env.sif", "ghcr.io/o/i:t"))
    exports = "export CLINICALOMX_SITE_MODEL_ROOT='/hpc/models root'"
    assert exports in script
    assert "export LAMIN_INSTANCE=owner/name" in script
    assert "UNRELATED_SETTING" not in script
    assert script.index(exports) < script.index("apptainer exec")
