"""Plan, then apply. ``plan`` is a dry run that writes nothing; ``assemble`` executes the Plan.

``assemble`` takes a :class:`Plan`, never a request, and refuses a plan older than
:data:`STALE_AFTER`. ``Plan.token`` is a content hash of what would be built.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from clinicalomx.extract import Element, Recipe, all_recipes, recipes_for
from clinicalomx.extract import relations as rel
from clinicalomx.extract.assemble import resolve_dotted
from clinicalomx.extract.availability import Availability, RecipeReadiness, describe
from clinicalomx.extract.coords import PlacementError, cross_check
from clinicalomx.geometry import roi_id_for
from clinicalomx.governance import permits_derive
from clinicalomx.snapshot import Snapshot

#: How long a plan may sit before ``assemble`` insists on a fresh one, in seconds.
STALE_AFTER = 30 * 60


@dataclass(frozen=True)
class Unit:
    """One output the plan would produce."""

    roi_uid: str
    roi_name: str
    output: str
    #: ``{element: role}`` for the elements written for this unit, resolved per unit.
    elements: dict[str, str]
    #: ``{child name: {element: role}}``, for ``layout="merged"`` only.
    child_elements: dict[str, dict[str, str]] = field(default_factory=dict)
    omitted: tuple[str, ...] = ()
    verdict: str = "build"
    reason: str = ""

    def to_dict(self) -> dict:
        return {"roi_uid": self.roi_uid, "roi_name": self.roi_name, "output": self.output,
                "elements": dict(self.elements),
                "child_elements": {k: dict(v) for k, v in self.child_elements.items()},
                "omitted": list(self.omitted),
                "verdict": self.verdict, "reason": self.reason}


@dataclass
class Plan:
    """What ``assemble`` would do. Writes nothing to produce."""

    recipe: str
    target: str
    layout: str
    roi_name: str
    availability: Availability
    units: list[Unit]
    blocked: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: ``{output key: governance verdict}``. Only when the destination registers.
    governance: dict[str, str] = field(default_factory=dict)
    #: ``{roi name: Placed}`` when `verify_placement` ran.
    placement: dict = field(default_factory=dict)
    request: dict | None = None
    index_built_at: str = ""
    bytes_checked: bool = False
    #: The recipe's content at plan time. Part of the token.
    recipe_content: dict = field(default_factory=dict)
    #: How the first buildable unit's elements relate: which image each labels element came from,
    #: and which labels each table annotates.
    relations: object | None = None

    @property
    def buildable(self) -> list[Unit]:
        return [u for u in self.units if u.verdict == "build"]

    @property
    def token(self) -> str:
        """Content hash over the recipe's content, units and resolved artifact keys."""
        blob = json.dumps(
            {"recipe": self.recipe, "recipe_content": self.recipe_content,
             "roi": self.roi_name, "layout": self.layout,
             "units": [u.to_dict() for u in self.units],
             "artifact_keys": sorted(self.artifact_keys),
             "placement": {k: v.to_dict() if hasattr(v, "to_dict") else v
                           for k, v in sorted(self.placement.items())},
             "blocked": sorted(self.blocked), "index_built_at": self.index_built_at,
             # `downsample` changes the pixels, so it is part of the token.
             "downsample": (self.request or {}).get("downsample", 1)},
            sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    @property
    def artifact_keys(self) -> list[str]:
        """Every artifact key that would be read, resolved through `newest_artifact`."""
        out: set[str] = set()
        for element in self.availability.recipes[0].elements if self.availability.recipes else ():
            out.update(element.keys)
        return sorted(out)

    def is_stale(self, *, now: str | None = None, max_age: int = STALE_AFTER) -> bool:
        if not self.index_built_at:
            return True
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        built = datetime.strptime(self.index_built_at, fmt).replace(tzinfo=UTC)
        current = (datetime.strptime(now, fmt).replace(tzinfo=UTC) if now
                   else datetime.now(UTC))
        return (current - built).total_seconds() > max_age

    def to_dict(self) -> dict:
        return {"token": self.token, "recipe": self.recipe, "target": self.target,
                "layout": self.layout, "roi_name": self.roi_name,
                "units": [u.to_dict() for u in self.units],
                "blocked": list(self.blocked), "warnings": list(self.warnings),
                "governance": dict(self.governance),
                "n_buildable": len(self.buildable),
                "index_built_at": self.index_built_at, "bytes_checked": self.bytes_checked,
                "availability": self.availability.to_dict(), "request": self.request,
                "placement": {k: v.to_dict() if hasattr(v, "to_dict") else v
                              for k, v in self.placement.items()},
                "relations": self.relations.to_dict() if self.relations is not None else None}

    def caveats(self) -> list[str]:
        out = list(self.availability.caveats())
        if self.relations is not None:
            out.extend(self.relations.caveats())
        if self.blocked:
            out.append(
                f"{len(self.blocked)} element(s) block this plan: {', '.join(self.blocked)}. "
                f"Nothing will be built until they resolve.")
        return out

    def summarise(self, *, max_examples: int = 5) -> tuple[str, bool]:
        """``(report, has_blocking)``."""
        lines = [
            f"recipe   {self.recipe}  [{self.target}, {self.layout}]",
            f"roi      {self.roi_name}",
            f"index    {self.index_built_at}"
            + ("" if self.bytes_checked else "   bytes NOT verified"),
            f"token    {self.token}",
            "",
        ]
        if self.blocked:
            lines.append(f"BLOCKED: {', '.join(self.blocked)}")
            lines.append("")
        lines.append(f"{len(self.buildable)} unit(s) would be built"
                     + (f", {len(self.units) - len(self.buildable)} skipped" if
                        len(self.units) > len(self.buildable) else ""))
        for u in self.units[:max_examples]:
            mark = "build" if u.verdict == "build" else u.verdict.upper()
            detail = ", ".join(f"{k}<-{v}" for k, v in u.elements.items()) or "nothing"
            lines.append(f"  {mark:5} {u.roi_name}")
            lines.append(f"        {u.output}")
            lines.append(f"        {detail}"
                         + (f"   omitted: {', '.join(u.omitted)}" if u.omitted else "")
                         + (f"   {u.reason}" if u.reason else ""))
        if len(self.units) > max_examples:
            lines.append(f"  ... and {len(self.units) - max_examples} more")
        if self.relations is not None and self.relations.edges:
            lines.append("")
            lines.append("relations")
            lines.append(self.relations.summarise())
        for name, placed in sorted(self.placement.items()):
            lines.append(f"  placement   {name}  <- {placed.source}")
            for strategy, verdict in sorted(placed.verdicts.items()):
                lines.append(f"                {strategy}: {verdict}")
        for key, verdict in sorted(self.governance.items()):
            lines.append(f"  governance  {key}: {verdict}")
        for w in self.warnings:
            lines.append(f"  WARNING  {w}")
        return "\n".join(lines), bool(self.blocked)


def _pick_recipe(request, roi_type: str) -> Recipe:
    if request.recipe:
        found = next((r for r in all_recipes() if r.name == request.recipe), None)
        if found is None:
            raise LookupError(f"no recipe named {request.recipe!r}; "
                              f"run `clinicalomx-extract recipes` to see the catalogue")
        if roi_type not in found.roi_types:
            raise ValueError(
                f"recipe {found.name!r} is for {', '.join(found.roi_types)}, not {roi_type!r}")
        return found

    matches = recipes_for(roi_type, target=request.target)
    if request.layout is not None:
        matches = [r for r in matches if r.layout == request.layout]
    if not matches:
        raise LookupError(
            f"no recipe produces {request.target!r} from a {roi_type}"
            + (f" with layout {request.layout!r}" if request.layout else "")
            + "; run `clinicalomx-extract recipes` to see the catalogue")
    if len(matches) > 1:
        names = ", ".join(r.name for r in matches)
        raise ValueError(
            f"{len(matches)} recipes produce {request.target!r} from a {roi_type} ({names}). "
            f"Pin one with `recipe`, or narrow with `layout`.")
    return matches[0]


def _pin_roles(recipe: Recipe, pins: dict[str, str]) -> Recipe:
    """A copy of ``recipe`` with the named elements narrowed to one candidate role each.

    A pinned element the ROI cannot supply reads absent; it never falls back.
    """
    if not pins:
        return recipe
    unknown = sorted(set(pins) - {e.name for e in recipe.elements})
    if unknown:
        raise ValueError(
            f"recipe {recipe.name!r} has no element(s) {unknown}; it has "
            f"{sorted(e.name for e in recipe.elements)}")
    elements = []
    for element in recipe.elements:
        role = pins.get(element.name)
        if role is None:
            elements.append(element)
            continue
        if role not in element.roles:
            raise ValueError(
                f"element {element.name!r} cannot be filled by {role!r}; its candidates are "
                f"{list(element.roles)}. Pinning a role the recipe does not declare would build "
                f"an element out of something nothing has said belongs there.")
        elements.append(replace(element, roles=(role,)))
    return replace(recipe, elements=tuple(elements))


def _add_elements(recipe: Recipe, added) -> Recipe:
    """A copy of ``recipe`` with the caller's extra slots appended, before availability."""

    if not added:
        return recipe
    existing = {e.name for e in recipe.elements}
    clash = sorted({a.name for a in added} & existing)
    if clash:
        raise ValueError(
            f"recipe {recipe.name!r} already declares {clash}; pin the role with "
            f"`elements.roles` instead of adding a slot that would collide with it")

    # An added image needs the same placement features as the declared images.
    default_needs = next((e.needs_features for e in recipe.elements if e.kind == "image"), ())
    extra = tuple(
        Element(
            name=a.name, kind=a.kind, roles=(a.role,), required=a.required, level=a.level,
            variable=a.variable,
            needs_features=tuple(a.needs_features) if a.needs_features is not None
            else (default_needs if a.kind in ("image", "labels") else ()),
            description=f"added by the request: {a.role}"
                        + (f"#{a.variable}" if a.variable else ""),
        )
        for a in added)
    return replace(recipe, elements=recipe.elements + extra)


def _output_for(request, recipe: Recipe, roi_name: str) -> str:
    suffix = {"OME-TIFF": ".ome.tiff", "SpatialData": ".zarr",
              "OME-Zarr": ".ome.zarr"}.get(recipe.target, "")
    stem = roi_name.replace(":", "_")
    if request.destination.kind == "artifact":
        return f"{request.destination.key.rstrip('/')}/{stem}{suffix}"
    if request.destination.kind == "memory":
        return f"<memory:{roi_id_for(roi_name)}>"
    return f"{request.destination.path.rstrip('/')}/{stem}{suffix}"


def _units(request, recipe: Recipe, readiness: RecipeReadiness, roi, snap) -> list[Unit]:
    """One Unit per output, with the elements that would actually be written."""
    targets = recipe.targets_under(roi, snap) if recipe.layout == "per_child" else [roi]

    wanted = {e.element: e for e in readiness.elements}
    if request.elements.include:
        wanted = {k: v for k, v in wanted.items() if k in request.elements.include}
    for name in request.elements.exclude:
        wanted.pop(name, None)

    units = []
    for target in targets:
        name = str(target.name)
        elements, omitted, reason = {}, [], ""
        for label, e in wanted.items():
            # `role_for` gives this ROI's own role.
            role = e.role_for(name) if e.level == "self" else e.chosen_role
            usable = e.satisfied and name not in e.unplaceable
            if e.level == "self":
                usable = usable and name in e.per_target_role
            if usable:
                elements[label] = role
            else:
                omitted.append(label)
                if e.required and label not in request.elements.optional:
                    reason = f"required element {label!r} is not available here"
        child_elements: dict[str, dict[str, str]] = {}
        if recipe.layout == "merged":
            for label, e in wanted.items():
                if e.level != "children":
                    continue
                for child, child_role in e.per_target_role.items():
                    child_elements.setdefault(child, {})[label] = child_role
        verdict = "skip" if reason else "build"
        units.append(Unit(target.uid, name, _output_for(request, recipe, name),
                          elements, child_elements, tuple(omitted), verdict, reason))
    return units


def plan(request, *, snapshot=None, now: str | None = None, source=None, index=None) -> Plan:
    """Resolve a request against the instance. Writes nothing.

    Raises on an ambiguous selector or a target several recipes could serve. ``index`` is passed
    to ``describe``.
    """
    from clinicalomx.extract.select import select

    snap = index.snapshot if index is not None else (
        snapshot if snapshot is not None else Snapshot())
    roots = select(request.roi, snapshot=snap, source=source)
    if not roots:
        raise LookupError("the ROI selector matched nothing")
    if len(roots) > 1:
        raise ValueError(
            f"the ROI selector matched {len(roots)} ROIs; plan one at a time, or use a recipe "
            f"whose layout fans out over children")
    roi = roots[0]

    roi_type = _type_of(roi)
    recipe = _add_elements(
        _pin_roles(_pick_recipe(request, roi_type), request.elements.roles),
        request.elements.add)
    av = describe(roi, snapshot=None if index is not None else snap,
                  include_storage=request.include_storage,
                  recipes=[recipe], now=now, source=source, index=index)
    readiness = av.recipes[0]

    blocked = []
    if request.strictness != "best_effort":
        blocked = [e.element for e in readiness.blocking
                   if e.element not in request.elements.optional]
        if request.strictness == "strict":
            named = set(request.elements.include)
            blocked += [e.element for e in readiness.elements
                        if e.element in named and not e.satisfied and e.element not in blocked]

    units = [] if blocked else _units(request, recipe, readiness, roi, snap)
    warnings = list(av.warnings)
    if request.layout is not None and request.layout != recipe.layout:
        warnings.append(f"requested layout {request.layout!r} but recipe {recipe.name!r} is "
                        f"{recipe.layout!r}")
    if request.channels is not None:
        warnings.append("channel subsetting is validated by the builder, not by plan")

    placement_evidence: dict = {}
    if request.verify_placement:
        placement_evidence = _verify_placement(units, recipe, snap)
        for name, placed in placement_evidence.items():
            if placed.disagreements:
                warnings.append(
                    f"{name}: {', '.join(placed.disagreements)} disagree with what the image "
                    f"records about its own origin. Placing from the evidence; the metadata is "
                    f"wrong and extract.placement_disagrees_with_evidence will say so.")

    governance: dict[str, str] = {}
    if request.destination.registers:
        for u in units:
            governance[u.output] = "derive" if permits_derive(u.output) else "DENIED"

    # Resolved for the first buildable unit only: every unit has the same shape.
    relations = None
    first = next((u for u in units if u.verdict == "build"), None)
    if first is not None and recipe.target == "SpatialData":
        relations = rel.resolve(
            first.roi_name,
            rel.planned_entries(first, recipe, str(roi.name), snap),
            snap)

    return Plan(recipe=recipe.name, target=recipe.target, layout=recipe.layout,
                recipe_content=recipe.to_dict(),
                roi_name=str(roi.name), availability=av, units=units,
                blocked=blocked, warnings=warnings, governance=governance,
                placement=placement_evidence, relations=relations,
                request=request.model_dump(exclude_none=True),
                index_built_at=av.index_built_at, bytes_checked=av.bytes_checked)


def _verify_placement(units, recipe, snap) -> dict:
    """Place each unit from what its image records, and cross-check the metadata strategies.

    Reads zarr metadata only. A failure is a per-unit warning.
    """
    from clinicalomx.extract.builders.imaging import open_image
    from clinicalomx.extract.builders.ometiff import _artifact_for, _resolve

    metadata = resolve_dotted(recipe.placement)()
    out: dict = {}
    for unit in units:
        try:
            roi, values, row, frame = _resolve(unit, snap)
        except Exception as exc:  # noqa: BLE001 - one unverifiable unit must not fail the plan
            out[unit.roi_name] = cross_check(None, {"resolve": str(exc)})
            continue

        origin = None
        for name, role in unit.elements.items():
            element = recipe.element(name)
            art = _artifact_for(roi, role, values, snap)
            if art is None:
                continue
            try:
                opened = open_image(art, variable=element.variable if element else None)
            except Exception:  # noqa: BLE001 - unreadable here means no evidence, not a failure
                continue
            if opened.origin is not None:
                origin = opened.origin.bounds
                break

        try:
            claimed = metadata.bounds(values=values, geometry_row=row,
                                      container_attrs={"frame": frame})
        except PlacementError as exc:
            claimed = str(exc)
        try:
            out[unit.roi_name] = cross_check(
                origin, {type(metadata).__name__: claimed})
        except PlacementError as exc:
            out[unit.roi_name] = cross_check(None, {"all": str(exc)})
    return out


def nearest_buildable(request, *, snapshot=None, now: str | None = None,
                      source=None, index=None) -> list[dict]:
    """What would build for this ROI instead: every recipe's real verdict, best first.

    Ready first, then degraded by unit count, then blocked.
    """
    from clinicalomx.extract.select import select

    snap = index.snapshot if index is not None else (
        snapshot if snapshot is not None else Snapshot())
    roots = select(request.roi, snapshot=snap, source=source)
    if not roots:
        raise LookupError("the ROI selector matched nothing")
    roi = roots[0]

    candidates = recipes_for(_type_of(roi))
    if not candidates:
        return []
    av = describe(roi, snapshot=None if index is not None else snap,
                  recipes=candidates, now=now, source=source, index=index)

    rank = {"ready": 0, "degraded": 1, "blocked": 2}
    out = []
    for r in sorted(av.recipes, key=lambda r: (rank[r.verdict], -r.n_units, r.recipe)):
        out.append({
            "recipe": r.recipe, "target": r.target, "layout": r.layout,
            "verdict": r.verdict, "n_units": r.n_units,
            "elements": {e.element: e.chosen_role for e in r.elements if e.satisfied},
            "missing": [e.element for e in r.elements if not e.satisfied],
            "why_blocked": [f"{e.element}: {e.state}" for e in r.blocking],
            "is_what_was_asked": r.recipe == request.recipe or r.target == request.target,
        })
    return out


def _type_of(record) -> str:
    rtype = getattr(record, "type", None)
    return str(getattr(rtype, "name", rtype)) if rtype is not None else ""
