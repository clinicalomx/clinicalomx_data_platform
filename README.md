# Clinicalomx Data Platform

The code, pipelines and infrastructure for clinicalOMx spatial-omics data.

## What it does

1. Registers each scanned slide or experiment in a lamindb instance, a data lakehouse framework on Postgres.
2. Detects the tissue microarrays (TMAs) on each slide, then the cores in each TMA, with a person reviewing each step.
3. Normalises the background of every multiplex core image.
4. Builds an export of any region on request: OME-TIFF, OME-Zarr or SpatialData.
5. Keeps the lab's marker registry and reagent inventory, and links each core to its patient, citing the source of each decision.

Prefect schedules the work.
Image work runs on an HPC cluster through SLURM.
Everything else runs on one cloud VM, the hub.

## Run it

You need `uv` and Python 3.11 or later.
Docker is needed for the hub and for the database test suites.

Each directory under `packages/` is its own Python project.
To work on one:

```bash
cd packages/clinicalomx
uv sync --extra vocab --extra extract --extra app
uv run pytest -q
```

`docs/researcher-setup.md` covers dev boxes, the HPC and the private package index.

To run the operator portal on a laptop, with the Build app live on demo data:

```bash
./scripts/dev/control-plane.sh
```

Then open `http://localhost:8088/#build`.
It needs no cluster, no HPC and no database.

To try curation on a spreadsheet, against a throwaway database in Docker:

```bash
./scripts/dev/curation-demo.sh path/to/inventory.xlsx
```

To run the hub database on your machine, follow `packages/clinicalomxdb/README.md`.

## How the pieces fit

```text
 your machine                 hub VM                                HPC
 ------------                 ------                                ---
 make, tofu      --ssh-->     Postgres + lamindb instance           SLURM jobs in
 the portal      <-tunnel-    Prefect server and VM workers  --->   Apptainer images
                              MLflow, the review and Build apps     (detection, bgnorm)
                              the operator portal, Grafana
```

| Path | Holds |
|---|---|
| `packages/clinicalomx` | the library every other package uses to read and write the instance |
| `packages/clinicalomx_pipelines` | the Prefect pipeline from raw slide to reviewed cores |
| `packages/clinicalomx_ml` | the ROI detection models and their training |
| `packages/clinicalomx_markers` | the marker registry |
| `packages/clinicalomx_reagents` | the reagent inventory |
| `packages/clinicalomx_ledger` | the append-only store of claims and decisions |
| `packages/clinicalomx_omop` | patient metadata in the OMOP Common Data Model |
| `packages/clinicalomxdb` | the hub: Compose stacks, the portal and the `Makefile` |
| `infra/` | OpenTofu for the hub VM and dev boxes, the systemd units, the credential list |
| `scripts/` | site configuration, release assembly and the laptop dev servers |
| `tools/` | the publication and drift checks CI runs |
| `docs/` | the operations guide and the researcher setup guide |

Each package's README says what it depends on and how to test it.
Each package has its own CI workflow, `.github/workflows/pkg-*.yml`.

## Configure a site

Everything that ties the platform to one site is in `site.yaml`, which is not tracked.

```bash
cp site.example.yaml site.yaml
make site-check
```

`site.example.yaml` says what each key is.
Run `make` at the root to list every other target.

## Deploy

Each deployable part has a `deployment.md` beside it.
The first time, deploy in this order:

1. The hub VM: `infra/control_plane/deployment.md`.
2. The hub services: `packages/clinicalomxdb/deployment.md`.
3. The Prefect workers: `packages/clinicalomxdb/hub-pipelines/prefect/workers/deployment.md`.
4. The pipelines and the two web apps: `packages/clinicalomx_pipelines/qptiff_pipeline/deployment.md`.
5. The ROI detection pipelines: `packages/clinicalomx_ml/roi_detection/deployment.md`.
6. Dev boxes, if you need them: `infra/compute/deployment.md`.

New images follow `packages/clinicalomx_pipelines/qptiff_pipeline/containers/deployment.md` and `packages/clinicalomx_ml/roi_detection/containers/deployment.md`.

## Where to go next

| To | Read |
|---|---|
| release, deploy, reboot, restore or park the platform | `docs/operations.md` |
| find where a credential lives | `infra/secrets/README.md` |
| get data out of the instance | `packages/clinicalomx/docs/extract.md` |
| use the portal | `packages/clinicalomxdb/docs/runbooks/operations-ui.md` |

## License

Apache License 2.0. See `LICENSE`.
`NOTICE` lists the separately licensed work this repository carries or builds on.
