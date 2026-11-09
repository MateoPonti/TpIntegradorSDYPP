# Pilar 3 · GKE, escalado y mediciones

## Cómo quedó adaptado el starter a este repo

- Los servicios **no** están en `services/` sino en `pilar2/services/`. Se construyen desde la raíz:
  - coordinator / pool / gateway: `docker build -f pilar2/services/Dockerfile --build-arg SERVICE=<svc> .`
  - miner-cpu (k3s local, **nunca GCP**): `docker build -f pilar2/services/miners/cpu/Dockerfile .`
- Los workflows están en `.github/workflows/` de la raíz, con las rutas `pilar3/...` y `pilar2/...`.
- `k8s/apps/config.yaml` tiene además las variables del grupo (armado del bloque, reintentos, `GATEWAY_HEALTH_URL`).
- `GET /scaling` lo sirve el **gateway** leyendo `pool:scaling` (duda 11, opción A, como trae el starter).
- Los mineros GPU no tienen manifiesto: corren en la PC (`pilar2/services/miners/gpu/run.ps1`) o en Colab.
- Pendiente: Prometheus + Grafana en GKE, y adaptar `bench.py` a `POST /transactions/bulk` en una copia
  propia (el `bench.py`, `matrix.sh` y `plot.py` de la cátedra no se modifican).

En los comandos de abajo, todo lo que dice `infra/terraform`, `k8s/` o `k3s-local/` está dentro de `pilar3/`.

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
