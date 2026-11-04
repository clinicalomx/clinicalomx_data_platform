# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Tier 1 - croi-<objective>-<mode>: thin layer on croi-base.
# Installs `clinical-roi` FROM THE BUILD CONTEXT, with the `[train]` extra when MODE=train, and
# the lamindb foundation layer (clinicalomx) FROM A WHEEL build.sh stages in dist/. Deps
# (torch/ultralytics/rfdetr/bioio/lamindb) are already warmed in croi-base.
#
# ONE DISTRIBUTION SINCE 0.2.0. This used to install `croi-<obj>-inference` and, for a train
# image, `croi-<obj>-train` as well. Those four member distributions are gone; the wheel carries
# both objectives and both layers, and `[train]` is the only thing that now separates a train
# image from an inference one. The FOUR IMAGES ARE UNCHANGED: same names, same tags, same SIF
# paths, same objective-by-mode loop in build.sh. CROI_OBJECTIVE says what an image is FOR and
# selects NOTHING: since Phase 2 re-layered `core` the last conditional step is gone, so all four
# images hold exactly the same wheel and differ only by the `[train]` extra. The variable stays
# because it is what a script inside the container reads to know which objective it was built for.
#
# THE MEMBERS USED TO COME FROM A GIT REF on the split repository
# `clinical_image_roi_object_detection`. That is what kept the image build alive on a repository the
# migration wants archived, and it is why archiving it was blocked. They now come from the
# staged context, which is the pattern every other monorepo image build follows: first-party
# code arrives in the context, never from a git clone or an index. Two consequences worth knowing:
#   - The image contents are now the CHECKOUT's contents, not a git ref's. `containers/build.sh`
#     stages them, so a build is reproducible from a tag by checking that tag out.
#   - The build needs no credential at all.
#
#   OBJECTIVE=general|core   MODE=inference|train
#   VERSION: what the image is tagged with, recorded as an image label. Provenance only -
#            nothing is fetched by it.
#   clinicalomx has no ref: build.sh builds its wheel from the same checkout.
#
#
#   docker build -f containers/objective.Dockerfile \
#     --build-arg OBJECTIVE=core --build-arg MODE=inference \
#     --build-arg VERSION=v0.1.10 \
#     -t ghcr.io/<owner>/croi-core-inference:v0.1.10 "$STAGE"
#
# The context is the staged tree build.sh assembles, NOT this directory: the paths below are
# relative to it. Run the build through build.sh rather than by hand.
# ---------------------------------------------------------------------------
ARG BASE_IMAGE=croi-base:dev
FROM ${BASE_IMAGE}

ARG OBJECTIVE=general
ARG MODE=inference
ARG OWNER
ARG VERSION=dev
# A site value, so no default: img-croi passes github.repository_owner through build.sh.
RUN : "${OWNER:?pass --build-arg OWNER}"

# The package source, staged by build.sh. /src is a BUILD INPUT, never an import root: nothing
# puts it on sys.path and nothing is copied out of it. It is the whole package rather than one
# objective because the wheel is built from here and its `packages` list spans both.
# containers/.dockerignore keeps governed data, notebooks and caches out, so this is about two
# megabytes of Python.
COPY clinicalomx_ml /src

# install the foundation layer + the clinical-roi wheel.
# --no-sources makes uv ignore the dev path sources, so every first-party package must be named
# explicitly here, each as a `name @ file://` direct reference: clinical-roi into the staged tree,
# clinicalomx to the wheel build.sh built beside it. A direct reference is what makes the local
# build authoritative for that NAME: no index can satisfy it.
# A train image asks for the `[train]` extra (mlflow, rfdetr[train]) on the same wheel an inference image
# installs bare. Nothing else differs between the two installs.
# clinicalomx shipped `dataspec`/`roispec` modules as deprecation shims until it deleted
# them, so the standalone distributions of those names must NOT be installed here either.
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eu; whl=$(echo /src/dist/clinicalomx-*.whl); [ -f "$whl" ]; \
    if [ "${MODE}" = "train" ]; then EXTRA="[train]"; else EXTRA=""; fi; \
    uv pip install --python /opt/venv --no-sources \
      "clinicalomx[models] @ file://$whl" \
      "clinical-roi${EXTRA} @ file:///src"

# Entry-point layer, and there is nothing left to do in it.
#
# Each objective's configs ship in the wheel as package data, so its stages run as
# `python -m croi <objective> <stage>` from anywhere and nothing outside site-packages is needed.
#
# WHAT WAS HERE was a conditional copy of `objectives/<obj>/*.py` and `objectives/<obj>/configs`
# into /opt/clinical_roi, because core's numbered drivers were top-level scripts a digit-prefixed
# filename keeps out of a wheel and `stage_config_dir()` resolved the configs/ SIBLING of each
# driver, so the two had to be co-located for the .sif to be a self-contained entry point. Both
# halves of that are gone: there is no driver, and each objective's `environment.CONFIGS` resolves the
# `configs/` sibling of `environment.py` inside site-packages.
#
# WORKDIR still exists and is deliberately EMPTY. The HPC worker runs `prefect flow-run execute`
# with this as the working directory, and a directory that exists is all that needs; WORKDIR
# creates it, so the `mkdir` that used to precede it went with the copy.
WORKDIR /opt/clinical_roi

ENV CROI_OBJECTIVE=${OBJECTIVE} \
    CROI_MODE=${MODE} \
    PATH=/opt/venv/bin:$PATH
LABEL org.opencontainers.image.title="croi-${OBJECTIVE}-${MODE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/${OWNER}/clinicalomx_data_platform"
CMD ["bash"]
