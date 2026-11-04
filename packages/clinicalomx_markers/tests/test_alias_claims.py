"""Alias claims seeded from captured UniProt synonyms. Two aliases from one source coexist,
because `alias` is multi-valued."""
from __future__ import annotations

import dataclasses

import pytest
from clinicalomx_ledger.grammar.gate import Judgement
from clinicalomx_ledger.grammar.store import MemoryClaimStore

from clinicalomx_markers.domain.marker import MARKER_DOMAIN, MarkerClaim, MarkerPredicate
from clinicalomx_markers.propose import alias
from clinicalomx_markers.propose.human import HumanProposer
from clinicalomx_markers.propose.schema import Attention, ProposalRequest
from clinicalomx_markers.query.claims import settled
from clinicalomx_markers.sources.uniprot import fetch
from conftest import ACCESSIONS, fixture_transport

ALIAS = MarkerPredicate.ALIAS
ACCEPT = Judgement(by="rafael", at="2026-09-25")
REJECT = Judgement(by="rafael", at="2026-09-26", kind="one_off",
                   reason="not a synonym anyone here uses")


@pytest.fixture(scope="module")
def records():
    transport = fixture_transport()
    return {marker: fetch(accession, transport=transport)[0]
            for marker, accession in ACCESSIONS.items()}


def seeded(store, claims):
    for claim in claims:
        store.add(claim)
    return store


def served(store, subject):
    return sorted(d.value for d in settled(store.answers(subject), subject, predicate=ALIAS))


def test_a_proposed_alias_carries_its_confidence_and_its_source(records):
    claims = {c.value: c for c in alias.seed_claims(records) if c.subject == "E-CADHERIN"}
    assert set(claims) == {"CDHE", "UVO", "CAM 120/80", "Epithelial cadherin", "Uvomorulin"}
    cdhe = claims["CDHE"]
    assert (str(cdhe.source), cdhe.source_id, cdhe.source_version) == (
        "uniprot", "P12830", "2026_02")
    assert cdhe.confidence == alias.SYNONYM_CONFIDENCE
    assert cdhe.evidence == "gene synonym: CDHE"

    store = seeded(MemoryClaimStore(MARKER_DOMAIN), [cdhe])
    assert store.decisions("E-CADHERIN") == [], "a proposal decides nothing"
    assert store.in_state("unresolved") == ["E-CADHERIN"]
    assert [c.confidence for c in store.held("E-CADHERIN")] == [alias.SYNONYM_CONFIDENCE]


def test_a_person_proposes_an_alias_through_the_same_contract(records):
    request = ProposalRequest(marker="Ecad", evidence="panel sheet column 'Ecad'")
    proposal = HumanProposer(by="rafael").answer(request, "E-CADHERIN", confidence=0.95,
                                                  predicate=ALIAS)
    claim = alias.as_claim(proposal, request, source="human", source_id="rafael",
                           evidence=request.evidence)
    assert (claim.subject, claim.value, claim.confidence) == ("E-CADHERIN", "Ecad", 0.95)


def test_two_aliases_for_one_subject_from_one_source_coexist(records):
    both = [c for c in alias.seed_claims(records)
            if c.subject == "E-CADHERIN" and c.value in ("CDHE", "UVO")]
    assert {c.source_id for c in both} == {"P12830"}, "one source, one accession"
    store = seeded(MemoryClaimStore(MARKER_DOMAIN), both)
    assert sorted(c.value for c in store.held("E-CADHERIN")) == ["CDHE", "UVO"]

    seeded(store, both)
    assert len(store.held("E-CADHERIN", history=True)) == 2, "a re-seed writes nothing"

    for claim in both:
        store.decide(claim, outcome="accepted", judgement=ACCEPT)
    assert served(store, "E-CADHERIN") == ["CDHE", "UVO"]


def test_a_rejection_stops_the_alias_being_served_and_appends(records):
    both = [c for c in alias.seed_claims(records)
            if c.subject == "E-CADHERIN" and c.value in ("CDHE", "UVO")]
    store = seeded(MemoryClaimStore(MARKER_DOMAIN), both)
    for claim in both:
        store.decide(claim, outcome="accepted", judgement=ACCEPT)
    uvo = next(c for c in both if c.value == "UVO")
    log = list(store.held("E-CADHERIN", history=True))

    store.decide(uvo, outcome="rejected", judgement=REJECT)
    assert served(store, "E-CADHERIN") == ["CDHE"]
    assert store.held("E-CADHERIN", history=True) == log, "no claim was edited or removed"
    assert [str(d.outcome) for d in store.decisions("E-CADHERIN") if d.value == "UVO"] == [
        "accepted", "rejected"], "the acceptance is overruled, not erased"


def test_two_subjects_may_carry_the_same_alias_string_without_collision(records):
    """A synonym two entries list is proposed for each, with the rival ranked behind it."""
    vimentin = dataclasses.replace(records["VIMENTIN"], gene_synonyms=("CDHE",))
    claims = [c for c in alias.seed_claims({"E-CADHERIN": records["E-CADHERIN"],
                                            "VIMENTIN": vimentin})
              if c.value == "CDHE"]
    assert sorted((c.subject, c.source_id) for c in claims) == [
        ("E-CADHERIN", "P12830"), ("VIMENTIN", "P08670")]
    assert {c.confidence for c in claims} == {alias.SYNONYM_CONFIDENCE / 2}

    [for_vimentin] = [p for p in alias.UniProtAliasProposer(
        {"E-CADHERIN": records["E-CADHERIN"], "VIMENTIN": vimentin}).propose(
            ProposalRequest(marker="CDHE", evidence="gene synonym: CDHE"))
        if p.value == "VIMENTIN"]
    assert [a.value for a in for_vimentin.alternatives] == ["E-CADHERIN"]
    assert for_vimentin.triage.label == Attention.SCRUTINISE

    store = seeded(MemoryClaimStore(MARKER_DOMAIN), claims)
    for claim in claims:
        store.decide(claim, outcome="accepted", judgement=ACCEPT)
    store.decide(next(c for c in claims if c.subject == "VIMENTIN"), outcome="rejected",
                 judgement=REJECT)
    assert served(store, "E-CADHERIN") == ["CDHE"]
    assert served(store, "VIMENTIN") == []


def test_only_alias_is_multi_valued_and_a_role_still_supersedes():
    """Only `alias` is multi-valued: a source restating a role holds one position."""
    assert MARKER_DOMAIN.multi_valued_predicates == (ALIAS,)
    store = MemoryClaimStore(MARKER_DOMAIN)
    for role in ("marker", "cyto_structural"):
        store.add(MarkerClaim(subject="CD8A", predicate="segmentation_role", value=role,
                              source="uniprot", source_id="P01732", evidence="Cell membrane."))
    assert [c.value for c in store.held("CD8A")] == ["cyto_structural"]
