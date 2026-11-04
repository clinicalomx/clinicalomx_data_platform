#!/usr/bin/env bash
# Manually roll the running review-app to the latest image CI pushed to GHCR.
#
# CI (.github/workflows/img-review-app.yml at the repository root) builds + pushes
#   $CLINICALOMX_SITE_IMAGE_REGISTRY/clinicalomx-review-app:latest   (and :<sha>)
# on every push to main. Deploying is MANUAL by design: a pull recreates the container, which
# drops any reviewer's in-progress canvas edits (Streamlit session state is in-memory), so you
# choose when. Run it when the review queue is idle.
#
# Run it on the hub VM:
#     ./deploy_review_app.sh
# or from your machine over SSH (the VM is docker-login'd to GHCR already, via cloud-init):
#     ssh ubuntu@<vm> 'bash -s' < deploy_review_app.sh
#
# Only the review-app container is touched; postgres / prefect / mlflow are left running.
#
# Env overrides:
#     COMPOSE_DIR   dir holding the hub docker-compose.yml (default: the current release, see below)
#     SERVICE       compose service name (default: review-app)
#     TAG           image tag to deploy (default: latest; pass a :<sha> to pin/roll back)
set -euo pipefail

# WHERE THE COMPOSE FILE IS. `review-app` is declared in the RELEASE tree's hub-pipelines compose
# file, and `current` is the name every unit under infra/systemd/ uses for it. `current` rather
# than an explicit platform-v<N>: assemble-release.sh materialises releases/<tag>/ and NEVER
# touches `current`, because the flip is a cutover step, so `current` is the one path that always
# names the stack actually running. A tag here would keep deploying into a release nothing serves
# from, one cutover later. The old default was the split-repo checkout /opt/clinicalomxdb. It still
# declares a review-app service, so this script appeared to work while reading a compose file and
# a .env the release has moved past; deploy_build_app.sh, whose service that checkout never had,
# failed `no such service` and is how the shared default was found.
COMPOSE_DIR="${COMPOSE_DIR:-/opt/clinicalomx_data_platform/current/packages/clinicalomxdb/hub-pipelines}"
SERVICE="${SERVICE:-review-app}"
TAG="${TAG:-latest}"

cd "$COMPOSE_DIR"

echo ">> Before:"
docker compose images "$SERVICE" || true

IMAGE="${CLINICALOMX_SITE_IMAGE_REGISTRY:?render site.env with scripts/site/render.py and export it}/clinicalomx-review-app"
echo ">> Pulling $SERVICE ($TAG) ..."
if [ "$TAG" = "latest" ]; then
  docker compose pull "$SERVICE"
else
  # Roll to a specific :<sha> without editing the compose file: pull it and locally retag it
  # as :latest, which is what the compose service references.
  docker pull "${IMAGE}:${TAG}"
  docker tag "${IMAGE}:${TAG}" "${IMAGE}:latest"
  echo "   (pinned this run to ${TAG}, retagged local :latest)"
fi

echo ">> Recreating $SERVICE ..."
docker compose up -d "$SERVICE"

echo ">> After:"
docker compose images "$SERVICE"
echo ">> Recent logs:"
docker compose logs --tail 20 "$SERVICE" || true
echo ">> Done. (tunnel: ssh -N -L 8501:localhost:8501 ubuntu@<vm>)"
