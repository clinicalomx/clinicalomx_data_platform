"""The human proposer beside the machine ones: same protocol, same curate/ flow, same
evidence rule. Offline. Actor typing is tested in `tests_pg/test_ledger_store.py`."""
from __future__ import annotations

import pytest
from clinicalomx_ledger.grammar.gate import Judgement
from clinicalomx_ledger.grammar.store import MemoryClaimStore
from tests.test_propose import request_for

from clinicalomx_markers.domain.marker import MARKER_DOMAIN, MarkerSource
from clinicalomx_markers.propose.human import HumanProposer
from clinicalomx_markers.propose.role import RuleBasedRoleProposer, as_claim
from clinicalomx_markers.propose.schema import Proposal, ProposalRequest, Proposer
from clinicalomx_markers.query.claims import value_of
from clinicalomx_markers.vocab import MarkerPredicate, ReviewState, SegmentationRole

APPROVAL = Judgement(by="rafael", at="2026-08-24")


def drive(proposer: Proposer, request: ProposalRequest) -> list[Proposal]:
    """A caller typed on the protocol."""
    return proposer.propose(request)


def test_the_human_proposer_satisfies_the_same_protocol(snapshot_root, transport):
    request, _ = request_for("NAKATPASE", snapshot_root, transport)
    human = HumanProposer(by="rafael")
    human.answer(request, SegmentationRole.CYTO_STRUCTURAL, confidence=0.9)
    for proposer in (human, RuleBasedRoleProposer()):
        assert isinstance(proposer.name, str) and proposer.name
        assert all(isinstance(p, Proposal) for p in drive(proposer, request))


def test_the_two_paths_produce_interchangeable_proposals(snapshot_root, transport):
    """Same request in, equal `Proposal` out."""
    request, _ = request_for("NAKATPASE", snapshot_root, transport)

    machine = RuleBasedRoleProposer().propose(request)[0]
    human = HumanProposer(by="rafael")
    human.answer(request, SegmentationRole.CYTO_STRUCTURAL, confidence=machine.confidence,
                 reasoning=machine.reasoning, alternatives=machine.alternatives)

    assert human.propose(request) == [machine]
    assert set(vars(machine)) == set(vars(human.propose(request)[0])), \
        "one shape, so a downstream branch on the path has nothing to read"


def test_both_paths_reach_an_accepted_claim_through_one_curate_flow(snapshot_root, transport):
    """One driver, two proposers, one curate/ flow. Only the citations differ."""
    store = MemoryClaimStore(MARKER_DOMAIN)
    request, snap = request_for("NAKATPASE", snapshot_root, transport)

    human = HumanProposer(by="rafael")
    human.answer(request, SegmentationRole.CYTO_STRUCTURAL, confidence=0.9,
                 reasoning="membrane, so it outlines")
    citations = [
        (RuleBasedRoleProposer(), {"source": snap.source, "source_id": snap.source_id}),
        (human, {"source": MarkerSource.HUMAN, "source_id": human.by}),
    ]

    for proposer, cited in citations:
        for proposal in drive(proposer, request):
            claim = as_claim(proposal, request, evidence=request.evidence, **cited)
            store.add(claim, outcome="accepted", judgement=APPROVAL)

    assert store.in_state(ReviewState.ACCEPTED) == ["NAKATPASE"]
    settled = value_of(store.answers("NAKATPASE"), "NAKATPASE",
                       MarkerPredicate.SEGMENTATION_ROLE)
    assert settled is not None and settled.value == "cyto_structural"
    assert sorted(str(c.source) for c in store.held("NAKATPASE")) == ["human", "uniprot"], \
        "two sources agreeing both stand; neither is the lesser one"
    assert len(store.answers("NAKATPASE")) == 2, "and both are the answer"


def test_an_unevidenced_human_proposal_is_refused(snapshot_root, transport):
    request, _ = request_for("NAKATPASE", snapshot_root, transport, evidence="   ")
    with pytest.raises(ValueError, match="citable"):
        HumanProposer(by="rafael").answer(request, SegmentationRole.CYTO_STRUCTURAL,
                                             confidence=0.9)


def test_an_unevidenced_claim_is_refused_on_either_path(snapshot_root, transport):
    store = MemoryClaimStore(MARKER_DOMAIN)
    request, snap = request_for("NAKATPASE", snapshot_root, transport)
    proposal = RuleBasedRoleProposer().propose(request)[0]

    for cited in ({"source": snap.source, "source_id": snap.source_id},
                  {"source": MarkerSource.HUMAN, "source_id": "rafael"}):
        with pytest.raises(ValueError, match="not citable"):
            store.add(as_claim(proposal, request, evidence="", **cited))


def test_an_anonymous_human_proposer_is_refused():
    with pytest.raises(ValueError, match="blank"):
        HumanProposer(by="  ")


def test_a_promoted_rule_tells_the_person_rather_than_rewriting_them(snapshot_root, transport):
    """A promoted rule raises on the human path, where the machine paths demote."""
    request, _ = request_for("CITRATE_SYNTHASE", snapshot_root, transport)
    assert RuleBasedRoleProposer().propose(request)[0].value == "marker", \
        "the machine path demotes rather than raising"
    with pytest.raises(ValueError, match="forbidden"):
        HumanProposer(by="rafael").answer(request, SegmentationRole.CYTO_STRUCTURAL,
                                             confidence=0.9)


def test_a_subject_the_person_has_not_answered_proposes_nothing():
    unanswered = ProposalRequest(marker="CD8A", evidence="Cell membrane.")
    assert HumanProposer(by="rafael").propose(unanswered) == []


def test_the_human_path_does_not_pull_the_agent_stack():
    import subprocess
    import sys

    probe = ("import sys; import clinicalomx_markers.propose.human as h; "
             "print(sorted(m for m in sys.modules if m in {'openai', 'httpx', 'anthropic'}))")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                         check=True)
    assert out.stdout.strip() == "[]", out.stdout


def test_a_proposal_carries_no_actor(snapshot_root, transport):
    """A proposal has no actor field."""
    assert "by" not in Proposal.__dataclass_fields__
    assert "actor" not in Proposal.__dataclass_fields__
    assert "actor_kind" not in Proposal.__dataclass_fields__
