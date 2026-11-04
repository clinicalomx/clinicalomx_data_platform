# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Tier 0, croi-base: the heavy, slow-changing, SHARED layer.
#   CUDA + torch + ultralytics + rfdetr + bioio(+tifffile fork) + lamindb + clinicalomx deps.
# Built once; every objective image (Tier 1) layers on top of it, so torch/CUDA are
# downloaded + resolved ONCE. Rebuilt only when uv.lock or a pyproject changes.
#
# Build CONTEXT is the tree `build.sh` stages. It also holds the objective sources, which the
# Tier 1 build needs and this one must ignore: the COPY below names the manifest and the lock
# ONLY, so a code edit cannot invalidate this layer. The sibling clinicalomx checkout is
# NOT needed, because this layer skips installing clinicalomx (below).
# ---------------------------------------------------------------------------
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# git: uv fetches the bioio-qptiff git source. libgl/glib: opencv/bioio runtime.
#
# gcc + libc6-dev: TRITON COMPILES C AT RUNTIME, so this runtime image needs a C compiler.
# RF-DETR's matcher calls `torch_linear_assignment`, which on Linux with CUDA and compute
# capability >= 8.0 (every GPU here: A100, A100 MIG, L40S) takes a Triton path, and Triton
# JIT-builds a small launcher stub with `gcc` (triton/runtime/build.py). Without one, RF-DETR
# training dies on its FIRST step with `Failed to find C compiler`. YOLO never reaches Triton.
# gcc alone cannot link the `-shared` stub: libc6-dev carries the crt objects and libc headers.
# Nothing else is needed: Triton bundles its own cuda.h, uv's Python ships Python.h, and
# libcuda.so.1 is the host driver `apptainer --nv` binds in. About 154 MiB on disk.
# The runtime base stays: the devel base adds ~2.5 GB compressed of nvcc and CUDA headers
# that nothing here uses, since Triton brings its own ptxas.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates libgl1 libglib2.0-0 gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /workspace
# Copy ONLY the dependency manifest and the lock, so this expensive layer caches independently of
# code. There is ONE manifest now: `clinical-roi` 0.2.0 collapsed the four member pyprojects into
# it, so the four extra COPY lines that preserved the workspace layout for the lockfile's path
# sources are gone with the path sources.
COPY clinicalomx_ml/pyproject.toml clinicalomx_ml/uv.lock \
     clinicalomx_ml/

WORKDIR /workspace/clinicalomx_ml
# Warm the INFERENCE dependency set into /opt/venv WITHOUT the project code.
# --no-install-workspace skips the project itself; --no-install-package skips clinicalomx too,
# whose path source is not in this context (the objective layer installs it from a wheel). Its
# third-party deps (lamindb, pydantic, ...) still get warmed.
#
# NO --all-extras AND NO DEV GROUP, which is ROADMAP defect 4. `--all-extras` pulled the root's
# `notebooks` extra, and the umbrella's own base dependencies pulled the SLURM TUI and ipykernel,
# so jupyter and friends were warmed into this SHARED layer and inherited by all four Tier 1
# images - the inference-versus-train size argument the image split rests on was not being
# realised. Base is now exactly what an inference image needs; `clinical-roi[train]` adds mlflow at
# Tier 1, where only the two train images pay for it, and the dev group cannot reach an image at
# all.
#
# MLFLOW IS STILL RESOLVED HERE, and that is not this line's doing: the base dependency is
# `clinicalomx[models]`, whose `models` extra IS mlflow (the portable-checkpoint download
# fallback). So an inference image has always had mlflow importable, with or without the four
# member wheels. What the wheel boundary really held is asserted by
# tests/test_inference_has_no_train_stack.py: no module reachable from an objective's two inference
# entry points imports mlflow or its training stack, and no inference-layer module imports either.
# The entry points are each objective's `croi.objectives.<obj>.registry` and the shared
# `croi.detection` seam, and `KNOWN_MLFLOW_IN_INFERENCE` is EMPTY.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-workspace --no-dev --python 3.11 \
        --no-install-package clinicalomx

CMD ["bash"]
