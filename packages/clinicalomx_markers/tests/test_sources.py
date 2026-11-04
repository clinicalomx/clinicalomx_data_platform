"""UniProt extraction is deterministic, offline, and cached."""
from __future__ import annotations

import pytest
from tests.conftest import ACCESSIONS, load_fixture

from clinicalomx_markers.sources.snapshot import capture, snapshot_key
from clinicalomx_markers.sources.uniprot import ENTRY_LEVEL, fetch, parse, url_for


def test_parses_the_real_response_shape():
    record, _ = fetch("P05023", transport=lambda url: load_fixture("P05023"))
    assert record.accession == "P05023"
    assert "ATPase" in record.protein_name
    assert "ATP1A1" in record.gene_names
    assert "Cell membrane" in record.locations
    assert all(go.startswith("GO:") for go in record.go_cc)


def test_go_terms_are_cellular_component_only():
    """Only cellular-component GO terms are kept."""
    body = load_fixture("P05023")
    record = parse(body)
    raw = {ref["id"]: ref for ref in
           __import__("json").loads(body.text)["uniProtKBCrossReferences"]
           if ref.get("database") == "GO"}
    for go_id in record.go_cc:
        terms = [p["value"] for p in raw[go_id]["properties"] if p["key"] == "GoTerm"]
        assert any(t.startswith("C:") for t in terms), f"{go_id} is not a component term"


def test_evidence_is_verbatim_and_quotable():
    record, _ = fetch("O75390", transport=lambda url: load_fixture("O75390"))
    assert "Mitochondrion" in record.evidence


def test_an_isoform_scoped_location_does_not_collapse_into_the_entry():
    """CD8A isoform 1 is on the membrane and isoform 2 is secreted; scopes stay separate."""
    record, _ = fetch("P01732", transport=lambda url: load_fixture("P01732"))

    assert record.locations_for("Isoform 1") == ("Cell membrane",)
    assert record.locations_for("Isoform 2") == ("Secreted",)
    assert record.scopes == ("Isoform 1", "Isoform 2")
    assert record.locations_for() == (), "neither annotation applies to the entry"
    assert record.locations == ("Cell membrane", "Secreted"), "the flat view keeps every scope"


def test_an_unscoped_entry_reads_as_entry_level():
    record, _ = fetch("O75390", transport=lambda url: load_fixture("O75390"))

    assert record.scopes == (ENTRY_LEVEL,)
    assert record.locations_for(ENTRY_LEVEL) == record.locations
    assert "[" not in record.evidence, "an entry-level quote gains no scope prefix"


def test_scoped_evidence_says_which_isoform_it_quotes():
    """Scoped evidence keeps UniProt's bracket prefix."""
    record, _ = fetch("P01732", transport=lambda url: load_fixture("P01732"))
    assert record.evidence == "[Isoform 1]: Cell membrane. [Isoform 2]: Secreted"


def test_alias_material_is_kept_rather_than_thrown_away():
    """Gene synonyms and alternative protein names are parsed."""
    record, _ = fetch("P12830", transport=lambda url: load_fixture("P12830"))

    assert record.gene_names == ("CDH1",)
    assert record.gene_synonyms == ("CDHE", "UVO")
    assert set(record.alternative_names) >= {"Epithelial cadherin", "Uvomorulin", "CAM 120/80"}


def test_function_text_is_captured_so_it_can_be_cited():
    """Function text is in the snapshotted bytes."""
    record, _ = fetch("O75390", transport=lambda url: load_fixture("O75390"))
    assert "tricarboxylic acid cycle" in record.function


@pytest.mark.parametrize("accession", sorted(ACCESSIONS.values()))
def test_every_fixture_carries_the_widened_fields(accession, transport):
    """Every fixture carries every field in `FIELDS`."""
    record, _ = fetch(accession, transport=transport)
    assert record.function, "recapture the fixtures: tests/fixtures/recapture_uniprot.py"
    assert "cc_function" in url_for(accession)


@pytest.mark.parametrize("accession", sorted(ACCESSIONS.values()))
def test_every_fixture_parses(accession, transport):
    record, _ = fetch(accession, transport=transport)
    assert record.accession == accession
    assert record.protein_name


def test_uncached_url_raises_rather_than_hitting_the_network(transport):
    with pytest.raises(LookupError, match="no fixture"):
        fetch("P99999", transport=transport)


def test_snapshot_key_is_stable_and_path_safe():
    a = snapshot_key("uniprot", "P05023", "2026-07-31")
    assert a == snapshot_key("uniprot", "P05023", "2026-07-31")
    b = snapshot_key("url", "https://example.org/a/b", "2026-07-31")
    assert "://" not in b and b.count("/") == 3


def test_capture_fetches_once_then_reuses(snapshot_root, transport):
    calls = {"n": 0}

    def fetch_once():
        calls["n"] += 1
        return transport(url_for("P05023"))

    first = capture(snapshot_root, source="uniprot", source_id="P05023",
                    retrieved_at="2026-07-31", fetch=fetch_once)
    second = capture(snapshot_root, source="uniprot", source_id="P05023",
                     retrieved_at="2026-07-31", fetch=fetch_once)

    assert calls["n"] == 1, "a retry must not re-fetch"
    assert first.key == second.key
    assert first.fetched_content.text == second.fetched_content.text


def test_a_new_retrieval_date_is_a_new_snapshot(snapshot_root, transport):
    old = capture(snapshot_root, source="uniprot", source_id="P05023", retrieved_at="2026-01-01",
                  fetch=lambda: transport(url_for("P05023")))
    new = capture(snapshot_root, source="uniprot", source_id="P05023", retrieved_at="2026-07-31",
                  fetch=lambda: transport(url_for("P05023")))
    assert old.key != new.key, "re-verification needs both snapshots to compare"
