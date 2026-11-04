"""What a given ROI can produce right now, and why not, where not. Reports only; never builds.

Each role resolves to one of seven states. ``missing`` is reported only when bytes were probed;
otherwise ``present`` is starred and the MISSING column prints ``-``. ``undeclared`` is relative
to the vocabularies installed here. ``denied`` (governance) is distinct from ``missing``.

``child_coverage`` counts stores per role. ``child_elements`` counts what a recipe can address,
including several variables in one store; store totals come from ``child_stores``.
Resolution is shared with :mod:`clinicalomx.extract.index`.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal

from clinicalomx.extract import Element, Layout, Recipe, all_recipes, is_inferred_cell
from clinicalomx.extract.builders.imaging import StoreContents, describe_store
from clinicalomx.snapshot import INHERITED_FEATURES, Snapshot

RoleState = Literal[
    "undeclared", "unlinked", "denied", "dangling", "missing", "unplaceable", "present"]

#: How usable each state is. A role with several artifacts takes the best; a coverage figure over
#: many ROIs takes the worst.
_RANK: dict[str, int] = {
    "undeclared": 0, "unlinked": 1, "denied": 2, "dangling": 3, "missing": 4,
    "unplaceable": 5, "present": 6,
}

_WHY: dict[str, str] = {
    "undeclared": ("neither this instance nor any installed vocabulary declares this role - "
                   "the name may be a typo, or nothing has ever produced one"),
    "unlinked": "declared, but this ROI has no artifact under it",
    "denied": "governance forbids reading these bytes",
    "dangling": "a value exists but no Artifact is registered under that key",
    "missing": "registered, but the bytes are not in storage",
    "unplaceable": ("the artifact IS present, but nothing on this ROI says where it belongs - "
                    "this is usually a declared needs_features that is never populated"),
    "present": "",
}

Verdict = Literal["ready", "degraded", "blocked"]


def _best(states) -> RoleState:
    return max(states, key=lambda s: _RANK[s], default="unlinked")


def _worst(states) -> RoleState:
    return min(states, key=lambda s: _RANK[s], default="unlinked")


@dataclass(frozen=True)
class ROIRef:
    uid: str
    name: str
    roi_type: str
    parent_name: str | None = None

    @property
    def slide(self) -> str:
        """The WholeSlide stem of the name."""
        return self.name.split(":")[0]

    @classmethod
    def of(cls, record, parent=None) -> ROIRef:
        rtype = getattr(record, "type", None)
        return cls(
            uid=record.uid, name=str(record.name),
            roi_type=str(getattr(rtype, "name", rtype)) if rtype is not None else "",
            parent_name=str(parent.name) if parent is not None else None,
        )

    def to_dict(self) -> dict:
        return {"uid": self.uid, "name": self.name, "roi_type": self.roi_type,
                "parent_name": self.parent_name, "slide": self.slide}


@dataclass(frozen=True)
class RoleAvailability:
    """One role, resolved against one ROI."""

    role: str
    state: RoleState
    keys: tuple[str, ...] = ()
    owner: str | None = None
    detail: str = ""

    def to_dict(self) -> dict:
        return {"role": self.role, "state": self.state, "keys": list(self.keys),
                "owner": self.owner, "detail": self.detail or _WHY[self.state]}


@dataclass(frozen=True)
class Coverage:
    """A role's coverage over a set of ROIs, such as 94 of 96 cores having an image."""

    role: str
    total: int
    #: Present and placeable.
    present: int = 0
    missing: int = 0
    dangling: int = 0
    denied: int = 0
    unlinked: int = 0
    undeclared: int = 0
    #: Has the artifact but lacks the ROI-side features the placement needs.
    unplaceable: int = 0
    state: RoleState = "unlinked"
    owner: str | None = None
    #: ``{state: (roi name, ...)}``, capped.
    examples: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def fraction(self) -> str:
        return f"{self.present}/{self.total}"

    def to_dict(self) -> dict:
        return {"role": self.role, "total": self.total, "present": self.present,
                "missing": self.missing, "dangling": self.dangling, "denied": self.denied,
                "unlinked": self.unlinked, "undeclared": self.undeclared,
                "unplaceable": self.unplaceable, "state": self.state, "owner": self.owner,
                "examples": {k: list(v) for k, v in self.examples.items()}}


@dataclass(frozen=True)
class ElementAvailability:
    """One slot of one recipe, resolved: which role fills it, and over how many ROIs."""

    element: str
    kind: str
    required: bool
    level: str
    #: The preferred candidate role actually in use. ``None`` means nothing resolved.
    chosen_role: str | None
    state: RoleState
    #: The array within the artifact, where the element names one.
    variable: str | None = None
    #: Set when ``level == "children"``: the winning role's spread over the children.
    coverage: Coverage | None = None
    keys: tuple[str, ...] = ()
    #: ROIs that have the artifact but lack the ROI-side features the placement needs.
    unplaceable: tuple[str, ...] = ()
    candidates: tuple[RoleAvailability, ...] = ()
    #: ``{role: how many targets it fills}``, in the recipe's preference order.
    roles_used: dict[str, int] = field(default_factory=dict)
    #: ``{roi name: the role that fills this element there}``.
    per_target_role: dict[str, str] = field(default_factory=dict)

    def role_for(self, roi_name: str) -> str | None:
        return self.per_target_role.get(roi_name, self.chosen_role)

    @property
    def satisfied(self) -> bool:
        """At least one target can supply this element. Unplaceable targets do not count."""
        return self.state == "present"

    def to_dict(self) -> dict:
        return {"element": self.element, "kind": self.kind, "required": self.required,
                "level": self.level, "variable": self.variable,
                "chosen_role": self.chosen_role, "state": self.state,
                "coverage": self.coverage.to_dict() if self.coverage else None,
                "keys": list(self.keys), "unplaceable": list(self.unplaceable),
                "roles_used": dict(self.roles_used),
                "per_target_role": dict(self.per_target_role),
                "candidates": [c.to_dict() for c in self.candidates]}


@dataclass(frozen=True)
class RecipeReadiness:
    """Whether one recipe could be built for one ROI, and what it would cost in completeness."""

    recipe: str
    target: str
    roi_type: str
    layout: Layout
    verdict: Verdict
    elements: tuple[ElementAvailability, ...]
    n_units: int
    notes: tuple[str, ...] = ()
    #: Elements that block, including a whole ``requires_any`` group.
    blocking_elements: tuple[str, ...] = ()

    @property
    def blocking(self) -> tuple[ElementAvailability, ...]:
        return tuple(e for e in self.elements if e.element in self.blocking_elements)

    @property
    def degraded(self) -> tuple[ElementAvailability, ...]:
        return tuple(e for e in self.elements if not e.required and not e.satisfied)

    def to_dict(self) -> dict:
        return {"recipe": self.recipe, "target": self.target, "roi_type": self.roi_type,
                "layout": self.layout, "verdict": self.verdict, "n_units": self.n_units,
                "elements": [e.to_dict() for e in self.elements],
                "blocking": [e.element for e in self.blocking],
                "degraded": [e.element for e in self.degraded],
                "notes": list(self.notes)}


@dataclass(frozen=True)
class Availability:
    """The whole answer to "what can this ROI give me?"."""

    roi: ROIRef
    subtree: dict[str, int]
    roles: tuple[RoleAvailability, ...]
    child_coverage: tuple[Coverage, ...]
    recipes: tuple[RecipeReadiness, ...]
    bytes_checked: bool
    installed_consumers: tuple[str, ...]
    index_built_at: str
    #: What the catalogue can address on a child, de-duplicated across recipes.
    child_elements: tuple[ElementAvailability, ...] = ()
    warnings: tuple[str, ...] = ()
    #: Inferred grid cells, excluded from every count above but still in ``subtree``.
    inferred_cells: tuple[str, ...] = ()
    #: What this ROI carries, as distinct from what the recipes expect. Reported only.
    observation: object | None = None

    @property
    def child_stores(self) -> dict[str, tuple[str, ...]]:
        """``{artifact key: the element names that address it}``, over the children.

        Store totals are taken over this map, so two elements over one zarr count once.
        """
        out: dict[str, list[str]] = {}
        for e in self.child_elements:
            for key in e.keys:
                names = out.setdefault(key, [])
                if e.element not in names:
                    names.append(e.element)
        return {k: tuple(v) for k, v in sorted(out.items())}

    def shares_store(self, element: ElementAvailability) -> tuple[str, ...]:
        """The other elements reading this one's artifact."""
        stores = self.child_stores
        return tuple(sorted({n for k in element.keys for n in stores.get(k, ())}
                            - {element.element}))

    def to_dict(self) -> dict:
        return {"roi": self.roi.to_dict(), "subtree": dict(self.subtree),
                "observation": self.observation.to_dict() if self.observation else None,
                "roles": [r.to_dict() for r in self.roles],
                "child_coverage": [c.to_dict() for c in self.child_coverage],
                "child_elements": [e.to_dict() for e in self.child_elements],
                "child_stores": {k: list(v) for k, v in self.child_stores.items()},
                "recipes": [r.to_dict() for r in self.recipes],
                "bytes_checked": self.bytes_checked,
                "installed_consumers": list(self.installed_consumers),
                "index_built_at": self.index_built_at,
                "inferred_cells": list(self.inferred_cells),
                "warnings": list(self.warnings)}

    def caveats(self) -> list[str]:
        """The caveats this report must be relayed with."""
        out = []
        if not self.bytes_checked:
            out.append(
                "Artifact bytes were NOT verified. 'present' means an Artifact is registered "
                "under that key, not that the file exists. Pass include_storage to probe.")
        undeclared = sorted({r.role for r in self.roles if r.state == "undeclared"}
                            | {c.role for c in self.child_coverage if c.state == "undeclared"})
        if undeclared:
            out.append(
                f"{len(undeclared)} role(s) are reported 'undeclared' ({', '.join(undeclared)}): "
                f"no vocabulary installed in THIS environment declares them. That is not proof "
                f"nothing produces them - a consumer that is git-cloned rather than pip-installed "
                f"advertises nothing.")
        if self.observation is not None:
            for seen in self.observation.unclaimed:
                out.append(
                    f"This ROI carries {seen.feature!r} ({seen.kind or 'unknown kind'}, "
                    f"registered), which no recipe reads. It is NOT in any plan. If that is what "
                    f"you wanted, name it - nothing will pick it up on your behalf.")
            for seen in self.observation.invisible_locally:
                out.append(
                    f"{seen.feature!r} is declared by this INSTANCE but by nothing installed "
                    f"here, so its owner cannot be named from this environment.")
        if self.inferred_cells:
            out.append(
                f"{len(self.inferred_cells)} child ROI(s) are INFERRED grid cells - positions the "
                f"dearray fit placed and no detector found - and are excluded from every count "
                f"above. They are not cores awaiting a stage; re-running one produces nothing "
                f"for them.")
        out.append(f"Counts come from an index built at {self.index_built_at}.")
        out += list(self.warnings)
        return out


# Resolution.
@dataclass
class _Context:
    """Everything the resolver reads, gathered once. :attr:`grid` memoises each role lookup.

    One context per index, so the corpus and the per-ROI view read the same cells.
    """

    snap: object
    declared: frozenset[str]
    owners: dict[str, str]
    backrefs: dict[str, dict]
    bytes_checked: bool
    present_bytes: frozenset[str] = frozenset()
    #: ``{(record uid, role): RoleAvailability}``, filled on first ask and never recomputed.
    grid: dict[tuple[str, str], RoleAvailability] = field(default_factory=dict)

    def state_of_key(self, key: str) -> RoleState:
        if self.backrefs.get(key) is None:
            return "dangling"
        from clinicalomx.governance import permits_read

        if not permits_read(key):
            return "denied"
        if self.bytes_checked and key not in self.present_bytes:
            return "missing"
        return "present"


def _role_on(role: str, record, ctx: _Context) -> RoleAvailability:
    """One grid cell: this role, resolved against this ROI, memoised in ``ctx.grid``."""
    cell = (getattr(record, "uid", record), role)
    hit = ctx.grid.get(cell)
    if hit is None:
        hit = ctx.grid[cell] = _resolve_role(role, record, ctx)
    return hit


def _resolve_role(role: str, record, ctx: _Context) -> RoleAvailability:
    """The resolution itself. Call through :func:`_role_on`.

    ``declared`` includes the foundation's provenance features such as ``source_image``.
    """
    if role not in ctx.declared:
        return RoleAvailability(role, "undeclared", owner=None)
    keys = ctx.snap.artifact_keys(record, role, inherit=role in INHERITED_FEATURES)
    owner = ctx.owners.get(role)
    if not keys:
        return RoleAvailability(role, "unlinked", owner=owner)
    states = [ctx.state_of_key(k) for k in keys]
    best = _best(states)
    usable = tuple(k for k, s in zip(keys, states, strict=True) if s == best)
    detail = "" if best == "present" else _WHY[best]
    return RoleAvailability(role, best, usable, owner, detail)


def _resolve_on(element: Element, record, ctx: _Context):
    """``(candidates, chosen_role, state, keys, placeable)`` for one element on one ROI."""
    cands = tuple(_role_on(r, record, ctx) for r in element.roles)
    winner = next((c for c in cands if c.state == "present"), None)
    if winner is None:
        return cands, None, _best([c.state for c in cands]), (), True
    values = ctx.snap.values(record)
    placeable = all(values.get(f) is not None for f in element.needs_features)
    return cands, winner.role, "present", winner.keys, placeable


def _targets(element: Element, units: list, ctx: _Context, recipe: Recipe) -> list:
    """The ROIs this element is read from. ``level`` is relative to a unit, not the named ROI."""
    if element.level == "self":
        found = list(units)
    elif element.level == "children":
        found = [c for u in units for c in recipe.targets_under(u, ctx.snap)]
    else:
        found = [p for u in units if (p := ctx.snap.parent(u)) is not None]
    return [t for t in found if not is_inferred_cell(ctx.snap.values(t))]


def _element_availability(element: Element, units: list, ctx: _Context,
                          recipe: Recipe) -> ElementAvailability:
    return _resolve_over(element, _targets(element, units, ctx, recipe), ctx)


def _resolve_over(element: Element, targets: list, ctx: _Context, *,
                  always_cover: bool = False) -> ElementAvailability:
    """Resolve one element across every ROI it is read from, and aggregate.

    Satisfied when at least one target can supply it; per-target gaps are reported as coverage.
    """
    if not targets:
        return ElementAvailability(
            element.name, element.kind, element.required, element.level,
            None, "unlinked", variable=element.variable)

    per_target: list[tuple[str, str]] = []
    unplaceable: list[str] = []
    usable_keys: list[str] = []
    used: dict[str, int] = {}
    by_target: dict[str, str] = {}
    cands: tuple[RoleAvailability, ...] = ()

    for target in targets:
        t_cands, role, state, keys, placeable = _resolve_on(element, target, ctx)
        cands = cands or t_cands
        name = str(target.name)
        if state == "present" and not placeable:
            unplaceable.append(name)
            per_target.append((name, "unplaceable"))
            continue
        if state == "present":
            used[role] = used.get(role, 0) + 1
            by_target[name] = role
            usable_keys += list(keys)
        per_target.append((name, state))

    # The best role in use, by the recipe's preference order.
    chosen = next((r for r in element.roles if r in used), None)
    cov = _coverage(chosen, per_target, ctx, fallback_role=element.roles[0])
    # `unplaceable` is the element's state only when no target supplied it.
    if cov.present:
        state: RoleState = "present"
    elif unplaceable:
        state = "unplaceable"
    else:
        state = _worst([s for _n, s in per_target if s != "unplaceable"] or ["unlinked"])
    return ElementAvailability(
        element.name, element.kind, element.required, element.level,
        chosen, state, variable=element.variable,
        coverage=cov if always_cover or len(targets) > 1 or element.level == "children" else None,
        keys=tuple(dict.fromkeys(usable_keys)), unplaceable=tuple(unplaceable), candidates=cands,
        roles_used={r: used[r] for r in element.roles if r in used},
        per_target_role=by_target)


def _coverage(role: str | None, per_target: list[tuple[str, str]], ctx: _Context,
              *, fallback_role: str, max_examples: int = 5) -> Coverage:
    """Aggregate per-target states. ``unplaceable`` is counted apart from the role ladder."""
    counts: dict[str, int] = {}
    examples: dict[str, list[str]] = {}
    for name, state in per_target:
        counts[state] = counts.get(state, 0) + 1
        if state != "present" and len(examples.setdefault(state, [])) < max_examples:
            examples[state].append(name)
    named = role or fallback_role
    ladder = [s for _n, s in per_target if s != "unplaceable"]
    return Coverage(
        role=named, total=len(per_target),
        present=counts.get("present", 0), missing=counts.get("missing", 0),
        dangling=counts.get("dangling", 0), denied=counts.get("denied", 0),
        unlinked=counts.get("unlinked", 0), undeclared=counts.get("undeclared", 0),
        unplaceable=counts.get("unplaceable", 0),
        state=_worst(ladder) if ladder else "unlinked",
        owner=ctx.owners.get(named),
        examples={k: tuple(v) for k, v in examples.items()},
    )


def _readiness(recipe: Recipe, roi, ctx: _Context, *,
               inferred: tuple[str, ...] = ()) -> RecipeReadiness:
    units = recipe.targets_under(roi, ctx.snap) if recipe.layout == "per_child" else [roi]
    elements = tuple(_element_availability(e, units, ctx, recipe) for e in recipe.elements)
    notes: list[str] = []
    if is_inferred_cell(ctx.snap.values(roi)):
        notes.append(
            "this ROI is an inferred grid cell: the dearray fit placed it, no detector found it, "
            "and bgnorm skips it. It is not a core that failed to process; it is not a core, and "
            "nothing will ever build from it")

    blocking = [e for e in elements if e.required and not e.satisfied]
    # requires_any blocks when no element of the group resolved.
    if recipe.requires_any:
        group = [e for e in elements if e.element in recipe.requires_any]
        if group and not any(e.satisfied for e in group):
            blocking = blocking + [e for e in group if e not in blocking]

    partial = [e for e in elements if not e.satisfied and e not in blocking]
    partial += [e for e in elements
                if e.satisfied and e.coverage and e.coverage.present < e.coverage.total]
    verdict: Verdict = "blocked" if blocking else ("degraded" if partial else "ready")

    if not units:
        notes.append(
            f"this {_type_of(roi)} has no child ROI to build for"
            + (f"; its {len(inferred)} inferred grid cell(s) are not cores and are excluded"
               if inferred else ""))
    if blocking:
        n_units = 0
    elif recipe.layout == "per_child":
        # One output per unit that can actually supply every required element.
        buildable = {str(u.name) for u in units}
        for e in elements:
            if e.required and e.coverage is not None:
                buildable -= {n for names in e.coverage.examples.values() for n in names}
        n_units = len(buildable)
    else:
        n_units = 1

    if recipe.layout == "merged":
        notes.append(
            f"the output anchors to this {_type_of(roi)}, never to a child - a query starting "
            f"from a child finds it by walking up to its parent")
    for e in elements:
        if e.unplaceable:
            notes.append(
                f"{len(e.unplaceable)} ROI(s) have {e.element!r} but lack the features its "
                f"placement needs, so it cannot be positioned: {', '.join(e.unplaceable[:3])}")
        if e.coverage and 0 < e.coverage.present < e.coverage.total:
            via = ", ".join(f"{r} x{n}" for r, n in e.roles_used.items()) or "nothing"
            notes.append(f"{e.element}: {e.coverage.fraction} usable, via {via}")
    return RecipeReadiness(recipe.name, recipe.target, _type_of(roi), recipe.layout,
                           verdict, elements, n_units, tuple(notes),
                           tuple(e.element for e in blocking))


def _reads_a_child(element: Element, recipe: Recipe) -> bool:
    """Is this element read from a child? Under ``per_child``, ``self`` elements are too."""
    return element.level == "children" or recipe.layout == "per_child"


def _child_elements(catalogue, children: list, ctx: _Context) -> tuple[ElementAvailability, ...]:
    """Every element the catalogue can address on a child, over the ``child_coverage`` children.

    De-duplicated by :class:`~clinicalomx.extract.Element`.
    """
    out: dict[Element, ElementAvailability] = {}
    for recipe in catalogue:
        for element in recipe.elements:
            if not _reads_a_child(element, recipe):
                continue
            flat = replace(element, level="self")
            if flat not in out:
                out[flat] = _resolve_over(flat, children, ctx, always_cover=True)
    return tuple(out.values())


def variables_addressed(elements, role: str) -> dict[str, str]:
    """``{variable: the element that names it}`` for one role. No variable is keyed ``""``."""
    return {e.variable or "": e.element for e in elements if e.chosen_role == role}


def addressable(av: Availability, *,
                child_level: str | None = "children") -> tuple[ElementAvailability, ...]:
    """Every element a request could name here: child elements, child roles, own artifacts.

    ``child_level`` is how the caller's recipe reaches a child. ``None`` means it does not.
    """
    out: list[ElementAvailability] = []
    if child_level is not None:
        addressed = {_store_of(e) for e in av.child_elements}
        out += [replace(e, level=child_level) for e in av.child_elements]
        out += [ElementAvailability(c.role, "", False, child_level, c.role, c.state, coverage=c)
                for c in av.child_coverage if c.present and c.role not in addressed]
    if child_level != "self" and av.observation is not None:
        states = {r.role: r for r in av.roles}
        for seen in av.observation.seen:
            if not seen.registered:
                continue  # not registered
            role = states.get(seen.feature)
            out.append(ElementAvailability(
                seen.feature, seen.kind or "", False, "self", seen.feature,
                role.state if role is not None else "present",
                keys=role.keys if role is not None else seen.keys))
    return tuple(out)


def inspect_stores(av: Availability, *, source=None) -> dict:
    """``{role: StoreContents}`` for one sample store per role. Opt-in; uncapped.

    Governance is checked before the open; a forbidden store is reported ``denied``.
    """
    from clinicalomx.extract.source import default_source
    from clinicalomx.governance import permits_read

    src = source if source is not None else default_source()
    out: dict[str, object] = {}
    for role, key in _representative_keys(av).items():
        if not permits_read(key):
            out[role] = StoreContents(key, error=_WHY["denied"])
            continue
        rows = src.artifacts([key])
        if not rows:
            out[role] = StoreContents(key, error=_WHY["dangling"])
        elif not rows[0].uri:
            out[role] = StoreContents(key, error="the registered Artifact resolves to no path")
        else:
            out[role] = describe_store(rows[0].uri, key=key)
    return out


def _representative_keys(av: Availability) -> dict[str, str]:
    """``{role: one artifact key}``, name-ordered so the sample is stable."""
    out: dict[str, str] = {}
    for e in sorted(av.child_elements, key=lambda e: e.element):
        if e.chosen_role and e.keys:
            out.setdefault(e.chosen_role, sorted(e.keys)[0])
    return out


def _bytes_present(keys, ctx_backrefs, *, source=None) -> frozenset[str]:
    """Which registered keys have bytes in storage. One read per key."""
    from clinicalomx.extract.source import default_source

    src = source if source is not None else default_source()
    registered = [k for k in keys if ctx_backrefs.get(k) is not None]
    return frozenset(a.artifact_id for a in src.artifacts(registered) if a.stored)


def describe(roi, *, snapshot=None, include_storage: bool = False,
             recipes=None, now: str | None = None, source=None,
             index=None) -> Availability:
    """Everything this ROI can give you, and why not, where not. Reads only.

    ``roi`` is an ``ln.Record``. ``include_storage`` probes bytes, one query per key, and needs
    storage access. ``index`` is an optional shared :class:`~clinicalomx.extract.index.Index`.
    """
    from clinicalomx.extract.index import Index, declared_features

    if index is not None:
        index.check(snapshot=snapshot, include_storage=include_storage)
        snap = index.snapshot
    else:
        snap = snapshot if snapshot is not None else Snapshot()
    if recipes is not None:
        catalogue = list(recipes)
    elif index is not None:
        catalogue = list(index.catalogue)
    else:
        catalogue = all_recipes()
    catalogue = [r for r in catalogue if _type_of(roi) in r.roi_types]

    # Inferred grid cells are excluded from every count and reported by name.
    children, inferred = [], []
    for child in snap.children(roi):
        (inferred if is_inferred_cell(snap.values(child)) else children).append(child)
    inferred_names = tuple(str(c.name) for c in inferred)

    if index is None:
        # The byte probe covers only this ROI and its children.
        declared = declared_features(snap)
        kin_keys = ({k for rec in [roi, *children] for f in declared
                     for k in snap.artifact_keys(rec, f)} if include_storage else None)
        index = Index.of(snap, recipes=catalogue, include_storage=include_storage,
                         probe_keys=kin_keys, source=source, now=now)
    ctx = index.ctx

    roles = tuple(sorted(
        (index.role_on(roi, r) for r in ctx.declared), key=lambda r: r.role))
    # Every role read from a child, including `self` elements under `per_child`.
    child_roles = sorted(
        {role
         for rec in catalogue
         for e in rec.elements
         if _reads_a_child(e, rec)
         for role in e.roles}
        | {r for c in children for r in snap.roles_of(c)})
    coverage = tuple(index.coverage(role, children)
                     for role in child_roles) if children else ()

    subtree: dict[str, int] = {_type_of(roi): 1}
    for c in snap.descendants(roi):
        subtree[_type_of(c)] = subtree.get(_type_of(c), 0) + 1

    return Availability(
        roi=ROIRef.of(roi, snap.parent(roi)),
        observation=index.observation(roi, recipes=catalogue),
        subtree=subtree,
        roles=roles,
        child_coverage=coverage,
        child_elements=index.child_elements(roi, children, recipes=catalogue) if children else (),
        recipes=tuple(index.readiness(r, roi, inferred=inferred_names) for r in catalogue),
        bytes_checked=index.bytes_checked,
        installed_consumers=index.providers,
        index_built_at=now or index.built_at,
        inferred_cells=inferred_names,
        warnings=tuple(snap.graph_warnings()),
    )


def _type_of(record) -> str:
    rtype = getattr(record, "type", None)
    return str(getattr(rtype, "name", rtype)) if rtype is not None else ""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# The report.
def _store_of(e: ElementAvailability) -> str:
    """The role naming this element's store. The declared one where nothing resolved."""
    return e.chosen_role or (e.candidates[0].role if e.candidates else "-")


def _element_order(e: ElementAvailability):
    """Sort key grouping elements over one store."""
    return (_store_of(e), e.element)


def summarise(av: Availability, *, max_examples: int = 3) -> str:
    """The human-readable report. Every absence names its owner."""
    star = "" if av.bytes_checked else "*"
    miss = lambda n: str(n) if av.bytes_checked else "-"  # noqa: E731 - a column formatter
    out = [
        f"{av.roi.roi_type}  {av.roi.name}   uid {av.roi.uid}",
        f"parent: {av.roi.parent_name or '(none)'}",
        "subtree: " + ", ".join(f"{n} {t}" for t, n in sorted(av.subtree.items())),
        f"index built {av.index_built_at} | bytes "
        + ("verified" if av.bytes_checked else "NOT verified (--include-storage to probe)"),
        f"vocabularies visible: {', '.join(av.installed_consumers)}",
    ]
    if av.inferred_cells:
        shown = ", ".join(av.inferred_cells[:max_examples])
        more = len(av.inferred_cells) - max_examples
        out.append(
            f"{len(av.inferred_cells)} inferred grid cell(s) excluded from every count below "
            f"({shown}{f', +{more} more' if more > 0 else ''}) - placed by the dearray fit, "
            f"never detected, so not cores")

    on_self = [r for r in av.roles if r.state != "undeclared"]
    if on_self:
        out += ["", f"roles on this {av.roi.roi_type}"]
        for r in sorted(on_self, key=lambda r: (-_RANK[r.state], r.role)):
            mark = f"{r.state}{star if r.state == 'present' else ''}"
            out.append(f"  {mark:11} {r.role:22} {len(r.keys)}  {r.owner or '(unowned)'}")

    if av.child_elements:
        stores = av.child_stores
        out += ["", f"addressable on {av.child_coverage[0].total} child ROI(s): "
                    f"{len(av.child_elements)} element(s) over {len(stores)} distinct store(s)",
                "  ELEMENT   KIND     ARTIFACT         VARIABLE                  USABLE  SHARED"]
        for e in sorted(av.child_elements, key=_element_order):
            shared = ", ".join(av.shares_store(e)) or "-"
            frac = e.coverage.fraction if e.coverage else "-"
            out.append(f"  {e.element:9} {e.kind:8} {_store_of(e):16} "
                       f"{e.variable or '-':25} {frac:>6}  {shared}".rstrip())

    if av.child_coverage:
        total = av.child_coverage[0].total
        out += ["", f"roles carried by those {total} child ROI(s)",
                "  ROLE                  PRESENT  DANGLING  MISSING  DENIED  UNLINKED  OWNER"]
        for c in sorted(av.child_coverage, key=lambda c: (-c.present, c.role)):
            owner = c.owner or ("(undeclared)" if c.state == "undeclared" else "(unowned)")
            out.append(f"  {c.role:20} {c.fraction:>8} {c.dangling:>9} {miss(c.missing):>8} "
                       f"{c.denied:>7} {c.unlinked:>9}  {owner}".rstrip())
            for state, names in sorted(c.examples.items()):
                if state in ("undeclared", "unlinked") and c.present == 0:
                    continue  # a role nothing has
                out.append(f"      {state}: {', '.join(names[:max_examples])}")

    out += ["", "recipes"]
    order = {"ready": 0, "degraded": 1, "blocked": 2}
    for r in sorted(av.recipes, key=lambda r: (order[r.verdict], r.recipe)):
        out.append(f"  {r.verdict:9} {r.recipe:26} {r.target:12} {r.layout:10} "
                   f"{r.n_units} unit(s)")
        for e in r.elements:
            frac = f"{e.coverage.fraction:>8}" if e.coverage else " " * 8
            why = "" if e.satisfied else f"  {_WHY[e.state]}"
            role = (", ".join(f"{k} x{n}" for k, n in e.roles_used.items())
                    if len(e.roles_used) > 1
                    else e.chosen_role or " / ".join(c.role for c in e.candidates))
            if e.variable:
                role = f"{role}#{e.variable}"
            tail = "  optional, omitted" if not e.required and not e.satisfied else ""
            if e.unplaceable:
                tail += f"  ({len(e.unplaceable)} unplaceable)"
            out.append(f"      {e.element:8} <- {role:20} {frac}{tail}{why}".rstrip())
        for note in r.notes:
            out.append(f"      NOTE  {note}")

    tally = {v: sum(1 for r in av.recipes if r.verdict == v)
             for v in ("ready", "degraded", "blocked")}
    out += ["", ", ".join(f"{n} {v}" for v, n in tally.items() if n)
            + ("   * = registered, bytes not verified." if not av.bytes_checked else "")]
    return "\n".join(out)
