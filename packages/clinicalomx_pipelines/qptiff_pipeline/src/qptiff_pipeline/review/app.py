"""ROI review app (Streamlit): the human review screen for the detection gates.

General reviews WholeSlides in ``general_pending``; Core reviews TMAs in ``core_pending``. The
reviewer edits the boxes from the detect flow's Prefect edit bundle, then accepts (the persist flow
registers the child Records) or rejects (terminal). Runs on the VM:

    PREFECT_API_URL=http://localhost:4200/api LAMIN_API_KEY=... \
        <vm-process venv>/bin/streamlit run \
        /home/ubuntu/clinicalomx_pipeline/src/qptiff_pipeline/review/app.py
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import logging
import re
import traceback
from datetime import UTC
from pathlib import Path

import streamlit as st
from clinicalomx import newest_artifact

from qptiff_pipeline.domain import hitl, keys, states, vocab
from qptiff_pipeline.domain import qptiff as qptiff_vocab
from qptiff_pipeline.orchestration import gate
from qptiff_pipeline.review import stage_nav
from qptiff_pipeline.review.grid_links import apply_grid_links, grid_params
from qptiff_pipeline.review.roi_canvas import roi_canvas

MAX_DISPLAY_W = 900  # canvas is shown at most this wide; larger images scale down

# Logging: to the container's stderr (`docker logs`). Errors also show in the UI.
log = logging.getLogger("review")  # children inherit the handler


def _install_log_capture() -> None:
    """Send INFO logs from every library to stderr. Idempotent across Streamlit reruns."""
    root = logging.getLogger()
    if not any(getattr(h, "_cro_review", False) for h in root.handlers):
        h = logging.StreamHandler()
        h._cro_review = True  # marker, so reruns do not stack handlers
        h.setFormatter(
            logging.Formatter("%(asctime)s  %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
        )
        root.addHandler(h)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    log.setLevel(logging.INFO)


def _log_exc(context: str, exc: BaseException) -> None:
    """Log an exception WITH its traceback under a human-readable context."""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log.error("%s: %s: %s\n%s", context, type(exc).__name__, exc, tb)


@contextlib.contextmanager
def _logged(context: str, *, reraise: bool = True):
    """Run a block, logging entry and any exception with its traceback.

    ``reraise=False`` swallows the exception after logging. The default re-raises.
    """
    log.info("%s ...", context)
    try:
        yield
    except Exception as exc:
        _log_exc(context, exc)
        if reraise:
            raise


# class -> stroke colour, by class order.
_PALETTE = ["#00ffff", "#ffff00", "#ff00ff", "#00ff00", "#ff8000"]

# Pulsing placeholder shown while an element loads. Injected once in main().
SKELETON_CSS = """
<style>
@keyframes cro-pulse { 0%,100% { opacity: 1 } 50% { opacity: .4 } }
.cro-skeleton {
  width: 100%; border-radius: 10px; box-sizing: border-box;
  background: rgba(140,140,140,0.22);
  animation: cro-pulse 1.2s ease-in-out infinite;
  display: flex; align-items: center; justify-content: center;
}
.cro-skeleton-label { color: rgba(130,130,130,0.95); font-size: 0.9rem; }
</style>
"""


def _skeleton(height_px: int, label: str = "Loading...") -> str:
    """HTML for a pulsing content-placeholder box of the given height."""
    return (
        f"<div class='cro-skeleton' style='height:{height_px}px'>"
        f"<span class='cro-skeleton-label'>{label}</span></div>"
    )


def _ensure_canvas_compat() -> None:
    """Patch ``streamlit.elements.image.image_to_url`` for streamlit-drawable-canvas 0.9.3.

    Newer Streamlit moved the function and changed its signature.
    """
    import streamlit.elements.image as st_image_mod
    from streamlit.elements.lib.image_utils import image_to_url as real
    from streamlit.elements.lib.layout_utils import LayoutConfig

    def adapter(image, width, clamp, channels, output_format, image_id):
        return real(image, LayoutConfig(width=10_000_000), clamp, channels, output_format, image_id)

    st_image_mod.image_to_url = adapter


def _colors(stage: str) -> tuple[dict, dict]:
    """(class->colour, colour->class) for a stage's editable classes."""
    classes = hitl.CLASSES_BY_STAGE[stage]
    cc = {c: _PALETTE[i % len(_PALETTE)] for i, c in enumerate(classes)}
    return cc, {v.lower(): k for k, v in cc.items()}




def _set_feature(record, name: str, value) -> None:
    """Overwrite a single-valued Record feature (clear then add)."""
    if record.features.get_values().get(name) is not None:
        record.features.remove_values(name)
    record.features.add_values({name: value})


def _resolve_record(value, type_name: str):
    """Resolve a Record-typed feature value (a Record or its name) to a Record, or None."""
    import lamindb as ln

    if value is None or hasattr(value, "features"):
        return value
    ut = ln.Record.filter(name=type_name, is_type=True).one_or_none()
    return ln.Record.filter(name=str(value), type=ut).first() if ut is not None else None


def _fresh_db() -> None:
    """Drop pooled DB connections, so the next query sees rows the HPC committed."""
    from django.db import connections

    for conn in connections.all():
        conn.close()


@st.cache_data(ttl=30, show_spinner="Loading review queue...")
def _unit_states(stage: str) -> list[tuple[str, str, str | None]]:
    """(uid, name, state) for every unit of the stage's type, in one bulk read.

    Cached per stage and cleared on a decision or refresh.
    """
    import lamindb as ln
    import pandas as pd
    from clinicalomx import STATE_FEATURE as feat

    _fresh_db()
    unit_type = ln.Record.filter(name=states.GATES[stage].unit_type, is_type=True).one()
    try:
        df = ln.Record.filter(type=unit_type).to_dataframe(features=[feat], limit=1_000_000)
        if feat not in df.columns:
            return [(u, n, None) for u, n in zip(df["uid"], df["name"])]
        return [(u, n, None if pd.isna(s) else str(s)) for u, n, s in zip(df["uid"], df["name"], df[feat])]
    except Exception:  # fall back to a per-record scan
        from clinicalomx import get_state

        return [(r.uid, r.name, get_state(r)) for r in ln.Record.filter(type=unit_type)]


@st.cache_data(ttl=30, show_spinner=False)
def _unit_folders(stage: str) -> dict[str, str]:
    """``{unit uid: source folder}`` for the stage's units, in one bulk read.

    The folder is the first segment of the unit's ``source_image`` key. Only WholeSlides carry it,
    so this is empty for other stages. Handles both a key string and an Artifact.
    """
    import lamindb as ln
    from clinicalomx import governance as gov

    _fresh_db()
    try:
        unit_type = ln.Record.filter(name=states.GATES[stage].unit_type, is_type=True).one()
        df = ln.Record.filter(type=unit_type).to_dataframe(
            features=["source_image"], limit=1_000_000)
        if "source_image" not in df.columns:
            return {}
        out = {}
        for uid, src in zip(df["uid"], df["source_image"]):
            key = getattr(src, "key", src)
            if isinstance(key, str) and key:
                out[uid] = gov.folder_of(key)
        return out
    except Exception as exc:  # a filter never takes the review queue down
        log.info("could not read source folders for %s: %s", stage, exc)
        return {}


def _filter_by_source(stage: str, units: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Narrow the pending list by source folder and by governance level. Both default to all."""
    from qptiff_pipeline.domain import governance as declared

    folders = _unit_folders(stage)
    if not folders:
        return units

    declared.register()  # level labels resolve through the framework's table
    present = sorted({folders.get(uid, "") for uid, _n in units} - {""})
    if not present:
        return units
    levels = {f: declared.level(f) for f in present}

    c1, c2 = st.columns(2)
    pick_folders = c1.multiselect(
        "Project folder", present, default=present, key=f"filter-folder-{stage}",
        help="The drop-box the scan arrived in - the first path segment of its source key.")
    pick_levels = c2.multiselect(
        "Governance", sorted(set(levels.values())), default=sorted(set(levels.values())),
        key=f"filter-gov-{stage}",
        help="What the folder's policy permits. 'denied' means no policy is declared for it.")

    kept = [
        (uid, name) for uid, name in units
        if (f := folders.get(uid)) in pick_folders and levels.get(f) in pick_levels
    ]
    if len(kept) != len(units):
        st.caption(f"Filtered to **{len(kept)}** of {len(units)} "
                   f"({', '.join(f'{f}: {levels[f]}' for f in sorted(pick_folders))})")
    return kept


def pending_units(stage: str) -> list[tuple[str, str]]:
    """(uid, name) of units currently at the stage's pending state."""
    pending = states.GATES[stage].pending
    return [(uid, name) for uid, name, s in _unit_states(stage) if s == pending]


def decided_units(stage: str) -> list[tuple[str, str, str]]:
    """(uid, name, state) of units already accepted or rejected, which can be reopened."""
    g = states.GATES[stage]
    return [(uid, name, s) for uid, name, s in _unit_states(stage) if s in (g.approved, g.rejected)]


# The triage board. A lane is draggable: dropping a card runs `set_state`, so every lane state must
# be non-terminal.
TRIAGE_LANES: tuple[tuple[str, states.S], ...] = (
    ("Unsorted (awaiting triage)", states.S.CORE_APPROVED),
    ("bgnorm queue", states.S.BGNORM_QUEUED),
    ("Running - bgnorm_running (drag ➜ queue to re-queue)", states.S.BGNORM_RUNNING),
    ("H&E / no bgnorm", states.S.HE_PENDING),
    ("Done - bgnorm_pending (drag ➜ queue to rerun)", states.S.BGNORM_PENDING),
)

# Terminal states shown on the board but not draggable. The only way back is ``gate.reopen``.
TRIAGE_REOPEN_STATES: tuple[states.S, ...] = (states.S.BGNORM_FAILED,)

#: Every state the triage board shows, lanes and terminal. The queue badge counts all of them.
TRIAGE_STATES: tuple[states.S, ...] = tuple(s for _h, s in TRIAGE_LANES) + TRIAGE_REOPEN_STATES

# Checked at import: a terminal lane would write a state nothing can leave.
if _bad := [s for _h, s in TRIAGE_LANES if s in states.TERMINAL_STATES]:
    raise ValueError(f"triage lanes must be non-terminal (drag = set_state); got {_bad}")
if _bad := [s for s in TRIAGE_REOPEN_STATES if s not in states.TERMINAL_STATES]:
    raise ValueError(f"triage reopen states must be terminal; got {_bad}")
if _bad := sorted({s for _h, s in TRIAGE_LANES} & set(TRIAGE_REOPEN_STATES)):
    raise ValueError(f"a state is both a lane and a reopen state; got {_bad}")


def _stage_counts() -> dict[str, int]:
    """{stage: units waiting on it}, for the selector's queue badges. Never raises."""
    try:
        return {
            "general": len(pending_units("general")),
            "core": len(pending_units("core")),
            "triage": sum(1 for _u, _n, s in _unit_states("core") if s in TRIAGE_STATES),
            "bgnorm": len(pending_units("bgnorm")),
        }
    except Exception as exc:
        log.info("stage counts unavailable: %s", exc)
        return {}


@st.cache_data(ttl=300, show_spinner="Loading unit...")
def load_bundle(edit_prefix: str, name: str) -> dict | None:
    """Fetch one unit's edit bundle (clean image, boxes, scale). Cached per (prefix, name)."""
    from prefect.client.orchestration import get_client
    from prefect.client.schemas.filters import ArtifactFilter, ArtifactFilterKey
    from prefect.client.schemas.sorting import ArtifactSort

    key = edit_prefix + keys.slug(name)
    with get_client(sync_client=True) as client:
        # Newest first: a detect re-run adds a new version of this keyed artifact.
        arts = client.read_artifacts(
            artifact_filter=ArtifactFilter(key=ArtifactFilterKey(any_=[key])),
            sort=ArtifactSort.CREATED_DESC,
            limit=1,
        )
    if not arts or not arts[0].data:
        return None
    m = re.search(r"```json\s*(\{.*\})\s*```", arts[0].data, re.DOTALL)
    return json.loads(m.group(1)) if m else None


@st.cache_data(ttl=300, show_spinner=False)
def _bgnorm_qc_rows(core_name: str) -> list[dict] | None:
    """A core's latest bgnorm QC metrics, one row per channel, or None.

    Read from the ``bgnorm-qc-<core-slug>`` Prefect table artifact. This app cannot read the HPC.
    """
    from prefect.client.orchestration import get_client
    from prefect.client.schemas.filters import ArtifactFilter, ArtifactFilterKey
    from prefect.client.schemas.sorting import ArtifactSort

    key = hitl.BGNORM_QC_PREFIX + keys.slug(core_name)
    try:
        with get_client(sync_client=True) as client:
            arts = client.read_artifacts(
                artifact_filter=ArtifactFilter(key=ArtifactFilterKey(any_=[key])),
                sort=ArtifactSort.CREATED_DESC,
                limit=1,
            )
        if not arts or not arts[0].data:
            return None
        rows = arts[0].data
        if isinstance(rows, str):  # table artifacts store the table as a JSON string
            rows = json.loads(rows)
        return rows or None
    except Exception as exc:  # never block the review panel on a lookup failure
        log.info("bgnorm QC lookup failed for %r: %s", core_name, exc)
        return None


def _initial_rects(boxes: list[dict], scale: float, class_color: dict) -> dict:
    """Candidate boxes (review px) -> one fabric group per box, in canvas px.

    Each group is a rect plus a text label, centred on the box, so the label moves with its box.
    A dearrayed cell carries ``occupied`` and ``rc``: detected cells are solid and labelled
    ``row,col``, inferred cells are dashed. Raw detections are solid and labelled ``#i class``.
    """
    objects = []
    for i, b in enumerate(boxes):
        x1, y1, x2, y2 = b["xyxy"]
        color = class_color.get(b["class"], "#ff0000")
        w, h = (x2 - x1) / scale, (y2 - y1) / scale
        cx, cy = (x1 + x2) / 2 / scale, (y1 + y2) / 2 / scale
        rc = b.get("rc")
        occupied = b.get("occupied", True)
        # Label dearrayed cells with row,col. Inferred cells are dashed.
        label = f"{rc[0]},{rc[1]}" if rc is not None else f"#{i} {b['class']}"
        rect = {
            "type": "rect",
            "originX": "center",
            "originY": "center",
            "left": 0,
            "top": 0,
            "width": w,
            "height": h,
            "fill": "rgba(0,0,0,0)",
            "stroke": color,
            "strokeWidth": 2,
        }
        if not occupied:
            rect["strokeDashArray"] = [6, 4]  # inferred grid cell
        objects.append(
            {
                "type": "group",
                "originX": "center",
                "originY": "center",
                "left": cx,
                "top": cy,
                "width": w,
                "height": h,
                "scaleX": 1,
                "scaleY": 1,
                "angle": 0,
                "objects": [
                    rect,
                    # Label centred, so the group stays symmetric about the box centre.
                    {
                        "type": "text",
                        "originX": "center",
                        "originY": "center",
                        "left": 0,
                        "top": 0,
                        "text": label,
                        "fontSize": 14,
                        "fontFamily": "sans-serif",
                        "fill": color,
                    },
                ],
            }
        )
    return {"version": "4.4.0", "objects": objects}


def _rects_to_boxes(objects, scale, w, h, default_class, color_class, clamp: bool = True) -> list[dict]:
    """Fabric objects -> ``[{class, xyxy}]`` in review px.

    Handles seeded groups and bare rects the user drew. The class comes from the rect's stroke
    colour. Coords are clamped to the image unless ``clamp=False``, which returns raw canvas coords
    for out-of-bounds detection.
    """
    boxes = []
    for o in objects:
        t = o.get("type")
        if t == "group":
            rect = next((c for c in (o.get("objects") or []) if c.get("type") == "rect"), None)
            if rect is None:
                continue
            sx, sy = o.get("scaleX", 1), o.get("scaleY", 1)
            gw, gh = o.get("width", 0) * sx, o.get("height", 0) * sy
            # Group centre in display px, whatever origin fabric serialised.
            ox, oy = o.get("originX", "left"), o.get("originY", "top")
            gl, gt = o.get("left", 0), o.get("top", 0)
            cx = gl if ox == "center" else (gl - gw / 2 if ox == "right" else gl + gw / 2)
            cy = gt if oy == "center" else (gt - gh / 2 if oy == "bottom" else gt + gh / 2)
            # Rect size from the child, so label overflow cannot inflate it.
            rw = rect.get("width", o.get("width", 0)) * rect.get("scaleX", 1) * sx
            rh = rect.get("height", o.get("height", 0)) * rect.get("scaleY", 1) * sy
            x1, y1, x2, y2 = cx - rw / 2, cy - rh / 2, cx + rw / 2, cy + rh / 2
            stroke = rect.get("stroke", o.get("stroke", ""))
        elif t == "rect":
            x1 = o["left"]
            y1 = o["top"]
            x2 = o["left"] + o["width"] * o.get("scaleX", 1)
            y2 = o["top"] + o["height"] * o.get("scaleY", 1)
            stroke = o.get("stroke", "")
        else:
            continue
        x1, y1, x2, y2 = x1 * scale, y1 * scale, x2 * scale, y2 * scale
        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        if clamp:  # edges cannot cross the image bounds
            x1, x2 = min(max(x1, 0.0), w), min(max(x2, 0.0), w)
            y1, y2 = min(max(y1, 0.0), h), min(max(y2, 0.0), h)
        cls = color_class.get(str(stroke).lower(), default_class)
        boxes.append({"class": cls, "xyxy": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)]})
    return boxes


def _clamp_xyxy(xyxy, w: float, h: float) -> list[float]:
    """Clamp a ``[x1, y1, x2, y2]`` box to ``[0, w] x [0, h]`` and order its corners."""
    x1, y1, x2, y2 = xyxy
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    return [
        round(min(max(x1, 0.0), w), 1),
        round(min(max(y1, 0.0), h), 1),
        round(min(max(x2, 0.0), w), 1),
        round(min(max(y2, 0.0), h), 1),
    ]


def _boxes_equivalent(a: list[dict], b: list[dict], tol: float) -> bool:
    """True if two box lists match within ``tol`` px: same count, same classes in order.

    Absorbs the sub-pixel drift fabric adds on move and undo, for the "modified" flag.
    """
    if len(a) != len(b):
        return False
    for ba, bb in zip(a, b):
        if ba["class"] != bb["class"]:
            return False
        if any(abs(x - y) > tol for x, y in zip(ba["xyxy"], bb["xyxy"])):
            return False
    return True


def _fmt_rc(rc) -> str:
    """Grid coordinate ``[row, col]`` -> ``"r,c"``, or ``""`` when absent."""
    if not rc:
        return ""
    return f"{int(rc[0])},{int(rc[1])}"


def _with_meta(b: dict, W: float, H: float, *, clamp: bool = False, bid: int | None = None) -> dict:
    """Canonical core box dict: class, xyxy, and grid metadata (occupied, rc, bid).

    The canvas returns only class and geometry, so the metadata is kept in session state and
    re-attached by index (see `_merge_grid_meta`). ``bid`` is a stable box id that grid links
    reference. It survives a re-dearray, a delete and a re-index.
    """
    xyxy = _clamp_xyxy(b["xyxy"], W, H) if clamp else list(b["xyxy"])
    out = {"class": b["class"], "xyxy": xyxy,
           "occupied": bool(b.get("occupied", True)), "rc": b.get("rc")}
    out["bid"] = b.get("bid") if b.get("bid") is not None else bid
    return out


def _with_bids(boxes: list[dict]) -> list[dict]:
    """Give every box a bid, keeping existing ones. New ids continue past the highest."""
    nxt = max([b.get("bid", -1) for b in boxes if b.get("bid") is not None], default=-1) + 1
    out = []
    for b in boxes:
        if b.get("bid") is None:
            b = {**b, "bid": nxt}
            nxt += 1
        out.append(b)
    return out


def _merge_grid_meta(geom_boxes: list[dict], prior: list[dict]) -> list[dict]:
    """Re-attach dearray metadata (occupied, rc) to canvas readback boxes, by index.

    fabric keeps object order, so readback index i is prior index i. Newly drawn boxes are real
    detections with no ``rc``.
    """
    out = []
    for i, b in enumerate(geom_boxes):
        m = prior[i] if i < len(prior) else {}
        out.append({"class": b["class"], "xyxy": b["xyxy"],
                    "occupied": bool(m.get("occupied", True)), "rc": m.get("rc")})
    return out


@st.cache_data(show_spinner=False)
def _bg_image(image_b64: str, disp_w: int, disp_h: int):
    """Decode and resize the review image once (cached)."""
    from PIL import Image

    return Image.open(io.BytesIO(base64.b64decode(image_b64))).resize((disp_w, disp_h))


def _dedupe_overlapping(gdf, center_frac: float = 0.3):
    """Merge near-coincident detections of the same core. Returns ``(gdf, n_merged)``.

    Two boxes merge when their centres are closer than ``center_frac`` times the smaller box's
    minimum side. The higher-``conf`` box is kept. The result is index-reset.
    """
    import numpy as np

    if len(gdf) < 2:
        return gdf, 0
    b = gdf.geometry.bounds.to_numpy()
    cx, cy = 0.5 * (b[:, 0] + b[:, 2]), 0.5 * (b[:, 1] + b[:, 3])
    w, h = b[:, 2] - b[:, 0], b[:, 3] - b[:, 1]
    conf = gdf["conf"].to_numpy() if "conf" in gdf.columns else np.ones(len(gdf))
    kept: list[int] = []
    for i in np.argsort(-conf):  # highest conf first; it is the one kept
        if any(
            np.hypot(cx[i] - cx[k], cy[i] - cy[k]) < center_frac * min(w[i], h[i], w[k], h[k])
            for k in kept
        ):
            continue
        kept.append(int(i))
    kept.sort()
    return gdf.iloc[kept].reset_index(drop=True), len(gdf) - len(kept)


def _dedupe_boxes(boxes: list[dict], center_frac: float = 0.3) -> tuple[list[dict], int]:
    """Merge near-coincident boxes on plain dicts, by the rule of :func:`_dedupe_overlapping`."""
    keep: list[dict] = []
    n = 0
    for b in boxes:
        x1, y1, x2, y2 = (float(v) for v in b["xyxy"])
        cx, cy, w, h = 0.5 * (x1 + x2), 0.5 * (y1 + y2), x2 - x1, y2 - y1
        dup = False
        for k in keep:
            kx1, ky1, kx2, ky2 = (float(v) for v in k["xyxy"])
            kcx, kcy = 0.5 * (kx1 + kx2), 0.5 * (ky1 + ky2)
            lim = center_frac * min(w, h, kx2 - kx1, ky2 - ky1)
            if ((cx - kcx) ** 2 + (cy - kcy) ** 2) ** 0.5 < lim:
                dup = True
                break
        if dup:
            n += 1
        else:
            keep.append(b)
    return keep, n


def _dearray_graph(boxes: list[dict], links: list[dict] | None) -> tuple[list[dict], dict | None]:
    """Connectivity dearray: row,col from a graph walk over the detections (review.grid_graph).

    Every detection is a node, so none is dropped. The reviewer's links are zero-cost edges.
    Returns ``(cell_boxes, geometry)``, like the lattice path.
    """
    from qptiff_pipeline.review.grid_graph import assign_by_connectivity, geometry_provider

    boxes, n_merged = _dedupe_boxes(boxes)
    if n_merged:
        log.info("dearray(graph): merged %d near-duplicate detection(s)", n_merged)
        st.caption(f"Merged {n_merged} near-duplicate detection(s) before the fit.")
    xy = [b["xyxy"] for b in boxes]
    centroids = [[0.5 * (x1 + x2), 0.5 * (y1 + y2)] for x1, y1, x2, y2 in xy]
    sizes = [[x2 - x1, y2 - y1] for x1, y1, x2, y2 in xy]
    # grid_graph addresses cores by index, so translate the bid-keyed links.
    idx_of = {b["bid"]: i for i, b in enumerate(boxes) if b.get("bid") is not None}
    glinks = [
        {"a": idx_of[e["a"]], "b": idx_of[e["b"]], "axis": e.get("axis", "row")}
        for e in (links or [])
        if e.get("a") in idx_of and e.get("b") in idx_of
    ]
    g = assign_by_connectivity(centroids, sizes, links=glinks)
    for w in g.warnings:
        log.warning("dearray(graph): %s", w)
        st.warning(w)
    if not g.n_assigned:
        st.warning("Connectivity fit could not grid these cores; showing the raw detections.")
        return boxes, None

    w, h = g.cell_size
    taken = {}
    out: list[dict] = []
    for i, rc in enumerate(g.rc):
        if rc is None:
            out.append({**boxes[i], "rc": None, "occupied": True})  # kept, but not on the grid
            continue
        taken[(rc[0], rc[1])] = True
        out.append({**boxes[i], "rc": [int(rc[0]), int(rc[1])], "occupied": True})
    # Missing cells: a box at the locally predicted position.
    for r in range(g.n_rows):
        for c in range(g.n_cols):
            if (r, c) in taken:
                continue
            at = g.cell_pos.get((r, c))
            if at is None:  # grid_graph fills the whole range; skip rather than
                continue  # plant a box at the image origin
            cx, cy = float(at[0]), float(at[1])
            out.append({
                "class": "inferred_core",
                "xyxy": [round(float(cx - w / 2), 1), round(float(cy - h / 2), 1),
                         round(float(cx + w / 2), 1), round(float(cy + h / 2), 1)],
                "occupied": False,
                "rc": [r, c],
                "bid": None,
            })
    out.sort(key=lambda b: (b["rc"][0], b["rc"][1]) if b.get("rc") else (10**6, 10**6))
    log.info("dearray(graph): %dx%d grid, %d/%d detection(s) placed, %d link(s) used",
             g.n_rows, g.n_cols, g.n_assigned, len(boxes), len(glinks))
    return _with_bids(out), geometry_provider(g)


GRAPH, LATTICE = "graph", "lattice"


def _dearray(boxes: list[dict], *, method: str = GRAPH,
             links: list[dict] | None = None) -> tuple[list[dict], dict | None]:
    """Dearray: assign the core detections to grid cells and return one box per cell.

    Each box carries ``occupied`` and ``rc``. Returns ``(cell_boxes, geometry)``. The geometry is
    what "Apply grid links" reads cell positions from, and is ``None`` when no grid was fitted.

    - ``GRAPH`` (default): ``review.grid_graph``, a walk over the detections' neighbour graph.
    - ``LATTICE``: two 1D lattice fits and nearest-cell assignment (``tma_gridding``).

    Only real detections are fitted. Cells a previous fit inferred are dropped and regenerated.
    """
    n_in = len(boxes)
    boxes = [b for b in boxes if b.get("occupied", True)]
    if len(boxes) != n_in:
        log.info("dearray: excluded %d grid-inferred cell(s) from the fit (real detections only)",
                 n_in - len(boxes))
    if len(boxes) < 4:  # too few points to fit a grid
        log.info("dearray: only %d detection(s) (<4); skipping grid fit, keeping raw", len(boxes))
        return boxes, None
    if method == GRAPH:
        try:
            return _dearray_graph(boxes, links)
        except Exception as exc:
            _log_exc("dearray(graph): failed; falling back to the lattice fit", exc)
            st.warning(f"Connectivity fit failed ({exc}); fell back to the lattice fit.")
    return _dearray_lattice(boxes)


def _dearray_lattice(boxes: list[dict]) -> tuple[list[dict], dict | None]:
    """The lattice grid fit: two 1D lattices and nearest-cell assignment (``tma_gridding``).

    Falls back to the raw detections if gridding is unavailable or the fit fails.
    """
    try:
        import geopandas as gpd
        from shapely.geometry import box as _box
        # Prefer croi's estimator, else the vendored copy in the light review image.
        try:
            from croi.objectives.tma_core.gridding import estimate_grid
            log.info("dearray: using croi.objectives.tma_core.gridding (canonical)")
        except Exception:
            from qptiff_pipeline.review.tma_gridding import estimate_grid
            log.info("dearray: using vendored tma_gridding "
                     "(croi.objectives.tma_core.gridding unavailable here)")
    except Exception as exc:
        _log_exc("dearray: could not import a grid estimator (geopandas/shapely/scipy missing?)", exc)
        st.warning(f"dearray unavailable in this venv ({exc}); showing raw detections. "
                   "The review image needs geopandas + shapely + scipy for the grid fit.")
        return boxes, None
    try:
        # `bid` rides along as a column, so each cell traces back to its box after reindexing.
        det = gpd.GeoDataFrame(
            [{"class": b["class"], "conf": b.get("conf", 1.0), "bid": b.get("bid"),
              "geometry": _box(*b["xyxy"])} for b in boxes],
            geometry="geometry",
        ).reset_index(drop=True)
        # Merge duplicates first: a near-zero-length edge skews the angle estimate and the grid
        # collapses.
        det, n_merged = _dedupe_overlapping(det)
        if n_merged:
            log.info("dearray: merged %d near-duplicate detection(s) before the grid fit", n_merged)
            st.caption(f"Merged {n_merged} near-duplicate detection(s) before the grid fit.")
        log.info("dearray: fitting grid to %d detection(s) (%d raw, %d merged)",
                 len(det), len(boxes), n_merged)
        fit = estimate_grid(det, expected_shape=None)
        cells = fit.to_geodataframe().reset_index(drop=True)
    except Exception as exc:
        _log_exc("dearray: estimate_grid failed", exc)
        st.warning(f"grid fit failed ({exc}); showing raw detections")
        return boxes, None
    bids = det["bid"].tolist() if "bid" in det.columns else []
    placed_idx: set[int] = set()  # detection rows the fit placed on a cell
    out = []
    for _, r in cells.iterrows():
        occupied = bool(r["occupied"])
        geom = r["geometry"] if r["geometry"] is not None else r.get("ideal_geometry")
        if geom is None:
            continue
        # Keep the class the fit assigned: detected cells carry the detector's class, missing cells
        # carry "inferred_core".
        cls = r["class"]
        didx = int(r["detection_idx"]) if "detection_idx" in cells.columns else -1
        if didx >= 0:
            placed_idx.add(didx)
        bid = bids[didx] if 0 <= didx < len(bids) else None
        out.append(
            {
                "class": cls,
                "xyxy": [round(float(v), 1) for v in geom.bounds],
                "occupied": occupied,
                "rc": [int(r["row"]), int(r["col"])],
                "bid": bid,  # from the detection; None for a grid-only cell
            }
        )
    # If the fit placed only a fraction of the deduped detections, or far fewer cells than inputs,
    # keep the raw detections.
    occupied = int(cells["occupied"].sum()) if "occupied" in cells else len(out)
    n_fit = len(det)
    log.info("dearray: %s, %d/%d deduped detections placed (%d raw)",
             grid_shape(cells), occupied, n_fit, len(boxes))
    if occupied < 0.7 * n_fit:
        log.warning("dearray: grid fit unreliable, only %d/%d placed (%s); keeping raw detections",
                    occupied, n_fit, grid_shape(cells))
        st.warning(
            f"Grid fit looks unreliable. Only {occupied}/{n_fit} detections were placed "
            f"({grid_shape(cells)}). Keeping the raw detections; the grid estimator under-fit "
            "this TMA."
        )
        return boxes, None

    # Keep every detection the fit did not place, without `rc`, so the reviewer sees it. A grid
    # link can then place it.
    unplaced = [i for i in range(len(det)) if i not in placed_idx]
    for i in unplaced:
        geom = det.geometry.iloc[i]
        out.append(
            {
                "class": str(det["class"].iloc[i]),
                "xyxy": [round(float(v), 1) for v in geom.bounds],
                "occupied": True,  # a real detection, not on the lattice
                "rc": None,
                "bid": bids[i] if i < len(bids) else None,
            }
        )
    if unplaced:
        log.warning("dearray: %d detection(s) were not placed on the grid; kept un-gridded",
                    len(unplaced))
        st.warning(
            f"{len(unplaced)} detected core(s) did not land on the grid and have no row,col. "
            "They are on the image without a label. Link one to a neighbour and re-apply, or "
            "move it onto its cell."
        )
    return _with_bids(out), grid_params(fit)


def grid_shape(grid) -> str:
    try:
        return f"{grid['row'].max() + 1}x{grid['col'].max() + 1} grid"
    except Exception:
        return "grid"


def _review_one(stage: str, uid: str, name: str, bundle: dict | None) -> None:
    import lamindb as ln

    if bundle is None:
        st.info(f"No edit bundle found (has {stage}-detect run for this unit?).")
        return

    classes = list(hitl.CLASSES_BY_STAGE[stage])
    class_color, _ = _colors(stage)

    # scale (display px -> review px), for the "modified" tolerance.
    scale = bundle["width"] / min(bundle["width"], MAX_DISPLAY_W)
    W, H = float(bundle["width"]), float(bundle["height"])

    # Session keys namespaced by stage: both tabs render each run.
    sk = f"{stage}-{uid}"
    seed_key, ver_key, cur_key, cand_key = f"seed-{sk}", f"ver-{sk}", f"cur-{sk}", f"cand-{sk}"
    deard_key = f"deard-{sk}"  # True once this TMA is dearrayed; gates core Accept
    edges_key, grid_key = f"edges-{sk}", f"grid-{sk}"  # grid links and the lattice they re-index
    if seed_key not in st.session_state:
        # Seed once per session: the accepted boxes if the unit was accepted, else the raw
        # detections. Dearray stays an explicit step. "Reload" re-pulls.
        src_boxes = _accepted_or_raw_boxes(uid, bundle, stage)
        prepared = _with_bids([{"class": b["class"], "xyxy": b["xyxy"]} for b in src_boxes])
        st.session_state[cand_key] = prepared  # baseline for the "modified" check
        st.session_state[seed_key] = list(prepared)
    st.session_state.setdefault(ver_key, 0)
    st.session_state.setdefault(edges_key, [])  # reviewer's grid links, session only
    st.session_state.setdefault(grid_key, None)  # lattice from the last dearray

    def remount(new_seed: list[dict]) -> None:
        st.session_state[seed_key] = new_seed
        st.session_state[ver_key] += 1
        st.rerun()

    c1, c2 = st.columns([3, 1])
    with c2:
        # Colour -> class legend.
        st.markdown(
            " &nbsp; ".join(
                f"<span style='color:{class_color[c]};font-weight:700'>&#9632;</span> {c}"
                for c in classes
            ),
            unsafe_allow_html=True,
        )
        st.caption(
            "**Right-click a box to change its class.** Use the *Move / resize* tool to adjust "
            "boxes, *Add box* to drag a new one, and Delete/Backspace to remove the selected box. "
            "Boxes are clamped to the image."
        )
        cur = st.session_state.get(cur_key) or st.session_state[seed_key]
        if cur:
            idx = st.selectbox(
                "Box to expand", range(len(cur)),
                format_func=lambda i: f"#{i} {cur[i]['class']}", key=f"exp-{sk}",
            )
            if st.button("Resize box to full image", key=f"full-{sk}"):
                new = [dict(b) for b in cur]
                new[idx] = {**cur[idx], "xyxy": [0.0, 0.0, W, H]}  # keep class/occupied/rc/bid
                remount(new)
        elif st.button("Add full-image box", key=f"full-{sk}"):
            remount(_with_bids(
                [{"class": classes[0], "xyxy": [0.0, 0.0, W, H], "occupied": True, "rc": None}]))

    with c1:
        # Custom fabric.js canvas: right-click a box to change its class; move, resize, add and
        # delete client-side. It keeps occupied/rc itself and returns clamped boxes. Bumping
        # `nonce` rebuilds from the seed; otherwise live edits are kept.
        result = roi_canvas(
            image_b64=bundle["image_b64"],
            boxes=[_with_meta(b, W, H) for b in st.session_state[seed_key]],
            classes=classes,
            colors=class_color,
            width=W,
            height=H,
            max_display_w=MAX_DISPLAY_W,
            edges=st.session_state[edges_key],
            nonce=st.session_state[ver_key],
            key=f"roican-{sk}",
        )

    nonce = str(st.session_state[ver_key])
    if (isinstance(result, dict) and str(result.get("nonce")) == nonce
            and result.get("boxes") is not None):
        boxes = [
            {"class": b["class"], "xyxy": [float(v) for v in b["xyxy"]],
             "occupied": bool(b.get("occupied", True)),
             "rc": list(b["rc"]) if b.get("rc") else None,
             "bid": b.get("bid")}
            for b in result["boxes"]
        ]
        # The component owns the links, so mirror them into session state to survive a remount.
        st.session_state[edges_key] = [
            {"a": e["a"], "b": e["b"], "axis": "col" if e.get("axis") == "col" else "row"}
            for e in (result.get("edges") or [])
        ]
    else:
        # Before the first paint, or right after a reseed, use the seed.
        boxes = [_with_meta(b, W, H, clamp=True) for b in st.session_state[seed_key]]
    st.session_state[cur_key] = boxes

    if boxes:
        is_core = stage == "core"
        # Read-only summary table. For core it adds the grid row,col and an inferred flag.
        link_count: dict = {}
        for e in st.session_state.get(edges_key, []):
            for end in (e.get("a"), e.get("b")):
                link_count[end] = link_count.get(end, 0) + 1
        rows = []
        for i in range(len(boxes)):
            row = {"box": i, "label": boxes[i]["class"]}
            if is_core:
                row["row,col"] = _fmt_rc(boxes[i].get("rc"))
                row["inferred_core"] = not bool(boxes[i].get("occupied", True))
                row["links"] = link_count.get(boxes[i].get("bid"), 0)
            row["xyxy"] = str(boxes[i]["xyxy"])
            row["delete"] = False
            rows.append(row)
        colcfg = {
            "box": st.column_config.NumberColumn(disabled=True),
            "label": st.column_config.TextColumn(
                "detection label", disabled=True,
                help="Change a box's class by RIGHT-CLICKING it on the image"),
            "xyxy": st.column_config.TextColumn(disabled=True),
            "delete": st.column_config.CheckboxColumn(help="Tick to remove this box"),
        }
        if is_core:
            colcfg["row,col"] = st.column_config.TextColumn(
                "row,col", disabled=True, help="Grid position from the dearray fit")
            colcfg["inferred_core"] = st.column_config.CheckboxColumn(
                "inferred?", disabled=True,
                help="True = grid-inferred (the DL detector missed this cell); False = DL-detected")
            colcfg["links"] = st.column_config.NumberColumn(
                "links", disabled=True,
                help="Grid links you drew on this core (Link tool). 0 = none")
        edited = st.data_editor(rows, column_config=colcfg, hide_index=True, key=f"tbl-{sk}")
        drop = {i for i, row in enumerate(edited) if row.get("delete")}
        if drop:
            remount([{"class": boxes[i]["class"], "xyxy": boxes[i]["xyxy"],
                      "occupied": boxes[i].get("occupied", True), "rc": boxes[i].get("rc"),
                      "bid": boxes[i].get("bid")}
                     for i in range(len(boxes)) if i not in drop])

    # Send `inferred` and `rc` with each box, so core-persist records is_detected and uses the
    # reviewer's row,col instead of re-fitting.
    corrected = [
        {"class": b["class"], "xyxy": b["xyxy"], "inferred": not bool(b.get("occupied", True)),
         "rc": list(b["rc"]) if b.get("rc") else None}
        for b in boxes
    ]
    # Compare to the initial boxes with a small px tolerance, so undoing an edit reads as unchanged.
    modified = not _boxes_equivalent(corrected, st.session_state.get(cand_key, []), tol=max(2.0, 2.0 * scale))
    by_class = {c: sum(1 for b in corrected if b["class"] == c) for c in classes}
    st.write(
        f"{len(corrected)} box(es): "
        + ", ".join(f"{n} {c}" for c, n in by_class.items())
        + (", **modified**" if modified else ", unchanged")
    )

    # Core: fit the grid to the current boxes. Each cell becomes an editable box labelled row,col:
    # detected cells solid, inferred cells dashed. Re-runnable after edits.
    if stage == "core" and corrected:
        n_det = sum(1 for b in boxes if b.get("occupied", True))
        n_links = len(st.session_state.get(edges_key, []))
        method = st.radio(
            "Dearray method",
            [GRAPH, LATTICE],
            format_func=lambda m: ("connectivity (graph traversal): handles offset blocks, "
                                   "uses your links as constraints" if m == GRAPH
                                   else "lattice fit (previous behaviour)"),
            key=f"method-{sk}", horizontal=False,
        )
        d1, d2 = st.columns(2)
        label = f"Dearray: fit grid to {n_det} detection(s)"
        if method == GRAPH and n_links:
            label += f" using {n_links} link(s)"
        if d1.button(label, key=f"dearray-{sk}"):
            log.info("Dearray clicked for %r (%s): %d detection(s) of %d box(es), %d link(s) ...",
                     name, method, n_det, len(boxes), n_links)
            # Feed the canvas boxes, which carry the `bid` that grid links point at. Links go into
            # the connectivity fit as constraints.
            out, lattice = _dearray(boxes, method=method,
                                    links=st.session_state.get(edges_key))
            # Mark the unit dearrayed only if the fit produced a grid. A failed fit does not unlock
            # Accept.
            gridded = any(b.get("rc") is not None for b in out)
            st.session_state[deard_key] = gridded
            st.session_state[grid_key] = lattice
            log.info("Dearray result for %r: %d cell(s), gridded=%s (Accept %s)",
                     name, len(out), gridded, "unlocked" if gridded else "still locked")
            remount(out)

        # Apply grid links: re-index the current grid without refitting. See review.grid_links.
        if d2.button(
            f"Apply grid links ({n_links})", key=f"links-{sk}", disabled=not n_links,
            help="Re-index the grid so every link you drew holds. Draw links with the 🔗 Link tool."
                 if n_links else "Draw a link first with the 🔗 Link tool on the image.",
        ):
            log.info("Apply grid links for %r: %d link(s) over %d box(es)", name, n_links, len(boxes))
            out, warns = apply_grid_links(boxes, st.session_state[edges_key],
                                          st.session_state.get(grid_key))
            for w in warns:
                log.warning("grid links (%s): %s", name, w)
                st.warning(w)
            if out != boxes:
                remount(out)
            else:
                st.info("Links are already satisfied; nothing to re-index.")
        st.caption("After dearray: solid = detected core, dashed = grid-inferred (missing) core, "
                   "label = grid row,col. Move / add / delete / relabel as usual. A re-fit uses only "
                   "the solid (detected) boxes. Dashed cells are regenerated, never fed back in. "
                   "If a block of columns is indexed wrongly, join two cores that share a row with "
                   "**🔗 Link** and *re-dearray* (connectivity uses links as constraints); "
                   "*Apply grid links* re-indexes without refitting.")

    # Core cannot be accepted until the grid is fitted at least once this session.
    gate_accept = stage == "core" and not st.session_state.get(deard_key, False)
    if gate_accept:
        st.info("Dearray the detections (fit the grid) before you can accept this TMA.")

    # ...nor while any box lacks a grid position: core-persist names each Core from its row,col.
    ungridded = [i for i, b in enumerate(boxes) if b.get("rc") is None] if stage == "core" else []
    if ungridded and not gate_accept:
        gate_accept = True
        st.warning(
            f"{len(ungridded)} core(s) have no row,col (box #{', #'.join(str(i) for i in ungridded[:8])}"
            f"{'...' if len(ungridded) > 8 else ''}). Link one to a neighbouring core and press "
            "*Apply grid links*, drag it onto its cell and re-dearray, or delete it if it is not a core."
        )

    a, r = st.columns(2)
    if a.button(
        "Accept", key=f"a-{sk}", type="primary", disabled=gate_accept,
        help=("Every core needs a row,col first." if ungridded else
              "Run 'Dearray: fit grid to current boxes' first.") if gate_accept else None,
    ):

        try:
            with _logged(f"{stage} ACCEPT {name} ({uid[:8]}): {len(corrected)} box(es), modified={modified}"):
                vocab.ensure()  # this repo's declared features, once per process
                rec = ln.Record.get(uid=uid)
                # Write F_CORRECTED and F_MODIFIED too: persist and harvest-feedback still read
                # them.
                _set_feature(rec, vocab.F_CORRECTED, json.dumps(corrected))
                _set_feature(rec, vocab.F_MODIFIED, bool(modified))
                _write_accepted_context(rec, stage, name, uid, boxes, bundle, modified)
                result = gate.decide(stage, uid, states.Decision.ACCEPT,
                                     payload={"modified": bool(modified), "n_boxes": len(corrected)})
            log.info("%s ACCEPT %r -> %s", stage, name, result)
            st.write(result)
        except Exception as exc:
            st.error(f"Accept failed for {name}: {exc}")
            st.exception(exc)
        else:
            _unit_states.clear()  # refresh the pending list; keep bundle caches warm
            # Drop cached overlays, which show the pre-accept picture.
            _overlay_thumb_png.clear()
            for k in (seed_key, ver_key, cur_key, cand_key, deard_key, edges_key, grid_key):
                st.session_state.pop(k, None)
            st.rerun()
    if r.button("Reject", key=f"r-{sk}"):
        try:
            with _logged(f"{stage} REJECT {name} ({uid[:8]})"):
                result = gate.decide(stage, uid, states.Decision.REJECT)
            log.info("%s REJECT %r -> %s", stage, name, result)
            st.write(result)
        except Exception as exc:
            st.error(f"Reject failed for {name}: {exc}")
            st.exception(exc)
        else:
            _unit_states.clear()
            for k in (seed_key, ver_key, cur_key, cand_key, deard_key, edges_key, grid_key):
                st.session_state.pop(k, None)
            st.rerun()


BGNORM_EXPERIMENT_PREFIX = "bgnorm"  # must match bgnorm_core's mlflow_experiment default
BGNORM_FAILED_CHANNELS = vocab.BGNORM_FAILED_CHANNELS  # declared in vocab.py


@st.cache_data(ttl=3600)
def _lamin_instance_slug() -> str:
    """owner/name of the instance for LaminHub links: LAMIN_INSTANCE, else lamindb, else ""."""
    import os

    if os.environ.get("LAMIN_INSTANCE"):
        return os.environ["LAMIN_INSTANCE"]
    try:
        import lamindb as ln

        return ln.setup.settings.instance.slug or ""
    except Exception:
        return ""


def _lamin_record_url(uid: str) -> str:
    """LaminHub page for a Record, e.g. https://lamin.ai/<owner>/<name>/record/<uid>."""
    return f"https://lamin.ai/{_lamin_instance_slug()}/record/{uid}"


def _write_accepted_context(rec, stage: str, name: str, uid: str, boxes: list[dict],
                            bundle: dict, modified: bool) -> None:
    """Store the reviewer's accepted state as a typed context on the unit Record.

    Best effort: a context write never fails an accept.
    """
    from datetime import datetime

    from qptiff_pipeline.domain import contexts as ctxmod
    from qptiff_pipeline.lamin import context_io

    try:
        ctx = context_io.read_context(rec, stage, bundle=bundle) or ctxmod.CONTEXT_BY_STAGE[stage](
            unit_name=name,
            unit_uid=uid,
            raw=ctxmod.DetectionSet(
                boxes=[ctxmod.Box.from_legacy(b) for b in bundle.get("boxes", [])],
                space=context_io._space_from(bundle["width"], bundle["height"], bundle, stage),
            ),
        )
        ctx.accepted = ctxmod.AcceptedSet(
            boxes=[ctxmod.Box.from_legacy(b) for b in boxes],  # canvas shape: occupied, rc, bid
            space=ctx.raw.space,
            modified=bool(modified),
            accepted_at=datetime.now(UTC),
        )
        if stage == "core":
            ctx.links = [ctxmod.GridLink(**e) for e in st.session_state.get(f"edges-{stage}-{uid}", [])]
        context_io.write_context(rec, ctx)
        log.info("%s: stored accepted context (%d box(es))", name, len(ctx.accepted.boxes))
    except Exception as exc:
        _log_exc(f"could not store the accepted context for {name}", exc)


def _unit_context(uid: str, stage: str, name: str, bundle: dict | None = None):
    """The unit's typed context, or None.

    The stored context if present, else one assembled from the older features.
    """
    import lamindb as ln

    from qptiff_pipeline.lamin import context_io

    try:
        record = ln.Record.get(uid=uid)
        return context_io.read_context(record, stage, bundle=bundle)
    except Exception as exc:  # never block rendering on a lookup failure
        log.info("context read failed for %s (%s): %s", name, stage, exc)
        return None


def _dearray_provenance(uid: str, name: str) -> tuple[str, bool]:
    """(explanation, is_the_accepted_grid) for a TMA's dearray thumbnail.

    The thumbnail is redrawn from the accepted grid in the database, on the newest
    ``core-edit-<slug>`` bundle image. The explanation says whether the accepted grid was found and
    whether its boxes fit the current crop.
    """
    bundle = load_bundle(hitl.EDIT_PREFIX_BY_STAGE["core"], name)
    if not bundle:
        return "no core edit bundle stored for this TMA; nothing to draw on.", False
    ctx = _unit_context(uid, "core", name, bundle)
    if ctx is None:
        return "could not read the accepted grid; showing whatever the bundle holds.", False
    if ctx.accepted is None:
        return (
            ("⚠️ no accepted grid stored for this TMA. Showing the model's RAW detections. Either it "
            "never went through Accept, or core-persist deleted the accepted boxes after writing "
            "its parquet (the parquet lives on the HPC, which this app cannot read)."),
            False,
        )
    boxes = ctx.accepted.boxes
    W, H = float(bundle["width"]), float(bundle["height"])
    out = [b for b in boxes if b.xyxy[2] > W * 1.02 or b.xyxy[3] > H * 1.02]
    if out:
        return (
            (f"⚠️ the accepted grid ({len(boxes)} cells) does not fit this crop ({W:.0f}×{H:.0f}px): "
            f"{len(out)} box(es) out of bounds. core-detect almost certainly re-ran after this TMA "
            "was accepted, so the grid and the crop are from different runs."),
            False,
        )
    n_inf = sum(1 for b in boxes if not b.detected)
    n_rc = sum(1 for b in boxes if b.rc is not None)
    return (
        f"accepted dearray: {len(boxes)} cell(s), {n_rc} with row,col, {n_inf} grid-inferred.",
        True,
    )


def _accepted_or_raw_boxes(uid: str, bundle: dict, stage: str = "core") -> list[dict]:
    """Canvas-shaped boxes to display: the accepted ones if the unit was decided, else the raw
    detections. Both are in the bundle image's px space."""
    ctx = _unit_context(uid, stage, "", bundle)
    if ctx is not None and ctx.accepted is not None and ctx.accepted.boxes:
        return [b.to_canvas() for b in ctx.accepted.boxes]
    return bundle.get("boxes", [])


def _dashed_rect(draw, xyxy, color: str, width: int, dash: int = 6) -> None:
    """Dashed rectangle outline for grid-inferred cells. PIL has no dash support."""
    x1, y1, x2, y2 = (int(v) for v in xyxy)
    for y in (y1, y2):  # top and bottom edges
        for x in range(x1, x2, dash * 2):
            draw.line([x, y, min(x + dash, x2), y], fill=color, width=width)
    for x in (x1, x2):  # left and right edges
        for y in range(y1, y2, dash * 2):
            draw.line([x, y, x, min(y + dash, y2)], fill=color, width=width)


@st.cache_data(ttl=300, show_spinner=False)
def _overlay_thumb_png(edit_prefix: str, stage: str, name: str, uid: str, max_w: int = 420) -> bytes | None:
    """The unit's accepted boxes drawn on its clean image, as PNG bytes, or None if no bundle.

    Detected cells are solid and labelled ``row,col``, inferred cells dashed. Boxes without grid
    metadata are solid and unlabelled. Coloured by class. Cached per unit.
    """
    from PIL import Image, ImageDraw

    b = load_bundle(edit_prefix, name)
    if not b:
        return None
    img = Image.open(io.BytesIO(base64.b64decode(b["image_b64"]))).convert("RGB")
    # Downscale first, then draw, so outlines and labels stay legible.
    s = min(1.0, max_w / img.width)
    if s < 1.0:
        img = img.resize((int(img.width * s), int(img.height * s)))
    class_color, _ = _colors(stage)
    draw = ImageDraw.Draw(img)
    lw = max(1, img.width // 400)
    for bx in _accepted_or_raw_boxes(uid, b, stage):
        x1, y1, x2, y2 = (v * s for v in bx["xyxy"])
        color = class_color.get(bx["class"], "#ff0000")
        # `inferred` is the stored flag; `occupied` is the in-session form.
        inferred = bool(bx.get("inferred", not bx.get("occupied", True)))
        if inferred:
            _dashed_rect(draw, (x1, y1, x2, y2), color, lw)
        else:
            draw.rectangle([x1, y1, x2, y2], outline=color, width=lw)
        rc = bx.get("rc")
        if rc:
            draw.text((x1 + lw + 1, y1 + lw + 1), f"{int(rc[0])},{int(rc[1])}", fill=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _mlflow_experiment_url(exp_name: str) -> tuple[str | None, str | None]:
    """(deep_link, resolved_experiment_name) for a bgnorm experiment. Best effort.

    MLFLOW_TRACKING_URI is the in-network address used to look up the experiment id. When MLflow
    is unreachable, returns the experiments list URL.
    """
    import os

    tracking = (os.environ.get("MLFLOW_TRACKING_URI") or "").rstrip("/")
    # The browser reaches MLflow through a tunnel on localhost.
    base = "http://localhost:5000"
    if not base:
        return None, None
    try:
        import mlflow

        client = mlflow.tracking.MlflowClient(tracking_uri=tracking or None)
        exp = client.get_experiment_by_name(exp_name)
        if exp is not None:
            return f"{base}/#/experiments/{exp.experiment_id}", exp_name
    except Exception as exc:  # mlflow absent or server down: link the experiments list
        log.info("mlflow experiment lookup failed for %r: %s", exp_name, exc)
    return f"{base}/#/experiments", exp_name


def _clear_bgnorm_for_rerun(tma) -> int:
    """Clear a TMA's cores' bgnormed and runner markers, so the runner recomputes them. Returns the
    number of cores touched.

    Non-destructive: the recompute writes a new zarr and QC parquet, and the old pair stays.
    """
    import lamindb as ln

    core_type = ln.Record.filter(name=states.CORE_TYPE, is_type=True).one_or_none()
    cores = (
        list(ln.Record.filter(name__startswith=f"{tma.name}:R", type=core_type))
        if core_type is not None
        else []
    )
    cleared = 0
    for c in cores:
        cv = c.features.get_values()
        touched = False
        for feat in (
            vocab.BGNORMED,
            vocab.BGNORM_QC_FEATURE,
            *vocab.BGNORM_BOOKKEEPING,
        ):
            if cv.get(feat) is not None:
                c.features.remove_values(feat)
                touched = True
        if touched:
            cleared += 1
    return cleared


def _reopen_failed_section(failed: list[tuple[str, str, str]]) -> None:
    """The triage board's shelf for ``bgnorm_failed`` TMAs, and the way back to the queue.

    Not a lane: ``bgnorm_failed`` is terminal. The way back is two steps:

      1. ``gate.reopen("bgnorm", uid)`` returns the TMA to ``bgnorm_pending``.
      2. From there it is a "Done" lane card. Dragging it to the queue reruns it and supersedes its
         cores' bgnorm outputs. The old files stay on disk.
    """
    if not failed:
        return
    st.divider()
    with st.expander(f"Failed - bgnorm_failed ({len(failed)})", expanded=bool(failed)):
        st.caption(
            "`bgnorm_failed` is **terminal**, so these are not board cards and cannot be dragged. "
            "Reopen returns a TMA to `bgnorm_pending`, where it becomes a **Done** card you drag "
            "into the queue to rerun: that rerun supersedes its cores' prior bgnorm outputs "
            "(kept on disk under their run-scoped keys, but no longer the cores' current values)."
        )
        for uid, name, state in failed:
            c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
            c1.write(f"{name} - `{state}`")
            c2.link_button("LaminHub Record", _lamin_record_url(uid))
            sure = c3.checkbox("Confirm", key=f"triage-reopen-ok-{uid}")
            if c4.button("Reopen", key=f"triage-reopen-{uid}", disabled=not sure):
                try:
                    with _logged(f"triage REOPEN {name} ({uid[:8]})"):
                        res = gate.reopen("bgnorm", uid)
                except Exception as exc:
                    st.error(f"Reopen failed for {name}: {exc}")
                    st.exception(exc)
                else:
                    st.success(
                        f"Reopened {res['unit']} -> {res['state']}. It is now a **Done** card; "
                        "drag it into **bgnorm queue** and run the queue to rerun it."
                    )
                    _unit_states.clear()
                    st.cache_data.clear()
                    st.rerun()


def _triage_panel(stage: str) -> None:
    """Human triage of core_approved TMAs, before any bgnorm runs.

    H&E is RGB brightfield and gets no bgnorm. Multiplex is fluorescence and is the bgnorm target.
    For each TMA it shows the filename, channel dims, proposed image_type and the detection
    overlays. The reviewer drags TMAs between the lanes in :data:`TRIAGE_LANES`; each lane is a
    pipeline state.

      - bgnorm_pending -> queue: rerun. Clears the cores' bgnormed markers. Old zarrs stay on disk.
      - bgnorm_running -> queue: resume a stranded batch. Clears nothing.

    ``bgnorm_failed`` shows below the board behind an explicit reopen. "Run bgnorm queue" commits
    the moves and starts the bgnorm-runner.
    """
    import lamindb as ln
    from clinicalomx import set_state

    if st.button("Reload (triage board)", key=f"refresh-{stage}"):
        st.cache_data.clear()
        st.rerun()

    # Each lane is a state. Terminal states render below, behind an explicit reopen.
    cols = list(TRIAGE_LANES)
    header_to_state = {h: s for h, s in cols}
    state_to_header = {s: h for h, s in cols}
    unit_states = _unit_states("core")
    tmas = [(uid, name, s) for uid, name, s in unit_states if s in state_to_header]
    failed = [(uid, name, s) for uid, name, s in unit_states if s in TRIAGE_REOPEN_STATES]
    if not tmas:
        st.info(
            "No TMAs to triage or rerun (none at "
            + " / ".join(str(s) for _h, s in cols)
            + ")."
        )
        # A board with no draggable cards can still hold reopenable ones.
        _reopen_failed_section(failed)
        return
    by_name = {name: (uid, s) for uid, name, s in tmas}
    st.write(f"**{len(tmas)}** TMA(s) in triage. Drag cards between lanes, then **Run bgnorm queue**.")
    st.caption(
        "Dragging only stages. Nothing is applied until you click **Run bgnorm queue** (and a "
        "page refresh discards unsaved drags). Drag a **bgnorm_pending** card into **bgnorm queue** "
        "to rerun it: on run, its cores' bgnorm markers are cleared so the runner recomputes them, "
        "while the prior zarr is kept on disk (run-scoped keys). Compare old vs new in the "
        "per-core MLflow experiment. Drag a **bgnorm_running** card into the queue to re-queue a "
        "batch a dead runner stranded: nothing is cleared, so finished cores stay finished."
    )

    # Per-slide image type, proposed by check-qptiffs. Confirm or override once per slide.
    slides: dict[str, list[str]] = {}
    for uid, name, _s in tmas:
        slides.setdefault(name.rsplit(":", 1)[0], []).append(uid)
    with st.expander(f"Image type per slide ({len(slides)})", expanded=False):
        opts = [vocab.MULTIPLEX, vocab.H_AND_E, vocab.IMAGE_TYPE_UNKNOWN]
        for base, uids in slides.items():
            rvals = ln.Record.get(uid=uids[0]).features.get_values()
            proposed = rvals.get(vocab.IMAGE_TYPE) or vocab.IMAGE_TYPE_UNKNOWN
            nch = rvals.get(qptiff_vocab.QPTIFF_N_CHANNELS)
            c1, c2 = st.columns([2, 1])
            c1.caption(f"**{base}**: proposed `{proposed}`" + (f" · {nch} ch" if nch is not None else ""))
            idx = opts.index(proposed) if proposed in opts else opts.index(vocab.IMAGE_TYPE_UNKNOWN)
            pick = c2.selectbox(
                "type", opts, index=idx, key=f"itype-{base}", label_visibility="collapsed"
            )
            if pick != proposed:
                slide = _resolve_record(rvals.get("slide"), states.SLIDE_TYPE)
                if slide is not None:
                    _set_feature(slide, vocab.IMAGE_TYPE, pick)
                for u in uids:  # keep the TMA copies consistent with the slide
                    _set_feature(ln.Record.get(uid=u), vocab.IMAGE_TYPE, pick)
                st.toast(f"{base}: image_type -> {pick}")

    # Drag-and-drop board: lanes are states.
    try:
        from streamlit_sortables import sort_items
    except Exception:
        st.error("streamlit-sortables is not installed in this image; rebuild the review app.")
        return
    containers = [
        {"header": h, "items": sorted(n for n, (u, s) in by_name.items() if s == st_)}
        for h, st_ in cols
    ]
    # The board keeps the arrangement while its key is stable. The key is bumped after a commit.
    # Uncommitted drags are lost on page refresh.
    rev = st.session_state.get("triage_rev", 0)
    arranged = sort_items(containers, multi_containers=True, key=f"triage-board-{rev}")
    # Dragging only stages moves. Nothing is written until "Run bgnorm queue".
    staged = {nm: header_to_state[cont["header"]] for cont in arranged for nm in cont["items"]}
    diffs = {
        nm: (by_name[nm][0], by_name[nm][1], tgt)  # nm -> (uid, current_state, target_state)
        for nm, tgt in staged.items()
        if nm in by_name and by_name[nm][1] != tgt
    }

    # Inspect one TMA: filename, channel dims, overlays.
    st.divider()
    st.subheader("Inspect a TMA")
    pick = st.selectbox("TMA", sorted(by_name), key=f"triage-inspect-{stage}")
    if pick:
        uid, _s = by_name[pick]
        tma = ln.Record.get(uid=uid)
        tvals = tma.features.get_values()
        src = tvals.get("source_image")
        qptiff = src if isinstance(src, ln.Artifact) else (ln.Artifact.get(key=src) if src else None)
        itype = tvals.get(vocab.IMAGE_TYPE) or vocab.IMAGE_TYPE_UNKNOWN
        nch = tvals.get(qptiff_vocab.QPTIFF_N_CHANNELS)
        slide = _resolve_record(tvals.get("slide"), states.SLIDE_TYPE)
        svals = slide.features.get_values() if slide is not None else {}
        dims = svals.get(qptiff_vocab.QPTIFF_DIMS)
        st.caption(
            f"**File:** `{getattr(qptiff, 'key', '?')}`  ·  **type:** `{itype}`"
            + (f"  ·  **channels:** {nch}" if nch is not None else "")
            + (f"  ·  **dims:** `{dims}`" if dims else "")
        )
        g, c = st.columns(2)
        with g:
            # The qptiff's embedded RGB thumbnail, in true colour: the modality cue for triage.
            st.caption("qptiff thumbnail (H&E: pink/white · multiplex: dark/multicolour)")
            # Fetched from the Prefect artifact check-qptiffs uploads, keyed by the qptiff stem.
            base = Path(qptiff.key).stem if qptiff is not None else None
            thumb = load_bundle(hitl.QPTIFF_THUMB_PREFIX, base) if base else None
            if thumb and thumb.get("image_b64"):
                st.image(base64.b64decode(thumb["image_b64"]))
            else:
                st.caption("No qptiff thumbnail stored. Run backfill-image-type (or re-check-qptiffs).")
        with c:
            # The approved dearray: one box per cell, labelled row,col, dashed where inferred.
            st.caption("Approved dearray (label = row,col · dashed = grid-inferred core)")
            cpng = _overlay_thumb_png(
                hitl.EDIT_PREFIX_BY_STAGE["core"], "core", pick, uid, max_w=560
            )
            if cpng:
                st.image(cpng)
            else:
                st.caption("No core overlay bundle.")
            # Say which source this picture came from.
            note, ok = _dearray_provenance(uid, pick)
            (st.caption if ok else st.warning)(note)

    # Commit staged moves and run the queue. The only place the board writes state.
    st.divider()
    staged_queue = [nm for nm, tgt in staged.items() if tgt == states.S.BGNORM_QUEUED]
    if diffs:
        st.caption(f"{len(diffs)} staged move(s) pending, committed when you run the queue.")
    st.write(f"**{len(staged_queue)}** TMA(s) staged in the bgnorm queue.")
    if st.button("Run bgnorm queue", type="primary", disabled=not staged_queue, key=f"run-queue-{stage}"):
        # 1) Commit every staged move. pending -> queue also clears the cores' bgnorm markers.
        #    running -> queue clears nothing: it resumes a stranded batch.
        reran = 0
        for (uid, cur, tgt) in diffs.values():
            rec = ln.Record.get(uid=uid)
            if cur == states.S.BGNORM_PENDING and tgt == states.S.BGNORM_QUEUED:
                reran += _clear_bgnorm_for_rerun(rec)
            set_state(rec, tgt)
        # 2) Start the runner for everything in the queue.
        ok = False
        try:
            from prefect.client.orchestration import get_client

            with get_client(sync_client=True) as client:
                dep = client.read_deployment_by_name("bgnorm-runner/pipeline-bgnorm-runner")
                run = client.create_flow_run_from_deployment(dep.id)
            log.info("bgnorm-runner triggered (%s) for %d queued TMA(s)", run.name, len(staged_queue))
            msg = f"Triggered bgnorm-runner ({run.name}) for {len(staged_queue)} queued TMA(s)."
            if reran:
                msg += f" Cleared bgnorm markers on {reran} core(s) for rerun."
            st.success(msg)
            ok = True
        except Exception as exc:
            st.error(f"Could not trigger bgnorm-runner: {exc}")
            st.exception(exc)
        # 3) Re-init the board from the database, only on success. st.rerun() stays outside the
        #    try, because it raises.
        _unit_states.clear()
        if ok:
            st.session_state["triage_rev"] = rev + 1
            st.cache_data.clear()
            st.rerun()

    _reopen_failed_section(failed)


def _submit_bgnorm_core(core_uids: list[str], **params) -> None:
    """Submit a bgnorm-core run for the given cores, with optional parameter overrides.

    Clears each core's runner bookkeeping first, so a manual re-run starts with a fresh budget.
    """
    import lamindb as ln
    from prefect.client.orchestration import get_client

    for u in core_uids:
        rec = ln.Record.get(uid=u)
        for f in vocab.BGNORM_BOOKKEEPING:
            if rec.features.get_values().get(f) is not None:
                rec.features.remove_values(f)
    with get_client(sync_client=True) as client:
        dep = client.read_deployment_by_name("bgnorm-core/pipeline-bgnorm-core")
        run = client.create_flow_run_from_deployment(
            dep.id, parameters={"core_uids": list(core_uids), **params}
        )
    log.info("bgnorm-core submitted (%s) for %d core(s), params=%s", run.name, len(core_uids), params)
    _unit_states.clear()


def _bgnorm_review_panel(stage: str) -> None:
    """bgnorm review, per core: pick a TMA, then a core. Inspect its MLflow experiment, re-run cores
    with other parameters, then accept or fail the TMA.
    """
    import lamindb as ln

    gate_cfg = states.GATES[stage]

    if st.button("Reload (pending list)", key=f"refresh-{stage}"):
        st.cache_data.clear()
        st.rerun()

    units = pending_units(stage)
    st.write(f"**{len(units)}** TMA(s) awaiting bgnorm review (`{gate_cfg.pending}`)")
    if units:
        by_label = {f"{name}  ({uid[:8]})": (uid, name) for uid, name in units}
        pick = st.selectbox("Parent TMA", list(by_label), key=f"pick-{stage}")
        uid, name = by_label[pick]

        core_type = ln.Record.filter(name=states.CORE_TYPE, is_type=True).one_or_none()
        cores = (
            list(ln.Record.filter(name__startswith=f"{name}:R", type=core_type))
            if core_type is not None else []
        )
        # One get_values() per core drives the metrics, the missing list and the selector labels.
        detected, bgnormed, missing = [], [], []
        core_by_label: dict[str, object] = {}
        for c in cores:
            v = c.features.get_values()
            if v.get("is_detected") is False:
                continue
            detected.append(c)
            if v.get(vocab.BGNORMED) is not None:  # presence only: a list is truthy too
                bgnormed.append(c)
                mark = "🟢"  # bgnormed
            else:
                missing.append(c)
                mark = "🟠" if v.get(vocab.BGNORM_CORE_FAILED) is True else "🔴"  # failed / pending
            core_by_label[f"{mark} {c.name}"] = c
        m1, m2, m3 = st.columns(3)
        m1.metric("Detected cores", len(detected))
        m2.metric("bgnormed", len(bgnormed))
        m3.metric("missing", len(missing))

        # Re-run the cores that failed or never completed.
        if missing:
            st.warning(f"{len(missing)} core(s) not bgnormed (failed or still running).")
            if st.button(f"Re-run all {len(missing)} missing core(s)", key=f"rerun-missing-{uid}"):
                try:
                    _submit_bgnorm_core([c.uid for c in missing])
                    st.success(f"Submitted bgnorm-core for {len(missing)} missing core(s).")
                except Exception as exc:
                    st.error(f"Submit failed: {exc}")

        # Per-core selector, status-tagged.
        if core_by_label:
            cpick = st.selectbox("Core within this TMA", list(core_by_label), key=f"corepick-{uid}")
            core = core_by_label[cpick]
            cv = core.features.get_values()
            # `bgnormed` can hold several values. Show the current one.
            art = newest_artifact(cv.get(vocab.BGNORMED))
            exp_name = keys.bgnorm_experiment_name(core.name, prefix=BGNORM_EXPERIMENT_PREFIX)
            url, _resolved = _mlflow_experiment_url(exp_name)
            st.caption(
                f"Experiment `{exp_name}`"
                + (f" · artifact `{getattr(art, 'key', art)}`" if art is not None else " · (not bgnormed yet)")
            )
            # MLflow blocks embedding, so open it in a new tab.
            if url:
                st.link_button("Open MLflow in a new tab", url, type="primary")
            else:
                st.info("Set `MLFLOW_TRACKING_URI` (in-network) to resolve the experiment.")

            # Per-channel QC metrics, the same numbers bgnorm logs to MLflow.
            qc_rows = _bgnorm_qc_rows(core.name)
            if qc_rows:
                with st.expander(f"QC metrics: {len(qc_rows)} channel(s)"):
                    st.dataframe(qc_rows, width="stretch", hide_index=True)
            elif art is not None:
                st.caption(
                    "No QC table for this core. It was bgnormed before QC tables were saved. "
                    "Re-run it (below) to generate one."
                )

            with st.expander("Re-run THIS core with different parameters (overwrites its result)"):
                p1, p2, p3 = st.columns(3)
                mfr = p1.number_input("median_filter_radius", 0, 25, 3, key=f"mfr-{uid}")
                cof = p2.number_input("log_cofactor", 1, 100_000, 150, key=f"cof-{uid}")
                ncomp = p3.number_input("n_components", 2, 12, 3, key=f"nc-{uid}")
                p4, p5 = st.columns(2)
                q = p4.number_input("quantile_post_hoc_value", 0.0, 1.0, 0.75, step=0.05, key=f"q-{uid}")
                seed = p5.number_input("sampling_seed", 0, 10_000_000, 0, key=f"seed-{uid}")
                if st.button("Re-run bgnorm for this core", type="primary", key=f"rerun-{core.uid}"):
                    try:
                        _submit_bgnorm_core(
                            [core.uid],
                            overwrite=True,
                            median_filter_radius=int(mfr),
                            log_cofactor=int(cof),
                            n_components=int(ncomp),
                            quantile_post_hoc_value=float(q),
                            sampling_seed=int(seed),
                        )
                        st.success(f"Re-run submitted for {core.name} (overwrite=True).")
                    except Exception as exc:
                        st.error(f"Re-run submit failed: {exc}")

        st.divider()
        bad_channels = st.text_input(
            "Offending channels (optional, comma-separated), recorded on Fail",
            key=f"fail-ch-{uid}",
        )
        a, r = st.columns(2)
        if a.button("Accept TMA (complete)", key=f"a-{stage}-{uid}", type="primary"):
            try:
                with _logged(f"bgnorm ACCEPT {name} ({uid[:8]})"):
                    result = gate.decide(stage, uid, states.Decision.ACCEPT)
                log.info("bgnorm ACCEPT %r -> %s", name, result)
                st.write(result)
            except Exception as exc:
                st.error(f"Accept failed for {name}: {exc}")
                st.exception(exc)
            else:
                _unit_states.clear()
                st.rerun()
        if r.button("Fail TMA (bad channels)", key=f"r-{stage}-{uid}"):
            try:
                with _logged(f"bgnorm FAIL {name} ({uid[:8]}): channels={bad_channels!r}"):
                    if bad_channels.strip():

                        vocab.ensure()
                        _set_feature(ln.Record.get(uid=uid), BGNORM_FAILED_CHANNELS, bad_channels.strip())
                    result = gate.decide(stage, uid, states.Decision.REJECT)
                log.info("bgnorm FAIL %r -> %s", name, result)
                st.write(result)
            except Exception as exc:
                st.error(f"Fail failed for {name}: {exc}")
                st.exception(exc)
            else:
                _unit_states.clear()
                st.rerun()

    decided = decided_units(stage)
    with st.expander(f"Reopen a decided TMA ({len(decided)})"):
        for uid, name, state in decided:
            cc1, cc2, cc3 = st.columns([3, 1, 1])
            cc1.write(f"{name}: `{state}`")
            cc2.link_button("LaminHub Record", _lamin_record_url(uid))
            if cc3.button("Reopen", key=f"reopen-{stage}-{uid}"):
                try:
                    res = gate.reopen(stage, uid)
                    st.success(f"Reopened {res['unit']} -> {res['state']}")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))


def _review_panel(stage: str, unit_label: str) -> None:
    """One gate's review screen: picker, editor and reopen."""
    gate_cfg = states.GATES[stage]
    edit_prefix = hitl.EDIT_PREFIX_BY_STAGE[stage]

    if st.button("Reload (pending list + latest detection boxes)", key=f"refresh-{stage}"):
        # Clear the caches and the per-unit canvas seeds, so a detect re-run shows its new boxes.
        # Discards in-progress edits.
        st.cache_data.clear()
        for k in [k for k in st.session_state if k.startswith(("seed-", "ver-", "cur-", "cand-", "deard-"))]:
            del st.session_state[k]
        st.rerun()

    units = pending_units(stage)
    st.write(f"**{len(units)}** {unit_label}(s) awaiting review (`{gate_cfg.pending}`)")
    units = _filter_by_source(stage, units)
    if units:
        by_label = {f"{name}  ({uid[:8]})": (uid, name) for uid, name in units}
        pick = st.selectbox(f"{unit_label} to review", list(by_label), key=f"pick-{stage}")
        uid, name = by_label[pick]
        with st.container(border=True):
            st.subheader(name)
            # Loading placeholder while the bundle is fetched.
            slot = st.empty()
            slot.markdown(_skeleton(520, "Loading detection image..."), unsafe_allow_html=True)
            try:
                bundle = load_bundle(edit_prefix, name)
            except Exception as exc:  # show Prefect fetch failures in the UI and logs
                _log_exc(f"load_bundle({edit_prefix!r}, {name!r})", exc)
                slot.empty()
                st.error(f"Could not load the edit bundle for {name}: {exc}")
                st.exception(exc)
                return
            slot.empty()
            n = len(bundle["boxes"]) if bundle else 0
            log.info("%s: loaded bundle for %r (%d box(es))", stage, name, n)
            _review_one(stage, uid, name, bundle)

    decided = decided_units(stage)
    with st.expander(f"Reopen a decided {unit_label} ({len(decided)})"):
        for uid, name, state in decided:
            with st.container(border=True):
                st.markdown(f"**{name}**: `{state}`")
                # Overlay of the accepted boxes on the clean image.
                try:
                    png = _overlay_thumb_png(edit_prefix, stage, name, uid)
                    if png:
                        st.image(png)
                    else:
                        st.caption("No detection overlay found for this unit.")
                except Exception as exc:  # one bad bundle never breaks the list
                    log.info("overlay thumb failed for %r: %s", name, exc)
                    st.caption("Could not render the detection overlay.")
                # A general slide whose TMAs reached core needs cascade, which runs reset-slide.
                cascade = stage == "general" and st.checkbox(
                    "Reset downstream (trash core/bgnorm, purge training pairs)",
                    key=f"casc-{stage}-{uid}",
                )
                cc1, cc2 = st.columns(2)
                cc1.link_button("LaminHub Record", _lamin_record_url(uid))
                if cc2.button("Reopen", key=f"reopen-{stage}-{uid}"):
                    try:
                        res = gate.reopen(stage, uid, cascade=bool(cascade))
                        msg = f"Reopened {res['unit']} -> {res['state']}"
                        if res.get("superseded_tmas"):
                            msg += f"; superseded {len(res['superseded_tmas'])} TMA(s)"
                        if res.get("resetting_tmas"):
                            msg += f"; resetting {len(res['resetting_tmas'])} TMA(s) (teardown running)"
                        st.success(msg)
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))


def main() -> None:
    st.set_page_config(page_title="qptiff pipeline", layout="wide")
    _install_log_capture()  # route INFO logs to stderr first
    st.markdown(SKELETON_CSS, unsafe_allow_html=True)  # loading placeholders
    st.title("qptiff pipeline")

    # A single-panel stage selector, not st.tabs: a canvas in a hidden tab never paints its image.
    # The widget lives in review.stage_nav.
    stage = stage_nav.render(_stage_counts())
    try:
        if stage == "bgnorm":
            _bgnorm_review_panel(stage)  # decision gate, not a box editor
        elif stage == "triage":
            _triage_panel(stage)  # route core_approved TMAs to the bgnorm queue or H&E
        else:
            _review_panel(stage, "slide" if stage == "general" else "TMA")
    except Exception as exc:  # last resort: log and show the traceback
        _log_exc("unhandled error while rendering review panel", exc)
        st.error(f"Unexpected error: {exc}")
        st.exception(exc)


if __name__ == "__main__":
    main()
