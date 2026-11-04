"""Name resolution is deterministic and never over-matches, with no model involved."""
from __future__ import annotations

import pytest
from clinicalomx_ledger.grammar.published import Answer, Published, load, manifest_hash, render

from clinicalomx_markers import resolve as resolution
from clinicalomx_markers.domain.imaging_channel import PLATFORM_TERMS
from clinicalomx_markers.domain.marker import MARKER_DOMAIN, MarkerPredicate
from clinicalomx_markers.resolve import Kind, match_key, resolve, resolve_all
from clinicalomx_markers.seed_data.rows import markers


def alias(marker: str, spelling: str, n: int) -> Answer:
    return Answer(subject=marker, predicate=MarkerPredicate.ALIAS, value=spelling,
                  source="human", source_id="rafael", source_version="", evidence="golden set",
                  claim_uid=f"clm-{n}", decision_uid=f"dec-{n}", decided_by="rafael",
                  decided_at="2026-09-25T00:00:00+00:00")


def export(answers: list[Answer], domain: str = MARKER_DOMAIN.name) -> Published:
    """A release rendered and read back, as a consumer holds it."""
    return load(render(Published(
        release_uid="rel-test", domain=domain,
        manifest_hash=manifest_hash(a.decision_uid for a in answers),
        released_at="2026-09-25T00:00:00+00:00", released_by="rafael", policy_version="",
        answers=tuple(answers))))


ECAD = export([alias("E-CADHERIN", "Ecad", 1)])


def test_the_hyphen_that_broke_a_classifier():
    assert match_key("NakATPase") == match_key("Na-K-ATPase")
    a, b = resolve("NakATPase"), resolve("Na-K-ATPase")
    assert a.canonical == b.canonical == "NAKATPASE"
    assert a.resolved and b.resolved


@pytest.mark.parametrize("raw", ["nakatpase", "NAK_ATPASE", "Nak ATPase", "na-k-atpase"])
def test_case_and_separator_variants_all_land(raw):
    assert resolve(raw).canonical == "NAKATPASE"


def test_an_accepted_alias_resolves_when_key_collapse_cannot():
    """`Ecad` collapses to "ecad", not "ecadherin"; only an approved alias reaches it."""
    assert match_key("Ecad") != match_key("E-CADHERIN")
    assert not resolve("Ecad").resolved, "no release, so no alias is approved"
    got = resolve("Ecad", ECAD)
    assert (got.canonical, got.via) == ("E-CADHERIN", "alias")


def test_an_alias_the_release_does_not_hold_does_not_resolve():
    """A rejected or undecided alias is not in the export."""
    assert not resolve("Ecad", export([])).resolved


def test_a_release_serves_only_what_it_pinned():
    """An alias approved after a release is not in that release."""
    later = export([alias("E-CADHERIN", "Ecad", 1), alias("VIMENTIN", "Vim", 2)])
    assert not resolve("Vim", ECAD).resolved
    assert resolve("Vim", later).canonical == "VIMENTIN"


def test_a_spelling_accepted_for_two_markers_names_neither():
    both = export([alias("E-CADHERIN", "Ecad", 1), alias("VIMENTIN", "Ecad", 2)])
    assert not resolve("Ecad", both).resolved


def test_an_alias_cannot_shadow_a_canonical_name():
    got = resolve("LDHA", export([alias("E-CADHERIN", "LDHA", 1)]))
    assert (got.canonical, got.via) == ("LDHA", "exact")


def test_a_release_of_another_domain_is_refused():
    with pytest.raises(ValueError, match="not a"):
        resolve("Ecad", export([alias("E-CADHERIN", "Ecad", 1)], domain="reagent_mapping"))


def test_a_dye_alias_does_not_resolve_as_a_marker():
    """`AF750` is a fluorophore alias, not a marker."""
    assert not resolve("AF750").resolved


def test_canonical_names_resolve_to_themselves():
    for row in markers():
        got = resolve(row["canonical_name"])
        assert got.canonical == row["canonical_name"]
        assert got.via == "exact"


def test_unknown_names_are_unresolved_not_guessed():
    """Unknown names stay unresolved. Edit distance is never used."""
    for raw in ("CD9999", "ZBQ-17", "Cathepsin", "LAG3"):
        got = resolve(raw)
        assert not got.resolved, f"{raw} should not resolve, got {got.canonical}"
        assert got.key == match_key(raw)


def test_near_miss_does_not_match():
    assert resolve("CD8A").canonical == "CD8A"
    assert not resolve("CD4").resolved


def test_resolve_all_partitions_a_panel():
    resolved, unresolved = resolve_all(["NakATPase", "Ecad", "CD9999", "LDHA"], ECAD)
    assert {r.canonical for r in resolved} == {"NAKATPASE", "E-CADHERIN", "LDHA"}
    assert [u.raw for u in unresolved] == ["CD9999"]


def test_unresolved_list_is_the_work_queue():
    _, unresolved = resolve_all(["DAPI", "totally-unknown-probe"])
    assert len(unresolved) == 1
    assert unresolved[0].raw == "totally-unknown-probe"


def test_a_platform_resolves_through_the_call_a_marker_does():
    """A platform and a marker resolve through the same call; the answer gives the kind."""
    platform, marker = resolve("PCF", ECAD), resolve("Ecad", ECAD)
    assert (platform.kind, platform.canonical, platform.via) == (
        Kind.PLATFORM, "EFO:0700001", "exact")
    assert (marker.kind, marker.canonical, marker.via) == (Kind.MARKER, "E-CADHERIN", "alias")


@pytest.mark.parametrize("label", list(PLATFORM_TERMS))
def test_every_platform_label_and_term_resolves_to_its_term(label):
    term = PLATFORM_TERMS[label]
    for raw in (label, label.lower(), term):
        got = resolve(raw)
        assert (got.kind, got.canonical) == (Kind.PLATFORM, term), raw


def test_a_platform_with_no_term_does_not_resolve():
    """CosMx has no EFO term at the pinned release."""
    assert not resolve("CosMx").resolved


def test_the_marker_case_is_unchanged():
    """Every seeded marker resolves."""
    for row in markers():
        for got in (resolve(row["canonical_name"]),
                    resolve(row["canonical_name"], kind=Kind.MARKER)):
            assert (got.kind, got.canonical, got.via) == (
                Kind.MARKER, row["canonical_name"], "exact")


def test_kind_narrows_the_answer():
    assert not resolve("PCF", kind=Kind.MARKER).resolved
    assert not resolve("Ecad", ECAD, kind=Kind.PLATFORM).resolved
    assert resolve("Ecad", ECAD, kind=Kind.MARKER).canonical == "E-CADHERIN"


def test_a_spelling_two_kinds_claim_names_neither(monkeypatch):
    """A spelling two kinds claim resolves to neither unless a kind is asked."""
    monkeypatch.setattr(resolution, "PLATFORM_TERMS", {"Vimentin": "EFO:0000000"})
    resolution.reset_cache()
    try:
        assert not resolve("VIMENTIN").resolved
        assert resolve("VIMENTIN", kind=Kind.MARKER).canonical == "VIMENTIN"
        assert resolve("VIMENTIN", kind=Kind.PLATFORM).canonical == "EFO:0000000"
    finally:
        monkeypatch.undo()
        resolution.reset_cache()


def test_every_lookup_path_reports_how_it_matched():
    """Each path reports its own `via`: "exact" for a declared spelling verbatim, "key" after
    key collapse."""
    term = PLATFORM_TERMS["PCF"]
    cases = {"LDHA": (Kind.MARKER, "LDHA", "exact"),
             "Ecad": (Kind.MARKER, "E-CADHERIN", "alias"),
             "Na-K-ATPase": (Kind.MARKER, "NAKATPASE", "key"),
             "ldha": (Kind.MARKER, "LDHA", "key"),
             "PCF": (Kind.PLATFORM, term, "exact"),
             "pcf": (Kind.PLATFORM, term, "key"),
             term: (Kind.PLATFORM, term, "exact")}
    got = {raw: resolve(raw, ECAD) for raw in cases}
    assert {raw: (r.kind, r.canonical, r.via) for raw, r in got.items()} == cases
    assert {r.via for r in got.values()} == {"exact", "key", "alias"}
