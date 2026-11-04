# Secrets catalogue

Every credential the platform uses, what uses it, and where its value lives.
This file holds names only. Never write a value here.
To replace a credential, use `rotation-runbook.md`.

## Expiry

| Credential kind | Where its expiry is shown |
|---|---|
| GitHub fine-grained token | <https://github.com/settings/personal-access-tokens>, logged in as the token's owner |
| GitHub classic token | <https://github.com/settings/tokens>, logged in as the token's owner |
| LaminHub API key | the owner's LaminHub account settings |
| cloud allocation | the allocation's page on the cloud dashboard |
| HPC account | the HPC facility's identity service |

The OpenStack EC2 credentials and the tofu-generated keys do not expire.

## GitHub

| Name | Used by | Where the value lives |
|---|---|---|
| `comx-ghcr-read`, classic token, `read:packages` only | `utils-build-environments`, to fetch a new SIF; a rebuilt VM, to pull the review and Build app images | vault `github/comx-ghcr-read`. Copies: Prefect block `ghcr-croi`; the VM's `docker login ghcr.io`; Barbican `<instance>-ghcr-packages-read-pat`, fed by `TF_VAR_ghcr_packages_read_pat` |
| the `gh` login of `ubuntu` on the hub VM | `assemble-release.sh`, to clone a platform release | the VM's `gh` configuration, owned by whoever ran `gh auth login` there. No vault entry: re-run `gh auth login` as the new owner (`docs/operations.md` section 4.3) |
| GitHub Actions `GITHUB_TOKEN` | CI | GitHub issues it per run |

## Hub services

These live in the three hub env files.
Keep each file whole in the vault.

| Vault entry | File on the operator workstation | Copy on the VM | Barbican copy |
|---|---|---|---|
| `env/hub-database.env` | `packages/clinicalomxdb/hub-database/.env` | `config/package-env/hub-database.env` | `<instance>-db-env` |
| `env/hub-pipelines.env` | `packages/clinicalomxdb/hub-pipelines/.env` | `config/package-env/hub-pipelines.env` | `<instance>-pipelines-env` |
| `env/hub-ops.env` | `packages/clinicalomxdb/hub-ops/.env` | `config/package-env/hub-ops.env` | `<instance>-ops-env` |

| Name | File | Used by |
|---|---|---|
| `POSTGRES_PASSWORD` | `hub-database.env` | the Postgres superuser. Every lamindb connection uses it, through the instance record LaminHub holds |
| `LAMIN_API_KEY` | `hub-database.env`, and a copy in the vm-process worker's `.env` | `lamin-connect.sh` at boot. LaminHub issues it to one user |
| the `mlflow` role password, inside `MLFLOW_DB_URL` | `hub-pipelines.env` | MLflow, to reach its database |
| the `prefect` role password, inside `PREFECT_DB_URL` | `hub-pipelines.env` | the Prefect server, to reach its database |
| `PGADMIN_DEFAULT_PASSWORD` | `hub-database.env` | the pgAdmin login |
| `GRAFANA_ADMIN_PASSWORD` | `hub-ops.env` | the Grafana admin login |

`POSTGRES_PASSWORD`, `PGADMIN_DEFAULT_PASSWORD` and `GRAFANA_ADMIN_PASSWORD` take effect only on the service's first start.
Change each one in its service first, then in every copy.

## Retired

Nothing in the tree reads these. **Revoke each one. Do not hand it over and do not mint a replacement.**

- `comx-ci-read`, fine-grained token
- `comx-infra-admin`, fine-grained token
- `comx-pipeline-read`, fine-grained token
- index read pair, `INDEX_READ_ACCESS_KEY_ID` / `INDEX_READ_SECRET_ACCESS_KEY`
- index publish pair, `INDEX_PUBLISH_ACCESS_KEY_ID` / `INDEX_PUBLISH_SECRET_ACCESS_KEY`

Both index pairs are OpenStack EC2 credentials, valid across the whole cloud project. Share them with lab members only.
The old index keeps the wheels it holds for the images still running against it.
Its credentials stay in the private repository. Never copy them to the public one.

## Infrastructure

| Name | Where the value lives |
|---|---|
| `TF_VAR_state_encryption_passphrase`, which unlocks the tofu state | vault `infra/state-passphrase`. **No other copy exists** |
| tofu state backend credential, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`, exported before `tofu init` | vault `infra/tfstate-backend`. Mint one with `openstack ec2 credentials create` |
| OpenStack RC file, the `OS_*` variables | a personal cloud login. To hand over, add a lab member to the cloud project |
| VM SSH key pair, `tls_private_key.deploy` | tofu state. Get a working copy with `tofu output -raw ssh_private_key`. Vault `infra/vm-ssh-key` |
| Barbican application credential, `openstack_identity_application_credential_v3.barbican`, read by a rebuilt VM at first boot | tofu state only |
| `prod.tfvars`, not a secret: it names the release tag, the volumes and the GHCR username | vault `infra/prod.tfvars`, and `infra/control_plane/prod.tfvars` on the operator workstation |

## People and external accounts

The platform stores none of these.

| Name | Used by | Where it lives |
|---|---|---|
| the HPC account in the first line of `packages/clinicalomxdb/hub-pipelines/prefect/workers/hpc/users.conf` | every HPC job | the HPC facility, with the account holder's password and second factor. Typed at the portal, never stored. `docs/operations.md` section 5 moves it to another person |
| the LaminHub account that issued `LAMIN_API_KEY`, and the instance's admins | issuing a new key; adding a collaborator to the instance `lamin.instance` | LaminHub. Add a second admin before the first one leaves |
| Teams webhook, in Prefect block `clinicalomx-teams`, and `PG_BACKUP_WEBHOOK_URL` in `/etc/pg-backup-alert.env` on the VM if that file exists | alerts | vault `teams/webhook`. The URL is the secret. The channel owner can regenerate it in Teams |
