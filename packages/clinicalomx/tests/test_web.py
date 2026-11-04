"""The Extract Explorer, driven headless through `ExplorerState` and a page compile.

Demo mode must reach no instance. Skipped where reflex (the `app` extra) is not installed.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("reflex", reason="the Extract Explorer needs clinicalomx[app]")

from clinicalomx.extract.app import demo  # noqa: E402
from clinicalomx.extract.app.web import cache, state, tokens, view  # noqa: E402
from clinicalomx.extract.app.web.design import states  # noqa: E402
from clinicalomx.extract.app.web.state import ExplorerState  # noqa: E402

RICHEST = "spatialdata.tma.by-core"


def _uid(name: str, roi_type: str) -> str:
    """The uid behind a fixture ROI, which is what the picker commits."""
    return next(r.uid for r in demo.rois(cache.demo_snapshot())
                if r.name == name and r.type.name == roi_type)


TMA_UID = _uid(demo.TMA_NAME, "TMA")
SLIDE_UID = _uid(demo.SLIDE_NAME, "WholeSlide")


@pytest.fixture(autouse=True)
def _pristine():
    """Reset `FORCED_LIVE` and the orchestrator, which are process state."""
    from clinicalomx.extract import submit as hop

    before = state.FORCED_LIVE
    yield
    state.FORCED_LIVE = before
    hop.register_submitter(None)
    hop.register_sizer(None)
    hop.register_run_reader(None)


@pytest.fixture
def fresh_app():
    """Build an `rx.App` in a forked registration context, so a test can build more than one."""
    from reflex_base.registry import RegistrationContext

    from clinicalomx.extract.app.web import make_app

    made = []

    def build(**kw):
        ctx = RegistrationContext.ensure_context()
        token = RegistrationContext._context_var.set(ctx.fork())
        made.append(token)
        return make_app(**kw)

    yield build
    for token in reversed(made):
        RegistrationContext._context_var.reset(token)


@pytest.fixture
def app():
    s = ExplorerState()
    s.on_load()
    return s


@pytest.fixture
def tma(app):
    """A TMA with the richest product selected by name."""
    app.choose_roi(TMA_UID)
    return _pick(app, RICHEST)


def _pick(s, recipe: str):
    """Select a product by name through the widget's handler. Matches whole delimited names."""
    s.choose_product(next(p for p in s.product_labels if f" {recipe} ·" in p))
    return s


def _slot(s, kind: str):
    return next(x for x in s.slots if x.kind == kind)


def _row(s, element: str):
    return next(r for x in s.slots for r in x.declared if r.element == element)


def _readiness(s):
    return s._readiness


def _add_options(s, kind: str) -> list[str]:
    """The options the + offers for one slot, opening a row if none is open yet."""
    if not _slot(s, kind).added:
        s.add_row(kind)
    return list(_slot(s, kind).added[0].options)


def _add(s, kind: str, match: str, *, name: str | None = None) -> str:
    """Add the first candidate whose label carries `match`. Returns that label."""
    option = next(o for o in _add_options(s, kind) if match in o)
    key = _slot(s, kind).added[0].slot_key
    s.choose_added(key, option)
    if name is not None:
        s.edit_added_name(key, name)
    return option


def _tables(s) -> list:
    """Every table the page draws for the selected ROI, whichever tab it is on."""
    return [s.element_table, s.role_table, s.corpus_coverage, s.corpus_recipes, s.layout_table]


def _layout_pieces(s) -> list[dict]:
    """The published layout: ``TargetLayout.to_dict()`` pieces."""
    return json.loads(s.layout_json)["pieces"]


# It builds at all.
def test_the_whole_page_evaluates(fresh_app):
    app = fresh_app(live=False, title="Extract Explorer")
    composed = str(next(iter(app._unevaluated_pages.values())).component().render())
    assert "Extract Explorer" in composed
    assert "roi_choices" in composed


def test_the_demo_reaches_no_instance(tma, monkeypatch):
    import builtins

    real = builtins.__import__

    def _no_lamindb(name, *args, **kw):
        if name == "lamindb" or name.startswith("lamindb."):
            raise AssertionError("the demo reached for lamindb")
        return real(name, *args, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_lamindb)
    tma.make_plan()
    assert tma.plan_error == ""
    assert tma.has_plan


# The widgets: one per element.
def test_the_page_is_arranged_by_the_target_models_slots(tma):
    labels = {s.label for s in tma.slots}
    assert labels >= {"Images", "Labels", "Shapes", "Tables", "Points"}


def test_an_empty_slot_is_shown_rather_than_hidden(tma):
    points = _slot(tma, "points")
    assert points.empty
    assert not points.add_disabled


def test_every_slot_has_its_own_add(tma):
    kinds = {s.kind for s in tma.slots if not s.add_disabled}
    assert kinds >= {"image", "labels", "shapes", "table"}


def test_there_is_one_role_widget_per_element(tma):
    rows = [r for s in tma.slots for r in s.declared]
    assert len(rows) == len(_readiness(tma).elements)


def test_a_role_widget_offers_auto_plus_every_candidate(tma):
    options = _row(tma, "cores").options
    assert options[0] == state.AUTO
    assert any("cores_accepted" in o for o in options)
    assert any("core_shapes" in o for o in options)


def test_the_images_slot_holds_several_entries_at_once(tma):
    images = {r.element for r in _slot(tma, "image").declared}
    assert images >= {"raw", "bgnorm", "bgnormQ"}


def test_two_entries_reading_one_role_are_distinguished_by_their_variable(tma):
    assert _row(tma, "bgnorm").variable == "adjusted_image"
    assert _row(tma, "bgnormQ").variable == "adjusted_image_post_hoc"


def test_the_add_offers_what_is_on_the_roi_and_says_when_a_kind_is_unknown(tma):
    options = _add_options(tma, "image")
    assert options[0] == state.NOTHING, "nothing is selected until somebody selects it"
    assert any("kind unknown" in o for o in options)


def test_adding_an_entry_puts_it_in_the_plan(tma):
    if not any("roi_thumbnail" in o for o in _add_options(tma, "image")):
        pytest.skip("the TMA carries no unclaimed image-ish artifact in this fixture")
    _add(tma, "image", "roi_thumbnail")
    tma.make_plan()
    assert "roi_thumbnail" in cache.plan(tma.plan_token).buildable[0].elements


def test_a_choice_names_the_array_and_not_only_the_store(tma):
    assert any("bgnormed · adjusted_image " in o for o in _row(tma, "bgnorm").options)
    assert any("bgnormed · adjusted_image_post_hoc" in o
               for o in _row(tma, "bgnormQ").options)


def test_the_add_offers_a_sub_image_of_a_store_and_not_only_the_store(tma):
    tma.include_element("bgnormQ", False)
    options = _add_options(tma, "image")
    assert any("adjusted_image_post_hoc" in o for o in options), options
    assert not any(o.endswith("adjusted_image usable") for o in options), \
        "bgnorm is still in the slot, so its array is not on offer"


def test_two_elements_over_one_store_can_both_be_chosen(tma):
    tma.include_element("bgnormQ", False)
    _add(tma, "image", "adjusted_image_post_hoc", name="bgnormQ_again")
    tma.make_plan()

    unit = cache.plan(tma.plan_token).buildable[0]
    shared = [c for c, els in unit.child_elements.items()
              if els.get("bgnorm") == els.get("bgnormQ_again") == "bgnormed"]
    assert shared, f"no core resolved both entries onto one store: {unit.child_elements}"

    def _by_child(element: str) -> dict[str, dict]:
        return {p["key"][:-len(element)]: p
                for p in _layout_pieces(tma) if p["element"] == element}

    mine, added = _by_child("bgnorm"), _by_child("bgnormQ_again")
    together = sorted(set(mine) & set(added))
    assert len(together) == len(shared), (sorted(mine), sorted(added))
    for child in together:
        assert mine[child]["role"] == added[child]["role"] == "bgnormed"
        assert mine[child]["key"] != added[child]["key"], "one slot, not two"


def test_the_widgets_and_the_diagram_agree_about_an_added_entry(tma):
    from clinicalomx.extract.app.diagram import STATE_COLOUR

    tma.include_element("bgnormQ", False)
    option = _add(tma, "image", "adjusted_image_post_hoc", name="bgnormQ_again")
    drawn = next(line for line in tma.object_diagram.source.splitlines()
                 if line.strip().startswith('"bgnormQ_again" [label'))
    assert "bgnormed#adjusted_image_post_hoc" in drawn

    reported = option.split(" ", 1)[0]
    assert reported in states.STATES, f"the option does not lead with a state: {option}"
    assert STATE_COLOUR[reported][1] in drawn, \
        f"the row showed {reported} and the picture did not"


def test_an_added_entry_is_drawn_with_the_fan_out_it_actually_has(tma):
    tma.include_element("bgnormQ", False)
    _add(tma, "image", "adjusted_image_post_hoc", name="bgnormQ_again")
    assert "src_R0C0_bgnormQ_again" in tma.object_diagram.source


def test_an_added_name_that_is_already_a_slot_is_refused_before_planning(tma):
    tma.include_element("bgnormQ", False)
    _add(tma, "image", "adjusted_image_post_hoc")  # default name: bgnormQ
    assert "already a slot" in tma.name_clash


def test_a_role_widget_shows_how_many_targets_each_role_fills(tma):
    assert any("fills 4" in o for r in [_row(tma, "bgnorm")] for o in r.options)


def test_an_unavailable_role_is_shown_with_its_state_rather_than_hidden(tma):
    qc = next(r for s in tma.slots for r in s.declared
              if any("bgnorm_qc" in o for o in r.options))
    assert any("unlinked" in o for o in qc.options)


def test_undeclared_now_means_nothing_anywhere_declares_it(app):
    app.choose_roi(SLIDE_UID)
    shapes = next(r for s in app.slots for r in s.declared
                  if any("tma_shapes" in o for o in r.options))
    assert any("undeclared" in o for o in shapes.options)


# The choice changes the plan.
def test_pinning_a_role_narrows_rather_than_falling_back(tma):
    row = _row(tma, "cores")
    tma.pin_role("cores", next(o for o in row.options if "core_shapes" in o))
    tma.make_plan()
    assert "BLOCKED: cores" in tma.plan_wiring


def test_both_image_variants_reach_the_plan(tma):
    tma.make_plan()
    assert "raw<-source_image" in tma.plan_wiring
    assert "bgnorm<-bgnormed" in tma.plan_wiring


def test_leaving_it_on_auto_takes_the_preferred_role(tma):
    tma.make_plan()
    assert "cores<-cores_accepted" in tma.plan_wiring


def test_a_plan_leads_with_the_object_it_will_produce(tma):
    composed = str(view.plan_panel().render())
    assert composed.index("layout_heading") < composed.index("plan_wiring")


def test_the_layout_places_each_element_in_the_object(tma):
    tma.make_plan()
    assert "Core_R0C0_bgnorm" in tma.layout_tree


def test_an_absence_reaches_the_page_as_an_effect_on_the_output(tma):
    tma.make_plan()
    assert any("in the object" in a for a in tma.layout_absences)


def test_the_demo_says_its_dimensions_were_not_read(tma):
    tma.make_plan()
    assert any("not read" in n for n in tma.layout_notes)


def test_a_plan_reports_a_token(tma):
    tma.make_plan()
    assert "token" in tma.plan_headline


def test_changing_a_choice_retires_the_plan(tma):
    tma.make_plan()
    assert tma.has_plan
    tma.include_element("bgnorm", False)
    assert not tma.has_plan


# Honesty carries into the UI.
def test_missing_is_a_dash_not_a_zero_when_bytes_were_not_probed(tma):
    tma.make_plan()
    checked = 0
    for table in _tables(tma):
        if "missing" not in table.columns:
            continue
        column = table.columns.index("missing")
        cells = {row[column] for row in table.rows}
        assert all("cx-notprobed" in c for c in cells), cells
        assert all(f">{tokens.MISSING_NOT_PROBED}<" in c for c in cells), cells
        assert not any("cx-chip" in c or ">0<" in c for c in cells), cells
        checked += 1
    assert checked >= 2, "no coverage table rendered"


def test_the_page_offers_no_way_to_probe_bytes(tma):
    names = set(ExplorerState.get_fields()) | set(ExplorerState.inherited_vars)
    assert not [n for n in names if "probe" in n or "bytes" in n]
    assert "Probe" not in str(view.body().render())


def test_the_header_claims_no_verification(tma):
    composed = str(view.body().render())
    assert "Subtree" in composed and "Recipes" in composed
    assert "Bytes" not in composed


def test_a_tma_reads_correctly_when_storage_cannot_be_reached(monkeypatch):
    from clinicalomx.extract import source as source_module

    def _unreachable():
        msg = "this host cannot reach the artifact storage"
        raise OSError(msg)

    monkeypatch.setattr(source_module, "default_source", _unreachable)
    cache.clear_all()
    s = ExplorerState()
    s.on_load()
    s.choose_roi(TMA_UID)

    assert not s.recipes_buildable.startswith("0/")
    usable = s.role_table.columns.index("usable")
    assert any(">0/" not in row[usable] for row in s.role_table.rows)
    missing = s.role_table.columns.index("missing")
    assert all("cx-notprobed" in row[missing] for row in s.role_table.rows)


def test_the_caveats_are_rendered_not_dropped(tma):
    assert any("bytes were NOT verified" in c for c in tma.caveats)


def test_assemble_is_disabled_on_demo_data(tma):
    tma.make_plan()
    assert tma.build_disabled
    assert "no bytes to read" in tma.build_disabled_note


# The fixture itself.
def test_the_default_product_is_the_richest_one_that_is_ready(app):
    app.choose_roi(TMA_UID)
    assert f" {RICHEST} ·" in app.product_label


def test_a_tma_can_be_asked_for_an_ome_tiff(app):
    app.choose_roi(TMA_UID)
    offered = " ".join(app.product_labels)
    for recipe in ("ometiff.tma.mosaic", "ometiff.tma.per-core"):
        assert f" {recipe} ·" in offered, recipe


def test_a_tma_ome_tiff_that_cannot_be_built_is_blocked_not_absent(app):
    app.choose_roi(TMA_UID)
    assert "ometiff.tma" in {r.recipe for r in app.recipe_rows}, \
        "a blocked recipe was hidden instead of explained"
    assert " ometiff.tma ·" not in " ".join(app.product_labels), \
        "a blocked recipe must not be offered as buildable"


def test_every_merged_tma_product_offers_a_resolution_and_nothing_else_does(app):
    app.choose_roi(TMA_UID)
    for recipe in ("ometiff.tma.mosaic", "omezarr.tma.plate"):
        _pick(app, recipe)
        assert app.offers_downsample, recipe

    _pick(app, RICHEST)
    assert not app.offers_downsample


def _planned_downsample(s) -> int:
    s.make_plan()
    assert s.plan_error == "", s.plan_error
    return cache.plan(s.plan_token).request["downsample"]


def test_every_merged_product_plans_at_full_resolution(app):
    app.choose_roi(TMA_UID)
    for recipe in ("omezarr.tma.plate", "ometiff.tma.mosaic"):
        assert _planned_downsample(_pick(app, recipe)) == 1, recipe
        assert app.downsample_label == "full", recipe

    app.choose_downsample([float(state.DOWNSAMPLE.index(4))])
    assert _planned_downsample(_pick(app, "omezarr.tma.plate")) == 1, (
        "the plate inherited a choice made about the mosaic")


def _components(c):
    """Every component in a built page, through `rx.cond`'s two branches as well as `children`."""
    yield c
    for kid in getattr(c, "children", None) or []:
        yield from _components(kid)
    for branch in ("comp1", "comp2"):
        sub = getattr(c, branch, None)
        if sub is not None:
            yield from _components(sub)


def test_the_resolution_slider_is_wired_to_what_it_submits(app, fresh_app):
    page = fresh_app(live=False, title="Extract Explorer")
    built = next(iter(page._unevaluated_pages.values())).component()
    sliders = [c for c in _components(built) if type(c).__name__ == "Slider"]
    assert len(sliders) == 1, f"expected one resolution slider, found {len(sliders)}"

    chain = sliders[0].event_triggers.get("on_change")
    assert chain is not None, "the slider is controlled by a value nothing moves"
    drag = chain.events[0].handler.fn

    app.choose_roi(TMA_UID)
    _pick(app, "omezarr.tma.plate")
    drag(app, [float(state.DOWNSAMPLE.index(4))])
    assert state.DOWNSAMPLE[app.downsample_index] == 4, "the thumb did not follow the drag"
    assert _planned_downsample(app) == 4, "the drag did not reach the request"


def test_the_slider_shows_the_value_it_will_submit(app):
    app.choose_roi(TMA_UID)
    for recipe in ("ometiff.tma.mosaic", "omezarr.tma.plate", "ometiff.tma.mosaic"):
        _pick(app, recipe)
        assert state.DOWNSAMPLE[app.downsample_index] == _planned_downsample(app), recipe


def test_moving_the_slider_beats_the_target_default(app):
    app.choose_roi(TMA_UID)
    _pick(app, "omezarr.tma.plate")
    app.choose_downsample([float(state.DOWNSAMPLE.index(4))])
    assert app.downsample_label == "1/4"
    assert _planned_downsample(app) == 4


def test_the_demo_reaches_every_state_worth_seeing():
    from clinicalomx.extract.availability import describe

    demo.install_governance()
    snap = demo.build_snapshot()
    tma = snap.record_by_name("TMA", demo.TMA_NAME)
    av = describe(tma, snapshot=snap, now="2026-01-01T00:00:00Z")
    slide_av = describe(snap.record_by_name("WholeSlide", demo.SLIDE_NAME),
                        snapshot=snap, now="2026-01-01T00:00:00Z")

    everywhere = {e.state for a in (av, slide_av) for r in a.recipes for e in r.elements}
    counted = {k for c in av.child_coverage for k, v in
               (("dangling", c.dangling), ("denied", c.denied), ("unlinked", c.unlinked)) if v}
    assert "undeclared" in everywhere, "no undeclared role: the most instructive case is missing"
    assert {"dangling", "denied", "unlinked"} <= counted

    images = [e for r in av.recipes for e in r.elements if e.kind == "image"]
    assert any(e.unplaceable for e in images), "no unplaceable core"
    satisfied = {e.element for e in images if e.satisfied}
    assert {"raw", "bgnorm"} <= satisfied, "both variants must be reachable side by side"


# The blueprint.
def test_the_page_shows_both_panels(tma):
    assert tma.source_diagram.source.startswith("digraph")
    assert tma.object_diagram.source.startswith("digraph")


def test_the_legend_teaches_the_same_marks_the_tables_use(tma):
    marks = [item.mark for item in tma.legend_states]
    assert len(marks) == len(states.LADDER), "the legend is not the whole ladder"
    assert [i.sentence for i in tma.legend_states][0] == states.STATES["undeclared"].legend, \
        "the legend does not run worst-first, so it teaches no ladder"
    assert all("cx-chip" in m for m in marks), "the legend draws different marks from the tables"
    assert "derived from" in tma.legend_edges, "the edge key was dropped with the emoji"
    assert "\U0001f7e9" not in tma.legend_edges, "an emoji state survived into the legend"


def test_a_missing_derivation_reaches_the_page_as_a_caveat(tma):
    joined = " ".join(tma.relation_caveats)
    assert "NOT guessed" in joined or "coin flip" in joined


def test_removing_an_element_removes_it_from_the_picture(tma):
    import re

    def drawn():
        return set(re.findall(r'^\s*"(\w+)" \[label', tma.object_diagram.source, re.M))

    assert "bgnorm" in drawn()
    tma.include_element("bgnorm", False)
    assert "bgnorm" not in drawn()
    assert "bgnormQ" in drawn(), "removing one variant took the other with it"
    assert "1 removed" in tma.building_heading


def test_removing_a_variant_can_resolve_an_ambiguous_derivation(tma):
    assert any("coin flip" in c for c in tma.relation_caveats), \
        "the ambiguity is not being reported to start with"
    tma.include_element("bgnorm", False)
    assert not any("coin flip" in c for c in tma.relation_caveats)
    assert '"bgnormQ" -> "labels"' in tma.object_diagram.source


def test_the_sources_can_be_grouped_two_ways(tma):
    assert tma.diagram_group == "roi", "the ROI-shaped reading is the default"
    assert "cluster_roi_R0C0" in tma.object_diagram.source

    tma.choose_diagram_group("element")
    assert "cluster_roi_" not in tma.object_diagram.source
    assert "R0C0: source_image" in tma.object_diagram.source


def test_a_diagram_without_graphviz_shows_its_source_and_says_why(tma, monkeypatch):
    from clinicalomx.extract.app.web import dot

    monkeypatch.setattr(dot, "available", lambda: False)
    dot._CACHE.clear()
    drawn = state._draw("digraph G { a -> b }")
    assert drawn.svg == ""
    assert "Graphviz is not installed" in drawn.error
    assert drawn.source == "digraph G { a -> b }"


def test_a_rendered_diagram_is_inlined_so_it_scales():
    from clinicalomx.extract.app.web import dot

    svg = dot._inline(
        '<?xml version="1.0"?>\n<!DOCTYPE svg PUBLIC "x" "y">\n'
        '<svg width="120pt" height="40pt" viewBox="0 0 120 40"><g/></svg>')
    assert svg.startswith("<svg ")
    assert 'width="100%"' in svg
    assert 'height="40pt"' not in svg
    assert 'viewBox="0 0 120 40"' in svg


# Caching.
def test_widget_interaction_does_not_re_read_the_instance(monkeypatch):
    import clinicalomx.extract.availability as availability
    import clinicalomx.extract.corpus as corpus

    cache.clear_all()
    calls = {"describe": 0, "survey": 0}
    for module, name in ((availability, "describe"), (corpus, "survey")):
        real = getattr(module, name)

        def counted(*a, _real=real, _name=name, **k):
            calls[_name] += 1
            return _real(*a, **k)

        monkeypatch.setattr(module, name, counted)

    s = ExplorerState()
    s.on_load()
    s.choose_roi(TMA_UID)
    _ = s.slots, s.element_table, s.role_table, s.corpus_recipes, s.recipe_rows
    settled = dict(calls)

    for i in range(5):
        s.include_element("bgnorm", i % 2 == 0)
        _ = s.slots, s.element_table, s.role_table, s.corpus_recipes, s.recipe_rows

    assert calls == settled, (
        f"five clicks cost {calls['describe'] - settled['describe']} extra describe(s) and "
        f"{calls['survey'] - settled['survey']} extra survey(s)")


def test_refresh_invalidates_every_cache_at_once(tma):
    before = tma.generation
    tma.refresh()
    assert tma.generation == before + 1
    assert cache.describe.cache_info().currsize == 0


def test_the_page_says_how_old_its_cached_view_is(tma):
    assert tma.index_built_at
    assert "index built" in str(view.sidebar().render())


# The embedding seam: a host app gets this screen.
def test_a_host_that_names_its_source_is_offered_no_toggle(fresh_app):
    fresh_app(live=True, title="Build")
    assert state.FORCED_LIVE is True
    assert not ExplorerState().show_source_toggle


def test_the_standalone_app_still_defaults_to_the_demo(fresh_app):
    fresh_app(live=None)
    s = ExplorerState()
    s.on_load()
    assert s.show_source_toggle
    assert s.live is False


def test_a_host_gets_the_whole_explorer_under_its_own_title(fresh_app):
    app = fresh_app(live=True, title="Build", subtitle="Builds run on the HPC")
    composed = str(next(iter(app._unevaluated_pages.values())).component().render())
    assert "Build" in composed and "Builds run on the HPC" in composed
    assert "product_labels" in composed, "the build panel did not draw"
    assert "roi_choices" in composed, "the Explorer's sidebar did not draw"


def test_a_hosts_api_is_served_by_the_apps_own_backend(fresh_app, monkeypatch):
    from reflex_base.environment import environment
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    api = Starlette(routes=[Route("/api/v1/hello", lambda request: JSONResponse({"ok": 1}))])
    app = fresh_app(live=False, api=api)
    environment.REFLEX_SKIP_COMPILE.set(True)
    try:
        client = TestClient(app())
    finally:
        environment.REFLEX_SKIP_COMPILE.set(False)
    assert client.get("/api/v1/hello").json() == {"ok": 1}
    assert client.get("/ping").status_code == 200


# What is here: elements, then stores, then what is inside one.
def _column(table, name: str) -> list[str]:
    i = table.columns.index(name)
    return [row[i] for row in table.rows]


def test_what_is_here_reports_elements_not_only_roles(tma):
    table = tma.element_table
    assert {"bgnorm", "bgnormQ", "raw"} <= set(_column(table, "element"))
    by_element = dict(zip(_column(table, "element"), _column(table, "variable"), strict=True))
    assert by_element["bgnorm"] == "adjusted_image"
    assert by_element["bgnormQ"] == "adjusted_image_post_hoc"


def test_two_elements_over_one_store_are_shown_as_related(tma):
    table = tma.element_table
    shares = dict(zip(_column(table, "element"),
                      _column(table, "shares the store with"), strict=True))
    assert shares["bgnorm"] == "bgnormQ" and shares["bgnormQ"] == "bgnorm"
    assert shares["raw"] == "-"


def test_the_store_count_does_not_double_count_a_shared_zarr(tma):
    assert "6 element(s) over 9 distinct store(s)" in tma.element_heading


def test_the_role_table_is_kept_underneath_rather_than_replaced(tma):
    assert demo.UNCLAIMED_ROLE in _column(tma.role_table, "role")


def test_looking_inside_a_store_is_refused_on_demo_data(tma):
    assert tma.stores_available
    assert not tma.stores_opened
    assert not tma.live, "the button is disabled off `live`, so this is the condition under test"


def test_the_store_panel_says_it_reads_no_pixel():
    assert "No pixel is read" in str(view.stores_panel().render())


# The ROI picker: identity, reach and narrowing.
#: A stand-in instance for the picker tests: larger than the cap, with a duplicate label.
def _crowd(n: int = 500, *, duplicate: bool = False) -> list:
    rois = [demo._Rec("uSLIDE", demo.SLIDE_NAME, "WholeSlide"),
            demo._Rec("uTMA0", demo.TMA_NAME, "TMA")]
    rois += [demo._Rec(f"uC{i:04d}", f"{demo.TMA_NAME}:R0C{i}", "Core") for i in range(n)]
    if duplicate:
        # A second slide's core with the same id.
        rois.append(demo._Rec("uTWIN", f"{demo.TMA_NAME}:R0C0", "Core"))
    return rois


@pytest.fixture
def crowded(monkeypatch):
    """An ExplorerState over ~500 ROIs."""
    def _options(*, live, generation):
        return _crowd()

    monkeypatch.setattr(cache, "roi_options", _options)
    s = ExplorerState()
    s.on_load()
    return s


def test_a_pick_commits_an_identity_and_not_a_formatted_string(monkeypatch):
    monkeypatch.setattr(cache, "roi_options",
                        lambda *, live, generation: _crowd(4, duplicate=True))
    s = ExplorerState()
    s.on_load()
    twins = [c for c in s.roi_choices if c.label.endswith(":R0C0")]
    assert len(twins) == 2, "the fixture must carry the collision this test is about"

    s.choose_roi(twins[1].uid)
    assert s.roi_uid == twins[1].uid, "picking the second of two same-named ROIs selected the first"


def test_choosing_an_roi_needs_no_lookup(crowded):
    crowded.choose_roi("uC0499")
    assert crowded.roi_uid == "uC0499"


def test_every_roi_is_reachable_although_the_list_is_bounded(crowded):
    def listed(s) -> bool:
        return any(c.label.endswith(s) for c in crowded.roi_choices)

    assert len(crowded.roi_choices) <= state.ROI_CHOICES_SHOWN
    # The last core of 500 is past the cap of 200 until it is searched for.
    assert not listed(":R0C499"), "an ROI past the cap was drawn, so the bound did nothing"
    assert str(len(_crowd())) in crowded.roi_choices_note, "the cap does not report the total"

    crowded.search_roi("R0C499")
    assert listed(":R0C499"), "a named ROI stayed out of reach"


def test_the_type_filter_offers_the_instances_own_subtypes(crowded):
    assert crowded.roi_types[0] == state.ANY_TYPE
    assert set(crowded.roi_types[1:]) == {"Core", "TMA", "WholeSlide"}

    crowded.choose_roi_type("TMA")
    assert [c.label for c in crowded.roi_choices if c.uid == "uTMA0"], "the TMA was filtered out"
    assert not [c for c in crowded.roi_choices if c.uid.startswith("uC")]


def test_narrowing_the_picker_does_not_change_the_selection(crowded):
    crowded.choose_roi("uC0100")
    crowded.pins = {"raw": "source_image"}

    crowded.choose_roi_type("Core")
    crowded.search_roi("R0C2")
    assert crowded.roi_uid == "uC0100"
    assert crowded.pins == {"raw": "source_image"}, "narrowing the picker retired the staging"


def test_the_selected_roi_is_listed_even_when_the_filter_excludes_it(crowded):
    crowded.choose_roi("uC0100")
    crowded.search_roi("R0C499")
    assert any(c.uid == "uC0100" for c in crowded.roi_choices)


def _bound_to(root, handler: str):
    """The one control in `root` whose `on_change` is the named handler."""
    found = [c for c in _components(root)
             if (t := getattr(c, "event_triggers", {}).get("on_change")) is not None
             and t.events[0].handler.fn.__name__ == handler]
    assert len(found) == 1, f"expected one control bound to {handler}, found {len(found)}"
    return found[0]


def test_the_roi_select_holds_the_pick_before_the_server_does():
    picker = _bound_to(view.sidebar(), "choose_roi")
    assert type(picker).__name__ == "DebounceInput", \
        "the ROI select is controlled straight off the server var, so a pick will snap back"
    assert str(picker.debounce_timeout) == "0", \
        "a select fires once per choice; nothing to coalesce"
    assert str(picker.value).endswith(".roi_uid_rx_state_"), \
        "the wrapper is on some other control than the one the pick binds to"


def test_the_picker_offers_the_uid_and_never_the_label_as_a_value():
    options = [c for c in _components(_bound_to(view.sidebar(), "choose_roi"))
               if type(c).__name__ == "Option"]
    assert len(options) == 1, "the ROI select no longer lists its choices through one option"
    assert str(options[0].value).endswith('?.["uid"]'), "the ROI select still posts a label"
    assert '?.["label"]' in str(options[0].children[0].contents), "the option lost its label"


def test_the_note_owns_up_to_a_selection_the_cap_would_have_dropped(crowded):
    crowded.search_roi("R0C499")
    crowded.choose_roi(next(c.uid for c in crowded.roi_choices if c.label.endswith(":R0C499")))
    crowded.search_roi("")

    assert any(c.label.endswith(":R0C499") for c in crowded.roi_choices)
    assert "the selected ROI is listed as well" in crowded.roi_choices_note


def test_the_note_counts_matches_against_the_whole_instance(crowded):
    crowded.search_roi("R0C49")
    note = crowded.roi_choices_note
    assert "11" in note and str(len(_crowd())) in note


# The hop.
def test_the_jobs_tab_draws_with_no_orchestrator(app):
    from clinicalomx.extract import submit as hop

    assert hop.can_submit() is False
    assert "no orchestrator registered" in app.no_runs_note
    assert app.run_groups == []
    assert app.jobs_error == ""


def test_a_dev_box_is_offered_an_in_process_build_and_the_control_plane_is_not(app):
    from clinicalomx.extract import submit as hop

    assert app.can_submit is False
    assert "runs HERE" in app.in_process_note
    hop.register_submitter(lambda chunk: None)
    assert app.can_submit is True
    assert app.in_process_note == ""


@pytest.fixture
def degraded(tma):
    """A Build page with a plan, and an orchestrator that fails as an unreachable Prefect does."""
    from clinicalomx.extract import submit as hop

    tma.make_plan()
    tma.live = True
    hop.register_submitter(lambda chunk: None)
    return tma


def test_an_unreachable_orchestrator_costs_the_button_and_not_the_page(degraded):
    from clinicalomx.extract import submit as hop

    hop.register_sizer(lambda: (_ for _ in ()).throw(ConnectionError("connection refused")))

    assert degraded.has_plan, "the plan is gone"
    assert "token" in degraded.plan_headline
    assert degraded.sizing_failure
    assert degraded.submit_note == "", "the page still offers a size it could not establish"


def test_the_page_says_why_it_cannot_submit_rather_than_only_that_it_cannot(degraded):
    from clinicalomx.extract import submit as hop

    hop.register_sizer(lambda: (_ for _ in ()).throw(ConnectionError("connection refused")))
    assert "ConnectionError" in degraded.sizing_failure


def test_a_submit_error_reaches_the_page_verbatim(degraded):
    from clinicalomx.extract import submit as hop

    hop.register_sizer(
        lambda: (_ for _ in ()).throw(hop.SubmitError("deployment X is not registered")))
    assert degraded.sizing_failure == "deployment X is not registered"


def test_pressing_submit_against_a_dead_orchestrator_reports_and_does_not_raise(degraded):
    from clinicalomx.extract import submit as hop

    hop.register_sizer(lambda: (_ for _ in ()).throw(ConnectionError("connection refused")))
    degraded.submit()
    assert "connection refused" in degraded.notice_error
    assert degraded.notice == ""


def test_a_reachable_orchestrator_still_offers_the_submit(degraded):
    from clinicalomx.extract import submit as hop

    hop.register_sizer(lambda: 360)
    assert degraded.sizing_failure == ""
    assert "cluster job(s)" in degraded.submit_note


def test_a_merged_plan_is_costed_as_the_cores_it_reads(app):
    from clinicalomx.extract import submit as hop
    from clinicalomx.extract.jobs import Job

    app.choose_roi(TMA_UID)
    _pick(app, "omezarr.tma.plate")
    app.make_plan()
    assert app.plan_error == "", app.plan_error
    app.live = True
    hop.register_submitter(lambda chunk: None)
    hop.register_sizer(lambda: 360)

    plan = cache.plan(app.plan_token)
    est = Job(intent="", request=plan.request or {}, plans=[plan]).estimate()
    assert est["cores"] > est["units"], "the fixture no longer merges several cores"

    note = app.submit_note
    assert f"{est['cores']} core(s)" in note, note
    assert f"{est['rough_hours']}h" in note, note
    assert "unit's" not in note, "the budget is per CORE, and a unit here is the whole TMA"


def test_a_submitted_plan_is_named_on_the_jobs_tab(degraded):
    from clinicalomx.extract import submit as hop

    hop.register_sizer(lambda: 360)
    degraded.submit()
    assert degraded.notice_error == ""
    assert "Watch them on the Jobs tab" in degraded.notice
    assert degraded.submitted_token == cache.plan(degraded.plan_token).token


# Serving.
def test_a_synchronous_instance_read_survives_the_event_loop(fresh_app, monkeypatch):
    import asyncio

    from django.core.exceptions import SynchronousOnlyOperation
    from django.utils.asyncio import async_unsafe

    @async_unsafe
    def read():
        return "rows"

    async def from_a_handler():
        return read()

    monkeypatch.delenv("DJANGO_ALLOW_ASYNC_UNSAFE", raising=False)
    with pytest.raises(SynchronousOnlyOperation):
        asyncio.run(from_a_handler())

    fresh_app(live=True)
    assert asyncio.run(from_a_handler()) == "rows"


def _serve(app, path: str) -> dict[bytes, bytes]:
    """Drive one GET through an ASGI app and hand back its response headers."""
    import asyncio

    seen: dict[bytes, bytes] = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            seen.update(dict(message["headers"]))

    scope = {"type": "http", "method": "GET", "path": path, "headers": []}
    asyncio.run(app(scope, receive, send))
    return seen


async def _stub_frontend(scope, receive, send):
    """A static mount that answers as Reflex's does: an ETag, a content type, no Cache-Control."""
    kind = (b"text/html; charset=utf-8" if scope["path"].endswith("/")
            else b"text/javascript; charset=utf-8")
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", kind), (b"etag", b'"abc"')]})
    await send({"type": "http.response.body", "body": b""})


def test_the_page_shell_is_served_with_revalidation():
    from reflex_base.environment import environment

    from clinicalomx.extract.app.web.serve import asgi_app

    flags = (environment.REFLEX_ENV_MODE, environment.REFLEX_MOUNT_FRONTEND_COMPILED_APP,
             environment.REFLEX_SKIP_COMPILE)
    # Process state that `asgi_app` sets; restored after the test.
    before = [f.get() if f.is_set() else None for f in flags]
    try:
        served = asgi_app(lambda: _stub_frontend)
        assert _serve(served, "/build/")[b"cache-control"] == b"no-cache"
        assert b"cache-control" not in _serve(served, "/build/assets/root-Fo1HsfoI.js")
    finally:
        for flag, was in zip(flags, before, strict=True):
            flag.set(was)
