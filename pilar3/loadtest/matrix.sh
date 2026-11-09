#!/usr/bin/env bash
# Runs the full experiment matrix of section 3.3: for every difficulty and
# fragment size, patch the ConfigMap, restart coordinator and pool, and bench.
# Usage: BASE_URL=http://<coordinator-ip> [GPU=1] [CPU_MINERS=4 | K3S_KUBECONFIG=~/.kube/k3s.yaml] ./matrix.sh
# kubectl (without --kubeconfig) must point to GKE: it patches tpi-config there.
set -euo pipefail

: "${BASE_URL:?set BASE_URL to the public URL of the coordinator}"
DIFFICULTIES=${DIFFICULTIES:-"0 00 000 0000 00000 000000 0000000 00000000"}
FRAGMENTS=${FRAGMENTS:-"1 5 10 25 50"}
BULKS=${BULKS:-"1,10,100,1000,10000,100000"}
GPU=${GPU:-0}
# CPU miners run in the group k3s, not in GKE (TPI Google Cloud policy):
# pass CPU_MINERS, or K3S_KUBECONFIG to read the ready replicas from the k3s.
if [ -z "${CPU_MINERS:-}" ] && [ -n "${K3S_KUBECONFIG:-}" ]; then
  CPU_MINERS=$(kubectl --kubeconfig "$K3S_KUBECONFIG" -n tpi-miners get deploy miner-cpu \
    -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)
fi
CPU=${CPU_MINERS:-0}

for d in $DIFFICULTIES; do
  for f in $FRAGMENTS; do
    kubectl -n tpi patch configmap tpi-config --type merge \
      -p "{\"data\":{\"DIFFICULTY_PREFIX\":\"$d\",\"POOL_FRAGMENT_PERCENT\":\"$f\"}}"
    kubectl -n tpi rollout restart deployment/coordinator deployment/pool
    kubectl -n tpi rollout status deployment/coordinator --timeout=180s
    kubectl -n tpi rollout status deployment/pool --timeout=180s
    python3 bench.py --base-url "$BASE_URL" --bulks "$BULKS" \
      --difficulty "$d" --fragment "$f" --gpu-miners "$GPU" --cpu-miners "${CPU:-0}"
  done
done
