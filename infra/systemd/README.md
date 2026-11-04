# Hub VM systemd units

The systemd unit files the hub VM runs.
A systemd unit is a service Linux starts at boot and restarts when it stops.
Every path in these files goes through `/opt/clinicalomx_data_platform/current/`, so a unit follows whichever release `current` points at.

Nothing here installs itself.
First boot installs them on a rebuilt VM, through `infra/control_plane/cloud_init.yaml.tftpl`.
After a release changes a unit, install it by hand (`docs/operations.md` section 2.4):

```bash
R=/opt/clinicalomx_data_platform
sudo install -m 644 $R/current/infra/systemd/<unit file> /etc/systemd/system/<unit file>
sudo systemctl daemon-reload
```

Before you replace a live worker unit, compare `sudo systemctl cat <unit>` with the file here.

## Files

These are the only copy. Edit them here:

- `clinicalomxdb.service`
- `pg-backup.service`, `pg-backup.timer`, `pg-backup-alert.service`
- `lamin-connect.sh`

The four worker units are renders of templates in `packages/clinicalomxdb/hub-pipelines/prefect/workers/`.
Change the template and the render together.

| File here | Template | Substitutions |
|---|---|---|
| `vm-pool-worker.service` | `vm-process/vm-pool-worker.service` | `@RUN_USER@=ubuntu`, `@RUN_HOME@=/home/ubuntu`, `@PREFECT_DIR@=` the `current` Prefect path, and the `HOME` override below |
| `pipeline-pool-worker.service` | `vm-pipeline/pipeline-pool-worker.service` | the same, and the `HOME` override below |
| `hpc-ssh-pool-worker.service` | `hpc/hpc-ssh-pool-worker.service` | the same three. `HOME` stays `/home/ubuntu`, which holds the HPC login sockets |
| `hpc-ssh-pool-worker@.service` | `hpc/hpc-ssh-pool-worker@.service` | the same three. `%i` stays a systemd instance name |

A worker's bootstrap script installs its own copy of the unit.
After you run a bootstrap, install the unit from here over it.

## The worker `HOME` override

`vm-pool-worker.service` and `pipeline-pool-worker.service` set `HOME` to `/opt/clinicalomx_data_platform/shared/runtime/worker-home`.
Both workers share that directory, and it holds their lamindb settings.
On a rebuilt VM it starts empty: seed its `.lamin` by hand, as `/etc/motd` lists.

## Installed from elsewhere

- The four `ops-*` portal units: their bootstraps under `packages/clinicalomxdb/hub-ops/` render them. Run each bootstrap from the `current/` path.
- The review app: it is a compose service in `packages/clinicalomxdb/hub-pipelines/docker-compose.yml`.
- The env files: `scripts/release/assemble-release.sh` links them to `/opt/clinicalomx_data_platform/config/package-env/`.
