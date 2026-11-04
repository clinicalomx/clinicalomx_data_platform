#!/usr/bin/env bash
# Manually roll the running build-app to the latest image CI pushed to GHCR.
#
# CI (.github/workflows/img-build-app.yml) builds + pushes
#   $CLINICALOMX_SITE_IMAGE_REGISTRY/clinicalomx-build-app:latest   (and :<sha>)
# Deploying is MANUAL by design, for the same reason deploy_review_app.sh is: a pull recreates the
# container, which drops any in-progress Explorer session (the Explorer's Reflex state lives in
# that process's memory, and that includes a plan someone has spent ten minutes assembling).
#
# The other reason it is manual is specific to this image. Most of what it serves comes from the
# `clinicalomx` wheel on the private index, not from this tree, so a rebuild can change the app
# without any commit here changing. Choose when that lands.
#
# ONE PROCESS, ONE PORT, and nothing about that changed when the Explorer moved from Streamlit to
# Reflex: the image serves a frontend bundle compiled at BUILD time, mounted into the same ASGI
# app that carries the websocket. So this script, the compose service and the /build* Caddy handle
# are all unchanged. What did change is that the `/build` prefix is compiled into the image rather
# than set by a compose variable, so rolling to a `:<sha>` rolls the prefix with it.
#
# Run it on the hub VM:
#     ./deploy_build_app.sh
# or from your machine over SSH (the VM is docker-login'd to GHCR already, via cloud-init):
#     ssh ubuntu@<vm> 'bash -s' < deploy_build_app.sh
#
# Only the build-app container is touched; postgres / prefect / mlflow / review-app keep running.
#
# Env overrides:
#     COMPOSE_DIR   dir holding the hub docker-compose.yml (default: the current release, see below)
#     SERVICE       compose service name (default: build-app)
#     TAG           image tag to deploy (default: latest; pass a :<sha> to pin/roll back)
set -euo pipefail

# WHERE THE COMPOSE FILE IS. `build-app` is declared in the RELEASE tree's hub-pipelines compose
# file, and `current` is the name every unit under infra/systemd/ uses for it. `current` rather
# than an explicit platform-v<N>: assemble-release.sh materialises releases/<tag>/ and NEVER
# touches `current`, because the flip is a cutover step, so `current` is the one path that always
# names the stack actually running. A tag here would keep deploying into a release nothing serves
# from, one cutover later. The old default was the split-repo checkout /opt/clinicalomxdb, which
# declares no build-app service at all, so running this as documented failed `no such service`.
COMPOSE_DIR="${COMPOSE_DIR:-/opt/clinicalomx_data_platform/current/packages/clinicalomxdb/hub-pipelines}"
SERVICE="${SERVICE:-build-app}"
TAG="${TAG:-latest}"

cd "$COMPOSE_DIR"

echo ">> Before:"
docker compose images "$SERVICE" || true

IMAGE="${CLINICALOMX_SITE_IMAGE_REGISTRY:?render site.env with scripts/site/render.py and export it}/clinicalomx-build-app"
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
echo ">> Done. Reach it at /build/ on the ops portal (make tunnel)."
