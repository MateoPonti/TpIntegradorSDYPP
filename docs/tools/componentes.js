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
const h2 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun(t)] });
const bullet = (t) => new Paragraph({ numbering: { reference: "bul", level: 0 }, children: runs(t), spacing: { after: 80 } });
const num = (t) => new Paragraph({ numbering: { reference: "num", level: 0 }, children: runs(t), spacing: { after: 80 } });
const gap = () => new Paragraph({ spacing: { after: 120 }, children: [] });

// Bloque monoespaciado (diagramas de texto)
const mono = (lines) => lines.map((l, i) => new Paragraph({
  spacing: { after: 0, line: 240 },
  shading: { type: ShadingType.CLEAR, color: "auto", fill: "F3F5F8" },
  keepNext: i < lines.length - 1,
  children: [new TextRun({ text: l || " ", font: "Consolas", size: 16 })],
}));

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
      ...(headers ? [new TableRow({ tableHeader: true, children: headers.map((h, i) => cell(h, i, true)) })] : []),
      ...rows.map((r) => new TableRow({ children: r.map((c, i) => cell(c, i, false)) })),
    ],
  });
}

// Ficha de un componente: qué es, de qué se encarga, qué no hace, con quién habla, dónde corre
function ficha(nombre, filas) {
  return [h2(nombre), table(null, filas, [1.6, 6]), gap()];
}

const children = [
  new Paragraph({ heading: HeadingLevel.TITLE, children: [new TextRun("Componentes del sistema")] }),
  new Paragraph({ children: [new TextRun({ text: "TPI SDyPP 2026 · Blockchain distribuida y CUDA · Qué hace cada pieza", color: "5B636E" })], spacing: { after: 240 } }),
  p("Este documento acompaña a `docs/arquitectura.html`. El diagrama muestra todo junto y cuesta leerlo; acá cada pieza está por separado: qué es, de qué se encarga, qué **no** hace y con quién habla. Las decisiones y su justificación siguen en `decisiones.docx`."),

  h1("1. Cómo leer el diagrama"),
  p("El diagrama tiene **tres zonas**. La clave para entenderlo es saber en qué zona vive cada cosa, porque eso decide quién puede hablar con quién."),
  table(["Zona", "Qué hay", "Regla"], [
    ["**Google Cloud (GKE)**", "coordinator, pool, gateway, RabbitMQ, Redis, Prometheus, Grafana", "Coordina y guarda. **Nunca mina** (regla del TPI)."],
    ["**Fuera de Google Cloud**", "k3s local con los mineros CPU y KEDA; la PC con la GPU (minero CUDA)", "Mina. Solo entra al sistema por el gateway, por HTTP."],
    ["**Servicios externos**", "GitHub (código y CI/CD), Artifact Registry (imágenes), usuarios / `bench.py`", "Construyen, despliegan o mandan transacciones desde afuera."],
  ], [2, 4, 3.6]),
  gap(),
  p("Y adentro de GKE hay **dos node pools** (grupos de máquinas):"),
  bullet("**apps:** los servicios que escribimos nosotros (coordinator, pool, gateway) y la observabilidad. Escalan con la carga."),
  bullet("**infra:** RabbitMQ y Redis. Tienen un taint para que solo corran ahí, separados de las apps, y no se exponen a Internet."),
  gap(),
  p("**Vista simplificada** (sin observabilidad ni CI/CD):"),
  ...mono([
    "USUARIOS / bench.py",
    "       │ POST /transactions",
    "       ▼",
    "┌─────────────────────── GOOGLE CLOUD (GKE) ───────────────────────┐",
    "│                                                                  │",
    "│   COORDINATOR ◄──────────►  RABBITMQ  ◄──────────►  GATEWAY      │",
    "│        │        tareas y     (colas)    tareas y      ▲          │",
    "│        │       resultados              resultados     │          │",
    "│        ▼                                              │          │",
    "│      REDIS ◄── estado: cadena, saldos, mineros vivos  │          │",
    "│        ▲                                              │          │",
    "│      POOL (cuenta mineros, decide el plan)            │          │",
    "│                                                       │          │",
    "└───────────────────────────────────────────────────────┼──────────┘",
    "                                                        │ HTTP + token",
    "                 FUERA DE GOOGLE CLOUD                  │ (el minero pide)",
    "          ┌─────────────────────────────┬───────────────┴──────────┐",
    "          │ k3s local                   │ PC con GTX 1050 Ti       │",
    "          │ miner-cpu x0..N (KEDA)      │ wrapper + miner_gpu      │",
    "          └─────────────────────────────┴──────────────────────────┘",
  ]),
  gap(),
  p("La idea central: **los mineros no tienen acceso a nada salvo al gateway.** No ven RabbitMQ, ni Redis, ni saldos. Piden trabajo (pull), lo hacen y devuelven el resultado."),

  h1("2. Los servicios que escribimos nosotros"),

  ...ficha("Coordinator (API + NCT)", [
    ["**Qué es**", "El cerebro del sistema. Servicio Python (FastAPI) en GKE, 2 a 5 réplicas con HPA por CPU. NCT = las tareas que el enunciado le asigna al nodo coordinador."],
    ["**De qué se encarga**", "1) Recibir transacciones (`POST /transactions` y `/bulk`) y validarlas: formato, monto, saldo disponible, duplicados. 2) Armar el bloque candidato cuando hay `MAX_TX_PER_BLOCK` transacciones o pasan 10 s desde la primera pendiente; apenas se sella un bloque, si hay pendientes, arma el siguiente enseguida. 3) Partir el espacio de nonces en rangos y publicar una tarea por rango en RabbitMQ, por tandas: cuando quedan menos tareas que mineros vivos, publica la tanda siguiente. 4) Verificar los resultados que vuelven (recalcula un MD5). 5) Sellar el bloque en Redis con un script Lua y mover los saldos. 6) Avisar `block.solved` a todos. 7) Exponer `/status` (público) y `/metrics`."],
    ["**Qué NO hace**", "No mina. No habla con los mineros directamente (eso es del gateway). No decide cuántos rangos hacer ni de qué tamaño (eso lo decide el pool)."],
    ["**Con quién habla**", "Usuarios (HTTP) · RabbitMQ (publica tareas, consume resultados) · Redis (cadena, saldos, transacciones) · pool (lee `pool:plan`)."],
    ["**Por qué 2 réplicas**", "Lo pide el TPI. Para que no publiquen tareas duplicadas, solo el **líder** (lock `coord:leader` en Redis) arma y publica bloques. Todas las réplicas atienden la API y verifican resultados."],
    ["**Ejemplo**", "Llegan 1000 transacciones → arma el bloque 12 → publica 20 tareas (\"nonces 0 a 49.999.999\", \"50.000.000 a 99.999.999\", …) → llega el nonce 48213 → lo verifica → sella el bloque 12 → user-3 baja $50 y user-7 sube $50."],
  ]),

  ...ficha("Pool (P5)", [
    ["**Qué es**", "Servicio Python en GKE, 2 réplicas con líder (`pool:leader`). P5 es el punto del enunciado que pide un pool que administre los mineros."],
    ["**De qué se encarga**", "Cada 15 s: 1) Cuenta los mineros vivos por tipo (GPU / CPU) con `SCAN worker:*`. 2) Mira cuántas tareas hay esperando en `mining.tasks`. 3) Decide el **plan**: prefijo efectivo (dificultad) y tamaño de cada rango, y lo deja en `pool:plan`. 4) Decide cuántos mineros CPU hacen falta y lo deja en `pool:scaling`, que KEDA lee por `GET /scaling`."],
    ["**Qué NO hace**", "No publica tareas (eso lo hace el coordinador con el plan del pool). No levanta mineros por sí mismo: solo publica el número deseado; el que crea los pods es KEDA."],
    ["**Con quién habla**", "Redis (lee `worker:*`, escribe `pool:plan` y `pool:scaling`) · RabbitMQ (largo de cola) · KEDA (le responde `GET /scaling`)."],
    ["**Ejemplo**", "No hay ninguna GPU viva y hay 40 tareas en cola → pide 6 mineros CPU y baja el prefijo para que la CPU llegue. Se conecta la GTX 1050 Ti → vuelve a 0 mineros CPU y sube el prefijo."],
  ]),

  ...ficha("Gateway", [
    ["**Qué es**", "La única puerta de entrada de los mineros. Servicio Python (FastAPI) en GKE, 2 a 6 réplicas con HPA, detrás de un balanceador público. Exige `Authorization: Bearer <MINER_TOKEN>`."],
    ["**De qué se encarga**", "1) `GET /tasks/next`: saca una tarea de `mining.tasks` y se la presta al minero, anotando el préstamo en Redis (`lease:<task>` con vencimiento). 2) `POST /results`: recibe el resultado y lo publica en `mining.results`. 3) `POST /keepalive`: registra que el minero está vivo (`worker:<id>`, TTL 15 s). 4) Responder `410` si el bloque ya se cerró, para que el minero abandone. 5) **Reaper**: si un préstamo vence sin resultado (el minero murió), devuelve la tarea a la cola; a los 3 intentos la manda a `mining.dlq`."],
    ["**Qué NO hace**", "No verifica resultados ni toca la cadena: solo traslada mensajes entre HTTP y RabbitMQ. No decide nada sobre bloques."],
    ["**Por qué existe**", "Los mineros están afuera de GCP, detrás de NAT. No conviene exponer RabbitMQ ni Redis a Internet. El gateway expone 3 endpoints HTTP chicos con token, y todo lo demás queda privado."],
    ["**Con quién habla**", "Mineros (HTTP) · RabbitMQ (toma tareas, publica resultados, escucha `block.solved`) · Redis (leases y keep-alive)."],
  ]),

  h1("3. Infraestructura de mensajería y datos"),

  ...ficha("RabbitMQ", [
    ["**Qué es**", "El broker de mensajes. StatefulSet en el node pool infra, sin acceso público."],
    ["**De qué se encarga**", "Mover trabajo entre servicios de forma desacoplada y sin perder mensajes. Tiene un exchange `blockchain` (topic) y cuatro colas:"],
    ["`mining.tasks`", "Tareas de minado (un rango de nonces cada una). Las publica el coordinador; las saca el gateway. Cada tarea la toma uno solo."],
    ["`mining.results`", "Resultados (\"encontré el nonce X\" o \"no hay nada en mi rango\"). Los publica el gateway; los consume una réplica del coordinador."],
    ["`block.events.<pod>`", "Aviso de \"bloque cerrado\". Cada pod tiene su propia cola, así **todos** reciben una copia (broadcast)."],
    ["`mining.dlq`", "Tareas que fallaron 3 veces. Nadie las procesa sola: se miran a mano y se cuentan en las métricas. El bloque no se traba, porque el coordinador sigue publicando rangos nuevos."],
    ["**Qué NO hace**", "No guarda estado del sistema (la cadena, los saldos o las transacciones pendientes). Una cola sirve para pasar mensajes, no para consultarlos: eso es Redis."],
  ]),

  ...ficha("Redis", [
    ["**Qué es**", "La base de datos en memoria, con persistencia en disco (AOF cada 1 s + PVC). StatefulSet en el node pool infra, sin acceso público."],
    ["**De qué se encarga**", "Guardar todo el **estado**: la cadena (`block:<i>`, `chain:tip`), las transacciones pendientes (`tx:pending`), los saldos (`balance:<user>`, `pending_out:<user>`), los mineros vivos (`worker:<id>`), los préstamos (`lease:<task>`), el plan del pool y los locks de líder."],
    ["**Por qué hace falta**", "Los servicios no guardan nada en memoria propia: si se cae un pod, el otro sigue desde Redis. Y los scripts Lua permiten operaciones atómicas (reservar saldo, sellar un bloque solo si nadie lo selló antes)."],
    ["**Qué NO hace**", "No reparte trabajo (eso es RabbitMQ)."],
  ]),

  h1("4. Los mineros (fuera de Google Cloud)"),

  ...ficha("Minero GPU: wrapper + miner_gpu", [
    ["**Qué es**", "Dos programas que corren en tu PC. `miner_gpu` es el binario CUDA (C++) del Pilar 1. El **wrapper** es un script Python que lo maneja."],
    ["**miner_gpu (binario)**", "Recibe un rango por línea de comandos (`--base --prefix --from --to`), prueba todos esos nonces en la GPU en paralelo y devuelve **un solo JSON** por stdout: si encontró el nonce, cuál, el hash y cuántos hashes hizo. No sabe nada de HTTP, colas ni bloques: es \"tonto\" a propósito."],
    ["**Wrapper (Python)**", "Hace todo lo demás: 1) Keep-alive al gateway cada 5 s. 2) Pide una tarea (`GET /tasks/next`). 3) Llama al binario por tramos dentro del rango. 4) Entre tramo y tramo pregunta si la tarea sigue vigente; si recibe `410`, abandona. 5) Manda el resultado (`POST /results`). 6) Escribe sus logs (consola + archivo)."],
    ["**Por qué separados**", "El binario se puede testear solo contra el oráculo (`make check`, 17/17) y el wrapper se puede cambiar (Pilar 1: coordinador de la cátedra; Pilar 2: nuestro gateway) sin tocar CUDA."],
    ["**Dónde más puede correr**", "Colab (GPU T4) o el cluster de la cátedra, con el mismo wrapper. Nunca en GCP."],
  ]),

  ...ficha("Mineros CPU (miner-cpu)", [
    ["**Qué es**", "Un Deployment en el k3s local (namespace `tpi-miners`). Python multiproceso: busca nonces con `hashlib.md5` usando todos los núcleos."],
    ["**De qué se encarga**", "Lo mismo que el minero GPU, pero por CPU: keep-alive, pedir tarea, buscar en el rango, devolver resultado. Existe para cuando **no hay GPU**: el enunciado pide levantar mineros CPU en ese caso."],
    ["**Cuántos hay**", "De 0 a N. No se fija a mano: lo decide el pool y lo ejecuta KEDA."],
  ]),

  ...ficha("k3s local y KEDA", [
    ["**k3s local**", "Un Kubernetes liviano en tu PC (con k3d, dentro de Docker), el mismo tipo de cluster del lab del TP3. Existe porque los mineros CPU tienen que correr en Kubernetes pero **fuera de GCP**."],
    ["**KEDA**", "Un autoscaler que corre en el k3s. Cada 15 s le pregunta al pool `GET /scaling` cuántos mineros CPU quiere y ajusta las réplicas de `miner-cpu`. Puede bajar a 0 (un HPA común no puede)."],
    ["**Qué NO hace**", "KEDA no decide la cantidad: obedece el número que publica el pool."],
  ]),

  h1("5. Observabilidad"),
  table(["Componente", "Dónde", "De qué se encarga"], [
    ["**/metrics** en cada servicio", "coordinator, pool, gateway", "Exponen números: bloques sellados, tiempo por bloque, transacciones pendientes, resultados por estado, mineros vivos, hashrate."],
    ["**rabbitmq_prometheus**", "plugin de RabbitMQ", "Métricas del broker: mensajes por cola, consumidores."],
    ["**redis_exporter**", "sidecar de Redis", "Métricas de Redis: memoria, clientes, comandos por segundo."],
    ["**Prometheus**", "GKE, pool apps", "Cada pocos segundos lee (scrapea) todos los `/metrics` y guarda la serie de tiempo."],
    ["**Grafana**", "GKE, pool apps", "Dibuja dashboards con lo que guardó Prometheus. Es lo que se muestra en la defensa."],
    ["**Logs**", "stdout de cada pod", "JSON por stdout → GKE ya trae Fluent Bit, que los manda solo a Cloud Logging. Además: archivo en disco y buffer en memoria (`/logs`), como pide el enunciado. Loki: pendiente de decidir (ver `decisiones.docx`)."],
    ["**/status**", "coordinator, público", "Un JSON con el estado de cada servicio y cuántos mineros hay. Lo pide el TPI."],
  ], [2.2, 2, 5.4]),

  h1("6. Construcción y despliegue"),
  table(["Componente", "De qué se encarga"], [
    ["**GitHub**", "Repositorio público con el código, un directorio por pilar."],
    ["**GitHub Actions**", "Los pipelines de CI/CD: gitleaks (que no se suban secretos), tests, compilación con nvcc, `tofu plan` en los PR y `tofu apply` en main, build de imágenes y deploy con kubectl."],
    ["**OIDC / Workload Identity Federation**", "Permite que GitHub Actions entre a GCP **sin una clave guardada**: Google confía en el token que firma GitHub. Lo mismo que hiciste en el TP3."],
    ["**OpenTofu**", "Crea la infraestructura como código: VPC, cluster GKE, los 2 node pools, Artifact Registry, bucket del estado. Es Terraform con otro nombre."],
    ["**Artifact Registry**", "Guarda las imágenes Docker de los servicios, con el SHA del commit como tag."],
    ["**Balanceadores públicos**", "Dos IP públicas: una para la API (usuarios y `/status`) y otra para el gateway (mineros). RabbitMQ y Redis no tienen IP pública."],
  ], [2.6, 7]),

  h1("7. Quién habla con quién"),
  table(["Desde", "Hacia", "Cómo", "Para qué"], [
    ["Usuario / `bench.py`", "coordinator", "HTTP público", "Mandar transacciones, consultar `/status`"],
    ["coordinator", "RabbitMQ", "AMQP", "Publicar tareas; consumir resultados; publicar `block.solved`"],
    ["coordinator", "Redis", "RESP + Lua", "Validar y reservar saldo, guardar pendientes, sellar bloques"],
    ["pool", "Redis / RabbitMQ", "RESP / API de RabbitMQ", "Contar mineros vivos y tareas en cola; publicar el plan"],
    ["KEDA (k3s)", "pool", "HTTP `GET /scaling`", "Saber cuántos mineros CPU levantar"],
    ["Minero (GPU o CPU)", "gateway", "HTTP + Bearer token", "Keep-alive, pedir tarea, devolver resultado"],
    ["gateway", "RabbitMQ", "AMQP", "Sacar tareas, publicar resultados, escuchar `block.solved`"],
    ["gateway", "Redis", "RESP", "Leases y keep-alive"],
    ["Prometheus", "todos", "HTTP `/metrics`", "Juntar métricas"],
    ["GitHub Actions", "GCP", "OIDC (WIF)", "Crear infraestructura y desplegar"],
  ], [2, 1.8, 2, 3.8]),
  gap(),
  p("**Nadie de afuera de GKE habla con RabbitMQ ni con Redis.** Esa es la frontera de seguridad del diseño."),

  h1("8. Una transacción de punta a punta"),
  num("`bench.py` manda `user-3 → user-7: $50` al **coordinator**."),
  num("El **coordinator** revisa en **Redis** que user-3 tenga $50 disponibles, los reserva y deja la transacción en `tx:pending`. Responde `202`."),
  num("Mientras tanto, el **pool** contó 1 minero GPU vivo y dejó en Redis el plan: prefijo de 7 ceros y rangos de 50 millones."),
  num("Se juntan 1000 transacciones: el **coordinator** líder arma el bloque 12 y publica 20 tareas en **RabbitMQ** (`mining.tasks`)."),
  num("El **wrapper** de tu PC pide trabajo al **gateway**. El gateway saca una tarea de la cola, anota el préstamo en Redis y se la da."),
  num("El wrapper llama a **miner_gpu**, que encuentra el nonce 48213. El wrapper lo manda al **gateway**, que lo publica en `mining.results`."),
  num("Una réplica del **coordinator** consume el resultado, recalcula el MD5 y confirma que empieza con 7 ceros."),
  num("Un script Lua en **Redis** sella el bloque 12 (si nadie lo selló antes) y mueve los saldos: user-3 baja $50 y user-7 sube $50."),
  num("El coordinator publica `block.solved`. Cada **gateway** lo recibe y, a los mineros que siguen con tareas del bloque 12, les responde `410`: abandonan."),
  num("**Prometheus** registra un bloque más y el tiempo que tardó; **Grafana** lo muestra."),

  h1("9. Glosario"),
  table(["Término", "Qué significa en este TP"], [
    ["**Nonce**", "El número que se va probando hasta que `MD5(base + nonce)` empiece con el prefijo pedido."],
    ["**Prefijo / dificultad**", "Cuántos ceros hex tiene que tener el hash al principio. Cada cero más multiplica por 16 el trabajo."],
    ["**NCT**", "Las tareas del nodo coordinador del enunciado (publicar, verificar, sellar)."],
    ["**P5**", "El punto del enunciado que pide el pool de mineros con keep-alive y fragmentación."],
    ["**Lease (préstamo)**", "Una tarea que el gateway le dio a un minero, con vencimiento. Si vence sin resultado, vuelve a la cola."],
    ["**Reaper**", "El proceso del gateway que recorre los leases vencidos y devuelve esas tareas."],
    ["**Keep-alive**", "El \"sigo vivo\" que cada minero manda cada 5 s. Si deja de mandarlo, su clave en Redis vence a los 15 s."],
    ["**HPA**", "Horizontal Pod Autoscaler: Kubernetes agrega o saca réplicas según el uso de CPU."],
    ["**Taint**", "Una marca en un node pool para que solo corran ahí los pods que la toleran (RabbitMQ y Redis en infra)."],
    ["**StatefulSet + PVC**", "La forma de correr en Kubernetes algo que guarda datos en disco (RabbitMQ, Redis) sin perderlos al reiniciar."],
    ["**DLQ**", "Dead letter queue: la cola de las tareas que fallaron demasiadas veces."],
  ], [2.2, 7.4]),
];

const hstyle = (size, color) => ({ run: { font: FONT, size, bold: true, color }, paragraph: { spacing: { before: 300, after: 120 }, keepNext: true } });

const doc = new Document({
  styles: {
    default: { document: { run: { font: FONT, size: 21 } } },
    paragraphStyles: [
      { id: "Title", name: "Title", basedOn: "Normal", next: "Normal", run: { font: FONT, size: 44, bold: true, color: "1F3A5F" }, paragraph: { spacing: { before: 0, after: 60 }, keepNext: true } },
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true, ...hstyle(28, "1F3A5F") },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true, ...hstyle(24, "2E5C8A") },
    ],
  },
  numbering: {
    config: [
      { reference: "bul", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 400, hanging: 260 } } } }] },
      { reference: "num", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 440, hanging: 320 } } } }] },
    ],
  },
  sections: [{ properties: { page: { margin: { top: 1134, bottom: 1134, left: 1134, right: 1134 } } }, children }],
});

Packer.toBuffer(doc).then((b) => { fs.writeFileSync(process.argv[2], b); console.log("ok"); });
