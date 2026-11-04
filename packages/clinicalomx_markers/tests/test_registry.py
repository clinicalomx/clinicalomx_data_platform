"""Integrity of the curated inputs: key collisions, dangling references, and roles outside
the controlled set. No database."""
from __future__ import annotations

from clinicalomx_reagents.rows import fluorophores

from clinicalomx_markers.resolve import match_key
from clinicalomx_markers.seed_data.rows import channels, markers
from clinicalomx_markers.vocab import SegmentationRole


def test_curated_files_are_populated():
    assert markers(), "markers.csv is empty"
    assert channels(), "channels.csv is empty"


def test_canonical_names_are_unique():
    names = [r["canonical_name"] for r in markers()]
    assert len(names) == len(set(names))


def test_canonical_names_do_not_collide_on_match_key():
    seen: dict[str, str] = {}
    for name in (r["canonical_name"] for r in markers()):
        key = match_key(name)
        assert key not in seen, f"collide on match key: {seen[key]} vs {name}"
        seen[key] = name


def test_every_channel_references_a_known_marker():
    known = {r["canonical_name"] for r in markers()}
    dangling = [c["canonical_name"] for c in channels() if c["canonical_name"] not in known]
    assert not dangling, f"channels.csv references unknown markers: {dangling}"


def test_one_spelling_can_be_canonical_as_a_marker_and_as_a_dye():
    """DAPI is both an analyte and a dye."""
    assert "DAPI" in {r["canonical_name"] for r in markers()}
    assert "DAPI" in {r["canonical_name"] for r in fluorophores()}


def test_no_dye_alias_duplicates_a_canonical_name():
    """A dye alias never repeats a canonical name."""
    dye_keys = {match_key(r["canonical_name"]) for r in fluorophores()}
    redundant = [a for r in fluorophores() for a in (r["aliases"] or "").split("|")
                  if a and match_key(a) in dye_keys]
    assert not redundant, f"aliases already handled by exact match: {redundant}"


def test_roles_are_in_the_controlled_set():
    for row in channels():
        role = row["segmentation_role"].strip()
        assert role in tuple(SegmentationRole), f"{row['canonical_name']}: bad role {role!r}"


def test_channels_with_a_uniprot_release_record_their_evidence():
    """A channel with a UniProt release has evidence text."""
    for row in channels():
        if row.get("uniprot_release", "").strip():
            assert row.get("uniprot_cc_raw", "").strip(), (
                f"{row['canonical_name']} has a release but no verbatim evidence"
            )


def test_mitochondrial_channels_are_not_structural():
    """Mitochondrial channels are never structural. Localisation vetoes; it never promotes."""
    for row in channels():
        if "mitochondrion" in row.get("compartment", "").lower():
            assert row["segmentation_role"].strip() != "cyto_structural", (
                f"{row['canonical_name']} is mitochondrial and cannot be structural"
            )


def test_nothing_is_active_yet():
    """The registry is unreviewed. Delete this test when that changes."""
    active = [r["canonical_name"] for r in markers()
              if (r.get("status") or "active").strip() == "active"]
    assert not active, f"markers are now active: {active} - review this test"
