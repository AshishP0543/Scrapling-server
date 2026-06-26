#!/usr/bin/env bash
# Apply the k8s manifests to your GKE cluster.
#
# Required env vars:
#   PROJECT_ID   GCP project
#   REGION       GKE region (e.g. us-central1) or set ZONE for a zonal cluster
#   CLUSTER      GKE cluster name
# Optional:
#   NAMESPACE    k8s namespace (default: default)
#   TAG          image tag to deploy (default: latest)
#   REPO/IMAGE   override registry path (defaults match build.sh)
set -euo pipefail
cd "$(dirname "$0")/.."

: "${PROJECT_ID:?set PROJECT_ID=your-gcp-project}"
: "${CLUSTER:?set CLUSTER=your-gke-cluster}"
REGION="${REGION:-}"
ZONE="${ZONE:-}"
if [[ -z "$REGION" && -z "$ZONE" ]]; then
  echo "set REGION (regional cluster) or ZONE (zonal cluster)" >&2
  exit 2
fi

NAMESPACE="${NAMESPACE:-default}"
REPO="${REPO:-scrapling}"
IMAGE="${IMAGE:-scrapling}"
TAG="${TAG:-latest}"
AR_REGION="${REGION:-${ZONE%-*}}"  # AR uses regions, even for zonal clusters
URI="${AR_REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${IMAGE}:${TAG}"

echo "→ fetching kubeconfig for ${CLUSTER}"
if [[ -n "$REGION" ]]; then
  gcloud container clusters get-credentials "$CLUSTER" --region "$REGION" --project "$PROJECT_ID"
else
  gcloud container clusters get-credentials "$CLUSTER" --zone "$ZONE" --project "$PROJECT_ID"
fi

kubectl get ns "$NAMESPACE" >/dev/null 2>&1 || kubectl create ns "$NAMESPACE"

# Render the image path in the Deployment without sed-in-place gymnastics.
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
for f in deploy/k8s/deployment.yaml deploy/k8s/service.yaml; do
  sed \
    -e "s|REGION-docker.pkg.dev/PROJECT_ID/scrapling/scrapling:latest|${URI}|g" \
    "$f" > "$TMP/$(basename "$f")"
done

if ! kubectl -n "$NAMESPACE" get secret scrapling-secrets >/dev/null 2>&1; then
  echo
  echo "⚠ secret 'scrapling-secrets' not found in namespace '${NAMESPACE}'."
  echo "  Copy deploy/k8s/secret.example.yaml to deploy/k8s/secret.yaml,"
  echo "  fill in real values, then:"
  echo "    kubectl -n ${NAMESPACE} apply -f deploy/k8s/secret.yaml"
  exit 1
fi

echo "→ applying manifests to namespace ${NAMESPACE}"
kubectl -n "$NAMESPACE" apply -f "$TMP/service.yaml"
kubectl -n "$NAMESPACE" apply -f "$TMP/deployment.yaml"

echo "→ waiting for rollout"
kubectl -n "$NAMESPACE" rollout status deploy/scrapling --timeout=5m

echo
kubectl -n "$NAMESPACE" get pods -l app=scrapling -o wide
kubectl -n "$NAMESPACE" get svc scrapling-api scrapling-dashboard
