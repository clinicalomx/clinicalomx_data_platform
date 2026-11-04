"""`LedgerClaimStore` against a throwaway instance, walked beside `MemoryClaimStore`.

One write sequence must leave both answering `held`, `decisions`, `answers` and the queue
identically. Also checks typed actors, stored confidence, run citation and the ledger's `reads`.
"""

from __future__ import annotations

import lamindb as ln
import pytest
from clinicalomx_ledger.grammar.gate import Judgement
from clinicalomx_ledger.grammar.store import LedgerClaimStore

from clinicalomx_markers.domain.marker import (
    MARKER_DOMAIN,
    MarkerClaim,
    MarkerPredicate,
    MarkerSourceInvalidation,
)
from conftest import AGENT, BRANCH, HUMAN, subject

APPROVAL = Judgement(by="rafael", at="2026-08-24")
REFUSAL = Judgement(by="rafael", at="2026-08-24", kind="one_off",
                    reason="wrong for this panel")


def role(name, uid, **over) -> MarkerClaim:
    base = {"subject": name, "predicate": MarkerPredicate.SEGMENTATION_ROLE,
                "value": "cyto_structural", "source": "uniprot", "source_id": f"P05023-{uid}",
                "source_version": "2024_03",
                "evidence": "Cell membrane; Multi-pass membrane protein."}
    return MarkerClaim(**{**base, **over})


def location(name, uid, **over) -> MarkerClaim:
    base = {"subject": name, "predicate": MarkerPredicate.SUBCELLULAR_LOCATION,
                "value": "Cell membrane", "source": "uniprot", "source_id": f"P05023-{uid}",
                "source_version": "2024_03",
                "evidence": "Cell membrane; Multi-pass membrane protein."}
    return MarkerClaim(**{**base, **over})


def paired(memory, ledger, *writes):
    """Apply one write sequence to both backends; each write is (claim, kwargs)."""
    for claim, kwargs in writes:
        memory.add(claim, **kwargs)
        ledger.add(claim, **kwargs)


def agree(memory, ledger, name, *, predicate=None):
    """Both backends answer the reads identically for ``name``."""
    for history in (False, True):
        assert (ledger.held(name, predicate=predicate, history=history)
                == memory.held(name, predicate=predicate, history=history))
    for read in ("decisions", "answers"):
        assert (getattr(ledger, read)(name, predicate=predicate)
                == getattr(memory, read)(name, predicate=predicate)), read


def test_the_protocol_sequence_runs_identically_on_both_backends(stores, unique):
    name, memory, ledger = stores
    second = Judgement(by="wenchao", at="2026-08-25")
    steps = [
        (role(name, unique), {}),
        (role(name, unique), {"outcome": "accepted", "judgement": APPROVAL}),
        (role(name, unique), {"outcome": "accepted", "judgement": second}),
        (role(name, unique), {"outcome": "accepted", "judgement": second}),
        (role(name, unique, value="marker", evidence="Surface stain."), {}),
        (role(name, unique, source="pubmed", source_id=f"PMID-{unique}",
              source_version=""), {}),
    ]
    for claim, kwargs in steps:
        paired(memory, ledger, (claim, kwargs))
        agree(memory, ledger, name)

    assert len(ledger.held(name, history=True)) == 3
    assert len(ledger.decisions(name)) == 2, "accept and re-affirm; a proposal writes none"
    assert [d.value for d in ledger.answers(name)] == ["cyto_structural"]
    assert name in ledger.in_state("unresolved"), "the superseding value re-queued it"


def test_supersession_and_voids_project_identically(stores, unique):
    """Supersession and voids project identically, one sequence per case."""
    name, memory, ledger = stores
    cases = [
        [location(name, unique), location(name, unique, value="Nucleus")],
        [location(name, unique),
         location(name, unique, source="pubmed", source_id=f"PMID-{unique}",
                  source_version="", value="Basolateral only")],
        [location(name, unique),
         location(name, unique, predicate=MarkerPredicate.FUNCTION,
                  value="ion transport")],
    ]
    for claims in cases:
        for claim in claims:
            paired(memory, ledger, (claim, {}))
        agree(memory, ledger, name)

    void = MarkerSourceInvalidation(
        source="uniprot", source_id=f"P05023-{unique}", source_version="2024_03",
        reason="the release shifted the location column", by="rafael", at="2026-08-27")
    memory.invalidate(void)
    ledger.invalidate(void)
    agree(memory, ledger, name)
    assert {str(c.source) for c in ledger.held(name)} == {"pubmed"}
    assert void in ledger.invalidations(), "read back whole, stated date included"


def test_a_bounded_void_restores_the_earlier_release(stores, unique):
    name, memory, ledger = stores
    paired(memory, ledger,
           (location(name, unique, source_version="2024_03"), {}),
           (location(name, unique, source_version="2026_03", value="Nucleus"), {}))
    void = MarkerSourceInvalidation(
        source="uniprot", source_id=f"P05023-{unique}", source_version="2026_03",
        reason="bad release", by="rafael", at="2026-08-27")
    memory.invalidate(void)
    ledger.invalidate(void)
    agree(memory, ledger, name)
    assert [c.source_version for c in ledger.held(name)] == ["2024_03"]


def test_a_later_stated_date_is_a_second_decision_and_a_repeat_is_not(stores, unique):
    name, memory, ledger = stores
    later = Judgement(by=APPROVAL.by, at="2026-08-25")
    paired(memory, ledger,
           (role(name, unique), {"outcome": "accepted", "judgement": APPROVAL}),
           (role(name, unique), {"outcome": "accepted", "judgement": APPROVAL}),
           (role(name, unique), {"outcome": "accepted", "judgement": later}))
    agree(memory, ledger, name)
    decided = ledger.decisions(name)
    assert [str(d.outcome) for d in decided] == ["accepted", "accepted"]
    assert [d.at for d in decided] == ["2026-08-24", "2026-08-25"]


def test_a_rejection_cycle_keeps_every_lap_and_the_queue_follows(stores, unique):
    name, memory, ledger = stores
    paired(memory, ledger,
           (role(name, unique), {"outcome": "accepted", "judgement": APPROVAL}),
           (role(name, unique), {"outcome": "rejected", "judgement": REFUSAL}),
           (role(name, unique), {"outcome": "accepted", "judgement": APPROVAL}),
           (role(name, unique), {"outcome": "rejected", "judgement": REFUSAL}))
    agree(memory, ledger, name)
    assert [str(d.outcome) for d in ledger.decisions(name)] == [
        "accepted", "rejected", "accepted", "rejected"]
    assert ledger.answers(name) == []
    assert name in ledger.in_state("rejected")


def test_a_superseded_claim_is_ruled_on_without_being_re_asserted(stores, unique):
    """Accept the new reading, reject the old with `decide`: both backends answer the new one."""
    name, memory, ledger = stores
    old = role(name, unique)
    new = role(name, unique, value="marker", source_version="2026_03")
    paired(memory, ledger, (old, {"outcome": "accepted", "judgement": APPROVAL}),
           (new, {"outcome": "accepted", "judgement": APPROVAL}))
    for store in (memory, ledger):
        store.decide(old, outcome="rejected", judgement=REFUSAL)
    agree(memory, ledger, name)
    assert [d.value for d in ledger.answers(name)] == ["marker"]
    assert len(ledger.held(name, history=True)) == 2, "nothing was re-asserted"


def test_two_aliases_coexist_on_the_ledger_and_a_rejection_touches_only_its_own(
        stores, unique):
    name, memory, ledger = stores
    one = role(name, unique, source="pubmed", source_id=f"PMID-{unique}-1",
               source_version="")
    two = role(name, unique, value="marker", source="pubmed",
               source_id=f"PMID-{unique}-2", source_version="")
    paired(memory, ledger, (one, {"outcome": "accepted", "judgement": APPROVAL}),
           (two, {"outcome": "accepted", "judgement": APPROVAL}))
    assert sorted(d.value for d in ledger.answers(name)) == ["cyto_structural", "marker"]
    for store in (memory, ledger):
        store.decide(two, outcome="rejected", judgement=REFUSAL)
    agree(memory, ledger, name)
    assert [d.value for d in ledger.answers(name)] == ["cyto_structural"]


def test_two_aliases_from_one_source_coexist_on_the_ledger(stores, unique):
    """One accession listing two alias spellings holds two slots."""
    name, memory, ledger = stores
    spelled = [MarkerClaim(subject=name, predicate=MarkerPredicate.ALIAS, value=v,
                           source="uniprot", source_id=f"P12830-{unique}",
                           source_version="2026_02", evidence=f"gene synonym: {v}",
                           confidence=0.9) for v in ("CDHE", "UVO")]
    paired(memory, ledger, *((c, {}) for c in spelled), *((c, {}) for c in spelled))
    agree(memory, ledger, name)
    assert sorted(c.value for c in ledger.held(name)) == ["CDHE", "UVO"]
    assert len(ledger.held(name, history=True)) == 2, "a re-seed writes nothing"


def test_a_judgement_on_an_unreviewed_predicate_lands_no_event(stores, unique):
    name, memory, ledger = stores
    with pytest.raises(ValueError, match="not reviewed"):
        ledger.add(location(name, unique), outcome="accepted", judgement=APPROVAL)
    assert ledger.held(name, history=True) == []
    assert ledger.decisions(name) == []


def test_a_wrong_subject_type_is_refused_before_any_event(stores, unique):
    name, memory, ledger = stores
    dye = subject(f"DAPI-{unique}", "Fluorophore")
    with pytest.raises(ValueError, match="is a Fluorophore"):
        ledger.add(role(dye, unique))
    assert ledger.held(dye, history=True) == []


def test_set_state_is_still_the_acknowledged_second_writer(stores, unique):
    name, memory, ledger = stores
    ledger.set_state(name, "unresolved")
    assert name in ledger.in_state("unresolved")
    assert ledger.decisions(name) == [], "no decision was recorded, by design"


def test_events_carry_typed_actors_and_the_read_contract_reads_them_back(stores, unique):
    from clinicalomx_ledger import reads

    name, memory, ledger = stores
    ledger.add(role(name, unique))
    ledger.add(role(name, unique), outcome="accepted", judgement=APPROVAL)

    standing = reads.standing_claims(MARKER_DOMAIN.name, name,
                                     str(MarkerPredicate.SEGMENTATION_ROLE))
    assert [(c.proposed_by, c.actor_kind, c.agent_model, c.agent_model_version)
            for c in standing] == [
        (AGENT["actor"], "agent", AGENT["agent_model"], AGENT["agent_model_version"])]

    trail = reads.decision_trail(MARKER_DOMAIN.name, name)
    assert [(d.by, d.actor_kind, d.outcome) for d in trail] == [
        ("rafael", "human", "accepted")], "the proposal wrote no decision"

    (resolved,) = reads.resolved_state(MARKER_DOMAIN.name, name,
                                       str(MarkerPredicate.SEGMENTATION_ROLE))
    assert (resolved.value, resolved.decided_by) == ("cyto_structural", "rafael")


def test_the_proposers_confidence_reaches_the_stored_row(stores, unique):
    """The proposer's confidence is stored on the row."""
    from clinicalomx_ledger import adapter

    name, memory, ledger = stores
    ledger.add(role(name, unique, confidence=0.65))
    (row,) = adapter.claim_log(MARKER_DOMAIN.name, name)
    assert row.confidence == 0.65
    assert [c.confidence for c in ledger.held(name)] == [0.65]


def test_an_agent_session_records_its_policy_on_every_claim(unique):
    from clinicalomx_ledger import adapter

    name = subject(f"NAKATPASE-{unique}-policy")
    LedgerClaimStore(BRANCH, MARKER_DOMAIN, **AGENT, policy_version="markers-rules-1").add(
        role(name, unique))
    (row,) = adapter.claim_log(MARKER_DOMAIN.name, name)
    assert (row.actor_kind, row.policy_version) == ("agent", "markers-rules-1")


def test_the_mapping_domain_runs_end_to_end_on_the_ledger(unique):
    """Propose, accept and answer in the mapping domain, under its own tag."""
    from clinicalomx_markers.domain.reagent_mapping import (
        REAGENT_MAPPING_DOMAIN,
        MappingClaim,
        ReagentMappingPredicate,
        ReagentMappingSource,
    )

    sheet = subject(f"akoya-panel-{unique}.xlsx", "Artifact")
    store = LedgerClaimStore(BRANCH, REAGENT_MAPPING_DOMAIN, **AGENT)
    mapped = MappingClaim(subject=sheet, predicate=ReagentMappingPredicate.RX_BARCODE,
                          value="Reporter Barcode", source=ReagentMappingSource.SPREADSHEET,
                          source_id="sheet1!C1", confidence=0.9,
                          evidence="header 'Reporter Barcode'; 28/28 values match RX[0-9]{3}")
    store.add(mapped)
    assert sheet in store.in_state("unresolved")
    store.add(mapped, outcome="accepted", judgement=APPROVAL)

    (answer,) = store.answers(sheet)
    assert (answer.predicate, answer.value, answer.by) == (
        "rx_barcode", "Reporter Barcode", "rafael")
    assert sheet in store.in_state("accepted")
    assert [c.confidence for c in store.held(sheet)] == [0.9]
    assert LedgerClaimStore(BRANCH, MARKER_DOMAIN, **AGENT).answers(sheet) == [], (
        "and the marker domain does not see it")


def test_the_human_and_agent_paths_differ_only_in_actor_typing(unique):
    """The same assertion from a human session and an agent session differs only in the four
    actor columns."""
    from clinicalomx_ledger import adapter

    said_by = {}
    for who, session in (("agent", AGENT), ("human", HUMAN)):
        name = subject(f"NAKATPASE-{unique}-{who}")
        LedgerClaimStore(BRANCH, MARKER_DOMAIN, **session).add(role(name, unique))
        (said_by[who],) = adapter.claim_log(MARKER_DOMAIN.name, name)

    typing = ("proposed_by", "actor_kind", "agent_model", "agent_model_version")
    identifying = ("uid", "subject", "created_at")
    shared = [f for f in type(said_by["agent"]).__dataclass_fields__
              if f not in typing + identifying]
    assert shared, "the row is all actor typing, so this proves nothing"
    for field in shared:
        assert getattr(said_by["agent"], field) == getattr(said_by["human"], field), field

    assert [(said_by[w].actor_kind, said_by[w].agent_model) for w in ("agent", "human")] == [
        ("agent", AGENT["agent_model"]), ("human", "")]


def test_a_tracked_session_cites_its_run(stores, unique):
    from clinicalomx_ledger import reads

    from clinicalomx_markers.curate import session

    name, memory, ledger = stores
    claim = role(name, unique)
    run = session.track(branch=BRANCH)
    try:
        ledger.add(claim)
    finally:
        ln.context._run = None

    told = ledger.lineage(claim)
    assert told is not None and told.run_uid == run.uid
    cited = reads.by_run(run.uid)
    assert {c.subject for c in cited} == {name}
    assert len(cited) == 1, "the claim cites the run, and a proposal writes no decision"

    untracked = role(subject(f"NAKATPASE-{unique}-notrack"), f"{unique}n")
    ledger.add(untracked)
    assert ledger.lineage(untracked) is None


def test_the_second_domain_would_need_its_own_tag():
    """The marker store reads only marker-tagged events."""
    from clinicalomx_ledger import adapter

    marker_store = LedgerClaimStore(BRANCH, MARKER_DOMAIN, **AGENT)
    tags = {v.domain for v in adapter.invalidation_log("marker")}
    assert tags <= {"marker"}
    assert all(str(v.source) in ("uniprot", "pubmed", "url", "human")
               for v in marker_store.invalidations())
