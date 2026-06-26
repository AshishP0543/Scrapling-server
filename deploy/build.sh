#!/usr/bin/env bash
# Build the Scrapling image and push it to Google Artifact Registry.
#
# Required env vars:
#   PROJECT_ID   GCP project (e.g. my-prod-123)
#   REGION       Artifact Registry region (e.g. us-central1, asia-south1)
# Optional:
#   REPO         AR repository name           (default: scrapling)
#   IMAGE        Image name                   (default: scrapling)
#   TAG          Tag to push                  (default: git sha or "latest")
#   PLATFORM     buildx target                (default: linux/amd64)
set -euo pipefail
cd "$(dirname "$0")/.."

: "${PROJECT_ID:?set PROJECT_ID=your-gcp-project}"
: "${REGION:?set REGION=your-region (e.g. us-central1)}"
REPO="${REPO:-scrapling}"
IMAGE="${IMAGE:-scrapling}"
TAG="${TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo latest)}"
PLATFORM="${PLATFORM:-linux/amd64}"

REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}"
URI="${REGISTRY}/${IMAGE}:${TAG}"
LATEST="${REGISTRY}/${IMAGE}:latest"

echo "→ ensuring docker is authenticated to ${REGION}-docker.pkg.dev"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

# Create the repo on first run (idempotent). Comment out if you manage AR elsewhere.
gcloud artifacts repositories describe "$REPO" --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "$REPO" \
    --repository-format=docker --location="$REGION" --project="$PROJECT_ID" \
    --description="Scrapling dashboard + API"

echo "→ building ${URI}  (platform: ${PLATFORM})"
docker buildx build \
  --platform "${PLATFORM}" \
  --file deploy/Dockerfile \
  --tag "${URI}" \
  --tag "${LATEST}" \
  --push \
  .

echo
echo "✓ pushed ${URI}"
echo "✓ pushed ${LATEST}"
