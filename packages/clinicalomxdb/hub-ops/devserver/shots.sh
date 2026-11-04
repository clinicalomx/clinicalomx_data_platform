#!/usr/bin/env bash
# Screenshot every portal state, so the agent can see its own output instead of guessing.
#
# No browser automation library. Because serve.py makes each state a plain URL, headless Chrome's
# own --screenshot is enough, which keeps this to one small image instead of a Playwright install.
# --virtual-time-budget is the load-bearing flag: the page fetches /health and renders from it, so
# without it the capture lands on an empty page.
#
# Requires the dev server to be running (make ui-dev).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/shots"
PORT="${UI_DEV_PORT:-8090}"
BASE="http://127.0.0.1:$PORT"
IMAGE="${UI_SHOTS_IMAGE:-zenika/alpine-chrome:latest}"
WIDTH="${UI_SHOTS_WIDTH:-1280}"
HEIGHT="${UI_SHOTS_HEIGHT:-900}"
BUDGET="${UI_SHOTS_BUDGET:-4000}"

curl -fsS --max-time 5 "$BASE/__dev/states" >/dev/null 2>&1 || {
    echo "ERROR: no dev server on $BASE. Run 'make ui-dev' first." >&2
    exit 1
}

mkdir -p "$OUT"
rm -f "$OUT"/*.png

# Health states: the tab that shows them differs, so each entry carries its own hash and params.
SHOTS=(
    "status-healthy|/?state=healthy#status"
    "status-backup-failed|/?state=backup-failed#status"
    "status-prefect-down|/?state=prefect-down#status"
    "status-old-endpoint|/?state=old-endpoint#status"
    "status-flapping|/?state=flapping-worker#status"
    "hpc-healthy|/?state=healthy#hpc"
    "hpc-dead-socket|/?state=dead-socket#hpc"
    "hpc-no-socket|/?state=no-socket#hpc"
    "hpc-unprovisioned|/?state=unprovisioned-user#hpc"
    "hpc-many-users|/?state=many-users#hpc"
    "hpc-no-connections|/?state=no-connections#hpc"
    "hpc-endpoint-offline|/?state=offline#hpc"
    "login-idle|/?state=dead-socket&open=login#hpc"
    "login-password|/?state=dead-socket&login=password-prompt&open=login-prompt#hpc"
    "login-duo-menu|/?state=dead-socket&login=duo-menu&open=login-prompt#hpc"
    "login-duo-passcode|/?state=dead-socket&login=duo-passcode&open=login-prompt#hpc"
    "login-failed|/?state=dead-socket&login=failed&open=login-prompt#hpc"
)

echo "[shots] $IMAGE at ${WIDTH}x${HEIGHT}, budget ${BUDGET}ms"
for entry in "${SHOTS[@]}"; do
    name="${entry%%|*}"
    path="${entry#*|}"
    # shot=1 disables the dev client's live-reload stream. Without it the request never
    # completes, the page never finishes loading, and the capture hangs indefinitely.
    query="${path%%#*}"
    frag=""
    case "$path" in *"#"*) frag="#${path#*#}" ;; esac
    docker run --rm --network host \
        -v "$OUT:/out" \
        --entrypoint chromium-browser \
        "$IMAGE" \
        --headless --no-sandbox --disable-gpu --disable-dev-shm-usage --hide-scrollbars \
        --window-size="$WIDTH,$HEIGHT" \
        --virtual-time-budget="$BUDGET" \
        --screenshot="/out/$name.png" \
        "$BASE$query&shot=1$frag" >/dev/null 2>&1 || echo "[shots] WARNING: $name failed"
    printf '[shots]   %-24s %s\n' "$name" "$path"
done

# Chrome writes as root inside the container; hand them back so the agent and editor can read them.
if [ -n "${SUDO_USER:-}" ] || [ "$(id -u)" -eq 0 ]; then
    chown -R "$(id -u):$(id -g)" "$OUT" 2>/dev/null || true
fi

echo "[shots] wrote $(ls -1 "$OUT"/*.png 2>/dev/null | wc -l) images to $OUT"
