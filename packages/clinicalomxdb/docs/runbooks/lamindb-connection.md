# Runbook: connecting code to the instance

Connect code to the instance through the lamindb Python API.
Do not compose a Postgres connection URL by hand.

## Connect

In a Python process whose `~/.lamin` is already populated:

```python
import lamindb as ln
ln.connect()
```

From a shell, with the slug from `lamin.instance` in `site.yaml`:

```bash
lamin connect <owner/name>
```

LaminHub returns the instance's database, and lamindb connects with the LaminHub-provisioned `_jwt` role.

## Connect from a host process on the VM

The Prefect workers are host systemd units, and they connect the same way.
`hub-pipelines/prefect/workers/hpc/worker.py` resolves a deployment's `sif_artifact` this way.
`workers/hpc/bootstrap.sh` installs lamindb into the shared worker venv.

- `hub-database/scripts/lamin_connect.sh` sets the database host in `~/.lamin` to `LAMIN_DB_HOST_OVERRIDE`. On the VM that is `postgres`.
- Cloud-init adds this hosts entry, so `postgres` resolves on the host:

  ```text
  127.0.0.1 postgres clinicalomxdb_postgres
  ```

- The postgres container publishes 5432 on the host. The same `~/.lamin` works in a container and on the host.
- Cloud-init writes `~/.lamin` once. It is owned by the worker user, whose `HOME` the unit sets.
- `LAMIN_API_KEY` reaches only the vm-process worker. The other workers cannot re-authenticate if their cached settings become invalid.

## Roles on the cluster

- `POSTGRES_USER` is `root`, the lab-owned superuser. Use it for administrative work.
- LaminHub provisions and rotates `<database>_jwt` and `<database>_read` on the instance database. Compose nothing from them.
- For a raw connection, create a dedicated lab-owned read-only role. None exists yet.

## `worker.env`

The worker units read `hub-pipelines/prefect/worker.env`.
The example is one level down, at `hub-pipelines/prefect/workers/worker.env.example`.
Copy it up one level, or let `workers/hpc/bootstrap.sh` write it.

## Remove the `_DB_URL` key from an older VM

An older VM's `worker.env` may hold a key ending in `_DB_URL`.
It carries the Postgres superuser password, and nothing reads it.

1. On the VM, delete the line:

   ```bash
   sudo -u ubuntu sed -i '/_DB_URL=/d' /opt/clinicalomxdb/hub-pipelines/prefect/worker.env
   ```

2. Decide whether to rotate `POSTGRES_PASSWORD`. The file was written with umask 077 and owned by the worker user. To rotate, follow `infra/secrets/rotation-runbook.md`, section 2.

No worker restart is needed.
