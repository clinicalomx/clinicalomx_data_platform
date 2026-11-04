"""An image is inferred only from an owner-qualified SIF key, the inverse of
clinicalomx.environments.sif_key_for_image."""
from __future__ import annotations

import pytest

from worker import HpcSlurmSSHPushWorker

infer = HpcSlurmSSHPushWorker._infer_docker_ref


def test_an_owner_qualified_key_names_that_owners_image(monkeypatch):
    monkeypatch.setenv("CLINICALOMX_SITE_IMAGE_REGISTRY", "ghcr.io/site-owner")
    assert (infer("environments/other-owner/croi-core-inference_v0.1.10.sif")
            == "ghcr.io/other-owner/croi-core-inference:v0.1.10")


@pytest.mark.parametrize("key", [
    "environments/croi-core-inference_v0.1.10.sif",
    "environments//croi-core-inference_v0.1.10.sif",
    "environments/o/n/croi-core-inference_v0.1.10.sif",
    "environments/o/croi-core-inference.sif",
    "0123456789abcdef",
])
def test_any_other_shape_infers_nothing(monkeypatch, key):
    monkeypatch.setenv("CLINICALOMX_SITE_IMAGE_REGISTRY", "ghcr.io/site-owner")
    assert infer(key) is None
