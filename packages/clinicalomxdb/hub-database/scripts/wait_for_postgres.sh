#!/usr/bin/env bash
# Polls until PostgreSQL is accepting connections, then exits 0.
# Exits 1 if postgres is not ready after MAX_RETRIES attempts.
# Called by: make up

set -euo pipefail

MAX_RETRIES=30
SLEEP_SECS=2
ENV_FILE="$(dirname "$0")/../.env"

if [ -f "$ENV_FILE" ]; then
    set -o allexport
    source "$ENV_FILE"
    set +o allexport
fi

: "${POSTGRES_USER:=root}"
: "${POSTGRES_DB:=lamindb}"

for i in $(seq 1 "$MAX_RETRIES"); do
    if docker compose -f "$(dirname "$0")/../docker-compose.yml" \
        exec -T database pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" -q 2>/dev/null; then
        sleep "$SLEEP_SECS"
        echo "Postgres is ready."
        exit 0
    fi
    echo "  Postgres not ready ($i/$MAX_RETRIES), retrying in ${SLEEP_SECS}s..."
    sleep "$SLEEP_SECS"
done

echo "ERROR: PostgreSQL did not become ready after $((MAX_RETRIES * SLEEP_SECS))s."
exit 1
