"""The work pool's site defaults come from site.env; config.py declares none."""
from __future__ import annotations

import deploy
from config import HpcSlurmVariables

SITE = {
    "CLINICALOMX_SITE_SLURM_ACCOUNT": "a_project",
    "CLINICALOMX_SITE_SLURM_PARTITION": "general",
    "CLINICALOMX_SITE_SLURM_QOS": "normal",
    "CLINICALOMX_SITE_CONTAINER_BINDS": "/data,/archive",
}


def test_the_pool_template_takes_each_site_default_from_site_env(monkeypatch):
    for name, value in SITE.items():
        monkeypatch.setenv(name, value)
    variables = deploy.base_job_template()["variables"]
    defaults = {k: variables["properties"][k].get("default")
                for k in ("account", "partition", "qos", "bind_paths")}
    assert defaults == {"account": "a_project", "partition": "general", "qos": "normal",
                        "bind_paths": ["/data", "/archive"]}
    assert set(variables.get("required", [])) == {"image"}


def test_the_code_declares_no_site_default():
    fields = HpcSlurmVariables.model_fields
    assert all(fields[k].is_required() for k in ("account", "partition", "qos", "bind_paths"))
