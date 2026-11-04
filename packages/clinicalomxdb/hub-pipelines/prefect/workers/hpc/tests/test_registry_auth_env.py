"""Registry credentials reach both the host and the container.

APPTAINER_DOCKER_* is read by apptainer on the host. APPTAINERENV_APPTAINER_DOCKER_* arrives
inside the container as APPTAINER_DOCKER_*, since apptainer does not forward
APPTAINER_-prefixed variables.
"""
from __future__ import annotations

import shlex

import pytest

from config import HpcSlurmJobConfiguration
from worker import HpcSlurmSSHPushWorker

TOKEN = "not a real $token"  # spaces and a $ so the rendered export has to quote it


def _script(**kwargs) -> str:
    configuration = HpcSlurmJobConfiguration(account="a_project", image="ghcr.io/o/i:t", bind_paths=["/scratch"])
    return HpcSlurmSSHPushWorker._build_script(
        HpcSlurmSSHPushWorker, None, configuration, **kwargs
    )


@pytest.fixture
def script() -> str:
    return _script(
        registry_env={
            "APPTAINER_DOCKER_USERNAME": "someone",
            "APPTAINER_DOCKER_PASSWORD": TOKEN,
        },
        sif=("/scratch/env.sif", "ghcr.io/o/i:t"),
    )


def test_apptainer_gets_the_credential_on_the_host(script):
    """The pull authenticates from these."""
    lines = script.splitlines()
    assert f"export APPTAINER_DOCKER_PASSWORD={shlex.quote(TOKEN)}" in lines
    assert "export APPTAINER_DOCKER_USERNAME=someone" in lines


def test_the_flow_inside_the_container_gets_it_too(script):
    """APPTAINERENV_<KEY> arrives inside the container as <KEY>."""
    lines = script.splitlines()
    assert f"export APPTAINERENV_APPTAINER_DOCKER_PASSWORD={shlex.quote(TOKEN)}" in lines
    assert "export APPTAINERENV_APPTAINER_DOCKER_USERNAME=someone" in lines


def test_both_exports_precede_the_apptainer_line(script):
    """An export after `apptainer exec` reaches neither consumer."""
    exec_at = script.index("apptainer exec")
    for name in ("APPTAINER_DOCKER_PASSWORD", "APPTAINERENV_APPTAINER_DOCKER_PASSWORD"):
        assert script.index(f"export {name}=") < exec_at


def test_no_credential_is_rendered_when_no_block_is_named():
    """No block named, no token rendered."""
    script = _script(registry_env=None, sif=None)
    assert "APPTAINER_DOCKER" not in script


def test_the_token_never_reaches_the_command_line(script):
    """--env would expose the token in the compute node's process list."""
    apptainer_line = next(
        ln for ln in script.splitlines() if ln.startswith("apptainer exec")
    )
    assert TOKEN not in apptainer_line
