"""check-qptiffs refresh: what it clears, and where it refuses to write. Uses doubles."""
from __future__ import annotations

import sys
import types

import pytest

from conftest import load_flow_module, load_pipeline_module

check_qptiffs = load_flow_module("check_qptiffs")
mint_slides = load_flow_module("mint_slides")
qptiff = load_pipeline_module("qptiff")
vocab = load_pipeline_module("vocab")


class _Features:
    """The feature accessors the refresh calls, recording what it did."""

    def __init__(self, values: dict):
        self.values = dict(values)
        self.removed: list[str] = []
        self.added: list[dict] = []

    def get_values(self) -> dict:
        return dict(self.values)

    def remove_values(self, name: str) -> None:
        self.removed.append(name)
        self.values.pop(name, None)

    def add_values(self, mapping: dict) -> None:
        self.added.append(dict(mapping))
        self.values.update(mapping)


class _Holder:
    def __init__(self, name: str = "slide-0", values: dict | None = None, key: str | None = None):
        self.name = name
        self.key = key or f"proj/{name}.qptiff"
        self.features = _Features(values or {})


# _clear_stale
def test_an_owned_feature_the_fresh_read_did_not_answer_is_cleared() -> None:
    holder = _Holder(values={qptiff.QPTIFF_CH_NAME: "Opal 520",
                             qptiff.QPTIFF_CH_FLUOROPHORE: "Opal 620"})
    fresh = {qptiff.QPTIFF_CH_NAME: "CD8"}  # the new reader answers the name, not the fluorophore
    cleared = check_qptiffs._clear_stale(holder, fresh, qptiff.CHANNEL_OWNED_FEATURES)
    assert cleared == [qptiff.QPTIFF_CH_FLUOROPHORE]
    assert holder.features.removed == [qptiff.QPTIFF_CH_FLUOROPHORE]
    # the name is left for the upsert to overwrite
    assert holder.features.values[qptiff.QPTIFF_CH_NAME] == "Opal 520"


def test_a_feature_outside_the_owned_set_is_never_cleared() -> None:
    holder = _Holder(values={"source_artifact": "proj/slide-0.qptiff",
                             vocab.IMAGE_TYPE: vocab.MULTIPLEX,
                             qptiff.QPTIFF_CH_GAIN: 2.0})
    cleared = check_qptiffs._clear_stale(holder, {}, qptiff.CHANNEL_OWNED_FEATURES)
    assert cleared == [qptiff.QPTIFF_CH_GAIN]
    assert holder.features.values["source_artifact"] == "proj/slide-0.qptiff"
    assert holder.features.values[vocab.IMAGE_TYPE] == vocab.MULTIPLEX


def test_an_absent_feature_is_not_removed_again() -> None:
    holder = _Holder(values={qptiff.QPTIFF_CH_NAME: "CD8"})
    assert check_qptiffs._clear_stale(holder, {qptiff.QPTIFF_CH_NAME: "CD8"},
                                      qptiff.CHANNEL_OWNED_FEATURES) == []
    assert holder.features.removed == []


# _refresh_slide
@pytest.fixture
def fake_lamindb(monkeypatch):
    """A stand-in ``lamindb`` answering the two Record lookups the refresh makes."""
    state: dict = {"slide_type": object(), "slide": None}

    class _Query:
        def __init__(self, result):
            self._result = result

        def one_or_none(self):
            return self._result

    class _Record:
        @staticmethod
        def filter(**kw):
            if kw.get("is_type"):
                return _Query(state["slide_type"])
            return _Query(state["slide"])

    module = types.ModuleType("lamindb")
    module.Record = _Record
    monkeypatch.setitem(sys.modules, "lamindb", module)
    return state


def _artifact(key: str = "proj/slide-0.qptiff"):
    art = _Holder(name="slide-0", key=key)
    return art


MARKS = {
    vocab.IMAGE_TYPE: vocab.H_AND_E,                      # excluded: triage may have set it
    qptiff.QPTIFF_CHANNEL_NAMES: "CD8,PD-L1,DAPI",        # corrected by the reparse
    qptiff.QPTIFF_N_CHANNELS: 3,
    qptiff.QPTIFF_METADATA: {"slide": {}},                # not a copied feature
}


def test_the_corrected_channel_names_reach_the_slide(fake_lamindb) -> None:
    slide = _Holder(values={"source_image": "proj/slide-0.qptiff",
                            qptiff.QPTIFF_CHANNEL_NAMES: "Opal 520,Opal 620,DAPI",
                            qptiff.QPTIFF_N_CHANNELS: 3,
                            vocab.IMAGE_TYPE: vocab.MULTIPLEX})
    fake_lamindb["slide"] = slide
    written = check_qptiffs._refresh_slide(_artifact(), MARKS)
    assert written == [qptiff.QPTIFF_CHANNEL_NAMES]
    assert slide.features.values[qptiff.QPTIFF_CHANNEL_NAMES] == "CD8,PD-L1,DAPI"
    # remove-then-add, so the feature holds one answer
    assert slide.features.removed == [qptiff.QPTIFF_CHANNEL_NAMES]


def test_a_confirmed_modality_is_never_overwritten(fake_lamindb) -> None:
    slide = _Holder(values={"source_image": "proj/slide-0.qptiff",
                            vocab.IMAGE_TYPE: vocab.MULTIPLEX})
    fake_lamindb["slide"] = slide
    written = check_qptiffs._refresh_slide(_artifact(), MARKS)
    assert qptiff.QPTIFF_CHANNEL_NAMES in written      # the corrected value still lands
    assert vocab.IMAGE_TYPE not in written             # the modality does not
    assert slide.features.values[vocab.IMAGE_TYPE] == vocab.MULTIPLEX
    assert vocab.IMAGE_TYPE not in slide.features.removed


def test_a_slide_minted_from_a_different_file_is_left_alone(fake_lamindb) -> None:
    slide = _Holder(values={"source_image": "other_project/slide-0.qptiff"})
    fake_lamindb["slide"] = slide
    assert check_qptiffs._refresh_slide(_artifact(), MARKS) == []
    assert slide.features.added == []
    assert slide.features.removed == []


def test_a_qptiff_with_no_slide_is_not_an_error(fake_lamindb) -> None:
    fake_lamindb["slide"] = None
    assert check_qptiffs._refresh_slide(_artifact(), MARKS) == []


def test_nothing_is_written_when_the_slide_already_agrees(fake_lamindb) -> None:
    slide = _Holder(values={"source_image": "proj/slide-0.qptiff",
                            qptiff.QPTIFF_CHANNEL_NAMES: "CD8,PD-L1,DAPI",
                            qptiff.QPTIFF_N_CHANNELS: 3})
    fake_lamindb["slide"] = slide
    assert check_qptiffs._refresh_slide(_artifact(), MARKS) == []
    assert slide.features.added == []
    assert slide.features.removed == []


def test_the_refresh_writes_only_what_mint_slides_copies(fake_lamindb) -> None:
    slide = _Holder(values={"source_image": "proj/slide-0.qptiff"})
    fake_lamindb["slide"] = slide
    check_qptiffs._refresh_slide(_artifact(), MARKS)
    written = set().union(*(set(a) for a in slide.features.added)) if slide.features.added else set()
    assert written <= set(mint_slides.slide_copied_features())
    assert qptiff.QPTIFF_METADATA not in written
    assert not written & mint_slides.SLIDE_REFRESH_EXCLUDED


# reader_predates_locus
def test_metadata_without_a_locus_means_an_old_reader() -> None:
    assert check_qptiffs.reader_predates_locus({qptiff.QPTIFF_BARCODE: "BC-001"}) is True


def test_an_unknown_locus_still_counts_as_the_fixed_reader() -> None:
    assert check_qptiffs.reader_predates_locus(
        {qptiff.QPTIFF_CHANNEL_LOCUS: qptiff.LOCUS_UNKNOWN}) is False


def test_a_placed_file_is_not_mistaken_for_an_old_reader() -> None:
    assert check_qptiffs.reader_predates_locus(
        {qptiff.QPTIFF_CHANNEL_LOCUS: qptiff.LOCUS_PER_PAGE_ROOT}) is False
