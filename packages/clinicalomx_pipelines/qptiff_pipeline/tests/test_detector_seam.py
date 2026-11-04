"""The detect flows consume an ``ObjectDetector`` and hold no framework knowledge.

Every detector here is a fake with no framework behind it. The geo stack is stubbed, so the test
runs without the ``review`` extra. Tests of the seam itself live in ``clinicalomx_ml``.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import types
from dataclasses import dataclass

import pytest

from conftest import SRC, _stub_packages, _stub_prefect


@dataclass
class FakeDetection:
    """What an `ObjectDetector` returns. Not the real class: the flows must not care."""

    class_id: int
    class_name: str
    confidence: float
    xyxy: tuple[float, float, float, float]


class FakeDetector:
    """An `ObjectDetector` with only `names` and `predict`."""

    def __init__(self, dets: list[FakeDetection]):
        self.names = {0: "TMA", 1: "tissue_section"}
        self._dets = dets
        self.calls: list[dict] = []

    def predict(self, image, conf: float = 0.25, imgsz: int | None = None):
        self.calls.append({"image": image, "conf": conf, "imgsz": imgsz})
        return list(self._dets)


class _RecordingGdf(list):
    """Stands in for a GeoDataFrame: keeps the rows and the column list it was built with."""

    def __init__(self, rows, columns=None, geometry=None):
        super().__init__(rows)
        self.columns = columns
        self.geometry_col = geometry
        self.attrs: dict = {}

    def reset_index(self, drop=False):
        return self


def _stub_geo(monkeypatch):
    """Register recording stand-ins for geopandas and shapely.geometry."""
    gpd = types.ModuleType("geopandas")
    gpd.GeoDataFrame = _RecordingGdf
    monkeypatch.setitem(sys.modules, "geopandas", gpd)

    shapely = types.ModuleType("shapely")
    shapely.__path__ = []
    geometry = types.ModuleType("shapely.geometry")
    geometry.box = lambda *xyxy: ("box", tuple(float(v) for v in xyxy))
    shapely.geometry = geometry
    monkeypatch.setitem(sys.modules, "shapely", shapely)
    monkeypatch.setitem(sys.modules, "shapely.geometry", geometry)


def _stub_objective(monkeypatch, dotted: str, module: types.ModuleType):
    """Register ``dotted`` and every package above it, so a flow's deferred import resolves."""
    parts = dotted.split(".")
    for i in range(1, len(parts)):
        name = ".".join(parts[:i])
        if name not in sys.modules:
            pkg = types.ModuleType(name)
            pkg.__path__ = []
            monkeypatch.setitem(sys.modules, name, pkg)
        if i > 1:
            monkeypatch.setattr(sys.modules[".".join(parts[: i - 1])], parts[i - 1],
                                sys.modules[name], raising=False)
    monkeypatch.setitem(sys.modules, dotted, module)
    monkeypatch.setattr(sys.modules[".".join(parts[:-1])], parts[-1], module, raising=False)


def _load_flow(monkeypatch, name: str):
    """Load a flow module fresh, so it binds this test's stubs."""
    from conftest import _FLOWS, _load

    _stub_packages()
    _stub_prefect()
    monkeypatch.delitem(sys.modules, f"qptiff_pipeline.flows.{name}", raising=False)
    mod = _load(f"qptiff_pipeline.flows.{name}", _FLOWS / f"{name}.py")
    monkeypatch.delitem(sys.modules, f"qptiff_pipeline.flows.{name}", raising=False)
    return mod


# general-detect


@pytest.fixture
def general(monkeypatch):
    """``general_detect`` plus a recorder for its suppression call."""
    _stub_geo(monkeypatch)
    seen: list[dict] = []

    detect = types.ModuleType("croi.detection.detector")

    def suppress_overlaps(dets, *, iou_thr=0.6, ios_thr=0.9, class_agnostic=True):
        seen.append({"n": len(dets), "iou_thr": iou_thr, "class_agnostic": class_agnostic})
        return dets

    detect.suppress_overlaps = suppress_overlaps
    _stub_objective(monkeypatch, "croi.detection.detector", detect)
    return _load_flow(monkeypatch, "general_detect"), seen


def test_general_builds_its_rows_from_detection_fields(general):
    mod, _ = general
    det = FakeDetector([
        FakeDetection(0, "TMA", 0.91, (10.0, 20.0, 30.0, 40.0)),
        FakeDetection(1, "tissue_section", 0.42, (1.0, 2.0, 3.0, 4.0)),
    ])

    gdf = mod._detect_rois(det, "thumbnail-array", conf=0.3, nms_iou=0.6)

    assert list(gdf) == [
        {"class": "TMA", "conf": 0.91, "geometry": ("box", (10.0, 20.0, 30.0, 40.0))},
        {"class": "tissue_section", "conf": 0.42, "geometry": ("box", (1.0, 2.0, 3.0, 4.0))},
    ]
    assert gdf.columns == ["class", "conf", "geometry"]
    assert gdf.geometry_col == "geometry"


def test_general_asks_the_detector_for_detections_and_nothing_else(general):
    mod, _ = general
    det = FakeDetector([FakeDetection(0, "TMA", 0.5, (0.0, 0.0, 1.0, 1.0))])

    mod._detect_rois(det, "img", conf=0.3, nms_iou=0.6)

    assert det.calls == [{"image": "img", "conf": 0.3, "imgsz": mod.DETECT_IMGSZ}]


def test_general_applies_model_agnostic_suppression(general):
    mod, seen = general
    det = FakeDetector([FakeDetection(0, "TMA", 0.5, (0.0, 0.0, 1.0, 1.0))])

    mod._detect_rois(det, "img", conf=0.3, nms_iou=0.55)

    assert seen == [{"n": 1, "iou_thr": 0.55, "class_agnostic": True}]


def test_general_default_nms_iou_cannot_collapse_every_box(general):
    mod, _ = general
    import inspect

    # Unwrap the real @flow when prefect is installed.
    fn = getattr(mod.general_detect, "fn", mod.general_detect)
    default = inspect.signature(fn).parameters["nms_iou"].default
    assert default == 0.6, "a model-agnostic NMS threshold of 0 keeps exactly one box"


def test_general_zero_detections_still_yields_a_shaped_frame(general):
    mod, _ = general
    gdf = mod._detect_rois(FakeDetector([]), "img", conf=0.3, nms_iou=0.6)
    assert list(gdf) == []
    assert gdf.columns == ["class", "conf", "geometry"]


def test_general_champion_cfg_disables_the_backends_own_containment(general):
    mod, _ = general
    assert mod.CHAMPION_CFG == {"contained_thr": 0}


# core-detect


@pytest.fixture
def core(monkeypatch):
    """``core_detect`` plus a recorder for its ``detect_cores`` call."""
    _stub_geo(monkeypatch)
    seen: list[dict] = []

    dearray = types.ModuleType("croi.objectives.tma_core.dearray")

    def detect_cores(image, detector, detection_bounding_box=None, **kw):
        seen.append({"image": image, "detector": detector, "kw": kw})
        return _RecordingGdf([{"class": "full_core", "conf": 0.8, "geometry": ("box", (0,) * 4)}])

    dearray.detect_cores = detect_cores
    _stub_objective(monkeypatch, "croi.objectives.tma_core.dearray", dearray)
    return _load_flow(monkeypatch, "core_detect"), seen


def test_core_hands_the_detector_straight_through(core):
    mod, seen = core
    det = FakeDetector([])

    out = mod._detect_cores(det, "tma-crop", conf=0.1)

    assert len(seen) == 1
    assert seen[0]["image"] == "tma-crop"
    assert seen[0]["detector"] is det
    assert seen[0]["kw"] == {"conf": 0.1}, "no framework kwargs may cross this boundary"
    assert list(out) == [{"class": "full_core", "conf": 0.8, "geometry": ("box", (0,) * 4)}]


def test_core_champion_cfg_disables_the_backends_own_containment(core):
    mod, _ = core
    assert mod.CHAMPION_CFG == {"contained_thr": 0}


# The boundary itself.

#: Attribute chains that only make sense on an ultralytics `Results.boxes` tensor.
TENSOR_CALLS = {"item", "cpu", "numpy"}


def _framework_references(path: pathlib.Path):
    """``(lineno, what)`` for every ultralytics import or tensor unpack in one module, by AST."""
    out = []
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name) for a in node.names if a.name.split(".")[0] == "ultralytics"]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "ultralytics":
                out.append((node.lineno, node.module))
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr in TENSOR_CALLS:
                out.append((node.lineno, f".{fn.attr}()"))
            elif isinstance(fn, ast.Name) and fn.id == "YOLO":
                out.append((node.lineno, "YOLO()"))
    return out


def test_no_framework_knowledge_left_in_this_package():
    hits = {
        str(p.relative_to(SRC)): refs
        for p in sorted(SRC.rglob("*.py"))
        if (refs := _framework_references(p))
    }
    assert hits == {}


#: Every detection framework the seam hides.
FRAMEWORKS = ("ultralytics", "rfdetr", "supervision")


@pytest.mark.parametrize("flow", ["general", "core"])
def test_the_detect_flows_run_with_every_framework_absent(monkeypatch, request, flow):
    for name in FRAMEWORKS:
        monkeypatch.setitem(sys.modules, name, None)
    mod, _ = request.getfixturevalue(flow)
    det = FakeDetector([FakeDetection(0, "TMA", 0.5, (0.0, 0.0, 1.0, 1.0))])
    if flow == "general":
        out = mod._detect_rois(det, "img", conf=0.3, nms_iou=0.6)
    else:
        out = mod._detect_cores(det, "tma-crop", conf=0.1)
    assert len(list(out)) == 1
