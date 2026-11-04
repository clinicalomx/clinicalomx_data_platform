"""Segmentation reads roles from a release export, with no database.

The export is rendered as `services.export_release` does and read back through
`published.load`, with every seed channel row approved; results must match the CSV.
`tests_pg/test_release_export.py` repeats this on a real ledger.
"""
from __future__ import annotations

import pytest
from clinicalomx_ledger.grammar.published import Answer, Published, load, manifest_hash, render

from clinicalomx_markers.domain.marker import MARKER_DOMAIN, MarkerPredicate
from clinicalomx_markers.query.segmentation import Candidate, candidates, nuclear, structural
from clinicalomx_markers.resolve import resolve
from clinicalomx_markers.seed_data.rows import channels

ROLE = MarkerPredicate.SEGMENTATION_ROLE
PANEL = ["Vimentin", "E-cadherin", "DAPI", "Citrate Synthase", "NaKATPase", "LDHA",
         "FoxP3", "CD8a", "Blank", "totally-unknown-probe"]


def answer(subject: str, value: str, n: int, predicate: str = ROLE) -> Answer:
    return Answer(subject=subject, predicate=predicate, value=value, source="human",
                  source_id="rafael", source_version="", evidence="seed row",
                  claim_uid=f"clm-{n}", decision_uid=f"dec-{n}", decided_by="rafael",
                  decided_at="2026-09-25T00:00:00+00:00")


def export(answers: list[Answer], domain: str = MARKER_DOMAIN.name) -> Published:
    return load(render(Published(
        release_uid="rel-test", domain=domain,
        manifest_hash=manifest_hash(a.decision_uid for a in answers),
        released_at="2026-09-25T00:00:00+00:00", released_by="rafael", policy_version="",
        answers=tuple(answers))))


@pytest.fixture
def seed_release() -> Published:
    return export([answer(r["canonical_name"], r["segmentation_role"], n)
                   for n, r in enumerate(channels())])


def from_the_csv(raw_channels: list[str], platform: str = "PCF") -> list[Candidate]:
    """What `candidates` returns from `channels.csv`: the expected result."""
    rows = {(r["canonical_name"], r["platform"]): r for r in channels()}
    out = []
    for raw in raw_channels:
        got = resolve(raw)
        row = rows.get((got.canonical, platform)) if got.resolved else None
        if row is not None:
            out.append(Candidate(raw=raw, canonical=got.canonical, platform=platform,
                                 segmentation_role=row["segmentation_role"]))
    return out


def test_the_approved_seed_rows_give_what_the_csv_gave(seed_release):
    assert candidates(PANEL, seed_release) == from_the_csv(PANEL)


def test_segmentation_returns_role_filtered_candidates(seed_release):
    got = structural(["Vimentin", "E-cadherin", "DAPI", "Citrate Synthase"], seed_release)
    names = {c.canonical for c in got}
    assert "VIMENTIN" in names and "E-CADHERIN" in names
    assert "CITRATE_SYNTHASE" not in names, "mitochondrial marker must not be structural"
    assert "DAPI" not in names


def test_segmentation_finds_the_nuclear_channel(seed_release):
    assert {c.canonical for c in nuclear(["DAPI", "Vimentin"], seed_release)} == {"DAPI"}


def test_unresolved_channels_are_dropped_not_guessed(seed_release):
    got = structural(["Vimentin", "totally-unknown-probe"], seed_release)
    assert {c.canonical for c in got} == {"VIMENTIN"}


def test_a_marker_with_no_approved_role_is_not_a_candidate():
    only = export([answer("VIMENTIN", "cyto_structural", 0)])
    assert [c.canonical for c in candidates(["Vimentin", "DAPI"], only)] == ["VIMENTIN"]


def test_the_newest_approved_role_wins():
    both = export([answer("VIMENTIN", "marker", 0), answer("VIMENTIN", "cyto_structural", 1)])
    assert [c.segmentation_role for c in candidates(["Vimentin"], both)] == ["cyto_structural"]


def test_a_role_on_a_channel_subject_is_not_read_as_the_markers():
    channel = export([answer("DAPI@PCF", "nucleus", 0)])
    assert candidates(["DAPI"], channel) == []


def test_another_domains_release_is_refused():
    with pytest.raises(ValueError, match="not a 'marker' one"):
        candidates(["DAPI"], export([], domain="reagent_mapping"))
