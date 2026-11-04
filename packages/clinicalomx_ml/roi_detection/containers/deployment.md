# ROI Detection Image Deployment
Builds the four ROI detection images and puts them into production on the HPC.

Words used here:

- A **container image** is a program packaged with everything it needs.
- A **registry** is the server that stores images. GitHub's registry is GHCR, at `registry.images` in `site.yaml`.
- A **SIF** is one image saved as a single `.sif` file. The HPC runs it with **Apptainer**, because the HPC has no Docker.
- **SLURM** is the HPC's job queue. Each pipeline run on the HPC is one SLURM job that runs a SIF.

GitHub builds the images and their SIFs when a `clinical-roi-v<version>` tag is pushed.
The build runs in `.github/workflows/img-croi.yml`.

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

## 1. Release a new version
Do `docs/operations.md` section 1.1 for the `clinical-roi` package.
The tag is `clinical-roi-v<version>`.
The image build takes 65 to 80 minutes. Leave it running.

## 2. Point the pipelines at the new images
Do `docs/operations.md` section 1.2.

## 3. Cut a platform tag
Do `docs/operations.md` section 1.3.

## 4. Deploy the platform tag
Do `docs/operations.md` section 2. It takes about an hour.
Its section 2.7 copies the new SIFs to the HPC.

# Roll back
Do `docs/operations.md` section 2.10.
Keep the previous SIFs on the HPC until the new ones work.

# Build by hand
Use only on a computer with Docker. Follow `README.md` in this directory, section "Build by hand".
