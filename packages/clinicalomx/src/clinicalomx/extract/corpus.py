"""What exists across the whole instance, not one ROI.

A survey reports role coverage and recipe buildability over every ROI, using the same
:class:`~clinicalomx.extract.availability.Coverage` and the same shared
:class:`~clinicalomx.extract.index.Index` as ``describe``. It carries ``index_built_at``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from clinicalomx.extract import is_inferred_cell
from clinicalomx.extract.availability import Coverage, _type_of
from clinicalomx.extract.index import Index, declared_features
from clinicalomx.names import FEATURES
from clinicalomx.snapshot import Snapshot


@dataclass(frozen=True)
class RecipeReach:
    """How many ROIs one recipe could be built for, across the instance."""

    recipe: str
    target: str
    roi_type: str
    layout: str
    ready: int = 0
    degraded: int = 0
    blocked: int = 0
    #: ROI names that would build, capped.
    examples: tuple[str, ...] = ()
    #: ``{element: {state: count}}`` over every ROI tried. ``unplaceable`` at 100% is a declaration
    #: fault; ``unlinked`` at 100% is a data gap.
    element_states: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def buildable(self) -> int:
        return self.ready + self.degraded

    def to_dict(self) -> dict:
        return {"recipe": self.recipe, "target": self.target, "roi_type": self.roi_type,
                "layout": self.layout, "ready": self.ready, "degraded": self.degraded,
                "blocked": self.blocked, "buildable": self.buildable,
                "examples": list(self.examples),
                "element_states": {k: dict(v) for k, v in self.element_states.items()}}

    def declaration_suspects(self, *, min_n: int = 20) -> tuple[str, ...]:
        """Elements unplaceable on every ROI tried: a false declaration rather than a gap."""
        total = self.ready + self.degraded + self.blocked
        if total < min_n:
            return ()
        return tuple(sorted(
            name for name, states in self.element_states.items()
            if states.get("unplaceable", 0) == total))


@dataclass(frozen=True)
class Survey:
    """The corpus answer: what is here, and what could be made from it."""

    #: ``{roi type: count}`` over the whole instance.
    population: dict[str, int]
    #: Role coverage across every ROI of the set.
    coverage: tuple[Coverage, ...]
    recipes: tuple[RecipeReach, ...]
    #: ``{group: {role: Coverage}}`` when grouped, else empty.
    groups: dict[str, tuple[Coverage, ...]] = field(default_factory=dict)
    group_by: str | None = None
    bytes_checked: bool = False
    installed_consumers: tuple[str, ...] = ()
    index_built_at: str = ""
    warnings: tuple[str, ...] = ()
    #: Inferred grid cells excluded from ``coverage``, ``groups`` and ``recipes``. ``population``
    #: still counts them.
    inferred_cells: int = 0

    def to_dict(self) -> dict:
        return {"population": dict(self.population),
                "coverage": [c.to_dict() for c in self.coverage],
                "recipes": [r.to_dict() for r in self.recipes],
                "groups": {g: [c.to_dict() for c in cs] for g, cs in self.groups.items()},
                "group_by": self.group_by,
                "bytes_checked": self.bytes_checked,
                "installed_consumers": list(self.installed_consumers),
                "index_built_at": self.index_built_at,
                "inferred_cells": self.inferred_cells,
                "warnings": list(self.warnings)}

    def caveats(self) -> list[str]:
        """The caveats a corpus number must be relayed with, the data's age first."""
        out = [
            f"These counts describe the whole instance as of {self.index_built_at}. Re-survey "
            f"rather than repeating a number from earlier in a conversation."
        ]
        if not self.bytes_checked:
            out.append(
                "Artifact bytes were NOT verified. 'present' means an Artifact is registered "
                "under that key, not that the file exists.")
        undeclared = sorted({c.role for c in self.coverage if c.state == "undeclared"})
        if undeclared:
            out.append(
                f"{len(undeclared)} role(s) are 'undeclared' ({', '.join(undeclared)}): no "
                f"vocabulary installed in THIS environment declares them. That is not proof "
                f"nothing produces them.")
        if self.inferred_cells:
            out.append(
                f"{self.inferred_cells} ROI(s) are INFERRED grid cells - positions the dearray "
                f"fit placed and no detector found - and are excluded from every coverage and "
                f"buildability figure here. They are counted in the population, because the "
                f"instance does hold them.")
        out += list(self.warnings)
        return out


def _group_of(record, snap, group_by: str) -> str:
    if group_by == "roi_type":
        return _type_of(record) or "(untyped)"
    if group_by == "slide":
        return str(record.name).split(":")[0]
    key = {"cohort": FEATURES.COHORT, "patient": FEATURES.PATIENT_ID,
           "source": FEATURES.SOURCE}.get(group_by, group_by)
    value = snap.values(record).get(key)
    return "(none)" if value is None else str(value)


def survey(*, snapshot=None, group_by: str | None = None, roi_types=None,
           include_storage: bool = False, recipes=None, now: str | None = None,
           source=None, index=None) -> Survey:
    """Role coverage and buildability across the instance. Reads only.

    ``group_by`` is ``roi_type``, ``slide``, ``cohort``, ``patient``, ``source``, or any feature
    name. ``index`` is an optional shared :class:`~clinicalomx.extract.index.Index`.
    """
    if index is not None:
        index.check(snapshot=snapshot, include_storage=include_storage)
        snap = index.snapshot
    else:
        snap = snapshot if snapshot is not None else Snapshot()
    types = list(roi_types) if roi_types else snap.roi_types()
    everything = [r for t in types for r in snap.records(t)]
    # Inferred grid cells are in `population` only.
    records = [r for r in everything if not is_inferred_cell(snap.values(r))]
    inferred = len(everything) - len(records)

    if index is None:
        declared = declared_features(snap)
        keys = ({k for r in records for f in declared for k in snap.artifact_keys(r, f)}
                if include_storage else None)
        index = Index.of(snap, recipes=recipes, include_storage=include_storage,
                         probe_keys=keys, source=source, now=now)

    roles = sorted(index.ctx.declared)
    coverage = tuple(index.coverage(role, records) for role in roles)

    groups: dict[str, tuple[Coverage, ...]] = {}
    if group_by:
        buckets: dict[str, list] = {}
        for rec in records:
            buckets.setdefault(_group_of(rec, snap, group_by), []).append(rec)
        for name, members in sorted(buckets.items()):
            groups[name] = tuple(index.coverage(role, members) for role in roles)

    catalogue = list(recipes) if recipes is not None else list(index.catalogue)
    reach = []
    for recipe in catalogue:
        tally = {"ready": 0, "degraded": 0, "blocked": 0}
        examples: list[str] = []
        states: dict[str, dict[str, int]] = {}
        for rec in (r for r in records if _type_of(r) in recipe.roi_types):
            readiness = index.readiness(recipe, rec)
            tally[readiness.verdict] += 1
            for element in readiness.elements:
                per = states.setdefault(element.element, {})
                per[element.state] = per.get(element.state, 0) + 1
            if readiness.verdict != "blocked" and len(examples) < 5:
                examples.append(str(rec.name))
        reach.append(RecipeReach(recipe.name, recipe.target, recipe.roi_types[0], recipe.layout,
                                 tally["ready"], tally["degraded"], tally["blocked"],
                                 tuple(examples), states))

    population: dict[str, int] = {}
    for rec in everything:
        t = _type_of(rec) or "(untyped)"
        population[t] = population.get(t, 0) + 1

    return Survey(
        population=population, coverage=coverage,
        recipes=tuple(sorted(reach, key=lambda r: (-r.buildable, r.recipe))),
        groups=groups, group_by=group_by, bytes_checked=index.bytes_checked,
        installed_consumers=index.providers, index_built_at=now or index.built_at,
        inferred_cells=inferred, warnings=tuple(snap.graph_warnings()))


def summarise(sv: Survey, *, max_groups: int = 10, min_n: int = 20) -> str:
    """The corpus report. Unprobed bytes print `-`, never `0`."""
    miss = (lambda n: str(n)) if sv.bytes_checked else (lambda _n: "-")
    out = [
        "instance survey  " + ", ".join(f"{n} {t}" for t, n in sorted(sv.population.items()))
        + (f"  ({sv.inferred_cells} inferred grid cell(s), excluded from everything below)"
           if sv.inferred_cells else ""),
        f"index built {sv.index_built_at} | bytes "
        + ("verified" if sv.bytes_checked else "NOT verified"),
        f"vocabularies visible: {', '.join(sv.installed_consumers)}",
        "",
        "role coverage across the instance",
        "  ROLE                  PRESENT  DANGLING  MISSING  DENIED  UNLINKED  OWNER",
    ]
    for c in sorted(sv.coverage, key=lambda c: (-c.present, c.role)):
        owner = c.owner or ("(undeclared)" if c.state == "undeclared" else "(unowned)")
        out.append(f"  {c.role:20} {c.fraction:>8} {c.dangling:>9} {miss(c.missing):>8} "
                   f"{c.denied:>7} {c.unlinked:>9}  {owner}".rstrip())

    if sv.groups:
        out += ["", f"by {sv.group_by}"]
        for name, covs in list(sv.groups.items())[:max_groups]:
            have = ", ".join(f"{c.role} {c.fraction}" for c in covs if c.present)
            out.append(f"  {name:28} {have or '(nothing)'}")
        if len(sv.groups) > max_groups:
            out.append(f"  ... and {len(sv.groups) - max_groups} more")

    out += ["", "what could be built"]
    for r in sv.recipes:
        out.append(f"  {r.buildable:>5} of {r.ready + r.degraded + r.blocked:<5} "
                   f"{r.recipe:26} {r.target:12} {r.layout}")
        if r.examples:
            out.append(f"        e.g. {', '.join(r.examples[:3])}")
        for suspect in r.declaration_suspects(min_n=min_n):
            out.append(f"        DECLARATION FAULT  {suspect!r} is unplaceable on every ROI "
                       f"tried - the artifact is there and a needs_features is refusing it")
    return "\n".join(out)
