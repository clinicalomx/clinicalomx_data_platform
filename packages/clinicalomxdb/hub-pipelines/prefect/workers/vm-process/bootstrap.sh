#!/usr/bin/env bash
# Idempotent bootstrap of the vm-pool Prefect worker (workers/vm-process).
#
# Unlike the shared-venv pools (see workers/hpc/bootstrap.sh), vm-pool runs on a
# DEDICATED venv with heavier deps and the private `clinicalomx` dependency. This script:
#   1. writes workers/vm-process/.env (LAMIN_API_KEY, from hub-database/.env)
#   2. builds the dedicated venv and installs this package with uv (clinicalomx from its
#      path source in this checkout)
#   2b. refreshes clinicalomx-pipeline's entry-point metadata (editable, --no-deps) so the
#      vocabulary/checks/governance consumers this instance declares actually resolve
#   3. waits for the Prefect API, ensures the vm-pool work pool exists
#   4. renders + starts the vm-pool systemd unit
#
# Safe to re-run: an existing venv and .env are left as-is.
#
# NO credential is needed to install: clinicalomx resolves from the path source in pyproject.toml,
# never from an index or a git clone. cloud-init may still pass GHCR_USERNAME/COMX_CI_READ; they
# are ignored.
#
# Optional:
#   PREFECT_API_URL            default http://localhost:4200/api
#   PYTHON                     interpreter for the dedicated venv (default python3.11).
#                              clinicalomx requires >=3.11, and the VM's default python3 is
#                              3.10, so this venv must be built with a newer interpreter.
#   PIPELINE_DIR               clinicalomx_pipeline working tree, default
#                              /home/ubuntu/clinicalomx_pipeline. Installed EDITABLE and
#                              --no-deps for its entry-point metadata only (step 2b); must be
#                              the same tree the deployments' set_working_directory names.
set -euo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
if [ "$(id -u)" -ne 0 ]; then
    exec sudo \
        PREFECT_API_URL="${PREFECT_API_URL:-}" PYTHON="${PYTHON:-}" \
        PIPELINE_DIR="${PIPELINE_DIR:-}" \
        bash "$SELF" "$@"
fi

SCRIPT_DIR="$(dirname "$SELF")"                     # .../hub-pipelines/prefect/workers/vm-process
PREFECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"      # .../hub-pipelines/prefect
REPO_DIR="$(cd "$PREFECT_DIR/../.." && pwd)"        # repo root
VENV="$SCRIPT_DIR/.venv"                            # DEDICATED venv (not the shared one)
ENV_FILE="$SCRIPT_DIR/.env"
DB_ENV="$REPO_DIR/hub-database/.env"
SITE_ENV="$(cd "$SCRIPT_DIR/../../../../../.." && pwd)/site.env"
[ -f "$SITE_ENV" ] || { echo "ERROR: $SITE_ENV missing; render it: uv run scripts/site/render.py" >&2; exit 1; }
set -a; . "$SITE_ENV"; set +a

RUN_USER="$(stat -c '%U' "$SCRIPT_DIR")"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
PREFECT_API_URL="${PREFECT_API_URL:-http://localhost:4200/api}"
# clinicalomx requires Python >=3.11; the VM default python3 is 3.10, so build this venv
# with a newer interpreter (provisioned by cloud-init via deadsnakes).
PYTHON="${PYTHON:-python3.11}"

log() { echo "[vm-process] $*"; }

# Read one key out of hub-database/.env. Never logs the value.
db_env_value() { grep -E "^$1=" "$DB_ENV" 2>/dev/null | head -1 | cut -d= -f2-; }

# --- 1. .env with LAMIN_API_KEY (reused from the hub-database env; never clobber) ---
if [ ! -f "$ENV_FILE" ]; then
    log "writing $ENV_FILE"
    [ -f "$DB_ENV" ] || { echo "ERROR: $DB_ENV not found (needed for LAMIN_API_KEY)" >&2; exit 1; }
    key="$(db_env_value LAMIN_API_KEY)"
    [ -n "$key" ] || { echo "ERROR: LAMIN_API_KEY not set in $DB_ENV" >&2; exit 1; }
    umask 077
    printf 'LAMIN_API_KEY=%s\n' "$key" > "$ENV_FILE"
    chown "$RUN_USER:$RUN_USER" "$ENV_FILE"
else
    log "$ENV_FILE present, keeping it"
fi

# --- 2. dedicated venv + install (clinicalomx from its path source) ---
#
# uv, not pip: only uv reads [tool.uv.sources], which is what points clinicalomx at this
# checkout. uv is installed INTO this venv from PyPI rather than provisioned on the host: it is a
# build tool for this one install, nothing at runtime calls it, and taking it from PyPI keeps
# cloud-init out of it.

if [ ! -x "$VENV/bin/prefect" ]; then
    command -v "$PYTHON" >/dev/null || {
        echo "ERROR: $PYTHON not found. clinicalomx needs Python >=3.11; install it" >&2
        echo "       (e.g. deadsnakes: apt install $PYTHON ${PYTHON}-venv) or set PYTHON." >&2
        exit 1
    }
    log "creating dedicated venv at $VENV with $PYTHON as $RUN_USER"
    sudo -u "$RUN_USER" "$PYTHON" -m venv "$VENV"
    sudo -u "$RUN_USER" "$VENV/bin/pip" install --upgrade pip uv

    log "installing vm-process package (+ clinicalomx from its path source) into $VENV"
    sudo -u "$RUN_USER" "$VENV/bin/uv" pip install --python "$VENV/bin/python" -e "$SCRIPT_DIR"
else
    log "dedicated venv present at $VENV, skipping build"
fi

# --- 2b. register clinicalomx-pipeline's ENTRY POINTS (not its code) ---
# Deliberately OUTSIDE the guard above. The guard skips an existing venv, and this is exactly the
# thing that must be refreshed on every run: entry points are read from INSTALLED distribution
# metadata, so after a refactor that moves a module the installed metadata still names the old path
# and `clinicalomx.vocab.discover()` logs
#   WARNING could not load vocabulary 'clinicalomx-pipeline': No module named ...
# then carries on with that consumer missing. The governance table failing to load the same way
# denied every folder on the HPC for weeks, silently, because deny-by-default looks identical to a
# policy that was loaded and said no.
#
# EDITABLE and --no-deps on purpose. Editable so the metadata tracks the working tree that
# `set_working_directory` already points flows at, rather than a copy that drifts from it. --no-deps
# because every dependency is pinned above, and lamindb in particular must not be perturbed.
# This installs no flow code that was not already going to be used: same tree, one source of truth.
# :- not :=, because the sudo re-exec above passes PIPELINE_DIR through as an EMPTY
# string when it was unset, and an empty value must still fall back to the default.
PIPELINE_DIR="${PIPELINE_DIR:-/home/ubuntu/clinicalomx_pipeline}"
if [ -d "$PIPELINE_DIR" ]; then
    log "refreshing clinicalomx-pipeline entry-point metadata from $PIPELINE_DIR"
    sudo -u "$RUN_USER" "$VENV/bin/pip" install -e "$PIPELINE_DIR" --no-deps --quiet
    sudo -u "$RUN_USER" "$VENV/bin/python" - <<'PY'
from importlib.metadata import entry_points
for group in ("clinicalomx.vocabulary", "clinicalomx.checks", "clinicalomx.governance"):
    for ep in entry_points(group=group):
        try:
            ep.load()
            print(f"  ok      {group}: {ep.value}")
        except Exception as exc:
            print(f"  BROKEN  {group}: {ep.value} -> {type(exc).__name__}: {exc}")
PY
else
    log "WARNING: $PIPELINE_DIR not found; clinicalomx-pipeline entry points will NOT resolve"
    log "         (flows still run - they call vocab.ensure()/governance.register() directly)"
fi

# --- 3. wait for the Prefect API, ensure the vm-pool work pool exists ---
log "waiting for Prefect API at $PREFECT_API_URL/health"
ready=0
for _ in $(seq 1 60); do
    if curl -fsS "$PREFECT_API_URL/health" >/dev/null 2>&1; then ready=1; break; fi
    sleep 5
done
[ "$ready" -eq 1 ] || { echo "ERROR: Prefect API not healthy after 300s" >&2; exit 1; }

log "ensuring vm-pool work pool exists"
sudo -u "$RUN_USER" env PREFECT_API_URL="$PREFECT_API_URL" \
    "$VENV/bin/prefect" work-pool create vm-pool --type process 2>/dev/null \
    || log "vm-pool already exists"

# --- 4. render + (re)start the vm-pool systemd unit ---
UNIT_NAME="vm-pool-worker.service"
UNIT_DST="/etc/systemd/system/$UNIT_NAME"
log "installing unit $UNIT_NAME"
rm -f "$UNIT_DST"
sed -e "s#@PREFECT_DIR@#$PREFECT_DIR#g" \
    -e "s#@RUN_USER@#$RUN_USER#g" \
    -e "s#@RUN_HOME@#$RUN_HOME#g" \
    "$SCRIPT_DIR/$UNIT_NAME" > "$UNIT_DST"
systemctl daemon-reload
systemctl enable "$UNIT_NAME"
systemctl restart "$UNIT_NAME"

log "done. vm-pool worker installed and started."
