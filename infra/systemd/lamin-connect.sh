#!/bin/bash
# Monorepo-release render of the cloud-init-authored /usr/local/bin/lamin-connect.sh;
# provenance in infra/systemd/README.md. Populates /home/ubuntu/.lamin so every container
# can bind-mount it (mirrors how Apptainer bind-mounts $HOME on the HPC). Invoked by
# clinicalomxdb.service between hub-database (Postgres up) and hub-pipelines (readers).
#
# Uses clinicalomxdb's OWN python-lamindb image to run the canonical connect script -
# populating ~/.lamin is clinicalomxdb's responsibility, so this must NOT depend on the
# pipeline's review image. The -v and -e repeat what the .env already says; they are kept
# because this is the one call that has to correct the settings whatever the file holds,
# and a -v at the same target as the service bind wins, so the outcome is identical.
set -euo pipefail
mkdir -p /home/ubuntu/.lamin
cd /opt/clinicalomx_data_platform/current/packages/clinicalomxdb
# LAMIN_INSTANCE, from site.yaml through site.env, read here so a hand run needs nothing exported.
set -a; . /opt/clinicalomx_data_platform/current/site.env; set +a
docker compose -f hub-database/docker-compose.yml --env-file hub-database/.env \
  run --rm -v /home/ubuntu/.lamin:/root/.lamin -e LAMIN_DB_HOST_OVERRIDE=postgres -e LAMIN_INSTANCE \
  python-lamindb bash scripts/lamin_connect.sh
chown -R ubuntu:ubuntu /home/ubuntu/.lamin
