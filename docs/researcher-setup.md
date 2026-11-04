# Set up your working environment

Get the code and install it, on a lab dev box or on the HPC.

## The repository

Everything is in one GitHub repository, `source.repository` in the operator's `site.yaml`:

```text
https://github.com/<source.repository>.git
```

| I want to work on | Directory |
|---|---|
| the shared vocabulary, ROI relations, artifact extract | `packages/clinicalomx` |
| marker curation and the marker registry | `packages/clinicalomx_markers` |
| the reagent inventory (antibodies, barcodes, fluorophores) | `packages/clinicalomx_reagents` |
| patient metadata as OMOP tables, and linking a core to its patient | `packages/clinicalomx_omop` |
| the QPTIFF ingest and processing flows | `packages/clinicalomx_pipelines` |
| the ROI detector and its training | `packages/clinicalomx_ml` |
| the governance ledger | `packages/clinicalomx_ledger` |
| the hub services, database and infrastructure | `packages/clinicalomxdb` and `infra/` |

Each `packages/<name>` directory is its own Python project with its own `pyproject.toml` and `uv.lock`.
Install only the package you work in. There is no repository-wide `uv sync`.

## The private index credential

The lab's own Python packages come from a private package index: `clinicalomx`, `clinicalomx_markers`, `clinicalomx_ledger`, `clinicalomx-pipeline` and the `clinical-roi` wheels.
`uv` needs a credential to read it.
The credential is four environment variables:

| Variable | What it is |
|---|---|
| `AWS_ACCESS_KEY_ID` | the credential's id |
| `AWS_SECRET_ACCESS_KEY` | the credential's secret |
| `AWS_REGION` | the storage zonegroup |
| `UV_S3_ENDPOINT_URL` | the object store the index is served from |

The store is the lab's own Nectar object store, not Amazon.

1. Ask the operator for the **read** credential, as a file of `KEY=value` lines.
2. Put it at `~/.config/clinicalomx/index.env`. The operator may have placed it there already.
3. Load it in the shell where you install things:

   ```bash
   chmod 600 ~/.config/clinicalomx/index.env
   set -a; . ~/.config/clinicalomx/index.env; set +a
   export UV_PREVIEW_FEATURES=s3-endpoint
   ```

4. Check it works:

   ```bash
   uv venv /tmp/index-check -q
   VIRTUAL_ENV=/tmp/index-check uv pip install --dry-run \
     --extra-index-url "<packages.index_url>" \
     --preview-features s3-endpoint \
     clinicalomx
   ```

   It must end in `clinicalomx==<version>` and download nothing.
   A `403` means the credential is missing, wrong, or not loaded in this shell.
   A warning about preview features means `UV_PREVIEW_FEATURES` is not set.

**Never put the credential inside your checkout, never commit it, and never put it in a git URL or `git config`.**

Add the index with `--extra-index-url` or `uv add --index`.
Never use `uv pip install --index-url`: it replaces PyPI.

## On a dev box

A dev box is your own Nectar virtual machine for interactive work.
It sits on the same private network as the database and MLflow.
Your home directory is on a separate volume that survives a rebuild of the box. Everything outside your home does not.

### Get the code

1. Log in with the username and address the operator gives you:

   ```bash
   ssh <your-username>@<the box's floating IP>
   ```

2. Log in to GitHub and clone into your home:

   ```bash
   gh auth login
   gh repo clone <source.repository> ~/clinicalomx_data_platform
   ```

   If `gh: command not found`, ask the operator for the package source, then run `sudo apt-get install -y gh`.

### Install a package

1. Install `uv`, which manages both Python and the dependencies:

   ```bash
   command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Load the index credential (see above).

3. Install the package you work in and run its tests. For the vocabulary and extract layer:

   ```bash
   cd ~/clinicalomx_data_platform/packages/clinicalomx
   uv sync --extra vocab --extra extract --extra app
   uv run pytest -q
   ```

   `uv sync` must finish without a `403`, and `pytest` must report no errors.

The other packages work the same way:

```bash
cd ~/clinicalomx_data_platform/packages/clinicalomx_markers && uv sync && uv run pytest -q
cd ~/clinicalomx_data_platform/packages/clinicalomx_reagents && uv sync && uv run pytest -q
cd ~/clinicalomx_data_platform/packages/clinicalomx_omop && uv sync && uv run pytest -q
cd ~/clinicalomx_data_platform/packages/clinicalomx_pipelines && uv sync && uv run pytest -q
cd ~/clinicalomx_data_platform/packages/clinicalomx_ml && uv sync
cd ~/clinicalomx_data_platform/packages/clinicalomx_ledger && uv sync && uv run pytest -q
```

- `clinicalomx_ml`: `uv sync` installs prediction and dev tooling. Add `--extra train` for MLflow and evaluation, or `--extra notebooks` for Jupyter and plotting.
- `clinicalomx_ledger`: the tests start a throwaway Postgres in Docker. Your account is already in the `docker` group.
- `clinicalomx_markers` and `clinicalomx_omop`: a second suite, `tests_pg`, runs after `uv sync --group pg` and needs Docker.
- `clinicalomx_omop`: real terminology codes need your own ATHENA download. That package's README says where to get one. Never commit it.

### Connect to the database

The box exports the MLflow URL, the instance name `lamin.instance` and the database address, from `/etc/profile.d/clinicalomx.sh`.
You connect with your own LaminHub account:

```bash
uv venv ~/.venvs/lamin
. ~/.venvs/lamin/bin/activate
uv pip install lamindb
lamin login
lamin connect <lamin.instance>
```

`lamin connect` must name the instance.

### Your box

- The box runs its setup once, at first boot. A change to how boxes are built reaches your box only when the operator rebuilds it.
- The box comes with an account, a persistent home, the `docker` group, `sudo`, and the network route to the hub. You install everything else, including the index credential.

## On the HPC

The HPC is the university cluster for heavy work: QPTIFF ingest, image processing, and GPU training for the ROI detector.
Raw slides are under `storage.raw_root` and working data under `storage.hpc_root`. Both are reachable only from the HPC.
The HPC has no Docker. Containers run under Apptainer.

Production pipeline and training runs do not use your checkout.
Each run fetches the repository itself at a fixed release tag.
Your clone is for reading, developing and interactive work.

### Get the code

1. Get your own HPC account and SLURM allocation through the operator.

2. Log in to `hpc.login_host` and clone:

   ```bash
   gh auth login
   gh repo clone <source.repository> ~/clinicalomx_data_platform
   ```

   To install or build, clone under your own directory in `hpc.user_scratch_root` instead, and keep your `uv` cache there too.
   If `gh` does not work there, use `git clone` over HTTPS with your own GitHub credential. Keep the credential out of `git config`.

### Install a package

1. Install `uv` into your home. Ask the operator first if the download is blocked or a cluster module provides `uv`.

   ```bash
   command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Load the index credential (see above).

3. Install the package. For the ROI detector:

   ```bash
   cd ~/clinicalomx_data_platform/packages/clinicalomx_ml
   uv sync --extra notebooks
   ```

   `uv sync` must finish without a `403`.
   `uv sync --extra train` adds MLflow and the evaluation tooling.

`uv sync` also fetches two git dependencies from their own GitHub repositories: the SLURM submission tool and the lab's fork of the image reader.

`clinicalomx` installs from the private index, not from your checkout.
To work against your local copy, install it over the top:

```bash
cd ~/clinicalomx_data_platform/packages/clinicalomx_ml
uv pip install -e ../clinicalomx
```

### Work on the HPC

- Run notebooks on a compute node, reached over an editor tunnel. Do not work on a login node.
- Do annotation on an HPC virtual desktop, so image thumbnails stay on the cluster.
- Submitting a job needs a login you complete yourself, with your password and DUO. The job is charged to your allocation.
- Do not write `.sbatch` files, and do not put SLURM directives in analysis code.
- Register a `/scratch` storage root only from the HPC.

## Old code that imports `dataspec` or `roispec`

`clinicalomx` 0.3.0 deleted these two names.
Every symbol they exported is in `clinicalomx` under the same name.
`packages/clinicalomx/docs/upgrading-to-v0.3.0.md` is the full map, including `roispec.ops`, now `clinicalomx.roi`.

1. Find the old imports in your code and notebooks:

   ```bash
   grep -rn "dataspec\|roispec" --include="*.py" --include="*.ipynb" --include="*.toml" . | grep -v "\.venv"
   ```

2. Rewrite them, before you raise your version pin.

3. Check each virtual environment, with it activated. No output means clean.

   ```bash
   pip list 2>/dev/null | grep -Ei '^(dataspec|roispec) '
   ```

4. Uninstall either name if it is still there.

## Read more

| Question | Document |
|---|---|
| what `clinicalomx` gives you and how to use it | `packages/clinicalomx/README.md` |
| the full 0.3.0 rename map | `packages/clinicalomx/docs/upgrading-to-v0.3.0.md` |
| what belongs in the ML package | `packages/clinicalomx_ml/README.md` |
| how a dev box is built and torn down | `infra/compute/deployment.md` |
