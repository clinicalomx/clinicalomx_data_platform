"""Target claims: candidate proteins, the set's kind, and how localisations combine.
Overlap is the default; only a declared kind sums."""
from __future__ import annotations

import pytest
from clinicalomx_ledger.grammar.store import MemoryClaimStore

from clinicalomx_markers.domain import target as target_module
from clinicalomx_markers.domain.marker import MARKER_DOMAIN, MarkerPredicate
from clinicalomx_markers.domain.target import (
    SUMMED_KINDS,
    Combine,
    TargetSet,
    combination,
    combine,
    seed_claims,
    target_claim,
)
from clinicalomx_markers.seed_data.rows import markers, targets
from clinicalomx_markers.seed_spec import TargetKind

KERATINS = ("P05787", "P08727", "P05783")
LOCATIONS = {
    "P05787": ("Cytoplasm", "Nucleus"),
    "P08727": ("Cytoplasm", "Cell membrane"),
    "P05783": ("Cytoplasm",),
}


# --- the four kinds ---------------------------------------------------------


def test_a_single_target_is_its_one_proteins_localisation():
    one = TargetSet("single", ("P08727",))
    assert combine(one, LOCATIONS) == ("Cytoplasm", "Cell membrane")
    assert combination(TargetKind.SINGLE) == Combine.OVERLAP


def test_an_isoform_ambiguous_target_overlaps():
    """Only what every isoform has is claimed."""
    isoforms = TargetSet("isoform_ambiguous", ("P08575-1", "P08575-8"))
    located = {"P08575-1": ("Cell membrane", "Secreted"), "P08575-8": ("Cell membrane",)}
    assert combination(TargetKind.ISOFORM_AMBIGUOUS) == Combine.OVERLAP
    assert combine(isoforms, located) == ("Cell membrane",)


def test_a_cross_reactive_target_sums():
    """panCK binds every keratin at once, so its signal is the union."""
    panck = TargetSet("cross_reactive", KERATINS)
    assert combination(TargetKind.CROSS_REACTIVE) == Combine.SUM
    assert set(combine(panck, LOCATIONS)) == {"Cytoplasm", "Nucleus", "Cell membrane"}


def test_a_complex_member_target_overlaps():
    complex_ = TargetSet("complex_member", KERATINS)
    assert combination(TargetKind.COMPLEX_MEMBER) == Combine.OVERLAP
    assert combine(complex_, LOCATIONS) == ("Cytoplasm",)


def test_only_a_declared_kind_sums_and_every_other_overlaps():
    assert SUMMED_KINDS == {TargetKind.CROSS_REACTIVE}
    for kind in TargetKind:
        assert combination(kind) == (Combine.SUM if kind in SUMMED_KINDS else Combine.OVERLAP)


def test_overlap_is_what_happens_when_nothing_declares_sum(monkeypatch):
    """Without the declaration, every kind overlaps."""
    monkeypatch.setattr(target_module, "SUMMED_KINDS", frozenset())
    assert {combination(kind) for kind in TargetKind} == {Combine.OVERLAP}
    assert combine(TargetSet("cross_reactive", KERATINS), LOCATIONS) == ("Cytoplasm",)


# --- the candidate set is kept whole ------------------------------------------


def test_every_candidate_survives_the_claim_value_round_trip():
    panck = TargetSet("cross_reactive", KERATINS)
    claim = target_claim("PANCK", panck, source="human", source_id="rafael",
                         evidence="datasheet: reacts with keratins 5, 8, 18")
    assert claim.predicate == MarkerPredicate.TARGET
    assert set(TargetSet.parse(claim.value).candidates) == set(KERATINS)
    assert TargetSet.parse(claim.value) == panck


def test_a_multi_candidate_claim_is_held_whole_by_a_store():
    """One claim holds the whole set."""
    store = MemoryClaimStore(MARKER_DOMAIN)
    store.add(target_claim("PANCK", TargetSet("cross_reactive", KERATINS), source="human",
                           source_id="rafael", evidence="datasheet"))
    held = store.held("PANCK", predicate=MarkerPredicate.TARGET)
    assert len(held) == 1
    assert TargetSet.parse(held[0].value).candidates == tuple(sorted(KERATINS))


def test_candidate_order_does_not_change_the_claim():
    """One set in two orders is one claim id."""
    assert (TargetSet("cross_reactive", KERATINS).value
            == TargetSet("cross_reactive", tuple(reversed(KERATINS))).value)


def test_a_candidate_with_no_localisation_is_refused_rather_than_dropped():
    """A candidate with no localisation raises."""
    located = {k: v for k, v in LOCATIONS.items() if k != "P05783"}
    with pytest.raises(ValueError, match="P05783"):
        combine(TargetSet("complex_member", KERATINS), located)


@pytest.mark.parametrize("kind, candidates, why", [
    ("single", ("P05787", "P08727"), "exactly one"),
    ("cross_reactive", ("P05787",), "several candidates"),
    ("isoform_ambiguous", ("P08575-1", "P12830-2"), "isoforms of one protein"),
    ("complex_member", ("P05787", "P05787"), "repeated"),
    ("single", ("CD8",), "not a UniProt accession"),
    ("single", (), "at least one"),
    ("mostly", ("P05787",), "is not a valid TargetKind"),
])
def test_a_set_whose_kind_and_candidates_disagree_is_refused(kind, candidates, why):
    with pytest.raises(ValueError, match=why):
        TargetSet(kind, candidates)


@pytest.mark.parametrize("value", ["P12830", "single:", "", "see the datasheet"])
def test_an_unreadable_value_is_refused_on_parse(value):
    with pytest.raises(ValueError):
        TargetSet.parse(value)


# --- the seed ---------------------------------------------------------------


def test_every_seeded_target_names_a_known_marker():
    known = {r["canonical_name"] for r in markers()}
    assert {r["canonical_name"] for r in targets()} <= known


def test_the_seeded_targets_read_as_claims():
    claims = seed_claims()
    assert len(claims) == len(targets())
    assert {c.subject for c in claims} >= {"E-CADHERIN", "CD8A"}
    assert all(c.predicate == MarkerPredicate.TARGET and c.source == "human" for c in claims)


def test_a_seeded_multi_candidate_row_keeps_every_candidate():
    row = {"canonical_name": "PANCK", "kind": "cross_reactive",
           "candidates": "|".join(KERATINS), "evidence": "datasheet", "curated_by": "rafael"}
    (claim,) = seed_claims([row])
    assert set(TargetSet.parse(claim.value).candidates) == set(KERATINS)
