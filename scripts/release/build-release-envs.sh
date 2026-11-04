#!/usr/bin/env bash
# Build one platform release's Python environments. 
# Usage:
#   build-release-envs.sh <platform-tag> [root]
#     platform-tag  e.g. platform-v5. The release must already be assembled.
#     root          default /opt/clinicalomx_data_platform
#
# The three environments, in build order:
#   1. the shared prefect venv        <release>/packages/clinicalomxdb/hub-pipelines/prefect/.venv
#   2. the vm-process venv            .../prefect/workers/vm-process/.venv
#   3. the clinicalomx_pipelines sync <release>/packages/clinicalomx_pipelines/.venv

set -euo pipefail

TAG="${1:?usage: build-release-envs.sh <platform-tag> [root]}"
ROOT="${2:-/opt/clinicalomx_data_platform}"
RELEASE="$ROOT/releases/$TAG"

case "$TAG" in
    platform-v*) ;;
    *) echo "ERROR: '$TAG' is not a platform release tag (expected platform-v<N>)" >&2; exit 2 ;;
esac

PREFECT_DIR="$RELEASE/packages/clinicalomxdb/hub-pipelines/prefect"
SHARED_VENV="$PREFECT_DIR/.venv"
VM_DIR="$PREFECT_DIR/workers/vm-process"
VM_VENV="$VM_DIR/.venv"
PIPELINES_DIR="$RELEASE/packages/clinicalomx_pipelines"

PYTHON_SHARED="${PYTHON_SHARED:-python3}"
PYTHON_VM="${PYTHON_VM:-python3.11}"

LAMINDB_PIN="lamindb==2.8.1"

log() { echo "[build-envs] $*"; }

[ -d "$RELEASE" ] || {
    echo "ERROR: $RELEASE does not exist. Assemble the release first:" >&2
    echo "       $ROOT/current/scripts/release/assemble-release.sh $TAG" >&2
    exit 1
}
[ -d "$PREFECT_DIR" ] || { echo "ERROR: $PREFECT_DIR not found; is $RELEASE a monorepo release tree?" >&2; exit 1; }

log "release  $RELEASE"

if [ "${REBUILD:-0}" = "1" ]; then
    log "REBUILD=1: removing the three venvs"
    rm -rf "$SHARED_VENV" "$VM_VENV" "$PIPELINES_DIR/.venv"
fi

# --- 1. the shared prefect venv ---------------------------------------------------------------
# Pinned by requirements.txt, which does not carry lamindb; the exact lamindb pin is installed
# after it and must match every other environment that touches the instance.
if [ -x "$SHARED_VENV/bin/prefect" ]; then
    log "shared prefect venv present at $SHARED_VENV, keeping it"
else
    command -v "$PYTHON_SHARED" >/dev/null || {
        echo "ERROR: $PYTHON_SHARED not found; set PYTHON_SHARED to an interpreter that exists" >&2
        exit 1
    }
    log "building the shared prefect venv with $PYTHON_SHARED"
    "$PYTHON_SHARED" -m venv "$SHARED_VENV"
    "$SHARED_VENV/bin/pip" install --upgrade pip
    "$SHARED_VENV/bin/pip" install -r "$PREFECT_DIR/requirements.txt"
    "$SHARED_VENV/bin/pip" install -e "$PREFECT_DIR/workers/hpc" --no-deps
    "$SHARED_VENV/bin/pip" install "$LAMINDB_PIN"
fi

# --- 2. the vm-process venv -------------------------------------------------------------------
# uv, not pip: only uv reads [tool.uv.sources], which is what points clinicalomx at the release
# tree. uv is pip-installed INTO this venv from PyPI: it is a build tool for this one install and
# nothing at run time calls it.
if [ -x "$VM_VENV/bin/prefect" ]; then
    log "vm-process venv present at $VM_VENV, keeping it"
else
    command -v "$PYTHON_VM" >/dev/null || {
        echo "ERROR: $PYTHON_VM not found. clinicalomx needs Python >=3.11 and the VM default" >&2
        echo "       python3 is 3.10; install it (deadsnakes: apt install $PYTHON_VM ${PYTHON_VM}-venv)" >&2
        echo "       or set PYTHON_VM." >&2
        exit 1
    }
    log "building the vm-process venv with $PYTHON_VM"
    "$PYTHON_VM" -m venv "$VM_VENV"
    "$VM_VENV/bin/pip" install --upgrade pip uv
    log "installing vm-process (+ clinicalomx from its path source) into $VM_VENV"
    "$VM_VENV/bin/uv" pip install --python "$VM_VENV/bin/python" -e "$VM_DIR"
fi

# --- 2b. refresh clinicalomx-pipeline's ENTRY POINTS (not its code) ---------------------------
# Outside the guard on purpose; see the header. Editable so the metadata tracks the release tree
# the deployments' set_working_directory already points flows at, --no-deps because everything is
# pinned above and lamindb in particular must not be perturbed.
log "refreshing clinicalomx-pipeline entry-point metadata from $PIPELINES_DIR"
"$VM_VENV/bin/pip" install -e "$PIPELINES_DIR" --no-deps --quiet

# --- 2c. the probe, as a gate ------------------------------------------------------------------
# PYTHONDONTWRITEBYTECODE, because the probe IMPORTS each consumer and a release tree is
# meant to be immutable: __pycache__ is gitignored here, but not writing it is simpler than
# relying on that.
log "probing the three clinicalomx.* entry-point groups"
PYTHONDONTWRITEBYTECODE=1 "$VM_VENV/bin/python" - <<'PY'
import sys
from importlib.metadata import entry_points

groups = ("clinicalomx.vocabulary", "clinicalomx.checks", "clinicalomx.governance")
bad = 0
for group in groups:
    eps = list(entry_points(group=group))
    if not eps:
        print(f"MISSING {group}: no entry point declares this group")
        bad += 1
        continue
    for ep in eps:
        try:
            ep.load()
            print("ok", group, ep.value)
        except Exception as exc:
            print(f"BROKEN {group}: {ep.value} -> {type(exc).__name__}: {exc}")
            bad += 1
sys.exit(1 if bad else 0)
PY

# --- 3. the clinicalomx_pipelines environment ---------------------------------------------------
UV="${UV:-$(command -v uv || echo "$VM_VENV/bin/uv")}"
[ -x "$UV" ] || { echo "ERROR: no uv found. Put uv on PATH or set UV to its path." >&2; exit 1; }
log "syncing $PIPELINES_DIR with $UV"
"$UV" sync --directory "$PIPELINES_DIR" --extra prefect

# --- 4. the release tree must still be clean -----------------------------------------------------
# All three venvs are gitignored, so anything reported here is a real change inside a tree that is
# supposed to be immutable. The realistic one is uv.lock: `uv sync` re-resolves when the committed
# lock does not satisfy the metadata, and a release that silently relocks is not the release that
# was tagged.
if git -C "$RELEASE" rev-parse --git-dir >/dev/null 2>&1; then
    dirty="$(git -C "$RELEASE" status --porcelain)"
    if [ -n "$dirty" ]; then
        {
            echo "ERROR: the release tree is no longer clean; a release is immutable."
            echo "$dirty"
            echo "       A uv.lock line means the committed lock did not satisfy the metadata and"
            echo "       uv re-resolved. Fix the lock on main and cut a new tag; do not keep this"
            echo "       tree. Anything else listed is a change this build made that no .gitignore"
            echo "       covers, and is a finding to report rather than to clean up in place."
        } >&2
        exit 1
    fi
else
    log "NOTE: $RELEASE is not a git checkout, so the clean-tree check was skipped"
fi

log "done. three environments built under $RELEASE; current stays untouched: $(readlink "$ROOT/current" 2>/dev/null || echo '<not set>')"
