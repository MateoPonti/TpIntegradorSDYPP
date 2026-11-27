# Informe final · TP Integrador SDyPP 2026 · Grupo SDMER

Mateo Ponti · Federico Kasparian · María Agustina Ortiz · Justino Bernal. Entrega: viernes 04/12/2026.

> **Borrador del 27/11**, armado con Claude Code a partir del repo, los README y las mediciones. Cada sección
> dice quién es su autor (quien hizo esa parte): esa persona la lee y corrige lo que no sea así. Después cada
> uno lee una sección que no escribió (revisión cruzada, Task 19 paso 2).

El sistema es un pool de minería colaborativo: todos los mineros trabajan sobre el mismo bloque, cada uno en un
rango distinto de nonces, con Proof of Work `md5(base + str(nonce))`. Corre en `https://sdmer.dpdns.org`.

---

## 1. Qué funciona (con evidencia)

### 1.1 Minero CUDA y minero CPU · Autor: Mateo

- Los 3 TODO del kernel (`pilar1/gpu/miner_gpu.cu`): MD5 de varios bloques de 64 bytes, grid-stride en
  `uint64_t` y corte temprano con `atomicCAS` sobre una bandera que se lee cada K iteraciones.
- `make test`, `make check-oracle` y `make check` (17/17) en verde: pasan los casos trampa del kit
  (`big-nonces`, `long-base-d3`, `utf8-base`, `none-in-range`, `single-nonce-hit`, `d4-offset`).
- Mismo contrato en CPU y GPU, así que el agente de minado es uno solo y cambia el motor.

### 1.2 Transacciones, cuentas y bloque · Autor: Federico y Justino

- Cuentas con clave Ed25519; toda tx de una cuenta registrada va firmada con `seq` (anti-replay) y con
  `CHAIN_ID = 2026` (Task 2). Las 97 cuentas del génesis solo transfieren entre ellas (Task 1, A1): `user-3` →
  cuenta propia da 400, verificado en producción el 27/11.
- Montos no finitos (`Infinity`, `NaN`) dan 400 y un error en una tx del bulk no corta las demás (Task 4, C3):
  verificado en producción el 27/11.
- Reserva de saldo atómica en Lua (disponible = saldo − pendiente), ventanas de reloj de 10 s, bloques de hasta
  1.000 tx, un bloque a la vez y sellado atómico con `tip == prev`.
- Un ganador que llega con el `block_id` de antes de un rearmado sella igual, y dos réplicas con el mismo
  ganador producen un solo sellado (Task 5, C1).
- Cuentas compartidas m de n y colectas "todo o nada"; aceptar y rechazar una compartida en un solo Lua
  (Task 6, C2).

### 1.3 Mineros: verificación, shares y premio · Autor: Federico (1.3) y Agus (premio)

- El gateway empuja las tareas por WebSocket (`mining.notify`) después de un `mining.authorize` firmado sobre
  un desafío; los resultados van firmados (Task 14).
- Canarios que no se distinguen de una tarea real, préstamos con dueño (409 si no) y límite de altas (Task 10).
- Hashrate medido con shares verificadas y Vardiff por worker (Task 11).
- Premio PPLNS con N = 2 y `reward:carry` cuando un bloque no tiene shares (Task 12): el 27/11 el bloque 610
  no tenía a quién pagarle y el premio pasó al siguiente sin perderse.

### 1.4 Notifier y front · Autor: Justino

- Notifier JSON-RPC 2.0 (`sdmer_subscribe` a `newBlocks`, `tx` y `account`, errores estándar) y front sin
  polling con el WebSocket abierto (Task 13). Probado en producción el 27/11 por `wss://sdmer.dpdns.org/ws`.

### 1.5 Infraestructura · Autor: Agus (dominio, endurecimiento, KEDA, pipelines) y Mateo (cluster, datos)

- GKE con dos node pools (`apps` e `infra`), RabbitMQ ×3 con colas quorum (Raft) y Redis 1 principal + 2
  réplicas + 3 Sentinel con AOF y PVC. Todos los servicios con 2 réplicas o más; coordinator y gateway con HPA.
- Dominio propio detrás de Cloudflare (A2): http contesta 301, `/logs` y `/metrics` dan 403 desde afuera, y la
  IP del LoadBalancer, la del gateway y sslip.io no responden (solo entran los rangos de Cloudflare).
  Certificado de Let's Encrypt por DNS-01. Verificado el 27/11.
- Pods sin root, raíz de solo lectura, sin capabilities, con límite de CPU y ServiceAccount propia (B2).
- KEDA baja a 0 mineros CPU a los 60 s sin trabajo: **mediana de 65 s** en 3 corridas del 27/11 (71, 60 y
  65 s), contra ~180 s antes (A3-h). Ver `pilar3/README.md`.
- Mineros apagados con SIGTERM devuelven sus préstamos sin contar como falla, y una colecta trabada no frena a
  las demás (Task 7, C5 y C6).
- CI con tests (Redis real para los Lua) y gitleaks en cada push; OIDC/WIF para GCP, sin claves estáticas.

---

## 2. Qué no funciona o quedó a medias (con el motivo)

- **El CD no despliega solo.** Los pipelines 1 a 4 están y hacen lo mismo que el despliegue a mano, pero el
  repo no tiene las variables de GCP (`GCP_WIF_PROVIDER`, `GCP_DEPLOYER_SA`, …), así que los jobs se saltean.
  El cluster se armó y se actualiza a mano desde la terminal. Motivo: el cluster venía del TP3, con datos de la
  cadena adentro, y probar el pipeline contra él a pocos días de la entrega era más riesgo que beneficio.
  · Autor: Agus
- **Las NetworkPolicies están aplicadas pero el cluster no las hace cumplir** (sin Dataplane V2). Lo que protege
  a Redis y RabbitMQ es que son `ClusterIP` y tienen contraseña. · Autor: Agus
- **Sin TLS dentro del cluster** para AMQP y Redis (B2.5). El tráfico no sale de la VPC. · Autor: Agus
- **Prefijo automático con GPU:** elige 8 ceros porque el minado promedio es 4,3 s, pero de punta a punta un
  bloque tarda ~14 s y con tx llegando todo el tiempo el sistema se atrasa. Quedó sin decidir si elegir el
  prefijo con el tiempo real de los últimos bloques o con más margen. · Autor: Mateo
- **Dos colas (GPU y CPU):** se dejó una sola; la decisión de separarlas quedó para después de medir.
- **Por Cloudflare la ingesta baja a la mitad** (de ~240 a ~124 tx/s con `bench.py`, ver 6.2), y en el bulk de
  100.000 hubo 28 pedidos (0,03 %) que no llegaron al coordinator y no se pudo saber por qué. · Autor: Mateo

---

## 3. Decisiones y alternativas descartadas

| Decisión | Alternativa descartada | Por qué | Autor |
|---|---|---|---|
| Génesis solo transfiere dentro del génesis | Borrar el génesis o pedirle firma | Es la única que no rompe `bench.py` (que no se puede tocar) y deja esa plata en un circuito cerrado de prueba | Federico |
| PPLNS con N = 2 | PROP (lo que había hasta el 26/11) y PPS | Con PROP convenía entrar recién cuando el bloque estaba por salir (*pool hopping*); PPS deja todo el riesgo en el pool | Agus |
| Vardiff con hashrate medido por shares | Confiar en lo que declara el minero | Un minero puede mentir; las shares las verifica el coordinator con MD5 | Federico |
| Cloudflare con dominio gratis | Comprar un dominio; seguir con sslip.io | sslip.io es la IP con otro nombre; el dominio gratis alcanza para TLS y proxy | Agus |
| Un bloque a la vez | Minar varios bloques en paralelo | Cada bloque necesita el hash del anterior | Todos |
| Redis + Lua | Base relacional con transacciones | Los Lua tocan varias claves de forma atómica y rápida; por eso Sentinel y no Redis Cluster | Mateo |
| El límite de entregas en el reaper del gateway | `x-delivery-limit` de las colas quorum | El gateway hace ack enseguida y lleva el préstamo en Redis: RabbitMQ nunca ve una re-entrega | Mateo |

El detalle de cada una, con fecha, está en `docs/decisiones.docx`.

---

## 4. Limitaciones

- **MD5 como PoW:** sirve para el TP, pero no es resistente a colisiones y tiene ASIC/GPU muy optimizados.
- **Un solo líder** arma y sella (`coord:leader`): si se cae, otra réplica toma el lock, pero mientras tanto no
  se sella. El Lua `tip == prev` evita dos sellados de la misma altura.
- **Ventanas de reloj:** las calcula solo el líder con su reloj, así que un desfase entre réplicas mueve la
  ventana a lo sumo lo que dura un cambio de líder (sección "Relojes" del Pilar 3).
- **TLS interno** ausente (ver 2).
- **Mineros solo locales:** la prueba gratuita de GCP prohíbe minar, así que todos los mineros están fuera de
  GCP (k3s local, la PC con la 1050 Ti y la RTX 4060 de InferHive).
- **Perder el `.key` es perder la cuenta:** no hay recuperación ni cambio de clave.
- **La lista de rangos de Cloudflare está congelada al 27/11:** si Cloudflare agrega uno, hay que actualizarla.

---

## 5. Dónde se aplicaría en un contexto real

- **Pools de minería reales:** el reparto de rangos, las shares con Vardiff, PPLNS y la verificación de workers
  son lo mismo que hace Stratum. Habría que cambiar el PoW (SHA-256 doble) y aceptar miles de workers por
  conexión persistente.
- **Ledger interno de pagos:** el modelo de cuentas con reserva atómica, firmas y `seq` sirve para un ledger de
  una organización. Sobraría el PoW: con participantes conocidos alcanza un consenso por votación.
- **Cómputo voluntario (tipo BOINC):** prestar trabajo con lease, verificar con canarios y premiar por trabajo
  válido es justo el problema de repartir cómputo entre máquinas en las que no se confía.

---

## 6. Resultados

### 6.1 Pilar 1: CPU contra GPU · Autor: Mateo

Hashrate MD5 del motor, mediana de 5 corridas con calentamiento (`pilar1/rendimiento/`):

| Motor | MH/s | Contra la mejor CPU |
|---|---:|---:|
| CPU, 1 proceso | 1,61 | |
| CPU, 12 procesos (i5-12400F) | 8,07 | ×1 |
| GTX 1050 Ti (sm_61) | 1.104 | ×137 |
| RTX 4060 de InferHive (sm_89) | 4.970 | ×616 |

Hit #6 (prefijos de 4 a 8) y Hit #7 (100 M de nonces con y sin solución) están en `pilar1/README.md`. Sin
solución hay que recorrer el rango entero y ahí se ve el hashrate puro: la RTX 4060 tarda 20 ms donde la CPU
tarda casi 12 s.

### 6.2 Carga · Autor: Mateo (por Cloudflare) y Justino (4 contra 8)

Por `https://sdmer.dpdns.org`, el 27/11, con `bench.py` (una tx por pedido, 32 en paralelo):

| Bulk | Aceptadas | Errores | Ingesta | p50 |
|---:|---:|---:|---:|---:|
| 10.000 | 10.000 | 0 | 121 tx/s | 259 ms |
| 100.000 | 99.972 | 28 | 124 tx/s | 252 ms |

No pegó ningún límite de Cloudflare (100 s, 100 MB) porque cada pedido es chico. La ingesta es la mitad que por
la IP directa el 20/11 (~240 tx/s, p50 ~110 ms): con 32 pedidos en paralelo, la ingesta es más o menos
32 ÷ latencia, y el proxy agrega ~140 ms. `throughput_tx_per_s` de `bench.py` mide **ingesta** (los 202), no
confirmación.

**4 contra 8 mineros CPU (Task 18, A6),** 1.000 tx con prefijo fijo 7, 3 corridas por punto
(`pilar3/loadtest/resultados/2026-11-27-escalado/`):

| Mineros CPU | Confirmado (tx/s) por corrida | Mediana | Ingesta |
|---:|---|---:|---:|
| 4 | 6,8 · 6,6 · 4,7 | 6,6 | 119 |
| 8 | 6,7 · 20,0 · 28,3 | 20,0 | 119 |

La mediana da 3×, pero no es un speedup real: los 8 pods comparten los 6 núcleos de una PC (~12 MH/s contra
~6,4), así que lo esperable es menos de 2×. Con prefijo 7 el tiempo hasta encontrar el nonce es exponencial y con
3 corridas la suerte pesa más que el hashrate; el mínimo de 8 pods (6,7) cae dentro del rango de 4. Lo que sí se
puede afirmar es que 8 confirman más rápido en mediana y que la ingesta (119 tx/s) no depende de los mineros, la
limita Cloudflare. Para cuantificar el speedup harían falta muchas más corridas.

---

## 7. Uso de IA

Está en el `README.md` raíz, sección "Uso de IA", una por integrante.
