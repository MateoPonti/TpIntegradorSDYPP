// Genera docs/diagramas.excalidraw (abrir en excalidraw.com) y una versión para pegar con Ctrl+V.
// Uso: node diagramas.js <salida.excalidraw> <salida-clipboard.json>
const fs = require("fs");

const els = [];
let n = 0;
const id = () => "el" + (++n);
const seed = () => Math.floor(Math.random() * 2 ** 31);
const FS = 16;

function base(type, x, y, w, h, o = {}) {
  return {
    id: o.id || id(), type, x, y, width: w, height: h, angle: 0,
    strokeColor: o.stroke || "#1e1e1e", backgroundColor: o.bg || "transparent",
    fillStyle: "solid", strokeWidth: o.sw || 2, strokeStyle: o.dashed ? "dashed" : "solid",
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
  const r = base(o.ellipse ? "ellipse" : "rectangle", x, y, w, h, o);
  const t = textEl(label, x + w / 2, y + h / 2, { container: r.id, size: o.size });
  r.boundElements.push({ type: "text", id: t.id });
  els.push(r, t);
  return r;
}

// Zona (rectángulo de fondo con título arriba a la izquierda)
function zone(x, y, w, h, title, stroke, bg) {
  els.push(base("rectangle", x, y, w, h, { stroke, bg, dashed: true }));
  els.push(textEl(title, x + 16, y + 12, { left: true, top: true, size: 20, color: stroke }));
}

// Flecha por puntos absolutos, pegada a las cajas de origen y destino
function arrow(pts, from, to, label, o = {}) {
  const [x0, y0] = pts[0];
  const rel = pts.map(([x, y]) => [x - x0, y - y0]);
  const xs = rel.map((p) => p[0]), ys = rel.map((p) => p[1]);
  const a = {
    ...base("arrow", x0, y0, Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), { stroke: o.stroke }),
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

const BLUE = ["#1971c2", "#e7f5ff"], GREEN = ["#2f9e44", "#ebfbee"], ORANGE = ["#e8590c", "#fff4e6"];
const W = "#ffffff";

// ───────────── 1. Arquitectura completa ─────────────
els.push(textEl("1. Arquitectura completa", 20, -60, { left: true, top: true, size: 28 }));
zone(20, 120, 1360, 520, "GOOGLE CLOUD (GKE) — nunca mina", ...BLUE);
zone(20, 700, 640, 330, "FUERA DE GCP — k3s local", ...GREEN);
zone(700, 700, 680, 330, "TU PC — GTX 1050 Ti", ...ORANGE);

const U = box(80, 10, 300, 70, "USUARIOS / bench.py", { bg: "#f1f3f5" });
const C = box(80, 200, 300, 130, "COORDINATOR\nvalida tx, arma bloque,\npublica tareas, verifica\nnonce, sella bloque", { bg: W });
const MQ = box(560, 190, 260, 150, "RABBITMQ\nmining.tasks\nmining.results\nblock.events\nmining.dlq", { bg: "#fff3bf" });
const G = box(1000, 200, 300, 130, "GATEWAY\npuerta de los mineros\ntraduce HTTP <-> RabbitMQ", { bg: W });
const R = box(560, 450, 260, 130, "REDIS\ncadena, saldos,\ntx pendientes,\nmineros vivos, plan", { bg: "#ffe3e3" });
const P = box(80, 460, 300, 110, "POOL\nplanifica cada 15 s", { bg: W });
const K = box(40, 760, 240, 90, "KEDA\nautoescalador", { bg: W });
const CPU = box(40, 920, 240, 80, "miner-cpu x0..N", { bg: W });
const WR = box(860, 760, 300, 90, "WRAPPER (Python)\nkeep-alive, pide tarea,\nmanda resultado", { bg: W });
const GPU = box(860, 920, 300, 80, "miner_gpu (CUDA)\nsolo calcula", { bg: W });

arrow([[230, 80], [230, 200]], U, C, "POST /transactions");
arrow([[380, 240], [560, 240]], C, MQ, "publica tareas");
arrow([[560, 295], [380, 295]], MQ, C, "resultados");
arrow([[820, 240], [1000, 240]], MQ, G, "tareas");
arrow([[1000, 295], [820, 295]], G, MQ, "resultados");
arrow([[330, 330], [600, 450]], C, R, "tx, saldos, sellar,\nlee pool:plan", { both: true });
arrow([[380, 515], [560, 515]], P, R, "pool:plan\npool:scaling");
arrow([[1100, 330], [820, 500]], G, R, "leases,\nkeep-alive");
arrow([[1010, 760], [1170, 330]], WR, G, "HTTP + token");
arrow([[280, 960], [760, 690], [1040, 330]], CPU, G, "HTTP + token");
arrow([[1010, 850], [1010, 920]], WR, GPU, "--from --to --prefix");
arrow([[160, 760], [230, 570]], K, P, "GET /scaling");
arrow([[160, 850], [160, 920]], K, CPU, "sube/baja réplicas");

// ───────────── 2. Las dos salidas del pool ─────────────
const X2 = 1600;
els.push(textEl("2. Las dos salidas del pool", X2, -60, { left: true, top: true, size: 28 }));
const P2 = box(X2 + 150, 10, 340, 110, "POOL (cada 15 s)\ncuenta mineros vivos (worker:*)\nmira largo de mining.tasks", { bg: W });
const PLAN = box(X2, 230, 280, 90, "dificultad efectiva\n+ tamaño de rango", { bg: BLUE[1], stroke: BLUE[0] });
const SCAL = box(X2 + 360, 230, 280, 90, "cuántos mineros CPU\nhacen falta", { bg: GREEN[1], stroke: GREEN[0] });
const C2 = box(X2, 420, 280, 90, "COORDINATOR\nparte el bloque en tareas", { bg: W });
const K2 = box(X2 + 360, 420, 280, 90, "KEDA", { bg: W });
const M2 = box(X2 + 360, 600, 280, 80, "miner-cpu en k3s\nde 0 a N réplicas", { bg: W });
arrow([[X2 + 250, 120], [X2 + 140, 230]], P2, PLAN, "pool:plan");
arrow([[X2 + 390, 120], [X2 + 500, 230]], P2, SCAL, "pool:scaling");
arrow([[X2 + 140, 320], [X2 + 140, 420]], PLAN, C2, "lo lee de Redis");
arrow([[X2 + 500, 320], [X2 + 500, 420]], SCAL, K2, "GET /scaling");
arrow([[X2 + 500, 510], [X2 + 500, 600]], K2, M2, "escala");

// ───────────── 3. Del bloque de blockchain a los threads CUDA ─────────────
const X3 = 2400;
els.push(textEl("3. Del bloque de blockchain a los threads CUDA", X3, -60, { left: true, top: true, size: 28 }));
const steps = [
  ["BLOQUE BLOCKCHAIN #12\nespacio de nonces 0 … N", "#f1f3f5", null],
  ["TAREAS\nrangos de nonces", BLUE[1], "lo decide el POOL"],
  ["TRAMOS (--chunk)\npermiten abandonar con 410", W, "lo decide el WRAPPER"],
  ["1 ejecución de miner_gpu", W, null],
  ["GRID\n6 / 12 / 48 / 192 bloques CUDA", ORANGE[1], "se elige midiendo"],
  ["BLOQUE CUDA\n64 / 128 / 256 / 512 threads", ORANGE[1], "se elige midiendo"],
  ["THREAD\nprueba from+id, from+id+stride, …", ORANGE[1], null],
];
let prev = null;
steps.forEach(([label, bg, why], i) => {
  const y = 10 + i * 140;
  const b = box(X3, y, 380, 80, label, { bg });
  if (prev) arrow([[X3 + 190, y - 60], [X3 + 190, y]], prev, b, why);
  prev = b;
});
els.push(textEl("Ojo: bloque de blockchain ≠ bloque de CUDA", X3, 1010, { left: true, top: true, size: 18, color: "#c92a2a" }));

const file = { type: "excalidraw", version: 2, source: "https://excalidraw.com", elements: els, appState: { viewBackgroundColor: "#ffffff", gridSize: null }, files: {} };
fs.writeFileSync(process.argv[2], JSON.stringify(file, null, 2));
fs.writeFileSync(process.argv[3], JSON.stringify({ type: "excalidraw/clipboard", elements: els, files: {} }));
console.log("ok", els.length, "elementos");
