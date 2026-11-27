#!/bin/sh
# A3-h: cuánto tarda el k3s en quedarse con 0 mineros CPU cuando no hay más trabajo.
#
#   export KUBECONFIG=~/.kube/k3s.yaml          # el del k3s local, no el de GKE
#   sh medir-escalado-a-cero.sh https://<dominio>
#
# Manda una transacción del génesis (A1: user-* solo transfiere a otra user-*), espera a que el
# bloque se selle y cronometra desde el sellado —el momento en que se entrega la última tarea—
# hasta que el Deployment miner-cpu queda en 0 réplicas.
#
# El contrato dice 60 s: los decide el pool (CPU_IDLE_SECONDS). KEDA solo agrega su
# pollingInterval de 5 s, así que el criterio es <= 70 s.
set -eu

URL="${1:?uso: sh medir-escalado-a-cero.sh https://<dominio-del-coordinator>}"
URL="${URL%/}"
NS="${NS:-tpi-miners}"
DEPLOY="${DEPLOY:-miner-cpu}"
LIMITE="${LIMITE:-70}"        # criterio del contrato, en segundos
ESPERA_MAX="${ESPERA_MAX:-300}"  # corta si algo quedó colgado

altura() {
  # chain_height de GET /status, sin depender de jq.
  curl -fsS "$URL/status" | tr ',{}' '\n\n\n' |
    sed -n 's/.*"chain_height"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p' | head -1
}

replicas() {
  kubectl -n "$NS" get deploy "$DEPLOY" -o jsonpath='{.status.replicas}' 2>/dev/null || true
}

esperar() {  # esperar <descripcion> <comando que devuelve 0 cuando ya está>
  _desc="$1"; shift
  _t0=$(date +%s)
  while ! "$@"; do
    if [ $(( $(date +%s) - _t0 )) -ge "$ESPERA_MAX" ]; then
      echo "ABORTADO: $_desc no pasó en ${ESPERA_MAX}s" >&2
      exit 1
    fi
    sleep 1
  done
}

hay_mineros() { n=$(replicas); [ -n "$n" ] && [ "$n" -gt 0 ] 2>/dev/null; }
sin_mineros() { n=$(replicas); [ -z "$n" ] || [ "$n" -eq 0 ] 2>/dev/null; }
subio_la_altura() { [ "$(altura)" != "$H0" ]; }

H0=$(altura)
[ -n "$H0" ] || { echo "no pude leer chain_height de $URL/status" >&2; exit 1; }
echo "altura inicial: $H0"
echo "cooldownPeriod configurado: $(kubectl -n "$NS" get scaledobject "$DEPLOY" \
  -o jsonpath='{.spec.cooldownPeriod}' 2>/dev/null || echo '?') s"

curl -fsS -X POST "$URL/transactions" -H 'Content-Type: application/json' \
  -d '{"sender":"user-1","receiver":"user-2","amount":1}' >/dev/null
echo "tx mandada; espero que KEDA levante los mineros..."
esperar "los mineros no levantaron" hay_mineros
echo "mineros arriba: $(replicas) réplicas"

esperar "el bloque no se selló" subio_la_altura
T0=$(date +%s)
echo "bloque sellado (altura $H0 -> $(altura)). Cronometrando el apagado..."

esperar "el Deployment no bajó a 0" sin_mineros
SEGUNDOS=$(( $(date +%s) - T0 ))

echo "----"
echo "fecha:             $(date '+%Y-%m-%d %H:%M:%S')"
echo "sellado -> 0 pods: ${SEGUNDOS}s (criterio: <= ${LIMITE}s)"
if [ "$SEGUNDOS" -le "$LIMITE" ]; then
  echo "RESULTADO: OK"
else
  echo "RESULTADO: SE PASA. Revisar cooldownPeriod del ScaledObject y CPU_IDLE_SECONDS del pool." >&2
  exit 1
fi
