"""The structured form of an extraction request: the only thing an agent may author.

``extra="forbid"`` rejects unknown fields, and "exactly one of" rules make ambiguous requests
unconstructable. Not a ``YamlSettings``: environment variables never change a request. A request
round-trips through YAML.
"""
from __future__ import annotations

import pathlib
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from clinicalomx.extract import Layout

_FORBID = ConfigDict(extra="forbid")


class RoiQuery(BaseModel):
    """A set-valued ROI predicate. Tier 1 fields filter in SQL; tier 2 fields scan in memory."""

    model_config = _FORBID

    # Tier 1: filtered in SQL.
    roi_type: str | None = None
    name_contains: str | None = None
    state: str | None = None
    parent: str | None = None
    has_roles: list[str] = Field(default_factory=list)

    # Tier 2: scanned in memory over the ROI index.
    cohort: str | None = None
    patient_id: str | None = None
    slide: str | None = None

    #: Maximum ROIs returned. Never unbounded.
    limit: int = Field(default=500, ge=1, le=100_000)

    #: Tier-1 field -> its ``narrow()`` keyword, and the tier-2 field names. The single list of
    #: predicates; ``select()``, ``_scan`` and ``is_empty`` all read it.
    PUSHDOWN: ClassVar[dict[str, str]] = {
        "roi_type": "roi_type", "name_contains": "name_contains", "state": "state",
        "parent": "parent_uid", "has_roles": "has_roles"}
    SCANNED: ClassVar[tuple[str, ...]] = ("cohort", "patient_id", "slide")

    @property
    def is_empty(self) -> bool:
        """No predicate asked for."""
        return not any(getattr(self, f) for f in (*self.PUSHDOWN, *self.SCANNED))

    @model_validator(mode="after")
    def _check_query(self):
        if self.is_empty:
            raise ValueError(
                "an ROI query needs at least one predicate; an unfiltered query over every ROI "
                "on the instance is almost never what was meant")
        return self


class ROISelector(BaseModel):
    """Which ROI, or ROIs, the request is about. Exactly one of uid / name / fuzzy / query."""

    model_config = _FORBID

    uid: str | None = None
    name: str | None = None
    #: Free text for the deterministic matcher. Must resolve to exactly one ROI.
    fuzzy: str | None = None
    query: RoiQuery | None = None

    include: Literal["self", "children", "descendants"] = "self"
    child_type: str | None = None

    @model_validator(mode="after")
    def _check_selector(self):
        chosen = [f for f in ("uid", "name", "fuzzy", "query") if getattr(self, f) is not None]
        if len(chosen) != 1:
            raise ValueError(
                "an ROI selector needs exactly one of `uid`, `name`, `fuzzy` or `query`; "
                f"got {chosen or 'none'}")
        if self.child_type and self.include == "self":
            raise ValueError(
                "`child_type` narrows children, so set include to 'children' or 'descendants'")
        return self


class AddedElement(BaseModel):
    """An extra slot beyond what the recipe declares, such as a second image."""

    model_config = _FORBID

    #: The slot name, and the key it gets in the output. Must not collide with a recipe element.
    name: str = Field(min_length=1)
    #: The Artifact-valued feature to read.
    role: str = Field(min_length=1)
    kind: Literal["image", "labels", "shapes", "table", "points", "attrs"] = "image"
    #: Variable within a multi-variable artifact (zarr only).
    variable: str | None = None
    level: Literal["self", "children", "parent"] = "self"
    #: ROI-side features the placement needs. Defaults to the recipe's own.
    needs_features: list[str] | None = None
    required: bool = False


class ElementSpec(BaseModel):
    """Which slots of the recipe to build. Empty means the recipe's own default set."""

    model_config = _FORBID

    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    #: Elements the recipe calls required that this request is willing to do without.
    optional: list[str] = Field(default_factory=list)
    #: ``{element: role}``: pin one slot to one role. An ROI lacking it reports absent; there is
    #: no fallback.
    roles: dict[str, str] = Field(default_factory=dict)
    #: Extra slots beyond the recipe's own. See :class:`AddedElement`.
    add: list[AddedElement] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_elements(self):
        names = [a.name for a in self.add]
        if len(names) != len(set(names)):
            raise ValueError(
                "two added elements share a name; they would write to the same output slot and "
                "one would silently overwrite the other")
        if self.include and self.exclude:
            raise ValueError("`include` and `exclude` are mutually exclusive; use one")
        overlap = set(self.include) & set(self.optional)
        if overlap:
            raise ValueError(
                f"{sorted(overlap)} are both explicitly included and marked optional, which does "
                "not say whether their absence should block")
        pinned_and_excluded = set(self.roles) & set(self.exclude)
        if pinned_and_excluded:
            raise ValueError(
                f"{sorted(pinned_and_excluded)} are both pinned to a role and excluded, which "
                "does not say whether to build them")
        return self


class ChannelSpec(BaseModel):
    """Channel subsetting. Exactly one of names or indices."""

    model_config = _FORBID

    names: list[str] = Field(default_factory=list)
    indices: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_channels(self):
        if bool(self.names) == bool(self.indices):
            raise ValueError("channel subsetting needs exactly one of `names` or `indices`")
        return self


class Destination(BaseModel):
    """Where the output goes: a local path by default, or a registered ``ln.Artifact``."""

    model_config = _FORBID

    kind: Literal["path", "artifact", "memory"] = "path"
    path: str | None = None
    #: Artifact key prefix, for ``kind="artifact"``.
    key: str | None = None
    overwrite: bool = False

    @model_validator(mode="after")
    def _check_destination(self):
        if self.kind == "path" and not self.path:
            raise ValueError("destination kind 'path' needs `path`")
        if self.kind == "artifact" and not self.key:
            raise ValueError("destination kind 'artifact' needs `key`")
        if self.kind == "memory" and (self.path or self.key):
            raise ValueError("destination kind 'memory' takes neither `path` nor `key`")
        if self.kind == "path" and self.key:
            raise ValueError("`key` names an artifact; use kind 'artifact' to register one")
        return self

    @property
    def registers(self) -> bool:
        return self.kind == "artifact"


class ExtractRequest(BaseModel):
    """The single structured target of natural-language translation."""

    model_config = _FORBID

    roi: ROISelector
    #: The output data structure: "SpatialData", "OME-TIFF".
    target: str
    #: Pin one recipe by name. Otherwise it is chosen from ``target`` and the ROI's subtype.
    recipe: str | None = None
    #: ``None`` means the chosen recipe's own layout.
    layout: Layout | None = None

    elements: ElementSpec = Field(default_factory=ElementSpec)
    channels: ChannelSpec | None = None
    destination: Destination
    strictness: Literal["strict", "degrade", "best_effort"] = "strict"
    include_storage: bool = False
    #: Read each image's recorded origin and cross-check the metadata placements against it.
    #: Opens image metadata.
    verify_placement: bool = False
    #: Take every Nth pixel in a merged product. Part of the plan token.
    downsample: int = Field(default=1, ge=1)
    label: str | None = None

    @model_validator(mode="after")
    def _check_request(self):
        if self.layout == "merged" and self.roi.include != "self":
            raise ValueError(
                "layout 'merged' produces ONE object for the ROI you named, with an element per "
                "child; set roi.include to 'self'")
        if self.strictness == "strict" and self.elements.optional:
            raise ValueError(
                "`elements.optional` only means something under 'degrade' or 'best_effort'; "
                "under 'strict' an absent element blocks whether or not it is listed")
        return self

    @classmethod
    def from_yaml(cls, path) -> ExtractRequest:

        import yaml

        return cls.model_validate(yaml.safe_load(pathlib.Path(path).read_text()))

    def to_yaml(self) -> str:
        import yaml

        return yaml.safe_dump(self.model_dump(exclude_none=True), sort_keys=False)
