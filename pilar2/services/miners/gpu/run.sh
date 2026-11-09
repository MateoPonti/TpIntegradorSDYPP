#!/usr/bin/env bash
# Minero GPU en Linux / Colab / cluster de la cátedra. NO es una imagen y nunca corre en GCP.
#   GATEWAY_URL=http://<ip> MINER_TOKEN=... ./pilar2/services/miners/gpu/run.sh
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
bin="$here/../../../../pilar1/build/miner_gpu"
[ -x "$bin" ] || { echo "No existe $bin: compilá el Pilar 1 (make ARCH=sm_75 en Colab T4)" >&2; exit 1; }
: "${GATEWAY_URL:?falta GATEWAY_URL}" "${MINER_TOKEN:?falta MINER_TOKEN}"
export MINER_KIND=gpu MINER_ID="${MINER_ID:-gpu-$(hostname)}" MINER_CHUNK="${MINER_CHUNK:-200000000}"
exec python3 "$here/../agent.py" -- "$bin"
