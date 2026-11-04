"""The curation review page: it draws the API's order and the API's graph, and adds neither."""

from __future__ import annotations

import asyncio
import json

import pytest

httpx = pytest.importorskip("httpx", reason="the curation page needs clinicalomx[app]")
pytest.importorskip("reflex", reason="the curation page needs clinicalomx[app]")

from clinicalomx.curation import app, client  # noqa: E402
from clinicalomx.curation.graph import flow_graph  # noqa: E402


def _walk(component):
    yield component
    for child in getattr(component, "children", []):
        yield from _walk(child)


def _item(claim_id: str, label: str, confidence: float, alternatives=()) -> dict:
    return {
        "claim_id": claim_id,
        "domain": "markers",
        "subject": f"ch-{claim_id}",
        "predicate": "canonical",
        "value": "CD3E",
        "confidence": confidence,
        "evidence": {
            "source": "uniprot",
            "source_id": "P07766",
            "source_version": "2026_03",
            "text": "T-cell surface glycoprotein CD3 epsilon chain",
        },
        "alternatives": [{"value": v, "confidence": c, "reasoning": ""} for v, c in alternatives],
        "triage": {"label": label, "reason": f"{label} because of {claim_id}"},
        "reasoning": "",
    }


def test_the_queue_is_drawn_in_the_order_the_api_returned():
    # In an order a client could not re-derive.
    payload = {
        "items": [
            _item("c3", "skim", 0.95),
            _item("c1", "scrutinise", 0.20, [("CD3D", 0.18)]),
            _item("c9", "review", 0.61, [("CD3G", 0.40)]),
            _item("c2", "scrutinise", 0.45),
        ],
        "next": None,
    }
    rows = client.queue_rows(payload)
    assert [r.claim_id for r in rows] == ["c3", "c1", "c9", "c2"]
    # The queue panel iterates that list itself.
    loops = [c for c in _walk(app.queue()) if type(c).__name__ == "Foreach"]
    assert [str(c.iterable) for c in loops] == [str(app.CurationState.items)]


def test_the_item_carries_confidence_alternatives_and_the_triage_reason():
    (row,) = client.queue_rows({"items": [_item("c1", "review", 0.612, [("CD3D", 0.4)])]})
    assert (row.confidence, row.triage, row.triage_reason) == (
        "0.61",
        "review",
        "review because of c1",
    )
    assert [(a.value, a.confidence) for a in row.alternatives] == [("CD3D", "0.40")]
    assert row.source == "uniprot / P07766 / 2026_03"


def test_a_lineage_of_the_four_kinds_is_four_nodes_left_to_right():
    payload = {
        "nodes": [
            {"id": "e", "kind": "entity", "label": "CD3E"},
            {"id": "s", "kind": "source", "label": "panel.csv column 4"},
            {"id": "d", "kind": "decision", "label": "accepted"},
            {"id": "c", "kind": "claim", "label": "canonical = CD3E"},
        ],
        "edges": [{"from": "s", "to": "c"}, {"from": "c", "to": "d"}, {"from": "d", "to": "e"}],
    }
    nodes, edges = flow_graph(payload)
    assert [n["id"] for n in nodes] == ["e", "s", "d", "c"]
    by_id = {n["id"]: n for n in nodes}
    xs = [by_id[k]["position"]["x"] for k in "scde"]
    assert xs == sorted(xs) and len(set(xs)) == 4
    assert [(e["source"], e["target"]) for e in edges] == [("s", "c"), ("c", "d"), ("d", "e")]


def test_an_unknown_kind_or_a_dangling_edge_is_refused():
    with pytest.raises(client.ContractError, match="kind"):
        flow_graph({"nodes": [{"id": "x", "kind": "column", "label": "?"}], "edges": []})
    with pytest.raises(client.ContractError, match="no node"):
        flow_graph(
            {
                "nodes": [{"id": "s", "kind": "source", "label": "a"}],
                "edges": [{"from": "s", "to": "missing"}],
            }
        )


def test_a_missing_field_is_named_rather_than_drawn_blank():
    item = _item("c1", "review", 0.6)
    del item["confidence"]
    with pytest.raises(client.ContractError, match="confidence"):
        client.queue_rows({"items": [item]})


def test_a_403_names_its_policy():
    r = httpx.Response(403, json={"policy": "agent-accepts-v2", "detail": "agents may not accept"})
    refused = client.refusal_of(r)
    assert refused.policy == "agent-accepts-v2"
    assert "agent-accepts-v2" in str(refused) and "agents may not accept" in str(refused)
    assert "no reason given" in str(client.refusal_of(httpx.Response(403, text="")))


def test_a_403_names_the_rule_that_refused_it_ahead_of_the_policy_it_sits_under():
    # The body the API sends when the bespoke-transform rule refuses an agent's accept.
    r = httpx.Response(403, json={
        "error": "a bespoke transform is accepted by a human; no policy lets an agent accept it",
        "rule": "a_bespoke_transform_is_accepted_by_a_human",
        "policy": "demo-policy-v1",
    })
    refused = client.refusal_of(r)
    assert refused.rule == "a_bespoke_transform_is_accepted_by_a_human"
    said = str(refused)
    assert said.startswith("refused by rule 'a_bespoke_transform_is_accepted_by_a_human'")
    assert said.index("demo-policy-v1") > said.index("a_bespoke_transform")
    assert said.endswith("no policy lets an agent accept it")
    # A refusal that names no rule still reads as the policy's.
    assert str(client.refusal_of(httpx.Response(403, json={"policy": "p", "error": "no"}))) == (
        "refused by policy 'p': no")


def test_reject_is_drawn_exactly_as_accept_is():
    accept, reject = app._decision_buttons().children
    assert accept.class_name == reject.class_name
    assert type(accept) is type(reject)


def test_two_claims_on_one_question_are_both_drawn_and_only_the_queue_claim_is_marked():
    payload = {
        "nodes": [
            {"id": "s", "kind": "source", "label": "panel.csv column 4"},
            {"id": "c1", "kind": "claim", "label": "canonical = CD3E"},
            {"id": "c2", "kind": "claim", "label": "canonical = CD3D"},
            {"id": "e", "kind": "entity", "label": "CD3E"},
        ],
        "edges": [{"from": "s", "to": "c1"}, {"from": "s", "to": "c2"}, {"from": "c1", "to": "e"}],
    }
    nodes, _ = flow_graph(payload, selected="c2")
    by_id = {n["id"]: n for n in nodes}
    assert {"c1", "c2"} <= by_id.keys()
    assert by_id["c2"]["selected"] and "cx-flow-selected" in by_id["c2"]["className"].split()
    assert "under review" in by_id["c2"]["data"]["label"]
    assert not by_id["c1"]["selected"] and "cx-flow-selected" not in by_id["c1"]["className"]
    assert "under review" not in by_id["c1"]["data"]["label"]


@pytest.mark.parametrize("token", ["", None])
def test_a_decision_with_no_token_is_refused_before_any_request(monkeypatch, token):
    monkeypatch.setenv("CURATION_API_TOKEN", "a-shared-default")
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={})

    c = client.Client(token=token, transport=httpx.MockTransport(handler))
    with pytest.raises(client.NoActor, match="no actor is identified"):
        asyncio.run(c.decide("c1", "accepted", "looks right"))
    assert sent == []


def test_a_decision_goes_under_the_typed_token():
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"decision_id": "d1"})

    c = client.Client(token="curator-7", transport=httpx.MockTransport(handler))
    asyncio.run(c.decide("c1", "rejected", "wrong gene"))
    assert seen == ["Bearer curator-7"]


def test_a_second_click_does_not_decide_the_claim_that_replaced_the_first(monkeypatch):
    sent = []

    def handler(request):
        if request.url.path.endswith("/decisions"):
            sent.append(json.loads(request.content)["claim_id"])
            return httpx.Response(200, json={})
        if request.url.path.endswith("/queue"):
            items = [] if sent else [_item("c1", "review", 0.6)]
            return httpx.Response(200, json={"items": [_item("c2", "skim", 0.9), *items]})
        return httpx.Response(200, json={"nodes": [], "edges": []})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        app.CurationState,
        "_client",
        lambda self: client.Client(token=self.token, transport=transport),
    )
    s = app.CurationState()
    s.token = "curator-7"
    asyncio.run(s.on_load())
    asyncio.run(s.select("c1"))

    asyncio.run(s.decide("c1", "accepted"))
    assert (sent, s.selected) == (["c1"], "c2")
    asyncio.run(s.decide("c1", "accepted"))
    assert sent == ["c1"]
    assert "no longer the claim on screen" in s.refusal


def test_a_decided_claim_is_not_offered_again_when_the_refresh_fails(monkeypatch):
    sent = []

    def handler(request):
        if request.url.path.endswith("/decisions"):
            sent.append(json.loads(request.content)["claim_id"])
            return httpx.Response(200, json={})
        if request.url.path.endswith("/queue"):
            if sent:
                return httpx.Response(503, text="down")
            return httpx.Response(
                200, json={"items": [_item("c1", "review", 0.6), _item("c2", "skim", 0.9)]}
            )
        return httpx.Response(200, json={"nodes": [], "edges": []})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        app.CurationState,
        "_client",
        lambda self: client.Client(token=self.token, transport=transport),
    )
    s = app.CurationState()
    s.token = "curator-7"
    asyncio.run(s.on_load())
    assert s.selected == "c1"

    asyncio.run(s.decide("c1", "accepted"))
    assert sent == ["c1"] and s.queue_error
    assert [r.claim_id for r in s.items] == ["c2"] and s.selected == "c2"


def test_a_token_is_checked_as_it_is_entered_and_a_good_one_loads_the_queue(monkeypatch):
    def handler(request):
        if request.headers.get("Authorization") != "Bearer curator-7":
            return httpx.Response(401, json={"error": "a bearer token naming a known actor"})
        if request.url.path.endswith("/queue"):
            return httpx.Response(200, json={"items": [_item("c1", "review", 0.6)]})
        return httpx.Response(200, json={"nodes": [], "edges": []})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        app.CurationState,
        "_client",
        lambda self: client.Client(token=self.token.strip(), transport=transport),
    )
    s = app.CurationState()
    asyncio.run(s.on_load())
    assert s.queue_error == str(client.NoActor()) and not s.items

    asyncio.run(s.set_token("curator-8"))
    assert s.queue_error == str(client.UnknownActor()) and not s.items

    asyncio.run(s.set_token("curator-7"))
    assert s.queue_error == "" and [r.claim_id for r in s.items] == ["c1"]
    assert s.selected == "c1"

    # A good token replaced by a wrong one stops showing a queue nobody can decide on.
    asyncio.run(s.set_token("curator-8"))
    assert s.queue_error == str(client.UnknownActor()) and not s.items and not s.selected


def _sheet_payload() -> dict:
    """A lineage: a sheet, a claimed and an unclaimed column, a claim with reasons, slots."""
    column = {
        "level": "column",
        "header": "Catalog Number",
        "address": "S!D1",
        "sheet": "S",
        "column": "D",
        "type": "number",
        "rows": 10,
        "blank": 1,
        "unique": 9,
        "top": [
            {"value": "12345", "count": 1},
            {"value": "x9", "count": 1, "note": "not a catalogue number"},
        ],
    }
    return {
        "nodes": [
            {
                "id": "e",
                "kind": "entity",
                "label": "panel.csv",
                "data": {
                    "slots": [
                        {"slot": "catalogue_number", "filled": False, "values": []},
                        {"slot": "fluorophore", "filled": True, "values": ["S!C1"]},
                    ],
                    "filled": 1,
                    "of": 2,
                },
            },
            {
                "id": "col-d",
                "kind": "source",
                "label": "spreadsheet S!D1 v1",
                "data": {**column, "claimed": True},
            },
            {
                "id": "c1",
                "kind": "claim",
                "label": "catalogue_number = S!D1",
                "data": {
                    "reasons": [
                        {
                            "kind": "semantic match",
                            "weight": 1.0,
                            "outcome": "passed",
                            "evidence": "Header is the vocabulary's own name.",
                            "check": "",
                        },
                        {
                            "kind": "not applicable",
                            "weight": None,
                            "outcome": "not applicable",
                            "evidence": "No value test exists.",
                            "check": "value test",
                        },
                        {
                            "kind": "held back",
                            "weight": -0.2,
                            "outcome": "held back",
                            "evidence": "Header alone.",
                            "check": "",
                        },
                    ],
                    "actor_kind": "agent",
                    "confidence": 0.8,
                    "outcome": None,
                },
            },
            {
                "id": "col-e",
                "kind": "source",
                "label": "S!E1 no claim",
                "data": {
                    **column,
                    "header": "Notes",
                    "address": "S!E1",
                    "column": "E",
                    "claimed": False,
                    "reason": "names no reagent field",
                },
            },
            {
                "id": "sheet",
                "kind": "source",
                "label": "panel.csv S",
                "data": {
                    "level": "file",
                    "file": "panel.csv",
                    "sheet": "S",
                    "rows": 10,
                    "columns": 2,
                    "content_hash": "abcdef0123456789",
                },
            },
        ],
        "edges": [
            {"from": "sheet", "to": "col-d"},
            {"from": "sheet", "to": "col-e"},
            {"from": "col-d", "to": "c1"},
        ],
    }


def test_a_sheet_reads_sheet_then_column_then_claim_then_entity():
    nodes, edges = flow_graph(_sheet_payload(), selected="c1")
    by_id = {n["id"]: n for n in nodes}
    x = {k: by_id[k]["position"]["x"] for k in by_id}
    assert x["sheet"] < x["col-d"] == x["col-e"] < x["c1"] < x["e"]
    assert by_id["col-d"]["type"] == "default", "a column takes an edge in from its sheet"
    assert "10 rows" in by_id["sheet"]["data"]["label"]
    assert "held back -0.20" in by_id["c1"]["data"]["label"]
    assert "not applicable n/a" in by_id["c1"]["data"]["label"]
    assert "1 of 2 filled" in by_id["e"]["data"]["label"]


def test_a_column_with_no_claim_is_drawn_dashed_and_says_why():
    nodes, _ = flow_graph(_sheet_payload())
    notes = next(n for n in nodes if n["id"] == "col-e")
    assert "cx-flow-open" in notes["className"].split()
    assert "no claim: names no reagent field" in notes["data"]["label"]


def test_the_panel_reads_the_column_and_the_slots_off_the_payload():
    payload = _sheet_payload()
    column = client.column_of(payload, "c1")
    assert (column.header, column.rows, column.blank, column.unique) == ("Catalog Number", 10, 1, 9)
    assert [(v.value, v.count, v.note) for v in column.top] == [
        ("12345", 1, ""),
        ("x9", 1, "not a catalogue number"),
    ]
    entity = client.entity_of(payload)
    assert (entity.filled, entity.of) == (1, 2)
    assert [(s.slot, s.filled, s.values) for s in entity.slots] == [
        ("catalogue_number", False, ""),
        ("fluorophore", True, "S!C1"),
    ]


def test_a_payload_without_data_still_draws_and_the_panel_says_so():
    payload = {
        "nodes": [
            {"id": "s", "kind": "source", "label": "uniprot P07766"},
            {"id": "c", "kind": "claim", "label": "canonical = CD3E"},
            {"id": "e", "kind": "entity", "label": "CD3E"},
        ],
        "edges": [{"from": "s", "to": "c"}],
    }
    nodes, _ = flow_graph(payload)
    assert [n["data"]["label"] for n in nodes] == [
        "source · uniprot P07766",
        "claim · canonical = CD3E",
        "entity · CD3E",
    ]
    assert client.column_of(payload, "c") is None and client.entity_of(payload) is None
    (row,) = client.queue_rows({"items": [_item("c1", "review", 0.6)]})
    assert row.reasons == []


def test_a_queue_item_carries_its_typed_reasons_with_the_outcome_in_words():
    item = {
        **_item("c1", "review", 0.8),
        "reasons": _sheet_payload()["nodes"][2]["data"]["reasons"],
    }
    (row,) = client.queue_rows({"items": [item]})
    assert [(r.kind, r.weight, r.outcome, r.check) for r in row.reasons] == [
        ("semantic match", "1.00", "passed", ""),
        ("not applicable", "n/a", "not applicable", "value test"),
        ("held back", "-0.20", "held back", ""),
    ]


def test_a_tab_selects_its_pane_and_a_new_claim_keeps_the_reviewer_on_it(monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={"items": [_item("c1", "review", 0.6), _item("c2", "skim", 0.9)]}
        if request.url.path.endswith("/queue") else {"nodes": [], "edges": []}))
    monkeypatch.setattr(app.CurationState, "_client",
                        lambda self: client.Client(token=self.token, transport=transport))
    s = app.CurationState()
    asyncio.run(s.on_load())
    assert s.tab == "why"
    for tab in ("column", "rivals"):
        s.set_tab(tab)
        assert s.tab == tab
    asyncio.run(s.select("c2"))
    assert s.tab == "rivals"


_TWO_CLIENTS = """
import asyncio, httpx, reflex as rx
from clinicalomx.curation import app, client

def substates(state=rx.State):
    return sorted(n for s in state.get_substates() for n in (s.get_full_name(), *substates(s)))

compiled = substates()
transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"items": []}))
app.CurationState._client = lambda self: client.Client(token=self.token, transport=transport)
for _ in range(2):
    asyncio.run(app.CurationState().on_load())
assert substates() == compiled, (compiled, substates())
"""


def test_loading_the_page_registers_no_state_the_host_did_not_compile():
    # A substate registered after the frontend compiled is dropped by Reflex. Run in a fresh
    # interpreter.
    import subprocess
    import sys

    run = subprocess.run([sys.executable, "-c", _TWO_CLIENTS], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


_TRANSFORM = {
    "reading": 'Sheet1!B1 | between("(", ")", last=True)',
    "chain": 'between("(", ")", last=True)',
    "attention": "skim",
    "why": 'one library tool: between("(", ")", last=True)',
    "bespoke": False,
    "produced": 69,
    "rows": 69,
    "complete": True,
    "examples": [{"input": "ASCT2 (AKYP0176)-BX074", "output": "AKYP0176"}],
    "choices": [
        {"chain": 'before(" (")', "reading": 'Sheet1!B1 | before(" (")'},
        {"chain": "identity()", "reading": "Sheet1!B1"},
    ],
    "accepted": 2,
    "accepted_on": ["panel-a.xlsx", "panel-b.xlsx"],
}


def _mapping_item() -> dict:
    return {
        **_item("m1", "skim", 0.9),
        "domain": "reagent_mapping",
        "subject": "panel.xlsx",
        "predicate": "clone",
        "value": _TRANSFORM["reading"],
        "evidence": {
            "source": "spreadsheet",
            "source_id": "Sheet1!B1",
            "source_version": "v1",
            "text": "header 'Panel Antibody'",
        },
        "transform": _TRANSFORM,
    }


def _mapping_lineage() -> dict:
    slots = [{"slot": s, "filled": False, "values": []} for s in ("clone", "target_label")]
    return {
        "nodes": [
            {"id": "m1", "kind": "claim", "label": "clone", "data": {"transform": _TRANSFORM}},
            {"id": "e", "kind": "entity", "label": "panel.xlsx",
             "data": {"slots": slots, "filled": 0, "of": 2}},
        ],
        "edges": [],
    }


def test_a_claims_transform_is_read_applied_off_its_node():
    shown = client.transform_of(_mapping_lineage(), "m1")
    assert (shown.chain, shown.attention, shown.produced, shown.rows) == (
        'between("(", ")", last=True)', "skim", 69, 69)
    assert [(e.input, e.output) for e in shown.examples] == [
        ("ASCT2 (AKYP0176)-BX074", "AKYP0176")]
    assert shown.accepted_on == "panel-a.xlsx, panel-b.xlsx"
    assert client.transform_of({"nodes": [{"id": "x", "kind": "claim", "label": "x"}]}, "x") is None


def test_uncounted_transform_says_so_rather_than_printing_zero():
    raw = {k: v for k, v in _TRANSFORM.items() if k not in ("produced", "rows", "examples")}
    assert client.transform_view(raw, "t").rows == -1


def _mapping_state(monkeypatch, sent):
    def handler(request):
        path = request.url.path
        if path.endswith("/queue"):
            return httpx.Response(200, json={"items": [_mapping_item()]})
        if path.endswith("/transforms/preview"):
            body = json.loads(request.content)
            sent.append(("preview", body))
            return httpx.Response(200, json={"preview": {
                **_TRANSFORM, "reading": " | ".join(p.strip() for p in body["value"].split("|")),
                "chain": 'before(" (", last=True)',
                "examples": [{"input": "ASCT2 (AKYP0176)-BX074", "output": "ASCT2"}],
                "produced": 68}})
        if path.endswith("/proposals"):
            sent.append(("propose", json.loads(request.content), request.headers["authorization"]))
            return httpx.Response(201, json={"proposal": {"claim_id": "m2"}})
        if path.endswith("/decisions"):
            sent.append(("decide", json.loads(request.content)))
            return httpx.Response(201, json={"decision": {}})
        return httpx.Response(200, json=_mapping_lineage())

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(app.CurationState, "_client",
                        lambda self: client.Client(token=self.token, transport=transport))
    s = app.CurationState()
    s.token = "curator-7"
    asyncio.run(s.on_load())
    return s


def test_editing_the_transform_previews_it_on_the_rows_and_accepting_derives_a_claim(
        monkeypatch):
    sent = []
    s = _mapping_state(monkeypatch, sent)
    assert s.transform.chain == 'between("(", ")", last=True)'
    assert (s.edit_slot, s.edit_value) == ("clone", _TRANSFORM["reading"])

    s.set_edit_slot("target_label")
    s.set_edit_value('Sheet1!B1 | before(" (", last=True)')
    asyncio.run(s.accept_edited())
    assert "Preview the edited transform" in s.edit_error and not sent

    asyncio.run(s.run_preview())
    assert sent[-1] == ("preview", {"claim_id": "m1",
                                     "value": 'Sheet1!B1 | before(" (", last=True)'})
    assert [e.output for e in s.edited.examples] == ["ASCT2"]

    s.token = ""
    asyncio.run(s.accept_edited())
    assert str(client.NoActor()) in s.edit_error, "no token, nothing proposed"
    assert [k for k, *_ in sent] == ["preview"]

    s.token = "curator-7"
    asyncio.run(s.accept_edited())
    _, body, auth = next(x for x in sent if x[0] == "propose")
    assert auth == "Bearer curator-7"
    assert (body["predicate"], body["value"], body["derived_from"]) == (
        "target_label", 'Sheet1!B1 | before(" (", last=True)', "m1")
    assert body["evidence"]["source_id"] == "Sheet1!B1"
    assert body["confidence"] == round(68 / 69, 3)
    decided = [x[1] for x in sent if x[0] == "decide"]
    assert [(d["claim_id"], d["outcome"]) for d in decided] == [
        ("m2", "accepted"), ("m1", "rejected")]
    assert "m2" in decided[1]["reason"]
    assert "rejected m1" in s.notice


def test_a_reading_typed_in_another_spelling_is_accepted_as_previewed(monkeypatch):
    sent = []
    s = _mapping_state(monkeypatch, sent)
    s.token = "curator-7"
    s.set_edit_value('Sheet1!B1|before(" (", last=True)')
    asyncio.run(s.run_preview())
    assert s.edited.reading == 'Sheet1!B1 | before(" (", last=True)'
    asyncio.run(s.accept_edited())
    assert s.edit_error == ""
    assert [k for k, *_ in sent] == ["preview", "propose", "decide", "decide"]


def test_an_unchanged_edit_is_refused_and_never_rejects_the_claim(monkeypatch):
    sent = []
    s = _mapping_state(monkeypatch, sent)
    s.token = "curator-7"
    asyncio.run(s.run_preview())
    asyncio.run(s.accept_edited())
    assert "use Accept instead" in s.edit_error
    assert [k for k, *_ in sent] == ["preview"]


def test_an_edit_the_api_answers_with_the_same_claim_decides_nothing(monkeypatch):
    sent = []
    s = _mapping_state(monkeypatch, sent)
    s.token = "curator-7"
    s.set_edit_slot("target_label")
    asyncio.run(s.run_preview())
    monkeypatch.setattr(client.Client, "propose", _answers_m1)
    asyncio.run(s.accept_edited())
    assert "use Accept instead" in s.edit_error
    assert [k for k, *_ in sent] == ["preview"]


async def _answers_m1(self, body):
    return {"claim_id": "m1"}


def test_a_library_choice_sets_the_reading_to_preview(monkeypatch):
    s = _mapping_state(monkeypatch, [])
    assert [c.chain for c in s.transform.choices] == ['before(" (")', "identity()"]
    s.set_edit_value(s.transform.choices[0].reading)
    assert s.edit_value == 'Sheet1!B1 | before(" (")' and s.edited is None


def test_changing_the_reading_drops_the_stale_preview(monkeypatch):
    s = _mapping_state(monkeypatch, [])
    s.set_edit_value('Sheet1!B1 | after("-", last=True)')
    asyncio.run(s.run_preview())
    assert s.edited is not None
    s.set_edit_value('Sheet1!B1 | trim()')
    assert s.edited is None


def test_fills_lists_the_proposal_its_rivals_the_other_slots_and_leaving_it_unplaced(
        monkeypatch):
    s = _mapping_state(monkeypatch, [])
    s.items[0].alternatives = [client.Alternative("catalogue_number", "0.19")]
    assert [(o.slot, o.label) for o in s.fills] == [
        ("clone", "clone - proposed, 0.90"),
        ("catalogue_number", "catalogue_number - rival, 0.19"),
        ("target_label", "target_label"),
        (app.UNPLACED, "- nothing, leave unplaced -"),
    ]


def test_fills_offers_rivals_and_unplaced_where_no_entity_was_drawn():
    row = client.queue_rows({"items": [{**_mapping_item(), "alternatives": [
        {"value": "catalogue_number", "confidence": 0.19, "reasoning": ""}]}]})[0]
    assert [o.slot for o in app.fill_options(row, None)] == [
        "clone", "catalogue_number", app.UNPLACED]
    assert app.fill_options(None, None) == []


def test_leaving_the_column_unplaced_rejects_the_claim_and_says_why(monkeypatch):
    sent = []
    s = _mapping_state(monkeypatch, sent)
    s.set_edit_slot(app.UNPLACED)
    asyncio.run(s.accept_edited())
    assert "Leave unplaced" in s.edit_error and not sent

    s.token = ""
    asyncio.run(s.leave_unplaced())
    assert s.refusal == str(client.NoActor()) and not sent

    s.token = "curator-7"
    s.reason = "no clone column on this sheet"
    asyncio.run(s.leave_unplaced())
    [decided] = [x[1] for x in sent if x[0] == "decide"]
    assert (decided["claim_id"], decided["outcome"]) == ("m1", "rejected")
    assert decided["reason"] == (
        "left unplaced by the curator: Sheet1!B1 does not fill clone; "
        "no clone column on this sheet")
    assert not [x for x in sent if x[0] == "propose"]
    assert s.notice == "rejected m1"
