"""Domain joins the resolution key on the read model.

`ResolvedState` was unique per (subject, predicate) across every domain, so two
domains asserting the same pair shared one row and the second domain's verdict
overwrote the first domain's answer through the projector's update_or_create.
Disjoint subject namespaces were a coincidence, not a constraint.

The event tables are untouched: this migrates the MUTABLE projection only, which
is rebuildable from events by `services.rebuild_resolved_state`. The backfill
reads each row's own verdict rather than rebuilding, so it is exact and needs no
operator step after deploy; `updated_at` is deliberately not touched, because
when the answer was decided did not change.
"""

from django.db import migrations, models


def _domain_from_verdict(apps, schema_editor):
    ResolvedState = apps.get_model("clinicalomx_ledger", "ResolvedState")
    for row in ResolvedState.objects.select_related("verdict").iterator():
        row.domain = row.verdict.domain
        row.save(update_fields=["domain"])


def _clear_projection(apps, schema_editor):
    """Empty the read model, so ROLLING BACK can restore the narrower constraint.

    Two domains sharing a (subject, predicate) are exactly what this migration made
    representable, so a reverse that kept the rows could not put the old unique index
    back. Dropping them is legitimate and not data loss: `ResolvedState` is derived,
    and the operator's step after a rollback is `services.rebuild_resolved_state()`,
    which is already in docs/runbook.md.
    """
    apps.get_model("clinicalomx_ledger", "ResolvedState").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("clinicalomx_ledger", "0003_roles")]

    operations = [
        migrations.RemoveConstraint(
            model_name="resolvedstate",
            name="one_answer_per_question",
        ),
        # Placed here so its REVERSE runs after the new constraint is dropped and
        # before the old one is re-added; forward it does nothing. Operations
        # reverse bottom-up, which is what makes the position load-bearing.
        migrations.RunPython(migrations.RunPython.noop, _clear_projection),
        migrations.AddField(
            model_name="resolvedstate",
            name="domain",
            field=models.CharField(db_index=True, default="", max_length=64),
            preserve_default=False,
        ),
        migrations.RunPython(_domain_from_verdict, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="resolvedstate",
            constraint=models.UniqueConstraint(
                fields=("domain", "subject", "predicate"),
                name="one_answer_per_question",
            ),
        ),
    ]
