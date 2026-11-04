"""What a run says about a claim, and a claim with no run. Offline, no database.

`MemoryClaimStore` has no runs, so most tests here are the no-run case.
"""
from __future__ import annotations

from dataclasses import fields

from clinicalomx_ledger.grammar import lineage
from clinicalomx_ledger.grammar.gate import Judgement
from clinicalomx_ledger.grammar.store import ClaimStore, MemoryClaimStore

from clinicalomx_markers.domain.marker import MARKER_DOMAIN, MarkerClaim, MarkerPredicate
from clinicalomx_markers.query.claims import asserted, block, claim_line, value_of


def human_claim(**over) -> MarkerClaim:
    """DAPI's localisation, asserted by a person."""
    base = {
        "subject": "DAPI",
        "predicate": MarkerPredicate.SUBCELLULAR_LOCATION,
        "value": "Nucleus",
        "source": "human",
        "source_id": "rafael",
        "evidence": "DAPI binds the minor groove of double-stranded DNA.",
    }
    base.update(over)
    return MarkerClaim(**base)


def store() -> MemoryClaimStore:
    return MemoryClaimStore(MARKER_DOMAIN)


class StubRun:
    """A run's shape, without lamindb."""

    uid = "Xk3fQ2p9wLmz"
    started_at = "2026-08-19 04:11:02"


# --- the claim with no run, end to end --------------------------------------------------

def test_a_claim_with_no_run_survives_the_whole_round_trip():
    """Written, read back through `asserted`, and cited, with no run anywhere."""
    claims = store()

    written = claims.add(human_claim())

    read_back = claims.held("DAPI")
    assert read_back == [written]
    said = asserted(read_back, "DAPI")[MarkerPredicate.SUBCELLULAR_LOCATION]
    assert said == (written,)
    assert claims.decisions("DAPI") == [], "an unreviewed predicate takes no decision"

    assert claims.lineage(written) is None

    rendered = claim_line(written, claims.lineage(written))
    assert "DAPI subcellular_location = 'Nucleus'" in rendered
    assert "UNDECIDED" in rendered
    assert "human rafael" in rendered
    assert "run " not in rendered, "a claim with no run must not render an empty run clause"


def test_a_claim_with_no_run_is_still_validated_and_queued_normally():
    claims = store()

    decided = Judgement(by="rafael", at="2026-08-24")
    claims.add(human_claim(predicate=MarkerPredicate.SEGMENTATION_ROLE, value="nucleus"),
               outcome="accepted", judgement=decided)
    corrected = claims.add(
        human_claim(predicate=MarkerPredicate.SEGMENTATION_ROLE, value="marker"),
        outcome="accepted", judgement=decided)

    assert claims.in_state("accepted") == ["DAPI"]
    assert value_of(claims.answers("DAPI"), "DAPI",
                    MarkerPredicate.SEGMENTATION_ROLE).value == corrected.value
    assert claims.lineage(corrected) is None


def test_a_block_of_decisions_with_no_runs_renders():
    """`block` renders from history without lineage."""
    claims = store()
    judged = Judgement(by="rafael", at="2026-08-24")
    for value in ("nucleus", "marker"):
        claims.add(human_claim(predicate=MarkerPredicate.SEGMENTATION_ROLE, value=value),
                   outcome="accepted", judgement=judged)

    rendered = block(claims.decisions("DAPI"), claims.held("DAPI", history=True))

    assert rendered.count("[DAPI:segmentation_role:human:rafael:") == 2
    assert "run " not in rendered


def test_a_block_asks_per_claim_rather_than_keying_on_the_claim_id():
    """Each cited claim gets its own run: two claims can share a `claim_id`."""
    from_uniprot = human_claim(source="uniprot", source_id="P05023")
    from_person = human_claim()
    assert from_uniprot.claim_id() != from_person.claim_id()

    def lineage_of(claim: MarkerClaim):
        return lineage.of(StubRun()) if claim.source == "uniprot" else None

    decisions = [MARKER_DOMAIN.decision(
        subject="DAPI", predicate=MarkerPredicate.SUBCELLULAR_LOCATION, value="Nucleus",
        claim_id=c.claim_id(), outcome="accepted", by="rafael", at="2026-08-24",
    ) for c in (from_uniprot, from_person)]
    rendered = block(decisions, [from_uniprot, from_person], lineage_of)

    cited = [line for line in rendered.splitlines() if "supported by" in line]
    assert len(cited) == 2
    assert "run Xk3fQ2p9wLmz" in next(line for line in cited if "uniprot" in line)
    assert "run " not in next(line for line in cited if "human" in line)


def test_two_claims_sharing_a_claim_id_are_cited_once():
    """Two claims sharing a `claim_id` cite as one: `evidence` is outside the id."""
    from clinicalomx_markers.query.claims import supporting

    terse = human_claim(evidence="Nucleus.")
    verbose = human_claim(evidence="SUBCELLULAR LOCATION: Nucleus. Binds dsDNA.")
    assert terse.claim_id() == verbose.claim_id() and terse != verbose

    decided = MARKER_DOMAIN.decision(
        subject="DAPI", predicate=MarkerPredicate.SUBCELLULAR_LOCATION, value="Nucleus",
        claim_id=terse.claim_id(), outcome="accepted", by="rafael", at="2026-08-24",
    )
    assert len(supporting(decided, [terse, verbose])) == 1


def test_a_block_takes_the_store_lookup_itself():
    claims = store()
    claims.add(human_claim(predicate=MarkerPredicate.SEGMENTATION_ROLE, value="nucleus"),
               outcome="accepted", judgement=Judgement(by="rafael", at="2026-08-24"))

    assert "run " not in block(claims.decisions("DAPI"), claims.held("DAPI", history=True),
                               claims.lineage)


# --- reading a run ----------------------------------------------------------------------

def test_there_is_no_lineage_without_a_run():
    assert lineage.of(None) is None


def test_a_run_gives_up_what_two_of_the_removed_fields_used_to_hold():
    """A run gives the run uid and start time, and nothing else."""
    got = lineage.of(StubRun())

    assert got.run_uid == "Xk3fQ2p9wLmz"
    assert got.started_at == "2026-08-19 04:11:02"


def test_a_lineage_offers_no_snapshot_pointer():
    """A lineage has no snapshot pointer: a snapshot is a file, not a run input."""
    assert {f.name for f in fields(lineage.Lineage)} == {"run_uid", "started_at"}


def test_a_run_with_nothing_on_it_still_reads():
    assert lineage.of(object()) == lineage.Lineage()


def test_a_citation_carries_the_run_when_there_is_one():
    rendered = claim_line(human_claim(), lineage.of(StubRun()))

    assert "run Xk3fQ2p9wLmz" in rendered
    assert "started 2026-08-19 04:11:02" in rendered
    assert "from " not in rendered, "a citation cannot point at bytes a run does not name"


# --- the two backends -------------------------------------------------------------------

def test_the_memory_backend_is_permanently_the_no_run_case():
    claims = store()
    written = claims.add(human_claim())

    assert claims.lineage(written) is None
    assert claims.lineage(human_claim(value="never written")) is None


def test_lineage_is_on_the_protocol_so_both_backends_answer_it():
    assert hasattr(ClaimStore, "lineage")
    assert isinstance(store(), ClaimStore)


def test_the_claim_holds_no_run_and_no_lamindb_reaches_it():
    held = set(MarkerClaim.model_fields)
    assert held == {
        "subject", "predicate", "value",
        "source", "source_id", "source_version", "evidence", "confidence",
    }
    assert not held & {"retrieved_at", "source_artifact_key", "attempt", "run", "run_uid"}
    assert not held & {"verdict_kind", "verdict_reason", "verdict_by", "verdict_at"}, \
        "what was decided is the Decision record's, and holding it here as well is the same "\
        "defect as holding the run's facts here"
    assert "status" not in held, \
        "a status is what the lab decided, which is the Decision's and not the claim's"
