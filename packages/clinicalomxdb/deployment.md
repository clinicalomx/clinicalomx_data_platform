# Hub Service Stack Deployment
Brings up and checks the hub service stack on the hub VM: Postgres and LaminDB, Prefect, MLflow, the two web apps, and the operator portal.

The hub VM builds this stack on its own the first time it boots.
These steps check that it did, finish the parts that need a person, and restart or update it later.

Words used here:

- A **container** is a program packaged with everything it needs. Docker starts it from an **image**, the stored package.
- **systemd** is the Linux service manager. A **unit** is one service it starts at boot, such as `clinicalomxdb.service`.
- **SSH** opens a shell on another machine. An **SSH tunnel** forwards a port on your computer to that machine.

## 0. Requirements
On your own computer:
```text
git
make
ssh
```

Access:

- The hub VM, created with `infra/control_plane/deployment.md`.
- The hub VM's address: `hub.public_address` in `site.yaml`.
- Either the hub VM's SSH key from tofu (step 1), or your own public key listed under `extra_ssh_authorized_keys` in the tfvars file.
- An HPC account named in `hpc.users`, with its password and Duo, the HPC's phone approval.

## 1. Save the hub VM's SSH key
On your own computer, in `infra/control_plane`, with the tofu variables loaded (`infra/control_plane/deployment.md` steps 1 to 3):
```bash
tofu output -raw ssh_private_key > ~/.ssh/clinicalomx-hub
chmod 600 ~/.ssh/clinicalomx-hub
ssh-add ~/.ssh/clinicalomx-hub
```
Skip this step if your own key is in `extra_ssh_authorized_keys`.

## 2. Log in to the hub VM
On your own computer:
```bash
ssh ubuntu@<hub.public_address>
```
The first time, ssh asks `Are you sure you want to continue connecting`. Type `yes`.
A correct result is a prompt that starts with `ubuntu@`.
Every step below marked "on the hub VM" runs in this shell.

## 3. Wait for first boot to finish
On the hub VM:
```bash
cloud-init status --wait
```
Cloud-init is the setup script a new VM runs on its first boot.
The command returns only when first boot ends, which can take many minutes. Leave it running.
A correct result is `status: done`.

On `status: error`, read the log and the notes first boot left:
```bash
sudo tail -n 50 /var/log/cloud-init-output.log
cat /etc/motd
```
A line starting `clinicalomx FATAL` names what stopped first boot.
This repository has no procedure to resume a stopped first boot: see [Gaps](#gaps).

## 4. Check the stack is running
On the hub VM:
```bash
systemctl is-active clinicalomxdb.service vm-pool-worker.service pipeline-pool-worker.service hpc-ssh-pool-worker.service pg-backup.timer
docker ps --format '{{.Names}}' | wc -l
curl -s http://localhost:4200/api/health
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:5000/health
```
A correct result: five lines of `active`, then `14`, then `true` from Prefect, then `200` from MLflow.

If `clinicalomxdb.service` prints `failed`, read its log:
```bash
journalctl -u clinicalomxdb.service -b --no-pager
```

## 5. Install the HPC login units
On the hub VM:
```bash
cd /opt/clinicalomx_data_platform/current/packages/clinicalomxdb
make hpc-users
make ops-login
make ops-ssh
```
These are safe to run again.
`socket=DEAD` in the output is expected until step 8.

## 6. Do the manual steps first boot printed
On the hub VM:
```bash
cat /var/log/clinicalomx-provision-manual-steps.txt
```
It lists three steps.
Step 1 of that list is step 8 below.
Steps 2 and 3 of that list have no procedure in this repository: see [Gaps](#gaps).

## 7. Open the portal
The portal is the web page that reaches every hub service. It listens only on the hub VM, so you reach it through an SSH tunnel.

On your own computer, in `packages/clinicalomxdb` of your clone:
```bash
make hub-ops/.env
```
This creates `hub-ops/.env`. If it already exists, make prints `is up to date`, which is fine.
Open `hub-ops/.env` in a text editor, set `OPS_SSH_TARGET=ubuntu@<hub.public_address>`, and save.

```bash
make tunnel
```
A correct result prints `Portal: http://localhost:8080/`.
Open that address in a browser. The page shows a row of health tiles.

## 8. Open the HPC connection
Do `docs/operations.md` section 3.2, steps 3 and 4.
Expect a password prompt and a Duo approval for each HPC user.
A correct result is `Master running`.

## 9. Register the pipelines
Do `packages/clinicalomx_pipelines/qptiff_pipeline/deployment.md`, then `packages/clinicalomx_ml/roi_detection/deployment.md`.

# Restart, update and stop

## Restart the stack
On the hub VM:
```bash
sudo systemctl restart clinicalomxdb.service
```
Then repeat step 4.

## Reboot the hub VM
Do `docs/operations.md` section 3.

## Deploy a new platform tag
Do `docs/operations.md` section 2. It takes about an hour.

## Roll back a platform tag
Do `docs/operations.md` section 2.10.

## Stop the stack
Drain the workers first (`docs/operations.md` section 2.3).
On the hub VM:
```bash
sudo systemctl stop clinicalomxdb.service
docker ps
```
A correct result is an empty list under the `docker ps` header line.
Start it again with `sudo systemctl start clinicalomxdb.service`, then repeat step 4.

## Rebuild the hub VM
Do `docs/operations.md` section 6.4.

# Gaps
- **A first boot that stopped.** This repository has no procedure to resume it.
- **Worker Lamin login.** `vm-pool-worker.service` and `pipeline-pool-worker.service` read the Lamin login from `/opt/clinicalomx_data_platform/shared/runtime/worker-home/.lamin`. First boot creates that directory empty. This repository does not record how to fill it.
- **Volume snapshot timer.** First boot does not install one, and this repository has no procedure for it.
