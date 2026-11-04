"""Typed detect contexts: what each detect -> review -> persist hop hands to the next.

    GeneralDetectContext / CoreDetectContext
      ├─ raw:      DetectionSet   - straight from the detect job, untouched
      ├─ accepted: AcceptedSet    - after human edits (None until a reviewer accepts)
      ├─ images:   list[ImageRef] - clean image + detection/dearray overlays
      └─ (core)    grid + links   - the dearray lattice and the reviewer's row/col assertions

:class:`Box` holds occupancy in one field, ``detected``. :meth:`Box.from_legacy` is the only place
older spellings are read. :class:`Space` converts both stages' coordinates to full resolution.

At module scope this imports stdlib and pydantic only. It must load in the HPC image, the VM venv
and the review image. Record adapters live in ``context_io``.

:class:`ArtifactRef` records what a stage produced. It is not ``clinicalomx.InputRef``, which is
``extra="forbid"`` and allows only one of ``uid``/``key``/``query``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Bump when a field's meaning changes, not when adding an optional one. Readers switch on it.
SCHEMA_VERSION = 1

Stage = Literal["general", "core"]

# Wire format: unknown keys are ignored, because a reader may run an older image than the writer.
# Add fields, never repurpose them. Bump SCHEMA_VERSION if a meaning changes.
_WIRE = ConfigDict(extra="ignore")


class ArtifactRef(BaseModel):
    """A pointer to a lamindb Artifact: its ``key``, and its ``uid`` once it is saved.

    The writer records the pointer. Readers never rebuild it, because a prediction key needs the
    model uid.
    """

    model_config = _WIRE

    key: str | None = None
    uid: str | None = None
    kind: Literal["artifact", "collection", "record"] = "artifact"

    @model_validator(mode="after")
    def _needs_a_selector(self):
        if not self.key and not self.uid:
            raise ValueError("ArtifactRef needs a `key` or a `uid`")
        return self


class Space(BaseModel):
    """The pixel space a list of boxes lives in, and how to reach full resolution.

        full_res = (v + origin) * scale_factor

    general uses thumbnail px with ``origin = 0``. core uses TMA-crop px, with ``origin`` the
    crop's offset in the low-res image. ``scale_factor`` always means "multiply to reach full-res".
    """

    model_config = _WIRE

    name: Literal["thumbnail", "crop", "full_res"]
    width: int
    height: int
    scale_factor: float = 1.0
    origin_x: float = 0.0
    origin_y: float = 0.0

    @model_validator(mode="after")
    def _positive_dims(self):
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"Space needs positive dims, got {self.width}x{self.height}")
        if self.scale_factor <= 0:
            raise ValueError(f"Space needs a positive scale_factor, got {self.scale_factor}")
        return self

    def to_full_res(self, xyxy) -> tuple[float, float, float, float]:
        """``(x1, y1, x2, y2)`` in this space -> full-resolution px."""
        x1, y1, x2, y2 = (float(v) for v in xyxy)
        return (
            (x1 + self.origin_x) * self.scale_factor,
            (y1 + self.origin_y) * self.scale_factor,
            (x2 + self.origin_x) * self.scale_factor,
            (y2 + self.origin_y) * self.scale_factor,
        )


class Box(BaseModel):
    """One ROI in its :class:`Space`'s pixels, used for canvas, transport, parquet and Record.

    ``detected`` is the occupancy bit: True for a real detection, False for a cell the dearray grid
    inferred. Only :meth:`from_legacy` reads the older spellings.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    cls: str = Field(alias="class")  # keeps the on-JSON key "class" (a python keyword)
    xyxy: tuple[float, float, float, float]
    conf: float | None = None
    detected: bool = True
    rc: tuple[int, int] | None = None  # grid row,col, core only; None = not gridded
    bid: int | None = None  # stable id the reviewer's grid links reference (survives a re-dearray)

    @model_validator(mode="after")
    def _ordered_xyxy(self):
        # Normalise rather than reject: fabric can return a box dragged past its own origin.
        x1, y1, x2, y2 = self.xyxy
        if x2 < x1 or y2 < y1:
            object.__setattr__(self, "xyxy", (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
        return self

    @property
    def inferred(self) -> bool:
        """``not detected``, in the sense ``roi_corrected_json`` and ``inferred_core`` use."""
        return not self.detected

    @classmethod
    def from_legacy(cls, d: dict) -> Box:
        """Build from any historical box dict.

        Occupancy, in priority order. The first key present wins. A missing key means detected,
        and bgnorm relies on that default:

        =================  ====================================  ==================
        key                where it comes from                   meaning
        =================  ====================================  ==================
        ``is_detected``    accepted parquet, Core feature         ``detected``
        ``inferred``       ``roi_corrected_json``                 ``not detected``
        ``occupied``       canvas, grid_links, candidate parquet  ``detected``
        (none)             older payloads                        ``detected=True``
        =================  ====================================  ==================

        ``class == "inferred_core"`` implies not detected only when no explicit flag is present.
        """
        if "is_detected" in d and d["is_detected"] is not None:
            detected = bool(d["is_detected"])
        elif "inferred" in d and d["inferred"] is not None:
            detected = not bool(d["inferred"])
        elif "occupied" in d and d["occupied"] is not None:
            detected = bool(d["occupied"])
        else:
            detected = str(d.get("class", "")) != "inferred_core"
        rc = d.get("rc")
        if rc is None and d.get("row") is not None and d.get("col") is not None:
            rc = (int(d["row"]), int(d["col"]))  # parquet spells it as two columns
        return cls(
            cls=str(d.get("class", "full_core")),
            xyxy=tuple(float(v) for v in d["xyxy"]),
            conf=None if d.get("conf") is None else float(d["conf"]),
            detected=detected,
            rc=None if rc is None else (int(rc[0]), int(rc[1])),
            bid=None if d.get("bid") is None else int(d["bid"]),
        )

    def to_canvas(self) -> dict:
        """The shape ``review.roi_canvas`` / ``review.grid_links`` expect (``occupied`` + ``bid``)."""
        return {
            "class": self.cls,
            "xyxy": list(self.xyxy),
            "occupied": self.detected,
            "rc": None if self.rc is None else list(self.rc),
            "bid": self.bid,
        }

    def to_corrected_json(self) -> dict:
        """The shape ``roi_corrected_json`` holds (``inferred``), for the flows still reading it."""
        return {
            "class": self.cls,
            "xyxy": list(self.xyxy),
            "inferred": self.inferred,
            "rc": None if self.rc is None else list(self.rc),
        }


class ImageRef(BaseModel):
    """One image of a unit: where it lives, and optionally its bytes.

    ``b64`` carries the image to the review app through Prefect. :meth:`_DetectContext.to_json`
    drops it by default.
    """

    model_config = _WIRE

    kind: Literal["clean", "detection_overlay", "dearray_overlay"]
    ref: ArtifactRef | None = None
    b64: str | None = None
    sha256: str | None = None  # ties the transport copy to this ref; see AcceptedSet.from_image_sha256
    width: int | None = None
    height: int | None = None


class DetectionSet(BaseModel):
    """The RAW detector output for a unit, exactly as the detect job produced it."""

    model_config = _WIRE

    boxes: list[Box] = Field(default_factory=list)
    space: Space
    model_uid: str | None = None  # versions the prediction key; see ArtifactRef
    model_alias: str | None = None
    params: dict[str, float] = Field(default_factory=dict)  # conf / iou / nms as actually run
    detected_at: datetime | None = None


class AcceptedSet(BaseModel):
    """The state a human accepted: boxes added, moved, deleted, reclassified, and (core) gridded.

    Same space as the raw set. ``modified`` feeds harvest-feedback, which takes only edited accepts.
    """

    model_config = _WIRE

    boxes: list[Box] = Field(default_factory=list)
    space: Space
    modified: bool = False
    accepted_at: datetime | None = None
    review_run: str | None = None
    # sha256 of the clean image the reviewer saw. Compare it with the current ImageRef.sha256 to
    # detect a grid drawn on a crop from a different detect run.
    from_image_sha256: str | None = None

    def duplicate_cells(self) -> list[tuple[int, int]]:
        """Grid cells claimed by more than one box. Reported, never rejected.

        Two boxes on one cell collapse into a single Core at persist, and one of them is lost.
        """
        seen: set[tuple[int, int]] = set()
        dupes: set[tuple[int, int]] = set()
        for b in self.boxes:
            if b.rc is None:
                continue
            rc = (b.rc[0], b.rc[1])
            (dupes if rc in seen else seen).add(rc)
        return sorted(dupes)

    @property
    def fully_gridded(self) -> bool:
        """Every box carries a ``row,col``. False means core-persist must re-fit the grid."""
        return bool(self.boxes) and all(b.rc is not None for b in self.boxes)


class GridShape(BaseModel):
    """The fitted dearray lattice, field-for-field what ``review.grid_links.grid_params`` emits."""

    model_config = _WIRE

    n_rows: int
    n_cols: int
    origin: tuple[float, float] | None = None
    row_vec: tuple[float, float] | None = None
    col_vec: tuple[float, float] | None = None
    cell_size: tuple[float, float] | None = None
    angle_rad: float | None = None


class GridLink(BaseModel):
    """A reviewer's assertion that two cores share a row (or a column).

    Keyed by ``bid``, never by list index, so it survives a re-dearray, a delete or a re-index.
    """

    model_config = _WIRE

    a: int
    b: int
    axis: Literal["row", "col"] = "row"


class _DetectContext(BaseModel):
    """Shared envelope. Use :class:`GeneralDetectContext` / :class:`CoreDetectContext`."""

    model_config = _WIRE

    schema_version: int = SCHEMA_VERSION
    stage: Stage
    unit_name: str  # the Record name, a load-bearing identity (Core names encode R<r>C<c>)
    unit_uid: str | None = None
    source_image: ArtifactRef | None = None  # the qptiff everything derives from
    raw: DetectionSet
    accepted: AcceptedSet | None = None  # None = never accepted (the sentinel the app reads)
    images: list[ImageRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _spaces_agree(self):
        # Raw and accepted boxes share the review px. A mismatch means the grid belongs to a crop
        # from a different detect run.
        if self.accepted is not None and self.accepted.space.name != self.raw.space.name:
            raise ValueError(
                f"accepted space {self.accepted.space.name!r} != raw space {self.raw.space.name!r}"
            )
        return self

    @property
    def is_accepted(self) -> bool:
        return self.accepted is not None

    @property
    def boxes(self) -> list[Box]:
        """What to display/persist: the accepted boxes if there are any, else the raw detections."""
        return self.accepted.boxes if self.accepted is not None else self.raw.boxes

    def image(self, kind: str) -> ImageRef | None:
        return next((im for im in self.images if im.kind == kind), None)

    def to_json(self, *, with_image_bytes: bool = False) -> str:
        """Serialise. ``with_image_bytes=False`` strips every ``b64``, so the stored form is small.

        The Prefect transport passes True. ``by_alias`` keeps the JSON key ``class``.
        """
        exclude = None if with_image_bytes else {"images": {"__all__": {"b64"}}}
        return self.model_dump_json(by_alias=True, exclude=exclude)


class GeneralDetectContext(_DetectContext):
    """TMA / tissue_section ROIs on a WholeSlide thumbnail."""

    stage: Literal["general"] = "general"


class CoreDetectContext(_DetectContext):
    """Cores on one TMA crop, plus the dearray grid the reviewer accepted."""

    stage: Literal["core"] = "core"
    grid: GridShape | None = None
    links: list[GridLink] = Field(default_factory=list)


CONTEXT_BY_STAGE: dict[str, type[_DetectContext]] = {
    "general": GeneralDetectContext,
    "core": CoreDetectContext,
}


def accepted_cells(accepted: AcceptedSet) -> list[dict]:
    """The accepted grid as plain row dicts, in accepted order: the input to the cores parquet.

    Row and column decide Core identity. A changed row/col creates a new Core and orphans the old
    one with its bgnorm results. Keep the order: downstream reads geometry by ``parquet_row``.
    ``geometry`` is left to the caller.
    """
    return [
        {
            "row": int(b.rc[0]) if b.rc else 0,
            "col": int(b.rc[1]) if b.rc else 0,
            "class": b.cls,
            "conf": 1.0 if b.conf is None else float(b.conf),
            "detection_idx": i,
            "occupied": b.detected,
            "is_detected": b.detected,
            "xyxy": list(b.xyxy),
        }
        for i, b in enumerate(accepted.boxes)
    ]


def parse_context(stage: str, raw: str | bytes | dict) -> _DetectContext:
    """Deserialise a stored context for ``stage`` (JSON text or an already-decoded dict)."""
    model = CONTEXT_BY_STAGE[stage]
    if isinstance(raw, (str, bytes)):
        return model.model_validate_json(raw)
    return model.model_validate(raw)
