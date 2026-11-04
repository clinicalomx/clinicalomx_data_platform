# clinical_roi containers

The Dockerfiles and build script for the ROI detection images.
CI builds each image with Docker and pushes it to GHCR.
The HPC has no Docker, so each image is also published as an Apptainer SIF, which a SLURM job runs with `apptainer run --nv`.

To release a new version, follow `deployment.md` in this directory.

## Images

```text
croi-base                  CUDA, torch, ultralytics, rfdetr, bioio and the lamindb dependencies
  croi-general-inference   clinical-roi
  croi-general-train       clinical-roi[train]
  croi-core-inference      clinical-roi
  croi-core-train          clinical-roi[train]
```

- `base.Dockerfile` builds `croi-base`, and `objective.Dockerfile` builds the other four.
- All four hold the same code. `CROI_OBJECTIVE` selects the objective, and the `train` extra is the only other difference.
- Each runs a stage as `python -m croi <objective> <stage>`.
- The image tag is the release tag's version: `clinical-roi-v0.1.10` builds `croi-general-inference:v0.1.10`.
- `SIF_MODES` in `build.sh` selects which images get a SIF, as a mode or an `<obj>/<mode>` entry.

## Build by hand

Build on a host with a Docker daemon, never on an HPC login node.

```bash
export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...      # the private index read credential
export UV_S3_ENDPOINT_URL=<packages.index_url origin> UV_PREVIEW_FEATURES=s3-endpoint
DOCKER_BUILDKIT=1 REGISTRY=<registry.images> PUSH=1 \
  REF=v0.1.10 bash containers/build.sh                      # croi-base and the four images
```

`<registry.images>` and `packages.index_url` are keys in `site.yaml`.

- `REF` names the resulting image tag and fetches nothing. Check the tag out first.
- The credential reaches the build as a BuildKit secret and is never stored in a layer.
- The `clinical-roi` wheel builds from the checkout. Only `clinicalomx` comes from the private index.
- The private index is an extra index. Never pass it as `--index-url`: public dependencies such as `lamindb` then stop resolving.
- `APPTAINER=1` also converts each image to a SIF, on a host that has both Docker and Apptainer.
- Push the images to a private registry only.

Run an image on the HPC:

```bash
apptainer run --nv croi-core-inference_v0.1.0.sif python -m croi tma_core extract-tma-cores --image "<key>"
```

Fetch a SIF with the `utils-build-environments` deployment (`docs/operations.md` section 2.7 at the repository root).
Converting from `docker://` on the HPC fails.

## Environments

The HPC caches each SIF by its path, `environments/<owner>/croi-<obj>-<mode>_<tag>.sif`.
A rebuilt image under an unchanged tag is never picked up, so every code change needs a new tag.

Each SIF is registered as a versioned `ln.Artifact(kind="environment")`.
`register_environments.py` can register environments ahead of time or move an alias.
It is not part of a release.
