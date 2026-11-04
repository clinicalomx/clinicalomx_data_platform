"""A synthetic instance the app runs against with no database.

A pre-populated :class:`~clinicalomx.snapshot.Snapshot` drives the real ``describe`` and ``plan``.
The TMA covers every report state: normalised and raw-only cores, a dangling pointer, a core with
no geometry, a denied folder, an artifact no recipe reads, and undeclared roles.
"""
from __future__ import annotations

from clinicalomx.governance import Policy, register_policies, reset_cache
from clinicalomx.snapshot import Snapshot

SLIDE_NAME = "Arutha YTMA 471-4_Scan1-16-bit"
TMA_NAME = f"{SLIDE_NAME}:TMA0"
#: A second TMA with a core sharing an id (R0C0) with one under TMA0.
TMA1_NAME = f"{SLIDE_NAME}:TMA1"
#: A subtype no foundation recipe places. It is walked and then skipped with a reason.
SECTION_NAME = f"{SLIDE_NAME}:TissueSection0"
N_CORES = 12

#: Cores 0-3 have a background-normalised image; the rest fall back to the raw qptiff.
BGNORMED_CORES = (0, 1, 2, 3)
#: A pointer whose Artifact was never registered.
DANGLING_CORE = 8
#: Registered, but under a folder no policy permits reading.
DENIED_CORE = 9
#: Has pixels but no geometry, so it cannot be placed.
UNPLACEABLE_CORE = 10
#: No artifact of its own; `raw` resolves through the slide.
EMPTY_CORE = 11
#: Carries a registered `roi_thumbnail` that no recipe reads.
UNCLAIMED_CORE = 2
UNCLAIMED_ROLE = "roi_thumbnail"

#: Cores carrying a segmentation. `SEGMENTED_TRACED` records its source; `SEGMENTED_UNTRACED`
#: does not and must report ``None``.
SEGMENTED_TRACED = 0
SEGMENTED_UNTRACED = 1
#: What each traced segmentation says it came from. Varies per core.
SEGMENTATION_SOURCE = {
    SEGMENTED_TRACED: f"bgnorm/{SLIDE_NAME}:TMA0:R0C0.zarr",
}

_PLACEABLE = {"bbox_x1": 100.0, "bbox_y1": 200.0, "bbox_x2": 1100.0, "bbox_y2": 1200.0,
              "parquet_row": 0, "scale_factor": 4.0}

#: A TMA's own ``bbox_*`` geometry. Cores have none.
_TMA_BBOX = {"bbox_x1": 50.0, "bbox_y1": 60.0, "bbox_x2": 2050.0, "bbox_y2": 2060.0,
             "scale_factor": 4.0}


class _Type:
    def __init__(self, name):
        self.name = name


class _Rec:
    """A stand-in ROI Record. Only uid, name and type are read."""

    def __init__(self, uid, name, type_name):
        self.uid, self.name, self.type = uid, name, _Type(type_name)

    def __repr__(self):
        return f"<{self.type.name} {self.name}>"


def _core_roles(index: int) -> list[tuple[str, str]]:
    if index == EMPTY_CORE:
        return []
    if index == DENIED_CORE:
        return [("bgnormed", "restricted/denied-core.zarr")]
    if index == DANGLING_CORE:
        return [("bgnormed", "bgnorm/never-registered.zarr")]
    if index in BGNORMED_CORES:
        out = [("bgnormed", f"bgnorm/{TMA_NAME}:R0C{index}.zarr"),
               ("source_image", f"raw/{SLIDE_NAME}.qptiff")]
        if index in (SEGMENTED_TRACED, SEGMENTED_UNTRACED):
            out.append(("segmentations", f"seg/{TMA_NAME}:R0C{index}-labels.zarr"))
            out.append(("cell_tables", f"seg/{TMA_NAME}:R0C{index}-cells.h5ad"))
        if index == UNCLAIMED_CORE:
            out.append((UNCLAIMED_ROLE, f"review/{TMA_NAME}:R0C{index}-thumb.png"))
        return out
    return [("source_image", f"raw/{SLIDE_NAME}.qptiff")]


def build_snapshot():
    """A Snapshot pre-filled as though the bulk reads had run. Touches no database."""
    slide = _Rec("uSLIDE", SLIDE_NAME, "WholeSlide")
    tma = _Rec("uTMA0", TMA_NAME, "TMA")
    tma1 = _Rec("uTMA1", TMA1_NAME, "TMA")
    section = _Rec("uSEC0", SECTION_NAME, "TissueSection")
    cores = [_Rec(f"uC{i:02d}", f"{TMA_NAME}:R0C{i}", "Core") for i in range(N_CORES)]
    #: Same roi_id as cores[0].
    twin = _Rec("uT1C0", f"{TMA1_NAME}:R0C0", "Core")

    values = {
        slide.uid: {"source_image": f"raw/{SLIDE_NAME}.qptiff"},
        tma.uid: {"parent_roi": SLIDE_NAME,
                  "cores_accepted": f"detections/{TMA_NAME}-grid.parquet",
                  "source_artifact": f"detections/{TMA_NAME}-grid.parquet"},
        tma1.uid: {"parent_roi": SLIDE_NAME,
                   "source_image": f"raw/{SLIDE_NAME}.qptiff",
                   **_TMA_BBOX},
        # No bbox and no parquet: a subtype nothing places.
        section.uid: {"parent_roi": SLIDE_NAME},
        twin.uid: {"parent_roi": TMA1_NAME,
                   "source_artifact": f"detections/{TMA1_NAME}-grid.parquet",
                   **_PLACEABLE, "parquet_row": 0,
                   "source_image": f"raw/{SLIDE_NAME}.qptiff"},
    }
    for i, core in enumerate(cores):
        values[core.uid] = {
            "parent_roi": TMA_NAME,
            "source_artifact": f"detections/{TMA_NAME}-grid.parquet",
            **({} if i == UNPLACEABLE_CORE else {**_PLACEABLE, "parquet_row": i}),
            **dict(_core_roles(i)),
        }

    snap = Snapshot()
    snap._roi_types = ["Core", "TMA", "TissueSection", "WholeSlide"]
    snap._records = {"WholeSlide": [slide], "TMA": [tma, tma1],
                     "TissueSection": [section], "Core": [*cores, twin]}
    snap._values = values
    snap._values_loaded = ["Core", "TMA", "TissueSection", "WholeSlide"]

    # `_role_artifacts` and `_backrefs` derive from `_values` and `_role_features`, as live.

    # `bgnorm_qc` is declared and carried by nobody (`unlinked`). `tma_shapes` and `roi_accepted`
    # are declared by nothing (`undeclared`). `roi_thumbnail` is a role no recipe reads.
    snap._role_features = ("bgnorm_qc", "bgnormed", "cell_tables", "cores_accepted",
                           "roi_thumbnail", "segmentations")
    # What the instance declares. `cell_tables` is registered on a core and read by no recipe.
    snap._artifact_features = ("bgnormed", "cell_tables", "cores_accepted", "roi_parquet",
                               "roi_thumbnail", "segmentations", "source_artifact",
                               "source_image")

    # Row lookups are served from here. An unregistered key is absent, so it reads `dangling`.
    snap.artifact_rows = lambda keys, columns: {
        k: {"roi_uid": "x", "roi_name": "y", "roi_type": "Core"}
        for k in keys if "never-registered" not in k}

    # Artifact lineage. Only the traced core has it.
    provenance = {f"seg/{TMA_NAME}:R0C{i}-labels.zarr": (src, "source_artifact")
                  for i, src in SEGMENTATION_SOURCE.items()}
    snap.artifact_provenance = lambda keys: {
        k: provenance[k] for k in keys if k in provenance}
    return snap


def install_governance() -> None:
    """Permit the demo's folders, except ``restricted/``."""
    reset_cache(registrations=True)
    register_policies([
        Policy("raw", True, True, False, True, "demo fixture"),
        Policy("bgnorm", True, True, False, True, "demo fixture"),
        Policy("detections", True, True, False, True, "demo fixture"),
        Policy("extracts", True, True, False, True, "demo fixture"),
        Policy("review", True, True, False, True, "demo fixture"),
        Policy("seg", True, True, False, True, "demo fixture"),
        Policy("restricted", False, False, False, False,
               "demo fixture: a collaborator agreement that forbids reading"),
    ])


def rois(snapshot) -> list:
    """Every ROI in the fixture, coarsest first."""
    return [r for t in ("WholeSlide", "TMA", "Core") for r in snapshot.records(t)]
