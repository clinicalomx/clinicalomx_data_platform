#!/usr/bin/env bash
# Install the portal's HPC login terminal.
#
# ONE terminal, not one per user. It runs hpc-login.sh, which asks which HPC account to
# authenticate; ControlPath's %r token gives each account its own ControlMaster socket without an
# ssh_config entry or a systemd unit per user. Adding a connection is therefore a login on the
# portal rather than an operator provisioning step.
#
# What a login does NOT do is make that account able to run flows. Submission also needs an ssh
# alias, a Prefect connection block, a work pool and a worker, provisioned from users.conf by
# hub-pipelines/prefect/workers/hpc/bootstrap-users.sh. The portal's connection table shows
# which half is in place per user, so the gap is visible rather than surprising.
#
# ttyd comes from the Ubuntu archive (jammy/universe), so it arrives through the signed package
# path rather than as a downloaded binary.
#
# Idempotent and re-runnable. Run from anywhere; re-execs itself with sudo.
#
# Overridable environment:
#   HPC_LOGIN_HOST  HPC login host (default: hpc.login_host from site.env)
#   TTYD_CREDENTIAL   optional basic-auth "user:password" on the terminal. Off by default: with
#                     no TCP port it is reachable only through the portal, which is loopback-only
#                     behind the operator's own SSH tunnel, so anyone who can reach it already
#                     has shell access to this host as the account it runs as. Offered for
#                     defence in depth, not because it closes a gap the tunnel leaves open.
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

if [ "$(id -u)" -ne 0 ]; then
    exec sudo \
        HPC_LOGIN_HOST="${HPC_LOGIN_HOST:-}" \
        TTYD_CREDENTIAL="${TTYD_CREDENTIAL:-}" \
        bash "$SELF" "$@"
fi

TTYD_DIR="$(dirname "$SELF")"
REPO_DIR="$(cd "$TTYD_DIR/../.." && pwd)"
UNIT_TEMPLATE="$TTYD_DIR/ops-ttyd.service"
UNIT_NAME="ops-ttyd.service"
# The deployed destination is the default. The override exists so
# tests/explorer/test_ops_bootstrap_paths.py can run this script whole as an ordinary user.
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
DROPIN_DIR="$SYSTEMD_DIR/$UNIT_NAME.d"
LOGIN_SCRIPT="$TTYD_DIR/hpc-login.sh"
SITE_ENV="$(cd "$TTYD_DIR/../../../.." && pwd)/site.env"
[ -f "$SITE_ENV" ] || { echo "ERROR: $SITE_ENV missing; render it: uv run scripts/site/render.py" >&2; exit 1; }
set -a; . "$SITE_ENV"; set +a
HPC_LOGIN_HOST="${HPC_LOGIN_HOST:-$CLINICALOMX_SITE_HPC_LOGIN_HOST}"

# Must run as the account that owns the checkout, because that is where the sockets the workers
# reuse have to land.
RUN_USER="$(stat -c '%U' "$REPO_DIR")"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"

log() { echo "[ttyd] $*"; }

[ -f "$UNIT_TEMPLATE" ] || { echo "ERROR: unit template not found: $UNIT_TEMPLATE" >&2; exit 1; }
[ -f "$LOGIN_SCRIPT" ] || { echo "ERROR: login script not found: $LOGIN_SCRIPT" >&2; exit 1; }
bash -n "$LOGIN_SCRIPT" || { echo "ERROR: $LOGIN_SCRIPT does not parse" >&2; exit 1; }
chmod +x "$LOGIN_SCRIPT"

log "run user: $RUN_USER (HOME=$RUN_HOME), login host: $HPC_LOGIN_HOST"

# --- 1. ttyd ---------------------------------------------------------------------------
if ! command -v ttyd >/dev/null; then
    log "installing ttyd from the Ubuntu archive"
    apt-get update -qq
    apt-get install -y -qq ttyd
fi
log "ttyd: $(ttyd --version 2>&1 | head -1)"

# Assert the flags this depends on rather than trusting a version string. 1.6.3, the jammy
# package, has no --writable: writable is the DEFAULT there and the suppressing flag is
# --readonly, so passing --writable (as an earlier plan did) aborts on an unknown option.
ttyd --help 2>&1 | grep -q -- "--base-path" \
    || { echo "ERROR: this ttyd lacks --base-path; it cannot be served under a subpath" >&2; exit 1; }
ttyd --help 2>&1 | grep -q -- "--interface" \
    || { echo "ERROR: this ttyd lacks --interface; it cannot listen on a unix socket" >&2; exit 1; }
if ttyd --help 2>&1 | grep -q -- "--writable"; then
    log "NOTE: this ttyd has --writable; the unit does not pass it and does not need to"
fi

# --- 2. retire the previous per-user units ----------------------------------------------
# An earlier design ran one ops-ttyd@USER instance per user. Leaving those behind would keep
# stale sockets in the directory Caddy reads, so they are removed here.
for unit in $(systemctl list-units --all --plain --no-legend 'ops-ttyd@*' 2>/dev/null | awk '{print $1}'); do
    log "retiring superseded $unit"
    systemctl disable --now "$unit" || true
done
if [ -f "$SYSTEMD_DIR/ops-ttyd@.service" ]; then
    log "removing superseded template ops-ttyd@.service"
    rm -f "$SYSTEMD_DIR/ops-ttyd@.service"
    rm -rf "$SYSTEMD_DIR/ops-ttyd@.service.d"
fi
rm -f /run/ops-ttyd/*.sock 2>/dev/null || true

# --- 3. the unit -----------------------------------------------------------------------
# What the unit that is RUNNING right now executes, read before the render overwrites the file.
# `enable --now` starts a stopped unit and leaves a live one alone, so re-pointing this terminal
# does not reach a process that is already up; this is the last moment the old path can be read.
PREV_ACTIVE="$(systemctl is-active "$UNIT_NAME" 2>/dev/null || true)"
PREV_EXEC="$(systemctl show -p ExecStart --value "$UNIT_NAME" 2>/dev/null || true)"

log "installing $UNIT_NAME"
rm -f "$SYSTEMD_DIR/$UNIT_NAME"
sed -e "s#@RUN_USER@#$RUN_USER#g" \
    -e "s#@RUN_HOME@#$RUN_HOME#g" \
    -e "s#@TTYD_DIR@#$TTYD_DIR#g" \
    -e "s#@HPC_LOGIN_HOST@#$HPC_LOGIN_HOST#g" \
    "$UNIT_TEMPLATE" > "$SYSTEMD_DIR/$UNIT_NAME"

# Basic auth, when asked for, goes in a drop-in so the committed template stays the single
# description of the service and the credential is not rendered into a world-readable unit.
rm -rf "$DROPIN_DIR"
if [ -n "${TTYD_CREDENTIAL:-}" ]; then
    log "applying basic authentication to the terminal"
    mkdir -p "$DROPIN_DIR"
    {
        printf '[Service]\n'
        printf '# Credential from TTYD_CREDENTIAL, kept out of the committed unit.\n'
        printf 'Environment=TTYD_CREDENTIAL=%s\n' "$TTYD_CREDENTIAL"
        printf 'ExecStart=\n'
        printf 'ExecStart=/usr/bin/ttyd --interface /run/ops-ttyd/login.sock --base-path /ssh'
        printf ' --credential ${TTYD_CREDENTIAL}'
        printf ' --terminal-type xterm-256color --ping-interval 30'
        printf ' %s/hpc-login.sh\n' "$TTYD_DIR"
    } > "$DROPIN_DIR/credential.conf"
    chmod 600 "$DROPIN_DIR/credential.conf"
fi

systemctl daemon-reload
systemctl enable --now "$UNIT_NAME"

# A terminal that was already up still runs the hpc-login.sh of the tree it started from, and
# the socket check at the end cannot tell the difference. This script does NOT restart it: the
# ssh processes it spawned ARE the DUO ControlMasters, they sit in this unit's cgroup, and
# whether a restart takes them with it is a deployed property nobody has measured. Losing them
# costs every HPC user a fresh DUO, so the choice belongs to the operator, stated not guessed.
case "$PREV_ACTIVE:$PREV_EXEC" in
    active:*"$TTYD_DIR"*) ;;
    active:*) log "NOT LIVE YET: the running unit still executes $PREV_EXEC"
              log "             restart it to pick this render up: systemctl restart $UNIT_NAME"
              log "             CONFIRM FIRST: that may end every live ControlMaster session" ;;
esac

# --- 3b. restart the health endpoint ----------------------------------------------------
# Not housekeeping: the portal's connection table and this endpoint share a JSON contract, and
# they update by different mechanisms. The page is a bind-mounted file that a git pull publishes
# immediately, while the endpoint is a long-running process holding whatever module it started
# with. So a pull that changes both leaves the page expecting fields the endpoint does not yet
# emit, and the table can only report that mismatch rather than any real state. Restarting here
# keeps the pair in step for anyone who deploys by running the bootstraps.
HEALTH_UNIT=ops-health.service
if systemctl list-unit-files "$HEALTH_UNIT" >/dev/null 2>&1 \
   && [ -n "$(systemctl list-unit-files --no-legend "$HEALTH_UNIT" 2>/dev/null)" ]; then
    log "restarting $HEALTH_UNIT so it serves the current code"
    systemctl restart "$HEALTH_UNIT" || log "WARNING: could not restart $HEALTH_UNIT"
else
    log "NOTE: $HEALTH_UNIT is not installed; run 'make ops-health' to get the connection table"
fi

# --- 4. verify it is actually serving ---------------------------------------------------
for _ in $(seq 1 15); do
    if [ -S /run/ops-ttyd/login.sock ] \
       && curl -fsS --max-time 5 --unix-socket /run/ops-ttyd/login.sock http://localhost/ssh/ >/dev/null 2>&1; then
        log "$UNIT_NAME is serving on unix:/run/ops-ttyd/login.sock"
        log "portal path: /ssh/  (the connection table's + button opens it)"
        log "reminder: run 'make ops-up' if Caddy has not been recreated with the /run/ops-ttyd mount"
        exit 0
    fi
    sleep 1
done

echo "ERROR: $UNIT_NAME did not answer on its socket. Recent logs:" >&2
journalctl -u "$UNIT_NAME" --no-pager -n 30 >&2
exit 1
