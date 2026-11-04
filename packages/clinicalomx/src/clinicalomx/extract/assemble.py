"""Execute a plan. The only place a heavy dependency is imported.

``Recipe.builder`` and ``Recipe.placement`` are resolved by :mod:`importlib` here and nowhere
earlier. This module does not write to the instance; :mod:`clinicalomx.extract.publish` registers
outputs.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass

from clinicalomx.extract import all_recipes
from clinicalomx.extract.coords import PlacementSet
from clinicalomx.extract.publish import register_outputs
from clinicalomx.snapshot import Snapshot


class AssemblyError(RuntimeError):
    """A plan could not be executed. Names the failing unit."""


@dataclass
class Built:
    """One produced output."""

    roi_name: str
    output: str
    registered: bool = False
    detail: str = ""

    def to_dict(self) -> dict:
        return {"roi_name": self.roi_name, "output": self.output,
                "registered": self.registered, "detail": self.detail}


def resolve_dotted(dotted: str):
    """``"module:attr"`` -> the object. The layer's only dynamic import."""
    if not dotted:
        raise AssemblyError("the recipe declares no builder")
    module, _, attr = dotted.partition(":")
    try:
        obj = importlib.import_module(module)
    except ImportError as exc:
        raise AssemblyError(
            f"{dotted!r} does not import here: {exc}. The builders need the extras: "
            f"`uv sync --extra extract`. Planning never needs them, which is why this is the "
            f"first point at which it could fail.") from exc
    if attr:
        try:
            return getattr(obj, attr)
        except AttributeError as exc:
            raise AssemblyError(f"{module} has no {attr!r}") from exc
    return obj


def _recipe(name: str):
    found = next((r for r in all_recipes() if r.name == name), None)
    if found is None:
        raise AssemblyError(f"no recipe named {name!r}")
    return found


def assemble(plan, *, confirm: bool = False, force_stale: bool = False,
             snapshot=None, **builder_kw) -> dict:
    """Build the units of a :class:`~clinicalomx.extract.plan.Plan`. Never takes a request."""
    if not confirm:
        raise AssemblyError("assemble needs confirm=True; a plan is meant to be read first")
    if plan.blocked:
        raise AssemblyError(
            f"this plan is blocked on {', '.join(plan.blocked)} and would build nothing")
    if plan.is_stale() and not force_stale:
        raise AssemblyError(
            f"this plan's index was built at {plan.index_built_at} and may no longer describe "
            f"the instance. Re-plan, or pass force_stale=True if you accept the risk that the "
            f"output disagrees with what was reviewed.")

    recipe = _recipe(plan.recipe)
    builder = resolve_dotted(recipe.builder)
    placement = PlacementSet({t: resolve_dotted(d)() for t, d in recipe.placements.items()})

    # Every placement's coordinate space must match the recipe's.
    disagree = {t: s for t, s in placement.spaces().items() if s != recipe.coordinate_space}
    if disagree:
        raise AssemblyError(
            f"recipe {recipe.name!r} declares coordinate space {recipe.coordinate_space!r} but "
            f"its placement(s) report {disagree}. Building would put elements in a space nothing "
            f"else agrees with.")

    # From the plan, which the token covers.
    builder_kw.setdefault("downsample", (plan.request or {}).get("downsample", 1))

    snap = snapshot if snapshot is not None else Snapshot()
    built, failed = [], []
    for unit in plan.buildable:
        try:
            detail = builder(unit, recipe=recipe, placement=placement, snapshot=snap,
                             **builder_kw)
        except Exception as exc:  # noqa: BLE001 - one bad core must not lose the other 95
            failed.append(f"{unit.roi_name}: {type(exc).__name__}: {exc}")
            continue
        built.append(Built(unit.roi_name, unit.output, detail=str(detail or "")))

    registered = 0
    if plan.request and plan.request.get("destination", {}).get("kind") == "artifact":
        registered = register_outputs(plan, built, recipe=recipe, snapshot=snap)

    return {
        "token": plan.token,
        "recipe": recipe.name,
        "built": [b.to_dict() for b in built],
        "n_built": len(built),
        "n_registered": registered,
        # ROI names, not a count.
        "failed": failed,
        "coordinate_space": recipe.coordinate_space,
    }
