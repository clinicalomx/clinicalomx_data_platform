"""Consumer-owned vocabulary: declaration, conflict detection, discovery, ownership.

No database needed - declaring is pure; only ``ensure()`` touches an instance.
"""
from __future__ import annotations

import pytest

from clinicalomx.vocab.registry import FOUNDATION, Declared, Vocabulary, owners


def test_feature_returns_its_own_name():
    v = Vocabulary("myrepo")
    name = v.feature("bgnorm_attempts", int, "runner launches for this core")
    assert name == "bgnorm_attempts"
    assert v.features[name] == Declared("bgnorm_attempts", int,
                                        "runner launches for this core", "myrepo")


def test_redeclaring_the_same_dtype_is_idempotent():
    v = Vocabulary("myrepo")
    v.feature("x", int, "first")
    v.feature("x", int, "second")
    assert v.features["x"].description == "second"


def test_redeclaring_a_different_dtype_raises():
    v = Vocabulary("myrepo")
    v.feature("x", int, "an int")
    with pytest.raises(ValueError, match="different dtypes"):
        v.feature("x", str, "now a str")


def test_type_records_its_parent():
    v = Vocabulary("myrepo")
    v.type("MyRoiKind", "What it is.", parent="ROI")
    decl = v.types["MyRoiKind"]
    assert decl.dtype == "ROI" and decl.owner == "myrepo"


def test_owners_attributes_the_base_to_the_foundation():
    from clinicalomx.vocab.dataio import features as declared_features

    mapping = owners()
    assert mapping, "features.csv is empty"
    base = {row["name"].strip() for row in declared_features()}
    assert base, "features.csv declares no features"
    wrong = {name: mapping.get(name) for name in base if mapping.get(name) != FOUNDATION}
    assert not wrong, f"base names not attributed to the foundation: {wrong}"


def test_foundation_is_small_and_framework_facing():
    from clinicalomx.names import ALL_FEATURES

    assert len(ALL_FEATURES) < 40, (
        f"{len(ALL_FEATURES)} foundation features - check whether a consumer-owned name "
        "was added to names.py instead of that repo's Vocabulary"
    )
    for leaked in ("bgnorm_attempts", "qptiff_dims", "detections", "cell_tables", "image_type"):
        assert leaked not in ALL_FEATURES, f"{leaked} is consumer-owned, not foundation"
