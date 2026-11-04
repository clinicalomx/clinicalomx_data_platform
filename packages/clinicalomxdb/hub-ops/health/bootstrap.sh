#!/usr/bin/env bash
# Install and start the operations health endpoint as a host systemd unit.
#
# Idempotent and re-runnable. It renders the versioned .service TEMPLATE (substituting the
# repo path, run user and its HOME) into /etc/systemd/system, so the unit is correct wherever
# the repo is checked out. Mirrors hub-pipelines/prefect/workers/hpc/deploy-service.sh.
#
# There is no venv and no pip step: health_server.py is standard library only, deliberately,
# so this cannot fail because PyPI is unreachable. See the module docstring.
#
# Run from anywhere; it re-execs itself with sudo when needed.
#
# Overridable environment (written to the unit as Environment= lines when set):
#   HEALTH_SOCKET_PATH           unix socket to serve on (default /run/ops-health/health.sock);
#                                set to the empty string to serve on TCP instead
#   HEALTH_BIND_PORT             TCP port, used only when HEALTH_SOCKET_PATH is empty
#   HEALTH_HPC_USERS           comma-separated HPC users to report sockets for
#   HEALTH_HPC_ALIAS_TEMPLATE  ssh alias, "{user}" substituted (Phase 3: hpc-{user})
#   HEALTH_PREFECT_API           Prefect API base (default http://localhost:4200/api)
set -euo pipefail

# The script's own location, with symlinks LEFT ALONE. readlink -f, which this used and which
# pg-backup.sh still uses, names the physical /opt/<root>/releases/<tag> path; a unit rendered
# from that path is abandoned by the next `ln -sfn releases/<tag> current`, which is why every
# tracked unit in infra/systemd/ names the current/ path instead. A logical path keeps whatever
# the caller used, so `make ops-health` run from current/ renders current/.
SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SELF="$SELF_DIR/$(basename -- "${BASH_SOURCE[0]}")"

case "$SELF" in
    */releases/platform-v*) echo "WARNING: rendering from a release tree directly, so the unit" \
        "below cannot survive the next current/ flip. Re-run from the current/ path." >&2 ;;
esac

OVERRIDABLE=(
    HEALTH_SOCKET_PATH
    HEALTH_BIND_PORT
    HEALTH_HPC_USERS
    HEALTH_HPC_ALIAS_TEMPLATE
    HEALTH_PREFECT_API
)

# Forward only the variables that are actually SET, rather than passing every name with an
# empty fallback. The distinction matters for HEALTH_SOCKET_PATH, where set-but-empty is
# meaningful (it selects the TCP fallback), so collapsing unset into empty would silently
# switch the default transport.
if [ "$(id -u)" -ne 0 ]; then
    FORWARD=()
    for var in "${OVERRIDABLE[@]}"; do
        [ -n "${!var+set}" ] && FORWARD+=("$var=${!var}")
    done
    exec sudo "${FORWARD[@]}" bash "$SELF" "$@"
fi

HEALTH_DIR="$(dirname "$SELF")"
# The connections check names each user's socket for the login host, hpc.login_host in site.yaml.
SITE_ENV="$(cd "$HEALTH_DIR/../../../.." && pwd)/site.env"
[ -f "$SITE_ENV" ] || { echo "ERROR: $SITE_ENV missing; render it: uv run scripts/site/render.py" >&2; exit 1; }
set -a; . "$SITE_ENV"; set +a
UNIT_TEMPLATE="$HEALTH_DIR/ops-health.service"
UNIT_NAME="ops-health.service"
# The deployed destination is the default. The override exists so
# tests/explorer/test_ops_bootstrap_paths.py can run this script whole as an ordinary user.
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
UNIT_DST="$SYSTEMD_DIR/$UNIT_NAME"
DROPIN_DIR="$SYSTEMD_DIR/$UNIT_NAME.d"

# The endpoint must run as the account that owns the HPC ControlMaster sockets and the ssh
# config, which is the account that owns the checkout (ubuntu on the VM). Derive it rather
# than hardcoding, so this is correct on a dev box too.
RUN_USER="$(stat -c '%U' "$HEALTH_DIR")"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"

echo "[health] unit template: $UNIT_TEMPLATE"
echo "[health] health dir   : $HEALTH_DIR"
echo "[health] run user     : $RUN_USER (HOME=$RUN_HOME)"

[ -f "$UNIT_TEMPLATE" ] || { echo "ERROR: unit template not found: $UNIT_TEMPLATE" >&2; exit 1; }
[ -f "$HEALTH_DIR/health_server.py" ] || { echo "ERROR: health_server.py not found" >&2; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: python3 not on PATH" >&2; exit 1; }

# Fail early and loudly rather than at first request: the script is stdlib-only, so if it
# does not import here it will not import under systemd either.
python3 -c "import ast,sys; ast.parse(open('$HEALTH_DIR/health_server.py').read())" \
    || { echo "ERROR: health_server.py does not parse" >&2; exit 1; }

# Render the template. rm first, matching deploy-service.sh: an earlier version may have
# symlinked the repo file here, and writing through a surviving symlink would clobber it.
echo "[health] rendering $UNIT_NAME -> $UNIT_DST"
rm -f "$UNIT_DST"
sed -e "s#@HEALTH_DIR@#$HEALTH_DIR#g" \
    -e "s#@RUN_USER@#$RUN_USER#g" \
    -e "s#@RUN_HOME@#$RUN_HOME#g" \
    -e "s#@HPC_LOGIN_HOST@#$CLINICALOMX_SITE_HPC_LOGIN_HOST#g" \
    "$UNIT_TEMPLATE" > "$UNIT_DST"

# Any overrides go in a drop-in rather than into the rendered unit, so the committed template
# stays the single description of the service and the local deviations are visible in one
# place (systemctl cat ops-health).
rm -rf "$DROPIN_DIR"
OVERRIDES=""
for var in "${OVERRIDABLE[@]}"; do
    # Set-but-empty is written through as an empty Environment= value, because for
    # HEALTH_SOCKET_PATH that is how the TCP fallback is requested.
    [ -n "${!var+set}" ] && OVERRIDES+="Environment=$var=${!var}"$'\n'
done
if [ -n "$OVERRIDES" ]; then
    echo "[health] writing overrides drop-in"
    mkdir -p "$DROPIN_DIR"
    printf '[Service]\n%s' "$OVERRIDES" > "$DROPIN_DIR/overrides.conf"
    printf '%s' "$OVERRIDES" | sed 's/^/[health]   /'
fi

systemctl daemon-reload
systemctl enable "$UNIT_NAME"
systemctl restart "$UNIT_NAME"

# Verify it is actually serving, not merely "active". A unit that starts and then fails its
# first request is the failure this check exists to catch.
#
# The transport must match what health_server.py chose: a unix socket by default, and TCP
# only when HEALTH_SOCKET_PATH is explicitly empty. Checking the wrong one reports a healthy
# service as broken.
SOCKET="${HEALTH_SOCKET_PATH-/run/ops-health/health.sock}"
if [ -n "$SOCKET" ]; then
    CURL_TARGET=(--unix-socket "$SOCKET" "http://localhost/health")
    WHERE="unix:$SOCKET"
else
    PORT="${HEALTH_BIND_PORT:-8099}"
    CURL_TARGET=("http://127.0.0.1:$PORT/health")
    WHERE="127.0.0.1:$PORT"
fi

for _ in $(seq 1 15); do
    if curl -fsS --max-time 5 "${CURL_TARGET[@]}" >/dev/null 2>&1; then
        echo "[health] $UNIT_NAME is serving on $WHERE"
        # Percent formatting, not an f-string: this program is inside bash single quotes, so
        # it cannot contain a single quote, and escaping double quotes inside an f-string
        # expression is a Python syntax error.
        curl -fsS "${CURL_TARGET[@]}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("[health] overall:", d["status"])
for name, check in d["checks"].items():
    print("[health]   %s: %s - %s" % (name, check["status"], check["summary"]))
'
        exit 0
    fi
    sleep 1
done

echo "ERROR: $UNIT_NAME did not answer on $WHERE. Recent logs:" >&2
journalctl -u "$UNIT_NAME" --no-pager -n 30 >&2
exit 1
