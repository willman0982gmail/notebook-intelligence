#!/usr/bin/env bash
# Build local spike images (LLM-S02 / go-live §2). Requires Docker.
# Usage:
#   ./local-dev/deploy/build-images.sh
#   TAG=dev ./local-dev/deploy/build-images.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TAG="${TAG:-local}"
cd "$ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker not found" >&2
  exit 1
fi

echo "==> building nbi-quota:${TAG}"
docker build -f local-dev/deploy/Dockerfile.quota -t "nbi-quota:${TAG}" .

echo "==> building nbi-singleuser:${TAG}"
docker build -f local-dev/deploy/Dockerfile.singleuser -t "nbi-singleuser:${TAG}" .

echo "OK:"
docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}' | grep -E 'REPOSITORY|nbi-quota|nbi-singleuser' || true
echo
echo "Next (when cluster is up):"
echo "  ./local-dev/deploy/apply-manifests.sh --apply"
echo "  # point Hub chart at nbi-singleuser:${TAG} and nbi-quota:${TAG}"
