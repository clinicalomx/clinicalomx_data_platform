"""Research extracts: what a given ROI can be turned into, and turning it into that.

A product is a declared :class:`Recipe`. ``plan`` is a dry run that writes nothing and reports
what is missing; ``assemble`` executes a plan.

``clinicalomx.extract`` is light and runs without heavy dependencies. ``.builders`` needs the
``extract`` extra and is imported only through :attr:`Recipe.builder` inside ``assemble``.
:attr:`Element.roles` is a preference-ordered tuple: the first role present fills the element.

lamindb is imported lazily inside functions, never at module scope.

Placement is declared per ROI subtype; a subtype a recipe does not cover is skipped, never
defaulted. A consumer recipe shadows a foundation recipe of the same name. ``assemble`` takes a
plan, never a request. The qptiff reader ``bioio-qptiff`` is not a declared dependency.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from clinicalomx.vocab.registry import FOUNDATION

__all__ = [
    "ENTRY_POINT_GROUP", "Element", "Kind", "Layout", "Level", "Recipe",
    "all_recipes", "discover", "is_inferred_cell", "recipes_for", "register_recipes",
    "reset_cache", "reset_registrations",
]

#: Entry-point group under which a consumer advertises an iterable of :class:`Recipe`.
ENTRY_POINT_GROUP = "clinicalomx.recipes"

#: What the target calls this slot, in SpatialData vocabulary. ``attrs`` is scalar metadata.
Kind = Literal["image", "labels", "shapes", "points", "table", "attrs"]

#: Which ROI the element is read from, relative to the one the user asked about.
Level = Literal["self", "children", "parent"]

#: ``single`` - one object for the ROI itself.
#: ``merged`` - ONE object whose elements are keyed by child (one master SpatialData per TMA).
#: ``per_child`` - one object PER child (a Core-level SpatialData for each core in that TMA).
Layout = Literal["single", "merged", "per_child"]

#: ``child_types=(ANY_CHILD,)``: every ROI subtype below this one. ``()`` means no children.
ANY_CHILD = "*"


@dataclass(frozen=True)
class Element:
    """One slot in a target object, and the role features that can fill it."""

    name: str
    kind: Kind
    #: Candidate role features in preference order, best first.
    roles: tuple[str, ...]
    required: bool = True
    level: Level = "self"
    #: ROI-side features that must carry a value for this element to be placeable.
    needs_features: tuple[str, ...] = ()
    #: Variable within a multi-variable artifact, such as ``adjusted_image`` in a bgnormed zarr.
    variable: str | None = None
    #: For a ``table``: the column matching each row to a label value. Never defaulted.
    instance_key: str | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if not self.roles:
            raise ValueError(f"element {self.name!r} declares no candidate roles")


@dataclass(frozen=True)
class Recipe:
    """One named downstream product a user can ask for."""

    name: str
    #: The output data structure: ``"SpatialData"``, ``"OME-TIFF"``.
    target: str
    #: ROI subtypes this recipe may be asked for.
    roi_types: tuple[str, ...]
    layout: Layout
    elements: tuple[Element, ...]
    #: Child subtypes to expand for ``level="children"`` / ``layout="per_child"``.
    #: ``(ANY_CHILD,)`` means every subtype the instance carries.
    child_types: tuple[str, ...] = ()
    #: Under ``merged``: composite the children into one element per name (a mosaic), rather
    #: than keep one element per child.
    aggregates: bool = False
    #: Walk every level below the anchor rather than its direct children only.
    descend: bool = False
    #: ``"module:callable"``, resolved by importlib inside ``assemble`` and nowhere earlier.
    builder: str = ""
    #: ``"module:callable"`` returning a :class:`~clinicalomx.extract.coords.Placement`, or a
    #: ``{ROI subtype: "module:callable"}`` mapping.
    placement: str | dict[str, str] = ""
    #: The coordinate system the builder's elements are registered in. Checked against the
    #: placement before building, and stamped on a registered output.
    coordinate_space: str = "global"
    #: Role feature a registered output takes on its anchor ROI. ``None`` means local export only.
    publish_role: str | None = None
    #: Element names of which at least one must be satisfied for the recipe to build.
    requires_any: tuple[str, ...] = ()
    owner: str = "clinicalomx"
    description: str = ""

    def __hash__(self) -> int:
        """Hash over the identifying fields only; ``placement`` may be an unhashable dict."""
        return hash((self.name, self.target, self.roi_types, self.layout, self.elements))

    def __post_init__(self) -> None:
        if not self.roi_types:
            raise ValueError(
                f"recipe {self.name!r} declares no roi_types; nothing could ever ask for it")
        if not self.elements:
            raise ValueError(f"recipe {self.name!r} declares no elements")
        needs_children = self.layout == "per_child" or any(
            e.level == "children" for e in self.elements)
        if needs_children and not self.child_types:
            raise ValueError(
                f"recipe {self.name!r} reads from children but declares no child_types; the "
                "planner would have no subtype to expand")
        if self.aggregates and self.layout != "merged":
            raise ValueError(
                f"recipe {self.name!r} sets aggregates on a {self.layout!r} layout. Compositing "
                f"children into one element only means something for a merged object.")
        if self.descend and self.layout == "single":
            raise ValueError(
                f"recipe {self.name!r} sets descend on a {self.layout!r} layout. Descending only "
                f"means something for an object built FROM descendants, and a single object is "
                f"built from the anchor.")
        if self.descend and self.layout == "per_child" and ANY_CHILD in self.child_types:
            raise ValueError(
                f"recipe {self.name!r} descends per_child over {ANY_CHILD!r}, which would write "
                f"an object per Core AND per TMA AND per anything else on the spine. Name the "
                f"subtype the objects are OF.")
        names = {e.name for e in self.elements}
        unknown = sorted(set(self.requires_any) - names)
        if unknown:
            raise ValueError(
                f"recipe {self.name!r} requires_any names {unknown}, which are not its elements "
                f"({sorted(names)}); it could never be satisfied")
        if not self.requires_any and not any(e.required for e in self.elements):
            raise ValueError(
                f"recipe {self.name!r} has no required element and no requires_any, so it would "
                f"'succeed' by building nothing. Name the elements at least one of which must "
                f"be present.")

    @property
    def required_roles(self) -> tuple[str, ...]:
        """Every role that could satisfy a required element."""
        return tuple(dict.fromkeys(r for e in self.elements if e.required for r in e.roles))

    def element(self, name: str) -> Element | None:
        return next((e for e in self.elements if e.name == name), None)

    @property
    def placements(self) -> dict[str, str]:
        """``{roi_type or "": dotted path}``. A plain string becomes the ``""`` default."""
        if isinstance(self.placement, dict):
            return dict(self.placement)
        return {"": self.placement} if self.placement else {}

    def placement_for(self, roi_type: str) -> str | None:
        """The placement declared for this subtype, else the recipe-wide one, else ``None``.

        On ``None`` the caller must skip the ROI and report it.
        """
        found = self.placements
        return found.get(roi_type) or found.get("") or None

    def expands_into(self, roi_type: str) -> bool:
        return ANY_CHILD in self.child_types or roi_type in self.child_types

    def targets_under(self, roi, snapshot) -> list:
        """The descendant ROIs this recipe fans out over, in spine order, minus inferred cells."""
        walk = snapshot.descendants if self.descend else snapshot.children
        found = (list(walk(roi)) if ANY_CHILD in self.child_types
                 else [c for t in self.child_types for c in walk(roi, roi_type=t)])
        return [c for c in found if not is_inferred_cell(snapshot.values(c))]

    def to_dict(self) -> dict:
        """A plain dict, for the agent surface and for stamping onto a registered output."""
        return asdict(self)


def is_inferred_cell(values) -> bool:
    """Is this ROI a grid position the dearray fit inferred, rather than a detected core?

    An inferred cell is not an extraction target.
    """
    return values.get("is_detected") is False


#: Recipes injected at runtime by :func:`register_recipes`, keyed by provider label.
_REGISTERED: dict[str, list[Recipe]] = {}

#: Memoised :func:`discover` result. ``None`` until the first scan.
_DISCOVERED: dict[str, list[Recipe]] | None = None


def register_recipes(recipes, *, provider: str = "registered") -> None:
    """Add recipes from a package with no entry point, such as a git clone."""
    _REGISTERED.setdefault(provider, []).extend(
        r for r in recipes if isinstance(r, Recipe))
    reset_cache()


def reset_registrations() -> None:
    """Drop runtime registrations and the memoised scan. For tests."""
    _REGISTERED.clear()
    reset_cache()


def reset_cache() -> None:
    """Drop the memoised :func:`discover` result. For tests."""
    global _DISCOVERED
    _DISCOVERED = None


def discover() -> dict[str, list[Recipe]]:
    """Every recipe set advertised by an installed distribution, keyed by provider. Memoised.

    A consumer that fails to load is reported and skipped; non-``Recipe`` objects are dropped.
    """
    global _DISCOVERED
    if _DISCOVERED is not None:
        return _DISCOVERED

    from importlib.metadata import entry_points

    found: dict[str, list[Recipe]] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            recipes = list(ep.load())
        except Exception as exc:  # noqa: BLE001 - one broken consumer must not break the run
            print(f"  WARNING  could not load recipes {ep.name!r}: {exc}")
            continue
        bad = [r for r in recipes if not isinstance(r, Recipe)]
        if bad:
            print(f"  WARNING  {ep.name!r} yielded {len(bad)} non-Recipe object(s); skipped")
        found[ep.name] = [r for r in recipes if isinstance(r, Recipe)]
    for provider, recipes in _REGISTERED.items():
        found.setdefault(provider, []).extend(recipes)
    _DISCOVERED = found
    return found


def all_recipes() -> list[Recipe]:
    """The foundation catalogue plus every discovered one, name-sorted.

    A consumer recipe replaces a foundation one of the same name.
    """
    from clinicalomx.extract.recipes import FOUNDATION_RECIPES

    by_name = {r.name: r for r in FOUNDATION_RECIPES}
    for recipes in discover().values():
        by_name.update({r.name: r for r in recipes})
    return sorted(by_name.values(), key=lambda r: r.name)


def recipes_for(roi_type: str, *, target: str | None = None) -> list[Recipe]:
    """Every recipe that can be asked for on an ROI of ``roi_type``."""
    return [r for r in all_recipes()
            if roi_type in r.roi_types and (target is None or r.target == target)]


def providers() -> tuple[str, ...]:
    """Who supplied the catalogue."""
    return (FOUNDATION, *sorted(discover()))
