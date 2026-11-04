"""Concurrent writers lose nothing. uid uniqueness is the only thing the database arbitrates."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from django.db import close_old_connections
from psycopg2 import errors as pgerrors

from clinicalomx_ledger import reads, services
from clinicalomx_ledger.models import Decision, SourceInvalidationEvent
from conftest import AGENT, HUMAN, superuser, table


def test_concurrent_decisions_on_one_claim_all_land_and_the_answer_holds():
    claim = services.create_claim(
        domain="marker", subject="CON-CD8", predicate="segmentation_role",
        value="marker", source="pubmed", source_id="CON-PMID:1",
        evidence="synthetic: concurrency fixture", proposed_by="synthetic-agent", **AGENT,
    )
    failures: list[str] = []

    def concurrent_decide(n: int):
        try:
            services.decide(claim=claim, outcome="accepted", by=f"synthetic-racer-{n}",
                            decided_at=datetime.now(UTC), reason="concurrent", **HUMAN)
        except Exception as caught:
            failures.append(type(caught).__name__)
        finally:
            close_old_connections()

    threads = [threading.Thread(target=concurrent_decide, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert failures == []
    assert Decision.objects.filter(claim=claim).count() == 8
    assert [a.value for a in reads.resolved_state("marker", "CON-CD8", "segmentation_role")] \
        == ["marker"]


def test_concurrent_same_uid_insert_refused_by_uniqueness():
    a, b = superuser(), superuser()
    refusal: list[str] = []
    try:
        with a.cursor() as cur:
            cur.execute(
                f"INSERT INTO {table(SourceInvalidationEvent)} (uid, domain,"
                " schema_version, source, source_id, source_version, reason, note,"
                " actor, actor_kind, agent_model, agent_model_version, occurred_at)"
                " VALUES ('inv-race', 'marker', 1, 'url', 'CON-race', '', 'synthetic',"
                " '', 'racer-a', 'human', '', '', now())"
            )

        def race():
            try:
                with b.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO {table(SourceInvalidationEvent)} (uid, domain,"
                        " schema_version, source, source_id, source_version, reason,"
                        " note, actor, actor_kind, agent_model, agent_model_version,"
                        " occurred_at)"
                        " VALUES ('inv-race', 'marker', 1, 'url', 'CON-race', '',"
                        " 'synthetic', '', 'racer-b', 'human', '', '', now())"
                    )
                b.commit()
                refusal.append("no error")
            except pgerrors.UniqueViolation:
                refusal.append("UniqueViolation")

        racer = threading.Thread(target=race)
        racer.start()
        time.sleep(1.0)
        a.commit()
        racer.join(timeout=10)
        assert refusal == ["UniqueViolation"]
    finally:
        a.close()
        b.close()
