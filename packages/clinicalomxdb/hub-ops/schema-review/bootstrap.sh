#!/usr/bin/env bash
# Install the schema review renderer as a host systemd unit.
#
# Idempotent. Stdlib-only Python, so there is no venv and no package install: nothing here can
# fail because a package index is unreachable.
#
# Run from anywhere; re-execs itself with sudo.
#
# Overridable environment:
#   REVIEW_LAMIN_HOME  the settings directory the render reads (default RUN_HOME/.lamin)
#   REVIEW_TIMEOUT     seconds a render may take before it is killed and reported failed
#   REVIEW_STALE_AFTER seconds after which the banner calls the served page old
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

OVERRIDABLE=(REVIEW_LAMIN_HOME REVIEW_TIMEOUT REVIEW_STALE_AFTER)

if [ "$(id -u)" -ne 0 ]; then
    FORWARD=()
    for var in "${OVERRIDABLE[@]}"; do
        [ -n "${!var+set}" ] && FORWARD+=("$var=${!var}")
    done
    exec sudo "${FORWARD[@]}" bash "$SELF" "$@"
fi

REVIEW_DIR="$(dirname "$SELF")"
UNIT_TEMPLATE="$REVIEW_DIR/ops-schema-review.service"
UNIT_NAME="ops-schema-review.service"
# The deployed destination is the default. The override exists so
# tests/explorer/test_ops_bootstrap_paths.py can run this script whole as an ordinary user.
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
DROPIN_DIR="$SYSTEMD_DIR/$UNIT_NAME.d"
REPO_DIR="$(cd "$REVIEW_DIR/../.." && pwd)"

RUN_USER="$(stat -c '%U' "$REPO_DIR")"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
# The instance settings cloud-init writes and every lamindb service binds. Named explicitly rather
# than left to hub-database/.env, because a .env is created once and never gains a later key.
LAMIN_HOME="${REVIEW_LAMIN_HOME:-$RUN_HOME/.lamin}"

log() { echo "[schema-review] $*"; }

[ -f "$UNIT_TEMPLATE" ] || { echo "ERROR: unit template not found: $UNIT_TEMPLATE" >&2; exit 1; }
[ -f "$REVIEW_DIR/review_server.py" ] || { echo "ERROR: review_server.py not found" >&2; exit 1; }
python3 -c "import ast,sys; ast.parse(open('$REVIEW_DIR/review_server.py').read())" \
    || { echo "ERROR: review_server.py does not parse" >&2; exit 1; }
command -v make >/dev/null || { echo "ERROR: make not on PATH" >&2; exit 1; }
command -v docker >/dev/null || { echo "ERROR: docker not on PATH" >&2; exit 1; }
[ -f "$REPO_DIR/Makefile" ] || { echo "ERROR: no Makefile at $REPO_DIR" >&2; exit 1; }
# The render is a docker compose call made as RUN_USER, so a missing group membership would turn
# every trigger into a permission error the browser can only report as a failed render.
id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx docker \
    || { echo "ERROR: $RUN_USER is not in the docker group; the render could not run" >&2; exit 1; }

log "run user: $RUN_USER (HOME=$RUN_HOME), repo: $REPO_DIR, lamin home: $LAMIN_HOME"

log "installing $UNIT_NAME"
rm -f "$SYSTEMD_DIR/$UNIT_NAME"
sed -e "s#@RUN_USER@#$RUN_USER#g" \
    -e "s#@RUN_HOME@#$RUN_HOME#g" \
    -e "s#@REVIEW_DIR@#$REVIEW_DIR#g" \
    -e "s#@REPO_DIR@#$REPO_DIR#g" \
    -e "s#@LAMIN_HOME@#$LAMIN_HOME#g" \
    "$UNIT_TEMPLATE" > "$SYSTEMD_DIR/$UNIT_NAME"

rm -rf "$DROPIN_DIR"
OVERRIDES=""
for var in REVIEW_TIMEOUT REVIEW_STALE_AFTER; do
    [ -n "${!var+set}" ] && OVERRIDES+="Environment=$var=${!var}"$'\n'
done
if [ -n "$OVERRIDES" ]; then
    mkdir -p "$DROPIN_DIR"
    printf '[Service]\n%s' "$OVERRIDES" > "$DROPIN_DIR/overrides.conf"
    log "wrote overrides drop-in"
fi

systemctl daemon-reload
systemctl enable "$UNIT_NAME"
# restart, not `enable --now`: --now leaves a unit that is already up alone, so a re-run left the
# widget serving from whatever tree it first started in and the socket check below still passed.
# Restarting is safe here in a way it is not for the terminal and the relay: this unit holds no
# session, the served page is a file under StateDirectory that survives, and an in-flight render
# killed with it leaves the previous page exactly as it was (review_server.py rule 4).
systemctl restart "$UNIT_NAME"

for _ in $(seq 1 15); do
    if [ -S /run/ops-schema-review/review.sock ] \
       && curl -fsS --max-time 5 --unix-socket /run/ops-schema-review/review.sock \
            http://localhost/schema-review/status >/dev/null 2>&1; then
        log "$UNIT_NAME is serving on unix:/run/ops-schema-review/review.sock"
        log "reminder: run 'make ops-up' if Caddy lacks the /run/ops-schema-review mount"
        log "nothing renders on its own; press Re-render on the portal's Schema tab"
        exit 0
    fi
    sleep 1
done

echo "ERROR: $UNIT_NAME did not answer on its socket. Recent logs:" >&2
journalctl -u "$UNIT_NAME" --no-pager -n 30 >&2
exit 1
