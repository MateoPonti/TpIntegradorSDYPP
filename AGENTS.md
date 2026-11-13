# Reglas del TP Integrador (para asistentes de IA)

TP Integrador de Sistemas Distribuidos y Programación Paralela (UNLu, 2C 2026):
blockchain distribuida con Proof of Work MD5 y minero CUDA. Entrega y defensa: **04/12/2026**.

Este archivo es para cualquier IA que use el grupo (Claude Code, Cursor, Copilot, Codex, ChatGPT).
Si una sugerencia genérica contradice algo de acá, **gana este archivo**.

Cada regla dice de dónde sale:
- **[Enunciado]**: `TP_Integrador.md` de la cátedra. No se negocia.
- **[Kit]**: kit del Pilar 1 (`Kit-TPI-Pilar1-Minero-CUDA.zip`). No se negocia, lo corrige la cátedra.
- **[Grupo]**: decisión del grupo. Está en `docs/decisiones.docx`. Se puede cambiar, pero lo decide el grupo, no la IA.

Lo que todavía no está decidido está en la sección "Pendiente" de `pilar2/README.md`. **La IA no decide esas dudas por su cuenta**: propone opciones y pregunta.

---

## 1. Lenguajes y estructura del repo

- El minero GPU va en **C/C++ con CUDA**. El resto, en Python, Node o Java. [Enunciado]
- Los servicios (coordinator, pool, gateway, minero CPU, wrapper del minero GPU) van en **Python**. [Grupo]
- Repo **público**, con una carpeta y un `README.md` por pilar: `pilar1/`, `pilar2/`, `pilar3/`. [Enunciado]
- Cada README tiene como mínimo: cómo ejecutarlo, diagrama de arquitectura y decisiones de diseño. [Enunciado]
- Todo se compila y se corre desde la terminal, sin abrir un IDE. [Enunciado]
- Hay que tener tests unitarios y de integración de lo crítico. [Enunciado]

## 2. Minero CUDA (Pilar 1)

### Contrato del binario [Kit]

```bash
miner_gpu --hash "abc"
# {"hash": "900150983cd24fb0d6963f7d28e17f72"}

miner_gpu --base "<previous_hash><data>" --difficulty 5 --from 0 --to 99999999
miner_gpu --base "<...>" --prefix 00ab --from 0 --to 99999999
# {"found": true, "nonce": 1234567, "hash": "00000...", "hashes": 1300000, "seconds": 0.01}
# {"found": false, "hashes": 100000000, "seconds": 0.05}
```

- La cadena que se hashea es `base + str(nonce)`: nonce en **decimal**, sin separadores, en **UTF-8**.
- `--difficulty d` es lo mismo que `--prefix` con `d` ceros. `--prefix` acepta cualquier hex.
- El rango `[from, to]` es **cerrado**. Si no hay solución: `"found": false`. **Nunca inventar un resultado.**
- Salida: **una sola línea JSON** por stdout y código de salida 0, encuentre o no.
- Errores de uso: mensaje por stderr y código de salida 2.
- No hace falta devolver el nonce más chico: sirve cualquier nonce del rango que cumpla el prefijo.
- No cambiar los nombres de los flags ni el formato del JSON: `check_contract.py`, `measure_energy.py`, `worker_adapter.py` y la corrección dependen de eso.

### Trampas que los tests detectan [Kit]

| Caso | Error que detecta |
|---|---|
| `big-nonces` | usar `int` o `uint32` para el nonce. **Los nonces son `uint64_t`.** |
| `long-base-d3` | un MD5 que procesa un solo bloque de 64 bytes. Tiene que procesar varios. |
| `utf8-base` | contar caracteres en vez de bytes (`ñ` y `·` ocupan 2 bytes) |
| `none-in-range` | inventar un resultado cuando no hay solución |
| `single-nonce-hit` | errores por uno en los bordes del rango |
| `d4-offset` | ignorar `--from` |

### Cómo se programa [Kit + Grupo]

- Hay que completar los 3 TODO de `gpu/miner_gpu.cu`:
  1. MD5 en el device, de varios bloques.
  2. Partición del espacio de nonces con grid-stride loop, en `uint64_t`.
  3. Corte temprano con `atomicCAS` sobre una bandera, que se lee como `volatile` cada K iteraciones.
- El MD5 se puede escribir desde el RFC 1321 o adaptar uno existente, **citando la fuente en el README**. [Kit]
- La base entra en `MAX_BASE` = 160 bytes. La nuestra es `previous_hash` (32 hex) + `tx_digest` (64 hex) = 96. [Kit + Grupo]
- La GPU del grupo es una **GTX 1050 Ti (Pascal, `sm_61`)**. Compilar con `make ARCH=sm_61`. Usar **CUDA 12.x**, no la 13, porque la 13 no soporta Pascal. [Grupo]
- Threads por bloque, bloques por grid, K y `--batch` **se eligen midiendo**, no se adivinan. Para arrancar: 256 threads y bloques = SMs × 8. [Grupo]
- Antes de dar algo por terminado: `make test`, `make check-oracle` y `make check` (17/17). [Kit]
- Hay que tener también un **minero CPU** con el mismo contrato, para comparar CPU contra GPU. El oráculo `oracle/miner_cpu.py` es la referencia. [Enunciado + Kit]

### Mediciones [Enunciado + Grupo]

- Prefijos de distinto largo (Hit #6), rango con y sin solución (Hit #7) y una batería de tests en CPU y GPU con comparativa.
- Se usan las mismas bases y los mismos rangos en CPU y GPU, con calentamiento, y se toma la **mediana de 5 corridas**.
- El speedup se calcula contra la **mejor** versión CPU (12 procesos), no contra un solo hilo.

## 3. Sistema distribuido (Pilar 2) [Grupo, salvo donde dice otra cosa]

Es un **pool de minería colaborativo**: todos minan el mismo bloque, cada uno en un rango de nonces distinto.

| Componente | Qué hace | Qué NO hace |
|---|---|---|
| **coordinator** | Valida las tx, arma el bloque, publica las tareas, verifica el resultado y sella. Un solo líder (`coord:leader`) arma y sella. | No mina. |
| **pool** | Cada 5 s escribe `pool:plan` (prefijo + tamaño de rango) y `pool:scaling` (cuántos mineros CPU). Un solo líder (`pool:leader`). | No toca la cadena. |
| **gateway** | Es la **única puerta HTTP** de los mineros. Presta las tareas con lease en Redis; un reaper re-publica las vencidas. | No decide nada del bloque. |
| **mineros** | Solo buscan el nonce en el rango que les dieron. | No ven saldos ni escriben la cadena. |

- **Ningún minero corre en GCP**, porque la prueba gratuita prohíbe minar. Los mineros CPU corren en un **k3s local** escalados por **KEDA**; el GPU corre en la PC con la 1050 Ti. En GKE corren solo coordinator, pool, gateway, RabbitMQ, Redis y la observabilidad.
- Los mineros hablan **solo con el gateway**, por HTTPS, con `Authorization: Bearer <MINER_TOKEN>` y en modo pull. RabbitMQ y Redis **no se exponen a Internet** (ClusterIP).
- **PoW:** `md5(base + str(nonce))` con ceros adelante. `base = previous_hash + tx_digest`, donde `tx_digest` = SHA-256 del JSON canónico de las transacciones. MD5 se usa solo para el PoW. La dificultad se guarda en cada bloque.
- **RabbitMQ** se usa solo para el minado; las transacciones **no** pasan por Rabbit.
  - Exchange topic `blockchain`.
  - Colas `mining.tasks`, `mining.results` y `block.events.<pod>`.
  - `blockchain.dlx` → `mining.dlq`. La DLQ **no se reprocesa sola**.
- **Redis** con AOF everysec y PVC. El sellado es atómico con un script Lua (si `tip == prev`).
- **Transacciones:** `POST /transactions {sender, receiver, amount}` (y `/bulk`), responde `202` + `tx_id`, o `400`. Hay que respetar el formato del `bench.py` de la cátedra. **No modificar** `bench.py`, `matrix.sh` ni `plot.py`.
- **Saldos:** modelo de cuentas. Disponible = `balance − pending_out`. La reserva es atómica en Lua. Los montos se guardan en **centavos enteros**, nunca en float.
- **Armado del bloque** (13/11, comentario del profe): **ventanas fijas de reloj** de 10 s (`BLOCK_WINDOW_SECONDS`). Cuando cierra la ventana de la primera tx pendiente, sus tx salen en bloques de hasta 1000 (`MAX_TX_PER_BLOCK`). **No se mezclan ventanas** y **no hay bloques vacíos**. Se mina **un bloque a la vez** (cada uno necesita el hash del anterior).
- **Rangos por tandas:** cuando en la cola quedan menos tareas que mineros vivos, se publica la tanda siguiente desde `block:<i>:next_from`. La cola es un colchón: **no** sirve para decidir cuántos mineros levantar.
- **Prefijo automático** (13/11): el pool elige el prefijo más largo con el que un bloque tarda < 10 s con el **hashrate real** (lo que entregan los mineros, no lo que declaran), con histéresis, entre 1 y 8. `DIFFICULTY_MODE=fixed` para las pruebas de carga. Si cae el hashrate a mitad de un bloque, el líder lo **rearma** con un prefijo más corto.
- **Tamaño de rango:** fragmentación `16^largo × POOL_FRAGMENT_PERCENT`, con un mínimo según si mina una GPU o solo CPU. Una sola cola (dos colas: se decide después de medir).
- **Mineros CPU:** todo o nada. Sin GPU y con trabajo, `CPU_MINERS_MAX`; 60 s sin trabajo, 0; con GPU, 0. La GPU no va en KEDA.
- **Gateway:** calibración al arrancar cada worker, canarios (1 de cada 20 tareas) y cuarentena (canario fallado o 3 préstamos vencidos seguidos). Nada de esto mina en GCP.
- **Premio proporcional** a los nonces completados, pagado con tx `sender="recompensa"` en el bloque siguiente.
- Al menos **2 réplicas por servicio**. [Enunciado]

## 4. Infraestructura y despliegue (Pilar 3)

- **GKE con OpenTofu**, con un node pool para la infraestructura (RabbitMQ, Redis) y otro para las aplicaciones. [Enunciado]
- CI/CD con **GitHub Actions**. [Enunciado]
- Un **endpoint público de status** que devuelva un JSON `{servicio: estado}`. [Enunciado]
- **Logs en memoria y en disco** (archivo rotativo + `GET /logs`). [Enunciado]
- Métricas con **Prometheus + Grafana**. Cada servicio expone `/metrics`. [Enunciado + Grupo]
- Pruebas de carga: bulks de 1 a 100.000 tx, prefijos de 1 a 8, fragmentación del 1 % al 50 %, y entrada y salida de GPUs. [Enunciado]
- El TPI corre en el **cluster GKE del TP3** (proyecto `tp3distribuidos`, cluster `tp3`), en el **namespace `tpi`**. **No tocar el namespace `tp3`** ni lo compartido (`monitoring`, node pools) sin que lo pida alguien del grupo. [Grupo]
- Prometheus (el del TP3, namespace `monitoring`) con disco de 10 GB y 30 días de retención para no perder las mediciones. [Grupo]

## 5. Seguridad [Enunciado]

- **Nunca commitear** `.env`, credenciales, tokens, tfvars ni el estado de tofu. El `.gitignore` va desde el primer commit.
- **Cero claves estáticas:** para entrar a GCP desde GitHub Actions se usa **OIDC / Workload Identity Federation**. Nada de JSON de service accounts.
- Los secretos van en GitHub Secrets o Secret Manager.
- **gitleaks** en el CI: si encuentra un secreto, el pipeline falla.
- La credencial del coordinador de la cátedra (`CADENA_TOKEN`) va **solo por variable de entorno**.
- Si se filtra un secreto, se revoca y se genera uno nuevo. Borrarlo del repo no alcanza: queda en el historial.

## 6. Cómo tiene que trabajar la IA

- Antes de proponer algo, leer `docs/decisiones.docx` y la sección "Pendiente" de `pilar2/README.md`.
- No reabrir decisiones ya tomadas sin avisar. Si algo parece mal, decirlo y explicar por qué, sin cambiarlo por su cuenta.
- No agregar componentes nuevos (otra cola, otra base, un frontend, etc.) sin preguntar.
- Explicar los comandos antes de correrlos, sobre todo los que tocan la nube (`tofu apply`, `kubectl delete`, `gcloud`).
- El enunciado pide **mencionar en el informe qué IA usaron y cómo ayudó**: anotar los usos importantes.
