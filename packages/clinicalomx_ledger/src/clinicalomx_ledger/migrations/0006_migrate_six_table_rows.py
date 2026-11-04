"""Copy every six-table row into the three tables, so the answer each question served survives.

What each old row becomes:

- A `ClaimEvent` is a `Claim` with the same uid, columns and append time. It carries
  schema_version 1, no confidence and no policy, because neither existed.
- A decided `VerdictEvent` (approved or rejected) is one `Decision` per claim it cited, with
  approved spelled accepted. The first keeps the verdict's uid, the rest take ``.<n>``. A cited
  claim asserting a different value than the verdict is not cited, and a verdict left with no
  claim asserting its value gets one: a human claim, sourced to the decider, whose evidence
  names the verdict. A decision rests on exactly one claim now, so the value has to be one.
- A `proposed` `VerdictEvent` writes nothing. Its claim has no decision, and a claim with no
  decision IS the review queue; a row saying nobody decided is what this model removes.
- A retracted verdict writes nothing either. Retraction's only read effect was to void the
  verdict, and a verdict that writes no decision is the same answer.
- A `RetractionEvent` on a CLAIM is a rejected `Decision` on it, dated when it occurred. An
  agent's retraction is refused, because an agent decision needs an approved policy and none
  existed; the migration stops and names the row rather than inventing one.
- `ResolvedState` rows are dropped by 0007 with no copy: they were derived, and the answer
  is now a read.

The six-table model answered with the newest verdict per QUESTION; this one answers with every
claim whose newest decision accepts it. So one step reconciles the two: where a question's
newest decided verdict leaves an earlier approved claim no longer the answer, that claim gets
a rejected decision by the same human at the same stated time, and the reason says why. One
divergence is deliberate and is the new rule: a question whose newest verdict was a
re-proposal after a decision keeps serving the decided answer until somebody rejects it.

Idempotent, so a partial rollback and a re-run does not collide on a uid.
"""

from django.db import migrations

ACCEPTED, REJECTED = "accepted", "rejected"
OUTCOME = {"approved": ACCEPTED, "rejected": REJECTED}


def _copy(apps, schema_editor):
    ClaimEvent = apps.get_model("clinicalomx_ledger", "ClaimEvent")
    VerdictEvent = apps.get_model("clinicalomx_ledger", "VerdictEvent")
    VerdictSupport = apps.get_model("clinicalomx_ledger", "VerdictSupport")
    RetractionEvent = apps.get_model("clinicalomx_ledger", "RetractionEvent")
    Claim = apps.get_model("clinicalomx_ledger", "Claim")
    Decision = apps.get_model("clinicalomx_ledger", "Decision")

    order = ("created_at", "id")
    done_claims = set(Claim.objects.values_list("uid", flat=True))
    done_decisions = set(Decision.objects.values_list("uid", flat=True))

    def claim(**row):
        if row["uid"] not in done_claims:
            Claim.objects.create(schema_version=1, confidence=None, policy_version="", **row)
            done_claims.add(row["uid"])
        return Claim.objects.get(uid=row["uid"])

    def decide(**row):
        if row["uid"] not in done_decisions:
            Decision.objects.create(schema_version=1, actor_kind="human", agent_model="",
                                    agent_model_version="", policy=None, **row)
            done_decisions.add(row["uid"])

    copied = {}
    for old in ClaimEvent.objects.order_by(*order):
        copied[old.id] = claim(
            uid=old.uid, domain=old.domain, subject=old.subject, predicate=old.predicate,
            value=old.value, source=old.source, source_id=old.source_id,
            source_version=old.source_version, evidence=old.evidence, proposed_by=old.actor,
            actor_kind=old.actor_kind, agent_model=old.agent_model,
            agent_model_version=old.agent_model_version, run_uid=old.proposed_in_run_uid,
            created_at=old.created_at,
        )

    retracted = set(RetractionEvent.objects.filter(verdict__isnull=False)
                    .values_list("verdict_id", flat=True))
    decided = [v for v in VerdictEvent.objects.order_by(*order)
               if v.to_status in OUTCOME and v.id not in retracted]
    accepted_by: dict[int, set[int]] = {}
    for verdict in decided:
        cited = [copied[link.claim_id] for link in
                 VerdictSupport.objects.filter(verdict=verdict).order_by("id")]
        cited = [c for c in cited if c.value == verdict.value] or [claim(
            uid=f"clm-{verdict.uid}"[:64], domain=verdict.domain, subject=verdict.subject,
            predicate=verdict.predicate, value=verdict.value, source="human",
            source_id=verdict.decided_by, source_version="",
            evidence=(f"decided in verdict {verdict.uid}; migration 0006 found no cited "
                      "claim asserting this value"),
            proposed_by=verdict.decided_by, actor_kind="human", agent_model="",
            agent_model_version="", run_uid=verdict.decided_in_run_uid,
            created_at=verdict.created_at,
        )]
        for n, target in enumerate(cited):
            decide(uid=verdict.uid if n == 0 else f"{verdict.uid}.{n}", claim=target,
                   outcome=OUTCOME[verdict.to_status], kind=verdict.kind,
                   reason=verdict.reason, by=verdict.decided_by,
                   decided_at=verdict.decided_at, run_uid=verdict.decided_in_run_uid,
                   created_at=verdict.created_at)
        accepted_by[verdict.id] = ({c.id for c in cited}
                                   if OUTCOME[verdict.to_status] == ACCEPTED else set())

    for retraction in RetractionEvent.objects.filter(claim__isnull=False).order_by(*order):
        if retraction.actor_kind != "human":
            raise RuntimeError(
                f"retraction {retraction.uid} was made by an agent, and an agent decision "
                "needs an approved policy that the six-table model never recorded. Approve "
                "one and record this retraction as a decision under it by hand, or roll back"
            )
        note = f"; {retraction.note}" if retraction.note else ""
        decide(uid=retraction.uid, claim=copied[retraction.claim_id], outcome=REJECTED,
               kind="", reason=f"retracted ({retraction.kind}): {retraction.reason}{note}",
               by=retraction.actor, decided_at=retraction.occurred_at, run_uid=None,
               created_at=retraction.created_at)

    def identity(c):
        return (c.subject, c.predicate, c.source, c.source_id, c.value, c.source_version)

    heads = {}
    for verdict in decided:
        heads[(verdict.domain, verdict.subject, verdict.predicate)] = verdict
    for (domain, subject, predicate), head in heads.items():
        kept = {identity(c) for c in Claim.objects.filter(id__in=accepted_by[head.id])}
        newest = {}
        for d in (Decision.objects.select_related("claim")
                  .filter(claim__domain=domain, claim__subject=subject,
                          claim__predicate=predicate).order_by(*order)):
            newest[identity(d.claim)] = d
        for key, d in newest.items():
            if d.outcome != ACCEPTED or key in kept:
                continue
            decide(uid=f"{head.uid}-r{d.claim_id}"[:64], claim=d.claim, outcome=REJECTED,
                   kind="",
                   reason=(f"superseded by verdict {head.uid}: the six-table model served "
                           "one answer per question"),
                   by=head.decided_by, decided_at=head.decided_at,
                   run_uid=head.decided_in_run_uid, created_at=head.created_at)

class Migration(migrations.Migration):
    dependencies = [("clinicalomx_ledger", "0005_three_table_model")]

    operations = [migrations.RunPython(_copy, migrations.RunPython.noop)]
