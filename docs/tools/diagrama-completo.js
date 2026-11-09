// Genera docs/diagrama-completo.excalidraw (abrir en excalidraw.com) y una versión para pegar con Ctrl+V.
// Uso: node diagrama-completo.js <salida.excalidraw> <salida-clipboard.json>
const fs = require("fs");

const els = [];
let n = 0;
const id = () => "el" + (++n);
const seed = () => Math.floor(Math.random() * 2 ** 31);
const FS = 16;
const INK = "#1e1e1e", GRIS = "#868e96";

function base(type, x, y, w, h, o = {}) {
  return {
    id: o.id || id(), type, x, y, width: w, height: h, angle: 0,
    strokeColor: o.stroke || INK, backgroundColor: o.bg || "transparent",
    fillStyle: o.fill || "solid", strokeWidth: o.sw || 2, strokeStyle: o.dashed ? "dashed" : "solid",
    roughness: 1, opacity: 100, groupIds: [], frameId: null,
    roundness: type === "rectangle" ? { type: 3 } : type === "arrow" ? { type: 2 } : null,
    seed: seed(), version: 1, versionNonce: seed(), isDeleted: false,
    boundElements: [], updated: 1, link: null, locked: false,
  };
}

function textEl(str, cx, cy, o = {}) {
  const size = o.size || FS;
  const lines = str.split("\n");
  const w = Math.max(...lines.map((l) => l.length)) * size * 0.58;
  const h = lines.length * size * 1.25;
  const x = o.left ? cx : cx - w / 2, y = o.top ? cy : cy - h / 2;
  return {
    ...base("text", x, y, w, h, { stroke: o.color }), roundness: null,
    text: str, originalText: str, fontSize: size, fontFamily: 5,
    textAlign: o.left ? "left" : "center", verticalAlign: o.top ? "top" : "middle",
    containerId: o.container || null, lineHeight: 1.25, autoResize: true,
  };
}

// Caja con texto adentro
function box(x, y, w, h, label, o = {}) {
  const r = base("rectangle", x, y, w, h, o);
  const t = textEl(label, x + w / 2, y + h / 2, { container: r.id, size: o.size });
  r.boundElements.push({ type: "text", id: t.id });
  els.push(r, t);
  return r;
}

// Zona: rectángulo punteado gris sin fondo, con el nombre arriba a la izquierda
function zone(x, y, w, h, title) {
  const r = base("rectangle", x, y, w, h, { stroke: GRIS, dashed: true });
  els.push(r);
  els.push(textEl(title, x + 16, y + 12, { left: true, top: true, size: 20 }));
  return r;
}

const title = (str, x, y) => els.push(textEl(str, x, y, { left: true, top: true, size: 32 }));
const note = (str, x, y, o = {}) => els.push(textEl(str, x, y, { left: true, top: true, size: o.size || 16, color: o.color }));

// Línea sin punta (líneas de vida del diagrama de secuencia)
function line(pts, o = {}) {
  const [x0, y0] = pts[0];
  const rel = pts.map(([x, y]) => [x - x0, y - y0]);
  els.push({
    ...base("line", x0, y0, Math.abs(rel[rel.length - 1][0]), Math.abs(rel[rel.length - 1][1]), o),
    points: rel, lastCommittedPoint: null, startBinding: null, endBinding: null,
    startArrowhead: null, endArrowhead: null,
  });
}

// Flecha por puntos absolutos, pegada a las cajas de origen y destino
function arrow(pts, from, to, label, o = {}) {
  const [x0, y0] = pts[0];
  const rel = pts.map(([x, y]) => [x - x0, y - y0]);
  const xs = rel.map((p) => p[0]), ys = rel.map((p) => p[1]);
  const a = {
    ...base("arrow", x0, y0, Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), o),
    points: rel, lastCommittedPoint: null, elbowed: false,
    startBinding: from ? { elementId: from.id, focus: 0, gap: 4 } : null,
    endBinding: to ? { elementId: to.id, focus: 0, gap: 4 } : null,
    startArrowhead: o.both ? "arrow" : null, endArrowhead: "arrow",
  };
  if (from) from.boundElements.push({ type: "arrow", id: a.id });
  if (to) to.boundElements.push({ type: "arrow", id: a.id });
  els.push(a);
  if (label) {
    let mx, my;
    if (pts.length % 2) [mx, my] = pts[(pts.length - 1) / 2];
    else { const i = pts.length / 2; mx = (pts[i - 1][0] + pts[i][0]) / 2; my = (pts[i - 1][1] + pts[i][1]) / 2; }
    const t = textEl(label, mx, my, { container: a.id, size: 14 });
    a.boundElements.push({ type: "text", id: t.id });
    els.push(t);
  }
  return a;
}

// Colores de la paleta común de Excalidraw
const SVC = "#a5d8ff", MQC = "#ffec99", RDC = "#ffc9c9", MIN = "#b2f2bb", OBS = "#d0bfff", W = "#ffffff";

// ═════════════ 1. Arquitectura ═════════════
title("1. Arquitectura", 20, -100);

const U = box(120, 0, 300, 90, "USUARIOS\nbench.py", { bg: W });
const NAV = box(620, 0, 300, 90, "NAVEGADOR", { bg: W });

zone(20, 180, 1860, 1080, "GKE");
const C = box(80, 280, 480, 200, "COORDINATOR (x2)\n\nrecibe y valida transacciones\narma el bloque\nreparte rangos de nonces\nverifica y sella", { bg: SVC });
const P = box(720, 280, 440, 200, "POOL (x2)\n\ncada 15 s decide:\ndificultad, tamaño de rango\ny cuántos mineros CPU", { bg: SVC });
const G = box(1320, 280, 480, 200, "GATEWAY (x2)\n\npuerta de entrada de los mineros\nentrega tareas, recibe resultados\nreaper + aviso 410", { bg: SVC });

const MQ = zone(80, 640, 780, 560, "RabbitMQ");
const EX = box(120, 760, 240, 80, "exchange\nblockchain", { bg: MQC });
const QT = box(520, 720, 300, 70, "mining.tasks\ntareas por hacer", { bg: MQC });
const QR = box(520, 830, 300, 70, "mining.results\nnonces encontrados", { bg: MQC });
const QE = box(520, 940, 300, 80, "block.events.<pod>\nbloque sellado", { bg: MQC });
const DLX = box(120, 1080, 240, 80, "blockchain.dlx", { bg: MQC });
const DLQ = box(520, 1080, 300, 80, "mining.dlq\nfallaron 3 veces", { bg: MQC });
arrow([[360, 780], [520, 755]], EX, QT, "task.#");
arrow([[360, 800], [520, 865]], EX, QR, "result.#");
arrow([[360, 820], [520, 980]], EX, QE, "block.*");
arrow([[360, 1120], [520, 1120]], DLX, DLQ, null);

const RD = zone(960, 640, 880, 560, "Redis");
[
  ["CADENA\nblock:<i>  hash:<h>  chain:tip", 990, 710],
  ["TRANSACCIONES\ntx:pending  tx:inflight  tx:seen", 1420, 710],
  ["SALDOS\nbalance:<u>  pending_out:<u>", 990, 870],
  ["MINEROS\nworker:<id>  lease:<task>", 1420, 870],
  ["PLAN\npool:plan  pool:scaling", 990, 1030],
  ["LÍDERES\ncoord:leader  pool:leader", 1420, 1030],
].forEach(([t, x, yy]) => box(x, yy, 390, 130, t, { bg: RDC }));

arrow([[270, 90], [270, 280]], U, C, "POST /transactions");
arrow([[770, 90], [480, 280]], NAV, C, "GET /status");
arrow([[240, 480], [240, 640]], C, MQ, "tareas / resultados", { both: true });
arrow([[520, 480], [1020, 640]], C, RD, "saldos, bloques, plan", { both: true });
arrow([[780, 480], [780, 640]], P, MQ, "largo de la cola");
arrow([[1100, 480], [1100, 640]], P, RD, "escribe el plan");
arrow([[1700, 480], [1700, 640]], G, RD, "mineros vivos, leases", { both: true });
arrow([[1380, 480], [1380, 565], [840, 565], [840, 640]], G, MQ, "saca tareas / publica resultados", { both: true });

zone(20, 1380, 880, 480, "k3s local");
const K = box(80, 1460, 300, 100, "KEDA", { bg: W });
const CPU = box(80, 1680, 600, 110, "MINEROS CPU\nminer-cpu (0..N réplicas)", { bg: MIN });
arrow([[230, 1560], [230, 1680]], K, CPU, "escala");
arrow([[380, 1510], [940, 1510], [940, 480]], K, P, "GET /scaling");
arrow([[380, 1790], [380, 1940], [1960, 1940], [1960, 380], [1800, 380]], CPU, G, "HTTPS + token");

zone(980, 1380, 900, 480, "PC (GTX 1050 Ti)");
const WR = box(1040, 1460, 560, 100, "WRAPPER (Python)", { bg: MIN });
const GPU = box(1040, 1680, 560, 110, "miner_gpu (CUDA)", { bg: MIN });
arrow([[1200, 1560], [1200, 1680]], WR, GPU, "--base --prefix --from --to");
arrow([[1450, 1680], [1450, 1560]], GPU, WR, "JSON");
arrow([[1600, 1510], [1920, 1510], [1920, 430], [1800, 430]], WR, G, "HTTPS + token");

// ═════════════ 2. Observabilidad ═════════════
const XO = 2200;
title("2. Observabilidad", XO, -100);

zone(XO, 0, 1880, 700, "Métricas");
const sources = [
  "COORDINATOR\n/metrics\nbloques, tiempo por bloque,\ntx pendientes, resultados",
  "POOL\n/metrics\nmineros vivos, hashrate,\ndificultad",
  "GATEWAY\n/metrics\ntareas entregadas,\nresultados recibidos",
  "RABBITMQ\nrabbitmq_prometheus\nmensajes por cola",
  "REDIS\nredis_exporter\nmemoria, comandos/s",
];
const PROM = box(XO + 690, 340, 500, 120, "PROMETHEUS  :9090\nva a buscar los /metrics cada 15 s\ny guarda cada número con su hora", { bg: OBS });
sources.forEach((t, i) => {
  const x = XO + 40 + i * 360;
  const b = box(x, 70, 320, 160, t, { bg: i < 3 ? SVC : i === 3 ? MQC : RDC });
  arrow([[XO + 740 + i * 100, 340], [x + 160, 230]], PROM, b, i === 2 ? "GET /metrics" : null);
});
const GRAF = box(XO + 690, 560, 500, 100, "GRAFANA  :3000\ndashboards", { bg: OBS });
const VOS = box(XO + 1380, 560, 300, 100, "VOS\nnavegador", { bg: W });
arrow([[XO + 940, 560], [XO + 940, 460]], GRAF, PROM, "consulta (PromQL)");
arrow([[XO + 1380, 610], [XO + 1190, 610]], VOS, GRAF, null);

zone(XO, 800, 1880, 520, "Logs (memoria y disco)");
const LS = box(XO + 40, 1000, 400, 120, "SERVICIOS\nlogs en JSON", { bg: SVC });
const L2 = box(XO + 680, 920, 600, 100, "archivo en disco (rotativo)", { bg: W });
const L3 = box(XO + 680, 1100, 600, 100, "memoria → GET /logs", { bg: W });
arrow([[XO + 440, 1040], [XO + 680, 970]], LS, L2, null);
arrow([[XO + 440, 1080], [XO + 680, 1150]], LS, L3, null);

zone(XO, 1420, 1880, 300, "Estado");
box(XO + 40, 1500, 1800, 160, "GET /status\nconsulta RabbitMQ, Redis, gateway y pool y devuelve el estado general:\naltura de la cadena, tx pendientes, mineros vivos, largo de las colas", { bg: SVC });

// ═════════════ 3. Flujo de un bloque ═════════════
const Y2 = 2150;
title("3. Flujo de un bloque", 20, Y2);
const lanes = [
  ["USUARIO", W], ["COORDINATOR", SVC], ["REDIS", RDC], ["RABBITMQ", MQC],
  ["GATEWAY", SVC], ["MINERO", MIN], ["POOL", SVC],
];
const cx = (i) => 220 + i * 420;
lanes.forEach(([name, bg], i) => box(cx(i) - 150, Y2 + 80, 300, 70, name, { bg }));

const steps = [
  ["msg", 6, 2, "0. cada 15 s: escribe\npool:plan y pool:scaling"],
  ["msg", 0, 1, "1. POST /transactions"],
  ["msg", 1, 2, "2. valida saldo y reserva\n→ tx:pending"],
  ["msg", 1, 0, "3. 202 (o 400)"],
  ["note", 1, "4. 1000 tx o 10 s\n(o enseguida si recién se selló uno)\n→ arma el bloque"],
  ["msg", 1, 2, "5. lee pool:plan"],
  ["msg", 1, 3, "6. publica tareas\n{base, prefix, from, to}"],
  ["msg", 5, 4, "7. pide tarea"],
  ["msg", 4, 3, "8. saca una tarea"],
  ["msg", 4, 2, "9. guarda el lease"],
  ["msg", 4, 5, "10. tarea"],
  ["note", 1, "si quedan pocas tareas en la cola,\npublica la tanda siguiente"],
  ["note", 5, "11. prueba nonces del rango"],
  ["msg", 5, 4, "12. resultado {nonce, hash}"],
  ["msg", 4, 3, "13. publica el resultado"],
  ["msg", 3, 1, "14. llega el resultado"],
  ["note", 1, "15. verifica md5(base + nonce)"],
  ["msg", 1, 2, "16. sella el bloque\ny actualiza saldos"],
  ["msg", 1, 3, "17. bloque sellado"],
  ["msg", 3, 4, "18. aviso a los gateways"],
  ["msg", 4, 5, "19. 410: dejá esa tarea"],
  ["note", 4, "reaper: lease vencido → vuelve a la cola\n3 intentos → mining.dlq"],
];
let y = Y2 + 220;
steps.forEach((s) => {
  if (s[0] === "msg") {
    const [, a, b, label] = s;
    const dir = b > a ? 1 : -1;
    arrow([[cx(a) + dir * 6, y], [cx(b) - dir * 6, y]], null, null, label);
    y += 100;
  } else {
    const [, lane, label] = s;
    const h = label.split("\n").length * 22 + 30;
    box(cx(lane) - 180, y - 25, 360, h, label, { bg: W });
    y += h + 40;
  }
});
lanes.forEach((_, i) => line([[cx(i), Y2 + 150], [cx(i), y]], { dashed: true, stroke: GRIS, sw: 1 }));

// ═════════════ 4. Datos ═════════════
const Y3 = y + 200;
title("4. Datos", 20, Y3);
const TX = box(60, Y3 + 100, 460, 180, "TRANSACCIÓN\nsender, receiver,\namount (centavos)", { bg: W });
const BL = box(660, Y3 + 80, 620, 220, "BLOQUE\nindex, previous_hash, timestamp,\ntransactions, tx_digest,\ndifficulty, nonce, hash\n\nhash = md5(previous_hash + tx_digest + nonce)", { bg: RDC });
const TA = box(1420, Y3 + 100, 460, 180, "TAREA\nbase, prefix, from, to", { bg: MQC });
const RE = box(2020, Y3 + 100, 460, 180, "RESULTADO\nnonce, hash, minero", { bg: MQC });
arrow([[520, Y3 + 190], [660, Y3 + 190]], TX, BL, "1000 o 10 s");
arrow([[1280, Y3 + 190], [1420, Y3 + 190]], BL, TA, "rangos");
arrow([[1880, Y3 + 190], [2020, Y3 + 190]], TA, RE, "minero");
note("saldo disponible = balance − pending_out", 60, Y3 + 340);

// ═════════════ 5. Del bloque a los threads CUDA ═════════════
const X4 = 2800;
title("5. Del bloque a los threads CUDA", X4, Y3);
const chain = [
  ["BLOQUE #12\nnonces 0 … N", W, null],
  ["TAREAS\nrangos de nonces", MQC, "pool"],
  ["TRAMOS (--chunk)", MIN, "wrapper"],
  ["1 ejecución de miner_gpu", MIN, null],
  ["GRID\n6 / 12 / 48 / 192 bloques CUDA", MIN, "se mide"],
  ["BLOQUE CUDA\n64 / 128 / 256 / 512 threads", MIN, "se mide"],
  ["THREAD\nfrom+id, from+id+stride, …", MIN, null],
];
let prev = null;
chain.forEach(([label, bg, why], i) => {
  const yy = Y3 + 100 + i * 160;
  const b = box(X4, yy, 400, 90, label, { bg });
  if (prev) arrow([[X4 + 200, yy - 70], [X4 + 200, yy]], prev, b, why);
  prev = b;
});
note("bloque de blockchain ≠ bloque de CUDA", X4, Y3 + 100 + 7 * 160);

const file = { type: "excalidraw", version: 2, source: "https://excalidraw.com", elements: els, appState: { viewBackgroundColor: "#ffffff", gridSize: null }, files: {} };
fs.writeFileSync(process.argv[2], JSON.stringify(file, null, 2));
fs.writeFileSync(process.argv[3], JSON.stringify({ type: "excalidraw/clipboard", elements: els, files: {} }));
console.log("ok", els.length, "elementos");
