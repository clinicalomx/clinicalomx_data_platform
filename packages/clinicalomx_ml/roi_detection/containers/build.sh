#!/usr/bin/env bash
# Build the clinical_roi container set: croi-base + the 4 Tier-1 images
# (general/core x inference/train). Docker is the source of truth; convert to .sif for the HPC.
#
#   DOCKER_BUILDKIT=1 REGISTRY=ghcr.io/<owner> PUSH=1 REF=v0.1.10 bash containers/build.sh
#   APPTAINER=1 bash containers/build.sh                # also convert to .sif (needs apptainer+docker on one host)
#   SIF_MODES="inference train" ...                     # which images convert: a bare mode, or <obj>/<mode>
#   APPTAINER=1 SIF_PUSH=1 bash containers/build.sh     # ...and publish each .sif as an ORAS artifact
#   ENV_MANIFEST_DIR=out SOURCE_REF=clinical-roi-v0.1.10 ... # ...and write one env manifest per image
#
# Must run on a host with a Docker daemon (NOT an HPC login node).
#
# WHY SIF_PUSH EXISTS. Converting on the HPC (apptainer pull docker://...) needs mksquashfs, and an
# unprivileged apptainer runs it under proot, which needs ptrace - refused on the compute nodes, and
# login nodes are not available for this. So the conversion happens HERE, where the builder is
# privileged, and the HPC downloads the finished SIF instead of building one:
#
#   apptainer pull "$ENV_DIR/croi-general-inference_$REF.sif" \
#     oras://$REGISTRY/croi-general-inference-sif:$REF
#
# That is a blob download - no squashfs, no proot, no ptrace, and it cannot OOM inside a job
# allocation the way a first-flight conversion can.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT="$(cd "$HERE/../../.." && pwd)"       # dir holding the clinicalomx_ml package
# Image tag. The wheel is built from the CHECKOUT (see the staging block below), so
# this names what is checked out rather than a ref anything fetches - there is no
# CLINICAL_ROI_REF any more. Check the tag out, then build it.
TAG="${REF:-main}"
REGISTRY="${REGISTRY:-}"; PREFIX="${REGISTRY:+$REGISTRY/}"
# A site value, passed to objective.Dockerfile: the registry owner its source label names.
OWNER="${OWNER:?set OWNER, the GitHub owner of this repository}"
PUSH="${PUSH:-0}"; APPTAINER="${APPTAINER:-0}"; SIF_PUSH="${SIF_PUSH:-0}"
# Where the SIFs land. Defaults to $HERE, which is right on a workstation; CI points it at a scratch
# directory it has already checked for room. Do NOT read a path as a promise of a separate disk: on
# the runner image that failed at v0.1.10, /mnt and / were the same filesystem, so redirecting the
# scratch there bought nothing at all. The free-space assertion below is what actually holds.
SIF_DIR="${SIF_DIR:-$HERE}"
# Free space one conversion needs on SIF_DIR's filesystem, asserted immediately before each one.
# Measured at clinical-roi-v0.1.10 (run 33460237751): the conversion started with 26GB free and
# died seven minutes in, mid-layer, with `copy_file_range: no space left on device`. A conversion
# holds three things at once - the pulled OCI blobs, the extracted rootfs, and the SIF being
# squashed - so 26GB is not enough and the number below is a floor with margin, not a measurement
# of the minimum. Checking it here is the point: a build that cannot finish should say so before it
# spends seven minutes proving it.
SIF_FREE_GB="${SIF_FREE_GB:-35}"
# Which images get a .sif, as a space-separated list. An entry is either a bare MODE, which takes
# that mode for every objective, or `<obj>/<mode>`, which takes one image. Only inference by
# default, because a train SIF is ~6GB and for most of this package's life nothing pulled one: a
# train deployment loaded its flow body out of the pull-tag clone, so the image behind it barely
# mattered. That stopped being true for `general` at Phase 1 and for `core` at Phase 2, when each
# objective's training code moved into the wheel, so `img-croi.yml` now asks for all four. The
# `<obj>/<mode>` form is kept because it is what made publishing ONE train image possible while
# the two objectives were at different phases, and a future third objective may need it again.
SIF_MODES="${SIF_MODES:-inference}"
# Where to write one environment manifest per pushed image: the tagged reference, the immutable
# digest the registry now holds for it, the release it was built from, and the ORAS reference of
# the published SIF when there is one. Unset by default, so only CI pays for it. The manifests are
# what `python -m clinicalomx.environments register` consumes at release time; a digest read at
# registration time instead would record whatever the tag points at THEN, which is not evidence.
ENV_MANIFEST_DIR="${ENV_MANIFEST_DIR:-}"
# The release or tag the build ran from. Defaults to the image tag, which is what a local run has.
SOURCE_REF="${SOURCE_REF:-$TAG}"
# The privileged step. A CI runner can sudo, and a privileged apptainer never reaches for proot;
# override so the docker builds stay unprivileged and only this one command escalates.
APPTAINER_CMD="${APPTAINER_CMD:-apptainer}"
export DOCKER_BUILDKIT=1

# Free space in whole GiB on the filesystem holding $1. `df -Pk` rather than `df --output=avail`
# so this stays POSIX and reports the filesystem the directory is ON, which is the question - a
# directory named /mnt is not evidence that /mnt is its own disk.
free_gb() { df -Pk "$1" | awk 'NR==2 {printf "%d", $4/1048576}'; }
require_free_gb() {
  local dir="$1" need="$2" have
  have="$(free_gb "$dir")"
  df -h "$dir" | tail -1
  [ "$have" -ge "$need" ] && return 0
  echo "ERROR: $dir has ${have}GB free, and one SIF conversion needs ${need}GB." >&2
  echo "       Raise SIF_FREE_GB only if you know the conversion fits; otherwise free space." >&2
  df -h >&2
  exit 1
}

# --- stage the build context, preserving the relative layout the lockfile's path sources
#     expect. Two parts, and Tier 0 must not see the second one:
#       the manifest + the lock   -> base.Dockerfile COPYs exactly these, so that expensive
#                                    layer caches on dependency changes alone
#       the source tree           -> objective.Dockerfile builds the clinical-roi wheel from the
#                                    staged package; both objectives' configs ship inside it
#     The source tree is what replaced the split repo's git-install (ER-23). It stages through
#     tar so the caches and venvs a working checkout carries are excluded on the way in rather
#     than relied on being excluded on the way out; the context stays at a couple of megabytes
#     of Python.
#
#     ONE TREE SINCE PHASE 2: `roi_detection/src` holds `general` and `core`, each an
#     importable package with its configs inside it, and `croi`, the layer both share. The second tar, over
#     `roi_detection/objectives`, is gone with the last code that lived there, and so is the
#     `--exclude=data` it carried for the working PNGs under `objectives/general/data`; the
#     directory itself was removed 2026-10-07. The exclude never applied to
#     THIS tar and must not be added to it: `src/croi/data` is a package and must ship,
#     which is the same trap `containers/.dockerignore` names.
#
#     README.md is staged because the Tier 1 build now builds the `clinical-roi` WHEEL from the
#     staged tree, and `readme = "README.md"` in pyproject.toml makes hatchling refuse without it.
#     Tier 0 never reads it. ---
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
copy() { mkdir -p "$STAGE/$(dirname "$1")"; cp "$PARENT/$1" "$STAGE/$1"; }
copy clinicalomx_ml/pyproject.toml
copy clinicalomx_ml/uv.lock
copy clinicalomx_ml/README.md
PKG_DIR=clinicalomx_ml/roi_detection
tar -C "$PARENT" --exclude=__pycache__ --exclude=.venv \
    -cf - "$PKG_DIR/src" | tar -C "$STAGE" -xf -
cp "$HERE/.dockerignore" "$STAGE/.dockerignore"
# clinicalomx as a wheel from the same checkout, which objective.Dockerfile installs by file.
uv build --wheel --out-dir "$STAGE/clinicalomx_ml/dist" "$PARENT/clinicalomx"

# --- Tier 0: base (heavy shared deps warmed from the lock) --------------------------------
BASE="${PREFIX}croi-base:${TAG}"
echo ">> build $BASE"
docker build -f "$HERE/base.Dockerfile" -t "$BASE" "$STAGE"

# Triton builds C stubs AT RUNTIME, and RF-DETR's matcher reaches Triton only on Linux with a
# CUDA GPU of compute capability >= 8.0, so neither a CPU smoke nor a stubbed test sees it; a base
# with no C compiler shipped once and failed every RF-DETR training run on its first step. This
# drives Triton's own build path on a tiny extension and needs no GPU, so it fails here, in
# seconds, instead. It proves compiler + libc headers + Python.h; libcuda is the host's.
echo ">> check $BASE can build Triton's C stubs"
docker run --rm "$BASE" python -c '
from triton.runtime.build import compile_module_from_src
compile_module_from_src(
    "#include <Python.h>\n"
    "static struct PyModuleDef m = {PyModuleDef_HEAD_INIT, \"croi_cc_probe\", NULL, -1, NULL};\n"
    "PyMODINIT_FUNC PyInit_croi_cc_probe(void) { return PyModule_Create(&m); }\n",
    "croi_cc_probe")
'

# --- Tier 1: objective x mode (both first-party wheels from the staged tree) ---------------
#
# BUILDING AND CONVERTING ARE TWO PASSES, and that separation is the disk fix (run 33460237751).
# Measured there, on ONE 72GB filesystem: 43GB free after the runner's disk-freeing step, 26GB free
# once the base and the first objective image were built and pushed, and the conversion of that same
# image then wanted more than the 26GB left and died mid-layer with
# `copy_file_range: no space left on device`. Interleaving the two meant docker's unpacked copy and
# apptainer's working copy of one image existed at the same time. So pass 1 builds and pushes every
# image, then the whole local docker store goes, then pass 2 converts from the REGISTRY. Peak usage
# is now one large copy rather than two, however many conversions $SIF_MODES asks for.
declare -A DIGEST=() SIF_REF=()
CONVERT=()                          # "obj mode" pairs that get a SIF, in build order

for OBJ in general core; do
  for MODE in inference train; do
    IMG="${PREFIX}croi-${OBJ}-${MODE}:${TAG}"
    echo ">> build $IMG"
    docker build -f "$HERE/objective.Dockerfile" \
      --build-arg BASE_IMAGE="$BASE" \
      --build-arg OBJECTIVE="$OBJ" --build-arg MODE="$MODE" --build-arg OWNER="$OWNER" \
      --build-arg VERSION="$TAG" \
      -t "$IMG" "$STAGE"
    if [ "$PUSH" = "1" ]; then
      echo ">> push $IMG"
      docker push "$IMG"
      # The digest is read back from the REGISTRY, not from the local daemon: it is what a puller
      # resolves for this tag, and imagetools queries the registry, so the reclaims below do not
      # take it away. No fallback on purpose - a build that cannot state what it pushed has no
      # provenance to record, and failing here is louder than registering a guess later.
      DIGEST["$OBJ/$MODE"]="$(docker buildx imagetools inspect "$IMG" --format '{{.Manifest.Digest}}')"
      # Reclaim the image NOW rather than after its conversion, which is safe only because pass 2
      # pulls from the registry. Frees the image's own layers; the shared base is deliberately left
      # alone, since every later objective builds FROM it. Pruning the builder matters as much as
      # the image: with the `docker` buildkit driver the build cache holds a second copy of the same
      # layers in the same store.
      echo ">> reclaim $IMG"
      docker image rm "$IMG" >/dev/null 2>&1 || true
      docker builder prune -f >/dev/null 2>&1 || true
    fi
    if [ "$APPTAINER" = "1" ] && \
       [[ " $SIF_MODES " == *" $MODE "* || " $SIF_MODES " == *" $OBJ/$MODE "* ]]; then
      CONVERT+=("$OBJ $MODE")
    fi
  done
done

# --- Pass 2: OCI -> SIF, one image in flight at a time ------------------------------------
if [ "${#CONVERT[@]}" -gt 0 ]; then
  if [ "$PUSH" = "1" ]; then
    # Everything built is durable in the registry, so the local store has no reader left and the
    # conversion gets the disk. This is the reclaim that actually pays: the base is the bulk of it.
    echo ">> drop the local docker store before converting"
    docker image rm "$BASE" >/dev/null 2>&1 || true
    docker builder prune -af >/dev/null 2>&1 || true
    docker system prune -af >/dev/null 2>&1 || true
  fi
  for PAIR in "${CONVERT[@]}"; do
    read -r OBJ MODE <<<"$PAIR"
    IMG="${PREFIX}croi-${OBJ}-${MODE}:${TAG}"
    SIF="$SIF_DIR/croi-${OBJ}-${MODE}_${TAG}.sif"
    # "$IMG", not a rebuilt bare name. `docker build -t` tagged the image WITH the registry prefix
    # when REGISTRY is set, so the daemon holds ghcr.io/<owner>/croi-...:<tag> and nothing called
    # croi-...:<tag> - asking for the short name fails with "reference does not exist". This block
    # only ever ran locally with REGISTRY empty, where the two spellings coincide, which is why the
    # difference stayed invisible until CI started converting.
    #
    # Registry when we pushed, daemon when we did not. `docker://` is the cheaper source in both
    # senses: the blobs arrive compressed rather than as an uncompressed `docker save` stream, and
    # it needs no local image, which is what lets the reclaim above happen first. It reads
    # APPTAINER_DOCKER_USERNAME / APPTAINER_DOCKER_PASSWORD, the same pair `apptainer push` already
    # uses, so a private GHCR repository is reachable with no extra credential.
    if [ "$PUSH" = "1" ]; then SRC="docker://${IMG}"; else SRC="docker-daemon://${IMG}"; fi
    echo ">> apptainer build $(basename "$SIF") from $SRC into $SIF_DIR"
    require_free_gb "$SIF_DIR" "$SIF_FREE_GB"
    $APPTAINER_CMD build "$SIF" "$SRC"
    if [ "$SIF_PUSH" = "1" ]; then
      ARTIFACT="oras://${PREFIX}croi-${OBJ}-${MODE}-sif:${TAG}"
      echo ">> apptainer push $ARTIFACT"
      $APPTAINER_CMD push "$SIF" "$ARTIFACT"
      SIF_REF["$OBJ/$MODE"]="$ARTIFACT"
      # Freed immediately, not at the end: two 6GB SIFs do not both fit alongside the next
      # conversion's scratch, and the artifact is now the durable copy.
      rm -f "$SIF"
    fi
  done
fi

# --- One environment manifest per pushed image, written last because the SIF reference is not
#     known until pass 2 has published it -------------------------------------------------
if [ "$PUSH" = "1" ] && [ -n "$ENV_MANIFEST_DIR" ]; then
  mkdir -p "$ENV_MANIFEST_DIR"
  for OBJ in general core; do
    for MODE in inference train; do
      MANIFEST="$ENV_MANIFEST_DIR/croi-${OBJ}-${MODE}.json"
      jq -n --arg image "${PREFIX}croi-${OBJ}-${MODE}:${TAG}" \
            --arg digest "${DIGEST["$OBJ/$MODE"]}" \
            --arg source_ref "$SOURCE_REF" \
            --arg sif_oras_ref "${SIF_REF["$OBJ/$MODE"]:-}" \
            '$ARGS.named | with_entries(select(.value != ""))' > "$MANIFEST"
      echo ">> env manifest $MANIFEST"; cat "$MANIFEST"
    done
  done
fi
echo ">> done."
