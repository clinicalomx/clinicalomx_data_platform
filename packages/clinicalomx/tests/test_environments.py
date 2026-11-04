"""Tests for environment-artifact identity: SIF keys, manifests, and refusals. No lamindb."""
from __future__ import annotations

import json

import pytest

from clinicalomx.environments import (
    F_IMAGE_DIGEST,
    F_SIF_ORAS_REF,
    F_SOURCE_REF,
    EnvImage,
    plan_registrations,
    sif_key_for_image,
)

DIGEST = "sha256:" + "ab" * 32
OTHER_DIGEST = "sha256:" + "cd" * 32

# One image of each kind the platform builds, with its sif_artifact key. The HPC worker infers one
# from the other.
IMAGES = [
    ("ghcr.io/example-org/comx-qptiff:cx0.2.6-bio4e1845a",
     "environments/example-org/comx-qptiff_cx0.2.6-bio4e1845a.sif"),
    ("ghcr.io/example-org/bgnorm-inference:v0.1.2-cx0.2.6",
     "environments/example-org/bgnorm-inference_v0.1.2-cx0.2.6.sif"),
    ("ghcr.io/example-org/croi-general-inference:v0.3.5",
     "environments/example-org/croi-general-inference_v0.3.5.sif"),
    ("ghcr.io/example-org/croi-core-inference:v0.3.5",
     "environments/example-org/croi-core-inference_v0.3.5.sif"),
    ("ghcr.io/example-org/croi-general-train:v0.3.5",
     "environments/example-org/croi-general-train_v0.3.5.sif"),
    ("ghcr.io/example-org/croi-core-train:v0.3.5",
     "environments/example-org/croi-core-train_v0.3.5.sif"),
]


@pytest.mark.parametrize(("image", "key"), IMAGES)
def test_sif_key_matches_the_declared_key(image, key):
    assert sif_key_for_image(image) == key


@pytest.mark.parametrize(("image", "key"), IMAGES)
def test_identity_is_derived_not_declared(image, key):
    env = EnvImage(image=image, digest=DIGEST, source_ref="clinical-roi-v0.1.10")
    assert env.sif_key == key
    assert env.registered_env == image.rsplit("/", 1)[-1].split(":")[0]
    assert env.pinned_ref == f"{image.split(':')[0]}@{DIGEST}"
    assert env.tag == image.rsplit(":", 1)[-1]


def test_provenance_features_carry_digest_and_source():
    env = EnvImage(
        image="ghcr.io/example-org/comx-qptiff:cx0.3.0-bio4e1845a",
        digest=DIGEST,
        source_ref="clinicalomx-pipeline-v1.2.0",
        sif_oras_ref="oras://ghcr.io/example-org/comx-qptiff-sif:cx0.3.0-bio4e1845a",
    )
    assert env.provenance_features() == {
        F_IMAGE_DIGEST: DIGEST,
        F_SOURCE_REF: "clinicalomx-pipeline-v1.2.0",
        F_SIF_ORAS_REF: "oras://ghcr.io/example-org/comx-qptiff-sif:cx0.3.0-bio4e1845a",
    }


# Refusals: an unidentifiable image never becomes a record.

@pytest.mark.parametrize("digest", ["", None, "sha256:abc", "ab" * 32,
                                    "sha512:" + "ab" * 32, "SHA256:" + "AB" * 32])
def test_a_missing_or_malformed_digest_is_refused(digest):
    with pytest.raises(ValueError, match="digest must be sha256"):
        EnvImage(image="ghcr.io/example-org/comx-qptiff:cx0.2.6", digest=digest,
                 source_ref="platform-v2")


@pytest.mark.parametrize("tag", ["latest", "main", "master"])
def test_a_floating_tag_is_refused_even_with_a_digest(tag):
    with pytest.raises(ValueError, match="floating tag"):
        EnvImage(image=f"ghcr.io/example-org/clinicalomx-review-app:{tag}", digest=DIGEST,
                 source_ref="platform-v2")


@pytest.mark.parametrize("image", [
    "ghcr.io/example-org/comx-qptiff",                     # no tag at all
    f"ghcr.io/example-org/comx-qptiff@{DIGEST}",           # digest form: no tag
    "",
    "ghcr.io/example-org/comx-qptiff:",
])
def test_a_reference_with_no_usable_tag_is_refused(image):
    with pytest.raises(ValueError, match="image must be"):
        EnvImage(image=image, digest=DIGEST, source_ref="platform-v2")


def test_source_ref_is_required():
    with pytest.raises(ValueError, match="source_ref is required"):
        EnvImage(image="ghcr.io/example-org/comx-qptiff:cx0.2.6", digest=DIGEST, source_ref="  ")


def test_an_empty_sif_reference_is_refused_rather_than_stamped():
    with pytest.raises(ValueError, match="never empty"):
        EnvImage(image="ghcr.io/example-org/comx-qptiff:cx0.2.6", digest=DIGEST,
                 source_ref="platform-v2", sif_oras_ref="")


def test_sif_key_for_image_refuses_an_untagged_reference():
    with pytest.raises(ValueError, match="not a tagged registry reference"):
        sif_key_for_image("ghcr.io/example-org/comx-qptiff")


def test_sif_key_for_image_refuses_a_reference_with_no_owner():
    with pytest.raises(ValueError, match="no owner"):
        sif_key_for_image("comx-qptiff:cx0.1.0")


def test_two_owners_never_share_a_sif_key():
    assert (sif_key_for_image("ghcr.io/old/croi-core-inference:v0.1.10")
            != sif_key_for_image("ghcr.io/new/croi-core-inference:v0.1.10"))


# Manifests.

def _write(tmp_path, name, **fields):
    path = tmp_path / name
    path.write_text(json.dumps(fields))
    return path


def test_manifest_round_trips(tmp_path):
    env = EnvImage(image="ghcr.io/example-org/croi-core-inference:v0.1.10", digest=DIGEST,
                   source_ref="clinical-roi-v0.1.10",
                   sif_oras_ref="oras://ghcr.io/example-org/croi-core-inference-sif:v0.1.10")
    path = tmp_path / "m.json"
    path.write_text(json.dumps(env.manifest()))
    assert EnvImage.from_manifest(path) == env


def test_a_manifest_omits_an_absent_sif_reference_rather_than_writing_it_empty():
    env = EnvImage(image="ghcr.io/example-org/croi-core-train:v0.1.10", digest=DIGEST,
                   source_ref="clinical-roi-v0.1.10")
    assert "sif_oras_ref" not in env.manifest()


def test_a_manifest_missing_the_digest_is_refused(tmp_path):
    path = _write(tmp_path, "m.json", image="ghcr.io/example-org/comx-qptiff:cx0.2.6",
                  source_ref="platform-v2")
    with pytest.raises(ValueError, match=r"missing \['digest'\]"):
        EnvImage.from_manifest(path)


def test_a_manifest_with_an_unknown_key_is_refused(tmp_path):
    path = _write(tmp_path, "m.json", image="ghcr.io/example-org/comx-qptiff:cx0.2.6",
                  digest=DIGEST, source_ref="platform-v2", sha="deadbeef")
    with pytest.raises(ValueError, match=r"unknown manifest keys \['sha'\]"):
        EnvImage.from_manifest(path)


# Planning: the .sif must be where the worker looks for it.

def _manifest_for(tmp_path, image, digest=DIGEST, **extra):
    name = image.rsplit("/", 1)[-1].replace(":", "_") + ".json"
    return _write(tmp_path, name, image=image, digest=digest,
                  source_ref="clinical-roi-v0.1.10", **extra)


def test_planning_pairs_each_manifest_with_the_key_the_worker_resolves(tmp_path):
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    root = tmp_path / "dbstorage"
    (root / "environments" / "example-org").mkdir(parents=True)
    for image, key in IMAGES[:3]:
        _manifest_for(manifests, image)
        (root / key).write_bytes(b"sif")

    planned = plan_registrations(root, manifests)

    assert {(env.image, str(sif)) for env, sif in planned} == {
        (image, str(root / key)) for image, key in IMAGES[:3]
    }


def test_planning_refuses_when_the_sif_is_not_under_the_storage_root(tmp_path):
    path = _manifest_for(tmp_path, "ghcr.io/example-org/croi-core-inference:v0.1.10",
                         sif_oras_ref="oras://ghcr.io/example-org/croi-core-inference-sif:v0.1.10")
    with pytest.raises(FileNotFoundError, match="apptainer pull"):
        plan_registrations(tmp_path / "dbstorage", path)


def test_planning_names_a_docker_build_when_no_sif_was_published(tmp_path):
    path = _manifest_for(tmp_path, "ghcr.io/example-org/croi-core-train:v0.1.10")
    with pytest.raises(FileNotFoundError, match=f"apptainer build .* docker://.*@{DIGEST}"):
        plan_registrations(tmp_path / "dbstorage", path)


def test_one_bad_manifest_refuses_the_whole_batch(tmp_path):
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    root = tmp_path / "dbstorage"
    (root / "environments" / "example-org").mkdir(parents=True)
    good, good_key = IMAGES[2]
    _manifest_for(manifests, good)
    (root / good_key).write_bytes(b"sif")
    _write(manifests, "review.json", image="ghcr.io/example-org/clinicalomx-review-app:latest",
           digest=OTHER_DIGEST, source_ref="platform-v2")

    with pytest.raises(ValueError, match="floating tag"):
        plan_registrations(root, manifests)


def test_planning_refuses_an_empty_manifest_directory(tmp_path):
    empty = tmp_path / "manifests"
    empty.mkdir()
    with pytest.raises(ValueError, match="no manifests found"):
        plan_registrations(tmp_path, empty)
