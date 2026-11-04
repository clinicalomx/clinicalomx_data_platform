#!/usr/bin/env bash
# Deploy the HPC Prefect worker systemd unit from this repository.
#
# Idempotent and re-runnable. It renders the versioned .service TEMPLATE
# (substituting the repo path, run user and its HOME) into /etc/systemd/system so
# the unit is correct wherever the repo is checked out. It also migrates away from
# earlier unit names (see OLD_UNITS) so you never end up with two workers at once.
#
# Run from anywhere; it re-execs itself with sudo when needed.
set -euo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"

# Re-exec as root if not already.
if [ "$(id -u)" -ne 0 ]; then
    exec sudo bash "$SELF" "$@"
fi

SCRIPT_DIR="$(dirname "$SELF")"
UNIT_TEMPLATE="$SCRIPT_DIR/hpc-ssh-pool-worker.service"
UNIT_NAME="$(basename "$UNIT_TEMPLATE")"
UNIT_DST="/etc/systemd/system/$UNIT_NAME"
# Legacy unit name this replaces: an earlier copied unit. It is disabled and
# removed so exactly one worker survives.
OLD_UNITS="prefect-worker.service"
PREFECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"   # hub-pipelines/prefect
VENV="$PREFECT_DIR/.venv"
WORKER_ENV="$PREFECT_DIR/worker.env"
SITE_ENV="$(cd "$SCRIPT_DIR/../../../../../.." && pwd)/site.env"

# The worker runs as whoever owns the checkout (ubuntu on the VM), so it shares
# that user's HOME and therefore its ~/.ssh/config hpc alias + ControlMaster
# socket. Derive it rather than hardcoding /home/ubuntu, so the unit is correct
# wherever the repo lives.
RUN_USER="$(stat -c '%U' "$PREFECT_DIR")"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"

echo "[deploy] unit template: $UNIT_TEMPLATE"
echo "[deploy] unit name    : $UNIT_NAME"
echo "[deploy] prefect dir  : $PREFECT_DIR"
echo "[deploy] run user     : $RUN_USER (HOME=$RUN_HOME)"

# Prerequisites.
[ -f "$UNIT_TEMPLATE" ] || { echo "ERROR: unit template not found: $UNIT_TEMPLATE" >&2; exit 1; }
[ -x "$VENV/bin/prefect" ] || {
    echo "ERROR: worker venv missing at $VENV" >&2
    echo "       create it: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e . --no-deps" >&2
    exit 1
}
[ -f "$SITE_ENV" ] || {
    echo "ERROR: $SITE_ENV missing; render it: uv run scripts/site/render.py" >&2
    exit 1
}
[ -f "$WORKER_ENV" ] || {
    echo "ERROR: $WORKER_ENV missing (copy worker.env.example to worker.env and fill it in)" >&2
    exit 1
}

# Migrate away from any previously installed unit under a different name.
for OLD_UNIT in $OLD_UNITS; do
    if [ "$OLD_UNIT" != "$UNIT_NAME" ] && \
       systemctl list-unit-files "$OLD_UNIT" >/dev/null 2>&1; then
        echo "[deploy] removing legacy unit $OLD_UNIT"
        systemctl disable --now "$OLD_UNIT" 2>/dev/null || true
        rm -f "/etc/systemd/system/$OLD_UNIT"
    fi
done

# Render the template to /etc/systemd/system with the resolved paths and user.
# rm first: an earlier version symlinked the repo file here, and writing through
# a surviving symlink would clobber the committed template.
echo "[deploy] rendering $UNIT_NAME -> $UNIT_DST"
rm -f "$UNIT_DST"
sed -e "s#@PREFECT_DIR@#$PREFECT_DIR#g" \
    -e "s#@SITE_ENV@#$SITE_ENV#g" \
    -e "s#@RUN_USER@#$RUN_USER#g" \
    -e "s#@RUN_HOME@#$RUN_HOME#g" \
    "$UNIT_TEMPLATE" > "$UNIT_DST"

systemctl daemon-reload
systemctl enable "$UNIT_NAME"
systemctl restart "$UNIT_NAME"

# Verify.
sleep 3
if systemctl is-active --quiet "$UNIT_NAME"; then
    echo "[deploy] $UNIT_NAME is active"
    systemctl status "$UNIT_NAME" --no-pager -n 3 | sed -n '1,4p' || true
    echo "[deploy] done. Reminder: run 'ssh hpc echo ok' as the ubuntu user (DUO) before submitting flows."
else
    echo "ERROR: $UNIT_NAME failed to start. Recent logs:" >&2
    journalctl -u "$UNIT_NAME" --no-pager -n 20 >&2
    exit 1
fi
