# Prefect Worker Deployment
Installs and starts the three Prefect workers on the hub VM, including the HPC worker.

The hub VM does steps 1 to 4 on its own the first time it boots.
Run them by hand only when a worker is missing or broken.

Words used here:

- **Prefect** is the scheduler that runs the pipelines.
- A **work pool** is a named queue of pipeline runs in Prefect.
- A **worker** is a background process that takes runs from one work pool and starts them.
- **SLURM** is the HPC's job queue. The HPC worker submits each run to SLURM over SSH.
- **systemd** is the Linux service manager. A **unit** is one service it starts at boot.

| Work pool | Unit | Runs its jobs on |
|---|---|---|
| `hpc-ssh-pool` | `hpc-ssh-pool-worker.service`, and one `hpc-ssh-pool-worker@<user>.service` per extra HPC user | the HPC, through SLURM |
| `pipeline-pool` | `pipeline-pool-worker.service` | the hub VM |
| `vm-pool` | `vm-pool-worker.service` | the hub VM |

## 0. Requirements
- A shell on the hub VM as `ubuntu`: `packages/clinicalomxdb/deployment.md` steps 1 and 2.
- The hub stack running: `packages/clinicalomxdb/deployment.md` step 4.
- An HPC account named in `hpc.users`, with its password and Duo.

Every command below runs on the hub VM, as `ubuntu`. Set this first, in the same shell:
```bash
R=/opt/clinicalomx_data_platform
```

## 1. Install the HPC worker and the pipeline worker
On the hub VM:
```bash
bash $R/current/packages/clinicalomxdb/hub-pipelines/prefect/workers/hpc/bootstrap.sh
```
The first run builds a Python environment and takes several minutes.
It is safe to run again.
A correct result ends with two lines:
```text
[bootstrap] done. Final manual step (as ubuntu, NO sudo): ssh hpc echo ok
[bootstrap] note: vm-pool (workers/vm-process) is not deployed here - it needs its own venv + the clinicalomx repo.
```
The `note` line is expected. Step 2 installs `vm-pool`.

## 2. Install the VM worker
On the hub VM:
```bash
PREFECT_API_URL=http://localhost:4200/api \
  PIPELINE_DIR=$R/current/packages/clinicalomx_pipelines \
  bash $R/current/packages/clinicalomxdb/hub-pipelines/prefect/workers/vm-process/bootstrap.sh
```
The first run builds a second Python environment and takes several minutes.
A correct result ends with `[vm-process] done. vm-pool worker installed and started.`
Every `ok` line above it is fine. A `BROKEN` line names a module that did not load.

## 3. Install the release copies of the units
On the hub VM:
```bash
for u in vm-pool-worker.service pipeline-pool-worker.service hpc-ssh-pool-worker.service hpc-ssh-pool-worker@.service; do
  sudo install -m 644 $R/current/infra/systemd/$u /etc/systemd/system/$u
done
sudo systemctl daemon-reload
sudo systemctl restart vm-pool-worker.service pipeline-pool-worker.service hpc-ssh-pool-worker.service
```
This prints nothing when it works.

## 4. Check the workers are running
On the hub VM:
```bash
systemctl is-active vm-pool-worker.service pipeline-pool-worker.service hpc-ssh-pool-worker.service
journalctl -u hpc-ssh-pool-worker.service -n 20 --no-pager
```
A correct result is three lines of `active`, and a log that ends with the worker polling `hpc-ssh-pool`.

## 5. Open the HPC connection
Do `docs/operations.md` section 3.2, steps 3 and 4.
Expect a password prompt and a Duo approval.
A correct result is `Master running`.

## 6. Add a worker for each extra HPC user
Skip this if `hpc.users` names one user.
Do `docs/operations.md` section 5.3, steps 1 and 2.

## 7. Check a run reaches the HPC
The pipelines must be registered first: `packages/clinicalomx_pipelines/qptiff_pipeline/deployment.md`.
Do `docs/operations.md` section 5.5.

# Stop and start

- Stop every worker after running jobs finish: `docs/operations.md` section 2.3.
- Start them again: `docs/operations.md` section 2.5.
- Stop them for a long time: `docs/operations.md` section 7.
- Move the main HPC user: `docs/operations.md` section 5.
