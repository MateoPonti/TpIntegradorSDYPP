# Pilar 3 · GKE, escalado y mediciones

## Cómo quedó adaptado el starter a este repo

- Los servicios **no** están en `services/` sino en `pilar2/services/`. Se construyen desde la raíz:
  - coordinator / pool / gateway: `docker build -f pilar2/services/Dockerfile --build-arg SERVICE=<svc> .`
  - miner-cpu (k3s local, **nunca GCP**): `docker build -f pilar2/services/miners/cpu/Dockerfile .`
- Los workflows están en `.github/workflows/` de la raíz, con las rutas `pilar3/...` y `pilar2/...`.
- `k8s/apps/config.yaml` tiene además las variables del grupo (armado del bloque, reintentos, `GATEWAY_HEALTH_URL`).
- `GET /scaling` lo sirve el **gateway** leyendo `pool:scaling` (duda 11, opción A, como trae el starter).
- Los mineros GPU no tienen manifiesto: corren en la PC (`pilar2/services/miners/gpu/run.ps1`) o en Colab.
- Prometheus + Grafana: los que instaló el TP3 en `monitoring` (quedaron en el cluster), con el dashboard "TPI Blockchain" (ver abajo).
- Pendiente: adaptar `bench.py` a `POST /transactions/bulk` en una copia
  propia (el `bench.py`, `matrix.sh` y `plot.py` de la cátedra no se modifican).

En los comandos de abajo, todo lo que dice `infra/terraform`, `k8s/` o `k3s-local/` está dentro de `pilar3/`.

## Cómo está desplegado hoy (13/11/2026)

El TPI corre en el **cluster GKE del TP3** (proyecto `tp3distribuidos`, cluster `tp3`, zona `us-central1-a`),
en su propio namespace **`tpi`**. El 17/11/2026 se sacó el TP3 del cluster (namespace `tp3`, su IP
`tp3-ingress` y su dashboard de Grafana) para liberar nodos. Quedan compartidos `monitoring`
(Prometheus, Grafana, Loki, Fluent Bit), el Artifact Registry `tp3` y los node pools.

| Qué | Dónde |
|---|---|
| Frontend, API y `/status` | **`https://35-223-237-60.sslip.io`** (y `http://35.223.237.60`, IP fija `tpi-coordinator`, hoy en Traefik) |
| Portal para unirse al pool | `https://35-223-237-60.sslip.io/unirse` |
| Gateway de los mineros | **`https://gateway.35-223-237-60.sslip.io`** (por Traefik) y `http://34.63.209.225` (IP fija `tpi-gateway`, para los pods del k3s) |
| Imágenes | `us-central1-docker.pkg.dev/tp3distribuidos/tp3/tpi-{coordinator,pool,gateway}:<sha>` |
| Prometheus, Grafana y Loki | Los que instaló el TP3 (namespace `monitoring`, siguen ahí). Leen nuestros pods con `k8s/observability/` |
| Grafana | `http://136.80.214.178`, dashboard **TPI Blockchain** (usuario y clave en el secret `grafana-admin` de `monitoring`) |

Diferencias con el starter, para que entre en ese cluster:
- Los nodos tienen la etiqueta `pool=infra` / `pool=apps` y el taint `pool=infra` (no `role` / `dedicated`).
- Los pods de las apps llevan `enableServiceLinks: false`: si no, Kubernetes inyecta `REDIS_PORT=tcp://…`
  por un Service que se llamaba `redis` y rompe la configuración.
- Piden 50m de CPU (se bajó cuando el nodo `apps` lo compartíamos con el TP3; ahora hay lugar para subirlo).

**Despliegue a mano** (los pipelines 1 a 3 necesitan las variables de GCP en GitHub; ver más abajo):

```bash
K="kubectl --context gke_tp3distribuidos_us-central1-a_tp3"
R=us-central1-docker.pkg.dev/tp3distribuidos/tp3; T=$(git rev-parse --short HEAD)
for s in coordinator pool gateway; do
  docker build -f pilar2/services/Dockerfile --build-arg SERVICE=$s -t $R/tpi-$s:$T . && docker push $R/tpi-$s:$T
done
$K apply -k pilar3/k8s/base && $K apply -k pilar3/k8s/infra         # namespace, RabbitMQ y Redis
$K kustomize pilar3/k8s/apps | sed -E "s#REGISTRY/(coordinator|pool|gateway):latest#$R/tpi-\1:$T#" | $K apply -f -
$K apply -k pilar3/k8s/observability                                  # PodMonitors y alertas
```

Los secrets (`rabbitmq-auth`, `redis-auth`, `gateway-auth`) se crearon con los valores de `pilar3/k8s/gke.env`,
que está solo en la PC de quien desplegó (`.gitignore`). El `MINER_TOKEN` de ese archivo es el que usan los mineros.

**Resetear la cadena** (empezar de cero con un génesis nuevo; se hizo el 13/11 para limpiar las pruebas).
Borra todo lo del TPI: bloques, saldos, préstamos, calibraciones y cuentas de los mineros. No se puede borrar solo la plata: los saldos salen de la cadena.

```bash
set -a; . pilar3/k8s/gke.env; set +a
K="kubectl --context gke_tp3distribuidos_us-central1-a_tp3 -n tpi"
M=$($K exec redis-0 -c sentinel -- redis-cli -p 26379 -a "$REDIS_PASSWORD" --no-auth-warning      sentinel get-master-addr-by-name tpi | head -1 | cut -d. -f1)          # el principal (puede no ser redis-0)
$K exec $M -c redis -- redis-cli -a "$REDIS_PASSWORD" --no-auth-warning FLUSHALL   # las réplicas copian solas
for q in mining.tasks mining.results mining.dlq; do $K exec rabbitmq-0 -- rabbitmqctl -q purge_queue $q; done
$K rollout restart deploy/coordinator deploy/pool deploy/gateway   # el coordinator crea el génesis al arrancar
```

**RabbitMQ y Redis con réplicas** (17/11/2026, cuando se sacó el TP3 del cluster). Los 6 pods están en el
único nodo `infra`: sirve para mostrar que se cae un pod y el sistema sigue, pero si se cae la máquina se cae
todo (para eso hay que sumar nodos al pool `infra`; los manifiestos ya reparten los pods si los hay).
- **RabbitMQ:** 3 nodos (`rabbitmq-0..2`) en cluster. Se encuentran con el plugin `rabbitmq_peer_discovery_k8s`
  (en 4.x no usa la API de Kubernetes: el `-0` arranca y los otros se le unen) y el Service headless
  `rabbitmq-headless`. Las colas quorum tienen una copia en cada nodo y Raft elige el líder; la reconciliación
  de miembros las lleva a 3 copias si se declararon con menos. La cookie de Erlang va en el secret
  `rabbitmq-auth` (`erlang-cookie`, sale del hash de la clave). Los servicios entran por el Service `rabbitmq`.
- **Redis:** `redis-0..2`, cada pod con Redis y un Sentinel. Al arrancar, cada pod le pregunta a los otros
  Sentinel quién es el principal; si nadie contesta, el principal es `redis-0`. Si se cae el principal, 2 de 3
  Sentinel lo dan por caído a los 5 s y ascienden una réplica (probado: 9 s, y el `redis-0` volvió como réplica).
  Los servicios usan `REDIS_SENTINELS` (Service `redis-sentinel`). La replicación es asíncrona: en un failover
  se puede perder lo último que escribió el principal (~1 s).
- Probado el 17/11: cadena intacta (altura 15), `POST /transactions` → 202 a los pocos segundos del failover de
  Redis, y con `rabbitmq-0` caído las colas siguieron con 2 copias y volvieron a 3. `test_caida_del_redis_principal`
  en `pilar2/tests/test_sistema.py` mata el principal.
- Para migrar desde la versión de 1 réplica hubo que borrar los StatefulSets (el `serviceName` no se puede cambiar;
  los PVC quedan) y el Service `redis` viejo: `$K -n tpi delete sts rabbitmq redis; $K -n tpi delete svc redis`.

**HTTPS** (17/11/2026, opción A: gratis, dentro del cluster). Traefik (namespace `traefik`, helm chart 42.4.0)
tiene la IP fija `tpi-coordinator` y reparte por nombre: `35-223-237-60.sslip.io` → coordinator y
`gateway.35-223-237-60.sslip.io` → gateway. cert-manager (namespace `cert-manager`, v1.21.2) saca y renueva solo
el certificado de Let's Encrypt (desafío HTTP-01). `sslip.io` resuelve esos nombres a la IP, así no hace falta
comprar un dominio (Let's Encrypt no firma una IP sola). HTTP sigue abierto para `bench.py` y los mineros ya
configurados. Se eligió Traefik y no ingress-nginx porque ingress-nginx ya no se mantiene. Manifiestos e
instalación en `k8s/https/`. Ojo: si se mueve la IP, el primer desafío puede fallar por timeout; se reintenta con
`kubectl -n tpi delete certificate tpi-tls`.

**Dashboard:** `k8s/observability/dashboard-tpi.yaml` es un ConfigMap con la etiqueta `grafana_dashboard`; el
sidecar de Grafana lo carga solo. 14 paneles: altura, pendientes, prefijo, hashrate por tipo y por minero, tiempo
por bloque, tx/s, bloques rearmados, cola, vencidos, DLQ, canarios, cuarentena y tx por bloque. Solo lee el
namespace `tpi`.

**Prometheus persistente:** `k8s/observability/prometheus-persistencia.yaml` le agrega al Prometheus de `monitoring`
un disco de 10 GB (`pd-standard`) y 30 días de retención, con `helm upgrade --reuse-values` (el comando está
en el archivo).

**Otra cuenta de GCP:** `infra/terraform` crea un cluster propio con los mismos node pools, discos de 30 GB
`pd-standard` (para que la cuota de SSD no frene al autoscaler), el registry `tpi`, la identidad de GitHub
y las dos IPs fijas (`tofu output static_ips`). Las IPs van a ser otras: hay que ponerlas en
`loadBalancerIP` de `k8s/apps/coordinator.yaml` y `gateway.yaml`, y en `k3s-local/`.

## Mineros CPU en varias PCs

Cada PC con un k3s corre el mismo Deployment (`k3s-local/`) contra el mismo gateway y con el **mismo
`MINER_TOKEN`** (es un solo token para todos los mineros: lo crea quien despliega y está en el secret
`gateway-auth`). Lo único que cambia por PC es `MINER_CLUSTER` y `MINER_ACCOUNT` en `k3s-local/config.yaml`. El id de cada
minero queda `<MINER_CLUSTER>-<nombre del pod>`, y todos los pods de esa PC cobran el premio en `MINER_ACCOUNT`
(por ejemplo `mateo`; se ve su saldo en el frontend), y con ese id el gateway lleva su calibración, su hashrate
real, su cuarentena y su premio (`GET /miners` del coordinator, y la tabla de mineros del frontend).
Cada k3s tiene su propio KEDA, y todos leen la misma señal de `GET /scaling`: cada PC levanta hasta
`CPU_MINERS_MAX` pods.

Así se levantó el k3s de prueba el 13/11 (un cluster k3d propio, sin tocar los otros de la PC):

```bash
k3d cluster create tpi-mineros --agents 0 --k3s-arg "--disable=traefik@server:0"
helm --kube-context k3d-tpi-mineros install keda kedacore/keda -n keda --create-namespace --wait
docker build -f pilar2/services/miners/cpu/Dockerfile -t tpi/miner-cpu:<sha> . && k3d image import tpi/miner-cpu:<sha> -c tpi-mineros
K="kubectl --context k3d-tpi-mineros"
$K apply -f pilar3/k3s-local/namespace.yaml
$K -n tpi-miners create secret generic gateway-auth --from-literal=miner-token="$MINER_TOKEN"
$K kustomize pilar3/k3s-local | sed "s#ghcr.io/OWNER/REPO/miner-cpu:latest#tpi/miner-cpu:<sha>#" | $K apply -f -
```

Resultado: KEDA subió a 8 pods al haber trabajo sin GPU, cada uno calibró (~1,4 MH/s) y el pool subió solo al
prefijo `000000` con 12,8 MH/s entre los 8. Para apagarlo: `k3d cluster stop tpi-mineros`.

---

# Starter del Pilar 3 del TPI: GKE + OpenTofu + pool + escalabilidad

Esqueleto de referencia para la **Guía del TPI Pilar 3**. No es una solución: los servicios
(`services/coordinator`, `services/pool`, `services/gateway`, `services/miner-cpu`) son los que
ustedes construyen en el Pilar 2. Lo que sí trae resuelto es la parte aburrida y peligrosa: red,
cluster con dos node pools, identidad sin claves para los pipelines, el despliegue de los mineros
en su k3s local y las herramientas para medir.

**Política de uso de Google Cloud del TPI**: ningún minero (ni CPU ni GPU) corre dentro de GCP.
La prueba gratuita prohíbe minar criptomonedas y no permite GPUs. En GKE van el coordinador,
RabbitMQ, Redis, el pool y el gateway HTTP; los mineros corren en su k3s local (los CPU, con
escalado elástico), su PC, Colab o el cluster GPU de la cátedra, y hablan solo con el gateway.

```
infra/terraform/     provider.tf, variables.tf, main.tf, outputs.tf (estructura del TP3):
                     cluster GKE zonal, node pools infra y apps, Artifact Registry,
                     Workload Identity Federation para GitHub Actions
k8s/base             namespace tpi (GKE)
k8s/infra            RabbitMQ y Redis (con persistencia) en el pool infra, sin exponer
k8s/apps             coordinator y gateway (con HPA y LoadBalancer público) y pool, en el pool apps
k3s-local/           mineros CPU en SU k3s local: Deployment + KEDA (lee GET /scaling del gateway)
.github/workflows    ci (gitleaks + tests) y los Pipelines 1 a 4 del enunciado
loadtest/            bench.py, matrix.sh y plot.py para la sección 3.3
examples/            pool_scaler.py: decisión "sin GPU -> prefijo más corto y N mineros CPU"
                     local_autoscaler.py: alternativa a KEDA para el k3s
tests/               tests de los ejemplos y de las herramientas de medición
```

## Arranque (una sola vez, desde su máquina)

El primer `apply` no puede correr en GitHub Actions: la identidad que usan los pipelines
la crea justamente este código.

```bash
gcloud auth application-default login
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars     # completen project_id y github_repo
tofu init
tofu apply
```

Después:

1. Creen un bucket para el estado remoto, descomenten el bloque `backend "gcs"` de `provider.tf`
   y corran `tofu init -migrate-state`.
2. En GitHub, *Settings > Secrets and variables > Actions*, carguen:
   - **Variables**: `GCP_PROJECT_ID`, `GCP_REGION`, `GKE_CLUSTER` (`tpi-gke`), `GKE_ZONE`,
     `GCP_WIF_PROVIDER`, `GCP_DEPLOYER_SA`, `GCP_INFRA_SA` (salen de `tofu output`) y,
     cuando el gateway tenga IP, `GATEWAY_URL` (`http://<ip>`).
   - **Secrets**: `RABBITMQ_USER`, `RABBITMQ_PASSWORD`, `REDIS_PASSWORD`, `MINER_TOKEN`
     (el token que presentan los mineros al gateway; generen uno largo y aleatorio).
3. Creen el environment `production` con *required reviewers*: ningún `apply` corre sin que alguien del grupo lo apruebe.

No hay ni una clave JSON de service account en todo el flujo. Si en algún momento descargan una, algo hicieron mal.

## Orden de los pipelines

| Pipeline | Archivo | Qué hace |
|---|---|---|
| CI | `ci.yml` | gitleaks sobre todo el historial + tests. Si encuentra un secreto, falla. |
| 1 | `1-infra.yml` | `tofu plan` en PR, `tofu apply` en main con aprobación |
| 2 | `2-base-services.yml` | Secrets de Kubernetes (incluido el token del gateway), RabbitMQ y Redis |
| 3 | `3-apps.yml` | build + push de coordinator, pool y gateway con tag = SHA del commit, `kubectl apply -k` y `rollout status` |
| 4 | `4-miners.yml` | build + push de `miner-cpu` a ghcr.io; opcional: despliegue en el k3s con un runner self-hosted |

## Mineros CPU en el k3s local

Usen el k3s de la Parte 0 del TP3, con **su** kubeconfig (no el de GKE). Una vez:

```bash
export KUBECONFIG=~/.kube/k3s.yaml          # el del k3s local
helm repo add kedacore https://kedacore.github.io/charts
helm install keda kedacore/keda --namespace keda --create-namespace
kubectl apply -f k3s-local/namespace.yaml
kubectl -n tpi-miners create secret generic gateway-auth --from-literal=miner-token="$MINER_TOKEN"
```

Sin runner self-hosted, el despliegue a mano es:

```bash
cd k3s-local
kustomize edit set image MINER_IMAGE=ghcr.io/<owner>/<repo>/miner-cpu:<sha>   # o editen kustomization.yaml
sed -i "s#http://GATEWAY_IP#http://<ip-del-gateway>#" config.yaml keda-scaledobject.yaml
kubectl apply -k .
```

Escalado: KEDA consulta `GET /scaling` del gateway y fija las réplicas en `desired_cpu_miners`
(0 mientras haya un minero GPU vivo). Si prefieren no instalar KEDA, saquen
`keda-scaledobject.yaml` de la kustomization y corran `examples/local_autoscaler.py`.

## Contrato mínimo que el starter espera de sus servicios

- Puerto HTTP `8080` con `GET /health` (lo usan las probes).
- El coordinador expone `POST /transactions` y `GET /status` con el campo `pending_transactions`
  (lo usa `loadtest/bench.py`; si sus rutas son otras, pásenlas con `--tx-path` y `--status-path`).
  Para bulks de 100.000 conviene que tengan un endpoint de carga masiva: `bench.py` manda de a una.
- El gateway expone `GET /tasks/next`, `POST /results`, `POST /keepalive` y `GET /scaling`, todos
  con `Authorization: Bearer <MINER_TOKEN>`.
- Los servicios de GKE leen `RABBITMQ_URL` y, los que usan Redis, `REDIS_HOST` y `REDIS_PASSWORD`.
  Los mineros leen `GATEWAY_URL`, `MINER_TOKEN`, `MINER_ID` y `MINER_KIND`.
- El pool lee `DIFFICULTY_PREFIX`, `POOL_FRAGMENT_PERCENT`, `GPU_KEEPALIVE_TIMEOUT_SECONDS`,
  `MIN_DIFFICULTY_WITHOUT_GPU`, `CPU_MINERS_MAX` y `TASKS_PER_CPU_MINER` del ConfigMap `tpi-config`;
  el gateway, `TASK_LEASE_SECONDS`.

## Mediciones (sección 3.3)

```bash
kubectl -n tpi get svc coordinator gateway    # IPs públicas (contexto GKE)
cd loadtest
BASE_URL=http://<ip> GPU=1 K3S_KUBECONFIG=~/.kube/k3s.yaml ./matrix.sh
uv run --with matplotlib python plot.py results.csv --out charts/
```

## Costos: apaguen todo al terminar

```bash
# Primero lo que creó Kubernetes (no está en el estado de OpenTofu): los LoadBalancer
# y los discos de los PVC. Si borran el cluster antes, quedan huérfanos y facturando.
kubectl delete namespace tpi
cd infra/terraform && tofu destroy
gcloud compute forwarding-rules list && gcloud compute disks list   # verificar que no quedó nada
```

Los mineros del k3s no cuestan crédito de Google, pero consumen su máquina:
`kubectl -n tpi-miners scale deploy/miner-cpu --replicas=0` (y paren KEDA o el autoscaler) cuando no midan.
