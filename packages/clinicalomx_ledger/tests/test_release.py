"""A release pins the decisions in effect; later decisions do not move it. Subjects are REL-*."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from clinicalomx_ledger import services
from clinicalomx_ledger.grammar import published
from conftest import AGENT, HUMAN

DOMAIN = "rel_domain"


def claim(subject: str, value: str):
    return services.create_claim(
        domain=DOMAIN, subject=subject, predicate="segmentation_role", value=value,
        source="uniprot", source_id=f"{subject}-src", evidence="synthetic: release fixture",
        proposed_by="synthetic-agent", **AGENT,
    )


def rule(target, outcome: str, day: int):
    return services.decide(claim=target, outcome=outcome, by="synthetic-human",
                           decided_at=datetime(2026, 9, day, tzinfo=UTC), **HUMAN)


@pytest.fixture(scope="module")
def cut():
    kept, overruled = claim("REL-CD3E", "marker"), claim("REL-CD8A", "marker")
    rule(kept, "accepted", 1)
    rule(overruled, "accepted", 1)
    claim("REL-CD4", "marker")  # undecided, so not in effect and not pinned
    release = services.cut_release(domain=DOMAIN, by="synthetic-human",
                                   policy_version="REL-policy-1", **HUMAN)
    return release, overruled


def test_a_release_pins_exactly_the_decisions_in_effect(cut):
    release, _ = cut
    pinned = services.release_contents(release.uid)
    assert sorted(d.claim.subject for d in pinned) == ["REL-CD3E", "REL-CD8A"]
    assert [d.uid for d in pinned] == [d.uid for d in services.answers(DOMAIN)]
    assert release.manifest_hash == services.manifest_hash(d.uid for d in pinned)


def test_a_later_decision_does_not_change_what_the_release_contains(cut):
    release, overruled = cut
    before = [d.uid for d in services.release_contents(release.uid)]
    rule(overruled, "rejected", 2)
    rule(claim("REL-CD4", "marker"), "accepted", 2)
    assert sorted(d.claim.subject for d in services.answers(DOMAIN)) == [
        "REL-CD3E", "REL-CD4"
    ], "the live answer moved"
    after = services.release_contents(release.uid)
    assert [d.uid for d in after] == before, "the release did not"
    assert services.manifest_hash(d.uid for d in after) == release.manifest_hash


def test_a_second_release_pins_the_answer_as_it_is_now(cut):
    first, _ = cut
    second = services.cut_release(domain=DOMAIN, by="synthetic-human", **HUMAN)
    assert second.manifest_hash != first.manifest_hash
    assert sorted(d.claim.subject for d in services.release_contents(second.uid)) == [
        "REL-CD3E", "REL-CD4"
    ]


EXPORT = "exp_domain"


def exp_claim(subject: str, value: str):
    return services.create_claim(
        domain=EXPORT, subject=subject, predicate="segmentation_role", value=value,
        source="uniprot", source_id=f"{subject}-src", evidence="synthetic: export fixture",
        proposed_by="synthetic-agent", **AGENT,
    )


@pytest.fixture(scope="module")
def exported():
    """One accepted, one rejected after acceptance, one undecided; then the release."""
    kept, refused = exp_claim("EXP-CD3E", "marker"), exp_claim("EXP-CD8A", "marker")
    rule(kept, "accepted", 1)
    rule(refused, "accepted", 1)
    rule(refused, "rejected", 2)
    exp_claim("EXP-CD4", "marker")
    release = services.cut_release(domain=EXPORT, by="synthetic-human",
                                   policy_version="EXP-policy-1", **HUMAN)
    return release, services.export_release(release.uid)


def test_an_export_holds_only_the_accepted_answers(exported):
    _, raw = exported
    assert [a.subject for a in published.load(raw).answers] == ["EXP-CD3E"]


def test_an_export_names_the_release_that_produced_it(exported):
    release, raw = exported
    got = published.load(raw)
    assert (got.release_uid, got.domain, got.manifest_hash, got.policy_version) == (
        release.uid, EXPORT, release.manifest_hash, "EXP-policy-1")
    assert got.released_by == "synthetic-human" and got.released_at


def test_exporting_one_release_twice_gives_the_same_bytes(exported):
    release, raw = exported
    assert services.export_release(release.uid) == raw


def test_a_decision_after_the_release_does_not_change_its_export(exported):
    release, raw = exported
    rule(exp_claim("EXP-CD4", "marker"), "accepted", 3)
    rule(exp_claim("EXP-CD8A", "marker"), "accepted", 3)
    assert {d.claim.subject for d in services.answers(EXPORT)} == {
        "EXP-CD3E", "EXP-CD4", "EXP-CD8A"}, "the live answer moved"
    assert services.export_release(release.uid) == raw, "the export did not"


def test_an_edited_export_is_refused(exported):
    _, raw = exported
    edited = raw.replace(b"EXP-CD3E", b"EXP-CD4").replace(
        published.load(raw).answers[0].decision_uid.encode(), b"dec-forged")
    with pytest.raises(ValueError, match="not that release"):
        published.load(edited)
