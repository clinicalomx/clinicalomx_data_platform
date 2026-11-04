# Operations

Procedures for running the platform.
Each numbered section is one job: run it top to bottom.
`infra/secrets/README.md` lists every credential and where its value lives.

## Before you start

### Words used here

- **Hub VM**: the one cloud server, `clinicalomxdb-hub` on Nectar, the research cloud. It runs the database, Prefect, MLflow and the portal.
- **Operator workstation**: your own computer, with this repository cloned.
- **HPC**: the university cluster where heavy jobs run. SLURM is its job queue.
- **SSH**: the command that opens a shell on another machine. An *SSH tunnel* forwards a port on your computer to that machine.
- **Prefect**: the scheduler that runs the pipelines.
  A *deployment* is a pipeline registered under a name.
  A *worker* is a background process that picks up runs from a *work pool*, its queue.
  A *block* is a setting or secret stored in Prefect.
- **systemd unit**: a background program Linux starts at boot, such as `vm-pool-worker.service`.
  `systemctl` starts, stops and checks one. `journalctl -u <unit>` shows its log.
- **Container image**: a program packaged with everything it needs. `docker ps` lists the running ones.
  On the HPC an image runs under Apptainer as one `.sif` file, a SIF.
- **Tag**: a fixed name for one git commit. `platform-v<N>` is the code production runs.
- **tofu** (OpenTofu): the tool that creates the cloud server, volumes and firewall rules from `infra/`. Its *state* is its record of what exists.
- **Vault**: the lab's password manager.
- **Barbican**: the cloud's secret store. A rebuilt VM reads its settings from it at first boot.

### How to read a command block

- The first comment names the machine and user, such as `# hub VM, as ubuntu`.
- `R` is `/opt/clinicalomx_data_platform` on the hub VM.
- `<angle brackets>` is a value you supply.
- A dotted name such as `hpc.slurm_account` is a key in `site.yaml`.
- On the hub VM, run as `ubuntu`. Use root only where the line starts with `sudo`.
- Never type a secret on a command line. Read it with `IFS= read -rs VAR`.

### Open the Prefect CLI

```bash
# hub VM, as ubuntu
R=/opt/clinicalomx_data_platform
export PREFECT_API_URL=http://localhost:4200/api
source $R/current/packages/clinicalomxdb/hub-pipelines/prefect/.venv/bin/activate
```

### Open the portal

1. Open the tunnel. It reads `OPS_SSH_TARGET` from your local `hub-ops/.env`.

   ```bash
   # operator workstation, in packages/clinicalomxdb of your clone
   make tunnel
   ```

2. Browse to `http://localhost:8080/`.

### Accounts the platform uses

| Account | What it holds |
|---|---|
| Nectar project | the hub VM, its three volumes, the private package index bucket, the tofu state bucket, Barbican |
| LaminHub | the lamindb instance `lamin.instance`, reached with an API key |
| HPC facility | the HPC user every job runs as (`hpc.users`), SLURM account `hpc.slurm_account`, project storage `storage.hpc_root` (alias `storage.hpc_alias`), research data collections `storage.raw_root` and `storage.raw_scratch_root` |
| GitHub | the private repository `clinicalomx/clinicalomx_data_platform`, 16 private container packages under `clinicalomx`, and the personal access tokens |
| Microsoft Teams | the alert channel behind the Prefect block `clinicalomx-teams` |

## 1. Publish tagged releases

Pushing a tag makes GitHub build a package or an image.

| Tag | What it builds |
|---|---|
| `clinicalomx-v<version>` | the `clinicalomx` wheel on the private index, then a Build app image on that wheel |
| `clinical-roi-v<version>` | the `clinical-roi` wheel, four croi images and four ORAS SIFs |
| `clinicalomx_ledger-v<version>`, `clinicalomx_markers-v<version>`, `clinicalomx_reagents-v<version>` | that wheel on the private index |
| `clinicalomx-pipeline-v<version>` | the pipeline wheel, and a rebuild of the qptiff, bgnorm and extract images at the tags their Dockerfiles pin |
| `platform-v<N>` | nothing. The hub VM assembles it, and both `prefect.yaml` pull steps clone it |

Production changes only when you deploy a platform tag (section 2).
Read the bgnorm and ultralytics paragraphs in [`NOTICE`](../NOTICE) before you make any image public.

### 1.1 Release a package

1. Open a pull request that changes the version:

   | Package | Change |
   |---|---|
   | any package | `version` in its `pyproject.toml`, and `__version__` in its `__init__.py` |
   | `clinicalomx` | also regenerate `docs/reference.md` and `docs/extract-recipes.md` with `python -m clinicalomx.vocab docs` and `python -m clinicalomx.extract docs` |
   | `clinical-roi` | also the `clinical-roi` block in `packages/clinicalomx_ml/uv.lock` and in `packages/clinicalomx_pipelines/uv.lock` |

2. Check the lock files. It must print no `LOCK DRIFT` line.

   ```bash
   # operator workstation, at the pull request head
   for p in packages/clinicalomx_ml packages/clinicalomx_pipelines packages/clinicalomx; do
     (cd "$p" && uv lock --check --offline) || echo "LOCK DRIFT in $p"
   done
   ```

3. Merge, then tag the merge commit:

   ```bash
   # operator workstation
   git fetch origin --tags
   git tag <distribution>-v<version> origin/main
   git push origin <distribution>-v<version>
   gh run list --repo <source.repository> --limit 6
   ```

4. Wait until every run for that tag ends `completed success`. A croi image takes 65 to 80 minutes.

### 1.2 Adopt a new image in production

Do this in one pull request, after section 1.1 has published the image.

1. Read each new image name from the build's manifest:

   ```bash
   # operator workstation
   gh run download --repo <source.repository> -n <manifest artifact> --dir /tmp/manifests
   jq -r '[.image, (.sif_oras_ref // "-")] | @tsv' /tmp/manifests/*.json
   ```

   The manifest artifact is `env-manifests-croi-v<version>`, `env-manifest-extract-<image tag>`, `env-manifest-qptiff-<image tag>` or `env-manifest-bgnorm-<image tag>`.
   GitHub keeps it for 90 days.
   For the extract image, skip this step until step 6.

2. Change these together:

   - the `image:` and `sif_artifact:` pair of each affected deployment, in `packages/clinicalomx_pipelines/qptiff_pipeline/prefect.yaml` and `packages/clinicalomx_ml/roi_detection/prefect.yaml`
   - the same image names in `tools/parity/runtime_defs_check.py`
   - for the extract image only, `ARG CLINICALOMX_VERSION` in `packages/clinicalomx_pipelines/qptiff_pipeline/containers/Dockerfile.extract`

3. Find the old image tag elsewhere:

   ```bash
   # operator workstation, on the pull request branch
   git grep -n '<old image tag>'
   ```

   Change the hits in `packages/clinicalomx_pipelines/qptiff_pipeline/docs/DEPLOYING.md`.
   Leave the hits in container READMEs, workflow files and test fixtures.

4. Set both `branch: platform-v<N>` lines to the platform tag you will cut next (section 1.3).

5. Check the declarations agree. It must print `ALL CHECKS PASS`.

   ```bash
   # operator workstation, at the pull request head
   uv run --with pyyaml python tools/parity/runtime_defs_check.py
   ```

6. Merge.
   For the extract image, wait for the `img-extract` run on `main` to succeed, then read its manifest (step 1) before you cut the platform tag.

### 1.3 Cut a platform tag

1. Find the latest platform tag on the remote. The next one is that number plus one.

   ```bash
   # operator workstation
   git fetch origin --tags
   git ls-remote --tags origin 'platform-v*' | sed 's|.*refs/tags/||' | sort -V | tail -1
   ```

2. Check that both pull steps on `main` name the new tag. It must print exactly one line, naming the new tag.

   ```bash
   git grep -h 'branch: platform-' origin/main -- \
     packages/clinicalomx_pipelines/qptiff_pipeline/prefect.yaml \
     packages/clinicalomx_ml/roi_detection/prefect.yaml | sort -u
   ```

   If it does not, merge a pull request that moves both lines first.

3. Check that CI is green on that commit, then tag it:

   ```bash
   gh run list --repo <source.repository> --commit "$(git rev-parse origin/main)"
   git tag platform-v<N> origin/main
   git push origin platform-v<N>
   ```

4. Deploy it with section 2.

## 2. Deploy a platform tag

This puts a platform tag into production on the hub VM. It takes about an hour.

Each release is a folder under `$R/releases/`.
`$R/current` is a symlink, a pointer to one of those folders.

### 2.1 Record what runs now

1. On the hub VM:

   ```bash
   # hub VM, as ubuntu
   R=/opt/clinicalomx_data_platform
   readlink $R/current
   git -C $R/current status --porcelain
   systemctl list-units 'hpc-ssh-pool-worker@*' --no-pager --no-legend
   ```

   Write down the release `current` names. It is your rollback target.
   `git status` must print nothing.
   Write down each `hpc-ssh-pool-worker@<user>` instance.

2. On your workstation, list what the release changes on the VM:

   ```bash
   # operator workstation
   git fetch origin --tags
   git diff --stat <release current names> platform-v<N> -- packages/clinicalomxdb infra/systemd site.example.yaml
   ```

   Keep the output for step 2.4.

### 2.2 Assemble the release beside the live one

```bash
# hub VM, as ubuntu
R=/opt/clinicalomx_data_platform
$R/current/scripts/release/assemble-release.sh platform-v<N>
git -C $R/releases/platform-v<N> describe --tags --exact-match
$R/releases/platform-v<N>/scripts/release/build-release-envs.sh platform-v<N>
```

- `assemble-release.sh` clones the tag into `$R/releases/platform-v<N>/` with the VM's `gh` login.
  If it fails with `401`, go to section 4.3.
  A `WARNING` about pruning is expected.
- `build-release-envs.sh` builds the three Python environments.
  It reads the package index pair from `hub-database.env`.
  It must end with `[build-envs] done.` and three `ok` lines.

### 2.3 Drain the workers

Draining means letting running jobs finish, then stopping the workers.

1. List what is running:

   ```bash
   # hub VM, as ubuntu, with the Prefect CLI open
   prefect flow-run ls --state Running --state Pending
   ```

2. Wait until the list is empty.

3. Stop every worker. Each line must print `inactive`.

   ```bash
   sudo systemctl stop vm-pool-worker.service pipeline-pool-worker.service \
     hpc-ssh-pool-worker.service 'hpc-ssh-pool-worker@*'
   systemctl is-active vm-pool-worker.service pipeline-pool-worker.service hpc-ssh-pool-worker.service
   ```

### 2.4 Point `current` at the new release

1. Move the link:

   ```bash
   # hub VM, as ubuntu
   R=/opt/clinicalomx_data_platform
   cd $R
   ln -sfn releases/platform-v<N> current
   readlink current
   ```

2. If the step 2.1 diff lists files under `infra/systemd/`, install each changed unit:

   ```bash
   sudo install -m 644 $R/current/infra/systemd/<unit file> /etc/systemd/system/<unit file>
   sudo systemctl daemon-reload
   ```

3. If it lists `site.example.yaml`, add each new key to `$R/config/package-env/site.yaml` and render it (section 8.2, step 2).

4. If it lists files under `packages/clinicalomxdb/`, restart the hub stack and check it:

   ```bash
   sudo systemctl restart clinicalomxdb.service
   docker ps --format '{{.Names}} {{.Status}}' | wc -l
   curl -s http://localhost:4200/api/health
   curl -s -o /dev/null -w '%{http_code}\n' http://localhost:5000/health
   ```

   Expect 14 containers, `true` from Prefect and `200` from MLflow.

### 2.5 Start the workers

Each journal must end with the worker polling its pool.

```bash
# hub VM, as ubuntu
sudo systemctl start vm-pool-worker.service pipeline-pool-worker.service hpc-ssh-pool-worker.service
sudo systemctl start hpc-ssh-pool-worker@<user>.service   # once per instance step 2.1 recorded
journalctl -u hpc-ssh-pool-worker.service -n 20 --no-pager
```

### 2.6 Register the deployments

```bash
# hub VM, as ubuntu, with the Prefect CLI open
cd $R/current/packages/clinicalomx_ml/roi_detection && prefect deploy --all
cd $R/current/packages/clinicalomx_pipelines/qptiff_pipeline && prefect deploy --all
prefect deployment ls
prefect deployment inspect 'data-tests/pipeline-data-tests' | grep -E "'branch'|x-access-token"
```

- The two `deploy` lines register 5 and 29 deployments. `deployment ls` lists 34.
- `inspect` must show `'branch': 'platform-v<N>'` and `'username': 'x-access-token'`. Ignore the `'branch': None` line.
- Do not re-run `automations.py`.

### 2.7 Fetch new environments, if an image moved

Skip this step if the release moved no `image:` line.

1. Copy each new build manifest to HPC scratch:

   ```bash
   # operator workstation; `hpc` is your own ssh alias for the HPC login node
   out=$(mktemp -d)
   gh run download --repo <source.repository> -n <manifest artifact> --dir "$out/raw"
   mkdir "$out/flat"
   for m in $(find "$out/raw" -name '*.json'); do
     img=$(jq -r .image "$m")
     cp "$m" "$out/flat/$(basename "${img%%:*}").json"
   done
   scp "$out"/flat/*.json hpc:<storage.hpc_alias>/env-manifests/
   ```

2. Check every declared image has a manifest. `comm` must print nothing.

   ```bash
   # operator workstation
   for f in packages/clinicalomx_pipelines/qptiff_pipeline/prefect.yaml packages/clinicalomx_ml/roi_detection/prefect.yaml; do
     git show "platform-v<N>:$f"
   done | sed -nE 's/^[[:space:]]*image:[[:space:]]*([^[:space:]]+).*/\1/p' | sort -u > /tmp/declared.txt
   ssh hpc 'jq -r .image <storage.hpc_alias>/env-manifests/*.json' | sort -u > /tmp/scratch.txt
   comm -23 /tmp/declared.txt /tmp/scratch.txt
   ```

3. Fetch the SIFs. Run this only after step 2.6.

   ```bash
   # hub VM, as ubuntu, with the Prefect CLI open
   prefect deployment run 'build-environments/utils-build-environments' --param dry_run=true --watch
   prefect deployment run 'build-environments/utils-build-environments' --watch
   ```

   The dry run must report no row in state `stale`. On a `stale` row, repeat step 1.
   `bgnorm-inference` reporting `unpublished` is expected.

### 2.8 Accept the release

1. Run both data tests and list old releases:

   ```bash
   # hub VM, as ubuntu, with the Prefect CLI open
   prefect deployment run 'data-tests/pipeline-data-tests-vm' --watch
   prefect deployment run 'data-tests/pipeline-data-tests' --watch
   deactivate
   sudo $R/current/scripts/release/prune-releases.sh
   ```

   Both runs must reach `Completed`.
   The HPC run's log must show `Deployment step 'git_clone' completed successfully`.
   If the release moved a croi or extract image, also run one deployment that uses it.

2. `prune-releases.sh` with no flag only reports. When the output looks right, run it again with `--apply`.

On a `REFUSED` line, fix the cause it names. Never delete a release folder by hand.

### 2.9 Record the tag for a rebuild

`prod.tfvars` names the tag a rebuilt VM boots onto. It lives on the operator workstation and in the vault.

1. Find the line:

   ```bash
   # operator workstation, in infra/control_plane
   grep -n 'platform_release_tag' prod.tfvars
   ```

2. Set it to `platform_release_tag = "platform-v<N>"`.
3. Copy the file to the vault entry `clinicalomx/infra/prod.tfvars`.
4. If you hold the tofu credentials (section 6.4), `tofu plan -var-file=prod.tfvars` must report no changes.

### 2.10 Roll back

1. Point `current` at the release step 2.1 recorded.
2. Repeat steps 2.3 to 2.6 from that release.

## 3. Reboot the hub VM

Every service restarts on its own. You must re-open two things: the HPC SSH socket and the portal tunnel.

The HPC SSH socket is one open SSH connection that every HPC job is submitted through.
A person opens it with their password and Duo, a phone approval.
It also closes after 96 hours unused, or when the HPC drops it. Re-open it with section 3.2, step 3.

### 3.1 Before a planned reboot

1. Drain the workers (section 2.3).
2. Reboot:

   ```bash
   # hub VM, as ubuntu
   sudo reboot
   ```

### 3.2 After any reboot

1. Re-open the portal tunnel:

   ```bash
   # operator workstation, in packages/clinicalomxdb
   make tunnel-close
   make tunnel
   ```

2. Check the stack. Every unit must print `active`, and 14 containers must be up.

   ```bash
   # hub VM, as ubuntu
   systemctl is-active clinicalomxdb.service vm-pool-worker.service pipeline-pool-worker.service hpc-ssh-pool-worker.service
   docker ps --format '{{.Names}}' | wc -l
   ```

   If `clinicalomxdb.service` failed, read `journalctl -u clinicalomxdb.service -b --no-pager`.

3. Re-open the HPC socket:

   1. Browse to `http://localhost:8080/#hpc`.
   2. Click **Log in** on the row of the first user in `users.conf`.
   3. Answer the password prompt, then approve the Duo push or type the passcode. The row turns live.
   4. Repeat for every other user row.

   If the login card misreads a prompt, use the terminal at `http://localhost:8080/ssh/`.

   Without the portal, run this on the VM:

   ```bash
   # hub VM, as ubuntu, never with sudo
   ssh hpc echo ok
   ssh hpc-<user> echo ok   # once per other user
   ```

4. Check the socket. It must print `Master running`.

   ```bash
   # hub VM, as ubuntu
   ssh -O check hpc
   ```

5. List runs the reboot cut off:

   ```bash
   # hub VM, as ubuntu, with the Prefect CLI open
   prefect flow-run ls --state Crashed --state Late --limit 20
   ```

   Re-run a crashed run from the Prefect tab if you still need it.
   A late run starts on its own.

## 4. Rotate a token

Rotate each token before it expires.
Expiry dates are at <https://github.com/settings/personal-access-tokens>, logged in as the account that owns the tokens.

### 4.1 Check the clone token

An expired clone token makes every HPC run fail with `Failed to clone repository` and `RPC failed; HTTP 401`, or `Authentication failed`.
The clone token is in the Prefect block `github-croi-pat`.

```bash
# hub VM, as ubuntu, with the Prefect CLI open
prefect deployment inspect 'data-tests/pipeline-data-tests' | grep -E "x-access-token|'branch'"
python - <<'PY'
import urllib.request, urllib.error
from prefect.blocks.system import Secret
req = urllib.request.Request(
    "https://api.github.com/repos/<source.repository>",
    headers={"Authorization": "Bearer " + Secret.load("github-croi-pat").get()},
)
try:
    with urllib.request.urlopen(req) as r:
        print("token accepted; expires:", r.headers.get("GitHub-Authentication-Token-Expiration", "never"))
except urllib.error.HTTPError as e:
    print("token refused:", e.code)
PY
```

| Output | Do |
|---|---|
| `inspect` shows no `'username': 'x-access-token'` | deploy a release from `platform-v33` or later (section 2) |
| `token refused: 401` | rotate the token (section 4.2) |
| `token refused: 404` | mint a new token with the right repository selected (section 4.2) |

### 4.2 Rotate the clone token or the packages token

| GitHub token name | Kind and scope | Prefect block | Other copies |
|---|---|---|---|
| `comx-ci-read` | fine-grained, `Contents: read` on `source.repository` | `github-croi-pat` | Barbican |
| `comx-ghcr-read` | classic, `read:packages` only | `ghcr-croi` | Barbican, and the VM's `docker login` |

`register_blocks.py` writes both blocks, so it needs both tokens. Take the unchanged one from the vault.

1. Mint the replacement in GitHub, with the same name, kind and scope.
   Do not revoke the old token yet.

2. Write both blocks. It must print `container-registry-auth/ghcr-croi saved` and `secret/github-croi-pat saved`.

   ```bash
   # hub VM, as ubuntu
   R=/opt/clinicalomx_data_platform
   IFS= read -rsp 'comx-ci-read: ' COMX_CI_READ; echo
   IFS= read -rsp 'comx-ghcr-read: ' COMX_GHCR_READ; echo
   export COMX_CI_READ COMX_GHCR_READ GHCR_USERNAME=<ghcr_username from prod.tfvars> PREFECT_API_URL=http://localhost:4200/api
   $R/current/packages/clinicalomxdb/hub-pipelines/prefect/.venv/bin/python \
     $R/current/packages/clinicalomx_pipelines/qptiff_pipeline/register_blocks.py
   ```

3. If you rotated the packages token, renew the VM's registry login in the same shell. Then clear the variables.

   ```bash
   printf '%s' "$COMX_GHCR_READ" | docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin
   unset COMX_CI_READ COMX_GHCR_READ
   ```

4. Run section 4.1, then one HPC deployment. Its log must show `Deployment step 'git_clone' completed successfully`.

   ```bash
   # hub VM, as ubuntu, with the Prefect CLI open
   prefect deployment run 'build-environments/utils-build-environments' --param dry_run=true --watch
   ```

5. Put the new value in its vault entry (`infra/secrets/README.md` names it).

6. If you hold the tofu credentials (section 6.4), update the Barbican copy.
   The plan must change exactly one Barbican secret.

   ```bash
   # operator workstation, in infra/control_plane, with the tofu environment loaded
   IFS= read -rsp 'new token: ' TF_VAR_github_contents_read_pat; echo   # or TF_VAR_ghcr_packages_read_pat
   export TF_VAR_github_contents_read_pat
   tofu apply -var-file=prod.tfvars
   ```

   Without the tofu credentials, skip this and note it in the vault entry.

7. Revoke the old token in GitHub.

### 4.3 The `gh` login on the hub VM

`assemble-release.sh` clones with the `gh` login of the `ubuntu` user on the VM.
Replace it when its owner leaves:

```bash
# hub VM, as ubuntu
gh auth status
gh auth login --hostname github.com --git-protocol https
```

### 4.4 Other credentials

Use `infra/secrets/rotation-runbook.md`.

## 5. Add an HPC user

Every HPC job runs as the first user in `packages/clinicalomxdb/hub-pipelines/prefect/workers/hpc/users.conf`.
To hand over, a lab member with an HPC account becomes that first line.
Do it while the old account still works.

### 5.1 Prepare the new user

1. The new user asks the HPC facility for an account, the SLURM account, and access to the raw scans and project storage.

2. Check access, as the new user:

   ```bash
   # HPC login node, as the new user. First set RAW to storage.raw_root and PROJECT to
   # storage.hpc_alias, from site.yaml.
   sacctmgr -nP show assoc user=$USER format=account,partition,qos
   ls "$RAW" > /dev/null && echo raw-scans-readable
   touch "$PROJECT/.write-test" && rm "$PROJECT/.write-test" && echo project-writable
   ```

   - `sacctmgr` must list `hpc.slurm_account` with partition `gpu_cuda` and qos `gpu`. A different account needs step 5.4.
   - Both `echo` lines must print.

3. Set up the user's LaminHub login on the HPC:

   ```bash
   # HPC login node, as the new user
   uv venv ~/.venvs/lamin && . ~/.venvs/lamin/bin/activate && uv pip install lamindb
   lamin login
   lamin connect <lamin.instance>
   ```

   `lamin connect` must name the instance.
   If it does not, an instance admin adds the user as a collaborator on LaminHub.

### 5.2 Ship the user list

1. Put the new user on the first line of `users.conf`, above the old one.
2. Merge, cut a platform tag (section 1.3), and deploy it (section 2).
3. Remove the old line once the old account is gone.

### 5.3 Provision and log in

1. Create the user's SSH alias, Prefect SSH block `hpc-<user>`, work pool and worker unit:

   ```bash
   # hub VM, as ubuntu
   cd /opt/clinicalomx_data_platform/current/packages/clinicalomxdb
   make hpc-users
   make ops-ssh
   ```

   `socket=DEAD` is expected until that user logs in.

2. Log in as the new user (section 3.2, step 3).

3. Point the shared HPC worker at the new user with a systemd drop-in, a small file that overrides one setting of a unit.
   Run it when section 2.3 lists no `Pending` run.

   ```bash
   # hub VM, as ubuntu
   sudo mkdir -p /etc/systemd/system/hpc-ssh-pool-worker.service.d
   printf '[Service]\nEnvironment=HPC_SSH_BLOCK=hpc-<user>\n' \
     | sudo tee /etc/systemd/system/hpc-ssh-pool-worker.service.d/user.conf
   sudo systemctl daemon-reload
   sudo systemctl restart hpc-ssh-pool-worker.service
   systemctl show -p Environment hpc-ssh-pool-worker.service
   ```

   The last line must include `HPC_SSH_BLOCK=hpc-<user>`.

4. Set `hpc_user = "<user>"` inside the `control_plane` block of `prod.tfvars` (section 2.9).

### 5.4 Change the SLURM account, if it differs

Skip this if the new user charges `hpc.slurm_account`.

1. Save the `hpc-ssh-pool` work pool's job template:

   ```bash
   # hub VM, as ubuntu, with the Prefect CLI open
   python - <<'PY'
   import asyncio, json
   from prefect.client.orchestration import get_client
   async def main():
       async with get_client() as c:
           pool = await c.read_work_pool("hpc-ssh-pool")
           json.dump(pool.base_job_template, open("/tmp/hpc-ssh-pool.json", "w"), indent=2)
   asyncio.run(main())
   PY
   cp /tmp/hpc-ssh-pool.json /tmp/hpc-ssh-pool.before.json
   ```

2. In `/tmp/hpc-ssh-pool.json`, set `variables.properties.account.default` to the new account. It starts with `a_`.

3. Apply it. The `diff` must show the account line and nothing else.

   ```bash
   diff /tmp/hpc-ssh-pool.before.json /tmp/hpc-ssh-pool.json
   prefect work-pool update hpc-ssh-pool --base-job-template /tmp/hpc-ssh-pool.json
   ```

### 5.5 Check it works

```bash
# hub VM, as ubuntu, with the Prefect CLI open
prefect deployment run 'data-tests/pipeline-data-tests' --watch
```

The run must reach `Completed`.
On the HPC, `sacct -u <user> -S today` must list its job.

## 6. Restore from backup

### 6.1 What is backed up

| What | Where the copy is | Copy made by |
|---|---|---|
| The whole Postgres database: the lamindb catalogue, MLflow and Prefect | `/mnt/data/artifacts/backups/postgres/` on the VM: 7 daily and 4 weekly `pg_dumpall` files | `pg-backup.timer`, daily at 02:30 |
| Raw scans | research data collection `storage.raw_root` | the research data service |
| Derived data, SIFs, manifests | `storage.hpc_root` on the HPC | nothing |
| croi labels, models and evaluations | `storage.model_root` on the HPC | nothing; see section 6.5 |
| VM configuration | the release tag, `prod.tfvars`, the three env files, tofu state | git, the vault, the tofu state bucket |

1. Copy a dump off the Nectar project monthly, and before any risky change:

   ```bash
   # hub VM, as ubuntu; the HPC socket must be open (section 3.2)
   DUMP=$(ls -1t /mnt/data/artifacts/backups/postgres/daily-*.sql.gz | head -1)
   scp "$DUMP" hpc:<RDM path the lab chose for backups>/
   ```

2. Check the backup is fresh:

   ```bash
   # hub VM, as ubuntu
   ls -lt /mnt/data/artifacts/backups/postgres | head -3
   ls /mnt/data/artifacts/backups/postgres/LAST_BACKUP_FAILED 2>/dev/null && echo BACKUP-FAILING
   ```

   The newest `daily-*.sql.gz` must be from today or last night. `BACKUP-FAILING` must not print.

### 6.2 Rehearse a restore

This restores the newest dump into a throwaway container and changes nothing live. It takes about ten minutes.

```bash
# hub VM, as ubuntu
DUMP=$(ls -1t /mnt/data/artifacts/backups/postgres/daily-*.sql.gz | head -1)
mkdir -p /mnt/data/artifacts/restore-drill/pgdata
IFS= read -rsp 'throwaway password: ' DRILL_PW; echo
docker run -d --name pg-restore-drill -e POSTGRES_USER=root -e POSTGRES_PASSWORD="$DRILL_PW" \
  -v /mnt/data/artifacts/restore-drill/pgdata:/var/lib/postgresql -p 127.0.0.1:5433:5432 postgres:18
unset DRILL_PW
until docker exec pg-restore-drill psql -U root -c 'select 1' >/dev/null 2>&1; do sleep 2; done
gunzip -c "$DUMP" | docker exec -i pg-restore-drill psql -U root -q -d postgres
docker exec pg-restore-drill psql -U root -d lamindb -tc 'select count(*) from lamindb_artifact'
docker exec postgres psql -U root -d lamindb -tc 'select count(*) from lamindb_artifact'
docker rm -f pg-restore-drill
sudo rm -rf /mnt/data/artifacts/restore-drill
```

The two counts must match, or differ only by artifacts written since the dump.
An error that a role already exists is expected.

### 6.3 Restore the live database

**This loses everything written since the dump**: Prefect runs, MLflow runs and catalogue rows.

1. Drain the workers (section 2.3), then stop the stack:

   ```bash
   # hub VM, as ubuntu
   sudo systemctl stop clinicalomxdb.service
   ```

2. Choose the dump and move the broken database aside:

   ```bash
   DUMP=$(ls -1t /mnt/data/artifacts/backups/postgres/daily-*.sql.gz | head -1); echo "$DUMP"
   sudo ls /mnt/data/postgres
   df -h /mnt/data/postgres
   sudo mv /mnt/data/postgres/18 /mnt/data/postgres/18.before-restore
   ```

   `ls` shows one directory per Postgres major version.
   `df` must show free space at least the size of the live database.

3. Start Postgres alone on an empty database, then load the dump:

   ```bash
   cd /opt/clinicalomx_data_platform/current/packages/clinicalomxdb
   docker compose -f hub-database/docker-compose.yml --env-file hub-database/.env up -d database
   until docker exec postgres psql -U root -c 'select 1' >/dev/null 2>&1; do sleep 2; done
   gunzip -c "$DUMP" | docker exec -i postgres psql -U root -q -d postgres
   docker exec postgres psql -U root -l
   ```

   An error that role `root` already exists is expected.
   The list must include `lamindb`, `mlflow` and `prefect`.

4. Start everything:

   ```bash
   sudo systemctl start clinicalomxdb.service
   ```

5. Run sections 2.5, 2.6 and 2.8 with the release `current` names.

6. After a week of normal running, remove `/mnt/data/postgres/18.before-restore`.

### 6.4 Rebuild the hub VM

A rebuild replaces the VM and keeps its three volumes.

**Put the tofu state passphrase in the vault entry `clinicalomx/infra/state-passphrase` now.**
Its only copy is the captain's own machine, unless it is already in the vault.
Without it nobody can rebuild or change the VM.

You need:

- the Nectar OpenStack RC file of a member of the Nectar project
- the tofu state backend credential, an EC2 credential pair for the state bucket
- the tofu state passphrase, `TF_VAR_state_encryption_passphrase`
- `prod.tfvars` and the three env files `hub-database/.env`, `hub-pipelines/.env` and `hub-ops/.env`, placed where `prod.tfvars` names them

`infra/control_plane/deployment.md` steps 1 to 3 load them.

1. Replace the VM:

   ```bash
   # operator workstation, in infra/control_plane, with OS_*, AWS_* and the passphrase exported
   tofu init -reconfigure -backend-config="key=prod/terraform.tfstate"
   tofu state list | grep -x 'github_repository_deploy_key.clone' && tofu state rm github_repository_deploy_key.clone
   tofu plan -var-file=prod.tfvars -replace=openstack_compute_instance_v2.hub
   tofu apply -var-file=prod.tfvars -replace=openstack_compute_instance_v2.hub
   tofu output -raw ssh_private_key > ~/.ssh/clinicalomx-hub && chmod 600 ~/.ssh/clinicalomx-hub
   ```

2. Log in and read `/etc/motd`. Do the manual steps it lists.
   The rebuild template has never been booted, so expect to finish by hand.
3. Run section 3.2.

### 6.5 Data with no second copy

croi's labels, trained models and evaluations live under `storage.model_root`, one person's HPC scratch directory.
The HPC facility deletes it when that account closes.

1. Copy it to project storage while the account is open:

   ```bash
   # HPC login node, as the user who owns storage.model_root. First set MODEL to
   # storage.model_root and PROJECT to the directory that holds storage.hpc_alias.
   rsync -a "$MODEL/" "$PROJECT/clinical_roi_lamindb_copy/"
   rsync -a --dry-run --itemize-changes "$MODEL/" "$PROJECT/clinical_roi_lamindb_copy/" | head
   ```

   The second command must print nothing.

2. The catalogue rows still name the old path. Repoint them by changing the instance's Storage record.

## 7. Park and unpark production

Parking stops all processing and keeps every file, catalogue row and deployment.

### 7.1 Park

1. Take a fresh backup and copy it off the Nectar project (section 6.1).

2. Pause every active schedule and automation, and record which ones were active in `parked.json`:

   ```bash
   # hub VM, as ubuntu, with the Prefect CLI open
   python - <<'PY'
   import asyncio, json, os
   from uuid import UUID
   from prefect.client.orchestration import get_client
   OUT = "/opt/clinicalomx_data_platform/shared/parked.json"
   async def main():
       if os.path.exists(OUT):
           raise SystemExit(f"already parked; {OUT} exists")
       async with get_client() as c:
           schedules = [[str(d.id), str(s.id), d.name] for d in await c.read_deployments(limit=200)
                        for s in d.schedules if s.active]
           automations = [[str(a.id), a.name] for a in await c.read_automations() if a.enabled]
           json.dump({"schedules": schedules, "automations": automations}, open(OUT, "w"), indent=2)
           for dep, sid, name in schedules:
               await c.update_deployment_schedule(UUID(dep), UUID(sid), active=False)
               print("paused schedule:", name)
           for aid, name in automations:
               await c.pause_automation(UUID(aid))
               print("paused automation:", name)
   asyncio.run(main())
   PY
   ```

3. Cancel what is queued, then wait until nothing is running:

   ```bash
   prefect flow-run ls --state Scheduled --state Late --limit 50
   prefect flow-run cancel <flow run id>   # for each one listed
   prefect flow-run ls --state Running --state Pending
   ```

4. Stop and disable the workers. Each must print `disabled`.

   ```bash
   systemctl list-units 'hpc-ssh-pool-worker@*' --no-pager --no-legend | tee $R/shared/parked-instances.txt
   sudo systemctl disable --now vm-pool-worker.service pipeline-pool-worker.service \
     hpc-ssh-pool-worker.service 'hpc-ssh-pool-worker@*'
   systemctl is-enabled vm-pool-worker.service pipeline-pool-worker.service hpc-ssh-pool-worker.service
   ```

   The catalogue and the nightly backup keep running.

5. Decide with the lab whether the VM stays up:

   | Option | What still works | Cost |
   |---|---|---|
   | VM stays up | the portal, the catalogue, MLflow, the review app, the nightly backup | the VM's Nectar quota |
   | VM shelved | nothing until unshelved; the volumes and their data are kept | none for compute |

6. To shelve it. The status must read `SHELVED_OFFLOADED`.

   ```bash
   # operator workstation, with the Nectar RC file sourced
   openstack server shelve clinicalomxdb-hub
   openstack server show clinicalomxdb-hub -c status
   ```

### 7.2 Unpark

1. If the VM is shelved, unshelve it, then run section 3.2:

   ```bash
   # operator workstation, with the Nectar RC file sourced
   openstack server unshelve clinicalomxdb-hub
   ```

2. Check the credentials:

   - Run section 4.1, and `gh auth status` on the VM. Rotate an expired token (section 4.2).
   - If the HPC user in `users.conf` no longer exists, run section 5.
   - If the repository moved to another GitHub owner, the new owner mints the clone token.
     Change the `repository:` URL in both `prefect.yaml` pull steps and in `assemble-release.sh`, then release and deploy (sections 1.3 and 2).

3. Start the workers:

   ```bash
   # hub VM, as ubuntu
   sudo systemctl enable --now vm-pool-worker.service pipeline-pool-worker.service hpc-ssh-pool-worker.service
   sudo systemctl enable --now hpc-ssh-pool-worker@<user>.service   # once per line in $R/shared/parked-instances.txt
   ```

4. Open the HPC socket (section 3.2, steps 3 and 4).

5. Resume exactly what park paused:

   ```bash
   # hub VM, as ubuntu, with the Prefect CLI open
   python - <<'PY'
   import asyncio, json, os, time
   from uuid import UUID
   from prefect.client.orchestration import get_client
   IN = "/opt/clinicalomx_data_platform/shared/parked.json"
   async def main():
       parked = json.load(open(IN))
       async with get_client() as c:
           for dep, sid, name in parked["schedules"]:
               await c.update_deployment_schedule(UUID(dep), UUID(sid), active=True)
               print("resumed schedule:", name)
           for aid, name in parked["automations"]:
               await c.resume_automation(UUID(aid))
               print("resumed automation:", name)
       os.rename(IN, f"{IN}.resumed-{int(time.time())}")
   asyncio.run(main())
   PY
   ```

6. Run section 2.8's two `data-tests` runs.

## 8. Move the facility NAT address

Run this when the HPC support desk announces a new NAT address.
The NAT address is the one public address every HPC job uses to reach the hub.

Two `site.yaml` keys hold it, and they change together:

- `hub.compute_ingress_cidr`: what the `hpc` security group, the cloud firewall, admits on ports 4200, 5000 and 5432.
- `hpc.nat_address`: what the Grafana rule "Unexpected source address on the HPC API ports" treats as expected.

The same rule also treats `hpc.compute_subnet`, the cluster's private compute subnet, as expected.
It does not move with the NAT.

### 8.1 Update the security group

1. In your workstation copy of `site.yaml`, set `hpc.nat_address` to the new address.
2. Set `hub.compute_ingress_cidr` to the same address followed by `/32`, which means that one address.
3. Check the file. It must print `OK`.

   ```bash
   # operator workstation, at the repository root
   uv run scripts/site/render.py --check site.yaml
   ```

4. Apply it. The plan must replace the three `hpc` security group rules and change nothing else.

   ```bash
   # operator workstation, in infra/control_plane, with OS_*, AWS_* and the passphrase exported
   tofu plan -var-file=prod.tfvars
   tofu apply -var-file=prod.tfvars
   ```

5. Copy the edited `site.yaml` to the vault.

### 8.2 Update the alert rule on the hub VM

1. Make the same two edits in `$R/config/package-env/site.yaml`.
2. Render and reload:

   ```bash
   # hub VM, as ubuntu
   R=/opt/clinicalomx_data_platform
   $R/shared/site-venv/bin/python $R/current/scripts/site/render.py
   cd $R/current/packages/clinicalomxdb && make ops-reload
   ```

   `render.py` must print `wrote site.env and ...`.
   `make ops-reload` must print `[alert-rules] wrote ...` and then `grafana config reloaded`.
   A `REFUSED` line names the problem and changes nothing.

3. After the next HPC job, the rule must still read `Normal` on the Grafana Alerting page.
