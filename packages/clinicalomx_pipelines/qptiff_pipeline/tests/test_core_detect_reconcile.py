"""``_reconcile_core`` works from the artifacts alone, as on the cached path.

The rebuilt space carries ``crop_x``/``crop_y``. A TMA past core review is never rewound.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from conftest import _FLOWS, _load, _stub_packages, _stub_prefect


class _Art:
    def __init__(self, key, path):
        self.key, self.path, self.uid = key, path, key


class _Rec:
    def __init__(self, name, uid):
        self.name, self.uid = name, uid


@pytest.fixture
def harness(tmp_path, monkeypatch):
    _stub_packages()
    _stub_prefect()
    calls = {"anchor": [], "link_input": [], "state": [], "context": [], "published": [],
             "markdown": 0, "table": 0, "current_state": None}

    class _Col(list):
        def __eq__(self, other):
            return _Col(v == other for v in self)

    class _Row(dict):
        geometry = types.SimpleNamespace(bounds=(0.0, 0.0, 10.0, 10.0))

    class _Gdf(list):
        # the crop space compute stored; reconcile reads it back from here
        attrs = {"scale_factor": 8.0, "crop_x": 120, "crop_y": 340,
                 "thumb_w": 512, "thumb_h": 480}

        def __getitem__(self, k):
            return _Col(r[k] for r in self) if isinstance(k, str) else list.__getitem__(self, k)

        def iterrows(self):
            return enumerate(self)

    rows = [_Row({"class": "full_core", "conf": 0.9}), _Row({"class": "partial_core", "conf": 0.4})]

    roi_io = types.ModuleType("qptiff_pipeline.domain.roi_io")
    roi_io.read_roi_parquet = lambda p: _Gdf(rows)
    roi_io.write_roi_parquet = lambda g, p: None

    linking = types.ModuleType("qptiff_pipeline.lamin.linking")
    registry = types.ModuleType("qptiff_pipeline.lamin.registry")
    tracking = types.ModuleType("qptiff_pipeline.lamin.tracking")
    queries = types.ModuleType("qptiff_pipeline.lamin.queries")
    linking.link_input = lambda *a: calls["link_input"].append(len(a))
    registry.get_or_create_artifact = lambda *a, **k: None
    tracking.track_prefect_run = lambda *a, **k: None

    states = types.ModuleType("qptiff_pipeline.domain.states")
    vocab = types.ModuleType("qptiff_pipeline.domain.vocab")
    hitl = types.ModuleType("qptiff_pipeline.domain.hitl")
    vocab.F_THUMBNAIL, vocab.F_PRED, vocab.F_DET_OVERLAY = "roi_thumbnail", "roi_pred", "roi_det"
    states.S = types.SimpleNamespace(REGISTERED="registered", CORE_PENDING="core_pending",
                                     CORE_FAILED="core_failed", CORE_APPROVED="core_approved")
    hitl.REVIEW_PREFIX_BY_STAGE = {"core": "core-review-"}
    states.STAGE_ENTRY = {"core": ("TMA", "registered")}

    ctx_io = types.ModuleType("qptiff_pipeline.lamin.context_io")
    ctx_io.build_detect_context = lambda stage, **kw: calls["context"].append(kw) or {"c": kw}
    ctx_io.write_context = lambda rec, ctx, **kw: None
    ctx_io.publish_context = lambda ctx, **kw: calls["published"].append(kw)

    import clinicalomx as _cx

    monkeypatch.setattr(_cx, "set_state", lambda rec, st: calls["state"].append((rec.name, st)))
    monkeypatch.setattr(_cx, "get_state", lambda rec: calls["current_state"])

    monkeypatch.setattr(
        _cx, "annotate",
        lambda roi, art, feat: calls["anchor"].append((roi.name, art.key, feat)),
        raising=False,
    )

    PKG_OF = {"roi_io": "domain", "states": "domain", "vocab": "domain",
              "hitl": "domain", "context_io": "lamin", "linking": "lamin",
              "registry": "lamin", "tracking": "lamin", "queries": "lamin"}
    for attr, fake in (("roi_io", roi_io), ("linking", linking), ("registry", registry),
                       ("tracking", tracking), ("queries", queries),
                       ("states", states), ("vocab", vocab),
                       ("hitl", hitl), ("context_io", ctx_io)):
        monkeypatch.setitem(sys.modules, f"qptiff_pipeline.{PKG_OF[attr]}.{attr}", fake)
        monkeypatch.setattr(sys.modules[f"qptiff_pipeline.{PKG_OF[attr]}"], attr, fake,
                            raising=False)

    monkeypatch.delitem(sys.modules, "qptiff_pipeline.flows.core_detect", raising=False)
    mod = _load("qptiff_pipeline.flows.core_detect", _FLOWS / "core_detect.py")
    monkeypatch.delitem(sys.modules, "qptiff_pipeline.flows.core_detect", raising=False)
    monkeypatch.setattr(mod, "create_markdown_artifact",
                        lambda **kw: calls.__setitem__("markdown", calls["markdown"] + 1))
    monkeypatch.setattr(mod, "create_table_artifact",
                        lambda **kw: calls.__setitem__("table", calls["table"] + 1))

    okeys = ("k/pred", "k/thumb", "k/overlay")
    arts = {}
    for key, name in zip(okeys, ("cores_pred.parquet", "thumbnail.png", "detections.png")):
        f = tmp_path / name
        f.write_bytes(b"\x89PNG")
        arts[key] = _Art(key, f)
    return mod, calls, okeys, arts


def _run(mod, okeys, arts):
    return mod._reconcile_core(
        _Rec("SlideA:TMA3", "uid-tma3"), arts,
        okeys=okeys,
        qptiff=_Art("projects/SlideA.qptiff", Path("/nonexistent")),
        roi_art=_Art("k/rois.parquet", Path("/nonexistent")),
        model_art=None, model_uid="m1", model_alias="production", conf=0.1,
        review_prefix="core-review-", target_directory="/tmp/store",
        logger=types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None),
    )


def test_reconcile_anchors_all_three_outputs(harness):
    mod, calls, okeys, arts = harness
    _run(mod, okeys, arts)
    assert [f for _, _, f in calls["anchor"]] == ["roi_thumbnail", "roi_pred", "roi_det"]
    assert all(roi == "SlideA:TMA3" for roi, _, _ in calls["anchor"])


def test_reconcile_rebuilds_the_crop_space_from_parquet_attrs(harness):
    mod, calls, okeys, arts = harness
    _run(mod, okeys, arts)
    ctx = calls["context"][0]
    assert (ctx["crop_x"], ctx["crop_y"]) == (120, 340)
    assert (ctx["width"], ctx["height"], ctx["scale_factor"]) == (512, 480, 8.0)


def test_reconcile_records_lineage_with_the_scoping_roi(harness):
    mod, calls, okeys, arts = harness
    out = _run(mod, okeys, arts)
    assert calls["link_input"] == [4]  # pred_art + qptiff + roi_art + model_art
    assert out["n_dets"] == 2
    assert out["review_key"] == "core-review-slidea-tma3"


@pytest.mark.parametrize("current", [None, "registered", "core_pending", "core_failed"])
def test_reconcile_parks_from_a_pre_review_state(harness, current):
    mod, calls, okeys, arts = harness
    calls["current_state"] = current
    _run(mod, okeys, arts)
    assert calls["state"] == [("SlideA:TMA3", "core_pending")]


@pytest.mark.parametrize("current",
                         ["core_approved", "bgnorm_queued", "bgnorm_running",
                          "bgnorm_pending", "complete", "he_pending"])
def test_reconcile_never_rewinds_a_tma_past_core_review(harness, current):
    mod, calls, okeys, arts = harness
    calls["current_state"] = current
    _run(mod, okeys, arts)
    assert calls["state"] == [], f"reconcile rewound a TMA at {current}"
    assert len(calls["anchor"]) == 3, "anchoring must still run; only the state is left alone"
