"""sync-model-aliases: mirror MLflow model aliases into lamindb.

MLflow aliases (e.g. ``production``) are the source of truth. lamindb keeps a ULabel mirror that
``clinicalomx.resolve_weights`` reads. Runs ``clinicalomx.sync_aliases`` per registered model and
reports each alias's ``in_sync`` state. Runs on the VM. Needs ``clinicalomx[models]`` and
``MLFLOW_TRACKING_URI`` (default http://localhost:5000). Idempotent.
"""
from __future__ import annotations

from prefect import flow, get_run_logger

# registered model -> lamindb alias namespace, per objective.
DEFAULT_TARGETS: dict[str, str] = {
    "roi-general": "model_alias_general",
    "roi-core": "model_alias_core",
}


@flow(name="sync-model-aliases", log_prints=True)
def sync_model_aliases(targets: dict | None = None) -> dict:
    """Mirror every MLflow alias of each registered model into lamindb, then report sync state.

    targets: {registered_model: alias_namespace}. Defaults to roi-general and roi-core.
    """
    logger = get_run_logger()

    import os

    os.environ.setdefault("MLFLOW_TRACKING_URI", "http://localhost:5000")

    from clinicalomx import alias_status, sync_aliases

    targets = targets or DEFAULT_TARGETS
    result: dict = {}
    for reg, ns in targets.items():
        try:
            mirrored = sync_aliases(reg, ns)  # {alias: lamindb_uid}
            logger.info(f"{reg}: mirrored {len(mirrored)} alias(es) -> {mirrored}")
            # Sync state per mirrored alias.
            statuses = {a: alias_status(a, reg, ns)["in_sync"] for a in mirrored}
            drifted = [a for a, ok in statuses.items() if not ok]
            if drifted:
                logger.warning(f"{reg}: aliases still out of sync after mirror: {drifted}")
            result[reg] = {"mirrored": mirrored, "in_sync": statuses}
        except Exception as exc:
            logger.error(f"{reg}: sync failed: {type(exc).__name__}: {exc}")
            result[reg] = {"error": f"{type(exc).__name__}: {exc}"}
    return result
