#!/bin/bash
# Invoked by systemd OnFailure for pg-backup.service. Logs to the journal, drops a
# sentinel file, and posts to a webhook if one is configured. It must never fail the
# boot or the failure path, so it always exits 0.
set -uo pipefail

SENTINEL=/mnt/data/artifacts/backups/postgres/LAST_BACKUP_FAILED
ALERT_ENV=/etc/pg-backup-alert.env

mkdir -p "$(dirname "$SENTINEL")" 2>/dev/null || true
date -u +"%Y-%m-%dT%H:%M:%SZ pg-backup FAILED on $(hostname)" > "$SENTINEL" 2>/dev/null || true
logger -t pg-backup "backup failed; wrote sentinel $SENTINEL"

# Optional webhook. Create /etc/pg-backup-alert.env with PG_BACKUP_WEBHOOK_URL=... to enable.
if [ -f "$ALERT_ENV" ]; then
    # shellcheck disable=SC1090
    . "$ALERT_ENV"
    if [ -n "${PG_BACKUP_WEBHOOK_URL:-}" ]; then
        curl -fsS -m 15 -X POST -H 'Content-Type: application/json' \
            -d "{\"text\":\"clinicalomxdb pg-backup FAILED on $(hostname)\"}" \
            "$PG_BACKUP_WEBHOOK_URL" || true
    fi
fi

exit 0
