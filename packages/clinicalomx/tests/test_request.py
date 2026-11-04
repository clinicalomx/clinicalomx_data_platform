"""Tests for the request model, the resolver, and the plan token. Mostly about what is refused."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from clinicalomx.extract.request import (
    ChannelSpec,
    Destination,
    ElementSpec,
    ExtractRequest,
    RoiQuery,
    ROISelector,
)
from clinicalomx.extract.select import Resolution, parse, resolve
from clinicalomx.names import FEATURES


def _request(**kw) -> ExtractRequest:
    base = {"roi": ROISelector(name="s:TMA0:R0C0"), "target": "OME-TIFF",
            "destination": Destination(kind="path", path="./out")}
    return ExtractRequest(**{**base, **kw})


# The schema is closed, and that is security not tidiness.
def test_an_invented_field_is_rejected_not_ignored():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        _request(fromat="OME-TIFF")


@pytest.mark.parametrize("model", [ROISelector, RoiQuery, ElementSpec, ChannelSpec, Destination])
def test_every_nested_model_is_closed_too(model):
    assert model.model_config["extra"] == "forbid"


# Ambiguity is rejected at construction, not resolved by a default.
@pytest.mark.parametrize(
    "kwargs",
    [{}, {"uid": "u", "name": "n"}, {"name": "n", "fuzzy": "f"},
     {"uid": "u", "query": RoiQuery(roi_type="Core")}],
    ids=["none", "uid+name", "name+fuzzy", "uid+query"],
)
def test_a_selector_needs_exactly_one_way_of_naming_an_roi(kwargs):
    with pytest.raises(ValidationError, match="exactly one"):
        ROISelector(**kwargs)


def test_child_type_without_descending_is_rejected():
    with pytest.raises(ValidationError, match="include"):
        ROISelector(name="s:TMA0", child_type="Core")


def test_an_unfiltered_roi_query_is_rejected():
    with pytest.raises(ValidationError, match="at least one predicate"):
        RoiQuery()


def test_include_and_exclude_are_mutually_exclusive():
    with pytest.raises(ValidationError, match="mutually exclusive"):
        ElementSpec(include=["image"], exclude=["table"])


def test_an_element_cannot_be_both_required_and_optional():
    with pytest.raises(ValidationError, match="optional"):
        ElementSpec(include=["image"], optional=["image"])


def test_channel_subsetting_needs_exactly_one_of_names_or_indices():
    with pytest.raises(ValidationError, match="exactly one"):
        ChannelSpec(names=["CD8"], indices=[0])
    with pytest.raises(ValidationError, match="exactly one"):
        ChannelSpec()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [({"kind": "path"}, "needs `path`"),
     ({"kind": "artifact"}, "needs `key`"),
     ({"kind": "memory", "path": "./x"}, "neither"),
     ({"kind": "path", "path": "./x", "key": "k"}, "use kind 'artifact'")],
    ids=["path-no-path", "artifact-no-key", "memory-with-path", "path-with-key"],
)
def test_a_destination_cannot_be_read_two_ways(kwargs, match):
    with pytest.raises(ValidationError, match=match):
        Destination(**kwargs)


def test_a_local_path_is_the_default_and_does_not_register():
    assert _request().destination.registers is False
    assert Destination(kind="artifact", key="extracts/x").registers is True


def test_optional_elements_are_meaningless_under_strict():
    with pytest.raises(ValidationError, match="only means something"):
        _request(elements=ElementSpec(optional=["table"]))


def test_merged_over_children_is_rejected():
    with pytest.raises(ValidationError, match="merged"):
        _request(layout="merged",
                 roi=ROISelector(name="s:TMA0", include="children", child_type="Core"))


def test_layout_may_be_omitted_so_a_minimal_request_validates():
    assert _request().layout is None


def test_a_request_round_trips_through_yaml():
    original = _request(recipe="ometiff.core", strictness="degrade")
    assert ExtractRequest.model_validate_json(original.model_dump_json()) == original
    assert "ometiff.core" in original.to_yaml()


# The resolver refuses to guess.
class _Rec:
    def __init__(self, uid, name, roi_type="Core"):
        self.uid, self.name = uid, name
        self.type = type("T", (), {"name": roi_type})()


SLIDE_A = "Arutha YTMA 471-4_Scan1-16-bit"
SLIDE_B = "Arutha YTMA 471-9_Scan2-16-bit"
RECORDS = [_Rec("a", f"{SLIDE_A}:TMA0:R2C4"), _Rec("b", f"{SLIDE_B}:TMA0:R2C4"),
           _Rec("c", f"{SLIDE_A}:TMA0:R2C5"), _Rec("t", f"{SLIDE_A}:TMA0", "TMA")]


@pytest.mark.parametrize(
    ("query", "expected"),
    [("core R2C4 in TMA0", {"tma": "TMA0", "cell": "R2C4"}),
     ("R 2 C 4 of tma_1", {"tma": "TMA1", "cell": "R2C4"}),
     ("the arutha slide", {"slide_tokens": "arutha"})],
    ids=["plain", "spaced-and-underscored", "slide-only"],
)
def test_the_spine_is_parsed_out_of_free_text(query, expected):
    assert parse(query) == expected


def test_format_words_do_not_become_slide_tokens():
    assert "ome.tiff" not in parse("give me R2C4 as an ome.tiff").get("slide_tokens", "")


def test_two_slides_matching_equally_well_is_ambiguous_and_picks_nothing():
    res = resolve("core R2C4 in TMA0 of the Arutha slide", RECORDS)
    assert res.exact is None and res.ambiguous
    assert res.tied == 2, "both Arutha slides tie; a third partial match must not inflate this"


def test_naming_the_scan_resolves_it_exactly():
    res = resolve(f"R2C4 in TMA0 of {SLIDE_B}", RECORDS)
    assert res.exact is not None and res.exact.name == f"{SLIDE_B}:TMA0:R2C4"


def test_an_exact_name_always_wins():
    res = resolve(f"{SLIDE_A}:TMA0:R2C4", RECORDS)
    assert res.exact.uid == "a" and res.exact.why == "exact name match"


def test_every_candidate_explains_itself():
    res = resolve("core R2C4 in TMA0 of the Arutha slide", RECORDS)
    assert all(c.why for c in res.candidates)


def test_the_roi_type_filter_is_respected():
    res = resolve("TMA0 of the Arutha slide", RECORDS, roi_type="TMA")
    assert {c.roi_type for c in res.candidates} == {"TMA"}


def test_nothing_matching_is_not_ambiguous_and_says_so():
    res = resolve("R9C9 in TMA7 of some other slide", RECORDS)
    assert res.exact is None and not res.ambiguous
    assert "No ROI matches" in res.caveats()[0]


def test_an_ambiguous_resolution_carries_an_instruction_not_just_a_state():
    res = resolve("core R2C4 in TMA0 of the Arutha slide", RECORDS)
    assert "do not choose" in res.caveats()[0].lower()


def test_a_resolution_is_json_serialisable():
    import json

    json.dumps(resolve("R2C4 in TMA0", RECORDS).to_dict())


def test_resolution_reports_the_tied_count_not_the_candidate_count():
    res = resolve("core R2C4 in TMA0 of the Arutha slide", RECORDS)
    assert len(res.candidates) > res.tied
    assert str(res.tied) in res.caveats()[0]


def test_resolution_type_is_frozen():
    res = resolve("R2C4 in TMA0", RECORDS)
    with pytest.raises((AttributeError, TypeError)):
        res.exact = None  # type: ignore[misc]
    assert isinstance(res, Resolution)


# Adding a slot the recipe does not declare.
# Added elements.
def test_an_added_element_names_what_it_reads_and_what_slot_it_fills():
    from clinicalomx.extract.request import AddedElement

    added = AddedElement(name="thumb", role="roi_thumbnail", kind="image")
    assert (added.name, added.role, added.kind) == ("thumb", "roi_thumbnail", "image")
    assert added.required is False, "an added slot must not be able to block the build"


def test_two_added_elements_cannot_share_a_name():
    from clinicalomx.extract.request import AddedElement

    with pytest.raises(ValidationError, match="share a name"):
        ElementSpec(add=[AddedElement(name="x", role="a"), AddedElement(name="x", role="b")])


def test_an_added_element_can_address_a_variable():
    from clinicalomx.extract.request import AddedElement

    added = AddedElement(name="posthoc", role="bgnormed", variable="adjusted_image_post_hoc")
    assert added.variable == "adjusted_image_post_hoc"


def test_an_added_element_is_still_a_closed_model():
    from clinicalomx.extract.request import AddedElement

    with pytest.raises(ValidationError, match="extra_forbidden"):
        AddedElement(name="x", role="a", kidn="image")


def test_adding_a_slot_that_collides_with_a_recipe_element_is_refused():
    from clinicalomx.extract import Element, Recipe
    from clinicalomx.extract.plan import _add_elements
    from clinicalomx.extract.request import AddedElement

    recipe = Recipe(name="r", target="SpatialData", roi_types=("Core",), layout="single",
                    elements=(Element("raw", "image", roles=("source_image",)),),
                    builder="m:b", placement="m:p")
    with pytest.raises(ValueError, match="already declares"):
        _add_elements(recipe, [AddedElement(name="raw", role="other")])


def test_an_added_image_inherits_the_recipes_placement_requirements():
    from clinicalomx.extract import Element, Recipe
    from clinicalomx.extract.plan import _add_elements
    from clinicalomx.extract.request import AddedElement

    recipe = Recipe(name="r", target="SpatialData", roi_types=("Core",), layout="single",
                    elements=(Element("raw", "image", roles=("source_image",),
                                      needs_features=("source_artifact", "parquet_row")),),
                    builder="m:b", placement="m:p")
    grown = _add_elements(recipe, [AddedElement(name="thumb", role="roi_thumbnail")])
    assert grown.element("thumb").needs_features == ("source_artifact", "parquet_row")


def test_an_added_table_does_not_inherit_image_placement():
    from clinicalomx.extract import Element, Recipe
    from clinicalomx.extract.plan import _add_elements
    from clinicalomx.extract.request import AddedElement

    recipe = Recipe(name="r", target="SpatialData", roi_types=("Core",), layout="single",
                    elements=(Element("raw", "image", roles=("source_image",),
                                      needs_features=("parquet_row",)),),
                    builder="m:b", placement="m:p")
    grown = _add_elements(recipe, [AddedElement(name="qc", role="bgnorm_qc", kind="table")])
    assert grown.element("qc").needs_features == ()


# A declared predicate has to reach the query layer.
# Every RoiQuery predicate is applied. Enumerated over `PUSHDOWN` and `SCANNED`.
class _Roi:
    """A stand-in ROI Record. `parent` is the container's uid."""

    def __init__(self, uid, name, *, parent=None, roi_type="Core", state=None, roles=()):
        self.uid, self.name = uid, name
        self.type = type("T", (), {"name": roi_type})()
        self.parent, self.state, self.roles = parent, state, tuple(roles)


class _RecordingSource:
    """A Source that applies the tier-1 predicates and records the Selection it was given."""

    def __init__(self, records=()):
        self.records = list(records)
        self.seen: list = []

    def slug(self) -> str:
        return "recording"

    def entities(self, selection):
        self.seen.append(selection)
        out = [r for r in self.records
               if (not selection.roi_type or r.type.name == selection.roi_type)
               and (not selection.name_contains
                    or selection.name_contains.lower() in r.name.lower())
               and (not selection.parent_uid or r.parent == selection.parent_uid)
               and (not selection.state or r.state == selection.state)
               and all(role in r.roles for role in selection.has_roles)]
        return out[:selection.limit]

    def entity(self, **_kw):
        return None

    def artifacts(self, _artifact_ids):
        return []


class _Values:
    """Only `values()` is read by the tier-2 scan."""

    def __init__(self, by_uid):
        self._by_uid = by_uid

    def values(self, rec):
        return self._by_uid.get(rec.uid, {})


def test_a_parent_filter_returns_that_containers_children_and_nothing_else():
    from clinicalomx.extract.select import select
    from clinicalomx.snapshot import Snapshot

    source = _RecordingSource([
        _Roi("p", "slideA:TMA0", roi_type="TMA"),
        _Roi("c1", "slideA:TMA0:R1C1", parent="p"),
        _Roi("c2", "slideA:TMA0:R1C2", parent="p"),
        _Roi("x", "slideB:TMA0:R1C1", parent="q"),
    ])
    got = select(ROISelector(query=RoiQuery(parent="p")), snapshot=Snapshot(), source=source)

    assert {r.uid for r in got} == {"c1", "c2"}
    assert "x" not in {r.uid for r in got}, (
        "an ROI under another container came back, so the parent predicate never reached the "
        "source and the caller got an unnarrowed set")


def test_every_declared_predicate_is_either_pushed_down_or_scanned():
    declared = set(RoiQuery.model_fields) - {"limit"}
    assert declared == set(RoiQuery.PUSHDOWN) | set(RoiQuery.SCANNED)


def test_a_predicate_missing_from_the_map_cannot_be_the_only_one_asked_for():
    assert RoiQuery(parent="p").is_empty is False
    assert RoiQuery.model_construct(parent=None, cohort=None).is_empty is True


#: One probe value per tier-1 predicate, parametrised from `PUSHDOWN`.
_PUSHED = {"roi_type": "Core", "name_contains": "R1C1", "state": "reviewed",
           "parent": "p", "has_roles": ["bgnormed"]}


@pytest.mark.parametrize("field", sorted(RoiQuery.PUSHDOWN))
def test_every_pushed_down_predicate_reaches_the_source(field):
    from clinicalomx.extract.select import select
    from clinicalomx.snapshot import Snapshot

    value = _PUSHED.get(field)
    assert value is not None, f"give the new tier-1 predicate {field!r} a probe value"

    source = _RecordingSource()
    select(ROISelector(query=RoiQuery(**{field: value})), snapshot=Snapshot(), source=source)

    got = getattr(source.seen[-1], RoiQuery.PUSHDOWN[field])
    assert got == (tuple(value) if isinstance(value, list) else value), (
        f"`{field}` was declared, accepted and then not handed to the source")


#: ``(value asked for, the matching record's feature values, the other record's)``.
_SCANNED = {
    "cohort": ("CohortA", {FEATURES.COHORT: "CohortA"}, {FEATURES.COHORT: "CohortB"}),
    "patient_id": ("P-1", {FEATURES.PATIENT_ID: "P-1"}, {FEATURES.PATIENT_ID: "P-2"}),
    "slide": ("slideA", {}, {}),
}


@pytest.mark.parametrize("field", sorted(RoiQuery.SCANNED))
def test_every_scanned_predicate_drops_a_record_that_does_not_match(field):
    from clinicalomx.extract.select import _scan

    probe = _SCANNED.get(field)
    assert probe is not None, f"give the new tier-2 predicate {field!r} a probe value"
    value, hit, miss = probe

    match = _Roi("m", "slideA:TMA0:R1C1")
    other = _Roi("o", "slideB:TMA0:R1C1")
    out = _scan([match, other], RoiQuery(**{field: value}),
                _Values({"m": hit, "o": miss}))

    assert [r.uid for r in out] == ["m"], f"`{field}` did not narrow the scan"
