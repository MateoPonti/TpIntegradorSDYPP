#!/usr/bin/env bash
# Task 18: ejecutar el 03/12 sobre el sistema congelado. Sin --run solo muestra el plan.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
GKE_CONTEXT=${GKE_CONTEXT:-gke_tp3distribuidos_us-central1-a_tp3}
K3S_CONTEXT=${K3S_CONTEXT:-}
BASE_URL=${BASE_URL:-}
PREFIX=${PREFIX:-0000000}
FRAGMENT=${FRAGMENT:-10}
BULK=${BULK:-1000}
OUT=${OUT:-$HERE/resultados/$(date +%F)-escalado}

if [[ ${1:-} != --run ]]; then
  cat <<EOF
Plan Task 18 (no se ejecutó ningún cambio):
  GKE: $GKE_CONTEXT, namespace tpi; dificultad fija $PREFIX y fragmentación $FRAGMENT %.
  k3s: ${K3S_CONTEXT:-sin definir}, namespace tpi-miners; 4 y 8 mineros CPU, KEDA en pausa.
  Carga: $BULK tx, 3 corridas por punto (matrix.sh y bench.py originales).
  Salida: $OUT. Se comparan ingesta y confirmación; al salir se restaura configuración previa.
Uso: BASE_URL=https://... K3S_CONTEXT=... bash pilar3/loadtest/escalado.sh --run
Requiere mineros GPU desconectados, ausencia de tráfico ajeno y relojes sincronizados.
EOF
  exit 0
fi
: "${BASE_URL:?definir BASE_URL}"
: "${K3S_CONTEXT:?definir K3S_CONTEXT explícitamente}"
[[ $K3S_CONTEXT != "$GKE_CONTEXT" ]] || { echo 'Los contextos GKE y k3s deben ser distintos.' >&2; exit 2; }
[[ $PREFIX =~ ^0{1,8}$ && $BULK =~ ^[1-9][0-9]*$ ]] || { echo 'Prefijo o bulk inválido.' >&2; exit 2; }
TPI_PYTHON=$(command -v "${PYTHON_BIN:-python3}")
TPI_KUBECTL=$(command -v kubectl)
export TPI_PYTHON TPI_KUBECTL HERE GKE_CONTEXT BASE_URL
kc() { "$TPI_KUBECTL" --context "$GKE_CONTEXT" -n tpi "$@"; }
km() { "$TPI_KUBECTL" --context "$K3S_CONTEXT" -n tpi-miners "$@"; }

# Verificación del destino antes de escalar: solo un servidor k3s, nunca nodos GCE.
km version -o json | "$TPI_PYTHON" -c 'import json,sys; v=json.load(sys.stdin)["serverVersion"]["gitVersion"]; assert "k3s" in v, "el contexto de mineros no es k3s"'
km get nodes -o json | "$TPI_PYTHON" -c 'import json,sys; n=json.load(sys.stdin)["items"]; assert n and all(not x.get("spec",{}).get("providerID","").startswith("gce:") for x in n), "no minar en GCP"'
[[ ! -e $OUT ]] || { echo "La salida ya existe: $OUT. Elegir otra para no mezclar corridas." >&2; exit 2; }
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
tmp=$(mktemp -d)
config_changed=0; miners_changed=0
kc get configmap tpi-config -o json | "$TPI_PYTHON" -c 'import json,sys; d=json.load(sys.stdin)["data"]; print(json.dumps({"data":{k:d.get(k) for k in ("DIFFICULTY_MODE","DIFFICULTY_PREFIX","POOL_FRAGMENT_PERCENT")}}))' > "$tmp/restore-config.json"
km get scaledobject miner-cpu -o json | "$TPI_PYTHON" -c 'import json,sys; d=json.load(sys.stdin); k="autoscaling.keda.sh/paused-replicas"; print(json.dumps({"metadata":{"annotations":{k:d.get("metadata",{}).get("annotations",{}).get(k)}}}))' > "$tmp/restore-keda.json"
old_replicas=$(km get deployment miner-cpu -o jsonpath='{.spec.replicas}')
cleanup() {
  rc=$?; trap - EXIT; set +e
  if [[ $config_changed == 1 ]]; then
    echo 'Restaurando dificultad y fragmentación originales en tpi.'
    kc patch configmap tpi-config --type merge --patch-file "$tmp/restore-config.json" || rc=1
    kc rollout restart deployment/coordinator deployment/pool || rc=1
  fi
  if [[ $miners_changed == 1 ]]; then
    echo 'Restaurando réplicas y pausa previa de KEDA en k3s.'
    km scale deployment miner-cpu --replicas="$old_replicas" || rc=1
    km patch scaledobject miner-cpu --type merge --patch-file "$tmp/restore-keda.json" || rc=1
  fi
  rm -rf "$tmp"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# matrix.sh usa kubectl/python3 sin contexto: estas funciones afectan solo este proceso y sus hijos.
# No cambian el contexto global del usuario ni los archivos de la cátedra.
kubectl() { "$TPI_KUBECTL" --context "$GKE_CONTEXT" "$@"; }
python3() { "$TPI_PYTHON" "$HERE/medir_corrida.py" "$@"; }
export -f kubectl python3

echo 'Fijando dificultad en tpi; matrix.sh reiniciará coordinator y pool para cada punto.'
config_changed=1
kc patch configmap tpi-config --type merge -p '{"data":{"DIFFICULTY_MODE":"fixed"}}'
for cpu in 4 8; do
  echo "Pausando KEDA y esperando $cpu mineros CPU en $K3S_CONTEXT."
  miners_changed=1
  km annotate scaledobject miner-cpu "autoscaling.keda.sh/paused-replicas=$cpu" --overwrite
  km scale deployment miner-cpu --replicas="$cpu"
  km rollout status deployment/miner-cpu --timeout=180s
  mkdir "$OUT/cpu-$cpu"
  cp "$HERE/bench.py" "$OUT/cpu-$cpu/bench.py"
  (cd "$OUT/cpu-$cpu" && DIFFICULTIES="$PREFIX" FRAGMENTS="$FRAGMENT" BULKS="$BULK" CPU_MINERS="$cpu" GPU=0 bash "$HERE/matrix.sh")
done
"$TPI_PYTHON" "$HERE/resumen_escalado.py" "$OUT"
echo "Resultados guardados en $OUT."
