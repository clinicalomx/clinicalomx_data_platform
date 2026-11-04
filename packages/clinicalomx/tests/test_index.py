"""Tests for the shared index: corpus and per-ROI views read one resolution and cannot disagree.

Also: the index is optional, its stamp is the data's age, and it refuses questions it cannot answer.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

import clinicalomx.extract.availability as availability
from clinicalomx.extract.app import demo
from clinicalomx.extract.availability import describe
from clinicalomx.extract.corpus import survey
from clinicalomx.extract.index import Index

NOW = "2026-01-01T00:00:00Z"


@pytest.fixture
def snap():
    demo.install_governance()
    return demo.build_snapshot()


def _every_roi(snap):
    return [r for t in snap.roi_types() for r in snap.records(t)]


# The property.
def test_one_index_gives_one_answer_per_pair(snap):
    resolved: list[tuple[str, str]] = []
    real = availability._resolve_role

    def counted(role, record, ctx):
        resolved.append((record.uid, role))
        return real(role, record, ctx)

    index = Index.of(snap, now=NOW)
    availability._resolve_role = counted
    try:
        survey(index=index)
        first = {(uid, role): index.role_on(r, role)
                 for r in _every_roi(snap) for role in index.ctx.declared
                 for uid in [r.uid]}
        for roi in _every_roi(snap):
            describe(roi, index=index)
    finally:
        availability._resolve_role = real

    assert len(resolved) == len(set(resolved)), (
        f"{len(resolved) - len(set(resolved))} pair(s) were resolved more than once")
    for roi in _every_roi(snap):
        for role in index.ctx.declared:
            assert index.role_on(roi, role) is first[(roi.uid, role)], (
                f"{roi.name}/{role} was answered with a second object")


def test_neither_view_resolves_a_role_outside_the_index(snap):
    index = Index.of(snap, now=NOW)
    survey(index=index, group_by="slide")
    for roi in _every_roi(snap):
        describe(roi, index=index)

    def refuse(role, record, ctx):
        raise AssertionError(f"resolved {record.uid}/{role} outside the index")

    real = availability._resolve_role
    availability._resolve_role = refuse
    try:
        survey(index=index, group_by="roi_type")
        for roi in _every_roi(snap):
            describe(roi, index=index)
    finally:
        availability._resolve_role = real


def test_the_corpus_and_the_roi_view_agree_about_an_inherited_provenance_key(snap):
    tma = snap.record_by_name("TMA", demo.TMA_NAME)
    per_roi = {c.role: c for c in describe(tma, snapshot=snap, now=NOW).child_coverage}
    corpus = {c.role: c for c in survey(snapshot=snap, roi_types=["Core"], now=NOW).coverage}

    assert per_roi["source_image"].state == corpus["source_image"].state == "present"
    for role, cov in per_roi.items():
        assert cov.state == corpus[role].state, (
            f"{role}: the ROI view says {cov.state}, the corpus says {corpus[role].state}")


# The contract.
def test_an_index_is_optional(snap):
    tma = snap.record_by_name("TMA", demo.TMA_NAME)
    alone = describe(tma, snapshot=snap, now=NOW)
    shared = describe(tma, index=Index.of(snap, now=NOW))
    assert alone.to_dict() == shared.to_dict()


def test_the_stamp_is_the_data_s_age_and_not_the_report_s(snap, monkeypatch):
    monkeypatch.setattr(snap, "_read_at", "2026-01-01T10:00:00Z", raising=False)
    index = Index.of(snap)
    assert index.built_at == "2026-01-01T10:00:00Z"

    stamps = {describe(r, index=index).index_built_at for r in _every_roi(snap)}
    stamps |= {survey(index=index).index_built_at}
    assert stamps == {"2026-01-01T10:00:00Z"}, (
        "every report off one set of reads carries one stamp, whenever it was formatted")


def test_a_snapshot_that_has_read_nothing_has_no_read_time(snap):
    assert snap.read_at is None
    assert Index.of(snap, now=NOW).built_at == NOW


def test_an_index_refuses_a_snapshot_it_was_not_built_over(snap):
    other = demo.build_snapshot()
    with pytest.raises(ValueError, match="different Snapshot"):
        describe(snap.record_by_name("TMA", demo.TMA_NAME), snapshot=other,
                 index=Index.of(snap, now=NOW))


def test_include_storage_with_no_keys_is_refused(snap):
    with pytest.raises(ValueError, match="needs probe_keys"):
        Index.of(snap, include_storage=True, now=NOW)


def test_an_index_that_did_not_probe_refuses_to_report_missing(snap):
    with pytest.raises(ValueError, match="did not probe"):
        survey(index=Index.of(snap, now=NOW), include_storage=True)


def test_a_pinned_recipe_is_not_shadowed_by_the_catalogue_s_own(snap):
    tma = snap.record_by_name("TMA", demo.TMA_NAME)
    index = Index.of(snap, now=NOW)
    stock = next(r for r in index.catalogue
                 if r.layout == "per_child" and "TMA" in r.roi_types)
    kept = stock.elements[0]
    narrowed = replace(stock, elements=(kept,), requires_any=(kept.name,))

    assert index.readiness(stock, tma) is not index.readiness(narrowed, tma)
    assert [e.element for e in index.readiness(narrowed, tma).elements] == [kept.name]
    assert index.readiness(stock, tma) is index.readiness(stock, tma)
