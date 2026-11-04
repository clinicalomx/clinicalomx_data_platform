"""``_reconcile_general`` does the full enforcement pass from the artifacts alone, as on the cached
path. It re-derives the coordinate space from the parquet ``.attrs`` and the PNGs from disk."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from conftest import _stub_packages, _stub_prefect


class _Art:
    def __init__(self, key, path):
        self.key, self.path, self.uid = key, path, key
        self.features = types.SimpleNamespace(get_values=dict)


class _Rec:
    def __init__(self, name, uid):
        self.name, self.uid = name, uid


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Load the flow module with every database and orchestrator call replaced by a recorder."""
    _stub_packages()
    _stub_prefect()

    calls = {"anchor": [], "link_input": [], "state": [], "context": [], "published": [],
             "markdown": 0, "table": 0}

    # a candidate parquet whose .attrs carry the coordinate space
    class _Col(list):
        """Just enough pandas: ``gdf["class"] == "TMA"`` -> a list with ``.sum()``."""

        def __eq__(self, other):
            return _Col(v == other for v in self)

        def sum(self):
            return sum(1 for v in self if v)

    class _Row(dict):
        geometry = types.SimpleNamespace(bounds=(0.0, 0.0, 1.0, 1.0))

    class _Gdf(list):
        attrs = {"scale_factor": 4.0, "thumb_w": 800, "thumb_h": 600}

        def __getitem__(self, k):
            if isinstance(k, str):
                return _Col(r[k] for r in self)
            return list.__getitem__(self, k)

        def iterrows(self):
            return enumerate(self)

    rows = [_Row({"class": "TMA", "conf": 0.9}), _Row({"class": "tissue_section", "conf": 0.5})]

    roi_io = types.ModuleType("qptiff_pipeline.domain.roi_io")
    roi_io.read_roi_parquet = lambda p: _Gdf(rows)  # attrs carry the space

    linking = types.ModuleType("qptiff_pipeline.lamin.linking")
    registry = types.ModuleType("qptiff_pipeline.lamin.registry")
    tracking = types.ModuleType("qptiff_pipeline.lamin.tracking")
    queries = types.ModuleType("qptiff_pipeline.lamin.queries")
    linking.link_input = lambda *a: calls["link_input"].append(tuple(getattr(x, "key", x) for x in a))
    registry.get_or_create_artifact = lambda *a, **k: None
    queries.select_registered_slide_uids = lambda *a, **k: []
    tracking.track_prefect_run = lambda *a, **k: None

    states = types.ModuleType("qptiff_pipeline.domain.states")
    vocab = types.ModuleType("qptiff_pipeline.domain.vocab")
    # One stub module per declarer.
    qptiff = types.ModuleType("qptiff_pipeline.domain.qptiff")
    hitl = types.ModuleType("qptiff_pipeline.domain.hitl")
    vocab.F_THUMBNAIL, vocab.F_PRED, vocab.F_DET_OVERLAY = "roi_thumbnail", "roi_pred", "roi_det"
    qptiff.INTEGRITY_FEATURE = "qptiff_integrity"
    qptiff.INTEGRITY_UNREADABLE = "unreadable"
    states.REGISTERED = "registered"
    states.S = types.SimpleNamespace(REGISTERED="registered", GENERAL_PENDING="general_pending",
                                     GENERAL_FAILED="general_failed",
                                     GENERAL_APPROVED="general_approved")

    ctx_io = types.ModuleType("qptiff_pipeline.lamin.context_io")
    ctx_io.build_detect_context = lambda stage, **kw: calls["context"].append(kw) or {"ctx": kw}
    ctx_io.write_context = lambda rec, ctx, **kw: None
    ctx_io.publish_context = lambda ctx, **kw: calls["published"].append(kw)

    # Bind each fake both in sys.modules and as a package attribute: once another test imports the
    # real submodule, `from package import name` reads the attribute.
    import clinicalomx as _cx

    monkeypatch.setattr(_cx, "set_state", lambda rec, st: calls["state"].append((rec.name, st)))
    monkeypatch.setattr(_cx, "get_state", lambda rec: calls.get("current_state"))

    monkeypatch.setattr(
        _cx, "annotate",
        lambda roi, art, feat: calls["anchor"].append((roi.name, art.key, feat)),
        raising=False,
    )

    PKG_OF = {"roi_io": "domain", "states": "domain", "vocab": "domain",
              "qptiff": "domain", "hitl": "domain", "context_io": "lamin", "linking": "lamin",
              "registry": "lamin", "tracking": "lamin", "queries": "lamin"}
    for attr, fake in (("roi_io", roi_io), ("linking", linking), ("registry", registry),
                       ("tracking", tracking), ("queries", queries),
                       ("states", states), ("vocab", vocab), ("qptiff", qptiff),
                       ("hitl", hitl), ("context_io", ctx_io)):
        monkeypatch.setitem(sys.modules, f"qptiff_pipeline.{PKG_OF[attr]}.{attr}", fake)
        monkeypatch.setattr(sys.modules[f"qptiff_pipeline.{PKG_OF[attr]}"], attr, fake,
                            raising=False)

    from conftest import _FLOWS, _load

    # Evict the cached flow module so it re-executes against this test's fakes.
    monkeypatch.delitem(sys.modules, "qptiff_pipeline.flows.general_detect", raising=False)
    mod = _load("qptiff_pipeline.flows.general_detect", _FLOWS / "general_detect.py")
    monkeypatch.delitem(sys.modules, "qptiff_pipeline.flows.general_detect", raising=False)

    # The artifact helpers bind at module import, so patch the module attribute.
    monkeypatch.setattr(mod, "create_markdown_artifact",
                        lambda **kw: calls.__setitem__("markdown", calls["markdown"] + 1))
    monkeypatch.setattr(mod, "create_table_artifact",
                        lambda **kw: calls.__setitem__("table", calls["table"] + 1))

    # PNG bytes on disk for reconcile to read back
    pred = tmp_path / "rois_pred.parquet"; pred.write_bytes(b"pq")
    thumb = tmp_path / "thumbnail.png"; thumb.write_bytes(b"\x89PNG-thumb")
    overlay = tmp_path / "detections.png"; overlay.write_bytes(b"\x89PNG-overlay")
    okeys = ("k/pred", "k/thumb", "k/overlay")
    arts = {okeys[0]: _Art(okeys[0], pred), okeys[1]: _Art(okeys[1], thumb),
            okeys[2]: _Art(okeys[2], overlay)}
    return mod, calls, okeys, arts


def _run(mod, okeys, arts):
    return mod._reconcile_general(
        _Rec("SlideA", "uid-a"), arts,
        okeys=okeys,
        qptiff=_Art("projects/SlideA.qptiff", Path("/nonexistent/SlideA.qptiff")),
        model_art=None, model_uid="m1", model_alias="production",
        conf=0.3, nms_iou=0.6, target_directory="/tmp/store",
        logger=types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None),
    )


def test_reconcile_anchors_all_three_outputs(harness):
    mod, calls, okeys, arts = harness
    _run(mod, okeys, arts)
    assert [f for _, _, f in calls["anchor"]] == ["roi_thumbnail", "roi_pred", "roi_det"]
    assert {k for _, k, _ in calls["anchor"]} == set(okeys)


def test_reconcile_sets_the_review_state(harness):
    mod, calls, okeys, arts = harness
    _run(mod, okeys, arts)
    assert calls["state"] == [("SlideA", "general_pending")]


@pytest.mark.parametrize("current", [None, "registered", "general_pending", "general_failed"])
def test_reconcile_parks_for_review_from_a_pre_review_state(harness, current):
    mod, calls, okeys, arts = harness
    calls["current_state"] = current
    _run(mod, okeys, arts)
    assert calls["state"] == [("SlideA", "general_pending")]


@pytest.mark.parametrize("current", ["general_approved", "core_pending", "bgnorm_pending", "complete"])
def test_reconcile_never_rewinds_a_gate_a_human_moved_past(harness, current):
    mod, calls, okeys, arts = harness
    calls["current_state"] = current
    _run(mod, okeys, arts)
    assert calls["state"] == [], f"reconcile rewound a slide at {current}"
    assert len(calls["anchor"]) == 3, "anchoring must still happen; only the state is left alone"


def test_reconcile_rebuilds_the_space_from_parquet_attrs(harness):
    mod, calls, okeys, arts = harness
    _run(mod, okeys, arts)
    ctx = calls["context"][0]
    assert (ctx["width"], ctx["height"], ctx["scale_factor"]) == (800, 600, 4.0)
    assert ctx["unit_name"] == "SlideA"


def test_reconcile_records_lineage_and_publishes(harness):
    mod, calls, okeys, arts = harness
    out = _run(mod, okeys, arts)
    assert calls["link_input"], "lineage was not recorded"
    assert calls["markdown"] == 1 and calls["table"] == 1
    assert calls["published"], "the editor bundle was not published"
    assert out["n_rois"] == 2 and out["n_tma"] == 1
    assert out["review_key"] == "roi-review-slidea"


def test_reconcile_survives_a_broken_review_transport(harness, monkeypatch):
    mod, calls, okeys, arts = harness
    monkeypatch.setattr(mod, "create_markdown_artifact",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("prefect down")))
    out = _run(mod, okeys, arts)
    assert len(calls["anchor"]) == 3
    assert calls["state"] == [("SlideA", "general_pending")]
    assert out["n_rois"] == 2
