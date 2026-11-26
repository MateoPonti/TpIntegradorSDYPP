# Ajustes para la entrega final (04/12/2026): plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolver todos los pedidos del profe (`ajustes-team.pdf`, revisión de `5ebe0a9` del 21/11): A1–A6, B1–B4 y C1–C9. En el contrato (A3) se implementan los puntos a–h en vez de solo corregir el texto.

**Architecture:** Los cambios caen sobre lo que ya existe: coordinator, gateway, notifier, pool, agente de los mineros, manifiestos de `pilar3/k8s` y `pilar3/k3s-local`, y workflows. No se agregan componentes nuevos: Cloudflare reemplaza a DuckDNS/sslip.io y no es un servicio del cluster. Cada tarea va en **su propia rama y su propio PR**, revisado por otro integrante (responde al "son cuatro" del PDF).

**Tech Stack:** Python 3 (FastAPI, redis-py asyncio + Lua, aio-pika, prometheus_client, cryptography Ed25519), JS del front sin framework, Kubernetes (GKE + k3s/KEDA), Traefik, cert-manager, OpenTofu, GitHub Actions, Cloudflare.

**Spec:** `ajustes-team.pdf` (raíz del repo) + `AGENTS.md` + `docs/contrato-1.md` + `docs/decisiones.docx`.

## Decisiones ya tomadas (26/11, con Federico)

- **A1:** el génesis solo transfiere entre cuentas del génesis (opción 1 del PDF). `bench.py` sigue andando sin cambios.
- **A2:** hay que **comprar un dominio** (no hay otra forma de tener los nameservers en Cloudflare). En el plan figura como `DOMINIO` (placeholder `sdmer.xyz`).
- **A3:** se implementan **a, b, c, d, e, f, g y h**. Para **i** se corrige el texto (son 3 nodos y el límite de entregas lo hace el reaper).
- **Fecha:** entrega el **viernes 04/12/2026**. El código se congela el **miércoles 02/12 a la noche**. El 03/12 queda para pruebas de carga, informe y video.

## Global Constraints

- Todo lo de `AGENTS.md` sigue valiendo. En particular: nonces `uint64`, montos en **centavos enteros**, **no modificar** `bench.py`, `matrix.sh` ni `plot.py`, **ningún minero en GCP**, nada de secretos en el repo (el token de la API de Cloudflare va en un Secret creado a mano o en GitHub Secrets).
- Cada decisión nueva va a `docs/decisiones.docx` con fecha y motivo (`python-docx` o a mano), y cada cambio del contrato va a `docs/contrato-1.md` con un apartado "Cambios respecto de la v1" que diga **por qué** cambió.
- `CHAIN_ID` = **`2026`** (entero, como en EIP-155). Va en todos los mensajes firmados.
- Tests: `pytest -q pilar2/tests` y `pytest -q pilar3/tests` en verde antes de cada PR. Los tests de Lua usan un Redis real: `REDIS_URL=redis://localhost:6379/15` (`docker run -p 6379:6379 redis:7.4-alpine`). En el CI, `services: redis`.
- Commits con autor real (cada integrante commitea lo suyo, sin trailer `Co-Authored-By` de la IA). El uso de IA se declara en la sección de A5 (Task 20).

## Review Focus

1. **Tx firmadas viejas después de agregar `CHAIN_ID`:** las tx que ya están pendientes en Redis con la firma vieja no se vuelven a verificar, pero **el front y `tools/cuenta.py` cacheados** van a mandar firmas sin chain id → tiene que volver un 400 claro ("firma inválida: actualizá la página, cambió el formato v2"), no un 500. Test en la Task 2.
2. **Bulk de 100.000 tx a través de Cloudflare:** el plan Free corta a los **100 s** y a los **100 MB** de body. Hay que verificar que el bulk más grande de `matrix.sh` entra en ese tiempo y, si no, documentarlo (bulk ≤ 10.000 por request o `gateway`/API en un host "DNS only"). Lo cubre el paso de verificación de la Task 9.
3. **Minero que se corta a mitad del WebSocket de trabajo (c):** si el WS se cae con tareas prestadas, el lease tiene que vencer y el reaper re-publicar igual que hoy. Ningún rango se puede perder ni pagar dos veces. Test en la Task 14.
4. **Rearmado + "encontré" tardío (C1) en paralelo con el sellado de otra réplica:** dos réplicas que procesan el mismo nonce ganador tienen que terminar con **un solo** sellado (el Lua `tip == prev` ya lo garantiza; el test lo tiene que pinear). Test en la Task 5.
5. **Ventana PPLNS al arrancar la cadena (bloque 1, sin shares previas):** el premio no se puede perder ni dividir por cero. Se acumula en `reward:carry` y se paga en el primer bloque con shares. Test en la Task 12.

---

## Reparto (decidido el 26/11)

| Frente | Responsable | Tasks |
|---|---|---|
| Cuentas, firmas, verificación de mineros y gateway | Federico | 8, 1, 2, 10, 11, 14 |
| Front, notifier, bloques, métricas y carga | Justino | 4, 5, 6, 13, 17, 18 |
| Infra, Kubernetes, premio y robustez | Agus (María Agustina) | 3, 7, 9, 12, 15, 16 |
| Todos (incluido Mateo) | — | 19 (docs/informe), 20 (IA), 21 (video) |

Rama por tarea: `task-<n>-<tema>` (ej. `task-12-pplns`). Un PR por tarea, revisado por otro integrante. Las tareas que necesitan la fixture de la Task 8 se hacen sobre `main` una vez mergeado su PR (o sobre su rama, con el PR apuntando a ella). La Task 9 (Cloudflare) espera a que esté el dominio.

Orden: la **Task 8** (fixture `redis_db` + Redis en el CI) va antes de las Tasks 6, 7, 10, 12 y 14, que la usan. La **Task 2** (chain id) va antes de la 14, porque la 14 agrega mensajes firmados nuevos.

Calendario: **27–28/11** Tasks 1–8 (rápidas, A y C). **28/11** comprar el dominio. **29/11–01/12** Tasks 9–13 y 15–17. **30/11–02/12** Task 14 (la más grande). **03/12** Task 18 (carga sobre el sistema final), 19, 20 y 21.

---

## Fase 1: prioridad A de código y correcciones C rápidas

### Task 1: A1, el génesis solo transfiere dentro del génesis

**Files:**
- Modify: `pilar2/services/coordinator/accounts.py` (función nueva)
- Modify: `pilar2/services/coordinator/main.py` (`_submit`, ~línea 138)
- Modify: `pilar2/services/coordinator/static/cuenta.html` (cartel en la sección "cuentas de prueba del génesis")
- Test: `pilar2/tests/test_unit.py`, `pilar2/tests/test_integration.py`
- Docs: `docs/decisiones.docx`, `pilar2/README.md` (reemplazar "limitación conocida… opción B"), `AGENTS.md` §3 "Cuentas con clave"

**Interfaces:**
- Produces: `accounts.genesis_transfer_ok(sender: str, receiver: str) -> bool`. Devuelve True si el sender no es del génesis, o si los dos son del génesis.

- [ ] **Step 1: Tests que fallan**

```python
def test_genesis_solo_dentro_del_genesis():
    assert cuentas.genesis_transfer_ok("user-3", "user-4")
    assert not cuentas.genesis_transfer_ok("user-3", "fede")
    assert not cuentas.genesis_transfer_ok("user-3", "colecta:viaje")
    assert cuentas.genesis_transfer_ok("fede", "user-3")  # al génesis sí se le puede mandar
```
En `test_integration.py` (con el compose): `POST /transactions {"sender":"user-3","receiver":<cuenta registrada en el test>,"amount":1}` → **400** con `"error"` que contiene `"génesis"`.

- [ ] **Step 2:** `pytest -q pilar2/tests -k genesis` → FAIL (`genesis_transfer_ok` no existe).
- [ ] **Step 3:** Implementar la función y llamarla en `_submit` **antes** de `_receiver_exists`. Rechazo con `TX_REJECTED.labels("genesis fuera del genesis")` y mensaje `"las cuentas del génesis solo transfieren a otras cuentas del génesis"`.
- [ ] **Step 4:** Tests en verde. Correr `bench.py` contra el compose (1 bulk de 1.000): todas aceptadas.
- [ ] **Step 5:** Decisión en `decisiones.docx` con el motivo: es la única opción que no rompe `bench.py` (que no se puede tocar) y deja los ~97M en un circuito cerrado de prueba; la plata "real" sale solo del minado. Commit: `A1: las cuentas del génesis solo transfieren entre sí`.

### Task 2: A3-g, chain id en todo lo que se firma

**Files:**
- Modify: `pilar2/services/tpi_common/firmas.py`, `pilar2/services/tpi_common/config.py`
- Modify: `pilar2/services/coordinator/accounts.py`, `multisig.py`, `crowdfund.py` (los `*_message`)
- Modify: `pilar2/services/coordinator/main.py` (`GET /status` o `GET /chain` expone `chain_id`)
- Modify: `pilar2/services/coordinator/static/cuenta.html`, `unirse.html` (y todo JS que arme mensajes firmados: `grep -n "register:\|tx:\|worker:\|multisig:" static/*.html`)
- Modify: `pilar2/tools/cuenta.py`
- Test: `pilar2/tests/test_unit.py`

**Interfaces:**
- Produces: `config.CHAIN_ID: int` (env `CHAIN_ID`, default `2026`); `firmas.domain(message: str) -> bytes` = `f"sdmer:{CHAIN_ID}:{message}".encode()`. **Todos** los `*_message` pasan por `domain()` (register, tx, worker, multisig create/reject/payment, crowdfund create/contribute).
- `GET /status` agrega `"chain_id": 2026`. El front y `tools/cuenta.py` lo leen de ahí, no lo hardcodean.

- [ ] **Step 1: Tests que fallan**

```python
def test_tx_message_lleva_chain_id(monkeypatch):
    assert cuentas.tx_message("a", "b", 100, 1) == b"sdmer:2026:tx:a:b:100:1"

def test_firma_de_otra_cadena_no_vale():
    k = Ed25519PrivateKey.generate(); pub = k.public_key().public_bytes_raw().hex()
    otra = k.sign(b"sdmer:2027:tx:a:b:100:1").hex()
    assert not firmas.verify(pub, cuentas.tx_message("a", "b", 100, 1), otra)

def test_firma_vieja_sin_chain_id_no_vale():
    k = Ed25519PrivateKey.generate(); pub = k.public_key().public_bytes_raw().hex()
    vieja = k.sign(b"tx:a:b:100:1").hex()
    assert not firmas.verify(pub, cuentas.tx_message("a", "b", 100, 1), vieja)
```
- [ ] **Step 2:** `pytest -q pilar2/tests -k chain` → FAIL.
- [ ] **Step 3:** Implementar `domain()` y aplicarlo a todos los mensajes. El 400 de firma inválida pasa a decir `"firma inválida (formato v2 con chain id: actualizá la página o tools/cuenta.py)"` (Review Focus 1).
- [ ] **Step 4:** Tests en verde. A mano en el compose: crear una cuenta en `/cuenta`, mandar una tx, aceptar una compartida y dar de alta un worker en `/unirse`. Todo tiene que andar.
- [ ] **Step 5:** `contrato-1.md`: punto g implementado (formato exacto del mensaje). Commit: `A3-g: chain id (EIP-155) en todos los mensajes firmados`.

### Task 3: A3-h, cero mineros CPU a los 60 s

**Files:**
- Modify: `pilar3/k3s-local/keda-scaledobject.yaml` (`cooldownPeriod`)
- Modify: `pilar3/README.md` (tiempo medido)

El pool ya pide 0 a los 60 s sin trabajo (`pool/decision.py`). KEDA suma su propio `cooldownPeriod` (120 s), y eso da los ~180 s que midió el profe. La decisión de cuándo bajar queda en un solo lugar, el pool.

- [ ] **Step 1:** `cooldownPeriod: 5` (un `pollingInterval`), con un comentario que explique que los 60 s los decide el pool.
- [ ] **Step 2: Verificación** en el k3s: mandar 1 tx, esperar a que se selle y medir desde la última tarea hasta que `kubectl -n tpi-miners get deploy miner-cpu` muestre 0. Tiene que dar **≤ 70 s**. Anotar la medición (comandos y hora) en `pilar3/README.md`.
- [ ] **Step 3:** Commit: `A3-h: KEDA baja a 0 mineros CPU a los ~60 s`.

### Task 4: C3, montos Infinity/NaN dan 400 y el bulk no queda a medias

**Files:**
- Modify: `pilar2/services/coordinator/ledger.py` (`to_cents`)
- Modify: `pilar2/services/coordinator/main.py` (`post_bulk`, ~línea 186)
- Test: `pilar2/tests/test_unit.py`

- [ ] **Step 1: Tests que fallan**

```python
@pytest.mark.parametrize("v", [float("inf"), float("-inf"), float("nan"), "Infinity", "NaN", "sNaN", "1e999999"])
def test_to_cents_no_finito(v):
    with pytest.raises(ledger.InvalidTx):
        ledger.to_cents(v)
```
Integración: un bulk `[{ok}, {"amount": "NaN", ...}, {ok}]` → 202 con `accepted == 2`, `rejected == 1`, y el `results[1]["error"]` dice "monto inválido".
- [ ] **Step 2:** FAIL (`OverflowError` / `InvalidOperation`).
- [ ] **Step 3:** En `to_cents`, `if not d.is_finite(): raise InvalidTx("monto inválido")` antes de multiplicar, y atrapar `InvalidOperation` también en la comparación. En `post_bulk`, cada `_submit` va en `try/except Exception` y devuelve `(400, {"error": "error interno con esta tx"})` (con log) para no cortar el resto.
- [ ] **Step 4:** Verde. **Step 5:** Commit `C3: montos no finitos -> 400 y bulk sin cortes`.

### Task 5: C1, no perder un nonce ganador de antes de un rearmado

**Files:**
- Modify: `pilar2/services/coordinator/blocks.py` (`_handle`, líneas ~336-345)
- Test: `pilar2/tests/test_unit.py` (o `test_sistema.py`, según dónde estén los tests de `_handle`)

Un rearmado mantiene índice y base y solo acorta el prefijo, así que un nonce que cumplía el prefijo viejo (más largo) cumple también el nuevo.

**Interfaces:**
- Produces: `blocks.is_rearm_of(res_block_id: str, cur: dict) -> bool` = `res_block_id != cur["block_id"] and res_block_id.startswith(f"{cur['index']}-")`.

- [ ] **Step 1: Tests que fallan:** `test_encontrado_de_block_id_rearmado_sella` (resultado `found` con block_id `"<i>-viejo"`, nonce válido para `cur["base"]` y `cur["prefix"]` → status `"sealed"` y la cadena crece 1). `test_dos_replicas_mismo_ganador_un_solo_sellado` (Review Focus 4): `_handle` dos veces con el mismo res, en paralelo con `asyncio.gather` → exactamente un `"sealed"`, el otro `"stale"`, y la altura crece 1.
- [ ] **Step 2:** FAIL (hoy devuelve `"stale"`).
- [ ] **Step 3:** En `_handle`, si `is_rearm_of(...)` y `res["found"]`, seguir por el camino normal verificando contra `cur` (mismo `base`, `cur["prefix"]`). El minero que encontró cobra igual.
- [ ] **Step 4:** Verde. **Step 5:** Commit `C1: un ganador del block_id anterior al rearmado sella igual`.

### Task 6: C2, rechazar y aceptar una compartida, atómico en Lua

**Files:**
- Modify: `pilar2/services/coordinator/multisig.py` (`REJECT_LUA` nuevo, revisar `ACCEPT_LUA`)
- Modify: `pilar2/services/coordinator/main.py` (`reject_multisig`, líneas ~408-416, registrar el script en el lifespan)
- Test: `pilar2/tests/test_lua.py` (nuevo; ver Task 8 para el fixture)

**Interfaces:**
- Produces: `multisig.REJECT_LUA`. KEYS: `status_key, invites_index(o)…`; ARGV: `account`. Devuelve `"rechazada"`, o el estado actual si ya no estaba `pendiente`. `ACCEPT_LUA` tiene que devolver `"rechazada"` si el estado ya es ese, sin tocar `accepted_key`.

- [ ] **Step 1: Test que falla:** `test_aceptar_y_rechazar_a_la_vez` lanza 50 veces en paralelo el último accept contra un reject. En cada corrida el estado final es `activa` **o** `rechazada`, nunca `rechazada` con todos los accepted ni `activa` después de devolver 200 al reject.
- [ ] **Step 2–4:** FAIL → implementar → verde. La respuesta HTTP 409 se decide según lo que devuelve el Lua.
- [ ] **Step 5:** Commit `C2: aceptar/rechazar una compartida en un solo Lua`.

### Task 7: C5 y C6, SIGTERM del minero CPU y colectas que no se frenan entre sí

**Files:**
- Modify: `pilar2/services/miners/agent.py` (handler de señal, loop principal ~línea 249-266)
- Modify: `pilar2/services/gateway/main.py` (`release` acepta `?reason=shutdown`)
- Modify: `pilar3/k3s-local/miner-cpu.yaml` (`terminationGracePeriodSeconds: 20`)
- Modify: `pilar2/services/coordinator/main.py` (liquidación de colectas, ~línea 520)
- Test: `pilar2/tests/test_unit.py`

**Interfaces:**
- Produces: `POST /tasks/{id}/release?reason=shutdown`: devuelve la tarea a la cola **sin** `count_expired` (apagarse no es una falla). Cualquier otro `reason`, o ninguno, se comporta como hoy.
- Agente: `Agent.stop(signum, frame) -> None` pone `self.stopping = True`. El loop termina el tramo actual, llama a `release(..., reason=shutdown)` para cada lease abierto y sale con código 0.

- [ ] **Step 1: Tests que fallan:** `test_agente_libera_leases_al_sigterm` (gateway falso, `agent.stop(signal.SIGTERM, None)` → se llamó `POST /tasks/<id>/release?reason=shutdown`). `test_release_shutdown_no_cuenta_falla` (gateway con Redis de test: `miner_expired` no aumenta). `test_colecta_sin_saldo_no_frena_las_demas` (dos colectas cerradas, la primera sin saldo → la segunda queda liquidada).
- [ ] **Step 2–4:** FAIL → implementar (`return` → `continue` y `try/except` por colecta con log y métrica `crowdfund_settle_errors_total`) → verde.
- [ ] **Step 5: Verificación en el k3s:** `kubectl -n tpi-miners scale deploy miner-cpu --replicas=0` mientras mina. En los logs del gateway aparece `tasks_released_total` con reason shutdown, y el bloque se sella sin esperar los 60 s del lease.
- [ ] **Step 6:** Commit `C5+C6: SIGTERM libera leases y una colecta trabada no frena a las demás`.

### Task 8: C7, tests de los Lua y del reaper (con Redis real en el CI)

**Files:**
- Create: `pilar2/tests/test_lua.py`
- Modify: `pilar2/tests/conftest.py` (fixture `redis_db`)
- Modify: `.github/workflows/ci.yml` (`services: redis: image: redis:7.4-alpine`, `env: REDIS_URL`)

**Interfaces:**
- Produces (hecho en el PR #2): fixture `redis_db`, que devuelve **una función** que crea un `redis.asyncio.Redis` (cada test lo crea adentro de su `asyncio.run`, porque el cliente queda atado al event loop). Usa la base 15 con `flushdb` antes y después, y se saltea sin `REDIS_URL` o si la URL no es de la base 15. También `conftest.import_gateway()`, para importar el gateway junto al coordinator. Ejemplos en `pilar2/tests/test_lua.py`. Lo usan las Tasks 6, 7, 10, 12 y 14.

- [ ] **Step 1: Tests:**
  - `test_reserva_no_deja_saldo_negativo`: 100 reservas en paralelo de 10 sobre un saldo de 500 → exactamente 50 aceptadas y `pending_out == 500`.
  - `test_reserva_seq_repetido_rechazado`.
  - `test_sign_lua_un_dueno_una_firma` y `test_sign_lua_dos_firmas_simultaneas_una_admision`.
  - `test_accept_lua_ultimo_activa`.
  - `test_cf_start_liquida_una_sola_vez` (dos llamadas → una devuelve 1).
  - `test_reaper_republica_vencidos`: lease con deadline en el pasado → `reaper_loop` en una iteración lo devuelve a la cola (exchange falso) y borra el lease.
- [ ] **Step 2:** `REDIS_URL=redis://localhost:6379/15 pytest -q pilar2/tests/test_lua.py` → todos los tests pasan contra el código actual (son tests de caracterización), salvo los que cubren C2, que entran con la Task 6.
- [ ] **Step 3:** CI en verde con el servicio Redis. **Step 4:** Commit `C7: tests de los Lua y del reaper con Redis real`.

---

## Fase 2: Cloudflare, verificación de mineros, contrato y endurecimiento

### Task 9: A2, de DuckDNS/sslip.io a Cloudflare

**Manual (alguien del grupo, el 28/11):** comprar `DOMINIO`, crear la cuenta de Cloudflare (Free), agregar el sitio, cambiar los nameservers en el registrador y esperar a que quede "Active". Crear un **API token** con permiso solo `Zone:DNS:Edit` sobre esa zona y cargarlo con `kubectl -n cert-manager create secret generic cloudflare-api-token --from-literal=api-token=…`. **Nunca** commitearlo.

**Files:**
- Modify: `pilar3/k8s/https/sdmer.yaml` → renombrar a `pilar3/k8s/https/dominio.yaml` (hosts `DOMINIO` y `gateway.DOMINIO`)
- Delete: los hosts sslip.io de `pilar3/k8s/https/ingress.yaml` (o borrar el archivo si queda vacío)
- Create: `pilar3/k8s/https/cluster-issuer.yaml` (`ClusterIssuer letsencrypt-dns` con `dns01.cloudflare.apiTokenSecretRef`)
- Modify: `pilar3/k8s/https/traefik-values.yaml` (`service.spec.loadBalancerSourceRanges` = rangos IPv4 de https://www.cloudflare.com/ips-v4, y redirect global `web → websecure`)
- Modify: el Service `tpi-gateway` con IP propia (`http://34.63.209.225`) pasa a `ClusterIP`
- Modify: `pilar3/k3s-local/config.yaml`, `keda-scaledobject.yaml`, `pilar2/services/miners/gpu/inferhive.yaml`, `pilar3/k8s/apps/config.yaml`, `pilar3/loadtest/completa/correr.sh` → `https://gateway.DOMINIO` y `https://DOMINIO`
- Modify: `pilar2/services/notifier/main.py` (orígenes permitidos) y los tests de `origin_ok` que usan sslip
- Docs: `README.md`, `pilar3/README.md`, `pilar2/README.md` (todas las URLs). Corregir lo que dice `e38619c` con un párrafo en `pilar3/README.md` ("el 21/11 escribimos que Cloudflare no hacía falta; el profe lo pidió en la revisión, por esto…"). La historia no se reescribe.

En Cloudflare: registro A `@` y `gateway`, **proxied**, a la IP de Traefik. SSL/TLS **Full (strict)**, "Always Use HTTPS" activado y WebSockets activados (vienen así por defecto).

- [ ] **Step 1:** `grep -rn "sslip\|duckdns\|34.63.209.225\|35.223.237.60" --include=*.{yaml,py,sh,md,html} .` → anotar la lista de archivos a cambiar.
- [ ] **Step 2:** Aplicar los manifiestos (explicar antes cada `kubectl apply`). `kubectl -n tpi get certificate` → `READY=True`.
- [ ] **Step 3: Verificación (la del profe):**
  - `curl -sI http://DOMINIO/` → `301` a `https://DOMINIO/`; ídem `http://gateway.DOMINIO/`.
  - `curl -sI https://DOMINIO/status` → `200` y `server: cloudflare`.
  - `curl -s --max-time 5 https://35-223-237-60.sslip.io/status` → falla (timeout o conexión rechazada).
  - `curl -s --max-time 5 http://34.63.209.225/` → falla.
  - Mineros del k3s, KEDA (`kubectl -n tpi-miners get scaledobject` → READY) y el GPU minando por `gateway.DOMINIO`.
  - **Review Focus 2:** `bench.py` con el bulk más grande de `matrix.sh` a través de Cloudflare. Si da 524 o 413, documentar el límite y la alternativa elegida.
- [ ] **Step 4:** Commit `A2: dominio propio detrás de Cloudflare, cerrado el acceso por IP y sslip.io`.

### Task 10: B1.1, B1.3 y B1.4: canarios indistinguibles, leases atados a su dueño y límite de altas

**Files:**
- Modify: `pilar2/services/gateway/main.py` (`canary_task`, `finish_canary`, `renew`, `release`, `post_result`, `register_worker`)
- Modify: `pilar2/services/coordinator/main.py` (`register_account`)
- Create: `pilar2/services/tpi_common/ratelimit.py`
- Modify: `pilar2/services/miners/agent.py` (manda `miner_id` en renew/release)
- Test: `pilar2/tests/test_unit.py`, `pilar2/tests/test_lua.py`

**Diseño del canario:** hoy usa la base de un bloque **sellado** (su nonce sale de `GET /blocks/{i}`) y no tiene `share_prefix`. El nuevo canario es un **rango ya resuelto del bloque actual**: mismo `block_id`, misma `base`, mismo `prefix` y mismo `share_prefix` que cualquier tarea. Las shares de ese rango ya las verificó el coordinator (`block:<i>:sharenonces`, que hay que guardar con su rango). El minero no puede saber que el rango se repite, y las shares no son públicas. Pasa el canario si devuelve **todas** las shares conocidas de ese rango (hasta `MAX_SHARES`).

**Interfaces:**
- Produces: `keys.block_verified_ranges(index) -> str` (lista Redis de JSON `{"from","to","share_prefix","shares":[...]}`, que escribe el coordinator en `_add_shares` cuando una tarea **no** era canario y tenía ≥ 1 share válida; se recorta a 200 y expira con el bloque).
- `gateway.canary_task(cur) -> dict | None`: toma uno al azar de `block_verified_ranges(cur["index"])`. Si no hay, None (todavía no hay canario posible en este bloque).
- `gateway.lease_owner_ok(lease: dict, ident: dict | None, miner_id: str | None) -> bool`: con token propio, `ident["miner_id"] == lease["miner_id"]`; con token compartido, `miner_id == lease["miner_id"]`.
- `/results`: `from`, `to`, `block_id` y `share_prefix` salen **del lease**, no del body. Sin lease válido, o con dueño distinto → **409**.
- `ratelimit.allow(r, key: str, limit: int, window_s: int) -> bool` (INCR + EXPIRE en Lua). `/accounts/register`: 5 por IP por hora (`X-Forwarded-For` / `CF-Connecting-IP` que agrega Cloudflare). `/workers/register`: 10 por cuenta (ya existe) y 20 por IP por hora.

- [ ] **Step 1: Tests que fallan:** `test_canario_mismo_formato_que_tarea` (mismas claves y tipos, `share_prefix` presente, `base == cur["base"]`). `test_canario_pasa_con_todas_las_shares` y `test_canario_falla_si_falta_una`. `test_results_de_otro_dueno_409`. `test_results_ignora_from_to_del_body` (el body dice `from=0,to=10`, el lease `100..199` → se publica con `100..199`). `test_renew_y_release_de_otro_dueno_409`. `test_ratelimit_corta_en_el_sexto`.
- [ ] **Step 2–4:** FAIL → implementar → verde. Agregar al agente `&miner_id=` en renew/release.
- [ ] **Step 5:** Commit `B1: canarios indistinguibles, leases con dueño y límite de altas`.

### Task 11: B1.2 + A3-d: hashrate medido con shares y Vardiff

**Files:**
- Create: `pilar2/services/tpi_common/vardiff.py`
- Modify: `pilar2/services/gateway/main.py` (`lend`, `record_result`, `finish_calibration`, `classify`)
- Modify: `pilar2/services/coordinator/blocks.py` (el `share_prefix` publicado es el "por defecto"; el gateway lo puede cambiar)
- Test: `pilar2/tests/test_unit.py`

**Interfaces:**
- Produces: `vardiff.measured_hashrate(entries: list[dict]) -> float`. Cada entry es `{"share_len": int, "valid_shares": int, "seconds": float}` y el resultado es `Σ valid_shares·16^share_len / Σ seconds`. Usa solo shares **verificadas por el gateway** con `pow.valid_shares`, y los segundos de punta a punta que mide el gateway, no los que informa el motor.
- `vardiff.share_len(hashrate: float, block_len: int, target_seconds_per_share: float = 2.0) -> int` = `clamp(floor(log16(max(hashrate,1) · target)), 1, block_len - 1)`.
- `record_result` guarda `measured_hashrate` en `keys.miner_measured(miner)` (TTL 600). `classify()` usa ese valor. La calibración pasa a ser solo el valor inicial hasta tener 3 resultados medidos.
- `lend()`: si hay hashrate medido, `task["share_prefix"] = "0" * vardiff.share_len(...)`. El lease guarda ese `share_prefix`, que es el que usa `/results` (Task 10). El peso `16^largo` ya compensa, así que el premio sigue siendo justo.

- [ ] **Step 1: Tests:** `test_measured_hashrate_de_shares` (2 entries conocidas → valor exacto). `test_share_len_limites` (`hashrate=1` → 1; `1e12` con `block_len=5` → 4). `test_vardiff_minero_rapido_prefijo_mas_largo` (el lease de un minero rápido trae un `share_prefix` más largo que el de uno lento). `test_clase_no_cambia_por_keepalive_mentiroso` (keepalive con `hashrate=1e12` → la clase sigue saliendo de lo medido).
- [ ] **Step 2–4:** FAIL → implementar → verde.
- [ ] **Step 5:** Explorador y Grafana: mostrar "medido (shares)" en vez de "calibrado/declarado". `contrato-1.md` punto d. Commit `B1.2 + A3-d: hashrate medido con shares y Vardiff por worker`.

### Task 12: A3-e + C4: PPLNS y un premio que nunca desaparece

**Files:**
- Modify: `pilar2/services/coordinator/reward.py`
- Modify: `pilar2/services/coordinator/blocks.py` (`SHARES_LUA` agrega a la ventana; `_rewards`; sellado con `carry`)
- Modify: `pilar2/services/tpi_common/keys.py`
- Test: `pilar2/tests/test_unit.py`, `pilar2/tests/test_lua.py`

**Interfaces:**
- Produces: `keys.PPLNS_WINDOW = "pplns:window"` (lista Redis de `"<cuenta>|<peso>"`, LPUSH en `SHARES_LUA` y LTRIM a `PPLNS_MAX_ENTRIES` = 100.000). `keys.REWARD_CARRY = "reward:carry"` (centavos sin pagar).
- `reward.pplns_split(entries: list[tuple[str, int]], window_weight: int, reward_cents: int, finder: str | None) -> dict[str, int]`: recorre de la más nueva a la más vieja hasta juntar `window_weight` (la última entry se cuenta parcial) y reparte con `split_reward`.
- `PPLNS_N` (env, default `2`): la ventana es `N ×` el trabajo esperado del bloque (`16^len(prefix)`). Usar el prefijo del bloque que se paga.
- Si `pplns_split` devuelve `{}`, el `reward_cents` entero suma a `REWARD_CARRY`. El próximo bloque con reparto no vacío paga `reward_cents + carry` y pone el carry en 0, en el mismo Lua del sellado (C4).

- [ ] **Step 1: Tests:**

```python
def test_pplns_cuenta_shares_de_bloques_anteriores():
    entries = [("a", 16), ("b", 16), ("a", 16)]  # la más nueva primero
    assert reward.pplns_split(entries, 32, 100, "a") == {"a": 50, "b": 50}

def test_pplns_ultima_entry_parcial():
    assert reward.pplns_split([("a", 16), ("b", 32)], 32, 100, "a") == {"a": 50, "b": 50}

def test_pplns_ventana_vacia_devuelve_vacio():
    assert reward.pplns_split([], 32, 100, None) == {}
```
`test_lua`: `test_bloque_sin_shares_acumula_carry` (Review Focus 5: bloque 1 sin shares → carry = premio; bloque 2 con shares → las tx de premio suman premio₁ + premio₂, y el carry queda en 0).
- [ ] **Step 2–4:** FAIL → implementar → verde. `split_reward` y `reward_transactions` no cambian.
- [ ] **Step 5:** `contrato-1.md` punto e y `decisiones.docx` ("PROP → PPLNS: premia al que mina de forma constante y desalienta el pool hopping; N=2"). Commit `A3-e + C4: PPLNS y premio acumulado si un bloque no tiene shares`.

### Task 13: A3-a + A3-b: notifier JSON-RPC 2.0 y front sin polling

**Files:**
- Modify: `pilar2/services/notifier/main.py` (`handle`, `parse_subscription`, formato de las notificaciones)
- Modify: `pilar2/services/coordinator/static/index.html` (~línea 389), `cuenta.html` (~líneas 361 y 438)
- Test: `pilar2/tests/test_unit.py`

**Interfaces (protocolo nuevo):**
- Pedido: `{"jsonrpc":"2.0","id":1,"method":"sdmer_subscribe","params":["newBlocks"]}`, también con `["tx","<tx_id>"]` y `["account","<cuenta>"]`. Respuesta: `{"jsonrpc":"2.0","id":1,"result":"0x<16 hex>"}`.
- Aviso: `{"jsonrpc":"2.0","method":"sdmer_subscription","params":{"subscription":"0x…","result":{…}}}`.
- `sdmer_unsubscribe` con `params:["0x…"]` → `result: true|false`.
- Errores: `-32700` JSON inválido, `-32600` pedido inválido, `-32601` método inexistente, `-32602` parámetros inválidos.
- Front: **ningún `setInterval` que pida datos.** `refresh()` corre al recibir un aviso de `newBlocks`. Las compartidas y colectas se recargan cuando llega un aviso de `account` de una cuenta propia o de `newBlocks`. Sin WS (`onclose`), un solo fallback de 15 s que se apaga al reconectar.

- [ ] **Step 1: Tests:** `test_jsonrpc_subscribe_newBlocks`, `test_jsonrpc_metodo_inexistente_32601`, `test_jsonrpc_parametros_invalidos_32602`, `test_jsonrpc_json_invalido_32700`, `test_jsonrpc_notificacion_formato` (el `route()` arma el `params.subscription` correcto).
- [ ] **Step 2–4:** FAIL → implementar → verde.
- [ ] **Step 5:** `grep -n "setInterval" pilar2/services/coordinator/static/*.html` → solo queda el fallback con guardia `!wsOpen`. En el navegador (DevTools → Network): con el WS conectado, ningún XHR periódico durante 1 minuto.
- [ ] **Step 6:** Commit `A3-a+b: notifier JSON-RPC 2.0 y front sin polling`.

### Task 14: A3-c + A3-f: el gateway empuja el trabajo y `authorize` con desafío firmado

> **Hecho en el PR #7, con un cambio:** la clave del worker no se genera en `/unirse`. Se ata en su primer `authorize` (TOFU), así sirve también para los pods del k3s. La entrega, `renew` y `release` siguen por HTTP (`POST /results` firmado). El detalle está en el PR y en `pilar2/README.md`.

La más grande. Si el **01/12 a la noche** no está en verde, se deja el HTTP pull y se corrige el texto de c y f en el contrato (con el motivo). Todo lo demás del plan no depende de esta tarea.

**Files:**
- Modify: `pilar2/services/gateway/main.py` (`/ws/miners` pasa de "solo avisa" a Stratum-like)
- Modify: `pilar2/services/gateway/main.py` `register_worker` (el alta incluye la clave pública del worker)
- Modify: `pilar2/services/miners/agent.py` (cliente WS que recibe tareas; queda HTTP como fallback)
- Modify: `pilar2/services/coordinator/static/unirse.html` (genera la clave del worker y la descarga como `<worker>.key`)
- Modify: `pilar3/k3s-local/miner-cpu.yaml` (cada pod genera su clave al arrancar)
- Test: `pilar2/tests/test_unit.py`, `pilar2/tests/test_lua.py`

**Interfaces (mensajes JSON-RPC 2.0 sobre `/ws/miners`):**
- Al conectar, el gateway manda `{"method":"mining.challenge","params":{"challenge":"<32 hex>"}}`.
- Worker → `mining.authorize` con `params: {"worker": str, "pubkey": hex, "signature": hex}`. La firma es sobre `firmas.domain(f"authorize:{worker}:{challenge}")`. La `pubkey` tiene que coincidir con la registrada para ese worker: alta pública = `workerkey:<worker>` que firmó la cuenta (`worker:<cuenta>:<worker>:<pubkey>`); pods del k3s = registrada con el `MINER_TOKEN` compartido en `POST /workers/register-pod`. Responde `result: true` o un error `-32001` y cierra.
- Gateway → `mining.notify`, `params` = la tarea completa (lo mismo que hoy devuelve `GET /tasks/next`, con `share_prefix` de Vardiff). Se manda cuando el minero tiene lugar (`MAX_LEASES_PER_MINER`) y hay tarea; el lease se crea con `lend()` igual que hoy.
- Worker → `mining.submit` con `params: {result…, "signature": hex}`, firmado sobre `firmas.domain(f"result:{task_id}:{found}:{nonce or ''}:{sha256(json(shares))}")`. Pasa por la misma lógica de `post_result` (refactor: `async def accept_result(res, lease, miner_id) -> int`, que usan el HTTP y el WS).
- Worker → `mining.renew` y `mining.release` (los mismos efectos que los endpoints HTTP).
- WS cerrado: no se hace nada especial. Los leases vencen y el reaper los re-publica (Review Focus 3).
- HTTP `/tasks/next` y `/results` siguen andando (minero GPU de InferHive y fallback), pero `/results` también exige la firma si el worker tiene `pubkey`.

- [ ] **Step 1: Tests:** `test_authorize_firma_valida_ok`, `test_authorize_desafio_reusado_falla` (cada conexión trae un desafío nuevo), `test_authorize_clave_distinta_falla`, `test_notify_respeta_max_leases`, `test_submit_sin_firma_rechazado`, `test_ws_cerrado_lease_vence_y_reaper_republica` (con `redis_db`), `test_mismo_resultado_por_ws_y_http_un_solo_pago`.
- [ ] **Step 2–4:** FAIL → implementar → verde.
- [ ] **Step 5: Verificación en el k3s:** escalar a 4 pods; en `/miners`, los 4 conectados por WS; minar 3 bloques; `tasks_leased_total` sube sin `GET /tasks/next` en el log del gateway. Matar un pod a mitad: el bloque se sella igual.
- [ ] **Step 6:** `contrato-1.md` puntos c y f. Commit `A3-c+f: trabajo empujado por WebSocket y authorize con desafío firmado`.

### Task 15: B2, endurecer Kubernetes

**Files:**
- Modify: `pilar3/k8s/apps/*.yaml`, `pilar3/k8s/infra/*.yaml`, `pilar3/k3s-local/miner-cpu.yaml`
- Create: `pilar3/k8s/apps/serviceaccounts.yaml`, `pilar3/k8s/infra/networkpolicies.yaml`, `pilar3/k8s/https/privado.yaml`
- Modify: `pilar2/services/Dockerfile` (`USER 10001`)
- Test: `pilar3/tests/test_manifiestos.py` (nuevo)

- [ ] **Step 1: Test que falla** (parsea los YAML con `yaml.safe_load_all`):
  - `test_todo_contenedor_tiene_security_context`: `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `capabilities.drop == ["ALL"]` y `readOnlyRootFilesystem: true`, **salvo** la lista explícita `EXCEPCIONES = {"rabbitmq", "redis"}`, que montan su PVC y escriben en él (se justifica en el README).
  - `test_todo_contenedor_tiene_limite_de_cpu`.
  - `test_cada_deployment_tiene_su_serviceaccount` (y `automountServiceAccountToken: false`, salvo el coordinator si usa la API).
  - `test_networkpolicy_redis_solo_apps` y `test_networkpolicy_rabbit_solo_apps`.
- [ ] **Step 2–4:** FAIL → implementar → verde. Donde la app escribe en disco (logs rotativos), montar un `emptyDir` en `/var/log/tpi` y en `/tmp`. Las NetworkPolicies dejan entrar a Redis solo desde `app in (coordinator, pool, gateway, notifier)` y desde el Prometheus de `monitoring`; lo mismo para RabbitMQ (más la comunicación entre sus 3 nodos).
- [ ] **Step 5 (B2.6):** `privado.yaml` = Middleware `ipAllowList` de Traefik para `PathPrefix(/logs) || PathPrefix(/metrics)`, con prioridad más alta, que solo deja pasar los rangos internos del cluster. Verificación: `curl -s -o /dev/null -w "%{http_code}" https://DOMINIO/logs` → `403`, y lo mismo con `/metrics`. Prometheus sigue scrapeando (Grafana con datos), porque scrapea por el Service, no por el ingress. Para leer `/logs`: `kubectl -n tpi port-forward`.
- [ ] **Step 6 (B2.5, opcional):** TLS para AMQP y Redis con un `Issuer` CA de cert-manager. Si al **01/12** no está, se documenta en `pilar3/README.md` → "Limitaciones": el tráfico va dentro de la VPC, el riesgo y cómo se haría.
- [ ] **Step 7 (B2.7):** sección "Relojes" en `pilar3/README.md`. Los nodos de GKE sincronizan con el NTP de Google (`metadata.google.internal`, chrony, "leap smear"). Las ventanas las calcula **solo el líder del coordinator**, con su propio reloj, así que un desfase entre réplicas mueve la ventana a lo sumo lo que dura un cambio de líder. Los mineros del k3s no usan la hora. Agregar `chronyc tracking` de un nodo como evidencia.
- [ ] **Step 8:** Aplicar en el cluster (explicando cada `kubectl apply`) y verificar que todos los pods vuelven a `Running` y que se sella un bloque. Commit `B2: securityContext, límites de CPU, ServiceAccounts, NetworkPolicies y /logs y /metrics privados`.

### Task 16: B3, que los pipelines desplieguen lo que corre

**Files:**
- Modify: `README.md` (primera línea), `pilar3/README.md`
- Modify: `pilar3/infra/terraform/provider.tf` (backend `gcs` sin `bucket`; el bucket va por `-backend-config`)
- Modify: `.github/workflows/1-infra.yml` (`tofu init -backend-config="bucket=${{ secrets.TF_STATE_BUCKET }}"`)
- Modify: `.github/workflows/3-apps.yml` y/o `pilar3/k8s/apps/kustomization.yaml` (`images:` con el nombre que publica el pipeline)
- Create: `pilar3/k8s/platform/traefik-values.yaml` (mover), `cert-manager-values.yaml`, `observability-values.yaml` (sacados con `helm get values <release> -n <ns>`, sin secretos)
- Test: `pilar3/tests/test_manifiestos.py` → `test_imagenes_coinciden_con_pipeline`

- [ ] **Step 1:** Test que falla: los `image:` de `kustomize build pilar3/k8s/apps` (o los `images:` del kustomization) coinciden con los `IMAGE`/tags que arma `3-apps.yml`.
- [ ] **Step 2–3:** FAIL → corregir → verde.
- [ ] **Step 4:** Primera línea del README: "El cluster en vivo se armó a mano (tofu + kubectl desde la terminal) porque [motivo real: los créditos, el cluster que venía del TP3 y la falta de tiempo para probar el pipeline contra un cluster con datos]. Los pipelines están y hacen lo mismo; ver Pilar 3".
- [ ] **Step 5:** Crear el bucket una vez a mano (`gcloud storage buckets create gs://…-tfstate --uniform-bucket-level-access`, explicado antes de correrlo) y migrar con `tofu init -migrate-state`. **No** commitear el estado.
- [ ] **Step 6:** Sección "VMs externas": no hay pipeline porque los mineros no pueden correr en GCP (prueba gratuita) y se reemplazan por el k3s local con `pilar3/k3s-local` + el workflow `4-miners.yml`.
- [ ] **Step 7:** Commit `B3: backend GCS, imágenes alineadas y values de los componentes instalados a mano`.

### Task 17: B4, métricas que faltan del checklist

**Files:**
- Modify: `pilar2/services/coordinator/blocks.py` (`RESULTS`, `BLOCK_SECONDS`, histograma nuevo; `published_at` en la tarea)
- Modify: `pilar2/services/gateway/main.py` (histograma nuevo en `lend`)
- Modify: `pilar3/k8s/observability/dashboard-tpi.yaml`, `alertas.yaml` (las consultas que usan `results_total` y `block_seconds`)
- Test: `pilar2/tests/test_unit.py`

**Interfaces:**
- `task_queue_to_lease_seconds` (Histogram, gateway, label `kind`) = `time.time() - task["published_at"]` en `lend()`. `published_at` lo pone el coordinator al publicar.
- `block_validation_seconds` (Histogram, coordinator) = tiempo de `pow.verify` + `_add_shares` + el Lua de sellado en el camino `found`.
- `results_total{status, kind}`: `kind` sale de `message.routing_key` (`result.cpu|gpu`).
- `block_seconds{prefix_len}`.

- [ ] **Step 1: Tests:** `test_results_total_tiene_label_kind` (después de `_handle` con routing key `result.gpu`, `REGISTRY.get_sample_value("results_total", {"status": "empty", "kind": "gpu"}) == 1`). Lo mismo para `block_seconds_count{prefix_len="4"}`.
- [ ] **Step 2–4:** FAIL → implementar → verde.
- [ ] **Step 5:** Paneles nuevos en Grafana ("RabbitMQ → worker p50/p95", "Validación del bloque", "Éxito CPU vs GPU", "Minado por prefijo") y captura en `pilar3/README.md`. Commit `B4: latencia cola→worker, validación, éxito por tipo y minado por prefijo`.

---

## Fase 3: medición y documentación (03/12, sobre el sistema congelado)

### Task 18: A6, M contra 2×M mineros y throughput confirmado

**Files:**
- Create: `pilar3/loadtest/confirmado.py` (no toca `bench.py`)
- Create: `pilar3/loadtest/resultados/2026-12-03-escalado/` (CSV, PNG, `README.md`)
- Create: `pilar3/loadtest/escalado.sh` (llama a `matrix.sh` con M=4 y M=8, 3 corridas cada uno)

**Interfaces:**
- `confirmado.py --api URL --desde <ts> --hasta <ts>` lee `GET /blocks` y devuelve `{"tx_confirmadas": int, "primera_tx_ts": float, "ultimo_sello_ts": float, "throughput_confirmado_tx_s": float}` contando solo las tx de usuario (sin `sender="recompensa"`) que estaban en bloques sellados en ese intervalo.

- [ ] **Step 1:** Test de `confirmado.py` con una cadena falsa (3 bloques, 2 en la ventana) → valores exactos.
- [ ] **Step 2:** Correr en el k3s con la dificultad fija (`DIFFICULTY_MODE=fixed`) la **misma carga** con `kubectl scale … --replicas=4` y `=8` (KEDA en pausa: `autoscaling.keda.sh/paused-replicas`), 3 corridas por punto.
- [ ] **Step 3:** Gráfico comparativo (ingesta y confirmado, mediana y min–max) y análisis en el README de resultados y en el informe: speedup real contra el ideal 2×, y por qué no da 2× (cuello en la ventana de 10 s, un bloque a la vez, el sellado secuencial). Aclarar que `throughput_tx_per_s` de `bench.py` mide **ingesta** (202 aceptados), no confirmación.
- [ ] **Step 4:** Commit `A6: prueba M vs 2M mineros CPU y throughput confirmado`.

### Task 19: C8 + C9: diagrama de Pilar 3 e informe con análisis crítico

**Files:**
- Modify: `pilar3/README.md` (diagrama de infraestructura en Mermaid: Cloudflare → Traefik → servicios → Redis/RabbitMQ, k3s + KEDA, GPU, Prometheus/Grafana/Loki)
- Modify: `docs/decisiones.docx` y todo lo que apunte a `docs/arquitectura.html` (`grep -rn arquitectura.html`): crear el archivo o cambiar la referencia al diagrama que exista
- Create: `docs/informe.md` (o `.pdf`; lo decide el grupo)
- Modify: `AGENTS.md` y `README.md`: entrega **04/12/2026**

Estructura del informe (cada sección la escribe y la firma quien la hizo):
1. Qué funciona: con evidencia (tests, mediciones, capturas).
2. Qué no funciona o quedó a medias: con el motivo.
3. Decisiones y alternativas descartadas: génesis, PPLNS, Vardiff, Cloudflare, un bloque a la vez, Redis + Lua contra una base relacional.
4. Limitaciones: MD5 como PoW, un solo líder, ventanas de reloj, TLS interno, mineros solo locales.
5. Dónde se aplicaría en un contexto real (pools reales, ledger interno de pagos, sistemas de cómputo voluntario tipo BOINC) y qué habría que cambiar.
6. Resultados de Pilar 1 (CPU vs GPU) y de la carga (Task 18).

- [ ] **Step 1:** `grep -rn "arquitectura.html" .` → 0 referencias rotas.
- [ ] **Step 1b (A3-i):** en `docs/contrato-1.md`, corregir el punto i (RabbitMQ con 3 nodos y colas quorum; el límite de entregas lo hace el reaper del gateway, por qué no `x-delivery-limit`). Cerrar el apartado "Cambios respecto de la v1" con una tabla a–i: implementado o corregido, la fecha y el PR.
- [ ] **Step 2:** Revisión cruzada: cada integrante lee una sección que no escribió y la corrige en un PR.
- [ ] **Step 3:** Commit `C8+C9: diagrama de Pilar 3, referencias arregladas e informe`.

### Task 20: A5, declaración de uso de IA por integrante

**Files:** Modify: `README.md` (sección "Uso de IA")

- [ ] **Step 1:** Una subsección **por integrante** con qué herramientas usó (Claude Code con Opus 5.5 y otras), para qué partes (con los commits o PRs), **cómo validó** lo generado (tests, `make check` 17/17, revisión del PR, mediciones) y qué corrigió a mano. Incluir que este plan se armó con Claude Code a partir del PDF del profe.
- [ ] **Step 2:** Commit `A5: declaración de uso de IA por integrante`.

### Task 21: A4, video nuevo con los cuatro

No es código. Guion propuesto (~20 min), cada parte la presenta quien la hizo:
1. Pilar 1: kernel CUDA, MD5 de varios bloques, `make check` 17/17 y CPU vs GPU.
2. Flujo de un bloque de punta a punta: tx firmada → ventana → tareas → WS/`mining.notify` → shares → sellado → PPLNS.
3. Contrato: los puntos a–i en vivo (notifier JSON-RPC, compartidas, colectas).
4. Fallas: matar un pod que mina, GPU zombie, canario fallado, caída del líder.
5. Escalado: KEDA 0 → 8 → 0 en ~60 s y entrada y salida de la GPU.
6. Pruebas de carga: gráfico M vs 2M y el confirmado.

- [ ] **Step 1:** Subirlo (YouTube no listado o Drive) y poner el link en la primera sección del `README.md` raíz.
- [ ] **Step 2:** Commit `A4: link al video de la entrega`.

---

## Antes de entregar (03/12 a la noche)

- [ ] `pytest -q pilar2/tests pilar3/tests` en verde, CI en verde y gitleaks limpio.
- [ ] `make test && make check-oracle && make check` en `pilar1` (17/17).
- [ ] Las verificaciones del PDF, una por una: A1 (`user-3` → cuenta propia = 400), A2 (`http→https` y sin acceso por IP ni sslip), A3 (contrato = sistema), A4 (link), A5 (sección), A6 (CSV + gráfico), B2.6 (`/logs` y `/metrics` = 403).
- [ ] Todo lo que quedó sin hacer de B está documentado en `pilar3/README.md` → "Limitaciones", con el motivo (lo pide el PDF).
