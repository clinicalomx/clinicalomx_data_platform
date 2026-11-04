"""The Explorer's interaction model: what a user has chosen, and what follows from it.

State vars are plain JSON values. Snapshots, Availabilities and Plans live in `cache`, keyed by
those values, and every panel is a computed var derived from them. A choice is an element, not a
role. Staging is cleared when the product or the ROI changes.
"""
from __future__ import annotations

import dataclasses
import json

import reflex as rx

from clinicalomx.extract import all_recipes
from clinicalomx.extract import relations as rel
from clinicalomx.extract import submit as hop
from clinicalomx.extract.app import diagram
from clinicalomx.extract.app.web import cache, dot, tokens
from clinicalomx.extract.assemble import AssemblyError
from clinicalomx.extract.assemble import assemble as _assemble
from clinicalomx.extract.availability import (
    _element_order,
    _store_of,
    addressable,
    variables_addressed,
)
from clinicalomx.extract.jobs import Job

#: Set by `web.make_app`. ``True``/``False`` fixes the source; ``None`` shows the live/demo toggle.
FORCED_LIVE: bool | None = None

#: The slots each target has, in display order. The page is organised by these.
MODEL_SLOTS = {
    "SpatialData": ("image", "labels", "shapes", "table", "points"),
    "OME-TIFF": ("image",),
}
SLOT_LABEL = {"image": "Images", "labels": "Labels", "shapes": "Shapes",
              "table": "Tables", "points": "Points"}
SLOT_HELP = {
    "image": "Intensity pixels. A SpatialData holds as many as you like - raw beside "
             "background-normalised, in one coordinate system.",
    "labels": "Integer masks, one value per object. NOT intensities.",
    "shapes": "Geometry: the core grid, TMA polygons.",
    "table": "Per-object measurements, region-keyed to a labels element.",
    "points": "Point clouds - transcript locations and the like.",
}

AUTO = "Auto (recipe preference)"
NOTHING = "- choose -"
GROUP_BY = ["nothing", "roi_type", "slide", "cohort", "patient", "source"]
STRICTNESS = ["strict", "degrade", "best_effort"]
STRICTNESS_HELP = {
    "strict": "Block the whole plan",
    "degrade": "Skip what cannot be built, report it",
    "best_effort": "Build with whatever is there",
}
DOWNSAMPLE = [1, 2, 4, 8, 16, 32]

#: Most layout rows listed. The full layout is published as JSON beside it.
LAYOUT_PIECE_LIMIT = 200

#: Most ROIs the picker lists at once. The cap is reported in `roi_choices_note`.
ROI_CHOICES_SHOWN = 200

#: The type filter's "do not filter" entry.
ANY_TYPE = "any type"


# Framework-free derivation.
def slots_for(readiness) -> tuple[str, ...]:
    """Which model slots to show: the target's own, else the kinds the recipe declares."""
    canonical = MODEL_SLOTS.get(readiness.target, ())
    declared = tuple(dict.fromkeys(e.kind for e in readiness.elements))
    return canonical or declared


def occupies(e, role: str | None = None) -> tuple:
    """What an entry occupies: ``(role, variable, level)``, the array it reads."""
    return (role or _store_of(e), e.variable, e.level)


def candidates_for(av, readiness, kind: str, taken: set) -> list:
    """The addressable elements that could fill this slot, minus those already in it.

    An element of unknown kind is offered in every slot and marked.
    """
    # `single` has no child level; under `per_child` the units are the children.
    level = {"merged": "children", "per_child": "self"}.get(readiness.layout)
    return [c for c in addressable(av, child_level=level)
            if c.kind in (kind, "") and occupies(c) not in taken]


def candidate_label(c, kind: str) -> str:
    """A candidate's option label: state, store, variable, kind fit and coverage."""
    name = _store_of(c)
    if c.variable:
        name += f" · {c.variable}"
    if not c.kind:
        name += "  (kind unknown)"
    elif c.kind != kind:
        name += f"  ({c.kind})"
    scope = " per child" if c.level == "children" else ""
    detail = f"{c.coverage.fraction} usable" if c.coverage else c.state
    return f"{tokens.state_text(c.state)} · {name}{scope} - {detail}"


def role_options(e) -> tuple[list[str], list[str | None]]:
    """The roles that could fill one declared element, as ``(labels, roles)``."""
    labels: list[str] = [AUTO]
    values: list[str | None] = [None]
    array = f" · {e.variable}" if e.variable else ""
    for cand in e.candidates:
        used = e.roles_used.get(cand.role)
        suffix = f" - fills {used}" if used else f" - {cand.state}"
        labels.append(f"{tokens.state_text(cand.state)} · {cand.role}{array}{suffix}")
        values.append(cand.role)
    return labels, values


class Effective:
    """`readiness` with the user's choices applied. Only `.elements` is read by the diagram."""

    def __init__(self, readiness, elements):
        self.recipe, self.target = readiness.recipe, readiness.target
        self.layout, self.verdict = readiness.layout, readiness.verdict
        self.elements = elements


def effective(readiness, excluded, pins, added) -> Effective:
    """The elements the request would build: exclusions dropped, pins applied, additions added."""
    out = []
    for element in readiness.elements:
        if element.element in excluded:
            continue
        pinned = pins.get(element.element)
        out.append(dataclasses.replace(element, chosen_role=pinned)
                   if pinned and pinned != element.chosen_role else element)
    return Effective(readiness, tuple(out) + tuple(added))


def recipe_named(name: str):
    return next((r for r in all_recipes() if r.name == name), None)


def preview_entries(av, readiness, snap):
    """``[(element key, kind, artifact key)]`` for one representative ROI, for the diagram only.

    A per-child element is read from a child that carries it.
    """
    anchor = snap.record_by_uid(av.roi.uid)
    out = []
    for element in readiness.elements:
        roi = anchor
        if element.level == "children" and element.per_target_role:
            roi = snap.record_by_any_name(sorted(element.per_target_role)[0]) or anchor
        keys = snap.artifact_keys(roi, element.chosen_role) if (
            roi is not None and element.chosen_role) else []
        out.append((element.element, element.kind, sorted(keys)[-1] if keys else None))
    return out


def kind_of(slot: str) -> str:
    """The element kind behind a SpatialData slot name. `series` maps to image."""
    return {"images": "image", "tables": "table"}.get(
        slot, slot.rstrip("s") if slot != "series" else "image")


# Component data: one dataclass per repeated shape.
#: How one table column is drawn:
#:
#:   state   a state chip or glyph, already rendered
#:   html    other pre-rendered markup
#:   path    a long artifact key, truncated at the start, full value in `title`
#:   key     an identifier, in mono
#:   num     a count, right-aligned
#:   text    everything else
CELL_KINDS = ("state", "html", "path", "key", "num", "text")


@dataclasses.dataclass
class Table:
    """Any table on the page. One shape, so one component draws all nine of them."""

    columns: list[str] = dataclasses.field(default_factory=list)
    rows: list[list[str]] = dataclasses.field(default_factory=list)
    #: One of `CELL_KINDS` per column.
    kinds: list[str] = dataclasses.field(default_factory=list)
    #: `compact` for tables that can exceed ~30 rows, `comfortable` under ~15.
    density: str = "compact"
    #: The footer: a row count, and what was truncated.
    note: str = ""


@dataclasses.dataclass
class DeclaredRow:
    """One entry the recipe declares: which role fills it, and whether it may be absent."""

    element: str = ""
    #: The state glyph, rendered, placed before the element name.
    mark: str = ""
    level: str = ""
    coverage: str = ""
    unplaceable: str = ""
    options: list[str] = dataclasses.field(default_factory=list)
    chosen: str = ""
    mixed: str = ""
    variable: str = ""
    included: bool = True
    required: bool = True
    allow_absent: bool = False


@dataclasses.dataclass
class AddedRow:
    """One entry the USER is adding to this slot."""

    kind: str = ""
    index: int = 0
    slot_key: str = ""
    options: list[str] = dataclasses.field(default_factory=list)
    chosen: str = ""
    variable: str = ""
    variable_fixed: bool = False
    fixed_variable: str = ""
    name: str = ""
    empty: bool = True


@dataclasses.dataclass
class Slot:
    """One slot of the target model, with everything currently in it."""

    kind: str = ""
    label: str = ""
    #: The kind glyph, rendered; the same mark `diagram.KIND_SHAPE` uses.
    glyph: str = ""
    count: str = ""
    help: str = ""
    declared: list[DeclaredRow] = dataclasses.field(default_factory=list)
    added: list[AddedRow] = dataclasses.field(default_factory=list)
    add_disabled: bool = False
    add_help: str = ""
    empty: bool = False


@dataclasses.dataclass
class RoiChoice:
    """One ROI picker entry. ``uid`` is the identity; ``label`` is display only."""

    uid: str = ""
    label: str = ""


@dataclasses.dataclass
class Store:
    """One artifact opened, and what its own metadata says is inside it."""

    role: str = ""
    key: str = ""
    ok: bool = True
    error: str = ""
    variables: Table = dataclasses.field(default_factory=Table)
    unread: str = ""
    coords: Table = dataclasses.field(default_factory=Table)


@dataclasses.dataclass
class RecipeRow:
    """One recipe this ROI could be asked for, and what each of its elements resolves to."""

    recipe: str = ""
    heading: str = ""
    #: The verdict meter, rendered.
    mark: str = ""
    verdict: str = ""
    elements: list[str] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class LegendItem:
    """One entry of the state key."""

    mark: str = ""
    sentence: str = ""


@dataclasses.dataclass
class Diagram:
    """One picture, or the reason there is not one."""

    svg: str = ""
    source: str = ""
    error: str = ""


@dataclasses.dataclass
class RunGroup:
    """Every cluster run submitted for one plan token."""

    token: str = ""
    heading: str = ""
    mine: bool = False
    runs: Table = dataclasses.field(default_factory=Table)
    artifact: str = ""


#: Declared dependencies for each panel's computed vars. Reflex cannot infer them through the
#: function-local imports used here, and a var with no deps never recomputes.
#:
#: Everything derived from `describe` depends on the source, the generation and the ROI.
_ROI = ["live", "generation", "roi_uid"]
#: The ROI picker. The selected ROI is always offered, even when the filter excludes it.
_PICKER = ["live", "generation", "roi_uid", "roi_type", "roi_query"]
#: A survey covers the instance, so the ROI is not a dependency.
_SURVEY = ["live", "generation", "group_by", "surveyed"]
#: The build tab: the ROI's availability, the product, and every choice made about it.
_STAGING = [*_ROI, "product", "pins", "excluded", "optional", "extra",
            "added_choice", "added_variable", "added_name"]
#: A plan is identified by its content-hash token, measured against a Snapshot.
_PLAN = ["plan_token", "live", "generation"]


class ExplorerState(rx.State):
    """Everything a user has chosen. Nothing derived, nothing heavy."""

    # -- source -------------------------------------------------------------------------
    live: bool = False
    generation: int = 0
    reach_error: str = ""

    # -- selection ----------------------------------------------------------------------
    roi_uid: str = ""
    #: Picker filters. Neither changes the selected ROI.
    roi_type: str = ANY_TYPE
    roi_query: str = ""
    product: str = ""

    # -- the element choices ------------------------------------------------------------
    pins: dict[str, str] = {}
    excluded: list[str] = []
    optional: list[str] = []
    extra: dict[str, int] = {}
    added_choice: dict[str, str] = {}
    added_variable: dict[str, str] = {}
    added_name: dict[str, str] = {}

    # -- the rest of the request --------------------------------------------------------
    strictness: str = "degrade"
    #: 1 is full resolution. A higher value is an overview.
    downsample: int = 1
    register: bool = False
    dest: str = "./out"

    # -- panels that cost something and are therefore asked for -------------------------
    group_by: str = "nothing"
    surveyed: bool = False
    inspected: list[str] = []
    diagram_group: str = "roi"

    # -- the plan, and what became of it ------------------------------------------------
    plan_token: str = ""
    plan_error: str = ""
    submitted_token: str = ""
    jobs_generation: int = 0
    notice: str = ""
    notice_error: str = ""
    assembled: str = ""

    # Lifecycle.
    @rx.event
    def on_load(self):
        """Pick a source and a ROI the first time the page is drawn."""
        if FORCED_LIVE is not None:
            self.live = FORCED_LIVE
        if not self.roi_uid:
            self._reselect()

    def _reselect(self) -> None:
        """Select the first ROI the picker offers and clear every choice."""
        self.reach_error = ""
        try:
            cache.roi_options(live=self.live, generation=self.generation)
        except Exception as exc:  # noqa: BLE001 - an unreachable instance is a UI state
            self.reach_error = f"Cannot reach the instance: {type(exc).__name__}: {exc}"
            self.roi_uid = ""
            return
        matched = self._matching()
        self.roi_uid = matched[0].uid if matched else ""
        self._forget_product()

    def _forget_product(self) -> None:
        """Clear every choice tied to the current product and ROI."""
        self.product = ""
        self.pins = {}
        self.excluded = []
        self.optional = []
        self.extra = {}
        self.added_choice = {}
        self.added_variable = {}
        self.added_name = {}
        self.downsample = 1
        self.plan_token = ""
        self.plan_error = ""
        self.notice = ""
        self.notice_error = ""
        self.assembled = ""

    # Sidebar.
    @rx.event
    def use_live(self, live: bool):
        self.live = live
        self.surveyed = False
        self.inspected = []
        self.roi_type = ANY_TYPE
        self.roi_query = ""
        self._reselect()

    @rx.event
    def choose_roi(self, uid: str):
        """Select the ROI by uid."""
        self.roi_uid = uid
        self._forget_product()

    @rx.event
    def choose_roi_type(self, value: str):
        """Narrow the picker to one ROI subtype. Does NOT change the selection."""
        self.roi_type = value

    @rx.event
    def search_roi(self, value: str):
        """Narrow the picker by name. Does NOT change the selection."""
        self.roi_query = value

    @rx.event
    def refresh(self):
        """One button invalidates every cache, and the generation is what says so."""
        cache.clear_all()
        self.generation += 1
        self.surveyed = False
        self.inspected = []
        self.roi_type = ANY_TYPE
        self.roi_query = ""
        self.plan_token = ""

    @property
    def _roi_records(self) -> list:
        try:
            return cache.roi_options(live=self.live, generation=self.generation)
        except Exception:  # noqa: BLE001 - reported through `reach_error`, not raised at a page
            return []

    @property
    def _av(self):
        try:
            return cache.describe(live=self.live, generation=self.generation,
                                  roi_uid=self.roi_uid)
        except Exception:  # noqa: BLE001 - an unreachable instance is a UI state
            return None

    @property
    def _snap(self):
        return cache.snapshot(live=self.live, generation=self.generation)

    @property
    def _index(self):
        """The shared resolution. See `clinicalomx.extract.index`."""
        return cache.index(live=self.live, generation=self.generation)

    @rx.var(deps=[], auto_deps=False)
    def show_source_toggle(self) -> bool:
        return FORCED_LIVE is None

    # The picker: narrow, then list.
    def _matching(self) -> list:
        """The records the filter admits, matched on type and name, in source order."""
        q = self.roi_query.strip().casefold()
        return [r for r in self._roi_records
                if (self.roi_type in (ANY_TYPE, "") or r.type.name == self.roi_type)
                and (not q or q in r.name.casefold())]

    @rx.var(deps=["live", "generation"], auto_deps=False)
    def roi_types(self) -> list[str]:
        """Every ROI subtype on the instance, plus the "any type" entry."""
        return [ANY_TYPE, *sorted({r.type.name for r in self._roi_records})]

    @rx.var(deps=_PICKER, auto_deps=False)
    def roi_choices(self) -> list[RoiChoice]:
        """The picker list: filter matches, capped, always including the selected ROI."""
        matched = self._matching()
        shown = matched[:ROI_CHOICES_SHOWN]
        if self.roi_uid and not any(r.uid == self.roi_uid for r in shown):
            selected = next((r for r in self._roi_records if r.uid == self.roi_uid), None)
            if selected is not None:
                shown = [selected, *shown[:ROI_CHOICES_SHOWN - 1]]
        return [RoiChoice(uid=r.uid, label=_roi_label(r)) for r in shown]

    @rx.var(deps=_PICKER, auto_deps=False)
    def roi_choices_note(self) -> str:
        """How many ROIs are shown of how many, and whether the selection was added."""
        total = len(self._roi_records)
        found = self._matching()
        matched = len(found)
        shown = found[:ROI_CHOICES_SHOWN]
        carried = ("" if not self.roi_uid or any(r.uid == self.roi_uid for r in shown)
                   else " - the selected ROI is listed as well")
        if matched > ROI_CHOICES_SHOWN and matched == total:
            return (f"{ROI_CHOICES_SHOWN} of {total} ROIs shown - "
                    f"search by name to reach the rest{carried}")
        if matched > ROI_CHOICES_SHOWN:
            return (f"{ROI_CHOICES_SHOWN} of {matched} matches shown, of {total} ROIs - "
                    f"narrow the search{carried}")
        if matched < total:
            return f"{matched} of {total} ROIs match{carried}"
        return f"{total} ROIs"

    @rx.var(deps=["live", "generation"], auto_deps=False)
    def index_built_at(self) -> str:
        """When the instance was read: the age of every number on this page."""
        try:
            return self._index.built_at
        except Exception:  # noqa: BLE001 - an unreachable instance is a UI state
            return ""

    # Header.
    @rx.var(deps=_ROI, auto_deps=False)
    def roi_heading(self) -> str:
        av = self._av
        return f"{av.roi.roi_type} · {av.roi.name}" if av is not None else ""

    @rx.var(deps=_ROI, auto_deps=False)
    def subtree(self) -> str:
        av = self._av
        if av is None:
            return ""
        return ", ".join(f"{n} {t}" for t, n in sorted(av.subtree.items()))

    @rx.var(deps=_ROI, auto_deps=False)
    def recipes_buildable(self) -> str:
        av = self._av
        if av is None:
            return ""
        return (f"{sum(1 for r in av.recipes if r.verdict != 'blocked')}"
                f"/{len(av.recipes)} buildable")

    @rx.var(deps=_ROI, auto_deps=False)
    def caveats(self) -> list[str]:
        av = self._av
        return list(av.caveats()) if av is not None else []

    # Corpus: what exists across the instance.
    @rx.event
    def choose_group_by(self, value: str):
        self.group_by = value

    @rx.event
    def do_survey(self):
        self.surveyed = True

    @rx.var(deps=["live", "surveyed"], auto_deps=False)
    def survey_gated(self) -> bool:
        """True on a live instance until the user asks for a survey. A survey reads every ROI."""
        return self.live and not self.surveyed

    @property
    def _survey(self):
        if self.survey_gated:
            return None
        try:
            return cache.survey(live=self.live, generation=self.generation,
                                group_by="" if self.group_by == "nothing" else self.group_by)
        except Exception:  # noqa: BLE001 - an unreachable instance is a UI state
            return None

    @rx.var(deps=_SURVEY, auto_deps=False)
    def population(self) -> str:
        sv = self._survey
        if sv is None:
            return ""
        return ", ".join(f"{n} {t}" for t, n in sorted(sv.population.items()))

    @rx.var(deps=_SURVEY, auto_deps=False)
    def corpus_recipes(self) -> Table:
        sv = self._survey
        if sv is None:
            return Table()
        rows = [[r.recipe, r.target, r.roi_type, r.layout,
                 tokens.coverage(f"{r.buildable}/{r.ready + r.degraded + r.blocked}"),
                 str(r.ready), str(r.degraded), str(r.blocked),
                 ", ".join(x.split(":")[-1] for x in r.examples[:3])]
                for r in sv.recipes]
        return _table(
            [("recipe", "key"), ("target", "text"), ("asked of", "text"), ("layout", "text"),
             ("buildable", "html"), ("ready", "num"), ("degraded", "num"), ("blocked", "num"),
             ("e.g.", "text")],
            rows, note=_rows_note(len(rows), len(rows), unit="recipe"))

    @rx.var(deps=_SURVEY, auto_deps=False)
    def corpus_coverage(self) -> Table:
        sv = self._survey
        if sv is None:
            return Table()
        return _coverage_table(sorted(sv.coverage, key=lambda c: (-c.present, c.role)))

    @rx.var(deps=_SURVEY, auto_deps=False)
    def corpus_groups(self) -> Table:
        sv = self._survey
        if sv is None or not sv.groups:
            return Table()
        roles = sorted({c.role for covs in sv.groups.values() for c in covs if c.present})
        rows = [[name, *[tokens.coverage(next(
            (c.fraction for c in covs if c.role == role and c.present), "-"))
            for role in roles]]
            for name, covs in sv.groups.items()]
        return _table([(sv.group_by, "key"), *[(role, "html") for role in roles]],
                      rows, note=_rows_note(len(rows), len(rows), unit="group"))

    @rx.var(deps=_SURVEY, auto_deps=False)
    def corpus_group_by(self) -> str:
        sv = self._survey
        return (sv.group_by or "") if sv is not None else ""

    @rx.var(deps=_SURVEY, auto_deps=False)
    def corpus_caveats(self) -> list[str]:
        sv = self._survey
        return list(sv.caveats()) if sv is not None else []

    # What is here: elements, then stores, then what is inside one.
    @rx.var(deps=_ROI, auto_deps=False)
    def child_total(self) -> int:
        av = self._av
        return av.child_coverage[0].total if av is not None and av.child_coverage else 0

    @rx.var(deps=_ROI, auto_deps=False)
    def element_heading(self) -> str:
        av = self._av
        if av is None or not av.child_elements:
            return ""
        return (f"What can be addressed on {self.child_total} child ROI(s): "
                f"{len(av.child_elements)} element(s) over {len(av.child_stores)} "
                f"distinct store(s)")

    @rx.var(deps=_ROI, auto_deps=False)
    def element_table(self) -> Table:
        """What the catalogue can address on the children, one row per element."""
        av = self._av
        if av is None or not av.child_elements:
            return Table()
        rows = [[tokens.state_glyph_only(e.state), e.element, tokens.kind(e.kind), _store_of(e),
                 e.variable or "-",
                 tokens.coverage(e.coverage.fraction) if e.coverage else "-",
                 tokens.not_probed(),
                 str(len(e.unplaceable)),
                 ", ".join(av.shares_store(e)) or "-"]
                for e in sorted(av.child_elements, key=_element_order)]
        return _table(
            [("", "state"), ("element", "key"), ("kind", "html"), ("artifact", "path"),
             ("variable", "key"), ("usable", "html"), ("missing", "html"),
             ("unplaceable", "num"), ("shares the store with", "text")],
            rows, note=_rows_note(len(rows), len(rows), unit="element"))

    @rx.var(deps=_ROI, auto_deps=False)
    def twinned_note(self) -> str:
        av = self._av
        if av is None:
            return ""
        twinned = {k: n for k, n in av.child_stores.items() if len(n) > 1}
        if not twinned:
            return ""
        names = sorted({n for group in twinned.values() for n in group})
        return (f"{', '.join(names)} read the same {len(twinned)} store(s). Their fractions are "
                f"counts of ROIs, not of artifacts: adding them would report those stores twice.")

    @rx.var(deps=_ROI, auto_deps=False)
    def no_children(self) -> bool:
        av = self._av
        return av is None or not av.child_coverage

    @rx.var(deps=_ROI, auto_deps=False)
    def role_table(self) -> Table:
        """Role coverage over the children, including roles no recipe reads."""
        av = self._av
        if av is None:
            return Table()
        return _coverage_table(sorted(av.child_coverage, key=lambda c: (-c.present, c.role)))

    @rx.var(deps=_ROI, auto_deps=False)
    def role_examples(self) -> list[str]:
        av = self._av
        if av is None:
            return []
        out = []
        for c in av.child_coverage:
            for state, names in sorted(c.examples.items()):
                if state in ("undeclared", "unlinked") and c.present == 0:
                    continue  # a role nothing has
                out.append(f"{c.role} · {state}: {', '.join(names[:5])}")
        return out

    # One store per artifact, on request.
    @rx.event
    def look_inside(self):
        if self.roi_uid and self.roi_uid not in self.inspected:
            self.inspected.append(self.roi_uid)

    @rx.var(deps=_ROI, auto_deps=False)
    def stores_available(self) -> bool:
        av = self._av
        return av is not None and bool(av.child_stores)

    @rx.var(deps=["roi_uid", "inspected"], auto_deps=False)
    def stores_opened(self) -> bool:
        return self.roi_uid in self.inspected

    @rx.var(deps=[*_ROI, "inspected"], auto_deps=False)
    def stores(self) -> list[Store]:
        """What is inside each sampled store, and which arrays no element names."""
        av = self._av
        if av is None or not self.stores_opened:
            return []
        try:
            contents = cache.inspect(live=self.live, generation=self.generation,
                                     roi_uid=self.roi_uid)
        except Exception as exc:  # noqa: BLE001 - a store that cannot be opened is a UI state
            return [Store(role="", key="", ok=False, error=f"{type(exc).__name__}: {exc}")]

        out = []
        for role, found in sorted(contents.items()):
            if not found.ok:
                out.append(Store(role=role, key=found.key, ok=False, error=found.error))
                continue
            addressed = variables_addressed(av.child_elements, role)
            whole = addressed.get("")
            variables = _table(
                [("array", "key"), ("dims", "text"), ("shape", "num"), ("dtype", "text"),
                 ("addressed by", "text")],
                [[v.name, ", ".join(v.dims), " x ".join(str(n) for n in v.shape), v.dtype,
                  addressed.get(v.name) or (f"{whole} (whole store)" if whole else "-")]
                 for v in found.variables], density="default")
            unread = [v.name for v in found.variables
                      if v.name not in addressed and whole is None]
            coords = _table(
                [("coordinate", "key"), ("dims", "text"), ("shape", "num"), ("dtype", "text"),
                 ("values", "text")],
                [[c.name, ", ".join(c.dims) or "(scalar)",
                  " x ".join(str(n) for n in c.shape) or "1", c.dtype,
                  ", ".join(c.values[:8]) + ("..." if len(c.values) > 8 else "")]
                 for c in found.coords], density="default") if found.coords else Table()
            out.append(Store(
                role=role, key=found.key, ok=True, variables=variables, coords=coords,
                unread=(f"{', '.join(unread)} are in this store and no element names them. They "
                        f"are NOT in any plan, and nothing here will pick one up on your behalf."
                        if unread else "")))
        return out

    @rx.var(deps=_ROI, auto_deps=False)
    def recipe_rows(self) -> list[RecipeRow]:
        av = self._av
        if av is None:
            return []
        out = []
        for r in sorted(av.recipes, key=lambda r: (r.verdict != "ready", r.recipe)):
            elements = []
            for e in r.elements:
                role = (", ".join(f"{k} x{n}" for k, n in e.roles_used.items())
                        if len(e.roles_used) > 1 else (e.chosen_role or "-"))
                elements.append(
                    f"{tokens.kind(e.kind)} <code>{e.element}</code> &lt;- {role}"
                    f"{'' if e.satisfied else '  -  unavailable'}")
            out.append(RecipeRow(
                recipe=r.recipe, verdict=r.verdict,
                mark=tokens.verdict(r.verdict),
                heading=f"{r.recipe} · {r.target} · {r.layout} "
                        f"· {r.n_units} unit(s)",
                elements=elements, notes=list(r.notes)))
        return out

    # Build something.
    @property
    def _buildable(self) -> list:
        """Non-blocked recipes: ready first, then those filling the most elements."""
        av = self._av
        if av is None:
            return []
        out = [r for r in av.recipes if r.verdict != "blocked"] or list(av.recipes)
        out.sort(key=lambda r: (r.verdict != "ready",
                                -sum(1 for e in r.elements if e.satisfied), r.recipe))
        return out

    @property
    def _readiness(self):
        offered = self._buildable
        if not offered:
            return None
        for r in offered:
            if r.recipe == self.product:
                return r
        return offered[0]

    @rx.var(deps=_ROI, auto_deps=False)
    def nothing_buildable(self) -> bool:
        av = self._av
        return av is not None and not self._buildable

    @rx.var(deps=_ROI, auto_deps=False)
    def nothing_buildable_note(self) -> str:
        av = self._av
        return f"No recipe can be asked of a {av.roi.roi_type}." if av is not None else ""

    @rx.var(deps=_ROI, auto_deps=False)
    def product_labels(self) -> list[str]:
        return [_product_label(r) for r in self._buildable]

    @rx.var(deps=[*_ROI, "product"], auto_deps=False)
    def product_label(self) -> str:
        readiness = self._readiness
        return _product_label(readiness) if readiness is not None else ""

    @rx.event
    def choose_product(self, label: str):
        for r in self._buildable:
            if _product_label(r) == label:
                chosen = r.recipe
                break
        else:
            return
        if chosen != self.product:
            self._forget_product()
            self.product = chosen

    # The element rows.
    def _added_entries(self, av, readiness) -> tuple[list, dict[str, list[AddedRow]]]:
        """``(added entries, {kind: [AddedRow]})``, resolved in one pass over the slots."""
        added, rows = [], {}
        for kind in slots_for(readiness):
            declared = [e for e in readiness.elements if e.kind == kind]
            taken = {occupies(e, self.pins.get(e.element))
                     for e in declared if e.element not in self.excluded}
            slot_rows = []
            for i in range(self.extra.get(kind, 0)):
                key = f"{kind}:{i}"
                options = candidates_for(av, readiness, kind, taken)
                labels = [candidate_label(c, kind) for c in options]
                chosen = self.added_choice.get(key, "")
                cand = options[labels.index(chosen)] if chosen in labels else None
                fixed = bool(cand is not None and cand.variable)
                name = self.added_name.get(key, cand.element if cand is not None else "")
                variable = (cand.variable if fixed
                            else (self.added_variable.get(key, "") or ""))
                slot_rows.append(AddedRow(
                    kind=kind, index=i, slot_key=key, options=[NOTHING, *labels],
                    chosen=chosen if chosen in labels else NOTHING,
                    variable=variable, variable_fixed=fixed,
                    fixed_variable=cand.variable if fixed else "",
                    name=name, empty=cand is None))
                if cand is not None and name:
                    entry = dataclasses.replace(
                        cand, element=name, kind=kind, variable=variable or None,
                        chosen_role=_store_of(cand), required=False)
                    added.append(entry)
                    taken.add(occupies(entry))
            rows[kind] = slot_rows
        return added, rows

    @property
    def _added(self) -> list:
        av, readiness = self._av, self._readiness
        if av is None or readiness is None:
            return []
        return self._added_entries(av, readiness)[0]

    @rx.var(deps=_STAGING, auto_deps=False)
    def slots(self) -> list[Slot]:
        """The build page, one entry per target model slot."""
        av, readiness = self._av, self._readiness
        if av is None or readiness is None:
            return []
        added_entries, added_rows = self._added_entries(av, readiness)

        out = []
        for kind in slots_for(readiness):
            declared = [e for e in readiness.elements if e.kind == kind]
            rows = []
            for e in declared:
                labels, values = role_options(e)
                pinned = self.pins.get(e.element)
                chosen = labels[values.index(pinned)] if pinned in values else AUTO
                mixed = ("auto -> " + ", ".join(f"{r} x{n}" for r, n in e.roles_used.items())
                         if pinned is None and len(e.roles_used) > 1 else "")
                rows.append(DeclaredRow(
                    element=e.element, level=e.level,
                    mark=tokens.state_glyph_only(e.state),
                    coverage=tokens.coverage(e.coverage.fraction) if e.coverage else "",
                    unplaceable=(f"{len(e.unplaceable)} cannot be positioned"
                                 if e.unplaceable else ""),
                    options=labels, chosen=chosen, mixed=mixed,
                    variable=e.variable or "",
                    included=e.element not in self.excluded,
                    required=e.required,
                    allow_absent=e.element in self.optional))

            taken = {occupies(e, self.pins.get(e.element))
                     for e in declared if e.element not in self.excluded}
            taken |= {occupies(a) for a in added_entries if a.kind == kind}
            available = candidates_for(av, readiness, kind, taken)
            out.append(Slot(
                kind=kind, label=SLOT_LABEL.get(kind, kind),
                glyph=tokens.kind(kind), help=SLOT_HELP.get(kind, ""),
                count=_entries(len(declared) + len(added_rows.get(kind, []))),
                declared=rows, added=added_rows.get(kind, []),
                add_disabled=not available,
                add_help=(f"{len(available)} artifact(s) on this ROI could fill this slot"
                          if available else
                          "no artifact on this ROI is left to put here"),
                empty=not declared and not added_rows.get(kind)))
        return out

    @rx.event
    def pin_role(self, element: str, label: str):
        readiness = self._readiness
        if readiness is None:
            return
        for e in readiness.elements:
            if e.element != element:
                continue
            labels, values = role_options(e)
            role = values[labels.index(label)] if label in labels else None
            if role is None:
                self.pins.pop(element, None)
            else:
                self.pins[element] = role
            self._invalidate_plan()
            return

    @rx.event
    def include_element(self, element: str, on: bool):
        if on:
            if element in self.excluded:
                self.excluded.remove(element)
        elif element not in self.excluded:
            self.excluded.append(element)
        self._invalidate_plan()

    @rx.event
    def allow_absent(self, element: str, on: bool):
        if on:
            if element not in self.optional:
                self.optional.append(element)
        elif element in self.optional:
            self.optional.remove(element)
        self._invalidate_plan()

    @rx.event
    def add_row(self, kind: str):
        self.extra[kind] = self.extra.get(kind, 0) + 1
        self._invalidate_plan()

    @rx.event
    def drop_row(self, kind: str, index: int):
        """Remove ONE row, and shift the rows under it up so the keys stay dense."""
        n = self.extra.get(kind, 0)
        for i in range(index, n - 1):
            for store in (self.added_choice, self.added_variable, self.added_name):
                nxt = store.get(f"{kind}:{i + 1}")
                if nxt is None:
                    store.pop(f"{kind}:{i}", None)
                else:
                    store[f"{kind}:{i}"] = nxt
        for store in (self.added_choice, self.added_variable, self.added_name):
            store.pop(f"{kind}:{n - 1}", None)
        self.extra[kind] = max(0, n - 1)
        self._invalidate_plan()

    @rx.event
    def choose_added(self, key: str, label: str):
        if label == NOTHING:
            self.added_choice.pop(key, None)
        else:
            self.added_choice[key] = label
        self.added_name.pop(key, None)
        self.added_variable.pop(key, None)
        self._invalidate_plan()

    @rx.event
    def edit_added_variable(self, key: str, value: str):
        self.added_variable[key] = value
        self._invalidate_plan()

    @rx.event
    def edit_added_name(self, key: str, value: str):
        self.added_name[key] = value
        self._invalidate_plan()

    @rx.var(deps=_STAGING, auto_deps=False)
    def name_clash(self) -> str:
        """A clash between an added entry's name and another element's name, or ``""``."""
        readiness = self._readiness
        if readiness is None:
            return ""
        names = [a.element for a in self._added]
        clash = sorted(set(names) & {e.element for e in readiness.elements})
        if clash:
            return (f"{', '.join(clash)} is already a slot in this product; give the added entry "
                    f"its own name.")
        if len(names) != len(set(names)):
            return "Two added entries share a name; one would overwrite the other."
        return ""

    # The rest of the request.
    @rx.event
    def choose_strictness(self, value: str):
        self.strictness = value
        self._invalidate_plan()

    @rx.event
    def choose_downsample(self, value: list[float]):
        # The slider value is an index into `DOWNSAMPLE`, not a factor.
        n = int(value[0]) if value else 0
        self.downsample = DOWNSAMPLE[min(max(n, 0), len(DOWNSAMPLE) - 1)]
        self._invalidate_plan()

    @rx.event
    def toggle_register(self, on: bool):
        self.register = on
        self.dest = "extracts/demo" if on else "./out"
        self._invalidate_plan()

    @rx.event
    def edit_dest(self, value: str):
        self.dest = value
        self._invalidate_plan()

    @rx.var(deps=[*_ROI, "product", "downsample"], auto_deps=False)
    def downsample_index(self) -> int:
        return DOWNSAMPLE.index(self.downsample) if self.downsample in DOWNSAMPLE else 0

    @rx.var(deps=[*_ROI, "product", "downsample"], auto_deps=False)
    def downsample_label(self) -> str:
        return "full" if self.downsample == 1 else f"1/{self.downsample}"

    @rx.var(deps=[*_ROI, "product"], auto_deps=False)
    def downsamples_a_plate(self) -> bool:
        """True when the product is a merged OME-Zarr plate rather than a mosaic."""
        readiness = self._readiness
        return readiness is not None and readiness.target == "OME-Zarr"

    @rx.var(deps=[*_ROI, "product"], auto_deps=False)
    def offers_downsample(self) -> bool:
        """True for a merged OME-TIFF or OME-Zarr product, the only ones that downsample."""
        readiness = self._readiness
        return (readiness is not None and readiness.layout == "merged"
                and readiness.target in ("OME-TIFF", "OME-Zarr"))

    @rx.var(deps=["register"], auto_deps=False)
    def dest_label(self) -> str:
        return "Artifact key prefix" if self.register else "Output directory"

    def _invalidate_plan(self) -> None:
        """Clear the plan and its outcome."""
        self.plan_token = ""
        self.plan_error = ""
        self.notice = ""
        self.notice_error = ""
        self.assembled = ""

    # Blueprint.
    @rx.event
    def choose_diagram_group(self, value: str | list[str]):
        # A segmented control returns `str | list[str]`.
        self.diagram_group = value[0] if isinstance(value, list) else value

    @property
    def _effective(self):
        readiness = self._readiness
        if readiness is None:
            return None
        return effective(readiness, self.excluded, dict(self.pins), self._added)

    @rx.var(deps=[*_ROI, "product"], auto_deps=False)
    def source_diagram(self) -> Diagram:
        av, readiness = self._av, self._readiness
        if av is None or readiness is None:
            return Diagram()
        return _draw(diagram.source_dot(av, self._snap, recipe=recipe_named(readiness.recipe)))

    @rx.var(deps=[*_STAGING, "diagram_group"], auto_deps=False)
    def object_diagram(self) -> Diagram:
        """The object diagram of the request, with every user choice applied."""
        av, readiness = self._av, self._readiness
        if av is None or readiness is None:
            return Diagram()
        recipe = recipe_named(readiness.recipe)
        eff = self._effective
        relations = None
        if recipe is not None and readiness.target == "SpatialData":
            relations = rel.resolve(av.roi.name, preview_entries(av, eff, self._snap), self._snap)
        return _draw(diagram.object_dot(
            eff, relations, group=self.diagram_group,
            aggregates=bool(recipe and recipe.aggregates)))

    @rx.var(deps=[], auto_deps=False)
    def diagram_groupings(self) -> list[str]:
        return list(diagram.GROUPING)

    @rx.var(deps=[], auto_deps=False)
    def legend_states(self) -> list[LegendItem]:
        """The state key, in ladder order, worst first."""
        return [LegendItem(mark=mark, sentence=sentence)
                for mark, sentence in tokens.legend_states()]

    @rx.var(deps=[], auto_deps=False)
    def legend_edges(self) -> str:
        """The edge half of `diagram.legend()`."""
        return tokens.legend_edges(diagram.legend())

    @rx.var(deps=[*_ROI, "product", "excluded"], auto_deps=False)
    def building_heading(self) -> str:
        readiness = self._readiness
        if readiness is None:
            return ""
        dropped = f" · {len(self.excluded)} removed" if self.excluded else ""
        return f"Building - one {readiness.target}{dropped}"

    @rx.var(deps=_STAGING, auto_deps=False)
    def relation_caveats(self) -> list[str]:
        av, readiness = self._av, self._readiness
        if av is None or readiness is None or readiness.target != "SpatialData":
            return []
        if recipe_named(readiness.recipe) is None:
            return []
        relations = rel.resolve(av.roi.name, preview_entries(av, self._effective, self._snap),
                                self._snap)
        return list(relations.caveats())

    # Plan.
    @rx.event
    def make_plan(self):
        from pydantic import ValidationError

        from clinicalomx.extract.plan import plan as _plan
        from clinicalomx.extract.request import (
            AddedElement,
            Destination,
            ElementSpec,
            ExtractRequest,
            ROISelector,
        )

        readiness = self._readiness
        if readiness is None:
            return
        self._invalidate_plan()
        try:
            request = ExtractRequest(
                roi=ROISelector(uid=self.roi_uid),
                target=readiness.target,
                recipe=readiness.recipe,
                elements=ElementSpec(
                    exclude=list(self.excluded), optional=list(self.optional),
                    roles=dict(self.pins),
                    add=[AddedElement(name=a.element, role=a.chosen_role, kind=a.kind,
                                      variable=a.variable, level=a.level)
                         for a in self._added]),
                destination=Destination(
                    kind="artifact" if self.register else "path",
                    **({"key": self.dest} if self.register else {"path": self.dest})),
                strictness=self.strictness,
                downsample=self.downsample,
            )
        except ValidationError as exc:
            self.plan_error = f"That combination is not a valid request:\n\n{exc}"
            return

        try:
            result = _plan(request, index=self._index)
        except (LookupError, ValueError) as exc:
            self.plan_error = f"Cannot plan: {exc}"
            return
        self.plan_token = cache.remember_plan(result)

    @property
    def _plan(self):
        return cache.plan(self.plan_token) if self.plan_token else None

    @rx.var(deps=_PLAN, auto_deps=False)
    def has_plan(self) -> bool:
        return self._plan is not None

    @rx.var(deps=_PLAN, auto_deps=False)
    def plan_blocked(self) -> bool:
        result = self._plan
        return bool(result is not None and result.summarise()[1])

    @rx.var(deps=_PLAN, auto_deps=False)
    def plan_headline(self) -> str:
        result = self._plan
        if result is None:
            return ""
        _, blocked = result.summarise()
        return (f"{'BLOCKED' if blocked else 'Plannable'} · {len(result.buildable)} unit(s) "
                f"· token {result.token}")

    @rx.var(deps=_PLAN, auto_deps=False)
    def plan_wiring(self) -> str:
        """The resolver's own role-by-element summary."""
        result = self._plan
        return result.summarise()[0] if result is not None else ""

    @rx.var(deps=_PLAN, auto_deps=False)
    def plan_caveats(self) -> list[str]:
        result = self._plan
        return list(result.caveats()) if result is not None else []

    # Layout.
    @property
    def _layout(self):
        result = self._plan
        if result is None:
            return None
        try:
            return cache.layout(token=result.token, live=self.live,
                                generation=self.generation, measure=self.live)
        except Exception:  # noqa: BLE001 - a preview that cannot be read is a UI state
            from clinicalomx.extract.layout import layout_of

            return layout_of(result, snapshot=self._snap, measure=False)

    @rx.var(deps=_PLAN, auto_deps=False)
    def layout_heading(self) -> str:
        layout = self._layout
        if layout is None:
            return ""
        head = layout.target
        if layout.n_outputs != 1:
            head += f" x {layout.n_outputs}"
        return f"{head} - {layout.output}"

    @rx.var(deps=_PLAN, auto_deps=False)
    def layout_table(self) -> Table:
        layout = self._layout
        if layout is None or not layout.pieces:
            return Table()
        rows = [[tokens.kind(kind_of(p.slot)), p.key, p.slot,
                 " x ".join(str(n) for n in p.shape) or "-",
                 p.dtype or "-",
                 (f"{len(p.channels)}: " + ", ".join(p.channels[:4])
                  + ("..." if len(p.channels) > 4 else "")) if p.channels else "-",
                 f"{p.element} <- {p.role or '-'}" + (f"#{p.variable}" if p.variable else ""),
                 p.transform or ("mosaic of " + str(p.n_from) if p.n_from > 1 else "-"),
                 p.note]
                for p in layout.pieces[:LAYOUT_PIECE_LIMIT]]
        return _table(
            [("", "html"), ("in the object", "path"), ("slot", "text"), ("dimensions", "num"),
             ("dtype", "text"), ("channels", "text"), ("filled by", "key"),
             ("placed", "text"), ("note", "text")],
            rows, note=_rows_note(len(rows), len(layout.pieces), unit="piece"))

    @rx.var(deps=_PLAN, auto_deps=False)
    def layout_overflow(self) -> str:
        layout = self._layout
        if layout is None or len(layout.pieces) <= LAYOUT_PIECE_LIMIT:
            return ""
        return f"{len(layout.pieces) - LAYOUT_PIECE_LIMIT} more in the full layout below."

    @rx.var(deps=_PLAN, auto_deps=False)
    def layout_absences(self) -> list[str]:
        """Every absence as an effect on the output, with the resolver's word."""
        layout = self._layout
        if layout is None:
            return []
        return [f"{a.element} - {a.effect}"
                + (f"  (the resolver calls it {a.state})" if a.state else "")
                for a in layout.absences]

    @rx.var(deps=_PLAN, auto_deps=False)
    def layout_notes(self) -> list[str]:
        layout = self._layout
        return list(layout.notes) if layout is not None else []

    @rx.var(deps=_PLAN, auto_deps=False)
    def layout_reads(self) -> str:
        layout = self._layout
        if layout is None or not layout.reads:
            return ""
        return (f"{layout.reads} metadata read(s), batched, against {layout.measured_on}. "
                f"No pixel was read: dimensions come from each store's own metadata and the "
                f"crop extent is arithmetic.")

    @rx.var(deps=_PLAN, auto_deps=False)
    def layout_ome_xml(self) -> str:
        layout = self._layout
        return layout.ome_xml or "" if layout is not None else ""

    @rx.var(deps=_PLAN, auto_deps=False)
    def layout_tree(self) -> str:
        layout = self._layout
        if layout is None:
            return ""
        return layout.tree(max_keys=len(layout.pieces) or 1)

    @rx.var(deps=_PLAN, auto_deps=False)
    def layout_json(self) -> str:
        """The layout as JSON."""
        layout = self._layout
        return json.dumps(layout.to_dict(), indent=2, default=str) if layout is not None else ""

    # Starting the build.
    @rx.var(cache=False)
    def can_submit(self) -> bool:
        """True when an orchestrator is registered; otherwise builds run in this process."""
        return hop.can_submit()

    @rx.var(deps=["live"], auto_deps=False)
    def build_disabled(self) -> bool:
        return not self.live

    @rx.var(deps=["live"], auto_deps=False)
    def build_disabled_note(self) -> str:
        return "Demo data has no bytes to read. Switch to the live instance." if not self.live \
            else ""

    @rx.var(deps=_PLAN, auto_deps=False)
    def units(self) -> int:
        result = self._plan
        return len(result.buildable) if result is not None else 0

    @rx.var(deps=_PLAN, auto_deps=False)
    def roi_name(self) -> str:
        result = self._plan
        return result.roi_name if result is not None else ""

    @property
    def _sizing(self) -> tuple[int, str]:
        """``(units per job, "")``, or ``(0, why not)``. Reads the Prefect deployment."""
        try:
            return hop.units_per_run(walltime_minutes=hop.walltime_minutes()), ""
        except hop.SubmitError as exc:
            return 0, str(exc)
        except Exception as exc:  # noqa: BLE001 - an unreachable orchestrator is a UI state
            return 0, f"{type(exc).__name__}: {exc}"

    @rx.var(cache=False)
    def sizing_failure(self) -> str:
        """Why the job size could not be read, or ``""``."""
        if not self.live or not self.can_submit or self._plan is None:
            return ""
        return self._sizing[1]

    @rx.var(cache=False)
    def submit_note(self) -> str:
        """The cost caption shown before submitting, from `Job.estimate` over this plan."""
        if self.sizing_failure or not self.live or not self.can_submit or self._plan is None:
            return ""
        per_run = self._sizing[0]
        if not per_run:
            return ""
        result = self._plan
        try:
            layout = self._layout
        except Exception:  # noqa: BLE001 - an unread layout leaves scratch unmeasured, and says so
            layout = None
        est = Job(intent="", request=result.request or {}, plans=[result]).estimate(
            per_run=per_run, layouts={result.token: layout} if layout is not None else None)
        return (f"{est['units']} unit(s) over {est['runs']} cluster job(s), up to {per_run} "
                f"core(s) per job. "
                f"{est['note']} That is roughly {est['rough_hours']}h, so this is not a "
                f"wait-and-watch operation. Jobs are resumable: submitting the same plan again "
                f"rebuilds only what is missing.")

    @rx.var(cache=False)
    def in_process_note(self) -> str:
        if self.can_submit:
            return ""
        return ("This process has no orchestrator registered, so a build runs HERE and needs the "
                "builders and the storage. That is right on a dev box and wrong on the control "
                "plane, which registers one at startup.")

    @rx.event
    def submit(self):
        result = self._plan
        if result is None:
            return
        self.notice = self.notice_error = ""
        per_run, why_not = self._sizing
        if why_not:
            self.notice_error = why_not
            return
        try:
            # Submit with the job size the caption showed.
            runs = hop.submit(result, per_run=per_run)
        except hop.SubmitError as exc:
            self.notice_error = str(exc)
        except Exception as exc:  # noqa: BLE001 - an unreachable orchestrator is a UI state
            self.notice_error = f"Could not submit: {type(exc).__name__}: {exc}"
        else:
            self.notice = (f"Submitted {len(runs)} run(s) for plan {result.token}. "
                           f"Watch them on the Jobs tab.")
            self.submitted_token = result.token

    @rx.event
    def assemble(self):

        result = self._plan
        if result is None:
            return
        self.notice = self.notice_error = self.assembled = ""
        try:
            out = _assemble(result, confirm=True, snapshot=self._snap)
        except AssemblyError as exc:
            self.notice_error = str(exc)
        else:
            self.notice = f"Built {out['n_built']}, registered {out['n_registered']}"
            self.assembled = json.dumps(out, indent=2, default=str)

    # Jobs.
    @rx.event
    def refresh_runs(self):
        self.jobs_generation += 1

    @rx.var(cache=False)
    def jobs_error(self) -> str:
        if not hop.can_submit():
            return ""
        try:
            cache.submitted_runs(self.jobs_generation)
        except Exception as exc:  # noqa: BLE001 - an unreachable orchestrator is a UI state
            return f"Cannot reach the orchestrator: {type(exc).__name__}: {exc}"
        return ""

    @property
    def _runs(self) -> tuple[list, str]:
        if not hop.can_submit():
            return [], ""
        try:
            return cache.submitted_runs(self.jobs_generation)
        except Exception:  # noqa: BLE001 - reported through `jobs_error`
            return [], ""

    @rx.var(cache=False)
    def runs_read_at(self) -> str:
        return self._runs[1]

    @rx.var(cache=False)
    def run_groups(self) -> list[RunGroup]:
        """Submitted runs, grouped by plan token, read from the orchestrator.

        Per-unit outcomes are in the Prefect table artifact ``extract-<token>``.
        """
        runs, _ = self._runs
        by_token: dict[str, list] = {}
        for run in runs:
            by_token.setdefault(run.token, []).append(run)
        out = []
        for token, group in by_token.items():
            tally: dict[str, int] = {}
            for run in group:
                tally[run.state] = tally.get(run.state, 0) + 1
            units = sum(len(r.outputs) for r in group)
            out.append(RunGroup(
                token=token, mine=token == self.submitted_token,
                heading=f"{token or '(untagged)'} - {len(group)} run(s), {units} unit(s) - "
                        + ", ".join(f"{n} {s}" for s, n in sorted(tally.items())),
                runs=_table(
                    [("run", "key"), ("state", "text"), ("units", "num"), ("submitted", "text")],
                    [[r.name, r.state, str(len(r.outputs)), str(r.submitted_at)]
                     for r in group], density="comfortable"),
                artifact=f"Per-unit outcomes are published by each run as the Prefect table "
                         f"artifact extract-{token}."))
        return out

    @rx.var(cache=False)
    def no_runs_note(self) -> str:
        if self.run_groups:
            return ""
        return ("Nothing submitted yet." if hop.can_submit() else
                "This process has no orchestrator registered, so nothing can be submitted from "
                "here and there is nothing to watch. Plan and assemble in process instead.")


# ---------------------------------------------------------------------------------------
def _roi_label(r) -> str:
    return f"{r.type.name} · {r.name}"


def _entries(n: int) -> str:
    return f"{n} entry" if n == 1 else f"{n} entries"


def _product_label(r) -> str:
    """A product `<select>` option label."""
    return (f"{r.verdict} · {r.recipe} · {r.target} · "
            f"{r.layout} · {r.n_units} unit(s)")


def _table(spec: list[tuple[str, str]], rows: list[list[str]], *,
           density: str = "compact", note: str = "") -> Table:
    """A Table from ``[(column, cell kind)]`` and the rows."""
    return Table(columns=[c for c, _ in spec], kinds=[k for _, k in spec],
                 rows=rows, density=density, note=note)


def _rows_note(shown: int, total: int, *, unit: str = "row") -> str:
    """A row count, and what was cut."""
    if total > shown:
        return f"{shown} of {total} {unit}(s) shown; {total - shown} not listed"
    return f"{total} {unit}(s)"


def _coverage_table(coverage) -> Table:
    """One row per role. MISSING always shows the not-probed dash."""
    rows = [[tokens.state_glyph_only(c.state), c.role, tokens.coverage(c.fraction),
             str(c.dangling), tokens.not_probed(), str(c.denied), str(c.unlinked),
             c.owner or ("(undeclared)" if c.state == "undeclared" else "(unowned)")]
            for c in coverage]
    return _table(
        [("", "state"), ("role", "key"), ("usable", "html"), ("dangling", "num"),
         ("missing", "html"), ("denied", "num"), ("unlinked", "num"), ("owner", "text")],
        rows, note=_rows_note(len(rows), len(rows), unit="role"))


def _draw(source: str) -> Diagram:
    try:
        return Diagram(svg=dot.render(source), source=source)
    except dot.DotUnavailable as exc:
        return Diagram(source=source, error=str(exc))
