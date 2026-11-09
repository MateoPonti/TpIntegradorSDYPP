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
const gap = () => new Paragraph({ spacing: { after: 120 }, children: [] });

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

// Una duda: qué falta, por qué importa, opciones, propuesta
function duda(titulo, filas) {
  return [h2(titulo), table(null, filas, [1.6, 6]), gap()];
}

const children = [
  new Paragraph({ heading: HeadingLevel.TITLE, children: [new TextRun("Dudas pendientes")] }),
  new Paragraph({ children: [new TextRun({ text: "TPI SDyPP 2026 · Blockchain distribuida y CUDA · Relevado el 6/11/2026", color: "5B636E" })], spacing: { after: 240 } }),
  p("Lo que todavía no está definido en `decisiones.docx`, `componentes.docx` ni `arquitectura.html`. Cada duda tiene opciones y una **propuesta** para discutir; nada de esto está decidido. Cuando se decida, pasa a `decisiones.docx` y se borra de acá."),
  p("**Ya decididas el 6/11 y pasadas a `decisiones.docx`:** 1 (bloque que nadie resuelve: rangos por tandas), 2 (el bloque siguiente se arma enseguida al sellar), 3 (la DLQ no se reprocesa sola) y 7 (la regla de armado quedó con el mismo texto en los tres documentos). Las que quedan conservan su número."),

  h1("Resumen por prioridad"),
  table(["#", "Duda", "Prioridad", "Por qué"], [
    ["4", "Fórmulas del pool", "**Alta**", "Sin esto el pool no se puede programar"],
    ["5", "Cada cuánto revisa el coordinador", "Media", "Detalle de implementación, afecta latencia"],
    ["6", "Dónde se guarda la hora de la primera pendiente", "Media", "Hace falta para medir `BLOCK_TIMEOUT`"],
    ["8", "Parámetros CUDA", "Media", "Se resuelven midiendo (Pilar 1)"],
    ["9", "Opcionales y infraestructura", "Baja", "Solo si sobra tiempo o crédito"],
    ["10", "Premio al minero", "Baja", "El enunciado describe el reparto proporcional, pero no lo exige como entregable"],
    ["11", "Cómo llega KEDA a `GET /scaling`", "**Alta**", "Hoy los docs no dicen cómo el k3s llega al pool, que no es público"],
    ["12", "Qué escala además de los mineros", "Media", "Puede hacer falta en el Pilar 3 si `bench.py` satura la API"],
  ], [0.4, 3.6, 1.2, 4.4]),

  h1("Comentarios del profe"),
  p("Cosas que sugirió el profe en clase. Todavía no están decididas: se anotan para discutirlas en el grupo."),

  ...duda("Profe 1 — Armar los bloques por ventana de tiempo, no por cantidad", [
    ["**Lo que dijo**", "Las transacciones se pueden agrupar por **ventana de tiempo** en vez de esperar a juntar 1000. Como mucho, si en la ventana entran demasiadas, se **divide** en varios bloques."],
    ["**Lo que tenemos**", "Regla de la duda 2/7: si no se está minando, el bloque sale con **1000 tx o a los 10 s** desde la primera pendiente, lo que pase primero. Al sellar, el siguiente se arma enseguida."],
    ["**Qué cambiaría**", "El disparo principal pasa a ser el **tiempo** (por ejemplo cada 10 s se cierra la ventana con todo lo que llegó), y la cantidad queda solo como **tope**: si en la ventana llegaron 2500 tx y el tope es 1000, salen 3 bloques (1000 + 1000 + 500) que se minan uno detrás del otro."],
    ["**A favor**", "Más fácil de explicar y de medir: cada bloque representa \"lo que pasó en 10 s\". La latencia de una tx queda acotada por la ventana."],
    ["**En contra**", "Con poca carga salen bloques chicos (pocas tx) y se gasta un PoW entero en ellos. Con mucha carga se acumulan bloques en espera, porque se mina uno a la vez."],
    ["**Qué hay que decidir**", "Largo de la ventana; si una ventana vacía no genera bloque; el tope por bloque; y si la ventana corre mientras se mina o arranca al sellar."],
  ]),

  ...duda("Profe 2 — Tarea inicial para medir y clasificar a cada worker", [
    ["**Lo que dijo**", "Cuando un worker se conecta, mandarle **una tarea de prueba** solo para medir cuánto rinde, y con eso asignarle un valor que lo clasifique (qué tan bueno es)."],
    ["**Lo que tenemos**", "El pool calcula el tamaño de rango con el hashrate **medido de sus resultados** (`hashes / seconds`), propuesta de la duda 4. Problema: la primera tarea de cada worker sale con un tamaño a ciegas."],
    ["**Cómo sería**", "Al primer keep-alive, el gateway le da una tarea de calibración: base fija y un rango fijo (por ejemplo 2^24 nonces) con un prefijo imposible, para que no corte antes. Con lo que tarda se guarda `worker:<id>:hashrate` y una clase (por ejemplo rápido / medio / lento). Después el hashrate se sigue actualizando con cada resultado real."],
    ["**A favor**", "Desde la primera tarea el rango tiene el tamaño correcto. La clase sirve para darle rangos grandes a la GPU y chicos a los CPU, y para mostrarlo en Grafana y en la defensa."],
    ["**En contra**", "Unos segundos perdidos por cada worker nuevo (con KEDA levantando pods seguido, se nota). El `seconds` lo reporta el propio worker: conviene que el gateway mida también el tiempo de punta a punta."],
    ["**Relación**", "Entra en la duda 4 (tamaño de rango) y en la duda 9 (separar `task.gpu` / `task.cpu`)."],
  ]),

  h1("1. Armado de bloques"),

  ...duda("Duda 5 — ¿Cada cuánto revisa el coordinador si hay que armar un bloque?", [
    ["**Lo que hay**", "Nada. Los docs dicen qué condición se chequea, no cuándo."],
    ["**Opción A**", "Loop del líder cada 1 s (o 500 ms): mira el largo de `tx:pending` y la hora de la primera."],
    ["**Opción B**", "Reaccionar al llegar cada tx (si llegó a 1000, armar) + un timer para el caso de los 10 s."],
    ["**Propuesta**", "**A**, más simple y alcanza: 1 s de error contra 10 s de timeout. Hacerlo configurable (`BLOCK_POLL_SECONDS`)."],
  ]),

  ...duda("Duda 6 — ¿Dónde se guarda la hora de la primera transacción pendiente?", [
    ["**Lo que hay**", "La regla mide 10 s \"desde la primera pendiente\", pero ninguna clave de Redis guarda esa hora."],
    ["**Opción A**", "Campo `ts` (hora del servidor) en cada tx de `tx:pending`; se lee con `LINDEX tx:pending 0`."],
    ["**Opción B**", "Clave aparte `tx:pending:since`, escrita con `SET NX` por el Lua de validación y borrada al armar el bloque."],
    ["**Propuesta**", "**A**: no agrega claves que puedan quedar desincronizadas, y el `ts` sirve además para medir latencia por transacción."],
  ]),

  h1("2. Pool"),

  ...duda("Duda 4 — ¿Con qué fórmulas decide el pool?", [
    ["**Lo que hay**", "Qué mira (mineros vivos por tipo, largo de `mining.tasks`) y qué publica (`pool:plan`, `pool:scaling`). Falta el **cómo**."],
    ["**Prefijo efectivo**", "¿Cuántos ceros con GPU y cuántos sin GPU? ¿Es un valor fijo por caso o depende del hashrate total? Propuesta: dos valores en el ConfigMap (`PREFIX_GPU`, `PREFIX_CPU_ONLY`) elegidos con las mediciones del Pilar 1, para que un bloque tarde un tiempo razonable (por ejemplo 10–30 s)."],
    ["**Tamaño de rango**", "¿Fijo o calculado? Propuesta: `rango = hashrate_del_minero × segundos_objetivo_por_tarea` (por ejemplo 5 s), usando el hashrate medido (`hashes / segundos` de sus resultados), no el reportado. La fragmentación del Pilar 3 (1 %, 5 %, 10 %, 25 %, 50 %) se prueba como alternativa."],
    ["**Mineros CPU deseados**", "¿Cuántos pide según la cola? Propuesta: 0 si hay alguna GPU viva; si no, `min(MAX_CPU_MINERS, ceil(tareas_en_cola / TAREAS_POR_MINERO))` con al menos 1 si hay tareas."],
    ["**¿CPU también si la GPU no da abasto?**", "Surgió el 6/11. El enunciado exige mineros CPU cuando no hay GPU, pero dice \"cuando sea necesario\", así que se puede ir más allá. **Opción A:** CPU solo si no hay ninguna GPU viva (simple y fácil de mostrar en la defensa). **Opción B:** CPU si no hay GPU o si el hashrate de los mineros vivos no alcanza para el prefijo (una sola fórmula, pero con la GPU viva un pod de CPU aporta del orden del 0,1 % y suma lógica al pool). Propuesta: **A**, justificada con el hashrate real que salga del Pilar 1. Se puede decidir después de medir."],
    ["**Cambio de prefijo**", "Si el pool baja el prefijo sin GPU, ¿se aplica al bloque que ya se está minando o solo al siguiente? Propuesta: solo al siguiente; la dificultad queda fija en cada bloque (`difficulty`)."],
    ["**Mineros levantados a mano**", "Cualquier minero con el token puede trabajar aunque no lo haya creado KEDA: manda keep-alive, el pool lo cuenta y recibe rangos. KEDA solo controla las réplicas de su Deployment `miner-cpu`. La fórmula de `pool:scaling` tiene que decidir si cuenta a esos mineros (para no pedir de más) o solo mira la cola."],
  ]),

  ...duda("Duda 11 — ¿Cómo llega el KEDA del k3s a GET /scaling?", [
    ["**Lo que hay**", "KEDA corre en el k3s local (tiene que estar ahí: solo crea pods en su propio cluster; un KEDA en GKE no puede tocar el k3s, que está detrás de NAT). Cada 15 s **sale** a preguntar `GET /scaling`. Los docs dicen que le pregunta **al pool**, pero el pool está en GKE sin IP pública: la única puerta pública es el gateway."],
    ["**Opción A**", "El gateway expone `GET /scaling` con el mismo Bearer token y devuelve lo que hay en `pool:scaling` (Redis). Una sola puerta a Internet."],
    ["**Opción B**", "Exponer el pool con su propio Ingress o LoadBalancer. Es otra IP pública y otro token que cuidar."],
    ["**Propuesta**", "**A**. Si se decide, hay que corregir `componentes.docx` (KEDA y la tabla de quién habla con quién), `decisiones.docx` §11 y las flechas \"GET /scaling\" de los diagramas, que hoy van al pool."],
  ]),

  ...duda("Duda 12 — ¿Qué más escala, además de los mineros?", [
    ["**Lo que hay**", "KEDA escala solo `miner-cpu` en el k3s. El gateway ya tiene HPA (2 a 6 réplicas). KEDA podría escalar cualquier Deployment, pero solo del cluster donde corre."],
    ["**Qué se puede escalar**", "**gateway:** sí, no guarda estado (los leases están en Redis); ya tiene HPA. **coordinator (API):** sí, la parte que recibe `POST /transactions`; el armado y el sellado los hace solo el líder (`coord:leader`). **pool:** no, decide uno solo (`pool:leader`); las réplicas son respaldo. **Redis y RabbitMQ:** no con KEDA ni HPA, tienen estado; escalarlos es armar un cluster."],
    ["**Opción A**", "Dejar KEDA solo para los mineros y, si en el Pilar 3 la API se satura, poner un **HPA por CPU** al coordinator en GKE (viene con GKE, no hay que instalar nada)."],
    ["**Opción B**", "Instalar también KEDA en GKE y escalar el coordinator según el largo de `tx:pending` o las requests."],
    ["**Propuesta**", "**A**: más simple. Se decide con lo que muestre `bench.py` en el Pilar 3."],
  ]),

  h1("3. Minero CUDA (se resuelven midiendo)"),
  p("Ya están en `decisiones.docx` §10 como \"se eligen midiendo\"; se listan acá para no perderlos."),
  table(["Parámetro", "Qué probar", "Estado"], [
    ["Threads por bloque CUDA", "64, 128, 256, 512 (arranque 256)", "Sin medir"],
    ["Bloques por grid", "6, 12, 48, 192 (1, 2, 8 y 32 × 6 SMs)", "Sin medir"],
    ["K (cada cuánto se mira la bandera de \"encontrado\")", "1, 64, 4096, sin sondeo", "Sin medir"],
    ["Nonces por lanzamiento de kernel", "según throughput", "Sin medir"],
    ["Tramo del wrapper (`--chunk`)", "medir el overhead de 0,18 s por proceso", "Sin medir"],
    ["Rango por tarea", "1 %, 5 %, 10 %, 25 %, 50 % (Pilar 3)", "Sin medir"],
  ], [3, 4, 1.6]),

  h1("4. Opcionales e infraestructura"),

  ...duda("Duda 10 — ¿Hay premio para los mineros y cómo se reparte?", [
    ["**Lo que hay**", "El **trabajo** ya es colaborativo: rangos de nonce disjuntos, nadie repite (P5). El **premio** no existe: la coinbase quedó como idea no prioritaria."],
    ["**Qué dice el enunciado**", "P2: \"N nodos deben competir o colaborar — en un pool de minería los mineros comparten poder de cómputo y dividen las recompensas proporcionalmente\". NCT.2 se llama \"Competencia o cooperación\". O sea: deja elegir, y cuando describe un pool, describe el reparto **proporcional** (opción B)."],
    ["**Dos ejes distintos**", "Repartir el trabajo y repartir el premio son cosas separadas. Se puede tener rangos divididos y aun así que compitan por la recompensa."],
    ["**Opción A**", "**El que encuentra se lleva todo.** El resultado ganador ya trae el `worker_id`; la coinbase va a su nombre. Con rangos asignados funciona como una lotería: gana el que recibió el rango con el nonce."],
    ["**Opción B**", "**Proporcional a los hashes probados** (como PPS/PPLNS en pools reales). Cada resultado ya reporta cuántos hashes hizo; se suman por minero y se reparte al sellar."],
    ["**Opción C**", "Sin premio (lo que hay hoy)."],
    ["**Propuesta**", "**C** mientras no sobre tiempo, pero saber explicar en la defensa que el trabajo es colaborativo. Si se agrega premio, **B**, porque es lo que describe el enunciado y los resultados ya traen `hashes`; **A** es más barata si el tiempo no alcanza."],
  ]),
  table(["Duda", "Dónde aparece", "Nota"], [
    ["¿Separar `task.gpu` y `task.cpu` con rangos de distinto tamaño?", "`decisiones.docx` §15", "La guía del Pilar 2 lo marca opcional"],
    ["¿Minero GPU persistente (un contexto CUDA por proceso)?", "`decisiones.docx` §15", "Mejora opcional; hay que mantener el modo CLI"],
    ["¿Frontend o explorador de bloques?", "`decisiones.docx` §15", "No obligatorio"],
    ["¿Loki para logs?", "`componentes.docx` §5", "Ya hay Cloud Logging + archivo + `/logs`"],
    ["¿RabbitMQ con 3 réplicas?", "`decisiones.docx` §2", "Quorum con 1 nodo no da alta disponibilidad; depende del crédito"],
    ["¿Firmas Ed25519?", "`decisiones.docx` §14", "Idea no prioritaria (la recompensa al minero es la duda 10)"],
  ], [3.8, 2.2, 3.6]),
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
    ],
  },
  sections: [{ properties: { page: { margin: { top: 1134, bottom: 1134, left: 1134, right: 1134 } } }, children }],
});

Packer.toBuffer(doc).then((b) => { fs.writeFileSync(process.argv[2], b); console.log("ok"); });
