"""The declared vocabulary: it imports, and its declarations match the owning constants.

`declared` is the `Vocabulary` object and `vocab` is the module.
"""
from __future__ import annotations

import pytest
from tests.conftest import values_of

from clinicalomx_markers import vocab


def test_the_vocabulary_imports():
    from clinicalomx_markers.vocab import VOCAB

    assert VOCAB is not None


def test_declaring_the_vocabulary_needs_no_database():
    """clinicalomx.vocab.registry imports without connecting: `import lamindb` connects."""
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-c", "import clinicalomx_markers.vocab, sys;"
                              " print('lamindb' in sys.modules)"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == "False", "declaring vocabulary must not import lamindb"


@pytest.fixture
def declared():
    """What `vocab.py` declared into the `Vocabulary`."""
    return vocab.VOCAB


def test_every_declared_type_is_named(declared):
    for name in declared.types:
        assert name in values_of(vocab.TYPES), f"{name} declared but absent from TYPES"


def test_every_declared_feature_is_named(declared):
    for name in declared.features:
        assert name in values_of(vocab.FEATURES), f"{name} declared but absent from FEATURES"


def test_every_named_feature_is_declared(declared):
    """Every constant reaches a declaration."""
    for named in values_of(vocab.FEATURES):
        assert named in declared.features, f"FEATURES.{named} is named but not declared"


def test_the_disputable_values_are_claims_and_not_features(declared):
    """These five were retired as features and must not return. A literal list on purpose."""
    for retired in ("targets", "aliases", "measures", "segmentation_role", "review_notes"):
        assert retired not in declared.features, \
            f"{retired} is disputable or derived, so it is a claim and not a feature"
        assert retired not in values_of(vocab.FEATURES), \
            f"{retired} is named but should be retired"
    assert vocab.MarkerPredicate.SEGMENTATION_ROLE in values_of(vocab.MarkerPredicate), \
        "the role still exists; it is a claim predicate, which is where it belongs"


def test_a_decision_is_declared_once_and_not_twice(declared):
    """These four now live only on the verdict."""
    for retired in ("claim_verdict_kind", "claim_verdict_reason", "claim_verdict_by",
                    "claim_verdict_at"):
        assert retired not in declared.features, \
            f"{retired} restates the Verdict record, which is the copy the rule refuses"
        assert retired not in values_of(vocab.FEATURES), \
            f"{retired} is named but should be retired"
    for kept in (vocab.FEATURES.VERDICT_KIND, vocab.FEATURES.VERDICT_REASON,
                 vocab.FEATURES.VERDICT_BY, vocab.FEATURES.VERDICT_AT):
        assert kept in declared.features, "the decision itself is still declared"


def test_the_aggregate_declares_what_the_owners_define(declared):
    """`vocab.py` declares the very strings its owning modules define."""
    from clinicalomx_markers.domain.imaging_channel import CHANNEL_FEATURES
    from clinicalomx_markers.domain.marker import MARKER_FEATURES, MARKER_TYPES
    from clinicalomx_markers.domain.reagent_mapping import MAPPING_FEATURES, MAPPING_TYPES
    from clinicalomx_markers.sources.snapshot import SNAPSHOT_FEATURES

    owned = set().union(*(values_of(owner) for owner in
                          (vocab.MODEL_FEATURES, MARKER_FEATURES, MAPPING_FEATURES,
                           SNAPSHOT_FEATURES, CHANNEL_FEATURES)))
    assert owned <= set(values_of(vocab.FEATURES))
    assert owned <= set(declared.features), \
        "an owned name that nothing declares is unwritable"

    domains = set(values_of(MARKER_TYPES)) | set(values_of(MAPPING_TYPES))
    assert domains <= set(values_of(vocab.TYPES))
    assert set(values_of(vocab.TYPES)) - domains == {
        vocab.TYPES.MEASUREMENT, vocab.TYPES.BLOOD_PROTEOMIC_ASSAY,
    }, "the only types vocab.py may spell itself are the two no code names"


def test_the_structural_identity_features_stay(declared):
    """`platform` and `claim_retrieved_at` are structural and stay."""
    assert vocab.FEATURES.PLATFORM in declared.features
    assert vocab.FEATURES.CLAIM_RETRIEVED_AT in declared.features


def test_the_unimplemented_subtype_is_named_but_not_declared(declared):
    assert vocab.TYPES.BLOOD_PROTEOMIC_ASSAY in values_of(vocab.TYPES)
    assert vocab.TYPES.BLOOD_PROTEOMIC_ASSAY not in declared.types


def test_the_verdict_type_is_parentless(declared):
    """`Declared.dtype` carries the parent name, so a parentless type has dtype None."""
    assert declared.types[vocab.TYPES.MARKER_VERDICT].dtype is None
    assert declared.types[vocab.TYPES.MAPPING_VERDICT].dtype is None


def test_the_imaging_channel_type_is_parentless(declared):
    assert declared.types[vocab.TYPES.IMAGING_CHANNEL].dtype is None
    assert vocab.TYPES.MEASUREMENT not in {d.dtype for d in declared.types.values()}


def test_a_declared_parent_still_lands_in_dtype():
    """Positive control for the parentless assertions: a parent still lands in `dtype`."""
    from clinicalomx.vocab import Vocabulary

    throwaway = Vocabulary("clinicalomx_markers_parent_control")
    throwaway.type("ControlParent", "A throwaway umbrella, declared only in this test.")
    throwaway.type(
        "ControlChild",
        "A throwaway type under that umbrella.",
        parent="ControlParent",
    )

    assert throwaway.types["ControlChild"].dtype == "ControlParent"
    assert throwaway.types["ControlParent"].dtype is None


def test_two_domains_declare_two_claim_types_and_no_shared_one(declared):
    """Each domain has its own claim and verdict types; none is shadowed by inheritance."""
    for named in (vocab.TYPES.MARKER_CLAIM, vocab.TYPES.MAPPING_CLAIM,
                  vocab.TYPES.MARKER_VERDICT, vocab.TYPES.MAPPING_VERDICT):
        assert named in declared.types, f"{named} is named but not declared"
    assert len({vocab.TYPES.MARKER_CLAIM, vocab.TYPES.MAPPING_CLAIM}) == 2


def test_the_artifact_subject_type_is_named_but_not_declared(declared):
    """`Artifact` is a constant only: lamindb owns that registry."""
    assert vocab.TYPES.ARTIFACT in values_of(vocab.TYPES)
    assert vocab.TYPES.ARTIFACT not in declared.types


def test_the_claim_features_are_shared_between_the_domains(declared):
    """One set of claim features for both domains. Only the review-state feature differs."""
    for shared in values_of(vocab.MODEL_FEATURES):
        assert shared in declared.features
    assert vocab.FEATURES.MARKER_STATE != vocab.FEATURES.MAPPING_STATE
    assert {vocab.FEATURES.MARKER_STATE, vocab.FEATURES.MAPPING_STATE} <= set(declared.features)


def test_the_uniprot_pin_is_explicit(declared):
    """Pinned: bionty resolves a different parquet per version."""
    assert declared.sources["Protein"]["source"] == "uniprot"
    assert declared.sources["Protein"]["version"] == "2024-03"
    assert declared.sources["Protein"]["organism"] == "human"
    assert declared.sources["Protein"]["relink"] is False


def test_the_efo_pin_is_explicit_and_is_the_channel_platform_source(declared):
    """The EFO pin is declared from `PLATFORM_SOURCE`, and `platform` is categorical over it."""
    from clinicalomx_markers.domain.imaging_channel import PLATFORM_SOURCE

    pin = declared.sources["ExperimentalFactor"]
    assert (pin["source"], pin["version"]) == ("efo", "3.85.0")
    assert pin["version"] == PLATFORM_SOURCE["version"]
    assert pin["relink"] is True
    assert declared.features[vocab.FEATURES.PLATFORM].dtype == "cat[bionty.ExperimentalFactor]"


def test_the_audit_attributes_efo_to_this_package():
    from clinicalomx.vocab.registry import source_owners

    assert source_owners()["ExperimentalFactor"] == "clinicalomx_markers"


def test_the_claim_features_cover_the_required_provenance(declared):
    for required in vocab.REQUIRED_PROVENANCE:
        assert required in declared.features, f"{required} is required of a claim but not declared"
