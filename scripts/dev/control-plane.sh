#!/usr/bin/env bash

set -euo pipefail
cd "$(dirname "$0")/../.."

PORTAL_PORT="${PORTAL_PORT:-8088}"
REFLEX_PORT="${REFLEX_PORT:-3000}"
REFLEX_BACKEND_PORT="${REFLEX_BACKEND_PORT:-8000}"
REFLEX_WAIT_SECONDS="${REFLEX_WAIT_SECONDS:-600}"

trap 'trap - INT TERM EXIT; kill 0 2>/dev/null || true' INT TERM EXIT

refuse_if_held() {
  if (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; then
    echo "==> Port $1 is already in use, so Reflex cannot have it." >&2
    echo "    Stop what holds it, or set $2 to a free port." >&2
    exit 1
  fi
}
refuse_if_held "${REFLEX_PORT}" REFLEX_PORT
refuse_if_held "${REFLEX_BACKEND_PORT}" REFLEX_BACKEND_PORT

echo "==> Reflex dev server (hot reload) on http://localhost:${REFLEX_PORT}"
( cd packages/clinicalomx && exec uv run --extra app --extra extract reflex run \
    --env dev --frontend-port "${REFLEX_PORT}" --backend-port "${REFLEX_BACKEND_PORT}" ) &
reflex_pid=$!

echo "==> Waiting for Reflex on port ${REFLEX_PORT} (a first start compiles the frontend)"
deadline=$(( SECONDS + REFLEX_WAIT_SECONDS ))
while true; do
  if ! kill -0 "${reflex_pid}" 2>/dev/null; then
    echo "==> Reflex exited before serving port ${REFLEX_PORT}. Its output is above." >&2
    exit 1
  fi
  if (exec 3<>"/dev/tcp/127.0.0.1/${REFLEX_PORT}") 2>/dev/null; then
    break
  fi
  if (( SECONDS >= deadline )); then
    echo "==> Reflex did not answer on port ${REFLEX_PORT} within ${REFLEX_WAIT_SECONDS}s." >&2
    echo "    Raise REFLEX_WAIT_SECONDS if the first compile is genuinely slower here." >&2
    exit 1
  fi
  sleep 1
done

echo "==> Control plane portal on http://localhost:${PORTAL_PORT}/#build"
python3 scripts/dev/portal.py \
  --port "${PORTAL_PORT}" --reflex-url "http://localhost:${REFLEX_PORT}/"
