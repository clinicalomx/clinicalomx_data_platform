"""The typed-actor CHECK constraints, and the approved policy an agent decision needs."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.db import close_old_connections
from django.db.utils import IntegrityError
from psycopg2 import errors as pgerrors

from clinicalomx_ledger import services
from clinicalomx_ledger.models import Claim, Decision
from conftest import AGENT, HUMAN, as_role, table

MISTYPED = [
    # agent without a model
    {"actor_kind": "agent", "agent_model": "", "agent_model_version": "1"},
    # agent without a model version
    {"actor_kind": "agent", "agent_model": "m", "agent_model_version": ""},
    # human carrying agent identity
    {"actor_kind": "human", "agent_model": "m", "agent_model_version": "1"},
    {"actor_kind": "daemon"},
]


def _claim(uid: str, proposed_by: str = "synthetic", **typing) -> Claim:
    return Claim.objects.create(
        uid=uid, domain="marker", subject="ACT-CD19", predicate="segmentation_role",
        value="marker", source="url", source_id=f"ACT-{uid}",
        evidence="synthetic: actor typing probe", proposed_by=proposed_by, **typing,
    )


@pytest.fixture(scope="module")
def ruled():
    return _claim("clm-act-ruled", proposed_by="synthetic-agent", **AGENT)


def _decide(claim, **fields):
    return services.decide(claim=claim, outcome="accepted", decided_at=datetime.now(UTC),
                           reason="synthetic", **fields)


@pytest.mark.parametrize("typing", MISTYPED + [{"actor_kind": "human", "proposed_by": ""}])
def test_mistyped_claim_refused(typing):
    with pytest.raises(IntegrityError):
        _claim("clm-act-bad", **typing)
    close_old_connections()


@pytest.mark.parametrize("typing", MISTYPED + [{"actor_kind": "human", "by": ""}])
def test_mistyped_decision_refused(ruled, typing):
    with pytest.raises(IntegrityError):
        _decide(ruled, **{"by": "synthetic", **typing})
    close_old_connections()


def test_typed_human_and_agent_claims_accepted():
    _claim("clm-act-human", proposed_by="synthetic-human", **HUMAN)
    _claim("clm-act-agent", proposed_by="synthetic-agent", **AGENT)


def test_an_agent_cannot_decide_without_a_policy(ruled):
    with pytest.raises(IntegrityError, match="agent_decides_under_an_approved_policy"):
        _decide(ruled, by="synthetic-agent", **AGENT)
    close_old_connections()


def test_an_agent_cannot_decide_under_a_policy_nobody_approved(ruled):
    """Naming a policy is not enough: the name has to be a row a human approved."""
    with pytest.raises(IntegrityError, match="policy"):
        _decide(ruled, by="synthetic-agent", policy_version="ACT-never-approved", **AGENT)
    close_old_connections()


def test_an_agent_decides_under_an_approved_policy(ruled):
    services.approve_policy(version="ACT-policy-1", approved_by="synthetic-human",
                            note="synthetic: agents may accept a surface stain")
    decided = _decide(ruled, by="synthetic-agent", policy_version="ACT-policy-1", **AGENT)
    assert (decided.actor_kind, decided.policy_id) == ("agent", "ACT-policy-1")


def test_a_policy_names_the_human_who_approved_it():
    with pytest.raises(IntegrityError):
        services.approve_policy(version="ACT-anonymous", approved_by="")
    close_old_connections()


def test_the_agent_role_cannot_write_a_decision_at_all(ruled):
    """So an agent session cannot type its own decision human; only the curator role may."""
    agent = as_role("clinicalomx_ledger_agent")
    try:
        with pytest.raises(pgerrors.InsufficientPrivilege):
            with agent.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {table(Decision)} (uid, schema_version, claim_id, outcome,"
                    " kind, reason, by, actor_kind, agent_model, agent_model_version,"
                    " decided_at) VALUES ('dec-role-bad', 2, %s, 'accepted', '', '',"
                    " 'posing-as-human', 'human', '', '', now())",
                    [ruled.id],
                )
    finally:
        agent.close()


def test_the_agent_role_may_propose():
    agent = as_role("clinicalomx_ledger_agent")
    try:
        with agent.cursor() as cur:
            cur.execute(
                f"INSERT INTO {table(Claim)} (uid, schema_version, domain, subject, predicate,"
                " value, source, source_id, source_version, evidence, proposed_by,"
                " actor_kind, agent_model, agent_model_version, policy_version)"
                " VALUES ('clm-role-ok', 2, 'marker', 'ACT-CD8A', 'segmentation_role',"
                " 'marker', 'url', 'ACT-role-probe', '', 'synthetic: grant probe',"
                " 'synthetic-agent', 'agent', 'synthetic-model', '1', '')"
            )
        agent.commit()
    finally:
        agent.close()
    assert Claim.objects.filter(uid="clm-role-ok").exists()
