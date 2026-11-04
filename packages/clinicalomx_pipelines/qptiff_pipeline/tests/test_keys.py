"""The storage key layout. Every key descends from PIPELINE_ROOT and the slide folder, and
predictions are namespaced by model."""
from __future__ import annotations

import pytest

from conftest import load_pipeline_module

keys = load_pipeline_module("keys")

QPTIFF = "projects/Mater_ovarian/Arutha_YTMA 404-2_Scan1-16-bit.qptiff"
SLIDE_DIR = f"{keys.PIPELINE_ROOT}/arutha-ytma-404-2-scan1-16-bit-qptiff"
TMA = "Arutha_YTMA 404-2_Scan1-16-bit:TMA0"
CORE = f"{TMA}:R4C2"


# slug and slide_dir: the root every other key inherits.
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Arutha_YTMA 404-2", "arutha-ytma-404-2"),
        ("a__b--c", "a-b-c"),
        ("  leading and trailing  ", "leading-and-trailing"),
        ("TMA0", "tma0"),
        ("20250312_TNBC#001", "20250312-tnbc-001"),
        (0, "0"),
    ],
    ids=["spaces", "runs-collapse", "trimmed", "case", "hash", "non-str"],
)
def test_slug(raw, expected):
    assert keys.slug(raw) == expected


def test_slide_dir_keeps_the_suffix_in_the_slug():
    assert keys.slide_dir(QPTIFF) == SLIDE_DIR
    assert keys.slide_dir(QPTIFF).endswith("-qptiff")


def test_every_key_descends_from_the_pipeline_root_and_the_slide():
    built = [
        keys.general_key(QPTIFF, "pred"),
        keys.core_key(QPTIFF, TMA, "pred"),
        keys.general_accepted_key(QPTIFF, "run1"),
        keys.core_accepted_key(QPTIFF, TMA, "run1"),
        keys.qptiff_thumbnail_key(QPTIFF),
        keys.bgnorm_key(QPTIFF, CORE, "r1"),
        keys.bgnorm_qc_key(QPTIFF, CORE, "r1"),
    ]
    for k in built:
        assert k.startswith(f"{keys.PIPELINE_ROOT}/"), k
        assert k.startswith(SLIDE_DIR + "/"), k


def test_the_slide_folder_is_independent_of_the_source_directory():
    assert keys.slide_dir(QPTIFF) == keys.slide_dir(
        "/rdm/Q0000/elsewhere/Arutha_YTMA 404-2_Scan1-16-bit.qptiff")


# The model-versioned namespace.
def test_a_model_uid_versions_the_prediction_namespace():
    plain = keys.general_key(QPTIFF, "pred")
    versioned = keys.general_key(QPTIFF, "pred", model_uid="oWDCT2Yt9rXw")
    assert "predictions/general/" in plain
    assert "predictions/general-oWDCT2Yt9rXw/" in versioned
    assert plain != versioned


def test_core_predictions_are_versioned_too():
    versioned = keys.core_key(QPTIFF, TMA, "pred", model_uid="abc123")
    assert "predictions/core-abc123/" in versioned


def test_model_uid_none_yields_the_legacy_key():
    assert keys.general_key(QPTIFF, "pred", model_uid=None) == keys.general_key(QPTIFF, "pred")


def test_a_model_uid_does_not_version_a_non_prediction_kind():
    a = keys.general_accepted_key(QPTIFF, "run1")
    assert "predictions" not in a


# Accepted keys are unique per persist run.
def test_accepted_keys_are_stamped_by_the_run_so_a_re_accept_does_not_overwrite():
    first = keys.general_accepted_key(QPTIFF, "runAAA")
    second = keys.general_accepted_key(QPTIFF, "runBBB")
    assert first != second
    assert "accepted-runAAA/" in first and "accepted-runBBB/" in second


@pytest.mark.parametrize(("kind", "leaf"), [("cores", "cores.parquet"), ("dearray", "dearray.png")])
def test_core_accepted_kinds(kind, leaf):
    assert keys.core_accepted_key(QPTIFF, TMA, "run1", kind=kind).endswith(f"accepted-run1/{leaf}")


def test_an_unknown_accepted_kind_raises_rather_than_inventing_a_leaf():
    with pytest.raises(KeyError):
        keys.core_accepted_key(QPTIFF, TMA, "run1", kind="not-a-kind")


# TMA and core folders nest under the slide.
def test_tma_and_core_folders_nest_by_name_segment():
    tma_dir = keys.tma_dir(QPTIFF, TMA)
    assert tma_dir == f"{SLIDE_DIR}/tma0"
    assert keys.core_dir(QPTIFF, CORE).startswith(tma_dir + "/")
    assert keys.core_dir(QPTIFF, CORE).endswith("/r4c2")


def test_the_qptiff_thumbnail_is_not_under_predictions():
    k = keys.qptiff_thumbnail_key(QPTIFF)
    assert k == f"{SLIDE_DIR}/qptiff/thumbnail.png"
    assert "predictions" not in k


def test_bgnorm_keys_are_scoped_to_the_core_and_the_run():
    zarr = keys.bgnorm_key(QPTIFF, CORE, "runX")
    qc = keys.bgnorm_qc_key(QPTIFF, CORE, "runX")
    core_dir = keys.core_dir(QPTIFF, CORE)
    assert zarr.startswith(core_dir) and qc.startswith(core_dir)
    assert zarr != qc


def test_the_bgnorm_experiment_name_is_stable_for_a_core():
    a = keys.bgnorm_experiment_name(CORE)
    assert a == keys.bgnorm_experiment_name(CORE)
    assert a != keys.bgnorm_experiment_name(f"{TMA}:R0C0")
