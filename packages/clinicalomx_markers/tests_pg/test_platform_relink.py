"""ensure relinks EFO terms held at another release to the pinned one.

bionty keys an EFO record on its ontology id alone. The fixture moves the platform terms, and
one unrelated term, onto an older release.
"""

from __future__ import annotations

import bionty as bt
import lamindb as ln
import pytest
from clinicalomx.vocab.ontology import pinned_source, reconcile
from clinicalomx.vocab.upsert import Reconciler

from clinicalomx_markers.curate.channels import platform_term
from clinicalomx_markers.domain.imaging_channel import PLATFORM_SOURCE, PLATFORM_TERMS
from clinicalomx_markers.vocab import VOCAB

ENTITY = PLATFORM_SOURCE["entity"]
OLDER = "3.84.0"


def _pinned():
    return pinned_source(ENTITY, PLATFORM_SOURCE)


def _older():
    pin = _pinned()
    found = bt.Source.filter(entity=pin.entity, name=pin.name, version=OLDER).one_or_none()
    return found or bt.Source(entity=pin.entity, organism=pin.organism, name=pin.name,
                              version=OLDER, description=pin.description,
                              branch=ln.Branch.get(name="main")).save()


def _ensure() -> Reconciler:
    rec = Reconciler()
    reconcile(rec, ENTITY, VOCAB.sources[ENTITY])
    return rec


def _versions(ids) -> dict[str, str]:
    return dict(bt.ExperimentalFactor.filter(ontology_id__in=ids)
                .values_list("ontology_id", "source__version"))


@pytest.fixture
def moved():
    """The platform terms plus one term no platform names, moved onto an older release."""
    other = (bt.ExperimentalFactor.filter(source=_pinned())
             .exclude(ontology_id__in=PLATFORM_TERMS.values())
             .values_list("ontology_id", flat=True).first())
    ids = sorted({*PLATFORM_TERMS.values(), other})
    bt.ExperimentalFactor.filter(ontology_id__in=ids).update(source=_older())
    assert set(_versions(ids).values()) == {OLDER}
    yield ids
    bt.ExperimentalFactor.filter(ontology_id__in=ids).update(source=_pinned())


def test_a_term_held_at_another_release_is_refused_naming_both(moved):
    with pytest.raises(ValueError, match=rf"held at efo {OLDER}, not the pinned efo "
                                         rf"{PLATFORM_SOURCE['version']}"):
        platform_term("PCF")


def test_ensure_relinks_the_held_terms_to_the_pin_and_says_which(moved):
    rec = _ensure()

    assert set(_versions(moved).values()) == {PLATFORM_SOURCE["version"]}
    (repair,) = [c for c in rec.pending_changes if c.action == "repair"]
    assert f"relink {len(moved)} record(s)" in repair.detail
    assert all(f"{oid} from {OLDER}" in repair.detail for oid in moved)
    for label, term in PLATFORM_TERMS.items():
        assert platform_term(label).ontology_id == term

    again = _ensure()
    assert again.pending_changes == []


def test_a_term_the_pin_cannot_take_stops_the_relink_and_moves_nothing(moved):
    orphan = bt.ExperimentalFactor(name="synthetic factor", ontology_id="EFO:9999999",
                                   source=_older(), branch=ln.Branch.get(name="main"),
                                   _skip_validation=True).save()
    try:
        with pytest.raises(ValueError,
                           match=rf"EFO:9999999 at {OLDER} \(absent from the pinned release\)"):
            _ensure()
        assert set(_versions(moved).values()) == {OLDER}
    finally:
        orphan.delete(permanent=True)


def test_a_pin_that_does_not_opt_in_leaves_records_at_another_release_alone(moved):
    """A source declared without the relink opt-in moves nothing."""
    orphan = bt.ExperimentalFactor(name="synthetic factor", ontology_id="EFO:9999999",
                                   source=_older(), branch=ln.Branch.get(name="main"),
                                   _skip_validation=True).save()
    try:
        rec = Reconciler()
        reconcile(rec, ENTITY, {**VOCAB.sources[ENTITY], "relink": False})
        assert rec.pending_changes == []
        assert set(_versions([*moved, "EFO:9999999"]).values()) == {OLDER}
    finally:
        orphan.delete(permanent=True)


def test_the_protein_pin_is_not_relinked_by_ensure():
    """A Protein held at another uniprot release keeps its source and its name."""
    spec = VOCAB.sources["Protein"]
    main = ln.Branch.get(name="main")

    def source(version):
        fields = {"entity": "bionty.Protein", "name": spec["source"],
                  "organism": spec["organism"], "version": version}
        return (bt.Source.filter(**fields).one_or_none()
                or bt.Source(**fields, branch=main).save())

    pin, older = source(spec["version"]), source("2023-01")
    assert pinned_source("Protein", spec) == pin
    human = bt.Organism.filter(name=pin.organism).one_or_none() or bt.Organism(
        name=pin.organism, _skip_validation=True, branch=main).save()
    held = [bt.Protein(name=name, uniprotkb_id=uid, organism=human, source=source,
                       branch=main, _skip_validation=True).save()
            for name, uid, source in (("synthetic pinned", "Z00001", pin),
                                      ("synthetic held", "Z00002", older))]
    try:
        rec = Reconciler()
        reconcile(rec, "Protein", spec)
        assert rec.pending_changes == []
        again = bt.Protein.get(uniprotkb_id="Z00002")
        assert (again.name, again.source_id) == ("synthetic held", older.id)
    finally:
        for record in held:
            record.delete(permanent=True)
