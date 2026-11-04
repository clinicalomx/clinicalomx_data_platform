"""The rendered Automations match ``tests/data/pre_split_edges.json``, field for field.

Regenerate the fixture in the same commit when the graph changes on purpose.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from qptiff_pipeline.domain import states
from qptiff_pipeline.domain.states import S
from qptiff_pipeline.orchestration import automations

FIXTURE = json.loads((Path(__file__).parent / "data" / "pre_split_edges.json").read_text())


def _as_dict(edge, fields):
    return {f: getattr(edge, f) for f in fields}


EDGE_FIELDS = ("name", "on_event", "deployment", "param", "uid_key", "description", "enabled")
FLOW_FIELDS = ("name", "source_deployment", "deployment", "parameters", "description", "on_state")


def test_unit_edges_are_unchanged():
    got = [_as_dict(e, EDGE_FIELDS) for e in automations.EDGES]
    assert got == FIXTURE["EDGES"]


def test_flow_edges_are_unchanged():
    got = [_as_dict(e, FLOW_FIELDS) for e in automations.FLOW_EDGES]
    assert got == FIXTURE["FLOW_EDGES"]


def test_the_paused_edge_is_still_paused():
    paused = [e.name for e in automations.EDGES if not e.enabled]
    assert paused == ["slide-registered-general-detect"]


@pytest.mark.parametrize("field", ["on_event", "deployment", "param"])
def test_no_edge_has_an_empty_required_field(field):
    for edge in automations.EDGES:
        assert getattr(edge, field), (edge.name, field)


def test_edge_names_are_unique():
    names = [e.name for e in automations.EDGES] + [e.name for e in automations.FLOW_EDGES]
    assert len(names) == len(set(names))


# The topology the edges are rendered from.
def test_every_edge_event_is_a_topology_event():
    known = set(states.ENTRY_EVENTS.values())
    known |= {g.event for g in states.GATES.values()}
    known.add(states.SLIDE_RESET_EVENT)
    unknown = [(e.name, e.on_event) for e in automations.EDGES if e.on_event not in known]
    assert not unknown, f"edges firing on events the graph does not emit: {unknown}"


def test_derived_views_agree_with_the_stage_table():
    assert set(states.GATES) <= {s.name for s in states.STAGES}
    for spec in states.STAGES:
        assert states.STAGE_ENTRY[spec.name] == (spec.unit_type, spec.entry_state)
        assert states.ENTRY_EVENTS[spec.name] == spec.entry_event
        if spec.gate is not None:
            assert states.GATES[spec.name] is spec.gate


def test_a_bad_state_in_the_topology_is_rejected_at_import():
    bad = states.StageSpec(
        name="broken", unit_type="TMA", entry_state="not_a_state",  # type: ignore[arg-type]
        entry_event="x", flow="y",
    )
    real = states.STAGES
    states.STAGES = (*real, bad)
    try:
        with pytest.raises(ValueError, match="not an S"):
            states._validate()
    finally:
        states.STAGES = real


def test_stage_for_disambiguates_by_unit_type():
    assert states.stage_for("WholeSlide", S.REGISTERED).name == "general"
    assert states.stage_for("TMA", S.REGISTERED).name == "core"
    assert states.stage_for("Core", S.REGISTERED) is None


def test_gate_for_state_finds_the_parked_stage():
    spec, gate = states.gate_for_state(S.CORE_PENDING)
    assert spec.name == "core" and gate.approved == S.CORE_APPROVED
    assert states.gate_for_state(S.REGISTERED) is None


def test_state_values_serialise_as_bare_strings():
    assert str(S.CORE_APPROVED) == "core_approved"
    assert f"{S.CORE_APPROVED}" == "core_approved"
    assert S.CORE_APPROVED == "core_approved"
