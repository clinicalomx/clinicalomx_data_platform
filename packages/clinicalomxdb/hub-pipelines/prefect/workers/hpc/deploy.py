"""Create or update the Prefect blocks and work pools for the HPC worker. Safe to rerun.

Usage: PREFECT_API_URL=http://localhost:4200/api python deploy.py

Reads the SLURM account, partition, QoS, container binds and primary HPC user from site.env.
"""

from __future__ import annotations

import asyncio
import os

from prefect.blocks.core import Block
from prefect.client.orchestration import get_client
from prefect.client.schemas.actions import WorkPoolCreate
from prefect.exceptions import ObjectNotFound

from connection import SSHConnection
from worker import HpcSlurmSSHPushWorker

# Must match the ControlPath in the host's ssh_config and in hub-ops/health. ssh expands %r
# remote user, %h resolved hostname, %p port.
CONTROL_PATH_TEMPLATE = os.environ.get(
    "HPC_CONTROL_PATH", "~/.ssh/cm-%r@%h:%p"
)

# The per-user pool is POOL_PREFIX-USER; the bare prefix is the shared pool.
POOL_PREFIX = os.environ.get("HPC_POOL_PREFIX", "hpc-ssh-pool")


async def ensure_block(block: Block, name: str, *, reconcile: tuple[str, ...] = ()) -> bool:
    """Create the named block, or rewrite it when a field named in `reconcile` differs.

    Returns True if it was written. Only named fields are compared: SecretStr fields always differ
    when dumped.
    """
    slug = type(block).get_block_type_slug()
    async with get_client() as client:
        try:
            existing = await client.read_block_document_by_name(name, slug)
        except ObjectNotFound:
            existing = None

    if existing is None:
        await block.asave(name)
        print(f"[deploy] {slug}/{name} created")
        return True

    drift = {
        key: (existing.data.get(key), getattr(block, key))
        for key in reconcile
        if existing.data.get(key) != getattr(block, key)
    }
    if not drift:
        print(f"[deploy] {slug}/{name} already correct - skipping")
        return False

    for key, (was, now) in drift.items():
        print(f"[deploy] {slug}/{name} drift: {key}: {was!r} -> {now!r}")
    await block.asave(name, overwrite=True)
    print(f"[deploy] {slug}/{name} updated")
    return True


def base_job_template() -> dict:
    """The worker's default base job template, with this site's defaults from site.env.

    An existing pool keeps its template; change it with
    `prefect work-pool update <pool> --base-job-template <file>`.
    """
    template = HpcSlurmSSHPushWorker.get_default_base_job_template()
    site = {
        "account": os.environ["CLINICALOMX_SITE_SLURM_ACCOUNT"],
        "partition": os.environ["CLINICALOMX_SITE_SLURM_PARTITION"],
        "qos": os.environ["CLINICALOMX_SITE_SLURM_QOS"],
        "bind_paths": os.environ["CLINICALOMX_SITE_CONTAINER_BINDS"].split(","),
    }
    variables = template["variables"]
    for name, value in site.items():
        variables["properties"][name]["default"] = value
    variables["required"] = [r for r in variables.get("required", []) if r not in site]
    return template


async def ensure_work_pool(name: str, worker_cls: type) -> bool:
    """Create a work pool of the worker's type unless one exists. True if created.

    An existing pool, and its base job template, is never changed.
    """
    async with get_client() as client:
        try:
            await client.read_work_pool(name)
            print(f"[deploy] work-pool/{name} already exists - skipping")
            return False
        except ObjectNotFound:
            pass
        await client.create_work_pool(
            WorkPoolCreate(
                name=name,
                type=worker_cls.type,
                base_job_template=base_job_template(),
            )
        )
    print(f"[deploy] work-pool/{name} ({worker_cls.type}) saved")
    return True


async def main() -> None:
    base = os.environ.get("HPC_HOST", "hpc")
    user = os.environ.get("HPC_USER") or os.environ["CLINICALOMX_SITE_HPC_USER"]
    # The ssh alias, block name and socket are per user. The host ssh_config defines this alias
    # with the same HostName as "hpc", so both share one socket.
    alias = f"{base}-{user}"

    # No ssh_key: the ControlMaster socket a human opened with DUO authenticates.
    # control_master_path is the unexpanded ssh_config template.
    await ensure_block(
        SSHConnection(
            host=alias,
            user=user,
            control_master_path=CONTROL_PATH_TEMPLATE,
        ),
        name=alias,
        reconcile=("host", "user", "port", "control_master_path"),
    )

    # The per-user pool selects whose HPC allocation a deployment uses.
    await ensure_work_pool(f"{POOL_PREFIX}-{user}", HpcSlurmSSHPushWorker)

    # The shared pool, still targeted by name by the pipeline deployments.
    await ensure_work_pool(POOL_PREFIX, HpcSlurmSSHPushWorker)

    if os.environ.get("VERIFY_SSH") == "1":
        conn = await SSHConnection.connect(alias, verify=True)
        print(f"[deploy] verified live SSH to {conn.host}")

    print("[deploy] done")


if __name__ == "__main__":
    asyncio.run(main())
