# clinicalomxdb

The hub: the Postgres database, the containers around it, the services beside it, and the web portal that reaches all of it.
It is Docker Compose configuration plus a `Makefile`.
There is nothing to `pip install` except the Prefect worker in `hub-pipelines/prefect/workers/hpc/`.

Run every operation with `make` from this directory or from the repository root.
`make help` lists every target.
To deploy the hub on its VM, follow `deployment.md`.

## Stacks

| Stack | Runs | Start |
|---|---|---|
| `hub-database` | Postgres 18, pgAdmin, the `python-lamindb` shell container, the Explore tab | `make db-up` |
| `hub-pipelines` | MLflow, Prefect, the ROI review app | `make pipelines-up` |
| `hub-ops` | the Caddy portal, Loki, Alloy, cAdvisor, Prometheus, Grafana. VM only | `make ops-up` |
| `hub-storage` | a SLURM emulator and the RDM and scratch mount points. Dev boxes only | `docker compose -f hub-storage/docker-compose.yml up -d` |

Four host systemd units run beside the containers on the VM: `ops-health`, `ops-login`, `ops-ttyd` and `ops-schema-review`.

## Environment files

Each stack reads `<stack>/.env`, copied from `<stack>/environment.example`.
The example says what each key is and where its value comes from.
`make db-up` refuses to start while `hub-database/.env` still holds a `CHANGEME` placeholder.
`make db-env-check`, `make pipelines-env-check` and `make ops-env-check` compare an `.env` with its example.

`hub-database/.env` names three host directories:

| Key | Holds | Dev value |
|---|---|---|
| `DB_DATA_DIR` | the Postgres cluster | `./pgdata` |
| `DB_CERT_DIR` | the TLS pair Postgres serves | `./certs` |
| `LAMIN_HOME` | the lamindb settings, including an API key and a database password | `~/.clinicalomxdb/lamin` |

Do not run any target under `sudo`.

## Run the database on your machine

You need Docker with Compose, `make` and `python3`.

1. Create the environment file, then replace each `CHANGEME` in it:

   ```bash
   cp hub-database/environment.example hub-database/.env
   ```

2. Start the stack and check it:

   ```bash
   make network
   make db-up
   make db-status
   ```

3. Create and connect a local lamindb instance. It needs no LaminHub account and no API key:

   ```bash
   make lamin-local
   docker restart clinicalomxdb_explorer
   ```

On the VM, run `make lamin-connect` instead.
Never run `make lamin-local` on the VM.

## Open the portal

The portal listens only on the VM, so you reach it through an SSH tunnel from your own machine.

```bash
make tunnel          # prints the URL, http://localhost:8080/ by default
make tunnel-close
```

On a fresh clone, `make tunnel` creates `hub-ops/.env`.
Set `OPS_SSH_TARGET` in it to the VM's address and run `make tunnel` again.
The portal has no login: only people with SSH access to the VM reach it.
Its tabs are `/prefect/`, `/mlflow/`, `/review/`, `/build/`, `/explore/`, `/schema-review/`, `/grafana/`, `/pgadmin/`, `/login/` and `/ssh/`.
`docs/runbooks/operations-ui.md` describes each one.

## Common tasks

| Task | Run |
|---|---|
| compare the declared vocabulary with the instance | `make schema-plan`, `make schema-check`, `make schema-review` |
| apply the declared vocabulary | `make schema-apply` |
| rebuild the lamindb image from this checkout's packages | `make db-build` |
| reload a changed service config | `make ops-reload` |
| render the Grafana alert rules from `site.env` | `make ops-alert-rules` |
| add an HPC user after editing `site.yaml` | `uv run scripts/site/render.py` at the root, then `make hpc-users`, `make ops-login`, `make ops-ssh` on the VM |
| preview the portal page with canned data | `make ui-dev` |

`docs/runbooks/` holds the full procedures.

## Storage roots

- Do not register a `/scratch` root from a developer machine.
- New work uses the `storage.hpc_root` form from `site.yaml`.
- `storage.hpc_alias` is a second path to the same bytes. Do not target it, and do not delete its `Storage` record: it holds artifacts.

## Change the configuration

- Mount a config directory, never a single file.
- In a new container that runs lamindb, bind `LAMIN_HOME` at `/root/.lamin`.
- On every bind from a host path, set `create_host_path: false` and give no default.
- Put a refusal on a make target's prerequisite list, never in its recipe.
- Name an example file `environment.example`. After adding a key to one, add it to every existing `.env` by hand.

## Test

```bash
make explorer-test
```

This runs `tests/explorer/` inside the `clinicalomxdb-lamindb:local` image, which `make db-build` builds.
No test touches the real instance.

The HPC worker suite needs no network and no credential:

```bash
cd hub-pipelines/prefect/workers/hpc
uv venv /tmp/hpc-test
uv pip install --python /tmp/hpc-test/bin/python \
    "prefect==3.6.27" "psycopg2-binary>=2.9" "pydantic>=2" pytest importlib_metadata
/tmp/hpc-test/bin/python -m pytest tests -q
```

`.github/workflows/pkg-clinicalomxdb.yml` runs both and checks every compose file renders against its example.
