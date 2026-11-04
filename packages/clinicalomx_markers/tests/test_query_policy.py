"""Trust policy on the read side.

`settled` holds claims whose newest decision accepts them. Rejected and undecided claims are
never served. `asserted` reads what sources say, decided or not.
"""
from __future__ import annotations

from clinicalomx_ledger.grammar.gate import Judgement
from clinicalomx_ledger.grammar.store import MemoryClaimStore

from clinicalomx_markers.domain.marker import (
    MARKER_DOMAIN,
    MarkerClaim,
    MarkerDecision,
    MarkerPredicate,
)
from clinicalomx_markers.query import claims
from clinicalomx_markers.query.claims import (
    asserted,
    settled,
    supporting,
    value_of,
)

ROLE = MarkerPredicate.SEGMENTATION_ROLE
SIGNED = Judgement(by="rafael", at="2026-08-25")


def claim(value: str, **over) -> MarkerClaim:
    base = {
        "subject": "NAKATPASE",
        "predicate": MarkerPredicate.SUBCELLULAR_LOCATION,
        "value": value,
        "source": "uniprot",
        "source_id": "P05023",
        "evidence": "verbatim",
    }
    base.update(over)
    return MarkerClaim(**base)


def role(value: str, **over) -> MarkerClaim:
    return claim(value, predicate=ROLE, **over)


def store() -> MemoryClaimStore:
    """One accepted role, one undecided rival, one rejected rival, and the localisation."""
    s = MemoryClaimStore(MARKER_DOMAIN)
    s.add(claim("Cell membrane"))
    s.add(role("cyto_structural"), outcome="accepted", judgement=SIGNED)
    s.add(role("marker", source="pubmed", source_id="PMID:1"))
    s.add(role("nucleus", source="url", source_id="https://x"), outcome="rejected",
          judgement=SIGNED)
    return s


def test_only_accepted_claims_are_settled():
    got = settled(store().answers("NAKATPASE"), "NAKATPASE")
    assert [d.value for d in got] == ["cyto_structural"]


def test_an_undecided_claim_is_never_served():
    s = store()
    assert "marker" in [c.value for c in s.held("NAKATPASE", predicate=ROLE)]
    assert "marker" not in [d.value for d in s.answers("NAKATPASE")]


def test_rejected_never_leaks():
    assert all(d.outcome == "accepted" for d in store().answers("NAKATPASE"))


def test_a_correction_is_the_newest_accepted_decision():
    """A later acceptance corrects without editing; the older row stays readable."""
    s = store()
    s.decide(role("marker", source="pubmed", source_id="PMID:1"), outcome="accepted",
             judgement=Judgement(by="rafael", at="2026-08-26"))

    assert value_of(s.answers("NAKATPASE"), "NAKATPASE", ROLE).value == "marker"
    assert len(s.decisions("NAKATPASE")) == 3, "the decision it corrects is still there"


def test_a_rejection_leaves_a_machine_consumer_without_that_answer():
    """Rejecting the only accepted claim leaves no answer; nothing older falls back in."""
    s = store()
    s.decide(role("cyto_structural"), outcome="rejected",
             judgement=Judgement(by="rafael", at="2026-08-26", kind="one_off"))

    assert value_of(s.answers("NAKATPASE"), "NAKATPASE", ROLE) is None


def test_value_of_is_none_when_nothing_qualifies():
    assert value_of(store().answers("UNKNOWN"), "UNKNOWN", ROLE) is None


def test_what_a_source_says_is_read_from_the_claims_and_not_from_a_decision():
    """An unreviewed predicate such as localisation is read through `asserted`."""
    s = store()
    got = asserted(s.held("NAKATPASE"), "NAKATPASE")[MarkerPredicate.SUBCELLULAR_LOCATION]
    assert [c.value for c in got] == ["Cell membrane"]
    assert settled(s.answers("NAKATPASE"), "NAKATPASE",
                   predicate=MarkerPredicate.SUBCELLULAR_LOCATION) == []


def test_a_claim_carries_no_status_for_a_policy_to_read():
    assert "status" not in MarkerClaim.model_fields
    assert "outcome" in MarkerDecision.model_fields


def test_citation_line_states_the_trust_level():
    s = store()
    history = s.held("NAKATPASE", history=True)
    accepted, rejected = s.decisions("NAKATPASE")
    ok = claims.line(accepted, supporting(accepted, history))
    bad = claims.line(rejected, supporting(rejected, history))
    assert "accepted" in ok and "UNVALIDATED" not in ok
    assert "REJECTED UNVALIDATED" in bad
    assert "url https://x" in bad, "the source travels with the decision on it"


def test_a_citation_names_the_human_who_decided_it():
    s = store()
    (accepted,) = s.answers("NAKATPASE")
    rendered = claims.line(accepted, supporting(accepted, s.held("NAKATPASE", history=True)))
    assert "rafael" in rendered and "2026-08-25" in rendered


def test_an_undecided_claim_renders_as_undecided():
    rendered = claims.claim_line(claim("Cell membrane"))
    assert "UNDECIDED" in rendered
    assert "uniprot P05023" in rendered


def test_citation_marker_is_the_claim_id():
    c = claim("Cell membrane")
    assert claims.marker(c).strip("[]") == c.claim_id()


def test_two_agreeing_sources_are_cited_by_their_own_decisions():
    s = MemoryClaimStore(MARKER_DOMAIN)
    sheet = role("cyto_structural", source="uniprot", source_id="P05023")
    header = role("cyto_structural", source="pubmed", source_id="PMID:9")
    for agreeing in (sheet, header):
        s.add(agreeing, outcome="accepted", judgement=SIGNED)

    rendered = claims.block(s.answers("NAKATPASE"), s.held("NAKATPASE", history=True))
    assert rendered.count("supported by") == 2
    assert "uniprot P05023" in rendered and "pubmed PMID:9" in rendered


def test_rival_sources_are_all_asserted_and_none_is_picked():
    """`asserted` returns every rival, without picking one."""
    said = asserted(store().held("NAKATPASE"), "NAKATPASE")[ROLE]
    assert sorted(c.value for c in said) == ["cyto_structural", "marker", "nucleus"]


def test_rivals_come_newest_first_by_creation_order_alone():
    """Rivals are newest first by creation order alone."""
    forward = MemoryClaimStore(MARKER_DOMAIN)
    backward = MemoryClaimStore(MARKER_DOMAIN)
    rivals = [role("marker", source="pubmed", source_id="PMID:1"),
              role("cyto_structural"),
              role("nucleus", source="url", source_id="https://x")]
    for c in rivals:
        forward.add(c)
    for c in reversed(rivals):
        backward.add(c)

    assert [c.value for c in asserted(forward.held("NAKATPASE"), "NAKATPASE")[ROLE]] == [
        "nucleus", "cyto_structural", "marker"]
    assert [c.value for c in asserted(backward.held("NAKATPASE"), "NAKATPASE")[ROLE]] == [
        "marker", "cyto_structural", "nucleus"]

    forward.add(role("marker", source="pubmed", source_id="PMID:1", source_version="2"))
    assert asserted(forward.held("NAKATPASE"), "NAKATPASE")[ROLE][0].source == "pubmed", (
        "a source restating itself is the newest statement")


def test_sources_that_agree_are_both_returned():
    s = MemoryClaimStore(MARKER_DOMAIN)
    s.add(role("cyto_structural"))
    s.add(role("cyto_structural", source="pubmed", source_id="PMID:1"))

    said = asserted(s.held("NAKATPASE"), "NAKATPASE")[ROLE]
    assert [c.value for c in said] == ["cyto_structural", "cyto_structural"]
    assert [c.source for c in said] == ["pubmed", "uniprot"], "both sources are still cited"


def test_asserted_drops_no_claim_the_projection_holds():
    """Each standing claim is in exactly one predicate's tuple; unasserted ones are absent."""
    s = store()
    held = s.held("NAKATPASE")
    grouped = asserted(held, "NAKATPASE")
    assert sorted(map(id, [c for said in grouped.values() for c in said])) == sorted(
        map(id, held))
    assert all(grouped.values())
    assert MarkerPredicate.SUBCELLULAR_LOCATION in grouped
