# Rotating a credential

How to replace any credential in `README.md` without taking the platform down.

For the two GitHub tokens, use `docs/operations.md` section 4.2 instead.

Three facts hold for every rotation below:

- **A secret value never goes on a command line.** Anything in argv reaches shell history and
  `ps` output. Use `IFS= read -rs`, or edit the file in an editor.
- **Rotate through the service where one exists.** An env value usually seeds only the first
  start, so changing the file alone leaves the old password live.
- **Close with `tofu plan` reporting no changes.**

Paths: the hub is `/opt/clinicalomx_data_platform/current/packages/clinicalomxdb/`, and its env
files are `/opt/clinicalomx_data_platform/config/package-env/<stack>.env`.

## The helper

Paste this into whichever shell does the edit, including a root shell. It prompts silently,
passes the value through the environment rather than argv, and keeps the file's permissions.

```bash
setenvkey() {  # usage: setenvkey <file> <KEY>
  local file=$1 key=$2 NEWVAL
  IFS= read -rsp "new value for $key: " NEWVAL; echo
  NEWVAL="$NEWVAL" KEYNAME="$key" awk '
    BEGIN { k = ENVIRON["KEYNAME"]; v = ENVIRON["NEWVAL"] }
    index($0, k "=") == 1 { $0 = k "=" v } { print }
  ' "$file" > "$file.tmp" && cat "$file.tmp" > "$file" && rm "$file.tmp"
}
```

## Order

Rotate in this order. The last group gates recovery of everything else, so it goes last.

| | Credential | Why here |
|---|---|---|
| 1 | LaminHub key, pgAdmin, Grafana, Teams webhook | Self-contained; nothing else breaks |
| 2 | Postgres root password | Several places move together; needs a window |
| 3 | State backend credential, state passphrase, Barbican credential | These gate recovery; last, once the rest is proven |

The VM SSH keypair rotates only with a planned rebuild. See the last section.

## Before you start

Confirm last night's backup ran, on the VM:

```bash
systemctl status pg-backup.service --no-pager
ls -lt /mnt/data/artifacts/backups | head -3
```

Confirm state is reachable and clean, on your workstation:

```bash
make infra-plan
```

No changes expected. Understand any diff before rotating anything.

## 1. Self-contained secrets

### LaminHub API key

1. In LaminHub, generate a new key for the instance owner.
2. On your workstation, set `LAMIN_API_KEY` in `packages/clinicalomxdb/hub-database/.env`, then
   `make infra-apply`. Only the database env secret should change.
3. On the VM:

```bash
sudo -i                     # paste setenvkey in, then:
setenvkey /opt/clinicalomx_data_platform/config/package-env/hub-database.env LAMIN_API_KEY
exit
# the vm-process bootstrap never overwrites an existing .env, so remove its copy first
rm /opt/clinicalomx_data_platform/current/packages/clinicalomxdb/hub-pipelines/prefect/workers/vm-process/.env
sudo /usr/local/bin/lamin-connect.sh
```

4. In LaminHub, revoke the old key.

`lamin-connect.sh` exiting zero is the login test. Then check `systemctl status vm-pool-worker`
and run one vm-pool deployment.

### pgAdmin and Grafana admin passwords

Both seed only the first start, so change them in the service first.

1. Change the password in the pgAdmin or Grafana UI.
2. On your workstation, update `PGADMIN_DEFAULT_PASSWORD` in `hub-database/.env`, or
   `GRAFANA_ADMIN_PASSWORD` in `hub-ops/.env`. Then `make infra-apply`.
3. On the VM, mirror the same edit into that stack's env file, so a container recreation seeds
   the new value.

Log out and back in to confirm.

Grafana can also be reset from the command line, if its build supports it:

```bash
IFS= read -rsp 'new grafana admin password: ' NEWVAL; echo
printf '%s\n' "$NEWVAL" | docker exec -i $(docker ps -qf name=grafana) \
  grafana-cli admin reset-admin-password --password-from-stdin
unset NEWVAL
```

### Teams webhook

1. Regenerate the incoming webhook on the alert channel.
2. On the VM, re-save the block and send the test card:

```bash
IFS= read -rsp 'new webhook URL: ' TEAMS_WEBHOOK_URL; echo
export TEAMS_WEBHOOK_URL
/opt/clinicalomx_data_platform/current/packages/clinicalomxdb/hub-pipelines/prefect/workers/vm-process/.venv/bin/python \
  -m qptiff_pipeline.setup_teams_webhook --test
unset TEAMS_WEBHOOK_URL
```

3. If `/etc/pg-backup-alert.env` exists, update `PG_BACKUP_WEBHOOK_URL` in it with `setenvkey`,
   then `sudo systemctl start pg-backup-alert.service`.
4. Delete the old webhook.

Both messages arriving in the channel is the proof.

## 2. Postgres root password

Every lamin client reconnects, so announce a window first.

The `mlflow` and `prefect` roles hold their own passwords and are untouched by this. Rotate one
of those the same way, with `\password <role>`, then edit that service's one key.

1. Confirm no HPC jobs are in flight and pause schedules that could start one.
2. On the VM, change it through the service. `\password` prompts twice and never puts the value
   in argv, history or the server log:

```bash
docker exec -it $(docker ps -qf name=postgres) psql -U root -c '\password root'
```

3. On your workstation, update `POSTGRES_PASSWORD` in `hub-database/.env`, then `make infra-apply`.
4. On the VM, mirror the edit with `setenvkey`, then `sudo systemctl restart clinicalomxdb.service`.
5. Update the LaminHub instance record so `lamin connect` hands out the new URL. Nothing in this
   repository does this step.
6. Reconnect every client: `sudo /usr/local/bin/lamin-connect.sh` on the VM, and `make lamin-connect`
   on each workstation and dev box.

Then prove it, without printing anything:

```bash
docker ps --format '{{.Names}} {{.Status}}'
curl -s http://localhost:4200/api/health
docker exec -it $(docker ps -qf name=python-lamindb) python -c "import lamindb as ln; print(ln.Storage.df().shape)"
sudo systemctl start pg-backup.service && systemctl status pg-backup.service --no-pager
```

Run one vm-pool flow and one HPC flow to green before closing the window.

## 3. The gates

### State backend credential

```bash
openstack ec2 credentials create
```

Export the new pair as `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`, then prove state access
for **both** roots before deleting anything:

```bash
make infra-plan
make compute-plan
```

Both must report no changes. Then `openstack ec2 credentials delete <old-access-id>`.

### State passphrase

Both roots read `TF_VAR_state_encryption_passphrase`. Do the control plane first, then repeat for
`infra/compute`.

1. In `versions.tf`, add a second pbkdf2 key provider fed by a new variable, point the `aes_gcm`
   chain at it, and keep the old provider as the fallback method.
2. Set both passphrase variables without touching argv, then write state. A no-change apply still
   re-encrypts it:

```bash
IFS= read -rs TF_VAR_state_encryption_passphrase_new
export TF_VAR_state_encryption_passphrase_new
make infra-apply
```

3. Unset the old variable and run `make infra-plan`. Success proves the new passphrase alone works.
4. Remove the fallback block and the temporary variable, move the new value onto
   `TF_VAR_state_encryption_passphrase`, and commit the `versions.tf` cleanup.

### Barbican application credential

```bash
make infra-plan PLAN_ARGS=-replace=openstack_identity_application_credential_v3.barbican
make infra-apply
```

A running VM is unaffected: the credential is read only at first boot, and `user_data` drift is
ignored by design. The baked `user_data` now holds a dead credential, and the next deliberate
rebuild re-renders it. That rebuild's clean boot is the real verification.

## Rebuild-only

The VM SSH keypair forces instance replacement, so rotate it only as part of a planned rebuild:

```bash
make infra-plan PLAN_ARGS=-replace=tls_private_key.deploy
make infra-apply
tofu -chdir=infra/control_plane output -raw ssh_private_key > <keyfile> && chmod 600 <keyfile>
ssh -i <keyfile> ubuntu@<vm-address> echo ok
```

To revoke one leaked key in the meantime, edit `~ubuntu/.ssh/authorized_keys` on the VM.

## After any rotation

Record the date on the vault entry, and close with `make infra-plan` and `make compute-plan`
reporting no changes. If something broke that this runbook did not mention, add it here.
