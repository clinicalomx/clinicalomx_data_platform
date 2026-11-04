"""The ontology provenance check compares EFO rows' sources against the pin.

It must name terms moved onto an older release and terms with no source, with the declared
release, what the rows point at, and how many.
"""

from __future__ import annotations

import bionty as bt
import lamindb as ln
import pytest
from clinicalomx.checks import ontology
from clinicalomx.vocab.ontology import pinned_source

from clinicalomx_markers.domain.imaging_channel import PLATFORM_SOURCE, PLATFORM_TERMS

ENTITY = PLATFORM_SOURCE["entity"]
OLDER = "3.84.0"
PIN = f"bionty.{ENTITY} declared efo {PLATFORM_SOURCE['version']} by clinicalomx_markers"


def _efo_findings(pins=None) -> list[str]:
    pins = pins if pins is not None else ontology.declared_pins()[0]
    return ontology.pin_mismatches({ENTITY: pins[ENTITY]}, bt)


@pytest.fixture
def older():
    """The platform terms moved onto an older efo release."""
    pin = pinned_source(ENTITY, PLATFORM_SOURCE)
    source = (bt.Source.filter(entity=pin.entity, name=pin.name, version=OLDER).one_or_none()
              or bt.Source(entity=pin.entity, organism=pin.organism, name=pin.name,
                           version=OLDER, branch=ln.Branch.get(name="main")).save())
    ids = sorted(PLATFORM_TERMS.values())
    bt.ExperimentalFactor.filter(ontology_id__in=ids).update(source=source)
    yield ids
    bt.ExperimentalFactor.filter(ontology_id__in=ids).update(source=pin)


@pytest.fixture
def unsourced():
    """A term saved by an import that recorded no version."""
    record = bt.ExperimentalFactor(name="synthetic unsourced", ontology_id="EFO:9999998",
                                   branch=ln.Branch.get(name="main"),
                                   _skip_validation=True).save()
    assert record.source_id is None
    yield record
    record.delete(permanent=True)


def test_a_catalogue_wholly_at_the_pin_passes():
    assert _efo_findings() == []


def test_terms_at_an_older_release_are_named_with_both_versions_and_a_count(older):
    (finding,) = _efo_findings()
    assert finding == (
        f"{PIN}: {len(older)} record(s) point at efo {OLDER} (e.g. {', '.join(older)}); "
        "the pin declares relink, so the owner's ensure() moves them, all or none")


def test_a_term_with_no_recorded_source_is_named_as_an_unrecorded_import(unsourced):
    (finding,) = _efo_findings()
    assert finding.startswith(f"{PIN}: 1 record(s) point at no recorded source (e.g. "
                              "EFO:9999998); an import that recorded no version")


def test_a_pin_the_instance_holds_no_source_for_is_a_finding_not_a_pass():
    pins = ontology.declared_pins()[0]
    missing = ontology.Pin(ENTITY, "efo", "0.0.0-never", None, True, "clinicalomx_markers")
    (finding,) = _efo_findings({ENTITY: missing})
    total = bt.ExperimentalFactor.filter().count()
    assert finding == (f"bionty.{ENTITY} declared efo 0.0.0-never by clinicalomx_markers: the "
                       f"instance holds no Source row for it, so none of its {total} record(s) "
                       "can be shown to be at the pin; run the owner's ensure()")
    assert pins[ENTITY].version == PLATFORM_SOURCE["version"]


def test_a_populated_ontology_nobody_pins_is_reported_with_what_it_points_at():
    record = bt.Tissue(name="synthetic tissue", ontology_id="UBERON:9999999",
                       branch=ln.Branch.get(name="main"), _skip_validation=True).save()
    try:
        findings = ontology.populated_ontologies_are_declared(None)
        assert ("bionty.Tissue: 1 record(s) point at no recorded source (e.g. UBERON:9999999) "
                "and no vocabulary declares a version for it") in findings
        assert not any(f.startswith(f"bionty.{ENTITY}:") for f in findings)
    finally:
        record.delete(permanent=True)


def test_a_protein_of_another_organism_is_named_as_held_under_an_undeclared_source():
    pin = ontology.declared_pins()[0]["Protein"]
    main = ln.Branch.get(name="main")
    human = bt.Source(entity="bionty.Protein", organism=pin.organism, name=pin.source,
                      version=pin.version, branch=main).save()
    mouse_source = bt.Source(entity="bionty.Protein", organism="mouse", name=pin.source,
                             version=pin.version, branch=main).save()
    mouse = bt.Organism(name="mouse", ontology_id="NCBITaxon:10090", branch=main,
                        _skip_validation=True).save()
    record = bt.Protein(name="synthetic mouse protein", uniprotkb_id="Q99999", organism=mouse,
                        source=mouse_source, branch=main, _skip_validation=True).save()
    try:
        (finding,) = ontology.pin_mismatches({"Protein": pin}, bt)
        assert finding == (f"bionty.Protein declared {pin.label} by clinicalomx_markers: 1 "
                           f"record(s) are held under {pin.source} {pin.version} (mouse) "
                           "(e.g. Q99999), which no pin declares")
    finally:
        record.delete(permanent=True)
        for row in (mouse, mouse_source, human):
            row.delete(permanent=True)
