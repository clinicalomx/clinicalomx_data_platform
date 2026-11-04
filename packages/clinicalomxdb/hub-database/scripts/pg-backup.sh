#!/bin/bash
# Daily pg_dumpall of the clinicalomxdb Postgres cluster (lamindb + mlflow + prefect
# databases) onto the data-artifacts volume. The dump is compressed, integrity
# checked, written atomically, and pruned to 7 daily plus 4 weekly copies. Run by
# the pg-backup systemd timer (see ADR 0001). Depends on the postgres container and
# the /mnt/data/artifacts mount (the unit gates on both).
set -euo pipefail

CONTAINER=postgres

# The DB hub's env is a sibling of this script's own directory, so the backup reads the
# config of the release it runs from rather than one fixed checkout. readlink -f resolves
# the release symlink the units invoke this through (current -> releases/platform-vN);
# in a release tree the .env is itself a link to config/package-env/hub-database.env.
# An explicitly set ENV_FILE still wins, so the operator can point it elsewhere.
SELF="$(readlink -f "${BASH_SOURCE[0]}")"
ENV_FILE="${ENV_FILE:-$(dirname "$(dirname "$SELF")")/.env}"

# The unit sets neither variable, so the deployed destination is the default below. The
# override exists so tests/explorer/test_pg_backup_env_path.py can run this script whole.
BACKUP_DIR="${BACKUP_DIR:-/mnt/data/artifacts/backups/postgres}"

# Superuser role for pg_dumpall. There is no fallback: a wrong role dumps nothing, so a
# missing env file has to name the path it looked for rather than guess a default.
if [ ! -f "$ENV_FILE" ]; then
    echo "pg-backup: no env file at $ENV_FILE" >&2
    exit 1
fi
POSTGRES_USER="$(grep -E '^POSTGRES_USER=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
if [ -z "$POSTGRES_USER" ]; then
    echo "pg-backup: no POSTGRES_USER in $ENV_FILE" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
stamp="$(date +%Y%m%d-%H%M%S)"
dow="$(date +%u)"                       # 1=Mon .. 7=Sun
tmp="$BACKUP_DIR/.daily-$stamp.sql.gz.tmp"
dest="$BACKUP_DIR/daily-$stamp.sql.gz"

# Dump -> gzip to a temp file, verify the gzip, then atomically publish. No `-t`:
# a TTY would translate newlines and corrupt the piped stream.
docker exec "$CONTAINER" pg_dumpall -U "$POSTGRES_USER" | gzip -c > "$tmp"
gzip -t "$tmp"
mv -f "$tmp" "$dest"

# Promote Sunday's dump to a weekly copy.
if [ "$dow" = "7" ]; then
    cp -f "$dest" "$BACKUP_DIR/weekly-$stamp.sql.gz"
fi

# Retention: keep the 7 most-recent daily and 4 most-recent weekly copies.
ls -1t "$BACKUP_DIR"/daily-*.sql.gz  2>/dev/null | tail -n +8 | xargs -r rm -f
ls -1t "$BACKUP_DIR"/weekly-*.sql.gz 2>/dev/null | tail -n +5 | xargs -r rm -f

# Clear any stale failure sentinel now that a backup has succeeded.
rm -f "$BACKUP_DIR/LAST_BACKUP_FAILED"

echo "pg-backup: wrote $dest"
