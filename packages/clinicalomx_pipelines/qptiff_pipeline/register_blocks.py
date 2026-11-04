"""Register the Prefect blocks the pipeline's deployments need.

Creates two blocks, each from its own narrowly scoped GitHub token:
  - ContainerRegistryAuth ``ghcr-croi``  -> credentials for the private croi image pull.
                                            Takes COMX_GHCR_READ: a classic PAT with
                                            read:packages and no repository access.
  - Secret ``github-croi-pat``           -> the token each prefect.yaml git_clone step uses,
                                            with username ``x-access-token``.
                                            Takes COMX_CI_READ: a fine-grained PAT with
                                            Contents: read on those repos only.

Never use one broad token for both: the Prefect server's database is readable by every account on
the HPC's network. Tokens come from a one-shot shell env and are never written to git or a .env.
Re-run with fresh tokens to rotate them.

Run with the shared prefect venv, the only one with hpc-worker installed:

    GHCR_USERNAME=... COMX_GHCR_READ=... COMX_CI_READ=... \\
      PREFECT_API_URL=http://localhost:4200/api \\
      /home/ubuntu/clinicalomxdb/hub-pipelines/prefect/.venv/bin/python \\
      /home/ubuntu/clinicalomx_pipeline/register_blocks.py
"""
from __future__ import annotations

import importlib
import os

from prefect.blocks.system import Secret

#: Where ContainerRegistryAuth lives at run time. hpc-worker installs flat top-level modules, so
#: the importable name is ``connection``. ``tests/test_register_blocks.py`` reads these names.
HPC_DISTRIBUTION = "hpc-worker"
HPC_BLOCK_MODULE = "connection"
HPC_BLOCK_CLASS = "ContainerRegistryAuth"

#: Path hints for the failure message: the shared venv on the hub VM, and the source path.
SHARED_PREFECT_VENV = (
    "/opt/clinicalomx_data_platform/current/packages/clinicalomxdb"
    "/hub-pipelines/prefect/.venv/bin/python"
)
HPC_SOURCE_PATH = "packages/clinicalomxdb/hub-pipelines/prefect/workers/hpc"

GHCR_BLOCK = os.environ.get("GHCR_BLOCK_NAME", "ghcr-croi")
GIT_SECRET = os.environ.get("GIT_SECRET_NAME", "github-croi-pat")


def container_registry_auth_block() -> type:
    """Return the hpc-worker ``ContainerRegistryAuth`` block class.

    Raises ``SystemExit`` naming the interpreter to use and the install command.
    """
    try:
        module = importlib.import_module(HPC_BLOCK_MODULE)
        return getattr(module, HPC_BLOCK_CLASS)
    except (ImportError, AttributeError) as exc:
        raise SystemExit(
            f"cannot import {HPC_BLOCK_CLASS} from the top-level "
            f"`{HPC_BLOCK_MODULE}` module ({exc}).\n"
            f"It is defined by the {HPC_DISTRIBUTION} distribution, which installs flat "
            f"top-level modules (config/connection/worker/deploy) and provides no "
            f"`hpc_worker` package.\n"
            f"On the hub VM, run this script with the SHARED prefect venv, which is the "
            f"only one that has it:\n"
            f"    {SHARED_PREFECT_VENV} {os.path.basename(__file__)}\n"
            f"Elsewhere, install it into the current interpreter first:\n"
            f"    pip install -e {HPC_SOURCE_PATH} --no-deps"
        ) from exc


def main() -> None:
    container_registry_auth = container_registry_auth_block()
    username = os.environ.get("GHCR_USERNAME")
    packages_read_pat = os.environ.get("COMX_GHCR_READ")
    contents_read_pat = os.environ.get("COMX_CI_READ")
    if not username or not packages_read_pat or not contents_read_pat:
        raise SystemExit(
            "set GHCR_USERNAME, "
            "COMX_GHCR_READ (classic PAT, read:packages only, no repo access) and "
            "COMX_CI_READ (fine-grained PAT, Contents: read on the private repos). "
            "Two separate tokens: neither block gets more than it needs."
        )
    container_registry_auth(username=username, password=packages_read_pat).save(
        GHCR_BLOCK, overwrite=True
    )
    print(f"[blocks] container-registry-auth/{GHCR_BLOCK} saved (private image pull)")
    Secret(value=contents_read_pat).save(GIT_SECRET, overwrite=True)
    print(f"[blocks] secret/{GIT_SECRET} saved (git_clone credentials, username x-access-token)")


if __name__ == "__main__":
    main()
