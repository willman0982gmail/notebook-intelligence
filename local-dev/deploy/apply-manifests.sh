#!/usr/bin/env bash
# Apply (or dry-run) local-dev K8s examples (LLM-S05/S06/S11).
# Does NOT create real JAR/JKS secrets — print the create command instead.
#
# Usage:
#   ./local-dev/deploy/apply-manifests.sh              # dry-run if kubectl present
#   ./local-dev/deploy/apply-manifests.sh --apply      # real apply
#   NS=jhub ./local-dev/deploy/apply-manifests.sh --apply
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K8S="${ROOT}/local-dev/deploy/k8s"
NS="${NS:-jhub}"
DO_APPLY=0
[[ "${1:-}" == "--apply" ]] && DO_APPLY=1

echo "namespace=${NS}"
echo "manifests=${K8S}"

if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl not found — printing commands only:"
  echo "  kubectl -n ${NS} apply -f ${K8S}/quota-service.yaml"
  echo "  kubectl -n ${NS} apply -f ${K8S}/networkpolicy-llm-egress.yaml"
  echo "  # Secret (real files, never commit):"
  echo "  kubectl -n ${NS} create secret generic nbi-llm-auth \\"
  echo "    --from-file=token-tool.jar=./token-tool.jar \\"
  echo "    --from-file=keystore.jks=./keystore.jks \\"
  echo "    --from-file=truststore.jks=./aitruststore.jks \\"
  echo "    --from-literal=KEYSTORE_PASSWORD='***' \\"
  echo "    --from-literal=TRUSTSTORE_PASSWORD='***'"
  exit 0
fi

run() {
  if [[ "$DO_APPLY" -eq 1 ]]; then
    kubectl -n "$NS" "$@"
  else
    echo "+ kubectl -n ${NS} $* --dry-run=client --validate=false"
    # --validate=false: API server may be unreachable on laptops without a cluster
    if ! kubectl -n "$NS" "$@" --dry-run=client --validate=false 2>/dev/null; then
      echo "  (kubectl dry-run skipped — no reachable cluster; manifests left unvalidated)"
    fi
  fi
}

run apply -f "${K8S}/quota-service.yaml"
run apply -f "${K8S}/networkpolicy-llm-egress.yaml"
run apply -f "${K8S}/usage-export-cronjob.yaml"
echo
echo "Optional (needs Prometheus operator CRDs):"
echo "  kubectl -n ${NS} apply -f ${K8S}/servicemonitor-sidecar.example.yaml"
echo
echo "Secret material is out-of-band. Example:"
echo "  kubectl -n ${NS} create secret generic nbi-llm-auth \\"
echo "    --from-file=token-tool.jar=./token-tool.jar \\"
echo "    --from-file=keystore.jks=./keystore.jks \\"
echo "    --from-file=truststore.jks=./aitruststore.jks \\"
echo "    --from-literal=KEYSTORE_PASSWORD='***' \\"
echo "    --from-literal=TRUSTSTORE_PASSWORD='***'"
echo
echo "Then wire Hub via local-dev/hub/jupyterhub_config.snippet.py"
if [[ "$DO_APPLY" -eq 0 ]]; then
  echo "(dry-run only — re-run with --apply to mutate the cluster)"
fi
