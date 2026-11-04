"""register-qptiffs: register new .qptiff files as lamindb Artifacts, in place.

Runs on the HPC, where the source storage is mounted. Keys are relative to the lamindb Storage
root. Files already registered are skipped. Only folders whose governance policy permits
``ingest`` are registered. check-qptiffs and mint-slides follow.
"""
from __future__ import annotations

# Put the clone's src/ on sys.path: the HPC runs a git clone, not an install.
import sys as _sys
from pathlib import Path as _P

_SRC = str(_P(__file__).resolve().parents[2])
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

import os
from pathlib import Path

from prefect import flow, get_run_logger, task
from prefect.tasks import exponential_backoff


@task(
    retries=4,
    retry_delay_seconds=exponential_backoff(backoff_factor=30),
    log_prints=True,
)
def register_one(qptiff_path: str, parent_storage_root: str) -> str:
    """Register one .qptiff as an Artifact keyed relative to the storage root. Idempotent."""
    import lamindb as ln

    # Never set `Artifact.suffix`: lamindb moves the file on save when it changes. These files are
    # registered in place and are the only copy.
    storage = ln.Storage.get(root=parent_storage_root)
    key = str(Path(qptiff_path).relative_to(parent_storage_root))
    artifact = ln.Artifact(qptiff_path, key=key, storage=storage)
    if artifact._state.adding:
        artifact.save()
        print(f"[register] saved {key} (hash={artifact.hash})", flush=True)
    else:
        print(f"[register] exists {key} (uid={artifact.uid})", flush=True)
    return artifact.uid


# retries: the HPC-to-VM postgres connection can time out at `import lamindb`.
@flow(name="register-qptiffs", log_prints=True, retries=3, retry_delay_seconds=60)
def register_qptiffs(
    source_directory: str,
    parent_storage_root: str,
    suffix: str = ".qptiff",
) -> dict:
    """Register every new .qptiff under source_directory as a lamindb Artifact.

    source_directory: where to scan, recursively. Defaults to the storage root. Narrow it only to
        limit one batch: the governance policy decides what is in scope.
    parent_storage_root: the lamindb Storage root the keys are relative to. Keep it the root, even
        when source_directory is narrowed.
    suffix: what to glob for. Never written to ``Artifact.suffix``.
    """
    logger = get_run_logger()
    import lamindb as ln
    from prefect.runtime import flow_run

    if ln.Storage.filter(root=parent_storage_root).one_or_none() is None:
        raise KeyError(f"lamindb Storage with root={parent_storage_root!r} not found")

        # Open a lamindb Run for this batch, as tracking.track_prefect_run does. The project name
        # repeats states.PROJECT_NAME.
    project_name = "Clinicalomx QPTIFF pipeline."  # keep in sync with PROJECT_NAME
    project = (
        ln.Project.filter(name=project_name).one_or_none()
        or ln.Project(name=project_name).save()
    )
    transform = (
        ln.Transform.filter(key="register-qptiffs", is_latest=True).first()
        or ln.Transform(key="register-qptiffs", type="pipeline").save()
    )
    ln.track(
        transform=transform,
        project=project.name,
        params={
            "prefect_run_id": str(flow_run.id or ""),
            "prefect_run_name": flow_run.name or "",
        },
    )
    try:
        from clinicalomx import governance

        from qptiff_pipeline.domain import governance as declared

        # Load the policy table explicitly: the SIF runs a git clone with no entry point.
        declared.register()

        source = Path(source_directory)
        found = sorted(
            str(f) for f in source.rglob(f"*{suffix}") if ".temp" not in f.parts
        )
        # Governance gate: registering an Artifact is ingest. The policy is resolved from the key,
        # whose first segment is the governed folder.
        qptiffs, denied = [], {}
        for f in found:
            try:
                key = str(Path(f).relative_to(parent_storage_root))
            except ValueError:  # outside the storage root; it has no key, so it cannot be ingested
                denied.setdefault("<outside the storage root>", []).append(f)
                continue
            if governance.is_hidden(key):
                continue  # .Trash-*/ and friends are not governed data
            if governance.permits_ingest(key):
                qptiffs.append(f)
            else:
                folder = governance.folder_of(key) or "<root>"
                denied.setdefault(folder, []).append(key)

        logger.info(f"found {len(found)} .qptiff under {source}; {len(qptiffs)} may be ingested")
        for folder, keys_ in sorted(denied.items(), key=lambda kv: -len(kv[1])):
            basis = governance.policy_for(f"{folder}/x").basis or "no policy declared"
            logger.info(f"  SKIP {folder}: {len(keys_)} not ingestable ({basis})")

        batch = int(os.environ.get("SLURM_CPUS_PER_TASK", "4"))
        uids: list[str] = []
        for i in range(0, len(qptiffs), batch):
            futures = [
                register_one.submit(f, parent_storage_root)
                for f in qptiffs[i : i + batch]
            ]
            uids.extend(fut.result() for fut in futures)
            logger.info(f"processed {min(i + batch, len(qptiffs))}/{len(qptiffs)}")

        logger.info(
            f"register-qptiffs done: {len(uids)} artifact(s); "
            f"{sum(len(v) for v in denied.values())} skipped by policy")
        return {"registered": len(uids), "uids": uids,
                "skipped_by_policy": {k: len(v) for k, v in denied.items()}}
    finally:
        ln.finish()
