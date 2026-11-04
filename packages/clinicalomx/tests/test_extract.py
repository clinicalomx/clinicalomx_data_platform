"""Tests for the recipe protocol, the catalogue, and discovery. Element-key uniqueness is in
``test_keys.py``."""
from __future__ import annotations

import subprocess
import sys

import pytest

from clinicalomx import extract
from clinicalomx.extract import Element, Recipe

# Imported explicitly so these tests do not depend on collection order.
from clinicalomx.extract import recipes as catalogue

IMAGE = Element("image", "image", roles=("bgnormed", "source_image"))
TABLE = Element("table", "table", roles=("cell_tables",), required=False)


def _recipe(**kw) -> Recipe:
    base = {"name": "t.one", "target": "SpatialData", "roi_types": ("Core",),
            "layout": "single", "elements": (IMAGE,)}
    return Recipe(**{**base, **kw})


@pytest.fixture(autouse=True)
def _no_leftover_registrations():
    extract.reset_registrations()
    yield
    extract.reset_registrations()


# The declaration cannot be incoherent.
def test_an_element_with_no_candidate_roles_is_rejected():
    with pytest.raises(ValueError, match="no candidate roles"):
        Element("image", "image", roles=())


def test_descending_per_child_over_every_subtype_is_refused():
    from clinicalomx.extract import ANY_CHILD

    with pytest.raises(ValueError, match="descend"):
        _recipe(layout="per_child", child_types=(ANY_CHILD,), descend=True,
                elements=(Element("image", "image", roles=("x",)),))
    named = _recipe(layout="per_child", child_types=("Core",), descend=True,
                    elements=(Element("image", "image", roles=("x",)),))
    assert named.descend, "a named subtype below the children is a slide's per-core product"


def test_descending_means_nothing_for_a_single_object():
    with pytest.raises(ValueError, match="descend"):
        _recipe(layout="single", descend=True,
                elements=(Element("image", "image", roles=("x",)),))


def test_ANY_CHILD_is_distinct_from_declaring_no_children():
    from clinicalomx.extract import ANY_CHILD

    with pytest.raises(ValueError, match="child_types"):
        _recipe(layout="per_child", child_types=(),
                elements=(Element("image", "image", roles=("x",)),))
    r = _recipe(layout="per_child", child_types=(ANY_CHILD,),
                elements=(Element("image", "image", roles=("x",)),))
    assert r.expands_into("TissueSection"), "the sentinel must cover a subtype nobody enumerated"


def test_a_recipe_that_reads_children_must_say_which_children():
    with pytest.raises(ValueError, match="child_types"):
        _recipe(layout="per_child")


def test_a_recipe_nothing_could_ask_for_is_rejected():
    with pytest.raises(ValueError, match="roi_types"):
        _recipe(roi_types=())


def test_a_recipe_with_no_elements_is_rejected():
    with pytest.raises(ValueError, match="no elements"):
        _recipe(elements=())


# Requires_any - what stops an availability-driven recipe writing an empty file.
def test_a_recipe_with_no_required_element_and_no_requires_any_is_rejected():
    with pytest.raises(ValueError, match="succeed.*by building nothing"):
        _recipe(elements=(TABLE,))


def test_requires_any_must_name_real_elements():
    with pytest.raises(ValueError, match="not its elements"):
        _recipe(elements=(TABLE,), requires_any=("no_such_element",))


def test_requires_any_satisfies_the_no_required_element_rule():
    r = _recipe(elements=(TABLE,), requires_any=("table",))
    assert r.requires_any == ("table",)


def test_an_element_can_name_a_variable_within_its_artifact():
    r = _recipe(elements=(Element("bgnorm", "image", roles=("bgnormed",),
                                  variable="adjusted_image"),))
    assert r.element("bgnorm").variable == "adjusted_image"
    assert r.to_dict()["elements"][0]["variable"] == "adjusted_image"


def test_ometiff_core_offers_three_series_none_of_them_individually_required():
    r = {x.name: x for x in catalogue.FOUNDATION_RECIPES}["ometiff.core"]
    assert [e.name for e in r.elements] == ["raw", "bgnorm", "bgnormQ"]
    assert not any(e.required for e in r.elements)
    assert set(r.requires_any) == {"raw", "bgnorm", "bgnormQ"}


def test_the_two_bgnorm_series_read_the_same_role_but_different_variables():
    r = {x.name: x for x in catalogue.FOUNDATION_RECIPES}["ometiff.core"]
    bgnorm, bgnormq = r.element("bgnorm"), r.element("bgnormQ")
    assert bgnorm.roles == bgnormq.roles == ("bgnormed",)
    assert bgnorm.variable == "adjusted_image"
    assert bgnormq.variable == "adjusted_image_post_hoc"


def test_no_core_element_asks_for_a_bbox_feature():
    from clinicalomx.names import TYPES

    asked = {f for r in catalogue.FOUNDATION_RECIPES
             if TYPES.CORE in (*r.roi_types, *r.child_types)
             for e in r.elements for f in e.needs_features}
    assert not {f for f in asked if f.startswith("bbox_")}


def test_a_bbox_element_is_paired_with_a_bbox_placement():
    for r in catalogue.FOUNDATION_RECIPES:
        if len(r.placements) != 1:
            continue
        wants_bbox = any(f.startswith("bbox_") for e in r.elements for f in e.needs_features)
        declared = " ".join(r.placements.values())
        assert wants_bbox == ("BboxPlacement" in declared), r.name


def test_no_recipe_anywhere_places_a_core_by_bbox():
    from clinicalomx.names import TYPES

    for r in catalogue.FOUNDATION_RECIPES:
        if not (TYPES.CORE in r.roi_types or r.expands_into(TYPES.CORE)):
            continue
        assert "Bbox" not in (r.placement_for(TYPES.CORE) or ""), r.name


def test_every_subtype_a_mixed_recipe_expands_into_has_a_placement():
    from clinicalomx.names import TYPES

    r = {x.name: x for x in catalogue.FOUNDATION_RECIPES}["spatialdata.slide.by-roi"]
    for subtype in (TYPES.TMA, TYPES.CORE):
        assert r.placement_for(subtype), subtype


def test_a_subtype_nothing_declares_resolves_to_no_placement():
    r = {x.name: x for x in catalogue.FOUNDATION_RECIPES}["spatialdata.slide.by-roi"]
    assert r.placement_for("TissueSection") is None
    assert r.expands_into("TissueSection"), "it is still walked - it is just not placed silently"


def test_required_roles_spans_only_the_required_elements():
    r = _recipe(elements=(IMAGE, TABLE))
    assert r.required_roles == ("bgnormed", "source_image")


def test_a_recipe_round_trips_to_a_plain_dict():
    import json

    as_dict = _recipe(elements=(IMAGE, TABLE)).to_dict()
    assert json.loads(json.dumps(as_dict))["elements"][0]["roles"] == ["bgnormed", "source_image"]


# Discovery.
class _EP:
    def __init__(self, name, value):
        self.name, self._value = name, value

    def load(self):
        if isinstance(self._value, Exception):
            raise self._value
        return self._value


def test_discover_collects_each_consumer_s_recipes(monkeypatch):
    monkeypatch.setattr("importlib.metadata.entry_points",
                        lambda group: [_EP("repo-a", [_recipe(name="a.one")])])
    assert {o: [r.name for r in rs] for o, rs in extract.discover().items()} == {
        "repo-a": ["a.one"]}


def test_a_broken_consumer_does_not_break_the_catalogue(monkeypatch):
    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda group: [_EP("broken", ImportError("no")),
                       _EP("fine", [_recipe(name="f.one")])])
    found = extract.discover()
    assert "broken" not in found
    assert [r.name for r in found["fine"]] == ["f.one"]


def test_non_recipe_objects_are_filtered_out(monkeypatch):
    monkeypatch.setattr("importlib.metadata.entry_points",
                        lambda group: [_EP("sloppy", [_recipe(), "not a recipe"])])
    assert [r.name for r in extract.discover()["sloppy"]] == ["t.one"]


def test_register_recipes_reaches_a_consumer_that_is_not_pip_installed():
    extract.register_recipes([_recipe(name="cloned.one")], provider="cloned")
    assert "cloned.one" in [r.name for r in extract.all_recipes()]
    assert "cloned" in extract.providers()


def test_register_recipes_ignores_objects_that_are_not_recipes():
    extract.register_recipes([_recipe(name="ok.one"), object()], provider="mixed")
    assert [r.name for r in extract.discover()["mixed"]] == ["ok.one"]


def test_a_consumer_recipe_shadows_a_foundation_one_of_the_same_name():
    extract.register_recipes(
        [_recipe(name="ometiff.core", owner="croi-core", builder="croi:build")])
    got = {r.name: r for r in extract.all_recipes()}["ometiff.core"]
    assert got.owner == "croi-core" and got.builder == "croi:build"
    assert len([r for r in extract.all_recipes() if r.name == "ometiff.core"]) == 1


# The foundation catalogue.
def test_the_catalogue_covers_both_tma_granularities():
    tma = {r.name: r for r in extract.recipes_for("TMA", target="SpatialData")}
    assert tma["spatialdata.tma.by-core"].layout == "merged"
    assert tma["spatialdata.tma.per-core"].layout == "per_child"


def test_every_foundation_recipe_names_a_builder_and_a_placement():
    missing = [r.name for r in catalogue.FOUNDATION_RECIPES
               if not r.builder or not r.placement]
    assert not missing


def test_the_core_grid_prefers_the_reviewed_shapes_over_the_raw_detection():
    cores = {r.name: r for r in catalogue.FOUNDATION_RECIPES}[
        "spatialdata.tma.by-core"].element("cores")
    assert cores.roles.index("cores_accepted") < cores.roles.index("core_shapes")


def test_a_spatialdata_can_hold_every_image_variant_at_once():
    r = {x.name: x for x in catalogue.FOUNDATION_RECIPES}["spatialdata.core"]
    images = [e for e in r.elements if e.kind == "image"]
    assert [e.name for e in images] == ["raw", "bgnorm", "bgnormQ"]
    assert set(r.requires_any) == {"raw", "bgnorm", "bgnormQ"}
    assert images[0].roles == ("source_image",) and images[1].roles == ("bgnormed",)


def test_only_the_parent_level_recipes_register_an_output():
    publishes = {r.name for r in catalogue.FOUNDATION_RECIPES if r.publish_role}
    assert publishes == {"spatialdata.tma.by-core", "spatialdata.slide",
                         "spatialdata.slide.by-roi"}


# The reference render.
def test_render_is_byte_stable():
    from clinicalomx.extract.docs import render

    assert render() == render()


def test_every_foundation_recipe_and_role_appears_in_the_render():
    from clinicalomx.extract.docs import render

    text = render()
    for r in catalogue.FOUNDATION_RECIPES:
        assert r.name in text, f"{r.name} is missing from the reference"
        for e in r.elements:
            for role in e.roles:
                assert role in text, f"role {role} is missing from the reference"


def test_the_render_excludes_discovered_consumer_recipes():
    from clinicalomx.extract.docs import render

    extract.register_recipes([_recipe(name="consumer.only")], provider="c")
    assert "consumer.only" not in render()


def test_the_committed_reference_is_current():
    import pathlib

    from clinicalomx.extract.docs import render

    path = pathlib.Path(__file__).parent.parent / "docs" / "extract-recipes.md"
    assert path.read_text() == render(), (
        "docs/extract-recipes.md is stale; regenerate with\n"
        "  python -m clinicalomx.extract docs > docs/extract-recipes.md")


def test_the_render_needs_no_database():
    proc = subprocess.run(
        [sys.executable, "-c",
         "from clinicalomx.extract.docs import render; render();"
         "import sys; print('yes' if 'lamindb' in sys.modules else '')"],
        capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


# The CLI.
@pytest.mark.parametrize("argv", [["--help"], ["recipes"], ["recipes", "--json"], ["docs"]])
def test_the_cli_works_without_a_database(argv):
    proc = subprocess.run([sys.executable, "-m", "clinicalomx.extract", *argv],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_recipes_always_reports_how_many_providers_were_found():
    proc = subprocess.run([sys.executable, "-m", "clinicalomx.extract", "recipes"],
                          capture_output=True, text=True, check=False)
    assert "provider(s)" in proc.stdout


def test_docs_check_exits_nonzero_on_a_stale_file(tmp_path):
    stale = tmp_path / "extract-recipes.md"
    stale.write_text("# not the render\n")
    proc = subprocess.run(
        [sys.executable, "-m", "clinicalomx.extract", "docs", "--check", str(stale)],
        capture_output=True, text=True, check=False)
    assert proc.returncode == 1
    assert "STALE" in proc.stdout and "Regenerate" in proc.stdout
