"""The curation entry point, end to end offline. Only the human's answer and the clock are
supplied. The run half is `tests_pg/test_curate_session.py`."""
from __future__ import annotations

import pytest
from clinicalomx_ledger.grammar.published import Answer, Published, manifest_hash, render
from clinicalomx_ledger.grammar.store import MemoryClaimStore
from tests.conftest import ACCESSIONS

from clinicalomx_markers.curate import session
from clinicalomx_markers.domain.marker import MARKER_DOMAIN, MarkerPredicate
from clinicalomx_markers.propose.schema import Attention, Proposal, Triage
from clinicalomx_markers.query.claims import settled, value_of
from clinicalomx_markers.seed_spec import SegmentationRole

ROLE = MarkerPredicate.SEGMENTATION_ROLE


def write_export(directory, uid, released_at, aliases, domain=MARKER_DOMAIN.name):
    """Write a ``marker-*`` release export holding ``aliases``."""
    answers = tuple(
        Answer(subject=marker, predicate=MarkerPredicate.ALIAS, value=spelling, source="human",
               source_id="rafael", source_version="", evidence="golden set",
               claim_uid=f"clm-{uid}-{n}", decision_uid=f"dec-{uid}-{n}", decided_by="rafael",
               decided_at=released_at)
        for n, (marker, spelling) in enumerate(aliases))
    path = directory / f"marker-{uid}.json"
    digest = manifest_hash(a.decision_uid for a in answers)
    path.write_bytes(render(Published(
        release_uid=uid, domain=domain, manifest_hash=digest,
        released_at=released_at, released_by="rafael", policy_version="", answers=answers)))
    return path


@pytest.fixture
def store():
    return MemoryClaimStore(MARKER_DOMAIN)


@pytest.fixture
def said():
    return []


def curate(raw, *, store, snapshot_root, transport, said, answer, today="2026-09-25",
           proposer=None):
    return session.curate(raw, store=store, snapshots=snapshot_root, by="rafael",
                          ask=lambda prompt: answer, today=today, fetch=transport,
                          proposer=proposer, say=said.append)


class ProposesCytoStructural:
    """A real proposer that consults no rule, so only the store's policy can refuse."""

    def propose(self, request):
        return [Proposal(predicate=ROLE, value=SegmentationRole.CYTO_STRUCTURAL,
                         confidence=0.9, triage=Triage(Attention.REVIEW, "test"),
                         reasoning="ignores the veto")]


def test_a_source_reaches_the_approved_state_the_export_reads(
        store, snapshot_root, transport, said):
    """Vendor spelling -> UniProt snapshot -> proposal -> human accepts -> answer."""
    claim = curate("NakATPase", store=store, snapshot_root=snapshot_root,
                   transport=transport, said=said, answer="")

    assert claim.subject == "NAKATPASE"
    assert claim.source_id == ACCESSIONS["NAKATPASE"]
    assert claim.confidence > 0, "the proposer's confidence is stored on the claim"
    answer = value_of(store.answers("NAKATPASE"), "NAKATPASE", ROLE)
    assert answer is not None and answer.value == claim.value == "cyto_structural"
    assert answer.by == "rafael" and answer.at == "2026-09-25"
    assert store.in_state("accepted") == ["NAKATPASE"]
    assert store.in_state("unresolved") == []
    assert store.lineage(claim) is None, "no run offline, which is the ordinary case"


def test_later_leaves_a_reviewable_claim_in_the_queue(store, snapshot_root, transport, said):
    claim = curate("NakATPase", store=store, snapshot_root=snapshot_root,
                   transport=transport, said=said, answer=session.LATER)

    assert store.held("NAKATPASE", predicate=ROLE) == [claim]
    assert store.decisions("NAKATPASE") == []
    assert store.in_state("unresolved") == ["NAKATPASE"]
    assert settled(store.answers("NAKATPASE"), "NAKATPASE") == []


def test_a_restated_rejection_is_one_standing_rejection(store, snapshot_root, transport, said):
    for day, reason in (("2026-09-25", "outlines epithelium only"),
                        ("2026-09-30", "still outlines epithelium only")):
        curate("NakATPase", store=store, snapshot_root=snapshot_root, transport=transport,
               said=said, answer=reason, today=day)

    (rejection,) = store.decisions("NAKATPASE", predicate=ROLE)
    assert rejection.outcome == "rejected"
    assert rejection.reason == "outlines epithelium only"
    assert len(store.held("NAKATPASE", predicate=ROLE, history=True)) == 1
    assert store.answers("NAKATPASE") == []
    assert store.in_state("rejected") == ["NAKATPASE"]
    assert "already rejected" in said[-1]


def test_a_rejection_still_reverses_an_acceptance(store, snapshot_root, transport, said):
    """A rejection still reverses an acceptance."""
    curate("NakATPase", store=store, snapshot_root=snapshot_root, transport=transport,
           said=said, answer="")
    curate("NakATPase", store=store, snapshot_root=snapshot_root, transport=transport,
           said=said, answer="wrong channel", today="2026-09-30")

    assert [d.outcome for d in store.decisions("NAKATPASE")] == ["accepted", "rejected"]
    assert store.answers("NAKATPASE") == []
    assert store.in_state("rejected") == ["NAKATPASE"]


def test_a_refused_write_queues_nothing_and_leaves_the_approval_standing(
        store, snapshot_root, transport, said):
    """The mitochondrial veto refuses the write and leaves the queue unchanged."""
    accepted = curate("Citrate Synthase", store=store, snapshot_root=snapshot_root,
                      transport=transport, said=said, answer="")
    assert accepted.value == "marker"

    with pytest.raises(ValueError, match="forbidden"):
        curate("Citrate Synthase", store=store, snapshot_root=snapshot_root,
               transport=transport, said=said, answer=session.LATER,
               proposer=ProposesCytoStructural())

    assert store.in_state("unresolved") == []
    assert store.in_state("rejected") == []
    assert store.in_state("accepted") == ["CITRATE_SYNTHASE"]
    assert [d.value for d in store.answers("CITRATE_SYNTHASE")] == ["marker"]
    assert store.held("CITRATE_SYNTHASE", history=True) == [accepted]


def test_a_refused_first_write_leaves_no_subject_anywhere(store, snapshot_root, transport, said):
    with pytest.raises(ValueError, match="forbidden"):
        curate("Citrate Synthase", store=store, snapshot_root=snapshot_root,
               transport=transport, said=said, answer="", proposer=ProposesCytoStructural())

    assert store.states == {}
    assert store.claims == [] and store.rulings == []


def test_nothing_is_written_for_what_cannot_be_curated(store, snapshot_root, transport, said):
    for raw in ("pNRFR2", "DAPI"):
        assert curate(raw, store=store, snapshot_root=snapshot_root, transport=transport,
                      said=said, answer="") is None
    assert store.claims == [] and store.states == {}
    assert "unresolved" in said[0] and "no single UniProt accession" in said[1]


def test_curate_all_reports_a_refusal_and_carries_on(store, snapshot_root, transport, said):
    run, refused = session.curate_all(
        ["Citrate Synthase", "NakATPase"], store=store, branch="", snapshots=snapshot_root,
        by="rafael", ask=lambda prompt: "", today="2026-09-25", fetch=transport,
        proposer=ProposesCytoStructural(), say=said.append)

    assert run is None
    assert refused == ["Citrate Synthase"]
    assert store.in_state("accepted") == ["NAKATPASE"]


def test_main_runs_offline_and_fails_loudly_on_nothing(transport, capsys):
    assert session.main(["--by", "rafael", "NakATPase"], ask=lambda prompt: "",
                        fetch=transport) == 0
    assert "NAKATPASE: accepted ['cyto_structural']" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        session.main(["NakATPase"])


def test_main_curates_through_an_alias_the_newest_export_accepts(tmp_path, transport, capsys):
    write_export(tmp_path, "rel-old", "2026-09-20T00:00:00+00:00", [])
    write_export(tmp_path, "rel-new", "2026-09-25T00:00:00+00:00", [("E-CADHERIN", "Ecad")])
    write_export(tmp_path, "rel-other", "2026-09-30T00:00:00+00:00", [("VIMENTIN", "Ecad")],
                 domain="reagent_mapping")

    assert session.main(["--by", "rafael", "--exports", str(tmp_path), "Ecad"],
                        ask=lambda prompt: "", fetch=transport) == 0
    out = capsys.readouterr().out
    assert "resolving against release rel-new" in out
    assert "E-CADHERIN: accepted" in out


def test_main_names_the_missing_release_as_the_reason(tmp_path, transport, capsys):
    assert session.main(["--by", "rafael", "--exports", str(tmp_path), "Ecad"],
                        ask=lambda prompt: "", fetch=transport) == 0
    assert "Ecad: unresolved - no release export found" in capsys.readouterr().out


def test_release_pins_an_export_over_a_newer_one(tmp_path, transport, capsys):
    pinned = write_export(tmp_path, "rel-old", "2026-09-20T00:00:00+00:00",
                          [("E-CADHERIN", "Ecad")])
    write_export(tmp_path, "rel-new", "2026-09-25T00:00:00+00:00", [])

    session.main(["--by", "rafael", "--exports", str(tmp_path), "Ecad"],
                 ask=lambda prompt: "", fetch=transport)
    assert "Ecad: unresolved against release rel-new, nothing guessed" in capsys.readouterr().out

    session.main(["--by", "rafael", "--exports", str(tmp_path), "--release", str(pinned), "Ecad"],
                 ask=lambda prompt: "", fetch=transport)
    assert "E-CADHERIN: accepted" in capsys.readouterr().out
