const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell, WidthType,
  ShadingType, HeadingLevel, LevelFormat, AlignmentType, BorderStyle,
} = require("docx");

const FONT = "Calibri";
const W = 9638; // A4 con márgenes de 2 cm

// Texto con `código` en monoespaciado y **negrita**
function runs(text, opts = {}) {
  const out = [];
  const re = /(`[^`]+`|\*\*[^*]+\*\*)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(new TextRun({ text: text.slice(last, m.index), ...opts }));
    const t = m[0];
    if (t.startsWith("`")) out.push(new TextRun({ text: t.slice(1, -1), font: "Consolas", ...opts, size: opts.size ? opts.size - 2 : 19 }));
    else out.push(new TextRun({ text: t.slice(2, -2), ...opts, bold: true }));
    last = m.index + t.length;
  }
  if (last < text.length) out.push(new TextRun({ text: text.slice(last), ...opts }));
  return out;
}

const p = (t) => new Paragraph({ children: runs(t), spacing: { after: 120 } });
const h1 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun(t)] });
const bullet = (t) => new Paragraph({ numbering: { reference: "bul", level: 0 }, children: runs(t), spacing: { after: 80 } });
const check = (t) => new Paragraph({ numbering: { reference: "chk", level: 0 }, children: runs(t), spacing: { after: 80 } });

const border = { style: BorderStyle.SINGLE, size: 4, color: "BFC5CC" };
const borders = { top: border, bottom: border, left: border, right: border };

function table(headers, rows, fr) {
  const total = fr.reduce((a, b) => a + b, 0);
  const widths = fr.map((f) => Math.floor((W * f) / total));
  widths[widths.length - 1] += W - widths.reduce((a, b) => a + b, 0);
  const cell = (text, i, head) => new TableCell({
    width: { size: widths[i], type: WidthType.DXA },
    borders,
    margins: { top: 60, bottom: 60, left: 100, right: 100 },
    shading: head ? { type: ShadingType.CLEAR, color: "auto", fill: "1F3A5F" } : undefined,
    children: [new Paragraph({ children: runs(text, { size: 18, color: head ? "FFFFFF" : undefined, bold: head || undefined }) })],
  });
  return new Table({
    width: { size: W, type: WidthType.DXA },
    columnWidths: widths,
    rows: [
      new TableRow({ tableHeader: true, children: headers.map((h, i) => cell(h, i, true)) }),
      ...rows.map((r) => new TableRow({ children: r.map((c, i) => cell(c, i, false)) })),
    ],
  });
}
const gap = () => new Paragraph({ spacing: { after: 120 }, children: [] });

const children = [
  new Paragraph({ heading: HeadingLevel.TITLE, children: [new TextRun("Decisiones de arquitectura")] }),
  new Paragraph({ children: [new TextRun({ text: "TPI SDyPP 2026 · Blockchain distribuida y CUDA · Borrador", color: "5B636E" })], spacing: { after: 240 } }),
  p("El diagrama está en `docs/arquitectura.html`. Este documento registra qué fija el TPI, qué decidimos nosotros y por qué."),

  h1("1. Qué fija el TPI y qué decidimos nosotros"),
  table(["Lo fija el enunciado o la guía (no negociable)", "Lo decidimos nosotros (se justifica en el README)"], [
    ["Minero CUDA en C/C++ con el contrato CLI del kit", "Lenguaje de los servicios (Python), formato de las transacciones"],
    ["RabbitMQ con colas y tópicos, Redis con persistencia", "Nombres de colas, tipo (quorum o classic), TTL, DLQ, leases"],
    ["Coordinador (NCT), pool (P5), keep-alive de mineros", "Cómo se coordinan 2 réplicas del coordinador y del pool"],
    ["2 réplicas por servicio, GKE con OpenTofu, 2 node pools", "Tamaño de las máquinas, límites del HPA, fragmentación por defecto"],
    ["Ningún minero en GCP, entrada por un gateway HTTP", "Cómo vuelve una tarea a la cola si el minero desaparece"],
    ["CI/CD con gitleaks, OIDC, endpoint público de status, logs", "Stack de métricas, dashboards, formato de logs"],
  ], [1, 1]),

  h1("2. RabbitMQ"),
  table(["Elemento", "Tipo", "Durable", "Configuración", "Por qué"], [
    ["`blockchain`", "exchange topic", "sí", "—", "Un solo punto de entrada. La routing key decide si es cola de trabajo o broadcast (esto es lo \"híbrido\")"],
    ["`blockchain.dlx`", "exchange fanout", "sí", "—", "Destino de las tareas que fallan"],
    ["`mining.tasks`", "quorum", "sí", "binding `task.#`, mensajes persistentes", "Si RabbitMQ reinicia no se pierden tareas. Quorum es el tipo replicado y recomendado en RabbitMQ 4"],
    ["`mining.results`", "quorum", "sí", "binding `result.#`, consumidores con `prefetch_count=1` y ack manual después de verificar", "Un resultado no se pierde aunque el coordinador muera a mitad de la verificación"],
    ["`block.events.<pod>`", "classic, exclusive, auto-delete", "no", "binding `block.*`", "Es un broadcast: cada pod necesita su propia copia y la cola muere con el pod"],
    ["`mining.dlq`", "quorum", "sí", "—", "Tareas que vencieron 3 leases. Se inspeccionan y se miden"],
  ], [2.2, 1.6, 0.9, 2.6, 3.2]),
  gap(),
  bullet("**Los mineros no hablan AMQP.** Hablan HTTP con el gateway. El gateway hace `basic_get` y ack inmediato, y guarda la tarea en Redis como un préstamo (`lease:<task_id>`, con duración `TASK_LEASE_SECONDS`). Un reaper dentro del gateway (con lock para que corra en una sola réplica) re-publica los leases vencidos y aumenta `attempts`. Con 3 intentos, la tarea va a la DLQ. Así se evita dejar un canal abierto por cada tarea prestada."),
  bullet("**Las tareas viejas** se descartan por `block_id`. Al resolverse un bloque, el coordinador además purga `mining.tasks`. Se trabaja un solo bloque a la vez, porque la cadena es secuencial."),
  bullet("**Con un solo nodo de RabbitMQ,** quorum no da alta disponibilidad (necesita 3 nodos). La mantenemos por durabilidad y para dejar el camino abierto. Si nos alcanza el crédito, se pasa a 3 réplicas. Hay que decirlo en el informe."),
  bullet("**Contrato de mensajes:** JSON con `\"v\": 1`. El payload es canónico: `json.dumps(sort_keys=True, separators=(\",\", \":\"))`."),
  gap(),
  p("**Quién usa cada cola y qué viaja:**"),
  table(["Cola", "Publica", "Consume", "Patrón", "Mensaje"], [
    ["`mining.tasks`", "coordinator líder (`task.cpu` / `task.gpu`)", "gateway, en nombre del minero que pide `/tasks/next`", "competencia: cada tarea la toma uno solo", "`{v, task_id, block_id, base, prefix, from, to}` (from/to = rango de nonces)"],
    ["`mining.results`", "gateway, al recibir `POST /results` (`result.*`)", "réplicas del coordinator", "competencia: una réplica verifica cada resultado", "`{v, task_id, block_id, worker, from, to, found, nonce, hash, hashes, seconds}`"],
    ["`block.events.<pod>`", "coordinator, al sellar (`block.solved`)", "cada pod de gateway, pool y coordinator (su propia cola)", "broadcast: todos reciben copia", "`{v, block_id, index, hash}`"],
    ["`mining.dlq`", "reaper del gateway, vía `blockchain.dlx`", "nadie automático: log, métrica y revisión manual", "basurero", "la tarea original + `attempts`"],
  ], [1.8, 2.1, 2.1, 1.8, 2.8]),
  gap(),
  p("**Lo híbrido:** los tres patrones (competencia en tareas, competencia en resultados y broadcast en eventos) conviven en el mismo exchange `blockchain`. La diferencia la hace qué cola está atada a qué patrón de routing key."),
  p("**Lo que no va por RabbitMQ:**"),
  table(["Qué", "Dónde", "Por qué no es una cola"], [
    ["Transacciones pendientes", "Redis `tx:pending`", "Hay que contarlas, moverlas de forma atómica a `tx:inflight` y calcular saldos. Una cola solo deja sacar mensajes de a uno"],
    ["Keep-alive de mineros", "Redis `worker:<id>` con TTL", "Interesa quién está vivo ahora, no un historial de latidos. El TTL lo resuelve solo"],
    ["Señal de escalado y plan del pool", "Redis `pool:scaling`, `pool:plan`", "Es un valor actual que se pisa, no un evento"],
  ], [2.2, 2.2, 5.2]),
  gap(),
  p("**Evaluada y descartada: una cola tx.incoming para las transacciones.** A favor: absorbe ráfagas. En contra: el cliente recibiría `202` sin saber si la transacción es válida. Validar en el API (un Lua de menos de un milisegundo) permite responder rápido **y** rechazar con `400` en el momento. Las guías lo encaran igual: la del Pilar 2 deja las pendientes en `tx:pending`, la del Pilar 3 pide `POST /transactions` en el coordinador, y el `bench.py` del starter manda HTTP directo y mide la confirmación consultando `/status`. Ninguna pone las transacciones en RabbitMQ."),

  h1("3. Redis (AOF everysec, StatefulSet + PVC)"),
  table(["Clave", "Tipo", "Contenido"], [
    ["`block:<index>`", "HASH", "`index`, `previous_hash`, `nonce`, `timestamp`, `transactions`, `block_hash`, `difficulty`, `miner`. Se guarda por posición, no por hash: un hash repetido nunca puede pisar un bloque"],
    ["`hash:<hash>`", "STRING", "índice hash → index, escrito con `SET NX`. Si el hash ya existe, se rechaza ese nonce y el bloque sigue abierto"],
    ["`chain`", "LIST", "hashes en orden"],
    ["`chain:tip`", "STRING", "último hash confirmado"],
    ["`tx:pending`", "LIST", "transacciones esperando bloque"],
    ["`tx:inflight:<block>`", "LIST", "transacciones del bloque que se está minando (movidas de forma atómica desde `tx:pending`). Si el coordinador se cae, el nuevo líder retoma el bloque"],
    ["`tx:seen`", "SET", "`tx_id` de todas las transacciones aceptadas. Rechaza duplicados y replays"],
    ["`balance:<user>`", "STRING (entero, centavos)", "saldo confirmado; caché que se reconstruye desde la cadena"],
    ["`pending_out:<user>`", "STRING (entero, centavos)", "suma de lo que el usuario tiene en transacciones pendientes (disponible = balance − pending_out)"],
    ["`worker:<id>`", "STRING con TTL 15 s", "keep-alive (`type`, `hashrate`)"],
    ["`lease:<task_id>`", "STRING con TTL", "tarea prestada a un minero"],
    ["`block:<index>:next_from`", "STRING (entero)", "primer nonce todavía no publicado del bloque que se está minando: desde ahí arranca la tanda siguiente de rangos (§7)"],
    ["`coord:leader`, `pool:leader`", "STRING NX EX", "elección de líder"],
    ["`gateway:reaper:lock`", "STRING NX EX", "lock del reaper: una sola réplica del gateway re-publica los leases vencidos en cada pasada"],
    ["`pool:plan`", "STRING con TTL", "prefijo efectivo y fragmentación que decidió el pool"],
    ["`pool:scaling`", "STRING con TTL", "señal para KEDA"],
  ], [2.4, 1.8, 5]),
  gap(),
  p("**El bloque se cierra con un script Lua** (comparar `chain:tip` y escribir de forma atómica). Esto resuelve tres casos a la vez: dos coordinadores que cierran el mismo bloque, resultados duplicados y resultados que llegan tarde."),

  h1("4. Entrada de datos"),
  bullet("`POST /transactions` recibe `{sender, receiver, amount}` (opcional `tx_id`) y `POST /transactions/bulk` recibe una lista (para la prueba de 100.000). Los nombres `sender` y `receiver` son los que usa el `bench.py` del starter, que no deja configurar los campos."),
  bullet("**Respuestas:** `202 Accepted` + `tx_id` si es válida (queda pendiente); `400` si no lo es (formato, monto, saldo, duplicado). Validez se sabe al instante; la confirmación se consulta después en `GET /status` (`pending_transactions`) o en `GET /transactions/<tx_id>`. `bench.py` cuenta como éxito cualquier 2xx."),
  bullet("**Validación** (un script Lua atómico): campos y tipos, `amount > 0`, `tx_id` no repetido y `amount <= balance − pending_out`. Si pasa, reserva el monto (`pending_out += amount`) y hace `RPUSH tx:pending` en la misma operación."),
  bullet("**Montos:** llegan como decimales (`bench.py` manda por ejemplo 37.52) y se guardan como enteros en centavos, para no acumular errores de redondeo en los saldos."),
  bullet("**tx_id:** si el cliente lo manda, se usa para detectar reintentos duplicados; si no (como `bench.py`), lo genera el servidor (UUID). Se registra en `tx:seen`."),
  bullet("**Usuarios:** el bloque génesis les da saldo inicial a `user-0` … `user-96`, los 97 usuarios que usa `bench.py`. Sin eso, la prueba de carga rechazaría casi todo por falta de fondos y no mediría nada. El saldo inicial tiene que alcanzar para el bulk más grande (100.000 tx de hasta 100 cada una)."),
  bullet("**Se permite sender == receiver.** Bitcoin y Ethereum lo permiten (vuelto a uno mismo, consolidar fondos, cancelar una transacción trabada). No mueve saldo, así que no rompe nada. Rechazarlo no protege contra el relleno de bloques, porque se puede hacer lo mismo entre dos cuentas propias. Además, `bench.py` (de la cátedra) genera alrededor de 1% de transacciones así (con i ≡ 48 mod 97), que contarían como fallas en las mediciones."),
  bullet("**Armado del bloque** (decidido el 6/11). Se mina un bloque a la vez: lo que llega durante el minado queda en `tx:pending` para el bloque siguiente. Si no se está minando nada, se arma un bloque con lo que ocurra primero entre `MAX_TX_PER_BLOCK` transacciones o `BLOCK_TIMEOUT` segundos desde la primera pendiente (con al menos 1). Apenas se sella un bloque, si hay pendientes, se arma el siguiente enseguida, sin volver a esperar el reloj: esas transacciones ya esperaron mientras se minaba."),
  p("**Ejemplo:** t=0 s llega una tx; t=3 s llega otra; t=10 s vence el timer y se arma el bloque 5 con 2 tx; mientras se mina llegan 4 más (t=5, 7, 9 y 11 s), que quedan pendientes; t=12 s se sella el bloque 5 y se arma enseguida el 6 con esas 4 (no se espera hasta t=15 s). Con un bulk de 100.000, los bloques se llenan al instante y se arman de a 1000 sin esperar el timer."),

  h1("5. Métricas y logs"),
  table(["Qué", "Cómo"], [
    ["Métricas de los servicios", "`prometheus_client`, `/metrics` en cada servicio: `blocks_total`, `block_time_seconds` (histograma), `tx_pending`, `tasks_published_total`, `results_total{status}` (valid, invalid, stale, dup), `miners_alive{type}`, `hashrate{miner}`, `difficulty`"],
    ["Broker y base de datos", "plugin `rabbitmq_prometheus` (largo de colas) y `redis_exporter`"],
    ["Recolección", "Prometheus + Grafana en el pool apps (versión liviana, no el kube-prometheus-stack completo, por CPU y crédito)"],
    ["Pruebas de carga", "`bench.py` y `matrix.sh` del starter, que escriben `results.csv`; `plot.py` genera los gráficos del informe"],
    ["Minero GPU", "`measure_energy.py` del kit y el JSON de salida del binario"],
    ["Logs", "JSON por stdout (Cloud Logging), `RotatingFileHandler` a disco y un buffer circular en memoria expuesto en `/logs` (el enunciado pide memoria y disco)"],
    ["Status público", "`GET /status` en el coordinador: `{\"coordinator\":\"up\",\"rabbitmq\":\"up\",\"redis\":\"up\",\"gateway\":\"up\",\"pool\":\"up\",\"miners_gpu\":1,\"miners_cpu\":0}`"],
  ], [2, 6]),

  h1("6. Servicios"),
  table(["Servicio", "Dónde", "Tecnología"], [
    ["coordinator (API + NCT)", "GKE apps", "Python (FastAPI)"],
    ["pool (P5)", "GKE apps", "Python"],
    ["gateway", "GKE apps", "Python (FastAPI)"],
    ["RabbitMQ, Redis", "GKE infra", "imágenes oficiales"],
    ["Prometheus, Grafana", "GKE apps", "imágenes oficiales"],
    ["miner-cpu", "k3s local, con KEDA", "Python multiproceso"],
    ["miner-gpu", "PC / Colab", "wrapper Python + binario CUDA"],
    ["CI/CD", "GitHub Actions", "gitleaks, pytest, nvcc, tofu, kubectl"],
    ["Infraestructura", "GCP", "GKE, Artifact Registry, bucket GCS para el estado, Workload Identity Federation"],
  ], [2.5, 2, 4]),

  h1("7. Flujo: quién hace qué"),
  p("El diagrama de secuencia completo está en `docs/arquitectura.html` (sección 3)."),
  bullet("**El pool decide y el coordinador publica.** Cada 15 s el pool líder calcula el prefijo efectivo y la fragmentación según los mineros vivos y el largo de `mining.tasks`, y los deja en `pool:plan`. El coordinador líder es el único que publica tareas."),
  bullet("**Los rangos se publican por tandas** (decidido el 6/11). Con prefijos largos lo normal es que la primera tanda no tenga el nonce: con 8 ceros hex cada nonce sirve con probabilidad 1 en 16^8 (unos 4.300 millones), y 20 rangos de 50 M (1.000 M nonces) lo contienen solo un 21 % de las veces. Por eso el coordinador publica la tanda siguiente a partir de `block:<i>:next_from`, sin esperar a que vuelvan todas: cuando en `mining.tasks` quedan menos tareas que mineros vivos. El espacio de nonces (uint64) no se agota, así que el bloque siempre termina cerrándose. Se descartaron cambiar el timestamp del bloque (cambia la base a mitad del minado) y bajar el prefijo (la dificultad dependería de la suerte)."),
  bullet("**La DLQ no se reprocesa sola** (decidido el 6/11). Una tarea que agotó sus 3 intentos queda en `mining.dlq` y nadie la vuelve a tomar. La DLQ corta el loop de una tarea que rompe a cualquier minero que la toma, guarda la evidencia para mirarla a mano y avisa con una métrica (`tasks_dlq_total`) y un log. El bloque no se traba: como los rangos se publican por tandas, otro minero encuentra un nonce válido en otro rango, y solo se pierde ese pedazo, que no hace falta."),
  bullet("**La dificultad se guarda en cada bloque** (`difficulty`). Sin GPUs el pool puede bajar el prefijo. Si el bloque no guarda con qué dificultad se minó, la validación de la cadena falla después."),
  bullet("**Colaboración, no competencia.** Los workers buscan en rangos disjuntos: nadie repite nonces. No es una elección libre: el P5 del enunciado obliga al pool a subdividir la tarea en rangos de búsqueda del nonce. La guía del Pilar 2 acepta las dos formas, pero los rangos ya implican colaboración."),
  bullet("**El minero abandona entre tramos.** El wrapper corre `miner_gpu` por tramos dentro de su rango y entre tramo y tramo pregunta si la tarea sigue vigente (`410` = abandonar). El binario CUDA no puede enterarse a mitad de una ejecución."),
  bullet("**/status** consulta a RabbitMQ, Redis, el gateway y el pool. **Prometheus** scrapea `/metrics` de los servicios y de los exporters."),

  h1("8. Workers: qué hacen y cómo se sabe si están vivos"),
  p("**Qué hace un worker:** pide un rango, busca un nonce que cumpla el prefijo y reporta si lo encontró o no. No ve usuarios, saldos ni transacciones (solo el digest) y no escribe en la cadena. Bloques y transacciones son del coordinador."),
  table(["Aspecto", "Decisión", "Por qué"], [
    ["Latido", "El worker hace `POST /keepalive` cada **5 s** con `{miner_id, type: cpu|gpu, hashrate}`", "Un período 3 veces menor que el TTL tolera perder un latido sin dar al worker por muerto"],
    ["Dónde se guarda", "El gateway escribe `SET worker:<id> <json> EX 15` en Redis", "Redis no está expuesto: el worker no le puede escribir directo"],
    ["Cómo se detecta un worker muerto", "Nadie lo detecta activamente: si deja de latir, la clave **expira sola** a los 15 s", "No hace falta un proceso que \"barra\" workers muertos"],
    ["Quién los cuenta", "El pool líder, cada 15 s, con `SCAN worker:*`, separando por `type`", "Con esos números decide el prefijo efectivo y `desired_cpu_miners`"],
    ["Timeout para GPU", "`GPU_KEEPALIVE_TIMEOUT_SECONDS = 15` (ConfigMap)", "Es lo que mide el experimento de entrada y salida de GPUs del Pilar 3"],
    ["Qué pasa con su trabajo", "Su `lease:<task>` vence y el reaper del gateway re-publica el rango (3 intentos y va a la DLQ)", "El rango no se pierde ni queda trabado"],
    ["Hashrate reportado", "Se usa para métricas y para el tamaño de rango, pero no se confía ciegamente: se contrasta con `hashes / segundos` de sus resultados", "Pregunta de defensa: \"el coordinador confía en el hashrate que le reportan\""],
  ], [2, 3.6, 3.6]),

  h1("9. Bloque de la blockchain: tamaño y largo de la cadena"),
  table(["Parámetro", "Propuesta", "Por qué"], [
    ["Largo de la cadena", "Sin límite. Crece de a un bloque desde un génesis fijo (`index 0`, `previous_hash` = 32 ceros)", "Es un log append-only: lo que se limita es cada bloque, no la cadena. Incluso con 100.000 transacciones entra en Redis sin problema"],
    ["Transacciones por bloque", "`MAX_TX_PER_BLOCK` en el ConfigMap. Arranque: 1000. Valor final: el que salga de medir 10, 100, 1000 y 5000 (throughput y latencia)", "El TP no lo fija. Agrupar abarata el PoW, que se paga por bloque: 100.000 tx a 1 por bloque son 100.000 bloques; a 1000 por bloque, 100. Tener un máximo evita bloques gigantes (DoS por saturación de memoria y mensajes)"],
    ["Disparo del bloque", "Se llena el bloque o pasan `BLOCK_TIMEOUT = 10 s` desde la primera pendiente, con al menos 1 transacción. Al sellar un bloque, si hay pendientes, el siguiente se arma enseguida (ver §4)", "Una transacción suelta no espera para siempre, y no se deja a los mineros ociosos esperando el reloj"],
    ["Qué se hashea", "`base = previous_hash + tx_digest`, donde `tx_digest` es el SHA-256 de las transacciones en JSON canónico (64 hex)", "La base mide siempre 96 bytes y, con el nonce (hasta 20 dígitos), entra en 2 bloques MD5 (máximo 119 bytes). Se usa SHA-256 y no MD5 porque MD5 tiene colisiones conocidas y el digest protege contenido; se calcula una vez por bloque, así que el costo es despreciable. Si se hashean las transacciones completas, cada intento cuesta más cuanto más grande es el bloque (1000 tx ≈ 100 KB ≈ 1.600 bloques MD5 por intento). Es la misma idea que el Merkle root de Bitcoin. El contrato del binario no cambia: `data = tx_digest`"],
    ["Validación", "`verify_chain` recorre toda la cadena: enlace `previous_hash`, `tx_digest`, hash y prefijo con la `difficulty` de cada bloque", "Detecta alteraciones y bloques perdidos al reiniciar Redis"],
  ], [2, 3.2, 4.4]),

  h1("10. Parámetros del minero CUDA (se eligen midiendo)"),
  p("GPU: GTX 1050 Ti, Pascal `sm_61`, 6 SMs, 768 núcleos CUDA, 4 GB. Ningún valor se fija a ojo: cada uno sale de una medición del informe."),
  table(["Parámetro", "Qué probamos", "Qué decide"], [
    ["Threads por bloque CUDA", "64, 128, 256, 512 (arranque: 256)", "Ocupación del SM. Siempre múltiplo de 32 (un warp)"],
    ["Bloques por grid", "1, 2, 8 y 32 × SMs (6, 12, 48, 192)", "Que haya suficientes warps para ocultar la latencia de memoria"],
    ["K (cada cuánto se mira la bandera)", "1, 64, 4096 y sin sondeo", "Overhead de leer la bandera contra trabajo tirado después de que alguien encontró la solución"],
    ["Nonces por lanzamiento de kernel", "según el throughput medido", "Cuánto tarda el host en enterarse de que hubo solución"],
    ["Tramo del wrapper (`--chunk`)", "medir; la cátedra midió 0,18 s solo en crear el proceso y el contexto CUDA", "Cada cuánto se entera el minero de que el bloque se cerró. Entra en la propuesta de tramo para el Pilar 2"],
    ["Rango por tarea (fragmentación)", "1 %, 5 %, 10 %, 25 %, 50 % (Pilar 3)", "Overhead de mensajes contra desbalance entre mineros"],
  ], [2.4, 3, 4.2]),

  h1("11. KEDA"),
  p("**El TPI no pide KEDA por nombre.** El enunciado pide levantar y destruir mineros CPU cuando no hay GPU, y menciona HPA, VMs on-demand o Cloud Run. La guía del Pilar 3 lo baja al k3s local y acepta cualquiera de tres caminos: KEDA (viene en el starter), un autoscaler propio (`local_autoscaler.py`) o un HPA con métrica externa."),
  p("**Decisión:** KEDA con el scaler `metrics-api` sobre `GET /scaling`. Ya viene configurado, puede escalar a cero y es lo que se usa en producción. Plan B: `local_autoscaler.py`, si instalar KEDA con Helm en el k3s da problemas. No se usan los dos a la vez sobre el mismo Deployment."),

  h1("12. Medición CPU vs GPU"),
  p("**Condiciones fijas, que se escriben en el informe:** GTX 1050 Ti (driver 581.80, CUDA 12.x), Intel i5-12400F (6 núcleos / 12 hilos), 16 GB, Windows 11. Mismas bases y mismos rangos en CPU y GPU, una corrida de calentamiento, 5 corridas por punto, se reporta la **mediana** y el comando exacto para reproducir."),
  table(["Experimento", "CPU", "GPU", "Qué se reporta"], [
    ["Corrección", "oráculo `miner_cpu.py`", "`miner_gpu`", "`check_contract.py` 17/17 en los dos"],
    ["Throughput", "oráculo (1 proceso), minero multiproceso (12 procesos con hashlib), `openssl speed md5` como techo", "`--prefix ffffffffffffffff`, al menos 30 s", "H/s y speedup contra la **mejor** CPU, no solo contra Python en un proceso"],
    ["Tiempo vs dificultad (Hit #6)", "d = 1 a 7 (más allá tarda horas)", "d = 1 a 8 o 9", "mediana y máximo contra 16^d intentos, en escala log"],
    ["Mensaje de 1 vs 2 bloques MD5", "—", "base corta vs base de 64 bytes", "H/s en cada caso. Justifica el `tx_digest`"],
    ["Forma del grid y K", "—", "ver sección 9", "H/s por configuración"],
    ["Energía", "estimada por TDP (65 W), aclarando que es una estimación", "`measure_energy.py` (sensor de la placa)", "hashes por joule y potencia media"],
    ["Overhead por proceso", "—", "`miner_gpu --hash abc`", "segundos fijos por invocación. Justifica el tamaño de tramo"],
    ["En el sistema (Pilar 3)", "1, 2, 4 y 8 mineros CPU en el k3s", "1 minero GPU", "tiempo por bloque, speedup y eficiencia"],
  ], [2.2, 2.6, 2.4, 2.6]),
  gap(),
  p("Un script `pilar1/bench/bench_cpu_gpu.py` corre la batería completa y escribe `results.csv`. Los gráficos se generan con matplotlib, así cada número del informe se puede volver a obtener con un solo comando."),

  h1("13. Saldos y doble gasto"),
  bullet("**Modelo de cuentas** (como Ethereum, no UTXO como Bitcoin). El saldo de un usuario es lo que recibió menos lo que envió, calculado desde la cadena. `balance:<user>` en Redis es una caché que se puede reconstruir recorriendo los bloques."),
  bullet("**Saldos iniciales** en el bloque génesis para `user-0` … `user-96` (los usuarios de `bench.py`)."),
  bullet("**Disponible = saldo confirmado − lo que el usuario ya tiene en transacciones pendientes.** Una transacción entra solo si `amount <= disponible`. Si no se resta lo pendiente, alguien con 100 puede mandar varias transacciones de 100 antes de que se mine ninguna (doble gasto)."),
  bullet("La verificación y la reserva se hacen juntas en un **script Lua** (atómico), así dos réplicas del coordinador no aprueban a la vez transacciones que no entran en el saldo."),

  h1("14. Ideas (no prioritarias)"),
  bullet("**Firmas Ed25519.** La dirección del usuario es su clave pública. El cliente firma `{sender, receiver, amount, seq}` con su clave privada y el coordinador verifica la firma. `seq` es un contador por cuenta que evita reenviar la misma transacción (replay). Sin firmas, cualquiera puede mandar una transacción a nombre de otro. Costo: unas 50 líneas con `cryptography`. El tiempo de verificación se puede medir como costo de la seguridad. El minero no cambia."),
  bullet("**Recompensa al minero** (como la coinbase de Bitcoin). Cada bloque incluye una transacción que le paga al worker ganador. Conecta con \"el primer minero es recompensado\" del enunciado."),

  h1("15. Pendiente de decidir"),
  p("Todos son **opcionales según las guías**: se deciden con las mediciones del Pilar 1, si sobra tiempo."),
  check("¿Separar `task.gpu` y `task.cpu` con rangos de distinto tamaño? La guía del Pilar 2 pone `mining.tasks.gpu` como \"(opcional)\"."),
  check("¿Minero GPU persistente (un contexto CUDA por proceso, no uno por tramo)? La guía del Pilar 1 lo llama \"mejora opcional\" y pide mantener también el modo CLI del contrato."),
  check("¿Frontend o explorador de bloques? No es obligatorio: el status \"no requiere GUI\" y la guía del Pilar 3 no lo lista. El \"frontend, backend, split, join\" del Pipeline 3 del enunciado parece copiado del TP3."),
];

const hstyle = (size, color) => ({ run: { font: FONT, size, bold: true, color }, paragraph: { spacing: { before: 300, after: 120 }, keepNext: true } });

const doc = new Document({
  styles: {
    default: { document: { run: { font: FONT, size: 21 } } },
    paragraphStyles: [
      { id: "Title", name: "Title", basedOn: "Normal", next: "Normal", run: { font: FONT, size: 44, bold: true, color: "1F3A5F" }, paragraph: { spacing: { before: 0, after: 60 }, keepNext: true } },
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true, ...hstyle(28, "1F3A5F") },
    ],
  },
  numbering: {
    config: [
      { reference: "bul", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 400, hanging: 260 } } } }] },
      { reference: "chk", levels: [{ level: 0, format: LevelFormat.BULLET, text: "☐", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 400, hanging: 300 } } } }] },
    ],
  },
  sections: [{ properties: { page: { margin: { top: 1134, bottom: 1134, left: 1134, right: 1134 } } }, children }],
});

Packer.toBuffer(doc).then((b) => { fs.writeFileSync(process.argv[2], b); console.log("ok"); });
