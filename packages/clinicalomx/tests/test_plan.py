"""Tests for plan, token, staleness, and the tool surface: ``assemble_extract`` takes only a token,
``assemble`` needs ``confirm``, and a stale plan refuses to run."""
from __future__ import annotations

from dataclasses import replace

import pytest

from clinicalomx.extract import Element, Recipe
from clinicalomx.extract.assemble import AssemblyError, assemble, resolve_dotted
from clinicalomx.extract.availability import (
    Availability,
    ElementAvailability,
    RecipeReadiness,
    ROIRef,
)
from clinicalomx.extract.plan import Plan, Unit
from clinicalomx.extract.request import Destination, ExtractRequest, ROISelector

RECIPE = Recipe(name="t.core", target="OME-TIFF", roi_types=("Core",), layout="single",
                elements=(Element("image", "image", roles=("source_image",)),),
                builder="clinicalomx.extract.builders.ometiff:build_core",
                placement="clinicalomx.extract.coords:BboxPlacement")


def _now() -> str:
    """The current time, read per call so a long suite never makes plans stale."""
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _availability(now=None) -> Availability:
    # `is None`, not `or`: one test passes an empty timestamp on purpose.
    now = _now() if now is None else now
    return Availability(
        roi=ROIRef("u1", "s:TMA0:R0C0", "Core"), subtree={"Core": 1}, roles=(),
        child_coverage=(), recipes=(), bytes_checked=False,
        installed_consumers=("clinicalomx",), index_built_at=now)


def _plan(units=None, blocked=(), now=None, availability=None, **kw) -> Plan:
    now = _now() if now is None else now
    return Plan(recipe="t.core", target="OME-TIFF", layout="single", roi_name="s:TMA0:R0C0",
                availability=availability if availability is not None else _availability(now),
                units=list(units if units is not None else [_unit()]),
                blocked=list(blocked), index_built_at=now, **kw)


def _unit(name="s:TMA0:R0C0", verdict="build") -> Unit:
    return Unit("u1", name, f"./out/{name}.ome.tiff", {"image": "source_image"},
                verdict=verdict)


# The token.
def test_the_token_is_stable_for_the_same_plan():
    assert _plan().token == _plan().token


def test_the_token_moves_when_what_would_be_built_moves():
    one = _plan().token
    two = _plan(units=[_unit(), _unit("s:TMA0:R0C1")]).token
    assert one != two


def test_the_token_moves_when_the_index_moves():
    assert _plan().token != _plan(now="2020-01-01T00:30:00Z").token


def test_buildable_excludes_skipped_units():
    p = _plan(units=[_unit(), _unit("s:TMA0:R0C1", verdict="skip")])
    assert [u.roi_name for u in p.buildable] == ["s:TMA0:R0C0"]


def test_a_plan_is_json_serialisable():
    import json

    json.dumps(_plan().to_dict())


def test_the_summary_names_the_token_and_the_units():
    report, blocked = _plan().summarise()
    assert _plan().token in report and "s:TMA0:R0C0" in report and blocked is False


def test_a_blocked_plan_summarises_as_blocking():
    report, blocked = _plan(units=[], blocked=["image"]).summarise()
    assert blocked is True and "BLOCKED" in report


def test_a_blocked_plan_says_so_in_its_caveats():
    assert any("block this plan" in c for c in _plan(units=[], blocked=["image"]).caveats())


# Staleness.
def test_a_fresh_plan_is_not_stale():
    assert _plan(now="2026-01-01T00:00:00Z").is_stale(now="2026-01-01T00:10:00Z") is False


def test_a_plan_goes_stale():
    assert _plan(now="2026-01-01T00:00:00Z").is_stale(now="2026-01-01T02:00:00Z") is True


def test_a_plan_with_no_index_timestamp_is_stale():
    assert _plan(now="").is_stale(now="2026-01-01T00:00:01Z") is True


# Assemble refuses.
def test_assemble_refuses_without_confirmation():
    with pytest.raises(AssemblyError, match="confirm"):
        assemble(_plan(), confirm=False)


def test_assemble_refuses_a_blocked_plan():
    with pytest.raises(AssemblyError, match="blocked"):
        assemble(_plan(units=[], blocked=["image"]), confirm=True)


def test_assemble_refuses_a_stale_plan():
    with pytest.raises(AssemblyError, match="stale|no longer describe"):
        assemble(_plan(now="2020-01-01T00:00:00Z"), confirm=True)


def test_assemble_refuses_when_the_placement_disagrees_about_the_coordinate_space(monkeypatch):
    from clinicalomx.extract import recipes as catalogue

    odd = Recipe(name="t.core", target="OME-TIFF", roi_types=("Core",), layout="single",
                 elements=RECIPE.elements, builder=RECIPE.builder, placement=RECIPE.placement,
                 coordinate_space="somewhere-else")
    monkeypatch.setattr(catalogue, "FOUNDATION_RECIPES", (odd,))
    with pytest.raises(AssemblyError, match="coordinate space"):
        assemble(_plan(), confirm=True)


def test_assemble_reports_an_unknown_recipe_rather_than_building_nothing():
    with pytest.raises(AssemblyError, match="no recipe named"):
        assemble(_plan(), confirm=True)


# The dotted path is the only dynamic import.
def test_a_dotted_path_resolves_to_the_object():
    from clinicalomx.extract.coords import BboxPlacement

    assert resolve_dotted("clinicalomx.extract.coords:BboxPlacement") is BboxPlacement


def test_an_unimportable_builder_names_the_extra_rather_than_the_traceback():
    with pytest.raises(AssemblyError, match="--extra extract"):
        resolve_dotted("no_such_module_anywhere:build")


def test_a_recipe_with_no_builder_says_so():
    with pytest.raises(AssemblyError, match="no builder"):
        resolve_dotted("")


# The tool surface.
def test_every_tool_response_carries_provenance_and_caveats():
    from clinicalomx.extract.tools import list_recipes

    got = list_recipes()
    assert got["ok"] is True
    assert set(got) >= {"ok", "kind", "data", "caveats", "provenance"}
    assert "recipe_providers" in got["provenance"]


def test_an_invalid_request_comes_back_as_an_error_the_agent_can_act_on():
    from clinicalomx.extract.tools import plan_extract

    got = plan_extract({"roi": {"name": "x"}, "fromat": "OME-TIFF"})
    assert got["ok"] is False
    assert "not valid" in got["error"] and "schema is closed" in got["hint"]


def test_a_failed_call_tells_the_agent_not_to_answer_from_memory():
    from clinicalomx.extract.tools import plan_extract

    got = plan_extract({"nonsense": True})
    assert any("do not answer from memory" in c.lower() for c in got["caveats"])


def test_assemble_extract_refuses_a_token_it_never_issued():
    from clinicalomx.extract.tools import assemble_extract, forget_plans

    forget_plans()
    got = assemble_extract("deadbeef", confirm=True)
    assert got["ok"] is False and "no plan with token" in got["error"]
    assert "plan_extract first" in got["hint"]


def test_assemble_extract_requires_confirmation_even_with_a_valid_token():
    from clinicalomx.extract import tools

    tools.forget_plans()
    plan = _plan()
    tools._PLANS[plan.token] = plan
    got = tools.assemble_extract(plan.token, confirm=False)
    assert got["ok"] is False and got["error"] == "not confirmed"
    assert "Show the report to the user" in got["hint"]
    tools.forget_plans()


def test_assemble_extract_refuses_a_stale_token():
    from clinicalomx.extract import tools

    tools.forget_plans()
    stale = _plan(now="2020-01-01T00:00:00Z")
    tools._PLANS[stale.token] = stale
    got = tools.assemble_extract(stale.token, confirm=True)
    assert got["ok"] is False and "stale" in got["error"]
    tools.forget_plans()


def test_list_recipes_says_when_no_consumer_advertises_anything():
    from clinicalomx.extract.tools import list_recipes

    assert any("git-cloned" in c for c in list_recipes()["caveats"])


def test_the_request_a_tool_accepts_is_the_documented_model(monkeypatch):
    from clinicalomx.extract import tools

    def _no_such_roi(_request, **_kw):
        raise LookupError("the ROI selector matched nothing")

    monkeypatch.setattr("clinicalomx.extract.plan.plan", _no_such_roi)
    valid = ExtractRequest(roi=ROISelector(name="nope"), target="OME-TIFF",
                           destination=Destination(kind="path", path="./out"))
    got = tools.plan_extract(valid.model_dump(exclude_none=True))
    # It got past validation to fail on the ROI.
    assert got["ok"] is False
    assert "not valid" not in got["error"]
    assert "matched nothing" in got["error"]


# What the token has to cover.
# The recipe's content is part of the token.
def test_the_token_moves_when_the_recipes_content_moves_under_the_same_name():
    one = _plan(recipe_content={"builder": "a:build", "coordinate_space": "global"}).token
    two = _plan(recipe_content={"builder": "b:build", "coordinate_space": "global"}).token
    assert one != two


def test_the_token_moves_when_the_coordinate_space_moves():
    assert (_plan(recipe_content={"coordinate_space": "global"}).token
            != _plan(recipe_content={"coordinate_space": "slide"}).token)


def test_the_token_covers_the_resolved_artifact_keys_not_only_the_roles():
    def with_keys(keys):
        av = _availability()
        readiness = RecipeReadiness(
            "t.core", "OME-TIFF", "Core", "single", "ready",
            (ElementAvailability("image", "image", True, "self", "source_image", "present",
                                 keys=keys),), 1)
        return _plan(availability=replace(av, recipes=(readiness,)))

    assert with_keys(("img/v1.tif",)).token != with_keys(("img/v2.tif",)).token


# Fanning out over a whole slide, whatever is under it.
def _slide_plan(strictness="degrade"):
    from clinicalomx.extract.app import demo
    from clinicalomx.extract.plan import plan as make_plan
    from clinicalomx.extract.request import Destination, ExtractRequest, ROISelector

    demo.install_governance()
    snap = demo.build_snapshot()
    slide = snap.record_by_name("WholeSlide", demo.SLIDE_NAME)
    request = ExtractRequest(
        roi=ROISelector(uid=slide.uid), target="SpatialData",
        recipe="spatialdata.slide.by-roi", strictness=strictness,
        destination=Destination(kind="path", path="./out"))
    return make_plan(request, snapshot=snap), snap, slide


def test_a_slide_fans_out_over_every_subtype_below_it():
    from clinicalomx.extract.recipes import SPATIALDATA_SLIDE_BY_ROI as recipe

    _plan, snap, slide = _slide_plan()
    seen = {c.type.name for c in recipe.targets_under(slide, snap)}
    assert seen == {"TMA", "Core", "TissueSection"}


def test_it_descends_rather_than_stopping_at_the_children():
    from clinicalomx.extract.recipes import SPATIALDATA_SLIDE_BY_ROI as recipe

    _plan, snap, slide = _slide_plan()
    names = {str(c.name) for c in recipe.targets_under(slide, snap)}
    assert any(":TMA0:R0C" in n for n in names), "no Core was reached from the slide"


def test_the_plan_carries_a_role_map_per_descendant():
    plan_, _snap, _slide = _slide_plan()
    assert len(plan_.units) == 1, "merged produces ONE object"
    assert len(plan_.units[0].child_elements) > 1


def test_two_tmas_with_a_same_named_core_get_distinct_element_keys():
    from clinicalomx.extract.keys import key_for
    from clinicalomx.extract.recipes import SPATIALDATA_SLIDE_BY_ROI as recipe
    from clinicalomx.snapshot import type_name_of

    _plan, snap, slide = _slide_plan()
    taken: set = set()
    keys = [key_for(str(slide.name), str(c.name), type_name_of(c), "bgnorm", taken=taken)
            for c in recipe.targets_under(slide, snap)]
    assert len(keys) == len(set(keys))
    assert sorted(k for k in keys if "R0C0_" in k) == \
        ["TMA0_Core_R0C0_bgnorm", "TMA1_Core_R0C0_bgnorm"]
