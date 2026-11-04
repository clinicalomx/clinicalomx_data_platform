"""The Explorer screen: structure and behaviour only. Appearance comes from `web/tokens.py`.

Every panel reads one computed var off `ExplorerState`. The app never probes artifact bytes, so
every MISSING column shows the not-probed dash.
"""
from __future__ import annotations

import reflex as rx

from clinicalomx.extract.app.web import tokens
from clinicalomx.extract.app.web.state import (
    DOWNSAMPLE,
    STRICTNESS,
    STRICTNESS_HELP,
    AddedRow,
    DeclaredRow,
    Diagram,
    ExplorerState,
    LegendItem,
    RecipeRow,
    RunGroup,
    Slot,
    Store,
    Table,
)

S = ExplorerState

#: Corpus grouping, as the widget offers it.
GROUP_BY = ["nothing", "roi_type", "slide", "cohort", "patient", "source"]


# Shared pieces.
def caption(text) -> rx.Component:
    return rx.el.p(text, class_name="cx-caption")


def captions(items) -> rx.Component:
    return rx.vstack(rx.foreach(items, caption), spacing=tokens.GAP["hair"],
                     align_items="start", width="100%")


def markup(value) -> rx.Component:
    """Pre-rendered markup from `web/tokens.py`: a chip, a coverage bar, a kind glyph."""
    return rx.html(value, class_name="cx-inline")


def notice(text, severity: str, *, label: str = "") -> rx.Component:
    """One sentence the reader has to see. ``severity`` is info, ok, warn or blocked."""
    return rx.cond(
        text != "",
        rx.el.div(
            *([rx.el.span(label, class_name="cx-notice__label")] if label else []),
            rx.el.span(text),
            class_name=f"cx-notice cx-notice--{severity}"))


def notices(items, severity: str, *, label: str = "") -> rx.Component:
    return rx.vstack(rx.foreach(items, lambda x: notice(x, severity, label=label)),
                     spacing=tokens.GAP["hair"], width="100%")


def _cell(value, kind) -> rx.Component:
    """One table cell, drawn by its column's kind."""
    return rx.match(
        kind,
        ("state", rx.table.cell(markup(value), class_name="cx-col-state")),
        ("html", rx.table.cell(markup(value))),
        ("path", rx.table.cell(
            rx.el.span(value, class_name="cx-key cx-key--path", title=value))),
        ("key", rx.table.cell(value, class_name="cx-col-key")),
        ("num", rx.table.cell(value, class_name="cx-num")),
        rx.table.cell(value))


def table(data: Table) -> rx.Component:
    """Any table on the page, with a sticky header and a row-count footer."""
    return rx.cond(
        data.rows.length() > 0,
        rx.el.div(
            rx.el.div(
                rx.el.table(
                    rx.el.thead(rx.el.tr(rx.foreach(data.columns, rx.el.th))),
                    rx.el.tbody(rx.foreach(
                        data.rows,
                        lambda row: rx.el.tr(rx.foreach(
                            row, lambda cell, i: _cell(cell, data.kinds[i]))))),
                    class_name="cx-table"),
                class_name="cx-table-scroll",
                style={"--cx-row-h": tokens.DENSITY["compact"]}),
            rx.cond(data.note != "", rx.el.div(data.note, class_name="cx-table-foot")),
            width="100%"))


def disclosure(title, *children, open_: bool = False) -> rx.Component:
    """A foldable ruled header row with an indented body."""
    return rx.el.details(
        rx.el.summary(
            rx.el.span("▸", class_name="cx-disclosure__caret"),
            rx.el.span(title),
            class_name="cx-disclosure__head"),
        rx.el.div(*children, class_name="cx-disclosure__body"),
        open=open_, class_name="cx-disclosure")


def code(text, language: str) -> rx.Component:
    """A long code block, bounded at the table height and scrolled."""
    return rx.cond(
        text != "",
        rx.scroll_area(
            rx.code_block(text, language=language, wrap_long_lines=True, width="100%"),
            type="auto", style={"max_height": tokens.CODE_MAX_H}, width="100%"))


def button(label, *, on_click=None, disabled=False, primary: bool = False,
           ghost: bool = False, title: str = "") -> rx.Component:
    """A `.cx-btn`. At most one ``primary`` per surface."""
    kind = " cx-btn--primary" if primary else (" cx-btn--ghost" if ghost else "")
    return rx.el.button(label, on_click=on_click, disabled=disabled, title=title,
                        class_name=f"cx-btn{kind}", type="button")


def readout(label, value) -> rx.Component:
    """A label above a value."""
    return rx.el.div(
        rx.el.div(label, class_name="cx-readout__label"),
        rx.el.div(value, class_name="cx-readout__value"),
        class_name="cx-readout")


def section(title, *children) -> rx.Component:
    return rx.vstack(
        rx.el.h3(title, class_name="cx-panel__title"),
        *children,
        spacing=tokens.GAP["block"], align_items="start", width="100%")


# Sidebar.
def sidebar() -> rx.Component:
    """Source, ROI picker, and when the data on screen was read."""
    return rx.el.aside(
        rx.vstack(
            rx.cond(
                S.show_source_toggle,
                rx.vstack(
                    rx.el.div("Source", class_name="cx-readout__label"),
                    rx.hstack(
                        rx.switch(checked=S.live, on_change=S.use_live),
                        rx.el.span("Live instance"),
                        spacing=tokens.GAP["row"], align="center"),
                    rx.cond(
                        ~S.live,
                        notice("Demo data. The resolver, the states and the plan are all real; "
                               "only the instance is fabricated.", "info", label="demo")),
                    spacing=tokens.GAP["row"], align_items="start", width="100%")),
            notice(S.reach_error, "blocked", label="unreachable"),
            rx.el.div("ROI", class_name="cx-readout__label"),
            # Narrow, then list: filter by ROI type and by name.
            rx.debounce_input(
                rx.el.select(
                    rx.foreach(S.roi_types, lambda t: rx.el.option(t, value=t)),
                    value=S.roi_type, on_change=S.choose_roi_type,
                    class_name="cx-select", width="100%"),
                debounce_timeout=0),
            rx.debounce_input(
                rx.el.input(placeholder="search by name", value=S.roi_query,
                            on_change=S.search_roi, class_name="cx-input", width="100%"),
                debounce_timeout=250),
            # `rx.debounce_input` keeps the pick on screen until the server state returns.
            # Options carry the uid, not the label.
            rx.debounce_input(
                rx.el.select(
                    rx.foreach(S.roi_choices,
                               lambda c: rx.el.option(c.label, value=c.uid)),
                    value=S.roi_uid, on_change=S.choose_roi,
                    class_name="cx-select", width="100%"),
                debounce_timeout=0),
            caption(S.roi_choices_note),
            rx.el.hr(class_name="cx-rule"),
            caption(f"index built {S.index_built_at}"),
            button("↻ Refresh from instance", on_click=S.refresh),
            rx.color_mode.button(),
            spacing=tokens.GAP["block"], align_items="start", width="100%"),
        class_name="cx-sidebar")


# Corpus.
def corpus_panel() -> rx.Component:
    """What exists across the instance. Opt-in against a live instance."""
    return rx.vstack(
        rx.hstack(
            rx.el.span("Group by", class_name="cx-readout__label"),
            rx.el.select(
                rx.foreach(GROUP_BY, lambda o: rx.el.option(o, value=o)),
                value=S.group_by, on_change=S.choose_group_by, class_name="cx-select"),
            spacing=tokens.GAP["row"], align="center"),
        rx.cond(
            S.survey_gated,
            rx.vstack(
                caption("A survey reads every ROI on the instance. It is cached once run."),
                button("Survey the instance", on_click=S.do_survey, primary=True),
                spacing=tokens.GAP["row"], align_items="start"),
            rx.vstack(
                rx.el.p(f"Population - {S.population}", class_name="cx-lead"),
                section("What could be built", table(S.corpus_recipes)),
                section("Artifact coverage across the instance",
                        caption("One row per role, so a store holding several addressable "
                                "arrays is one row here."),
                        table(S.corpus_coverage)),
                rx.cond(
                    S.corpus_groups.rows.length() > 0,
                    section(f"By {S.corpus_group_by}", table(S.corpus_groups))),
                notices(S.corpus_caveats, "warn", label="caveat"),
                spacing=tokens.GAP["section"], align_items="start", width="100%")),
        spacing=tokens.GAP["section"], align_items="start", width="100%")


# What is here.
def store_card(s: Store) -> rx.Component:
    return disclosure(
        rx.fragment(rx.el.span(s.role), rx.el.code(s.key, class_name="cx-key cx-key--path")),
        rx.cond(
            s.ok,
            rx.vstack(
                table(s.variables),
                notice(s.unread, "warn", label="unaddressed"),
                rx.cond(
                    s.coords.rows.length() > 0,
                    rx.vstack(
                        caption("Coordinates. Values are shown where the coordinate is short "
                                "enough that reading them is metadata rather than an axis read."),
                        table(s.coords),
                        spacing=tokens.GAP["hair"], align_items="start", width="100%")),
                spacing=tokens.GAP["row"], align_items="start", width="100%"),
            notice(s.error, "warn", label="unreadable")),
        open_=True)


def recipe_card(r: RecipeRow) -> rx.Component:
    return disclosure(
        rx.fragment(markup(r.mark), rx.el.span(r.heading)),
        rx.vstack(
            rx.foreach(r.elements, markup),
            captions(r.notes),
            spacing=tokens.GAP["hair"], align_items="start", width="100%"))


def here_panel() -> rx.Component:
    """Elements on the children, then role coverage, then store contents."""
    return rx.vstack(
        rx.cond(
            S.no_children,
            caption("This ROI has no children, so there is no coverage to report."),
            rx.vstack(
                rx.cond(
                    S.element_heading != "",
                    rx.vstack(
                        rx.el.p(S.element_heading, class_name="cx-lead"),
                        table(S.element_table),
                        notice(S.twinned_note, "info", label="shared store"),
                        spacing=tokens.GAP["row"], align_items="start", width="100%"),
                    caption("No recipe in this catalogue reads anything from these children.")),
                disclosure(
                    f"Roles carried by those {S.child_total} child ROI(s)",
                    table(S.role_table),
                    captions(S.role_examples)),
                spacing=tokens.GAP["section"], align_items="start", width="100%")),
        rx.cond(S.stores_available, stores_panel()),
        section("Recipes", rx.foreach(S.recipe_rows, recipe_card)),
        notices(S.caveats, "warn", label="caveat"),
        spacing=tokens.GAP["section"], align_items="start", width="100%")


def stores_panel() -> rx.Component:
    """What is inside the stores. Opt-in: it opens one store per artifact."""
    return section(
        "What is inside those stores",
        caption("Lists each store's arrays from its own metadata. No pixel is read: a data "
                "variable's dims, shape and dtype come from the store's JSON, and only a short "
                "coordinate's values are ever touched. One store is opened per artifact, named "
                "below; nothing here asserts the others match it."),
        rx.cond(
            S.stores_opened,
            rx.vstack(rx.foreach(S.stores, store_card), spacing=tokens.GAP["row"], width="100%"),
            rx.vstack(
                button("Look inside one store per artifact", on_click=S.look_inside,
                       disabled=~S.live,
                       title=rx.cond(S.live, "",
                                     "Demo data has no bytes to read. Switch to the live "
                                     "instance.")),
                rx.cond(~S.live, caption("Demo data has no bytes to read. Switch to the live "
                                         "instance.")),
                spacing=tokens.GAP["hair"], align_items="start")))


# Blueprint.
def diagram(d: Diagram) -> rx.Component:
    """One picture, or the DOT source and the reason it was not drawn.

    `.cx-diagram` keeps a light ground in both colour modes: the DOT carries light-mode colours.
    """
    return rx.cond(
        d.error == "",
        rx.el.div(rx.html(d.svg), class_name="cx-diagram"),
        rx.vstack(notice(d.error, "warn", label="no graphviz"), code(d.source, "dot"),
                  spacing=tokens.GAP["row"], width="100%"))


def legend_item(item: LegendItem) -> rx.Component:
    return rx.el.span(markup(item.mark), rx.el.span(item.sentence),
                      class_name="cx-legend__item")


def blueprint() -> rx.Component:
    """The source and object diagrams, side by side, with the user's choices applied."""
    return disclosure(
        "What is being built",
        rx.vstack(
            rx.hstack(
                rx.vstack(
                    caption("Source - the ROIs under this one"),
                    diagram(S.source_diagram),
                    spacing=tokens.GAP["row"], align_items="start", width="100%"),
                rx.vstack(
                    rx.hstack(
                        caption(S.building_heading),
                        rx.spacer(),
                        rx.el.div(
                            rx.foreach(
                                S.diagram_groupings,
                                lambda g: rx.el.button(
                                    g, on_click=lambda: S.choose_diagram_group(g),
                                    aria_pressed=rx.cond(S.diagram_group == g, "true", "false"),
                                    class_name="cx-segmented__item", type="button")),
                            class_name="cx-segmented"),
                        width="100%", align="center"),
                    diagram(S.object_diagram),
                    spacing=tokens.GAP["row"], align_items="start", width="100%"),
                spacing=tokens.GAP["section"], align_items="start", width="100%"),
            rx.el.div(rx.foreach(S.legend_states, legend_item), class_name="cx-legend"),
            rx.el.div(S.legend_edges, class_name="cx-legend"),
            notices(S.relation_caveats, "warn", label="caveat"),
            spacing=tokens.GAP["block"], align_items="start", width="100%"),
        open_=True)


# Element rows.
def declared_row(e: DeclaredRow) -> rx.Component:
    """One entry the recipe declares. An excluded row stays visible, struck through."""
    return rx.el.div(
        rx.el.div(
            rx.el.div(
                markup(e.mark),
                rx.el.span(e.element, class_name="cx-row__name"),
                class_name="cx-row__id"),
            caption(f"reads {e.level}"),
            rx.cond(e.coverage != "", markup(e.coverage)),
            rx.cond(e.unplaceable != "", notice(e.unplaceable, "warn", label="unplaceable")),
            class_name="cx-row__left"),
        rx.el.div(
            rx.el.select(
                rx.foreach(e.options, lambda o: rx.el.option(o, value=o)),
                value=e.chosen, on_change=lambda v: S.pin_role(e.element, v),
                class_name="cx-select cx-select--key", width="100%"),
            rx.cond(e.mixed != "", caption(e.mixed)),
            rx.cond(e.variable != "", caption(f"variable {e.variable}")),
            class_name="cx-row__mid"),
        rx.el.div(
            rx.el.label(
                rx.el.input(type="checkbox", checked=e.included,
                            on_change=lambda v: S.include_element(e.element, v)),
                rx.el.span("include"),
                class_name="cx-check"),
            rx.cond(
                e.included & e.required,
                rx.el.label(
                    rx.el.input(type="checkbox", checked=e.allow_absent,
                                on_change=lambda v: S.allow_absent(e.element, v)),
                    rx.el.span("allow absent"),
                    class_name="cx-check")),
            rx.cond(~e.required, caption("optional")),
            class_name="cx-row__right"),
        class_name=rx.cond(e.included, "cx-row", "cx-row cx-row--excluded"))


def added_row(a: AddedRow) -> rx.Component:
    """One entry the user is adding to this slot, labelled ``added``."""
    return rx.el.div(
        rx.el.div(
            rx.el.span("added", class_name="cx-row__added-label"),
            rx.el.select(
                rx.foreach(a.options, lambda o: rx.el.option(o, value=o)),
                value=a.chosen, on_change=lambda v: S.choose_added(a.slot_key, v),
                class_name="cx-select cx-select--key", width="100%"),
            rx.cond(a.empty, caption("nothing selected - this slot will be skipped")),
            class_name="cx-row__left"),
        rx.el.div(
            rx.cond(
                a.variable_fixed,
                caption(f"reads {a.fixed_variable}"),
                # Shown only where no element names a variable already.
                rx.el.input(value=a.variable, placeholder="variable",
                            on_change=lambda v: S.edit_added_variable(a.slot_key, v),
                            class_name="cx-input cx-input--key", width="100%")),
            class_name="cx-row__mid"),
        rx.el.div(
            rx.el.input(value=a.name, placeholder="slot name",
                        on_change=lambda v: S.edit_added_name(a.slot_key, v),
                        class_name="cx-input cx-input--key", width="100%"),
            button("✕", on_click=lambda: S.drop_row(a.kind, a.index), ghost=True,
                   title="remove"),
            class_name="cx-row__right"),
        class_name="cx-row cx-row--added")


def slot_section(s: Slot) -> rx.Component:
    return rx.el.section(
        rx.el.div(
            markup(s.glyph),
            rx.el.span(s.label),
            rx.el.span(s.count, class_name="cx-slot__count"),
            class_name="cx-slot__head"),
        caption(s.help),
        rx.foreach(s.declared, declared_row),
        rx.foreach(s.added, added_row),
        rx.cond(s.empty, caption("nothing here yet")),
        rx.el.button(f"+ add {s.kind}", on_click=lambda: S.add_row(s.kind),
                     disabled=s.add_disabled, title=s.add_help,
                     class_name="cx-add", type="button"),
        class_name="cx-slot")


def element_widgets() -> rx.Component:
    """The build page, arranged by the target model's slots. A `+` adds an entry."""
    return rx.vstack(
        rx.foreach(S.slots, slot_section),
        notice(S.name_clash, "blocked", label="name taken"),
        spacing=tokens.GAP["section"], align_items="start", width="100%")


# Plan.
def layout_panel() -> rx.Component:
    """The shape of the object the plan will produce. Measured only against a live instance."""
    return rx.vstack(
        rx.el.p(S.layout_heading, class_name="cx-lead"),
        table(S.layout_table),
        rx.cond(S.layout_overflow != "", caption(S.layout_overflow)),
        notices(S.layout_absences, "warn", label="absent"),
        captions(S.layout_notes),
        rx.cond(S.layout_reads != "", caption(S.layout_reads)),
        rx.cond(
            S.layout_ome_xml != "",
            disclosure("OME-XML - the model this file will carry",
                       code(S.layout_ome_xml, "markup"))),
        disclosure("The layout as a tree, and as JSON",
                   code(S.layout_tree, "yaml"),
                   code(S.layout_json, "json")),
        spacing=tokens.GAP["block"], align_items="start", width="100%")


def plan_panel() -> rx.Component:
    return rx.vstack(
        notice(S.plan_error, "blocked", label="cannot plan"),
        rx.cond(
            S.has_plan,
            rx.vstack(
                rx.cond(
                    S.plan_blocked,
                    notice(S.plan_headline, "blocked", label="blocked"),
                    notice(S.plan_headline, "ok", label="plannable")),
                layout_panel(),
                disclosure("How it resolved - the wiring", code(S.plan_wiring, "yaml")),
                notices(S.plan_caveats, "warn", label="caveat"),
                rx.el.hr(class_name="cx-rule"),
                build_panel(),
                spacing=tokens.GAP["block"], align_items="start", width="100%")),
        spacing=tokens.GAP["block"], align_items="start", width="100%")


def build_panel() -> rx.Component:
    """Start the build: submit to the cluster, or assemble in process."""
    return rx.vstack(
        rx.cond(
            S.build_disabled,
            rx.vstack(
                button("Assemble", disabled=True, title=S.build_disabled_note),
                caption(S.build_disabled_note),
                spacing=tokens.GAP["hair"], align_items="start"),
            rx.cond(
                S.can_submit,
                rx.vstack(
                    rx.cond(
                        S.sizing_failure != "",
                        notice(f"The submit target is unavailable, so this plan cannot be built "
                               f"from here: {S.sizing_failure}", "blocked",
                               label="no submit target")),
                    rx.cond(S.submit_note != "", caption(S.submit_note)),
                    button(f"Submit {S.units} unit(s) of {S.roi_name} to the cluster",
                           on_click=S.submit, disabled=S.sizing_failure != "", primary=True),
                    rx.cond(
                        S.sizing_failure != "",
                        caption("Sizing a job reads the deployment, and that read failed. The "
                                "plan above is unaffected - it needs no orchestrator - and "
                                "submitting the same plan once this is fixed builds exactly it.")),
                    spacing=tokens.GAP["row"], align_items="start", width="100%"),
                rx.vstack(
                    button(f"Assemble {S.units} unit(s) of {S.roi_name} here",
                           on_click=S.assemble, primary=True),
                    caption(S.in_process_note),
                    code(S.assembled, "json"),
                    spacing=tokens.GAP["row"], align_items="start", width="100%"))),
        notice(S.notice_error, "blocked", label="failed"),
        notice(S.notice, "ok", label="submitted"),
        spacing=tokens.GAP["block"], align_items="start", width="100%")


def build_tab() -> rx.Component:
    return rx.cond(
        S.nothing_buildable,
        notice(S.nothing_buildable_note, "warn", label="nothing to build"),
        rx.vstack(
            rx.hstack(
                rx.el.span("Product", class_name="cx-readout__label"),
                rx.el.select(
                    rx.foreach(S.product_labels, lambda o: rx.el.option(o, value=o)),
                    value=S.product_label, on_change=S.choose_product,
                    class_name="cx-select cx-select--key", width="100%"),
                spacing=tokens.GAP["row"], align="center", width="100%"),
            blueprint(),
            element_widgets(),
            rx.hstack(
                rx.vstack(
                    rx.el.div("If something is missing", class_name="cx-readout__label"),
                    rx.el.div(
                        rx.foreach(
                            STRICTNESS,
                            lambda s: rx.el.button(
                                s, on_click=lambda: S.choose_strictness(s),
                                aria_pressed=rx.cond(S.strictness == s, "true", "false"),
                                class_name="cx-segmented__item", type="button")),
                        class_name="cx-segmented"),
                    rx.vstack(*[caption(f"{k}: {v}") for k, v in STRICTNESS_HELP.items()],
                              spacing=tokens.GAP["hair"], align_items="start"),
                    rx.cond(
                        S.offers_downsample,
                        rx.vstack(
                            rx.el.div(
                                rx.cond(S.downsamples_a_plate,
                                        f"Plate resolution - {S.downsample_label}",
                                        f"Mosaic resolution - {S.downsample_label}"),
                                class_name="cx-readout__label"),
                            rx.slider(value=[S.downsample_index], min=0,
                                      max=len(DOWNSAMPLE) - 1, step=1,
                                      on_change=S.choose_downsample, width="100%"),
                            rx.cond(
                                S.downsamples_a_plate,
                                caption("A plate stores every core as its own image group, so "
                                        "it costs the sum of the cores and nothing is refused - "
                                        "full resolution is what it is for. A lower setting "
                                        "SAMPLES every nth pixel rather than averaging."),
                                caption("A merged OME-TIFF places every core's pixels in one "
                                        "frame, and the gaps between them are written as ABSENT "
                                        "tiles that cost the file nothing - so full resolution "
                                        "builds, and the size is the cores rather than the "
                                        "rectangle around them. It is still a large file. A "
                                        "lower setting SAMPLES every nth pixel rather than "
                                        "averaging, and is what an overview is for.")),
                            spacing=tokens.GAP["hair"], align_items="start", width="100%")),
                    spacing=tokens.GAP["row"], align_items="start", width="100%"),
                rx.vstack(
                    rx.hstack(
                        rx.switch(checked=S.register, on_change=S.toggle_register),
                        rx.el.span("Register outputs as Artifacts"),
                        spacing=tokens.GAP["row"], align="center"),
                    caption("Off writes local files. On registers ROI-anchored Artifacts and "
                            "needs permits_derive on the output folder."),
                    rx.el.div(S.dest_label, class_name="cx-readout__label"),
                    rx.el.input(value=S.dest, on_change=S.edit_dest,
                                class_name="cx-input cx-input--key", width="100%"),
                    spacing=tokens.GAP["row"], align_items="start", width="100%"),
                spacing=tokens.GAP["section"], align_items="start", width="100%"),
            button("Plan", on_click=S.make_plan, primary=True),
            plan_panel(),
            spacing=tokens.GAP["section"], align_items="start", width="100%"))


# Jobs.
def run_group_card(g: RunGroup) -> rx.Component:
    return disclosure(
        rx.fragment(
            rx.el.code(g.heading, class_name="cx-key"),
            rx.cond(g.mine, rx.el.span("this session", class_name="cx-row__added-label"))),
        rx.vstack(table(g.runs), caption(g.artifact),
                  spacing=tokens.GAP["row"], align_items="start", width="100%"))


def jobs_panel() -> rx.Component:
    """Submitted runs, read from the orchestrator."""
    return rx.vstack(
        rx.hstack(
            rx.el.h3("Submitted builds", class_name="cx-panel__title"),
            rx.spacer(),
            button("↻ Refresh runs", on_click=S.refresh_runs),
            width="100%", align="center"),
        notice(S.jobs_error, "warn", label="unreachable"),
        rx.cond(S.runs_read_at != "", caption(f"run states as of {S.runs_read_at}")),
        rx.cond(
            S.no_runs_note != "",
            caption(S.no_runs_note),
            rx.vstack(rx.foreach(S.run_groups, run_group_card),
                      spacing=tokens.GAP["row"], width="100%")),
        spacing=tokens.GAP["block"], align_items="start", width="100%")


# The page.
def tab(label: str, value: str) -> rx.Component:
    return rx.tabs.trigger(label, value=value, class_name="cx-tab")


def body() -> rx.Component:
    """The Explorer below whatever chrome the host put above it."""
    return rx.hstack(
        sidebar(),
        rx.vstack(
            rx.el.h2(S.roi_heading, class_name="cx-section-head"),
            rx.hstack(
                readout("Subtree", S.subtree),
                rx.el.hr(class_name="cx-rule cx-rule--vertical"),
                readout("Recipes", S.recipes_buildable),
                spacing=tokens.GAP["wide"], align="start"),
            rx.tabs.root(
                rx.tabs.list(
                    tab("Corpus", "corpus"),
                    tab("What is here", "here"),
                    tab("Build something", "build"),
                    tab("Jobs", "jobs"),
                    class_name="cx-tabs"),
                rx.tabs.content(corpus_panel(), value="corpus", class_name="cx-tabpanel"),
                rx.tabs.content(here_panel(), value="here", class_name="cx-tabpanel"),
                rx.tabs.content(build_tab(), value="build", class_name="cx-tabpanel"),
                rx.tabs.content(jobs_panel(), value="jobs", class_name="cx-tabpanel"),
                default_value="build", width="100%"),
            spacing=tokens.GAP["section"], align_items="start", width="100%"),
        spacing=tokens.GAP["section"], align_items="start", width="100%")
