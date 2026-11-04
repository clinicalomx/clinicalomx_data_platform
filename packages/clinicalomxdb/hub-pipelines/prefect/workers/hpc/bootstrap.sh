#!/usr/bin/env bash
# One-command, idempotent bootstrap of the VM-side Prefect workers that run on the
# SHARED venv: the HPC SSH-push worker (hpc-ssh-pool) and the pipeline
# orchestration worker (pipeline-pool). Both live in hub-pipelines/prefect/.venv.
#
# Collapses the manual post-boot sequence into a single re-runnable script:
#   1. create the shared venv from the pinned lockfile, add the worker entry point
#      and lamindb (needed to resolve sif_artifact and by the pipeline-pool flows)
#   2. write prefect/worker.env (the LOCAL Prefect + MLflow addresses only; no credential)
#   3. wait for the Prefect API, then register the SSH block + hpc work pool
#   4. render and start the hpc + pipeline-pool systemd units
#
# NOT handled here: the vm-pool worker (workers/vm-process) runs on a DEDICATED venv
# with heavier deps and the private `clinicalomx` repo, which is not provisioned on the
# VM; deploy it separately once those exist.
#
# Safe to re-run: an existing venv and an existing worker.env are left untouched,
# and deploy.py / the unit installs are themselves idempotent.
#
# Invoked automatically by cloud-init on the VM, and runnable by hand on any host.
# The ONE thing this cannot do is the interactive DUO auth. When it finishes, run
# once as the worker user (never sudo):  ssh hpc echo ok
#
# Site values come from site.env at the repository root, which scripts/site/render.py writes from
# site.yaml: the external Prefect and MLflow addresses HPC jobs report back to, the primary HPC
# user, and the work pool's SLURM defaults. The worker units read the same file.
#
# Overridable environment:
#   PREFECT_API_URL           Prefect API this host talks to (default http://localhost:4200/api)
#   MLFLOW_TRACKING_URI       MLflow URL this host's own workers use; defaults to
#                             http://localhost:5000, and must NOT be the floating IP
#   HPC_HOST / HPC_USER   ssh alias + HPC username (default hpc / the primary HPC user)
set -euo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"

# Re-exec as root if needed: installing the systemd unit requires it. Forward the
# overridable vars explicitly rather than relying on `sudo -E` (which only preserves
# env if the sudoers policy allows it), so a hand-run `HPC_USER=... bootstrap.sh`
# survives the privilege escalation on any sudo configuration.
if [ "$(id -u)" -ne 0 ]; then
    exec sudo \
        PREFECT_API_URL="${PREFECT_API_URL:-}" \
        MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-}" \
        HPC_HOST="${HPC_HOST:-}" \
        HPC_USER="${HPC_USER:-}" \
        bash "$SELF" "$@"
fi

SCRIPT_DIR="$(dirname "$SELF")"                      # .../hub-pipelines/prefect/workers/hpc
PREFECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"       # .../hub-pipelines/prefect
VENV="$PREFECT_DIR/.venv"
WORKER_ENV="$PREFECT_DIR/worker.env"
SITE_ENV="$(cd "$SCRIPT_DIR/../../../../../.." && pwd)/site.env"
[ -f "$SITE_ENV" ] || { echo "ERROR: $SITE_ENV missing; render it: uv run scripts/site/render.py" >&2; exit 1; }
set -a; . "$SITE_ENV"; set +a
# deploy.py runs through `sudo -u`, which drops the environment, so the site values are passed to
# it by name. render.py refuses whitespace in a value, which is what makes the word split safe.
SITE_VARS="$(grep -E '^[A-Z][A-Z0-9_]*=' "$SITE_ENV")"

WORKERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"         # .../hub-pipelines/prefect/workers
RUN_USER="$(stat -c '%U' "$PREFECT_DIR")"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
PREFECT_API_URL="${PREFECT_API_URL:-http://localhost:4200/api}"
HPC_HOST="${HPC_HOST:-hpc}"
HPC_USER="${HPC_USER:-$CLINICALOMX_SITE_HPC_USER}"
# lamindb is layered on top of the frozen requirements.txt rather than added to it:
# it drags a large transitive tree that would force a re-resolve of the whole lock.
# Pinned to match the vm-pipeline/vm-process pyprojects.
# Must equal the instance DB schema and every other environment's pin: a package BEHIND the
# schema refuses to connect, and 2.8.x changed Artifact.suffix derivation, so a mixed fleet writes
# two spellings of `.qptiff` and silently breaks filter(suffix=...) queries.
LAMINDB_VERSION="2.8.1"

log() { echo "[bootstrap] $*"; }

# Render a worker unit TEMPLATE (@PREFECT_DIR@/@RUN_USER@/@RUN_HOME@) into
# /etc/systemd/system and (re)start it. rm first: an earlier version symlinked the
# repo file here, and writing through a surviving symlink would clobber the template.
install_unit() {
    local template="$1" name="$2" dst="/etc/systemd/system/$2"
    [ -f "$template" ] || { echo "ERROR: unit template not found: $template" >&2; return 1; }
    log "installing unit $name from $template"
    rm -f "$dst"
    sed -e "s#@PREFECT_DIR@#$PREFECT_DIR#g" \
        -e "s#@RUN_USER@#$RUN_USER#g" \
        -e "s#@RUN_HOME@#$RUN_HOME#g" \
        "$template" > "$dst"
    systemctl daemon-reload
    systemctl enable "$name"
    systemctl restart "$name"
}

# --- 1a. venv + pinned deps (heavy; only when the venv is absent) ---
if [ ! -x "$VENV/bin/prefect" ]; then
    log "creating venv at $VENV as $RUN_USER"
    sudo -u "$RUN_USER" python3 -m venv "$VENV"
    sudo -u "$RUN_USER" "$VENV/bin/pip" install --upgrade pip
    sudo -u "$RUN_USER" "$VENV/bin/pip" install -r "$PREFECT_DIR/requirements.txt"
else
    log "venv present at $VENV, skipping deps"
fi

# --- 1b. editable install of the worker package (workers/hpc, i.e. SCRIPT_DIR,
#     NOT prefect/). This is what registers the prefect.collections entry point so
#     `prefect worker start --type hpc-slurm-ssh-pusher` can resolve the type.
#     Cheap and idempotent, so it runs every time to self-heal a venv that was
#     built before this install target was corrected. ---
log "installing worker package (entry point) from $SCRIPT_DIR"
sudo -u "$RUN_USER" "$VENV/bin/pip" install -e "$SCRIPT_DIR" --no-deps

# --- 1c. lamindb (guarded): the hpc worker imports it to resolve sif_artifact, and
#     the pipeline-pool flows query it. Only install when absent so re-runs stay fast. ---
if ! sudo -u "$RUN_USER" "$VENV/bin/python" -c "import lamindb" 2>/dev/null; then
    log "installing lamindb==$LAMINDB_VERSION into shared venv"
    sudo -u "$RUN_USER" "$VENV/bin/pip" install "lamindb==$LAMINDB_VERSION"
fi

# --- 2. worker.env (never clobber a hand-edited one) ---
if [ ! -f "$WORKER_ENV" ]; then
    log "writing $WORKER_ENV"
    # No composed database URL here. One was composed from the hub-database env and written to every
    # worker.env, and nothing ever read it: no consumer in the hpc worker package and none in
    # clinicalomx_pipeline. It carried the Postgres SUPERUSER credential for a reader that does
    # not exist. Code that does need the instance calls lamindb, which resolves the database
    # through LaminHub rather than being told one.
    # See docs/runbooks/lamindb-connection.md before reintroducing anything of this shape.
    # Only the LOCAL addresses: the external ones HPC jobs use are site values, in site.env.
    # They differ because Nectar does not hairpin the floating IP back to the instance, so a
    # VM-side flow using the external address times out.
    MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-http://localhost:5000}"
    umask 077
    cat > "$WORKER_ENV" <<EOF
PREFECT_API_URL=$PREFECT_API_URL
MLFLOW_TRACKING_URI=$MLFLOW_TRACKING_URI
EOF
    chown "$RUN_USER:$RUN_USER" "$WORKER_ENV"
else
    log "worker.env present, keeping it"
fi

# --- 3. wait for the Prefect API, then register blocks + work pool ---
log "waiting for Prefect API at $PREFECT_API_URL/health"
ready=0
for _ in $(seq 1 60); do
    if curl -fsS "$PREFECT_API_URL/health" >/dev/null 2>&1; then ready=1; break; fi
    sleep 5
done
[ "$ready" -eq 1 ] || { echo "ERROR: Prefect API not healthy after 300s" >&2; exit 1; }

log "registering SSH block + work pool via deploy.py"
# shellcheck disable=SC2086
sudo -u "$RUN_USER" env $SITE_VARS \
    PREFECT_API_URL="$PREFECT_API_URL" \
    HPC_HOST="$HPC_HOST" HPC_USER="$HPC_USER" \
    "$VENV/bin/python" "$SCRIPT_DIR/deploy.py"

# --- 4. render + (re)start the hpc worker systemd unit (deploy-service.sh keeps
#     the hpc-specific legacy-unit migration + DUO reminder) ---
log "installing hpc worker systemd unit"
bash "$SCRIPT_DIR/deploy-service.sh"

# --- 5. pipeline-pool worker (process type, same shared venv). Ensure the pool
#     exists, then render + start its unit. Its flows orchestrate the hpc leaves
#     and query lamindb; no SLURM allocation, so a plain process worker suffices. ---
log "ensuring pipeline-pool work pool exists"
sudo -u "$RUN_USER" env PREFECT_API_URL="$PREFECT_API_URL" \
    "$VENV/bin/prefect" work-pool create pipeline-pool --type process 2>/dev/null \
    || log "pipeline-pool already exists"
install_unit "$WORKERS_DIR/vm-pipeline/pipeline-pool-worker.service" "pipeline-pool-worker.service"

log "done. Final manual step (as $RUN_USER, NO sudo): ssh $HPC_HOST echo ok"
log "note: vm-pool (workers/vm-process) is not deployed here - it needs its own venv + the clinicalomx repo."
