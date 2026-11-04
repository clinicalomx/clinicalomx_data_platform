"""Every Artifact annotates an ROI Record, through the fake lamindb.

``cx.annotate`` writes the role on the ROI Record and the back-refs on the artifact. Role features
are multi-valued: readers use ``newest_artifact`` for the current result and ``all_artifacts`` for
teardown.
"""
from __future__ import annotations

import pytest

pytest.importorskip("streamlit")  # the fake lamindb lives in the dev harness package


@pytest.fixture()
def anchored():
    """A Core ROI with a bgnormed zarr and QC parquet annotated onto it."""
    from qptiff_pipeline.review.dev import fakes, fixtures

    fakes.install_modules()
    fakes.reset_data()

    import clinicalomx as cx
    import lamindb as ln

    from qptiff_pipeline.domain import states, vocab
    from qptiff_pipeline.review.dev.fakes import _fake_vocab_ensure

    _fake_vocab_ensure()  # declare this repo's features in the fake registry
    core_uid = next(u for u, r in fixtures.RECORDS.items() if r["type"] == states.CORE_TYPE)
    core = ln.Record.get(uid=core_uid)
    zarr = ln.Artifact("bgnorm/run1/image.zarr")
    qc = ln.Artifact("bgnorm/run1/qc.parquet")
    cx.annotate(core, qc, vocab.BGNORM_QC_FEATURE)
    cx.annotate(core, zarr, vocab.BGNORMED)
    return core, zarr, qc


def test_annotate_writes_the_back_refs_on_the_artifact(anchored) -> None:
    core, zarr, _qc = anchored
    from clinicalomx.names import FEATURES

    back = zarr.features.get_values()
    assert back[FEATURES.ROI_UID] == core.uid
    assert back[FEATURES.ROI_NAME] == core.name
    assert back[FEATURES.ROI_TYPE] == "Core"


def test_annotate_writes_the_role_on_the_roi(anchored) -> None:
    core, zarr, qc = anchored
    from clinicalomx import all_artifacts

    from qptiff_pipeline.domain import vocab

    vals = core.features.get_values()
    assert zarr.key in {a.key for a in all_artifacts(vals[vocab.BGNORMED])}
    assert qc.key in {a.key for a in all_artifacts(vals[vocab.BGNORM_QC_FEATURE])}


def test_resolve_roi_finds_it(anchored) -> None:
    import clinicalomx as cx

    core, zarr, _qc = anchored
    assert cx.resolve_roi(zarr).uid == core.uid
    assert cx.require_roi(zarr).name == core.name


def test_unanchored_artifact_resolves_to_none() -> None:
    import clinicalomx as cx

    from qptiff_pipeline.review.dev import fakes

    fakes.install_modules()
    import lamindb as ln

    assert cx.resolve_roi(ln.Artifact("nowhere/orphan.zarr")) is None
    with pytest.raises(Exception, match="annotates no ROI"):
        cx.require_roi(ln.Artifact("nowhere/orphan.zarr"))


def test_newest_vs_all_artifacts() -> None:
    from qptiff_pipeline.review.dev import fakes

    fakes.install_modules()
    import lamindb as ln
    from clinicalomx import all_artifacts, newest_artifact

    a, b = ln.Artifact("bgnorm/run1/image.zarr"), ln.Artifact("bgnorm/run2/image.zarr")
    assert newest_artifact(None) is None and all_artifacts(None) == []
    assert newest_artifact(a) is a                     # a bare Artifact
    assert newest_artifact([a, b]) is b  # a list -> the newest
    assert all_artifacts([a, b]) == [a, b]             # every version
    assert newest_artifact("bgnorm/run1/image.zarr") is a   # a bare KEY string resolves


# 1:N parentage, as the persist flows write it with cx.split.
def test_split_writes_parent_roi_on_each_child() -> None:
    import clinicalomx as cx
    import lamindb as ln
    from clinicalomx.names import FEATURES

    from qptiff_pipeline.domain import states, vocab
    from qptiff_pipeline.review.dev import fakes, fixtures
    from qptiff_pipeline.review.dev.fakes import _fake_vocab_ensure

    fakes.install_modules()
    fakes.reset_data()
    _fake_vocab_ensure()

    slide_uid = next(u for u, r in fixtures.RECORDS.items() if r["type"] == states.SLIDE_TYPE)
    slide = ln.Record.get(uid=slide_uid)
    tmas = [ln.Record.get(uid=u) for u, r in fixtures.RECORDS.items()
            if r["type"] == states.TMA_TYPE][:2]
    assert len(tmas) == 2

    cand = ln.Artifact("predictions/general/rois_pred.parquet")
    parent, children = cx.split(cand, [(t, None) for t in tmas],
                                vocab.F_ROI_ACCEPTED, parent=slide)

    assert parent.uid == slide.uid
    assert {c.uid for c in children} == {t.uid for t in tmas}
    for tma in tmas:
        assert tma.features.get_values()[FEATURES.PARENT_ROI] is slide


def test_shared_parquet_is_anchored_to_the_parent_not_the_children() -> None:
    import clinicalomx as cx
    import lamindb as ln
    from clinicalomx.names import FEATURES

    from qptiff_pipeline.domain import states, vocab
    from qptiff_pipeline.review.dev import fakes, fixtures
    from qptiff_pipeline.review.dev.fakes import _fake_vocab_ensure

    fakes.install_modules()
    fakes.reset_data()
    _fake_vocab_ensure()

    slide_uid = next(u for u, r in fixtures.RECORDS.items() if r["type"] == states.SLIDE_TYPE)
    slide = ln.Record.get(uid=slide_uid)
    parquet = ln.Artifact("accepted-run1/rois.parquet")
    cx.annotate(slide, parquet, vocab.F_ROI_ACCEPTED)

    assert parquet.features.get_values()[FEATURES.ROI_UID] == slide.uid
    assert cx.resolve_roi(parquet).uid == slide.uid


def test_annotate_is_idempotent(anchored) -> None:
    import lamindb as ln
    from clinicalomx import all_artifacts, annotate

    from qptiff_pipeline.domain import vocab

    core, zarr, _qc = anchored
    before = all_artifacts(core.features.get_values().get(vocab.BGNORMED))

    annotate(core, zarr, vocab.BGNORMED)          # same link again
    annotate(core, zarr, vocab.BGNORMED)
    after = all_artifacts(core.features.get_values().get(vocab.BGNORMED))
    assert [a.key for a in after] == [a.key for a in before], "re-annotating duplicated the role"

    # a new artifact still accrues
    second = ln.Artifact("bgnorm/run2/image.zarr")
    annotate(core, second, vocab.BGNORMED)
    keys = {a.key for a in all_artifacts(core.features.get_values().get(vocab.BGNORMED))}
    assert {"bgnorm/run1/image.zarr", "bgnorm/run2/image.zarr"} <= keys
    assert len(keys) == len(before) + 1, "exactly one new artifact should have accrued"
