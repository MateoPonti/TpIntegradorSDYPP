#!/bin/sh
# B3: deja en el repo los values con los que están instalados los componentes de plataforma, para que lo que
# corre en el cluster se pueda revisar en un PR y para poder rearmar el cluster igual.
#
#   gcloud container clusters get-credentials tp3 --zone us-central1-a
#   sh pilar3/k8s/platform/exportar-values.sh
#   git diff pilar3/k8s/platform
#
# No toca el cluster: `helm get values` y `helm status` son de solo lectura.
#
# Hay dos casos distintos y el script los trata distinto:
#   * traefik: el values del repo es el que **aplicamos** (`helm upgrade -f`), con sus comentarios. Acá solo se
#     COMPARA contra el cluster y se avisa si no coinciden; no se sobreescribe.
#   * cert-manager y kube-prometheus-stack: se instalaron con `--set` desde la terminal, así que no hay archivo
#     nuestro. Esos dos se EXPORTAN tal como están.
set -eu

DIR="$(dirname "$0")"

# Claves que pueden traer un secreto si el chart las recibió por --set (por ejemplo grafana.adminPassword):
# se reemplaza el valor por una marca. gitleaks corre en el CI igual, pero mejor que no salgan de acá.
SECRETOS='adminPassword|password|Password|token|Token|key|Key|secret|Secret|cookie'

sin_secretos() {
  sed -E "s/^([[:space:]]*[A-Za-z0-9_-]*($SECRETOS)[A-Za-z0-9_-]*:).*/\1 \"<en un Secret, no en el repo>\"/"
}

instalado() {
  helm status "$1" -n "$2" >/dev/null 2>&1
}

version_chart() {
  helm list -n "$2" -f "^${1}\$" -o json | tr ',' '\n' |
    sed -n 's/.*"chart":"\([^"]*\)".*/\1/p' | head -1
}

exportar() {
  release="$1"; namespace="$2"; chart="$3"; archivo="$DIR/$4"
  if ! instalado "$release" "$namespace"; then
    echo "AVISO: no existe el release $release en $namespace; dejo $4 como está" >&2
    return 0
  fi
  {
    echo "# Values con los que está instalado \`$release\` en el cluster ($chart)."
    echo "# GENERADO por pilar3/k8s/platform/exportar-values.sh — no editar a mano: si hay que cambiar algo,"
    echo "# se cambia con helm upgrade y se vuelve a exportar."
    echo "# Chart instalado: $(version_chart "$release" "$namespace")"
    echo "# Exportado: $(date '+%Y-%m-%d %H:%M')"
    echo "#"
    echo "#   helm upgrade --install $release $chart -n $namespace --create-namespace \\"
    echo "#     -f pilar3/k8s/platform/$4"
    helm get values "$release" -n "$namespace" | sin_secretos
  } > "$archivo"
  echo "escrito $archivo"
}

comparar() {
  release="$1"; namespace="$2"; archivo="$DIR/$3"
  if ! instalado "$release" "$namespace"; then
    echo "AVISO: no existe el release $release en $namespace; no puedo comparar $3" >&2
    return 0
  fi
  tmp=$(mktemp)
  helm get values "$release" -n "$namespace" | sin_secretos | grep -v '^USER-SUPPLIED VALUES:' > "$tmp"
  # Se comparan solo las líneas con contenido: el archivo del repo tiene comentarios y el cluster no.
  # Con archivos temporales y no con <(...), que es de bash y este script es sh.
  a=$(mktemp); b=$(mktemp)
  grep -vE '^[[:space:]]*(#|$)' "$archivo" | sort > "$a"
  grep -vE '^[[:space:]]*(#|$)' "$tmp" | sort > "$b"
  if diff -u "$a" "$b"; then
    echo "$3 coincide con lo que está instalado"
  else
    echo "ATENCION: $3 NO coincide con lo instalado (arriba, '-' repo y '+' cluster)." >&2
    echo "Alguien hizo un helm upgrade sin commitear, o el archivo cambió y no se aplicó." >&2
  fi
  rm -f "$tmp" "$a" "$b"
}

comparar traefik traefik traefik-values.yaml
exportar cert-manager           cert-manager jetstack/cert-manager                        cert-manager-values.yaml
exportar kube-prometheus-stack  monitoring   prometheus-community/kube-prometheus-stack   observability-values.yaml

echo
echo "Listo. Revisar con: git diff pilar3/k8s/platform"
