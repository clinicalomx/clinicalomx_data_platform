"""The curation API against a real ledger: who may write what, and that the API writes what the
CLI writes. The offline half, authentication with no database, is `tests/test_api.py`."""

from __future__ import annotations

from pathlib import Path

import lamindb as ln
import pytest
from clinicalomx.review import ensure_state_feature
from clinicalomx_ledger import adapter, reads, services
from clinicalomx_ledger.grammar.gate import Judgement
from clinicalomx_ledger.grammar.store import LedgerClaimStore, MemoryClaimStore
from starlette.testclient import TestClient
from tests.conftest import fixture_transport

from clinicalomx_markers.api.tokens import Actor, Tokens, digest
from clinicalomx_markers.api.v1 import (
    DIRECTED_BY_HEADER,
    DIRECTED_BY_RULE,
    POLICY_RULE,
    RELEASE_RULE,
    _proposal,
    curation_api,
)
from clinicalomx_markers.curate import session
from clinicalomx_markers.domain.marker import MARKER_DOMAIN, MarkerPredicate
from clinicalomx_markers.domain.reagent_mapping import (
    MAPPING_TYPES,
    REAGENT_MAPPING_DOMAIN,
    NeedsAHuman,
)
from clinicalomx_markers.propose.columns import column_profile, map_columns, source_profile
from clinicalomx_markers.propose.schema import review_order
from clinicalomx_markers.sources.sheet import columns_of, read_rows
from conftest import BRANCH, HUMAN, subject

AGENT_MODEL = {"agent_model": "synthetic-model", "agent_model_version": "1"}
TOKENS = Tokens({
    digest("human-token"): Actor(name="rafael", kind="human"),
    digest("agent-token"): Actor(name="curator-agent", kind="agent", **AGENT_MODEL),
    digest("unapproved-token"): Actor(name="curator-agent", kind="agent", **AGENT_MODEL,
                                      policy_version="never-approved"),
    digest("approved-token"): Actor(name="policy-agent", kind="agent", **AGENT_MODEL,
                                    policy_version="api-test-policy"),
})
TODAY = "2026-09-25"


def as_(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module", autouse=True)
def instance():
    # Declared on main before any run exists.
    ensure_state_feature(feature=MARKER_DOMAIN.state_feature)
    ensure_state_feature(feature=REAGENT_MAPPING_DOMAIN.state_feature)
    services.approve_policy(version="api-test-policy", approved_by="rafael")


@pytest.fixture(autouse=True)
def default_branch():
    """Restore the default branch after `ln.finish` changes it."""
    yield
    ln.context._run = None
    ln.context._branch = None


@pytest.fixture
def client():
    return TestClient(curation_api(tokens=TOKENS, branch=BRANCH, today=lambda: TODAY))


def proposal(name: str, **over) -> dict:
    body = {"domain": MARKER_DOMAIN.name, "subject": name,
            "predicate": str(MarkerPredicate.SEGMENTATION_ROLE), "value": "marker",
            "evidence": {"source": "human", "source_id": "rafael", "source_version": "",
                         "text": f"{name} stains the membrane on PCF"},
            "confidence": 0.9,
            "alternatives": [{"value": "cyto_structural", "confidence": 0.1}],
            "triage": {"label": "skim", "reason": "one clear answer"},
            "reasoning": "membrane stain"}
    return {**body, **over}


def test_a_human_may_accept_and_an_agent_may_not_and_the_403_names_the_policy(client, unique):
    name = subject(f"DECIDE-{unique}")
    made = client.post("/api/v1/proposals", json=proposal(name), headers=as_("agent-token"))
    assert made.status_code == 201, made.text
    claim_id = made.json()["proposal"]["claim_id"]

    for token, policy in (("agent-token", None), ("unapproved-token", "never-approved")):
        refused = client.post("/api/v1/decisions", headers=as_(token), json={
            "claim_id": claim_id, "outcome": "accepted", "reason": "looks right"})
        assert refused.status_code == 403, refused.text
        assert refused.json()["policy"] == policy
        assert refused.json()["rule"] == POLICY_RULE
        assert "polic" in refused.json()["error"]
    assert adapter.decision_log(MARKER_DOMAIN.name, name) == ()

    accepted = client.post("/api/v1/decisions", headers=as_("human-token"), json={
        "claim_id": claim_id, "outcome": "accepted", "reason": "membrane on PCF"})
    assert accepted.status_code == 201, accepted.text
    decision = accepted.json()["decision"]
    assert (decision["claim_id"], decision["outcome"], decision["by"], decision["actor_kind"]) \
        == (claim_id, "accepted", "rafael", "human")
    assert decision["run"], "a decision made through the API cites its run"
    assert [d.uid for d in adapter.answers(MARKER_DOMAIN.name, name)] == [
        decision["decision_id"]]


def test_an_agent_under_an_approved_policy_decides_as_itself(client, unique):
    """Once a human approves the policy, the agent's decision succeeds."""
    name = subject(f"POLICY-{unique}")
    claim_id = client.post("/api/v1/proposals", json=proposal(name),
                           headers=as_("human-token")).json()["proposal"]["claim_id"]
    decided = client.post("/api/v1/decisions", headers=as_("approved-token"), json={
        "claim_id": claim_id, "outcome": "rejected", "reason": "synthetic"})
    assert decided.status_code == 201, decided.text
    row = adapter.decision_log(MARKER_DOMAIN.name, name)[-1]
    assert (row.by, row.actor_kind, row.agent_model, row.policy_version) == (
        "policy-agent", "agent", "synthetic-model", "api-test-policy")


def test_an_agent_may_propose_and_the_claim_names_it(client, unique):
    name = subject(f"PROPOSE-{unique}")
    made = client.post("/api/v1/proposals", json=proposal(name), headers=as_("agent-token"))
    assert made.status_code == 201, made.text
    got = made.json()["proposal"]
    assert (got["proposed_by"], got["actor_kind"], got["agent_model"]) == (
        "curator-agent", "agent", "synthetic-model")
    assert got["triage"] == {"label": "skim", "reason": "one clear answer"}
    assert got["reasoning"] == "membrane stain"
    assert got["evidence"]["text"] == f"{name} stains the membrane on PCF"
    assert reads.by_run(got["run"])[0].uid == got["claim_id"]

    again = client.post("/api/v1/proposals", headers=as_("agent-token"), json=proposal(
        name, reasoning="a second thought", triage={"label": "review", "reason": "unsure"},
        alternatives=[{"value": "nuclear", "confidence": 0.2}]))
    assert again.status_code == 200, "a repeat writes nothing and says so"
    repeat = again.json()["proposal"]
    assert repeat["claim_id"] == got["claim_id"]
    assert (repeat["reasoning"], repeat["triage"], repeat["alternatives"]) == (
        got["reasoning"], got["triage"], got["alternatives"]), \
        "a repeat answers with the working the queue serves, not the one it discarded"


def test_an_anonymous_write_is_refused(client, unique):
    name = subject(f"ANON-{unique}")
    for headers in ({}, as_("not-a-token")):
        refused = client.post("/api/v1/proposals", json=proposal(name), headers=headers)
        assert refused.status_code == 401
    assert adapter.claim_log(MARKER_DOMAIN.name, name) == ()


def test_only_a_human_may_be_named_as_directing_a_proposal(client, unique):
    name = subject(f"DIRECTED-{unique}")

    def post(director: str):
        return client.post("/api/v1/proposals", json=proposal(name), headers={
            **as_("agent-token"), DIRECTED_BY_HEADER: f"Bearer {director}"})

    by_agent = post("agent-token")
    assert by_agent.status_code == 403, by_agent.text
    assert by_agent.json()["rule"] == DIRECTED_BY_RULE
    assert post("not-a-token").status_code == 401
    assert adapter.claim_log(MARKER_DOMAIN.name, name) == ()

    made = post("human-token")
    assert made.status_code == 201, made.text
    got = made.json()["proposal"]
    assert (got["proposed_by"], got["directed_by"]) == ("curator-agent", "rafael")
    assert ln.Run.filter(uid=got["run"]).one().params["directed_by"] == "rafael"


def test_without_the_directed_by_header_a_proposal_names_no_director(client, unique):
    made = client.post("/api/v1/proposals", json=proposal(subject(f"UNDIRECTED-{unique}")),
                       headers=as_("agent-token"))
    assert made.status_code == 201, made.text
    got = made.json()["proposal"]
    assert got["directed_by"] is None
    assert "directed_by" not in (ln.Run.filter(uid=got["run"]).one().params or {})


PROVENANCE = ("domain", "predicate", "source", "proposed_by", "actor_kind", "agent_model",
              "agent_model_version", "policy_version", "schema_version")


def _cli(raw: str, store, tmp_path, branch: str = BRANCH):
    return session.curate_all([raw], store=store, branch=branch, snapshots=tmp_path, by="rafael",
                              ask=lambda prompt: session.LATER, today=TODAY,
                              fetch=fixture_transport(), say=lambda line: None)


def test_an_api_proposal_is_indistinguishable_in_the_ledger_from_a_cli_one(client, tmp_path):
    """A claim proposed through the API matches the CLI's in content, provenance and run, and the
    CLI finds it already standing."""
    cd8a, foxp3 = subject("CD8A"), subject("FOXP3")
    memory = MemoryClaimStore(MARKER_DOMAIN)
    _cli("CD8A", memory, tmp_path / "memory", branch="")
    (claim,) = memory.held(cd8a)

    made = client.post("/api/v1/proposals", headers=as_("human-token"), json={
        "domain": MARKER_DOMAIN.name, "subject": cd8a, "predicate": str(claim.predicate),
        "value": claim.value, "confidence": claim.confidence,
        "evidence": {"source": str(claim.source), "source_id": claim.source_id,
                     "source_version": claim.source_version, "text": claim.evidence}})
    assert made.status_code == 201, made.text
    api_row = adapter.claim(made.json()["proposal"]["claim_id"])

    ledger = LedgerClaimStore(BRANCH, MARKER_DOMAIN, **HUMAN)
    cli_run, refused = _cli("FOXP3", ledger, tmp_path / "ledger")
    assert refused == []
    (cli_row,) = adapter.claim_log(MARKER_DOMAIN.name, foxp3)

    assert (api_row.subject, api_row.value, api_row.source_id, api_row.source_version,
            api_row.evidence, api_row.confidence) == (
        claim.subject, claim.value, claim.source_id, claim.source_version, claim.evidence,
        claim.confidence)
    assert {f: getattr(api_row, f) for f in PROVENANCE} == \
        {f: getattr(cli_row, f) for f in PROVENANCE}

    runs = {row.run_uid: ln.Run.filter(uid=row.run_uid).one() for row in (api_row, cli_row)}
    assert cli_row.run_uid == cli_run.uid and api_row.run_uid not in (None, cli_run.uid)
    assert {run.transform.key for run in runs.values()} == {session.CURATION_KEY}
    assert {run.branch.name for run in runs.values()} == {BRANCH}
    assert reads.by_run(api_row.run_uid) == (api_row,)

    _cli("CD8A", ledger, tmp_path / "again")
    assert adapter.claim_log(MARKER_DOMAIN.name, cd8a) == (api_row,), \
        "the ledger holds the API's proposal and the CLI's as one claim"


def test_the_queue_is_in_review_order(client, unique):
    bodies = [
        proposal(subject(f"QUEUE-{unique}-a"), confidence=0.95,
                 triage={"label": "skim", "reason": "clear"}),
        proposal(subject(f"QUEUE-{unique}-b"), confidence=0.6,
                 triage={"label": "review", "reason": "close"}),
        proposal(subject(f"QUEUE-{unique}-c"), confidence=0.3, triage=None),
        proposal(subject(f"QUEUE-{unique}-d"), confidence=0.7,
                 triage={"label": "scrutinise", "reason": "a promoted rule demoted it"}),
        proposal(subject(f"QUEUE-{unique}-e"), confidence=0.55, alternatives=[
            {"value": "cyto_structural", "confidence": 0.5}], triage=None),
    ]
    ids = {}
    for body in bodies:
        made = client.post("/api/v1/proposals", json=body, headers=as_("agent-token"))
        assert made.status_code == 201, made.text
        ids[body["subject"]] = made.json()["proposal"]["claim_id"]

    seen, cursor = [], None
    while True:
        page = client.get("/api/v1/queue", headers=as_("human-token"), params={
            "domain": MARKER_DOMAIN.name, "limit": 3, **({"next": cursor} if cursor else {})})
        assert page.status_code == 200, page.text
        seen += [item["claim_id"] for item in page.json()["items"]]
        cursor = page.json()["next"]
        if cursor is None:
            break
    ours = [claim_id for claim_id in seen if claim_id in ids.values()]
    expected = [ids[s] for s, _ in review_order((b["subject"], _proposal(b)) for b in bodies)]
    assert ours == expected


def test_lineage_is_the_documented_graph(client, unique):
    name = subject(f"LINEAGE-{unique}")
    claim_id = client.post("/api/v1/proposals", json=proposal(name),
                           headers=as_("agent-token")).json()["proposal"]["claim_id"]
    decision_id = client.post("/api/v1/decisions", headers=as_("human-token"), json={
        "claim_id": claim_id, "outcome": "accepted", "reason": "membrane"},
    ).json()["decision"]["decision_id"]

    graph = client.get(f"/api/v1/lineage/{MARKER_DOMAIN.name}/{name}",
                       headers=as_("human-token"))
    assert graph.status_code == 200, graph.text
    nodes, edges = graph.json()["nodes"], graph.json()["edges"]
    assert all({"id", "kind", "label"} <= set(n) for n in nodes)
    assert all(set(e) == {"from", "to"} for e in edges)
    by_kind = {n["kind"]: n["id"] for n in nodes}
    assert set(by_kind) == {"source", "claim", "decision", "entity"}
    assert not any("data" in n for n in nodes if n["kind"] == "source"), (
        "a source read from a proposal with no profile carries no data (amendment v4)")
    assert by_kind["claim"] == claim_id, "a claim node's id is the claim id"
    assert by_kind["decision"] == f"decision:{decision_id}"
    assert {(e["from"], e["to"]) for e in edges} == {
        (by_kind["source"], by_kind["claim"]),
        (by_kind["claim"], by_kind["decision"]),
        (by_kind["decision"], by_kind["entity"]),
    }


SHEET = Path(__file__).parents[1] / "tests" / "fixtures" / "sheets" / "io_panel.csv"


def sheet_bodies(key: str) -> tuple[list[dict], dict]:
    """One proposal body per claimed column of the fixture sheet."""
    name, rows = read_rows(SHEET)
    mapping = map_columns(key, columns_of(rows, sheet=name), source_version="v1")
    sheet = source_profile(mapping, file=SHEET.name, content_hash="fixture-hash")
    bodies = []
    for column, proposal, claim in mapping.placed:
        bodies.append({
            "domain": REAGENT_MAPPING_DOMAIN.name, "subject": key,
            "predicate": str(claim.predicate), "value": claim.value,
            "evidence": {"source": str(claim.source), "source_id": claim.source_id,
                         "source_version": claim.source_version, "text": claim.evidence},
            "confidence": claim.confidence, "reasoning": proposal.reasoning,
            "reasons": [r.to_json() for r in proposal.reasons],
            "alternatives": [{"value": a.value, "confidence": a.confidence,
                              "reasoning": a.reasoning} for a in proposal.alternatives],
            "column": column_profile(column, None if proposal.transform else proposal.value),
            "source_profile": sheet,
            "triage": {"label": proposal.triage.label.value,
                       "reason": proposal.triage.reason}})
    return bodies, sheet


def test_a_sheet_lineage_carries_its_columns_reasons_and_slots(client, unique):
    key = subject(f"io-panel-{unique}.csv", MAPPING_TYPES.ARTIFACT)
    bodies, sheet = sheet_bodies(key)
    made = {}
    for body in bodies:
        answer = client.post("/api/v1/proposals", json=body, headers=as_("agent-token"))
        assert answer.status_code == 201, answer.text
        made[body["evidence"]["source_id"]] = answer.json()["proposal"]
    catalogue = next(b for b in bodies if b["predicate"] == "catalogue_number")
    echoed = made[catalogue["evidence"]["source_id"]]
    assert echoed["reasons"] == catalogue["reasons"], "the echo returns the typed reasons"
    client.post("/api/v1/decisions", headers=as_("human-token"), json={
        "claim_id": echoed["claim_id"], "outcome": "accepted", "reason": "catalogue column"})

    queued = [i for i in client.get("/api/v1/queue", params={"limit": 500},
                                     headers=as_("human-token")).json()["items"]
              if i["subject"] == key]
    assert queued and all(i["reasons"] for i in queued), "the queue reads the reasons back"

    graph = client.get(f"/api/v1/lineage/{REAGENT_MAPPING_DOMAIN.name}/{key}",
                       headers=as_("human-token")).json()
    nodes = {n["id"]: n for n in graph["nodes"]}
    edges = {(e["from"], e["to"]) for e in graph["edges"]}

    # The claim node keeps its negative and not-applicable reasons.
    claim = nodes[echoed["claim_id"]]["data"]
    outcomes = {r["outcome"] for r in claim["reasons"]}
    assert {"held back", "not applicable", "passed"} <= outcomes
    assert any(r["weight"] < 0 for r in claim["reasons"] if r["weight"] is not None)
    assert (claim["actor_kind"], claim["proposed_by"], claim["outcome"]) == (
        "agent", "curator-agent", "accepted")

    # Every column is on the graph, claimed or not.
    columns = {n["data"]["address"]: n for n in nodes.values()
               if n.get("data", {}).get("level") == "column"}
    (file_node,) = [n for n in nodes.values() if n.get("data", {}).get("level") == "file"]
    assert file_node["kind"] == "source"
    assert (file_node["data"]["rows"], file_node["data"]["content_hash"]) == (
        sheet["rows"], "fixture-hash")
    assert len(columns) == sheet["columns"]
    assert all((file_node["id"], n["id"]) in edges for n in columns.values())
    for body in bodies:
        assert columns[body["evidence"]["source_id"]]["data"] == {
            "level": "column", **body["column"], "claimed": True}
    (notes,) = [n for n in columns.values() if not n["data"]["claimed"]]
    assert notes["data"]["header"] == "Notes" and notes["data"]["reason"]

    entity = nodes[f"entity:{REAGENT_MAPPING_DOMAIN.name}:{key}"]["data"]
    assert (entity["filled"], entity["of"]) == (1, len(REAGENT_MAPPING_DOMAIN.reviewed_predicates))
    assert [s["values"] for s in entity["slots"] if s["filled"]] == [[catalogue["value"]]]


def test_a_malformed_column_profile_is_refused_before_any_write(client, unique):
    key = subject(f"io-panel-bad-{unique}.csv", MAPPING_TYPES.ARTIFACT)
    body = sheet_bodies(key)[0][0]
    answer = client.post("/api/v1/proposals", headers=as_("agent-token"),
                         json={**body, "column": {**body["column"], "rows": "ten"}})
    assert answer.status_code == 422 and "column.rows" in answer.json()["error"]
    assert adapter.claim_log(REAGENT_MAPPING_DOMAIN.name, key) == ()


def test_a_release_freezes_the_approved_set_and_cites_its_run(client, unique):
    name = subject(f"RELEASE-{unique}")
    claim_id = client.post("/api/v1/proposals", json=proposal(name),
                           headers=as_("agent-token")).json()["proposal"]["claim_id"]
    client.post("/api/v1/decisions", headers=as_("human-token"),
                json={"claim_id": claim_id, "outcome": "accepted", "reason": "fine"})

    cut = client.post("/api/v1/releases", headers=as_("human-token"),
                      json={"domain": MARKER_DOMAIN.name})
    assert cut.status_code == 201, cut.text
    release = cut.json()["release"]
    assert release["run"] and release["by"] == "rafael"
    pinned = {d.claim.uid for d in services.release_contents(release["release_id"])}
    assert claim_id in pinned
    listed = client.get("/api/v1/releases", headers=as_("human-token"),
                        params={"domain": MARKER_DOMAIN.name}).json()["items"]
    assert release["release_id"] in {r["release_id"] for r in listed}


def test_deciding_a_page_does_not_skip_the_next(client, unique):
    """Deciding page by page reaches every claim."""
    ids = set()
    for letter in "abcd":
        made = client.post("/api/v1/proposals", headers=as_("agent-token"), json=proposal(
            subject(f"KEYSET-{unique}-{letter}"), confidence=0.01, alternatives=[],
            triage={"label": "scrutinise", "reason": "first in the queue"}))
        ids.add(made.json()["proposal"]["claim_id"])

    seen, cursor = set(), None
    while ids - seen:
        page = client.get("/api/v1/queue", headers=as_("human-token"), params={
            "domain": MARKER_DOMAIN.name, "limit": 1, **({"next": cursor} if cursor else {})})
        assert page.status_code == 200, page.text
        for item in page.json()["items"]:
            if item["claim_id"] in ids:
                seen.add(item["claim_id"])
                decided = client.post("/api/v1/decisions", headers=as_("human-token"), json={
                    "claim_id": item["claim_id"], "outcome": "rejected", "reason": "keyset"})
                assert decided.status_code == 201, decided.text
        cursor = page.json()["next"]
        if cursor is None:
            break
    assert seen == ids


def test_an_agent_may_not_cut_a_release_and_the_403_names_the_rule(client, unique):
    for token in ("agent-token", "approved-token"):
        refused = client.post("/api/v1/releases", headers=as_(token),
                              json={"domain": MARKER_DOMAIN.name})
        assert refused.status_code == 403, refused.text
        assert refused.json()["rule"] == RELEASE_RULE
        assert "releases belong to the curator role" in refused.json()["error"]
    assert all(r.by not in ("curator-agent", "policy-agent")
               for r in adapter.releases(MARKER_DOMAIN.name))
    cut = client.post("/api/v1/releases", headers=as_("human-token"),
                      json={"domain": MARKER_DOMAIN.name})
    assert cut.status_code == 201, cut.text


COMPOUND = Path(__file__).parents[1] / "tests" / "fixtures" / "sheets" / "compound_panel.csv"
CHAINS = {"target_label": 'before(" (", last=True)', "clone": 'between("(", ")", last=True)',
          "bx_barcode": 'after("-", last=True)'}


def compound_body(key: str, predicate: str, chain: str, **over) -> dict:
    """A proposal reading the compound fixture's "Panel Antibody" column through ``chain``."""
    name, rows = read_rows(COMPOUND)
    column = next(c for c in columns_of(rows, sheet=name) if c.header == "Panel Antibody")
    body = {"domain": REAGENT_MAPPING_DOMAIN.name, "subject": key, "predicate": predicate,
            "value": f"{column.address} | {chain}",
            "evidence": {"source": "spreadsheet", "source_id": column.address,
                         "source_version": "v1", "text": "header 'Panel Antibody'"},
            "confidence": 0.95, "column": column_profile(column),
            "triage": {"label": "skim", "reason": "the proposer is sure"}}
    return {**body, **over}


def test_one_column_is_three_claims_each_shown_applied_to_its_rows(client, unique):
    key = subject(f"compound-{unique}.csv", MAPPING_TYPES.ARTIFACT)
    made = {}
    for predicate, chain in CHAINS.items():
        answer = client.post("/api/v1/proposals", headers=as_("approved-token"),
                             json=compound_body(key, predicate, chain))
        assert answer.status_code == 201, answer.text
        made[predicate] = answer.json()["proposal"]
    assert len({p["claim_id"] for p in made.values()}) == 3
    shown = made["clone"]["transform"]
    assert (shown["produced"], shown["rows"], shown["attention"]) == (9, 11, "skim")
    assert {"input": "ASCT2 (AKYP0176)-BX074", "output": "AKYP0176"} in shown["examples"]

    # A curator changes the transform: previewed, nothing written.
    preview = client.post("/api/v1/transforms/preview", headers=as_("human-token"), json={
        "claim_id": made["clone"]["claim_id"],
        "value": 'compound_panel!B1 | between("(", ")") | trim()'})
    assert preview.status_code == 200, preview.text
    changed = preview.json()["preview"]
    assert (changed["produced"], changed["attention"]) == (9, "review")
    assert {"input": "Histone H3 Phospho (Ser28) (AKYP0060)-BX030",
            "output": "Ser28"} in changed["examples"]
    assert len(adapter.claim_log(REAGENT_MAPPING_DOMAIN.name, key)) == 3

    # The agent accepts what its approved policy covers; rejecting one leaves the others.
    for predicate, outcome in (("target_label", "accepted"), ("clone", "accepted"),
                               ("bx_barcode", "rejected")):
        decided = client.post("/api/v1/decisions", headers=as_("approved-token"), json={
            "claim_id": made[predicate]["claim_id"], "outcome": outcome, "reason": "applied"})
        assert decided.status_code == 201, decided.text
    answered = {d.claim_uid for d in adapter.answers(REAGENT_MAPPING_DOMAIN.name, key)}
    assert answered == {made["target_label"]["claim_id"], made["clone"]["claim_id"]}

    graph = client.get(f"/api/v1/lineage/{REAGENT_MAPPING_DOMAIN.name}/{key}",
                       headers=as_("human-token")).json()
    node = next(n for n in graph["nodes"] if n["id"] == made["clone"]["claim_id"])
    assert node["data"]["transform"]["chain"] == CHAINS["clone"]
    assert node["data"]["transform"]["accepted"] >= 1
    assert key in node["data"]["transform"]["accepted_on"]

    usage = {u["chain"]: u for u in client.get("/api/v1/transforms",
                                               headers=as_("human-token")).json()["items"]}
    assert key in usage[CHAINS["clone"]]["sources"]
    assert CHAINS["bx_barcode"] not in usage or key not in usage[CHAINS["bx_barcode"]]["sources"]


def test_a_bespoke_transform_is_refused_to_an_agent_its_policy_would_otherwise_permit(
        client, unique):
    key = subject(f"compound-bespoke-{unique}.csv", MAPPING_TYPES.ARTIFACT)
    library = client.post("/api/v1/proposals", headers=as_("approved-token"),
                          json=compound_body(key, "clone", CHAINS["clone"]))
    written = client.post("/api/v1/proposals", headers=as_("approved-token"),
                          json=compound_body(key, "target_label",
                                             'pattern("^(.*) \\\\([^()]*\\\\)-")'))
    assert library.status_code == written.status_code == 201, written.text
    # Bespoke code is scrutinised whatever label the proposer sent.
    shown = written.json()["proposal"]
    assert shown["triage"]["label"] == "scrutinise"
    assert shown["transform"]["bespoke"] and shown["transform"]["produced"] == 9

    # The same agent under the same approved policy: the library chain is accepted...
    ok = client.post("/api/v1/decisions", headers=as_("approved-token"), json={
        "claim_id": library.json()["proposal"]["claim_id"], "outcome": "accepted"})
    assert ok.status_code == 201, ok.text
    # The bespoke one is refused, naming the rule; no decision is written.
    refused = client.post("/api/v1/decisions", headers=as_("approved-token"), json={
        "claim_id": shown["claim_id"], "outcome": "accepted", "reason": "0.95"})
    assert refused.status_code == 403, refused.text
    assert refused.json()["rule"] == NeedsAHuman.rule
    assert refused.json()["policy"] == "api-test-policy"
    log = adapter.decision_log(REAGENT_MAPPING_DOMAIN.name, key)
    assert [d.claim_uid for d in log] == [library.json()["proposal"]["claim_id"]]
    # The agent may still reject it, and a human may accept it.
    human = client.post("/api/v1/decisions", headers=as_("human-token"), json={
        "claim_id": shown["claim_id"], "outcome": "accepted", "reason": "read the pattern"})
    assert human.status_code == 201, human.text


def test_an_agent_store_session_refuses_a_bespoke_acceptance_before_any_row(unique):
    key = subject(f"compound-store-{unique}.csv", MAPPING_TYPES.ARTIFACT)
    agent = LedgerClaimStore(BRANCH, REAGENT_MAPPING_DOMAIN, actor="policy-agent",
                             actor_kind="agent", policy_version="api-test-policy",
                             **AGENT_MODEL)
    claim = REAGENT_MAPPING_DOMAIN.claim(
        subject=key, predicate="clone", value='compound_panel!B1 | pattern("AKYP[0-9]+")',
        source="spreadsheet", source_id="compound_panel!B1", evidence="header 'Panel Antibody'")
    with pytest.raises(NeedsAHuman):
        agent.add(claim, outcome="accepted",
                  judgement=Judgement(by="policy-agent", at=TODAY, reason="sure"))
    assert adapter.claim_log(REAGENT_MAPPING_DOMAIN.name, key) == ()


def test_an_edited_transform_is_a_new_claim_derived_from_the_one_it_edits(client, unique):
    key = subject(f"compound-edit-{unique}.csv", MAPPING_TYPES.ARTIFACT)
    original = client.post("/api/v1/proposals", headers=as_("agent-token"),
                           json=compound_body(key, "bx_barcode", CHAINS["bx_barcode"])).json()
    edited = client.post("/api/v1/proposals", headers=as_("human-token"), json={
        **compound_body(key, "clone", CHAINS["clone"], confidence=0.82),
        "column": None, "derived_from": original["proposal"]["claim_id"]})
    assert edited.status_code == 201, edited.text
    assert edited.json()["proposal"]["transform"]["produced"] == 9, "the column came across"
    uids = {c.uid for c in adapter.claim_log(REAGENT_MAPPING_DOMAIN.name, key)}
    assert uids == {original["proposal"]["claim_id"], edited.json()["proposal"]["claim_id"]}
