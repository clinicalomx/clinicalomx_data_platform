"""The answer and the queue are reads over append-only rows. Subjects are namespaced ANS-*."""

from __future__ import annotations

from datetime import UTC, datetime

from clinicalomx_ledger import reads, services
from clinicalomx_ledger.models import Claim
from conftest import AGENT, HUMAN

DOMAIN = "marker"


def day(n: int) -> datetime:
    return datetime(2026, 9, n, tzinfo=UTC)


def claim(subject: str, value: str, *, predicate: str = "segmentation_role",
          source: str = "uniprot", source_id: str = "ANS-P1", source_version: str = "",
          confidence: float | None = None, multi_valued: tuple[str, ...] = ()) -> Claim:
    return services.create_claim(
        domain=DOMAIN, subject=subject, predicate=predicate, value=value, source=source,
        source_id=source_id, source_version=source_version,
        evidence=f"synthetic: {subject} {value}", proposed_by="synthetic-agent",
        confidence=confidence, multi_valued=multi_valued, **AGENT,
    )


def rule(target: Claim, outcome: str, on: int, reason: str = "synthetic"):
    return services.decide(claim=target, outcome=outcome, by="synthetic-human",
                           decided_at=day(on), reason=reason, **HUMAN)


def served(subject: str, predicate: str = "segmentation_role") -> list[str]:
    return [a.value for a in reads.resolved_state(DOMAIN, subject, predicate)]


def test_a_claim_nobody_decided_is_in_the_queue_and_is_not_the_answer():
    fresh = claim("ANS-queued", "marker")
    assert served("ANS-queued") == []
    assert fresh in services.queue(DOMAIN)
    rule(fresh, "accepted", 1)
    assert served("ANS-queued") == ["marker"]
    assert fresh not in services.queue(DOMAIN)


def test_a_rejection_stops_the_accepted_answer_being_served():
    wrong = claim("ANS-reject", "cyto_structural")
    rule(wrong, "accepted", 1)
    assert served("ANS-reject") == ["cyto_structural"]
    rule(wrong, "rejected", 2, reason="synthetic: it is a lineage marker")
    assert served("ANS-reject") == []
    assert [d.outcome for d in reads.decision_trail(DOMAIN, "ANS-reject")] == [
        "accepted", "rejected"
    ], "the acceptance is still on the record, overruled rather than removed"


def test_a_correction_appends_and_the_newest_decision_wins():
    """A re-read at a new release is a new claim; the old answer stands until someone rules."""
    old = claim("ANS-correct", "marker", source_version="2026_01")
    rule(old, "accepted", 1)
    moved = claim("ANS-correct", "cyto_structural", source_version="2026_03")
    assert moved.pk != old.pk, "the new release is appended, not refused"
    assert served("ANS-correct") == ["marker"], "a reading is not a ruling"

    rule(moved, "accepted", 2, reason="synthetic: the new release is right")
    rule(old, "rejected", 2, reason="synthetic: superseded by the 2026_03 reading")
    assert served("ANS-correct") == ["cyto_structural"]
    assert Claim.objects.get(pk=old.pk).value == "marker", "nothing was edited"


MULTI = ("alias",)


def test_two_aliases_for_one_subject_coexist_and_a_rejection_touches_only_its_own():
    """On a multi-valued predicate, two values from one source both stand."""
    cdhe = claim("ANS-CDH1", "CDHE", predicate="alias", source_id="ANS-P12830",
                 multi_valued=MULTI)
    uvo = claim("ANS-CDH1", "UVO", predicate="alias", source_id="ANS-P12830",
                multi_valued=MULTI)
    assert {c.value for c in services.standing(DOMAIN, "ANS-CDH1", "alias",
                                               multi_valued=MULTI)} == {"CDHE", "UVO"}
    queued = {c.value for c in services.queue(DOMAIN, multi_valued=MULTI)
              if c.subject == "ANS-CDH1"}
    assert queued == {"CDHE", "UVO"}
    again = claim("ANS-CDH1", "CDHE", predicate="alias", source_id="ANS-P12830",
                  multi_valued=MULTI)
    assert again.pk == cdhe.pk, "re-reading a sibling that still stands writes nothing"
    rule(cdhe, "accepted", 1)
    rule(uvo, "accepted", 1)
    assert sorted(served("ANS-CDH1", "alias")) == ["CDHE", "UVO"]
    rule(uvo, "rejected", 2, reason="synthetic: not a synonym in this lab")
    assert served("ANS-CDH1", "alias") == ["CDHE"]


def test_a_single_valued_predicate_still_supersedes_per_source():
    first = claim("ANS-single", "marker", multi_valued=MULTI)
    second = claim("ANS-single", "cyto_structural", multi_valued=MULTI)
    standing = services.standing(DOMAIN, "ANS-single", "segmentation_role",
                                 multi_valued=MULTI)
    assert [c.pk for c in standing] == [second.pk]
    back = claim("ANS-single", "marker", multi_valued=MULTI)
    assert back.pk not in (first.pk, second.pk)


def test_a_voided_source_is_not_the_answer_however_it_was_ruled():
    ruled = claim("ANS-void", "marker", source="url", source_id="ANS-void-src")
    rule(ruled, "accepted", 1)
    services.invalidate_source(domain=DOMAIN, source="url", source_id="ANS-void-src",
                               reason="synthetic: bad extraction", actor="synthetic-human",
                               **HUMAN)
    assert served("ANS-void") == []


def test_the_same_ruling_twice_is_one_row_and_a_later_stated_date_is_two():
    target = claim("ANS-repeat", "marker")
    assert rule(target, "accepted", 1) is not None
    assert rule(target, "accepted", 1) is None
    assert rule(target, "accepted", 2) is not None, "a re-affirmation is a second decision"


def test_a_reviewer_sorts_the_queue_by_the_confidence_stored_on_the_claim():
    for subject, confidence in (("ANS-q-low", 0.2), ("ANS-q-none", None),
                                ("ANS-q-high", 0.9), ("ANS-q-mid", 0.5)):
        claim(subject, "marker", confidence=confidence)
    order = [c.subject for c in services.queue(DOMAIN) if c.subject.startswith("ANS-q-")]
    assert order == ["ANS-q-high", "ANS-q-mid", "ANS-q-low", "ANS-q-none"]
    stored = sorted(Claim.objects.filter(subject__startswith="ANS-q-",
                                         confidence__isnull=False),
                    key=lambda c: -c.confidence)
    assert [c.confidence for c in stored] == [0.9, 0.5, 0.2]


def test_confidence_is_not_part_of_what_makes_a_repeat():
    first = claim("ANS-conf", "marker", confidence=0.4)
    again = claim("ANS-conf", "marker", confidence=0.8)
    assert again.pk == first.pk
