# Pipeline Image Deployment
Builds the three pipeline images in this directory and puts them into production on the HPC.

Words used here:

- A **container image** is a program packaged with everything it needs.
- A **registry** is the server that stores images. GitHub's registry is GHCR, at `registry.images` in `site.yaml`.
- A **SIF** is one image saved as a single `.sif` file. The HPC runs it with **Apptainer**, because the HPC has no Docker.
- **SLURM** is the HPC's job queue. Each pipeline run on the HPC is one SLURM job that runs a SIF.

| Dockerfile | Image | GitHub build | SIF published |
|---|---|---|---|
| `Dockerfile.qptiff` | `comx-qptiff` | `.github/workflows/img-qptiff.yml` | yes |
| `Dockerfile.extract` | `comx-extract` | `.github/workflows/img-extract.yml` | yes |
| `Dockerfile.bgnorm` | `bgnorm-inference` | `.github/workflows/img-bgnorm.yml` | no |

GitHub builds an image when a change to its Dockerfile merges to `main`.
The image's tag comes from the versions pinned in the Dockerfile's `ARG` and `FROM` lines.

## 0. Requirements
On your own computer:
```text
git
gh
uv
jq
```
`gh` is the GitHub command line tool. Log it in once with `gh auth login`.

Access:

- Push access to the repository `source.repository` on GitHub.
- A shell on the hub VM as `ubuntu`: `packages/clinicalomxdb/deployment.md` steps 1 and 2.
- An HPC account named in `hpc.users`.

## 1. Change the pinned version
On your own computer, in a pull request, change the pin in the Dockerfile:

- `ARG CLINICALOMX_VERSION` for a new `clinicalomx`
- `ARG BIOIO_QPTIFF_COMMIT` for a new slide reader, in `Dockerfile.qptiff` or `Dockerfile.extract`
- the tag on the `FROM` line for a new `bgnormpy` base, in `Dockerfile.bgnorm`

Merge the pull request.

## 2. Wait for the image build
On your own computer:
```bash
gh run list --repo <source.repository> --workflow img-qptiff.yml --limit 3
```
Use the workflow from the table for the image you changed.
A correct result shows the newest run as `completed` and `success`.
Repeat the command until it does.

To build one without a change:
```bash
gh workflow run img-qptiff.yml --repo <source.repository>
```

## 3. Point the pipelines at the new image
Do `docs/operations.md` section 1.2.
For `comx-extract`, that section's step 6 waits for the build on `main`.

## 4. Cut a platform tag
Do `docs/operations.md` section 1.3.

## 5. Deploy the platform tag
Do `docs/operations.md` section 2. It takes about an hour.
Its section 2.7 copies the new SIFs to the HPC.
A `bgnorm-inference` row reporting `unpublished` is expected.

# Roll back
Do `docs/operations.md` section 2.10.
Keep the previous SIFs on the HPC until the new ones work.

# Gaps
- **A new `bgnorm-inference` on the HPC.** GitHub publishes no SIF for it, and converting the image on the HPC fails. This repository has no working way to put a new `bgnorm-inference` SIF on the HPC.
