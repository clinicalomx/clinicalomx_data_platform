"""Deterministic localisation and the role proposer.

Citrate synthase must be demoted; LDHA and FOXP3 must not be promoted.
"""
from __future__ import annotations

import pytest
from clinicalomx_ledger.grammar.model import validate
from tests.conftest import ACCESSIONS

from clinicalomx_markers.propose.localisation import claims_from, record_from
from clinicalomx_markers.propose.role import RuleBasedRoleProposer, as_claim
from clinicalomx_markers.propose.schema import ProposalRequest
from clinicalomx_markers.seed_data.rows import channels
from clinicalomx_markers.sources.snapshot import capture
from clinicalomx_markers.sources.uniprot import url_for
from clinicalomx_markers.vocab import MarkerPredicate, SegmentationRole


def snapshot_for(marker, snapshot_root, transport):
    accession = ACCESSIONS[marker]
    return capture(snapshot_root, source="uniprot", source_id=accession, retrieved_at="2026-07-31",
                   fetch=lambda: transport(url_for(accession)))


def request_for(marker, snapshot_root, transport, **over):
    snap = snapshot_for(marker, snapshot_root, transport)
    record = record_from(snap)
    base = {
        "marker": marker,
        "evidence": record.evidence,
        "locations": record.locations,
        "go_cc": record.go_cc,
    }
    base.update(over)
    return ProposalRequest(**base), snap


def test_localisation_claims_are_fully_cited(snapshot_root, transport):
    snap = snapshot_for("NAKATPASE", snapshot_root, transport)
    claims = claims_from(snap, "NAKATPASE")
    assert claims, "expected at least a subcellular_location claim"
    for claim in claims:
        validate(claim)                       # provenance completeness
        assert claim.source_id == snap.source_id
        assert "status" not in type(claim).model_fields, \
            "a claim is what a source said; what the lab thinks of it is a Decision"


def test_localisation_needs_no_model(snapshot_root, transport):
    """A structured field is parsed without a model."""
    snap = snapshot_for("VIMENTIN", snapshot_root, transport)
    first = [c.value for c in claims_from(snap, "VIMENTIN")]
    second = [c.value for c in claims_from(snap, "VIMENTIN")]
    assert first == second and first


@pytest.mark.parametrize("marker,expected", [
    ("NAKATPASE", "cyto_structural"),      # cell membrane
    ("E-CADHERIN", "cyto_structural"),     # cell junction
    ("CITRATE_SYNTHASE", "marker"),        # mitochondrial: the veto
    ("LDHA", "marker"),                    # soluble in cytoplasm: the inverse trap
    ("FOXP3", "marker"),                   # nucleus AND cytoplasm: the other direction
])
def test_role_proposals_match_the_curated_judgement(marker, expected, snapshot_root, transport):
    request, _ = request_for(marker, snapshot_root, transport)
    proposals = RuleBasedRoleProposer().propose(request)
    assert len(proposals) == 1
    assert proposals[0].value == expected, proposals[0].reasoning


def test_the_mitochondrial_veto_fires_and_names_the_term(snapshot_root, transport):
    request, _ = request_for("CITRATE_SYNTHASE", snapshot_root, transport)
    proposal = RuleBasedRoleProposer().propose(request)[0]
    assert proposal.value == "marker"
    assert "mitochondrion" in proposal.reasoning.lower(), proposal.reasoning


def test_extra_locations_do_not_disqualify(snapshot_root, transport):
    """NaK-ATPase is Cell membrane and Melanosome, and still structural. The veto is the absence
    of structural evidence, not the presence of an organelle."""
    request, _ = request_for("NAKATPASE", snapshot_root, transport)
    assert any("melanosome" in loc.lower() for loc in request.locations), \
        "fixture should still contain the extra location this guards"
    assert RuleBasedRoleProposer().propose(request)[0].value == "cyto_structural"


def test_localisation_alone_cannot_settle_a_lineage_marker(snapshot_root, transport):
    """Known limit: CD8A is a membrane protein but only on T cells; its curated role is `marker`.
    Localisation cannot see this; human review does."""
    request, _ = request_for("CD8A", snapshot_root, transport)
    proposed = RuleBasedRoleProposer().propose(request)[0].value
    curated = next(
        c["segmentation_role"] for c in channels() if c["canonical_name"] == "CD8A"
    )
    assert proposed == "cyto_structural"
    assert curated == "marker"
    assert proposed != curated, "if these ever agree, revisit this test"


def test_proposals_are_in_the_controlled_set(snapshot_root, transport):
    for marker in ACCESSIONS:
        request, _ = request_for(marker, snapshot_root, transport)
        for proposal in RuleBasedRoleProposer().propose(request):
            assert proposal.value in tuple(SegmentationRole)


def test_a_proposal_becomes_a_citable_claim(snapshot_root, transport):
    request, snap = request_for("NAKATPASE", snapshot_root, transport, attempt=3)
    proposal = RuleBasedRoleProposer().propose(request)[0]
    claim = as_claim(proposal, request, source=snap.source, source_id=snap.source_id,
                     evidence=request.evidence)
    validate(claim, subject_row={"canonical_name": "NAKATPASE",
                                 "compartment": " ".join(request.locations)})
    assert claim.predicate == MarkerPredicate.SEGMENTATION_ROLE


def test_the_attempt_stays_a_proposer_input_and_never_reaches_the_claim(snapshot_root, transport):
    """`ProposalRequest.attempt` stays a proposer input and never reaches the claim."""
    from clinicalomx_markers.domain.marker import MarkerClaim

    request, snap = request_for("NAKATPASE", snapshot_root, transport, attempt=3)
    assert request.attempt == 3
    claim = as_claim(RuleBasedRoleProposer().propose(request)[0], request,
                     source=snap.source, source_id=snap.source_id, evidence=request.evidence)
    assert "attempt" not in MarkerClaim.model_fields
    assert not hasattr(claim, "attempt")


def test_steering_reaches_the_proposer(snapshot_root, transport):
    request, _ = request_for("LDHA", snapshot_root, transport, attempt=2,
                             steering=("consider metabolic state",))
    assert request.steering == ("consider metabolic state",)
    assert RuleBasedRoleProposer().propose(request)[0].usable


def test_an_invalid_role_is_refused_when_made_into_a_claim(snapshot_root, transport):
    from clinicalomx_markers.propose.schema import Proposal, assess

    request, snap = request_for("LDHA", snapshot_root, transport)
    bogus = Proposal(predicate=MarkerPredicate.SEGMENTATION_ROLE, value="definitely_structural",
                     confidence=0.9, triage=assess(0.9))
    with pytest.raises(ValueError, match="not a valid SegmentationRole"):
        as_claim(bogus, request, source="uniprot", source_id="P00338", evidence="x")
