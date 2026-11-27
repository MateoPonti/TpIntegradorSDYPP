# Pilar 3 · GKE, escalado y mediciones

## Cómo quedó adaptado el starter a este repo

- Los servicios **no** están en `services/` sino en `pilar2/services/`. Se construyen desde la raíz:
  - coordinator / pool / gateway / notifier: `docker build -f pilar2/services/Dockerfile --build-arg SERVICE=<svc> .`
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
| Frontend, API y `/status` | **`https://sdmer.dpdns.org`** (dominio propio detrás de Cloudflare; por http contesta 301) |
| Portal para unirse al pool | `https://sdmer.dpdns.org/unirse` |
| Avisos por websocket (notifier) | `wss://sdmer.dpdns.org/ws` (Traefik manda `/ws` al notifier) |
| Gateway de los mineros | **`https://gateway.sdmer.dpdns.org`** (única puerta: el Service dejó de tener IP pública propia) |
| IP del cluster (35.223.237.60) | No contesta desde Internet: el LoadBalancer solo acepta los rangos de Cloudflare |
| Imágenes | `us-central1-docker.pkg.dev/tp3distribuidos/tp3/tpi-{coordinator,pool,gateway,notifier}:<sha>` |
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
tiene la IP fija `tpi-coordinator` y reparte por nombre: `sdmer.dpdns.org` → coordinator (y `/ws` → notifier, el
websocket pasa sin configuración extra) y `gateway.sdmer.dpdns.org` → gateway. cert-manager (namespace
`cert-manager`, v1.21.2) saca y renueva solo el certificado de Let's Encrypt. Se eligió Traefik y no
ingress-nginx porque ingress-nginx ya no se mantiene. Manifiestos e instalación en `k8s/https/`. Ojo: si se mueve
la IP, el primer desafío puede fallar por timeout; se reintenta con `kubectl -n tpi delete certificate tpi-tls`.

Los nombres pasaron por tres etapas: DuckDNS (`sdmer.duckdns.org`, 20/11), `sslip.io` sobre la IP
(`35-223-237-60.sslip.io`, que es DuckDNS afuera y no necesita cuenta) y, desde el 27/11, el dominio propio
detrás de Cloudflare que pidió el profe (abajo). El desafío de Let's Encrypt pasó de HTTP-01 a DNS-01 en el
mismo cambio.

**Dashboard:** `k8s/observability/dashboard-tpi.yaml` es un ConfigMap con la etiqueta `grafana_dashboard`; el
sidecar de Grafana lo carga solo. 22 paneles: altura, pendientes, prefijo, hashrate por tipo y por minero, tiempo
por bloque, tx/s, bloques rearmados, cola, vencidos, DLQ, canarios, cuarentena y tx por bloque. Solo lee el
namespace `tpi`.

Task 17 (26/11): cuatro paneles adicionales, **RabbitMQ → worker p50/p95**, **Validación del bloque**,
**Éxito CPU vs GPU** y **Minado por prefijo**. `results_total` tiene etiquetas `status,kind` y
`block_seconds` tiene `prefix_len`. Las consultas agregan réplicas preservando `le` en los histogramas.
`task_queue_to_lease_seconds{kind}` mide desde la última publicación hasta el préstamo, sin canarios ni
calibraciones. `published_at` queda en RabbitMQ/Redis y no se envía al minero para no revelar canarios.
Se reinicia al republicar; tareas anteriores sin timestamp no generan muestras. Un reloj desfasado que dé
duración negativa tampoco genera muestra. `block_validation_seconds` incluye PoW, shares y sellado Redis,
antes de publicar el outbox. Ambos hosts deben sincronizar hora para medir espera de cola.

Verificación local: pruebas de muestras Prometheus y sellado real con Redis. **Pendiente de despliegue:**
comprobar series en Prometheus y adjuntar captura de los cuatro paneles con datos reales; no hay captura
de producción todavía. No requiere cambios en `monitoring` ni en los node pools.

**Prometheus persistente:** `k8s/observability/prometheus-persistencia.yaml` le agrega al Prometheus de `monitoring`
un disco de 10 GB (`pd-standard`) y 30 días de retención, con `helm upgrade --reuse-values` (el comando está
en el archivo).

**Otra cuenta de GCP:** `infra/terraform` crea un cluster propio con los mismos node pools, discos de 30 GB
`pd-standard` (para que la cuota de SSD no frene al autoscaler), el registry `tpi`, la identidad de GitHub
y las dos IPs fijas (`tofu output static_ips`). La IP va a ser otra: hay que ponerla en
`loadBalancerIP` de `k8s/https/traefik-values.yaml` y en los registros A de Cloudflare (desde el 27/11 los
Services de las apps no piden IP pública, así que no hay nada que cambiar en `k8s/apps/`). La IP de
`tpi-gateway` quedó sin uso.

## Dominio propio detrás de Cloudflare (A2, 27/11/2026)

Lo que el profe marcó en A2: el sistema se servía en `35-223-237-60.sslip.io` y, además, por dos IPs públicas
peladas (`http://35.223.237.60` el front y `http://34.63.209.225` el gateway). Un nombre de `sslip.io` es la IP
escrita con guiones, así que no es un dominio: es la IP con otro traje. Y mientras las IPs contestaran, cualquier
cosa que se ponga adelante (TLS, WAF, rate limit) se saltea pidiéndole directo a la IP.

**Corrección de lo que decía este README el 21/11.** El commit `e38619c` dice "sacar el pendiente de Cloudflare
(el profe aclaró que no hace falta)". Lo que había aclarado era que no hacía falta **comprar** un dominio, y de
ahí sacamos que tampoco hacía falta Cloudflare. En la revisión del 27/11 lo pidió explícito. El historial no se
reescribe: el commit queda como está y este párrafo es la corrección.

| Qué | Dónde | Por qué |
|---|---|---|
| Dominio `sdmer.dpdns.org` | DigitalPlat FreeDomain, con los nameservers de Cloudflare | Gratis y es un dominio de verdad: Let's Encrypt lo firma y Cloudflare lo puede proxear. No se compró ninguno |
| Ingress de los dos nombres | `k8s/https/dominio.yaml` (reemplaza a `ingress.yaml`) | `sdmer.dpdns.org` → coordinator, `/ws` → notifier, `gateway.sdmer.dpdns.org` → gateway. **Sin regla sin host**: la que había servía la IP pelada a cualquiera |
| Certificado por DNS-01 | `k8s/https/cluster-issuer.yaml` (`letsencrypt-dns`) | Con el proxy de Cloudflare adelante y el redirect global a https, el desafío HTTP-01 depende de demasiadas cosas. DNS-01 solo necesita escribir un TXT |
| Solo Cloudflare entra al LB | `k8s/https/traefik-values.yaml` (`loadBalancerSourceRanges`, 15 rangos; la Task 16 mueve este archivo a `k8s/platform/`) | Es lo que convierte al dominio en el único camino. Sin esto el resto es decoración |
| Todo http redirige a https | el mismo `traefik-values.yaml` (`ports.web.redirections`) | 301 para cualquier nombre y cualquier ruta, así que el redirect no delata qué rutas existen |
| El gateway sin IP propia | `k8s/apps/gateway.yaml` (`type: ClusterIP`) | Tenía dos puertas: una con TLS y rate limit, y otra en claro que las salteaba |

Lo revisa `pilar3/tests/test_dominio.py` (sin cluster): que no quede ninguna URL vieja en el repo, que el
Ingress tenga solo los dos nombres, que el issuer sea de DNS-01 y no tenga el token adentro, que los 15 rangos
estén y ninguno sea `0.0.0.0/0`, y que ningún Service nuestro vuelva a pedir una IP pública.

### Lo que hay que hacer a mano (no está en el repo, y no puede estar)

1. **Cloudflare, DNS:** dos registros `A` **proxied** (la nubecita naranja) a `35.223.237.60`, uno en `@` y otro
   en `gateway`. Si quedan en "DNS only", el tráfico no pasa por Cloudflare y el LoadBalancer lo descarta.
2. **Cloudflare, SSL/TLS:** modo **Full (strict)**, "Always Use HTTPS" activado y WebSockets activados (vienen
   así; son los que hacen andar `/ws`). En Flexible, Cloudflare hablaría http con Traefik y el redirect global
   lo manda de vuelta a https: bucle de redirecciones.
3. **El token de la API** con permisos `Zone:DNS:Edit` y `Zone:Zone:Read`, **solo** sobre esta zona, cargado a
   mano una vez. El de lectura hace falta porque cert-manager primero busca el ID de la zona y recién después
   escribe el TXT; sin eso falla con "zone not found" y el certificado nunca sale. **No se commitea** (regla 5
   de `AGENTS.md`):
   ```bash
   kubectl -n cert-manager create secret generic cloudflare-api-token --from-literal=api-token=XXXXXXXX
   ```
4. **La variable `GATEWAY_URL` del repo en GitHub** (Settings → Variables, no Secrets) tiene que decir
   `https://gateway.sdmer.dpdns.org`. El Pipeline 4 reescribe con `vars.GATEWAY_URL` el `config.yaml` y el
   `keda-scaledobject.yaml` de los mineros: si quedó la de `sslip.io`, el próximo despliegue de mineros vuelve
   atrás el cambio de este PR y los mineros dejan de minar sin que nadie toque el repo.
5. **Borrar el issuer viejo**, que ya no está en el repo (lo tenía `ingress.yaml`):
   `kubectl delete clusterissuer letsencrypt`.
6. **Opcional, después de la entrega:** liberar la IP que dejó de usarse,
   `gcloud compute addresses delete tpi-gateway --region us-central1`. Reservada y sin usar, se factura.

### Cómo aplicarlo

```bash
K="kubectl --context gke_tp3distribuidos_us-central1-a_tp3"

# 1. El issuer y el Ingress del dominio. `apply` del Ingress reemplaza al viejo (mismo nombre, `tpi`), y
#    cert-manager pide un certificado nuevo para los dos nombres. El secret `tpi-tls` lo sigue sirviendo
#    Traefik con el certificado viejo hasta que el nuevo esté listo, así que esto no corta nada.
$K apply -f pilar3/k8s/https/cluster-issuer.yaml
$K apply -f pilar3/k8s/https/dominio.yaml
$K -n tpi get certificate tpi-tls -w      # READY=True en 1-2 min. Si tarda: $K -n tpi describe challenge

# 2. Cerrar el LoadBalancer y prender el redirect. `helm upgrade` de Traefik toca su Service: GKE reescribe la
#    regla de firewall del balanceador (unos segundos de cortes en los pedidos nuevos).
helm upgrade --install traefik traefik/traefik -n traefik -f pilar3/k8s/https/traefik-values.yaml
$K -n traefik get svc traefik -o jsonpath='{.spec.loadBalancerSourceRanges}'   # tienen que estar los 15

# 3. Recién ahora el gateway a ClusterIP: hasta que el dominio sirva, los mineros del k3s siguen entrando por
#    la IP vieja. Esto borra el LoadBalancer de GCP (la IP reservada queda, sin usar).
$K apply -f pilar3/k8s/apps/gateway.yaml
$K apply -f pilar3/k8s/apps/config.yaml && $K -n tpi rollout restart deploy coordinator   # /unirse
```

El certificado primero y el gateway al final: en ese orden no hay ningún momento en el que los mineros se
queden sin puerta.

### Verificación (es la que va a correr el profe)

```bash
curl -sI http://sdmer.dpdns.org/          | head -2    # 301 a https://sdmer.dpdns.org/
curl -sI http://gateway.sdmer.dpdns.org/  | head -2    # 301
curl -sI https://sdmer.dpdns.org/status   | head -5    # 200 y `server: cloudflare`
curl -s   https://sdmer.dpdns.org/status  | head -c 200

# Las tres puertas viejas tienen que fallar (timeout del firewall del LB, no 403 ni 404):
curl -s --max-time 5 https://35-223-237-60.sslip.io/status ; echo "salida $?"
curl -s --max-time 5 http://35.223.237.60/                 ; echo "salida $?"
curl -s --max-time 5 http://34.63.209.225/                 ; echo "salida $?"

# Que no se haya roto nada de lo demás:
curl -s -o /dev/null -w "%{http_code}\n" https://sdmer.dpdns.org/logs     # 403 (B2.6)
kubectl -n tpi-miners get scaledobject                                     # READY=True
kubectl -n tpi-miners logs -l app=miner-cpu --tail=5                       # tareas por el dominio
```

### La prueba de carga por Cloudflare (Review Focus 2)

Cloudflare Free tiene dos límites que nos pueden pegar: el **body de 100 MB** y los **100 s de respuesta**
(error 524). El bulk más grande de `matrix.sh` (100.000 tx) puede pasar cualquiera de los dos. Si pasa, la
alternativa ya no es la IP pelada (no existe más): se corre `bench.py` **desde un pod del cluster** contra
`http://coordinator.tpi.svc.cluster.local`, que es lo que ya hacía `loadtest/completa/correr.sh` para la
medición interna, y se anota en los resultados que el bulk grande se midió por adentro y por qué.

### Limitaciones que quedan

- La lista de rangos de Cloudflare está **congelada al 27/11/2026**. Si Cloudflare agrega un rango, el tráfico
  que entre por ahí se descarta y hay que mirar https://www.cloudflare.com/ips-v4 y hacer `helm upgrade` otra
  vez. El test del repo no se puede enterar: no sale a Internet.
- El dominio es gratis y por un año. Si DigitalPlat lo da de baja, se cae el nombre; el cluster sigue vivo pero
  sin puerta pública hasta que se apunte otro nombre.
- Con el proxy de Cloudflare adelante, **todos** los pedidos llegan con la IP de Cloudflare. Cualquier límite
  por IP (el de `/accounts/register`, B1.4) tiene que leer `CF-Connecting-IP`, o todo Internet cuenta como un
  solo cliente.
- El `IngressRoute privado-http` cierra `/logs` y `/metrics` por http; con el redirect global, por http ahora
  sale 301 antes de llegar ahí. Queda igual, como red de seguridad si algún día se saca el redirect.

## Endurecimiento de Kubernetes (B2, 27/11/2026)

Lo que el profe marcó en B2 era que los manifiestos corrían con los valores por defecto de Kubernetes, que son
los permisivos: root, sin límite de CPU, con la ServiceAccount `default` y con Redis y RabbitMQ accesibles
desde cualquier pod del cluster.

| Qué | Dónde | Por qué |
|---|---|---|
| `runAsNonRoot` + `runAsUser` explícito | todos los pods | Un proceso que se escapa del contenedor sin ser root no es root en el nodo. Las apps y el minero corren como **10001** (el mismo uid del `USER` de sus Dockerfile); Redis como **999:1000** y RabbitMQ como **999:999**, que son los usuarios que ya traen esas imágenes |
| `allowPrivilegeEscalation: false` | todos los contenedores | Ni con un binario setuid adentro de la imagen se recupera root |
| `capabilities.drop: [ALL]` | todos los contenedores | Ninguno necesita capabilities del kernel: no abren puertos bajo 1024 ni tocan la red |
| `readOnlyRootFilesystem: true` | apps y minero | No se puede escribir un binario ni un `.py` adentro del contenedor. Donde hace falta escribir hay un `emptyDir` en `/tmp` (los logs rotativos van a `LOG_DIR=/tmp/logs`, y el minero guarda ahí la clave Ed25519 de su worker) |
| `seccompProfile: RuntimeDefault` | todos los pods | Filtra las syscalls que ningún proceso normal usa |
| `resources.limits.cpu` | todos los contenedores | Sin límite, un pod que se vuelve loco se come el nodo y se lleva puestos a los demás |
| ServiceAccount propia + `automountServiceAccountToken: false` | todos | Ninguno de nuestros servicios le habla a la API de Kubernetes (se coordinan con Redis, `coord:leader` y `pool:leader`), así que no hay ningún token de la API para robar. Y en los logs de auditoría se ve quién hizo qué, en vez de `...:tpi:default` para todo |
| NetworkPolicy | Redis y RabbitMQ | `ClusterIP` quiere decir "no publicado", no "protegido": cualquier pod del cluster les hablaba igual |
| `/logs` y `/metrics` con 403 desde Internet | `k8s/https/privado.yaml` | Los logs tienen nombres de cuentas y de transacciones, y `/metrics` es un mapa de cómo está armado el sistema |

`pilar3/tests/test_manifiestos.py` revisa todo eso parseando los YAML, así que si alguien saca un
`securityContext` o se olvida el límite de CPU en un manifiesto nuevo, el CI lo marca.

### Cómo aplicarlo

Cada paso reinicia pods, así que conviene de a uno y mirando que vuelva a `Running`. Los StatefulSets van
primero porque las apps no arrancan sin Redis ni RabbitMQ.

```bash
# 1. Las ServiceAccounts y las políticas, que no reinician nada
kubectl apply -f pilar3/k8s/infra/serviceaccounts.yaml -f pilar3/k8s/apps/serviceaccounts.yaml
kubectl apply -f pilar3/k8s/infra/networkpolicies.yaml

# 2. Redis: rollout de a un pod. Es el que tiene la cadena, así que se mira pod por pod.
#    Al cambiar el uid, Kubernetes le pone el grupo 1000 y g+rwX al PVC (fsGroup), también a los AOF que ya
#    están, así que no se pierde nada; pero un rollout de Redis es un failover de Sentinel: hay unos segundos
#    sin principal.
kubectl apply -f pilar3/k8s/infra/redis.yaml
kubectl -n tpi rollout status statefulset/redis --timeout=300s

# 3. RabbitMQ, igual (pause_minority: con 2 de 3 nodos arriba sigue atendiendo)
kubectl apply -f pilar3/k8s/infra/rabbitmq.yaml
kubectl -n tpi rollout status statefulset/rabbitmq --timeout=300s

# 4. Las apps. Necesitan la imagen nueva (USER 10001): primero el Pipeline 3, o build y push a mano.
kubectl apply -k pilar3/k8s/apps
kubectl -n tpi rollout status deploy/coordinator deploy/gateway deploy/pool deploy/notifier --timeout=300s

# 5. El corte de /logs y /metrics
kubectl apply -f pilar3/k8s/https/privado.yaml
```

Si algo no vuelve a `Running`, el motivo está en `kubectl -n tpi describe pod <pod>` y en los logs del
contenedor. Para volver atrás alcanza con `kubectl apply` del archivo de la rama anterior: no hay migración de
datos en el medio, solo cambia cómo arranca el pod.

**Antes de confiar en las NetworkPolicy**, hay que ver si el cluster las aplica. En GKE solo filtran si está
Dataplane V2 o el add-on de NetworkPolicy; si no, la API las guarda y no hacen nada:

```bash
gcloud container clusters describe tp3 --zone us-central1-a \
  --format="value(networkPolicy.enabled,networkConfig.datapathProvider)"
```

Habilitarlo reinicia los pods del cluster (y el `monitoring` que compartimos no es nuestro), así que si no
está habilitado **no se toca a esta altura**: queda en "Limitaciones" de acá abajo.

### Verificación (es la que va a correr el profe)

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://<dominio>/logs      # 403
curl -s -o /dev/null -w "%{http_code}\n" https://<dominio>/metrics   # 403
curl -s -o /dev/null -w "%{http_code}\n" https://<dominio>/status     # 200: lo público sigue andando
kubectl -n tpi port-forward deploy/coordinator 8080:8080             # así sí se leen los logs
curl -s localhost:8080/logs | tail -5
```

Que Prometheus siga teniendo datos se ve en Grafana: scrapea los pods directo (`PodMonitor`, `pod:8080/metrics`),
no por el Ingress, así que el 403 de afuera no lo afecta.

### Relojes (B2.7)

El profe preguntó de qué reloj dependen las ventanas de 10 s con las que se arman los bloques.

- **Los nodos de GKE sincronizan contra el NTP interno de Google** (`metadata.google.internal`, con `chrony`).
  Google usa *leap smear*: en vez de un segundo intercalar de golpe, reparte la corrección a lo largo del día,
  así que el reloj nunca salta para atrás.
- **La ventana la calcula una sola réplica**: solo el líder del coordinator (`coord:leader`) arma bloques, y
  usa su propio reloj. O sea que un desfase entre réplicas **no** parte una ventana en dos: a lo sumo mueve el
  borde de la ventana cuando cambia el líder, y el bloque siguiente vuelve a la grilla fija
  (`window_end` redondea a múltiplos de `BLOCK_WINDOW_SECONDS`).
- **Las transacciones se ordenan por el `ts` que les pone el coordinator** al aceptarlas, no por la hora del
  cliente: un navegador con la hora mal no puede meter una tx en otra ventana.
- **Los mineros no usan la hora para nada**: el préstamo lo vence el reaper con el reloj del gateway, y el
  minero solo mide duraciones (`time.perf_counter`), que no dependen de la hora del día.

Evidencia del nodo (pegar la salida cuando se corra, para el informe):

```bash
# El nodo no tiene shell: se entra con un pod privilegiado en su namespace de host, o directo por SSH
gcloud compute ssh <nodo> --zone us-central1-a --command "chronyc tracking"
```

```
(completar con la salida de chronyc tracking: interesa "System time" y "Last offset", que tienen que estar
en microsegundos o pocos milisegundos)
```

### Limitaciones que quedan

- **AMQP y Redis van sin TLS dentro del cluster (B2.5).** El tráfico no sale de la VPC y las credenciales van
  en Secrets, pero cualquier cosa que logre ponerse en el medio dentro del cluster ve las contraseñas y los
  mensajes. Se haría con un `Issuer` CA de cert-manager, un certificado por StatefulSet y `rabbitmq.conf` +
  `redis.conf` escuchando TLS; no entró en el tiempo que quedaba y cambiar el transporte de Redis a cinco días
  de la entrega era más riesgo que beneficio.
- **Redis, Sentinel y RabbitMQ corren con la raíz escribible.** Son las tres únicas excepciones y están en la
  lista del test. Esas imágenes escriben fuera de su PVC al arrancar (RabbitMQ instala la cookie de Erlang y
  arma su config; Sentinel reescribe la suya) y no son nuestras, así que no sabemos todo lo que tocan;
  probarlo significaba probarlo en el cluster que tiene la cadena. Igual no corren como root, no pueden
  escalar privilegios y no tienen ninguna capability.
- **Si el cluster no tiene Dataplane V2**, las NetworkPolicy están escritas y aplicadas pero no filtran. En ese
  caso lo que protege a Redis y RabbitMQ sigue siendo que son `ClusterIP` (no se llega de Internet) más la
  contraseña de cada uno. Hay que decirlo en el informe en lugar de dar por hecho que filtran.

## Mineros CPU en varias PCs

### Comparación M frente a 2×M (Task 18, preparada para el 03/12)

`loadtest/escalado.sh` compara 4 y 8 mineros CPU con igual carga, prefijo fijo y fragmentación. Sin argumentos
solo muestra el plan. Con `--run`, pausa KEDA únicamente en el k3s elegido, espera mineros y cola vacía,
llama al `matrix.sh` original y conserva las tres corridas de `bench.py` por punto. No modifica archivos de
la cátedra ni el contexto global de kubectl. Al salir restaura dificultad, fragmentación, réplicas y pausa previa.

```bash
bash pilar3/loadtest/escalado.sh
BASE_URL=https://DOMINIO K3S_CONTEXT=k3d-tpi-mineros \
  OUT="$PWD/pilar3/loadtest/resultados/2026-12-03-escalado" \
  bash pilar3/loadtest/escalado.sh --run
python pilar3/loadtest/confirmado.py --api https://DOMINIO --desde 1796328000 --hasta 1796331600
```

Antes de ejecutar: código congelado, GPU desconectada, ninguna otra carga y relojes sincronizados. Usar
el entorno de `requirements-dev.txt` para generar gráficos. `GKE_CONTEXT` identifica solo los servicios;
`K3S_CONTEXT` es obligatorio y el script rechaza nodos GCE. Configurar `PREFIX`, `FRAGMENT` y `BULK`
para elegir el experimento (por defecto, 7 ceros, 10 % y 1000 tx).

`confirmado.py` pagina `/blocks` y obtiene transacciones desde `/blocks/{i}`. Cuenta solo tx de usuario en
bloques sellados dentro del intervalo, sin génesis ni premios. Throughput confirmado = cantidad / (último
sello − timestamp de primera tx), incluyendo espera de ventana. Sin datos devuelve 0 y timestamps `null`.
`medir_corrida.py` envuelve `bench.run` sin alterar su código y guarda `confirmados.csv` y JSON por corrida;
un conteo distinto al aceptado invalida la corrida. `resumen_escalado.py` produce CSV, PNG y README con
mediana, mínimo, máximo y speedup contra 2× ideal. La atribución de cuellos de botella requiere métricas.

**Estado:** herramientas y pruebas locales preparadas. Medición real, CSV/PNG de entrega y análisis del
cluster pendientes para el 03/12; no se generaron resultados de carga ficticios.

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
$K -n tpi-miners create secret generic gateway-auth --from-literal=miner-token="$MINER_TOKEN" --from-literal=scaling-token="$SCALING_TOKEN"
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
kubectl -n tpi-miners create secret generic gateway-auth --from-literal=miner-token="$MINER_TOKEN" --from-literal=scaling-token="$SCALING_TOKEN"
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

### Bajar a 0 mineros CPU: quién decide los 60 s (A3-h)

En la revisión del 21/11 el apagado tardaba ~180 s y el contrato decía 60 s. Eran **dos esperas
sumadas**, no una:

| Quién espera | Cuánto | Dónde está |
|---|---|---|
| El pool, antes de informar `desired_cpu_miners = 0` | `CPU_IDLE_SECONDS` = 60 s | `pool/main.py` → `decision.desired_cpu_miners` |
| KEDA, después de ver el 0, antes de escalar a cero | `cooldownPeriod` = 120 s | `k3s-local/keda-scaledobject.yaml` |

La decisión queda en **un solo lugar, el pool**: `cooldownPeriod` baja a **5 s** (un
`pollingInterval`), así KEDA escala apenas ve el 0 y el tiempo total es el que decide el pool.
Para cambiar ese tiempo se toca `CPU_IDLE_SECONDS`, no el ScaledObject.

**Medición** (en el k3s local, con el gateway en GKE):

```bash
export KUBECONFIG=~/.kube/k3s.yaml
bash k3s-local/medir-escalado-a-cero.sh https://<dominio>        # manda 1 tx y cronometra
```

El script manda una transacción, espera a que el bloque se selle y cronometra desde el último
resultado entregado hasta que `kubectl -n tpi-miners get deploy miner-cpu` muestra 0 réplicas.
Criterio del contrato: **≤ 70 s** (60 s del pool + el `pollingInterval` de 5 s + el apagado del pod).

| Fecha y hora | Antes (`cooldownPeriod: 120`) | Después (`cooldownPeriod: 5`) |
|---|---|---|
| _(completar con la corrida)_ | ~180 s (lo que midió el profe el 21/11) | _(completar: tiene que dar ≤ 70 s)_ |

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

**Hechas el 20/11/2026:** [`loadtest/resultados/2026-11-20-completa/`](loadtest/resultados/2026-11-20-completa/README.md)
(bulks de 1 a 100.000, prefijos 1 a 8, fragmentación 1 a 50 %, CPU contra GPU con el mismo prefijo y entrada y
salida de la GPU), corridas con [`loadtest/completa/correr.sh`](loadtest/completa/correr.sh) desde un pod dentro de
GKE. Resumen:
- Con el **mismo prefijo** la GPU gana claro: con 8 ceros, 19 s contra 251 s (13 veces); con 7 ceros, de 3 a 8 veces.
- Hasta 6 ceros los dos dan ~10 s: el límite es la **ventana de 10 s**, no el minado.
- En **automático** la GPU tarda más, porque el pool sube el prefijo a 8: usa la potencia para subir la dificultad.
  Con carga continua cada bloque tarda ~14 s y el sistema se atrasa (mejora posible, no decidida).
- Al entrar la GPU, el prefijo sube a 8 en 5 s; al salir, el bloque se rearma a los 19 s y KEDA vuelve a levantar
  los 8 pods CPU.
- Se encontró que el HPA tiraba pedidos al bajar réplicas (142 errores en una corrida de 10.000): arreglado con
  `preStop` y una ventana de 300 s; después, 0 errores.

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
