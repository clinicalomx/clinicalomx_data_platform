"""Segmentation over a release cut on a real ledger.

Seed channel roles are accepted beside one rejected and one undecided rival. The exported
release must give the CSV's results, and a later decision must not reach it.
"""
from __future__ import annotations

from clinicalomx_ledger import services
from clinicalomx_ledger.grammar.gate import Judgement
from clinicalomx_ledger.grammar.published import load
from clinicalomx_ledger.grammar.store import LedgerClaimStore
from tests.test_segmentation import PANEL, from_the_csv

from clinicalomx_markers.domain.marker import MARKER_DOMAIN, MarkerClaim, MarkerPredicate
from clinicalomx_markers.query.segmentation import candidates
from clinicalomx_markers.resolve import resolve
from clinicalomx_markers.seed_data.rows import channels
from conftest import BRANCH, HUMAN, subject

SIGNED = Judgement(by="rafael", at="2026-09-25")


def role(marker: str, value: str, source_id: str = "rafael") -> MarkerClaim:
    return MarkerClaim(subject=marker, predicate=MarkerPredicate.SEGMENTATION_ROLE,
                       value=value, source="human", source_id=source_id,
                       evidence=f"{marker} is {value} on PCF")


def test_a_release_export_serves_segmentation_what_the_csv_did():
    store = LedgerClaimStore(BRANCH, MARKER_DOMAIN, **HUMAN)
    for row in channels():
        marker = subject(row["canonical_name"])
        store.add(role(marker, row["segmentation_role"]), outcome="accepted", judgement=SIGNED,
                  subject_row={"canonical_name": marker, "compartment": row["compartment"]})
    rival = role("VIMENTIN", "marker", source_id="wenchao")
    store.add(rival, outcome="accepted", judgement=SIGNED)
    store.decide(rival, outcome="rejected",
                 judgement=Judgement(by="rafael", at="2026-09-25", kind="one_off"))
    store.add(role("LDHA", "cyto_structural", source_id="wenchao"))

    release = services.cut_release(domain=MARKER_DOMAIN.name, by="rafael",
                                   actor_kind="human")
    raw = services.export_release(release.uid)
    pinned = load(raw)
    assert pinned.release_uid == release.uid
    served = {(a.subject, a.value) for a in pinned.answers}
    assert ("VIMENTIN", "marker") not in served, "a rejected claim was exported"
    assert ("LDHA", "cyto_structural") not in served, "an undecided claim was exported"
    assert candidates(PANEL, pinned) == from_the_csv(PANEL)

    store.decide(role("NAKATPASE", "cyto_structural"), outcome="rejected",
                 judgement=Judgement(by="rafael", at="2026-09-26", kind="one_off"))
    assert services.export_release(release.uid) == raw
    assert candidates(PANEL, load(raw)) == from_the_csv(PANEL)


def spelled(marker: str, spelling: str) -> MarkerClaim:
    return MarkerClaim(subject=marker, predicate=MarkerPredicate.ALIAS, value=spelling,
                       source="human", source_id="rafael", evidence=f"{spelling} names {marker}")


def test_a_release_export_resolves_the_aliases_it_approved_and_no_others(unique):
    """Accepted resolves; rejected, undecided and accepted-after-the-release do not."""
    store = LedgerClaimStore(BRANCH, MARKER_DOMAIN, **HUMAN)
    marker = subject("E-CADHERIN")
    accepted, rejected, undecided, later = (f"{name}{unique}"
                                            for name in ("Ecad", "Uvo", "Cdhe", "Late"))
    store.add(spelled(marker, accepted), outcome="accepted", judgement=SIGNED)
    store.add(spelled(marker, rejected), outcome="accepted", judgement=SIGNED)
    store.decide(spelled(marker, rejected), outcome="rejected",
                 judgement=Judgement(by="rafael", at="2026-09-25", kind="one_off"))
    store.add(spelled(marker, undecided))

    pinned = load(services.export_release(
        services.cut_release(domain=MARKER_DOMAIN.name, by="rafael", actor_kind="human").uid))
    store.add(spelled(marker, later), outcome="accepted", judgement=SIGNED)

    got = resolve(accepted, pinned)
    assert (got.canonical, got.via) == (marker, "alias")
    for spelling in (rejected, undecided, later):
        assert not resolve(spelling, pinned).resolved, f"{spelling} resolved from the export"
