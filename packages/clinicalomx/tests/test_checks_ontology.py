"""Tests for gathering ontology pins from declarations. The database half is in
``packages/clinicalomx_markers/tests_pg/test_ontology_provenance.py``."""
from __future__ import annotations

from clinicalomx.checks import ontology
from clinicalomx.vocab.registry import Vocabulary


class _EP:
    def __init__(self, name, value):
        self.name, self._value = name, value

    def load(self):
        if isinstance(self._value, Exception):
            raise self._value
        return self._value


def _vocab(owner, entity="ExperimentalFactor", version="3.85.0", **kw):
    v = Vocabulary(owner)
    v.source(entity, source="efo", version=version, **kw)
    return v


def _entry_points(monkeypatch, *eps):
    monkeypatch.setattr("importlib.metadata.entry_points", lambda group: list(eps))


def test_a_consumer_pin_is_gathered_with_its_owner(monkeypatch):
    _entry_points(monkeypatch, _EP("markers", _vocab("markers", relink=True)))
    pins, findings = ontology.declared_pins()
    assert findings == []
    assert pins["ExperimentalFactor"] == ontology.Pin(
        "ExperimentalFactor", "efo", "3.85.0", None, True, "markers")


def test_a_vocabulary_that_will_not_load_is_a_finding_not_an_absence(monkeypatch):
    _entry_points(monkeypatch, _EP("broken", ImportError("no module named x")),
                  _EP("sloppy", "not a vocabulary"))
    pins, findings = ontology.declared_pins()
    assert pins == {}
    assert len(findings) == 2
    assert "'broken' could not be loaded" in findings[0] and "no module named x" in findings[0]
    assert "'sloppy' is a str, not a Vocabulary" in findings[1]


def test_two_owners_pinning_one_ontology_at_different_releases_is_a_finding(monkeypatch):
    _entry_points(monkeypatch, _EP("a", _vocab("a", version="3.85.0")),
                  _EP("b", _vocab("b", version="3.84.0")))
    pins, findings = ontology.declared_pins()
    assert pins["ExperimentalFactor"].owner == "a"
    assert findings == ["bionty.ExperimentalFactor: pinned at efo 3.85.0 by a and at efo "
                        "3.84.0 by b; one instance can hold only one"]


def test_an_instance_without_bionty_reports_every_pin_it_cannot_check(monkeypatch):
    _entry_points(monkeypatch, _EP("markers", _vocab("markers")))
    monkeypatch.setattr(ontology, "_bionty", lambda: None)
    assert ontology.ontology_terms_are_at_the_pin(None) == [
        "bionty.ExperimentalFactor declared efo 3.85.0 by markers: the instance does not "
        "carry the bionty module"]


def test_an_instance_that_cannot_be_reached_is_a_finding(monkeypatch):
    _entry_points(monkeypatch, _EP("markers", _vocab("markers")))

    def unreachable():
        raise RuntimeError("no instance connected")

    monkeypatch.setattr(ontology, "_bionty", unreachable)
    assert ontology.ontology_terms_are_at_the_pin(None) == [
        "bionty could not be reached, so 1 pin(s) are unchecked: RuntimeError: no instance "
        "connected"]


def test_both_checks_are_registered_with_the_framework():
    from clinicalomx.checks import all_checks

    names = {c.name: c.severity for c in all_checks()}
    assert names["vocab.ontology_terms_are_at_the_pin"] == "error"
    assert names["vocab.populated_ontologies_are_declared"] == "warn"
