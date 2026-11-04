#!/usr/bin/env bash
# Install the HPC login relay as a host systemd unit.
#
# Idempotent. Stdlib-only Python, so there is no venv and no package install: nothing here can
# fail because a package index is unreachable.
#
# Run from anywhere; re-execs itself with sudo.
#
# Overridable environment:
#   HPC_LOGIN_HOST   HPC login host (default: hpc.login_host from site.env)
#   LOGIN_SESSION_TTL  seconds a half-finished login may sit before its ssh child is reaped
set -euo pipefail

# The script's own location, with symlinks LEFT ALONE: readlink -f would name the physical
# releases/<tag> path, and a unit rendered there is abandoned by the next current/ flip. See
# hub-ops/health/bootstrap.sh for the whole reason; infra/systemd/README.md for the convention.
SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SELF="$SELF_DIR/$(basename -- "${BASH_SOURCE[0]}")"

case "$SELF" in
    */releases/platform-v*) echo "WARNING: rendering from a release tree directly, so the unit" \
        "below cannot survive the next current/ flip. Re-run from the current/ path." >&2 ;;
esac

OVERRIDABLE=(HPC_LOGIN_HOST LOGIN_SESSION_TTL LOGIN_MAX_SESSIONS)

if [ "$(id -u)" -ne 0 ]; then
    FORWARD=()
    for var in "${OVERRIDABLE[@]}"; do
        [ -n "${!var+set}" ] && FORWARD+=("$var=${!var}")
    done
    exec sudo "${FORWARD[@]}" bash "$SELF" "$@"
fi

LOGIN_DIR="$(dirname "$SELF")"
UNIT_TEMPLATE="$LOGIN_DIR/ops-login.service"
UNIT_NAME="ops-login.service"
# The deployed destination is the default. The override exists so
# tests/explorer/test_ops_bootstrap_paths.py can run this script whole as an ordinary user.
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
DROPIN_DIR="$SYSTEMD_DIR/$UNIT_NAME.d"
REPO_DIR="$(cd "$LOGIN_DIR/../.." && pwd)"
SITE_ENV="$(cd "$LOGIN_DIR/../../../.." && pwd)/site.env"
[ -f "$SITE_ENV" ] || { echo "ERROR: $SITE_ENV missing; render it: uv run scripts/site/render.py" >&2; exit 1; }
set -a; . "$SITE_ENV"; set +a
HPC_LOGIN_HOST="${HPC_LOGIN_HOST:-$CLINICALOMX_SITE_HPC_LOGIN_HOST}"

RUN_USER="$(stat -c '%U' "$REPO_DIR")"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"

log() { echo "[login] $*"; }

[ -f "$UNIT_TEMPLATE" ] || { echo "ERROR: unit template not found: $UNIT_TEMPLATE" >&2; exit 1; }
[ -f "$LOGIN_DIR/login_server.py" ] || { echo "ERROR: login_server.py not found" >&2; exit 1; }
python3 -c "import ast,sys; ast.parse(open('$LOGIN_DIR/login_server.py').read())" \
    || { echo "ERROR: login_server.py does not parse" >&2; exit 1; }
command -v ssh >/dev/null || { echo "ERROR: ssh not on PATH" >&2; exit 1; }

log "run user: $RUN_USER (HOME=$RUN_HOME), login host: $HPC_LOGIN_HOST"

# What the unit that is RUNNING right now executes, read before the render overwrites the file.
# `enable --now` starts a stopped unit and leaves a live one alone, so re-pointing this widget
# does not reach a process that is already up; this is the last moment the old path can be read.
PREV_ACTIVE="$(systemctl is-active "$UNIT_NAME" 2>/dev/null || true)"
PREV_EXEC="$(systemctl show -p ExecStart --value "$UNIT_NAME" 2>/dev/null || true)"

log "installing $UNIT_NAME"
rm -f "$SYSTEMD_DIR/$UNIT_NAME"
sed -e "s#@RUN_USER@#$RUN_USER#g" \
    -e "s#@RUN_HOME@#$RUN_HOME#g" \
    -e "s#@LOGIN_DIR@#$LOGIN_DIR#g" \
    -e "s#@HPC_LOGIN_HOST@#$HPC_LOGIN_HOST#g" \
    "$UNIT_TEMPLATE" > "$SYSTEMD_DIR/$UNIT_NAME"

rm -rf "$DROPIN_DIR"
OVERRIDES=""
for var in LOGIN_SESSION_TTL LOGIN_MAX_SESSIONS; do
    [ -n "${!var+set}" ] && OVERRIDES+="Environment=$var=${!var}"$'\n'
done
if [ -n "$OVERRIDES" ]; then
    mkdir -p "$DROPIN_DIR"
    printf '[Service]\n%s' "$OVERRIDES" > "$DROPIN_DIR/overrides.conf"
    log "wrote overrides drop-in"
fi

systemctl daemon-reload
systemctl enable --now "$UNIT_NAME"

# A relay that was already up is still serving the tree it started from, and the socket check
# below cannot tell the difference. Restarting it is the operator's call rather than this
# script's: the ssh children it holds are the DUO ControlMasters (docs/architecture/decisions/0005), so a restart
# may end every live session. Report the fact and name the command instead of running it.
case "$PREV_ACTIVE:$PREV_EXEC" in
    active:*"$LOGIN_DIR"*) ;;
    active:*) log "NOT LIVE YET: the running unit still executes $PREV_EXEC"
              log "             restart it to pick this render up: systemctl restart $UNIT_NAME"
              log "             that ends any half-finished login and may drop live sockets" ;;
esac

for _ in $(seq 1 15); do
    if [ -S /run/ops-login/login.sock ] \
       && curl -fsS --max-time 5 --unix-socket /run/ops-login/login.sock \
            http://localhost/login/config >/dev/null 2>&1; then
        log "$UNIT_NAME is serving on unix:/run/ops-login/login.sock"
        log "reminder: run 'make ops-up' if Caddy lacks the /run/ops-login mount"
        exit 0
    fi
    sleep 1
done

echo "ERROR: $UNIT_NAME did not answer on its socket. Recent logs:" >&2
journalctl -u "$UNIT_NAME" --no-pager -n 30 >&2
exit 1
