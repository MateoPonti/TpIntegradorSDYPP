#!/usr/bin/env bash
# Pruebas de carga completas del Pilar 3 (las del 20/11/2026, en resultados/2026-11-20-completa).
#
#   bash pilar3/loadtest/completa/correr.sh <fase...>
#   fases: cpu  gpu  mismo_prefijo  entrada
#
# bench.py corre en un pod "bench" de GKE (mide el sistema, no la conexión de la PC; no mina: solo manda tx), la GPU
# en esta PC y los mineros CPU en el k3d local (KEDA). Crear el pod antes:
#   kubectl -n tpi run bench --image=python:3.12-slim --restart=Never --command -- sleep 86400
# y borrarlo al terminar: kubectl -n tpi delete pod bench
#
# Ojo en Windows (Git Bash): MSYS_NO_PATHCONV=1 hace falta para que kubectl no convierta "/tmp/...", pero rompe las
# rutas que reciben Python o kubectl cp. Por eso va solo en kc() y el resto usa rutas relativas o cygpath -m.
# (El 20/11 la primera tanda lo tenía exportado para todo y la GPU nunca arrancó: lo medido fue CPU.)
set -uo pipefail
REPO=$(cd "$(dirname "$0")/../../.." && pwd)
HERE=$REPO/pilar3/loadtest/completa
OUT=${OUT:-$REPO/pilar3/loadtest/resultados/$(date +%F)-completa}
mkdir -p "$OUT"
CTX=${GKE_CONTEXT:-gke_tp3distribuidos_us-central1-a_tp3}
# A2 (27/11): por el dominio, que es el único camino de afuera. Si Cloudflare corta los bulks grandes (524 o
# 413), se corre con IN= desde un pod del cluster; ver pilar3/README.md.
API=${API:-https://sdmer.dpdns.org}
GATEWAY=${GATEWAY:-https://gateway.sdmer.dpdns.org}
IN=http://coordinator.tpi.svc.cluster.local
GPU_PID=""

kc() { MSYS_NO_PATHCONV=1 kubectl --context "$CTX" -n tpi "$@"; }
win() { command -v cygpath > /dev/null && cygpath -m "$1" || echo "$1"; }
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$OUT/log.txt"; }
status() { curl -s "$API/status" | python -c "import json,sys;s=json.load(sys.stdin);print($1)" 2>/dev/null; }

gpu_on() {
  [ -n "$GPU_PID" ] && return
  export $(grep '^MINER_TOKEN=' "$REPO/pilar3/k8s/gke.env" | xargs)
  GATEWAY_URL=$GATEWAY MINER_ID=gpu-pc-mateo MINER_ACCOUNT=mateo MINER_KIND=gpu MINER_CHUNK=1000000000 \
    LOG_DIR="$(win "$OUT/logs")" python "$(win "$REPO/pilar2/services/miners/agent.py")" -- \
    "$(win "$REPO/pilar1/build/miner_gpu.exe")" > "$OUT/gpu_agent.txt" 2>&1 &
  GPU_PID=$!
  for _ in $(seq 1 30); do
    sleep 3
    [ "$(status "s['miners']['gpu']")" = "1" ] && { log "GPU conectada (pid $GPU_PID)"; return; }
  done
  log "ERROR: la GPU no se conectó (ver $OUT/gpu_agent.txt)"; exit 1
}

gpu_off() {
  [ -z "$GPU_PID" ] && return
  kill "$GPU_PID" 2>/dev/null; wait "$GPU_PID" 2>/dev/null
  taskkill //F //IM miner_gpu.exe > /dev/null 2>&1 || pkill -f miner_gpu 2>/dev/null
  GPU_PID=""
  log "GPU apagada"
}

config() {  # config <modo> <prefijo> <fragmento>: ConfigMap + reinicio de coordinator y pool
  kc patch configmap tpi-config --type merge \
    -p "{\"data\":{\"DIFFICULTY_MODE\":\"$1\",\"DIFFICULTY_PREFIX\":\"$2\",\"POOL_FRAGMENT_PERCENT\":\"$3\"}}" > /dev/null
  kc rollout restart deployment/coordinator deployment/pool > /dev/null
  kc rollout status deployment/coordinator --timeout=300s > /dev/null
  kc rollout status deployment/pool --timeout=300s > /dev/null
  sleep 15  # el pool publica el plan nuevo (cada 5 s)
  log "config modo=$1 prefijo=$2 fragmento=$3"
}

bench() {  # bench <csv> <bulks> <repeat> <dificultad> <fragmento> <gpu> <cpu>
  log "bench $1 bulks=$2 repeat=$3 dif=$4 frag=$5 gpu=$6 cpu=$7"
  kc exec bench -- python /tmp/bench.py --base-url $IN --bulks "$2" --repeat "$3" --difficulty "$4" --fragment "$5" \
    --gpu-miners "$6" --cpu-miners "$7" --out "/tmp/$1" 2>&1 | grep "bulk=" | tee -a "$OUT/log.txt"
  (cd "$OUT" && kc cp "bench:/tmp/$1" "./$1" > /dev/null 2>&1)
}

cpu_ready() {  # sin GPU: espera los 8 pods de KEDA (manda una tx para que haya trabajo)
  for _ in $(seq 1 60); do
    [ "$(kubectl --context k3d-tpi-mineros -n tpi-miners get deploy miner-cpu -o jsonpath='{.status.readyReplicas}')" \
      = "8" ] && return
    curl -s -o /dev/null -X POST "$API/transactions" -H 'Content-Type: application/json' \
      -d '{"sender":"user-1","receiver":"user-2","amount":0.01}'
    sleep 10
  done
  log "ojo: no llegaron los 8 mineros CPU"
}

prefix() { printf '0%.0s' $(seq 1 "$1"); }

fase_cpu() {
  gpu_off; config auto 00000 10; cpu_ready
  bench bulks_cpu.csv 1,10,100,1000,10000 3 auto 10 0 8
  bench bulks_cpu.csv 100000 1 auto 10 0 8
  for n in 1 2 3 4 5 6 7 8; do config fixed "$(prefix $n)" 10; cpu_ready; bench prefijos_cpu.csv 100 3 "$(prefix $n)" 10 0 8; done
  for f in 1 5 10 25 50; do config fixed 0000000 "$f"; cpu_ready; bench fragmentos_cpu.csv 100 3 0000000 "$f" 0 8; done
}

fase_gpu() {
  gpu_on; config auto 00000 10
  bench bulks_gpu.csv 1,10,100,1000,10000 3 auto 10 1 0
  bench bulks_gpu.csv 100000 1 auto 10 1 0
  for n in 1 2 3 4 5 6 7 8; do config fixed "$(prefix $n)" 10; bench prefijos_gpu.csv 100 3 "$(prefix $n)" 10 1 0; done
  for f in 1 5 10 25 50; do config fixed 00000000 "$f"; bench fragmentos_gpu.csv 100 3 00000000 "$f" 1 0; done
}

fase_mismo_prefijo() {  # mismos bulks con el mismo prefijo fijo en GPU y en CPU (comparación justa)
  gpu_on; config fixed 0000000 10
  bench mismo_prefijo_gpu.csv 1,10,100,1000,10000 3 0000000 10 1 0
  gpu_off; config fixed 0000000 10; cpu_ready
  bench mismo_prefijo_cpu.csv 1,10,100,1000,10000 3 0000000 10 0 8
}

fase_entrada() {  # entrada y salida de la GPU con 5 tx/s todo el tiempo (prefijo automático)
  gpu_off; config auto 00000 10; cpu_ready
  (cd "$HERE" && kc cp ./stream.py bench:/tmp/stream.py > /dev/null)
  kc exec bench -- python /tmp/stream.py $IN 5 420 > "$OUT/entrada_stream.txt" 2>&1 &
  local stream=$!
  python "$(win "$HERE/monitor.py")" "$API" "$(win "$OUT/entrada_salida.csv")" 450 &
  local mon=$!
  log "entrada/salida: 0 s solo CPU"
  sleep 120; gpu_on; log "entrada/salida: entra la GPU"
  sleep 150; gpu_off; log "entrada/salida: sale la GPU"
  wait $stream; wait $mon
  log "entrada/salida: fin"
}

trap 'gpu_off' EXIT
(cd "$REPO/pilar3/loadtest" && kc cp ./bench.py bench:/tmp/bench.py > /dev/null)
for fase in "$@"; do
  log "=== fase $fase ==="
  "fase_$fase"
done
gpu_off
config auto 00000 10
log "=== listo: ConfigMap de vuelta en auto 00000 10 ==="
