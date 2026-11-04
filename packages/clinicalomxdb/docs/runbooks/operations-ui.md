# Runbook: operations UI

Reach, deploy, check and fix the web services on the control-plane VM.
They all sit behind one portal that you reach through an SSH tunnel.

## Open the portal

On your own machine, not the VM:

1. Open the tunnel. Running it again reports that it is already up.

   ```bash
   make tunnel
   ```

2. Browse to `http://localhost:8080/`.
3. Close the tunnel when you are done:

   ```bash
   make tunnel-close
   ```

The portal is a tab shell.
Each service loads in its own tab and stays loaded.
A fragment such as `/#prefect` selects a tab on load.
To bookmark a page inside a service, use the "open separately" link in the tab bar.
Type the trailing slash on every path.

| Path | What it is |
|---|---|
| `/` | tab shell, opening on the Status panel with the health tiles |
| `/prefect/` | Prefect: flow runs, deployments, work pools |
| `/mlflow/` | MLflow: experiments, runs, model registry |
| `/review/` | the ROI review app |
| `/build/` | the Extract Explorer: what any ROI can be turned into, and a plan for it |
| `/explore/` | query the LaminDB instance through its Python API |
| `/schema-review/` | the last schema review, and the button that renders the next one |
| `/grafana/` | logs, metrics and alerts; opens on Containers |
| `/pgadmin/` | pgAdmin |
| `/login/` | the login relay behind the HPC login widgets; it carries credentials |
| `/ssh/` | the fallback HPC terminal, pinned to one command |
| `/health` | the JSON behind the Status tiles |

Caddy is the reverse proxy: one web server that forwards each path to the right service.
It also serves two ports for clients on the HPC:

| Port and path | Serves |
|---|---|
| `:4200/api*` | Prefect, for flow runs reporting state and workers subscribing to events |
| `:5000/api/*` | MLflow tracking, including the artifact proxy under `/api/2.0/mlflow-artifacts` |
| `:5000/health` | MLflow liveness |

Everything else on those two ports returns 404.
`make ops-down` stops HPC job reporting until Caddy is back.

The portal has no login.
Do not publish the portal port on a non-loopback address.

## Fix a problem

### A path returns 502

The upstream container is down, or it is not on `clinicalomxdb_net`.

1. Check that the container runs: `docker ps`.
2. Check that the service resolves from inside Caddy:

   ```bash
   docker exec clinicalomxdb_caddy wget -qO- http://clinicalomxdb_prefect:4200/api/health
   ```

3. For pgAdmin, check that `hub-database/docker-compose.yml` lists the `clinicalomxdb_net` alias for it, then run `make db-up`.

### A path you just added returns 404

Caddy has not reloaded its config. Run `make ops-reload`.

### A page loads blank, or its assets return 404

The subpath prefix is set the wrong way round.
Prefect, the review app, the Build app and pgAdmin each get their own base path, and the prefix is forwarded intact.
MLflow has no static prefix, and Caddy strips its prefix.
Do not add `--static-prefix` to MLflow. It moves the tracking API and breaks every HPC job.

### A service will not work under a subpath

Give it its own Caddy listener on a second loopback port, and add a second `-L` to the tunnel target.

### MLflow links from the review app point at the wrong host

`MLFLOW_UI_URL` in `hub-pipelines/.env` is stale on the VM.
OpenTofu stores that file in Barbican, the cloud's secret store, and the VM fetches it only at boot.
Edit `/opt/clinicalomxdb/hub-pipelines/.env` on the VM by hand, or boot the VM after a fresh `tofu apply`.

### A curl from the HPC to the VM hangs

A security group, the cloud's firewall rules for the VM, is dropping the packets.

1. Run the test from a compute node, not a login node. See [Verify after a deploy](#verify-after-a-deploy).
2. If a compute node also hangs, get its source address and compare it with `hpc_cidr`:

   ```bash
   srun --partition=general --time=00:05:00 --pty curl -s --max-time 10 https://api.ipify.org
   ```

### `make tunnel` succeeds but nothing answers

The tunnel socket is stale:

```bash
make tunnel-close
make tunnel
```

### `/health` returns 502, or the Status tab says the endpoint is unreachable

On the VM:

```bash
systemctl status ops-health
journalctl -u ops-health -n 40
ls -l /run/ops-health/
curl -fsS --unix-socket /run/ops-health/health.sock http://localhost/health
```

If `/run/ops-health/` is empty while the unit is active, restart Caddy: `make ops-down && make ops-up`.

### A tile shows an HPC socket as failed

As `ubuntu`, never under `sudo`, run:

```bash
ssh hpc true
```

### A tile shows a worker unit inactive

Run `journalctl -u <unit> -n 50`.
If `LoadState` is `not-found`, re-run the worker bootstrap from `../../infra/control_plane/README.md`.

### Per-container metric panels are empty

On the VM, check that cAdvisor sees containers:

```bash
curl -s localhost:8080/metrics | grep -c 'container_memory_working_set_bytes.*name='
```

A count of zero, with a non-zero total, means cAdvisor sees cgroups but not containers.
Check `OPS_DOCKER_ROOT` in `hub-ops/.env`: the Docker data-root, `/mnt/docker` on this host.
Check the containerd socket.
After you change `OPS_DOCKER_ROOT`, remove the named volume with `docker volume rm`.

### `docker logs` returns nothing

Check whether the Loki Docker logging driver was installed.
This stack collects logs with Alloy instead.

### The Agent tab chats but never calls a tool

The model server runs without tool calling.
See [Run a model on your own machine](#run-a-model-on-your-own-machine).

## Deploy a change to the portal

On the VM:

1. Pull as root, then hand ownership back:

   ```bash
   sudo GIT_SSH_COMMAND="ssh -i /root/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new" \
     git -C /opt/clinicalomxdb -c safe.directory=/opt/clinicalomxdb pull --ff-only
   sudo chown -R ubuntu:ubuntu /opt/clinicalomxdb
   ```

2. If the pull adds keys that cloud-init appends to a `.env`, append them by hand. The three per-host paths in `hub-database/.env` are such keys. The procedure is in `../../infra/control_plane/README.md`, under "Updating an ALREADY provisioned VM".
3. From `/opt/clinicalomxdb`, bring each part up:

   ```bash
   make db-up          # picks up the pgAdmin subpath, loopback bind, and shared network
   make pipelines-up   # picks up the Prefect and Streamlit subpath variables
   make ops-up         # Caddy portal, logging and metrics stack
   make ops-health     # the health endpoint host unit (idempotent)
   make hpc-users    # per-user ssh aliases, blocks, pools, workers
   make ops-login      # the login relay behind the portal's login widgets
   make ops-ssh        # the fallback terminal
   ```

4. After you edit a Caddy, Alloy, Prometheus or Grafana config, reload:

   ```bash
   make ops-up        # brings the stack up AND reloads the config-file services
   make ops-reload    # reload on its own
   ```

`make db-up` recreates the pgAdmin container.
Its `pgadmin_data` volume keeps saved server definitions.

`make ops-reload` renders the alert rules template first.
It checks that each container sees the config on disk, and fails if a service rejects its config.
If it prints an error, the service still runs the previous config.

Reload Caddy, do not restart it.
A restart drops in-flight HPC job reporting.
Caddy's global options need `make ops-caddy-recreate`, which briefly drops connections.

### Rules for portal configs

- Mount `hub-ops/caddy/`, `hub-ops/alloy/`, `hub-ops/loki/` and `hub-ops/prometheus/` as directories. Never mount a single config file.
- For a new service that sends `X-Frame-Options: DENY`, turn the header off in the service and add `Content-Security-Policy frame-ancestors 'self'` on that route only. Grafana does this with `GF_SECURITY_ALLOW_EMBEDDING`.

### Check which config Alloy runs

```bash
docker exec clinicalomxdb_grafana \
  curl -s http://clinicalomxdb_alloy:12345/api/v0/web/components | tr ',' '\n' | grep localID
```

`prometheus.scrape.containers` means the current config.
`prometheus.exporter.cadvisor` means the old config, and a reload has not happened.
Use `curl` from the Grafana container for any cross-container check.

### After a reboot

The Caddy container has `restart: unless-stopped` and comes back by itself.
The running VM's `/etc/systemd/system/clinicalomxdb.service` knows only the two original stacks.
For strict boot ordering, sync that file by hand with `../../infra/control_plane/cloud_init.yaml.tftpl`, then run `systemctl daemon-reload`.

## Verify after a deploy

1. From your own machine, with the tunnel up:

   ```bash
   curl -sf localhost:8080/
   curl -sf localhost:8080/prefect/api/health     # returns true
   curl -sf localhost:8080/mlflow/health
   curl -sfo /dev/null -w '%{http_code}\n' localhost:8080/review/
   curl -sfo /dev/null -w '%{http_code}\n' localhost:8080/build/
   curl -sfLo /dev/null -w '%{http_code}\n' localhost:8080/grafana/   # -L: 302 to login is normal
   curl -sfo /dev/null -w '%{http_code}\n' localhost:8080/pgadmin/
   curl -s localhost:8080/health | jq '.status, (.checks | map_values(.summary))'
   ```

2. Open each path in a browser:
   - Prefect loads its run list.
   - MLflow loads its experiment list.
   - The review app connects its Streamlit socket, with no reconnect spinner.
   - An MLflow link from the review app lands on `/mlflow/`, not the VM's external address.
   - The Build tab connects its Streamlit socket. Open a bgnormed core and check that its artifacts read as present, not denied.

3. From an HPC compute node, not a login node, check the firewall. The `hpc` security group admits only `hub.compute_ingress_cidr` from `site.yaml`, the NAT that compute nodes reach the internet through. Add `--account` if you have no default.

   ```bash
   srun --partition=general --time=00:05:00 --mem=1G --pty bash -c '
     curl -sf --max-time 10 http://EXTERNAL_IP:4200/api/health; echo " prefect exit=$?"
     curl -sf --max-time 10 http://EXTERNAL_IP:5000/health;     echo " mlflow  exit=$?"
     timeout 5 bash -c "</dev/tcp/EXTERNAL_IP/5432" && echo "postgres reachable"
     timeout 5 bash -c "</dev/tcp/EXTERNAL_IP/5050" && echo "pgadmin OPEN (wrong)" || echo "pgadmin closed (right)"
     timeout 5 bash -c "</dev/tcp/EXTERNAL_IP/22"   && echo "ssh OPEN (wrong)"     || echo "ssh closed (right)"
   '
   ```

4. Submit one real flow to `hpc-ssh-pool` and confirm it runs.

## The Status tab and `/health`

The Status tiles come from `/health`, served by `ops-health`, a host systemd unit.
It reports five checks:

1. **HPC ControlMaster sockets, per user**: whether the socket file exists and the master is alive. No socket means DUO was not completed since the last reboot. A socket that exists but is dead makes every submission fail. It reports the socket's age.
2. **Host systemd units**: the three untemplated Prefect workers, the login relay, the fallback terminal, and the boot unit that owns both compose stacks. One restart since the previous poll warns, and three fail, even while the unit is active. A short time in the active state is also reported. The first poll after `ops-health` restarts has no baseline. Restarts are counted only while something polls.
3. **Prefect**: API liveness and every work pool's readiness. `NOT_READY` means no worker is polling that pool.
4. **Postgres backup**: the `LAST_BACKUP_FAILED` sentinel and the age of the newest dump.
5. **The connected LaminDB instance**: its identity only.

Click a row to see each unit, pool or account behind the count.
A failing check opens by itself and stays open across the 15-second refresh.
Per-user HPC workers appear on the HPC tab, not here.

Install or restart it, on the VM:

```bash
make ops-health
make ops-health-status
```

- It serves over the unix socket `/run/ops-health/health.sock`, and Caddy bind-mounts that directory.
- It uses only the Python standard library.
- It holds no Docker socket and opens no database connection.
- It cannot report that the host is down.
- The only alert that does not depend on it is the `PG_BACKUP_WEBHOOK_URL` hook in `/etc/pg-backup-alert.env`.

## The schema review tab

`/schema-review/` serves the last page the `ops-schema-review` unit rendered, and a button that renders the next one.
`docs/runbooks/schema-review.md` covers the page and what to do with it.

Install or restart it, on the VM:

```bash
make ops-schema-review
make ops-schema-review-status
```

- It is a host systemd unit that runs `make schema-review`.
- The button takes no value from the request. A second press while a render runs gets 409.
- A failed or empty render leaves the served page as it was, and says so above it.
- Command output goes to the journal: `journalctl -u ops-schema-review`.
- The page lives at `/var/lib/ops-schema-review/page.html` and survives a restart and a reboot.
- The render writes `hub-database/.lavish/ops-schema-review.html`, not the default `.lavish/schema-review.html`.

`tests/explorer/test_schema_review_server.py` tests these rules.

## Logs

Grafana at `/grafana/` queries Loki.
Loki holds both container logs and the host systemd journal for thirty days.

```text
{job="journal", unit="hpc-ssh-pool-worker.service"}
{job="docker", container="clinicalomxdb_mlflow"}
{job=~"journal|docker"} |= "<username>"          both sources at once
```

Grafana Alloy collects both, with `loki.source.journal` for host units and `loki.source.docker` for containers.
Loki's compactor enforces the retention.

### Log lines you can ignore

Loki logs all of these at ERROR level.

- `ingestion rate limit exceeded` from Loki, at first start only. During normal running, cut what is collected. Do not raise the limit again.
- `error inspecting Docker container ... No such container` from Alloy. A short-lived container exited before Alloy reached it.
- `error getting ingester clients ... empty ring` from Loki, in the first seconds after start.
- `error processing requests from scheduler ... context canceled` from Loki's querier, several lines at once. It appears on every restart, usually just before the `empty ring` line.

To confirm a restart has settled, run this query and check that nothing new arrives after the first minute:

```text
{container="clinicalomxdb_loki"} |= "error"
```

Or ask Loki directly:

```bash
docker exec clinicalomxdb_loki wget -qO- localhost:3100/ready
```

### Alert rules

`hub-ops/grafana/alerting/rules.template.yaml` holds four rules.
`make ops-alert-rules` renders it with `site.env` to `hub-ops/grafana/provisioning/alerting/rules.yaml`, which Grafana loads read-only.
Change the rules only in the template.

| Rule | Fires when |
|---|---|
| Postgres backup failed | the sentinel line appears in the journal |
| Unit restarting repeatedly | one unit restarts more than three times in fifteen minutes |
| Unexpected source on API ports | `:4200` or `:5000` gets a hit from outside the allowlist |
| Repeated authentication failures | more than ten across sshd, the relay and Postgres |

The rules have no delivery destination.
While a rule fires, Grafana logs a delivery failure against its built-in mail contact point.

The rules depend on two collection settings.
Keep the relabel in `alloy/config.alloy` that promotes `UNIT` from systemd's restart messages.
Keep the named access loggers in the Caddyfile, `http.log.access.prefect_api` and `mlflow_api`; the source rule keys on `remote_ip`.

## Dashboards

Dashboards are generated:

```text
hub-ops/grafana/dashboards/build.py     the source of truth
hub-ops/grafana/dashboards/*.json       generated, committed
make ops-dashboards                     regenerate
```

Grafana picks up a regenerated dashboard within 30 seconds, with no restart.

An edit made in the Grafana UI is lost on the next container rebuild.
To keep it, export the panel JSON from the UI and fold it into `build.py`.

The portal's Grafana tab opens on the Containers dashboard:

- **Containers table**: every container, last seen, CPU and memory. Click a row to filter the rest of the dashboard. A row that stops updating is a container that has gone.
- **CPU / Memory**: per container, following the same selection.
- **Logs**: the same selection, with a "Log contains" box for substring search.

Refresh is off by default.
A live tail is in Grafana's Explore view, linked at the top of the dashboard.

## Metrics

cAdvisor supplies container CPU and memory.
Alloy scrapes it, Prometheus stores it, and the Containers dashboard shows it.
cAdvisor reads the Docker data-root from `OPS_DOCKER_ROOT` in `hub-ops/.env`.

## The Explore tab

The Explore tab queries the instance through the lamindb Python API.
Use pgAdmin for schema questions and one-off `UPDATE`s.

The sidebar lists:

- **Entity types**: Cohort, Patient, Sample, Slide, ROI and the ROI subtypes, with counts. These are `ln.Record` rows with `is_type=True`, so they are not in the registry list.
- **Registries**: Artifact, Record, Feature, Project and the rest, with row counts.
- **Features**: every Feature with its dtype and, where the dtype names a registry, its permitted values.

The tab has two modes:

- **Build**: forms that become chained `Registry.filter()` calls. It evaluates nothing you type.
- **Console**: a Python expression, evaluated as root in the container. It is not sandboxed. Set `EXPLORER_CONSOLE=off` in `hub-database/.env` to remove it.

### Build a query

1. Click an entity type or a registry. The query runs.
2. Add a condition. Type any part of a field or feature name. Each option is labelled field, relation or feature. An unknown name turns the box red.
3. Click the value box. It offers the distinct values across the rows matched so far.
4. For `in`, enter one value per box. Use "+" to add a box and "x" to drop one.
5. Group conditions. An inner connective joins conditions in a group, and an outer one joins the groups:

   ```text
   group 1                       key icontains .qptiff
   AND
   group 2  match any (OR)       study = STUDY-A
                                 study = STUDY-B
   ```

6. Under **Merge features**, add feature columns: none, all on the result, or a chosen few.
7. Under **Summarise**, run count, describe, unique, nunique, min, max, sum or mean over the whole result. Start with describe on an unfamiliar field.
8. Click **Download CSV**.

Click a result row to see its fields, related objects and provenance.
The tab always shows the equivalent lamindb expression.
Copy it into a notebook, or use "copy link" to save the query.

### How the builder behaves

- After a query, the feature list narrows to the features the matched rows carry, with their observed values. "show all" restores the full list. Changing registry clears it.
- A string feature offers only `exact`, `contains`, `icontains`, `startswith` and `in`.
- A feature that shares a name with a column is shown disabled.
- In an AND group, the value list ignores only the condition you are editing. In an OR group, it ignores the whole group.
- An `in` with no values is refused.
- An OR group that involves a feature refuses past 20000 matched rows.
- Several values in one feature cell are joined into one cell, such as `CamA-01, CamZ-99`.
- Queries are capped at 1000 rows and, on Postgres, a 30-second `statement_timeout`.
- Foreign-key columns are hidden behind "internal columns".
- Nothing polls.

### Export

The export link has the same shape as a query link:

```text
/explore/api/export.csv?registry=Artifact&f=0:key:icontains:.qptiff&cols=uid,key
```

`f` is `<group>:<field>:<lookup>:<value>`, and `gm` is `<group>:<all|any>`.
The group index is optional.
Merged features go in `&merge=name,name`.

- It exports the whole result set, not the displayed page, up to 50000 rows. Past that, write to storage from the console.
- The filename carries the row count. A capped export is named `...-TRUNCATED.csv`.
- Columns and their order follow the screen.
- Aggregate and console results export from the browser.

### Columns and images

- Drag a column header to reorder. The order is remembered per registry. "reset column order" appears next to the row count once one is set.
- A base64 column holding PNG, JPEG, GIF or WEBP renders as thumbnails. Click one to open it full size. Untick "thumbnails" to see the text. TIFF is not rendered.
- A cell above one megabyte is withheld and counted in a warning.
- The CSV export keeps the raw base64.

### The tab says no instance is connected

The `~/.lamin` mount was empty when the server started.
On the VM, cloud-init fills `/home/ubuntu/.lamin`.
After the first connect, restart the server:

```bash
docker restart clinicalomxdb_explorer
```

Off the VM, neither `make lamin-connect` nor `make lamin-local` gives this tab a working instance.

### Change the app code

The app code is a bind mount.
After a change, restart the container; do not rebuild the image:

```bash
docker restart clinicalomxdb_explorer
```

The Agent tab's dependencies come from the image, so they need `make db-build`.

### lamindb trap

`to_dataframe` defaults to `limit=20` and ignores a slice on the queryset, so `qs[:1000].to_dataframe()` returns twenty rows.
Pass `limit=` explicitly.
`tests/explorer/test_limits.py` checks this with more than twenty rows.

## The Agent tab

A prototype chat that builds the query for you.
It refines one query step by step, with the Build tab's own moves: `set_registry`, `add_condition`, `remove_conditions`, `set_match`, `available_fields`, `field_values`, `preview`, `summarise`.

```text
key icontains .qptiff        ->  3 rows; these carry qptiff_study_name and qptiff_scanner
qptiff_study_name = STUDY-A  ->  2 rows; the only scanner left is Vectra
```

- The graph panel shows the four nodes, the active node, entry counts, and a step log of every tool call, its arguments, the text fed back, the running query and the reason for each edge.
- An `exact` or `in` condition also returns related values of that field. Put them in one group with match "any" if they belong.
- The table below the chat is the final query, re-run when the turn ends.
- "open in Build" hands the query to the Build tab.
- "clear" drops the query and the transcript.
- The model receives column names and counts, never row values.
- Context the model gave up is reported in the step log.
- Closing the tab mid-turn stops the run.

### Connect a model

Pick a provider under "model connection". It fills the base URL:

| Provider | Base URL | Key |
|---|---|---|
| Anthropic | `https://api.anthropic.com/v1` | an `sk-ant-...` key, model e.g. `claude-sonnet-5` |
| OpenAI | `https://api.openai.com/v1` | an `sk-...` key |
| local | `http://127.0.0.1:8000/v1` | usually no key |

The browser holds the key and sends it with each request.
The service stores, writes and logs nothing.
Anyone who can reach the portal can read what the browser remembered.
Press "forget" to clear it.
The page holds the conversation, so closing the tab ends it.

### Run a model on your own machine

1. Start vLLM with tool calling:

   ```bash
   vllm serve Qwen/Qwen2.5-7B-Instruct --enable-auto-tool-choice --tool-call-parser hermes
   ```

2. On your own machine, beside `make tunnel`, open the model tunnel:

   ```bash
   make tunnel-model                 # on the operator machine, alongside make tunnel
   make tunnel-model MODEL_PORT=8001 # if the server is not on 8000
   ```

3. In the tab, set the base URL to `http://host.docker.internal:8000/v1`, the model name to the one the server reports at `/v1/models`, and the key to any string.
4. Close the model tunnel when you are done:

   ```bash
   make tunnel-model-close
   ```

The tunnel binds the VM's Docker bridge.
That needs `GatewayPorts clientspecified` in the VM's sshd.
Cloud-init writes it on a new VM (`../../infra/control_plane/cloud_init.yaml.tftpl`).
On a running VM, apply it by hand once, per `../../infra/control_plane/README.md`.

## HPC connections

The HPC tab has one row per connection: the SSH session, and the Prefect worker and pool that submit through it.
A row is ready when the session is live and the account is provisioned.

### Log in

1. On the HPC tab, press **Log in** on your row, or the plus button to add an account.
2. Answer the prompts in the login card. The second factor appears as the HPC asks for it: a push, a passcode field, or a numbered menu.
3. If the card misreads a prompt, use the terminal at `/ssh/`, linked from the card.

The relay writes each response to the SSH client and drops it.
It never stores, returns, logs or writes a response.
The transcript redacts responses of four characters or more.
Sessions live in memory, are capped, and expire on a timer.

### Provision an account

On the VM:

```bash
# add the account to hpc.users in site.yaml, render it (uv run scripts/site/render.py), then
make hpc-users
make hpc-users-status
```

A row that shows "logged in only" has a live session and no worker.
The tab finds sessions from the ControlMaster sockets on disk, and adds the accounts in `users.conf`.

### Verify

1. On the VM:

   ```bash
   ls -l /home/ubuntu/.ssh/cm-*                       # one socket per authenticated account
   curl -s localhost:8080/health | jq '.checks.connections'
   make hpc-users-status
   ```

2. Submit a flow to `hpc-ssh-pool-USER`, and check with `sacct` on the HPC that it ran under that account.
3. Kill the socket with `ssh -O exit hpc-USER`.
4. Check that the row goes red, and that a submission fails with the re-auth message instead of hanging.

To run a flow under a user's allocation, send it to that user's pool, `hpc-ssh-pool-USER`.
Do not remove the shared `hpc-ssh-pool` or its worker.
Deployments in the pipeline repository still target it by name.

## Add the per-user HPC alias to a running VM

The Prefect SSH block stores the alias `hpc-USER`.
Both `hpc` and `hpc-USER` must resolve in `/home/ubuntu/.ssh/config`.
Cloud-init writes `Host hpc hpc-USER` on first boot only.

On the VM:

1. Add the alias and check the master:

   ```bash
   sudo -u ubuntu sed -i 's/^Host hpc$/Host hpc hpc-USER/' /home/ubuntu/.ssh/config
   sudo -u ubuntu ssh -O check hpc-USER      # must report a running master
   ```

2. Check that there is still exactly one socket. Both aliases share it, so you do not log in again.

   ```bash
   ls -l /home/ubuntu/.ssh/cm-*
   ```

3. Pull, then re-register the block:

   ```bash
   cd /opt/clinicalomxdb/hub-pipelines/prefect
   sudo -u ubuntu env PREFECT_API_URL=http://localhost:4200/api \
     .venv/bin/python workers/hpc/deploy.py
   ```

   The first run prints the host changing from `hpc` to `hpc-USER`. Later runs print "already correct".

4. Restart the health endpoint:

   ```bash
   sudo systemctl restart ops-health
   ```

5. Submit one HPC flow.

## MLflow addresses

A process on the VM cannot reach the VM's own floating IP.
`worker.env` therefore carries two addresses:

```text
MLFLOW_TRACKING_URI=http://localhost:5000            what this host's own workers use
MLFLOW_EXTERNAL_TRACKING_URI=http://EXTERNAL_IP:5000  injected into HPC jobs
```

A VM-side flow given the external address hangs for 120 seconds, then reports a connect timeout.
Check this before you raise `MLFLOW_HTTP_REQUEST_TIMEOUT`.

To fix an older `worker.env` that has only one address:

1. On your own machine, get `EXTERNAL_IP`, the VM's floating IP:

   ```bash
   tofu -chdir=../../infra/control_plane output -raw vm_ip
   ```

2. On the VM, replace `EXTERNAL_IP` below with that address, then run:

   ```bash
   sudo -u ubuntu sed -i 's#^MLFLOW_TRACKING_URI=.*#MLFLOW_TRACKING_URI=http://localhost:5000#' \
     /opt/clinicalomxdb/hub-pipelines/prefect/worker.env
   grep -q MLFLOW_EXTERNAL /opt/clinicalomxdb/hub-pipelines/prefect/worker.env \
     || echo 'MLFLOW_EXTERNAL_TRACKING_URI=http://EXTERNAL_IP:5000' \
        | sudo -u ubuntu tee -a /opt/clinicalomxdb/hub-pipelines/prefect/worker.env
   sudo systemctl restart pipeline-pool-worker vm-pool-worker 'hpc-ssh-pool-worker@*'
   ```

## Configuration

`hub-ops/.env` holds the tunnel target, the portal port, the tunnel socket path, the logging bind mounts and the Grafana admin login.

- The Grafana admin login is a secret. It is not in Barbican, so a rebuilt VM has no copy of it.
- `make ops-up` and the boot unit create `hub-ops/.env` from `hub-ops/environment.example` if it is missing.
- `make ops-env-check` refuses the example's placeholder password. The boot unit does not run that check.
- To point the tunnel at a staging VM, edit `hub-ops/.env`.

`hub-pipelines/.env` holds two browser addresses, `OPS_PORTAL_URL` and `MLFLOW_UI_URL`.
Both are `localhost` addresses on your machine.
Nothing on the server and no HPC job reads them.
Keep the port in `OPS_PORTAL_URL` equal to `OPS_PORTAL_PORT` in `hub-ops/.env`.

## Change the portal UI

Work locally with `hub-ops/devserver`:

```bash
make ui-dev              # http://127.0.0.1:8090/
make ui-shots            # captures every state
make ui-fixtures-check   # fails if the fixtures drifted from the real /health shape
```

It refuses to start on the VM.
`hub-ops/devserver/serve.py` describes the fixtures and the inspector.

## Known gaps

- No authentication on the portal or on any service behind it. The tunnel is the only control.
- MLflow and Prefect are published on all interfaces, with no authentication for anything sharing the HPC's NAT address.
- Alerts have no contact point. Only the backup check alerts out of band.
- No service in either compose file sets a memory limit.
- HPC second-factor login is a human step, once per user per rebuild.
- A second user needs their own SSH access to the VM to reach the portal.
