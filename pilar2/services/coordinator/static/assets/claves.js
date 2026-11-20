// Claves y firmas de las cuentas, compartido por /cuenta y /unirse. Ed25519 con TweetNaCl (RFC 8032, mismo
// esquema que la librería cryptography del servidor). Se usa en vez de crypto.subtle porque el navegador solo lo
// habilita en páginas HTTPS. El archivo .key guarda la semilla de 32 bytes en hex, igual que
// pilar2/tools/cuenta.py, así sirve en los dos lados. La clave privada nunca sale del navegador.
const hex = (b) => [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
const unhex = (h) => new Uint8Array(h.match(/../g).map((x) => parseInt(x, 16)));
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const money = (c) => (c / 100).toLocaleString("es-AR", { minimumFractionDigits: 2 });

function say(id, text, ok) { const m = $(id); m.textContent = text; m.className = "msg " + (ok ? "ok" : "bad"); }
const firmar = (par, texto) => hex(nacl.sign.detached(new TextEncoder().encode(texto), par.secretKey));

async function post(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || data.error || ("error " + r.status));
  return data;
}

// Monto escrito por la persona ("12,5" o "12.50") a centavos, sin pasar por float (igual que el servidor).
function centavos(texto) {
  const m = String(texto).trim().replace(",", ".").match(/^(\d+)(?:\.(\d{1,2}))?$/);
  if (!m) throw new Error("Monto: un número con hasta 2 decimales.");
  const c = Number(m[1]) * 100 + Number((m[2] || "").padEnd(2, "0"));
  if (!(c > 0)) throw new Error("El monto tiene que ser mayor que 0.");
  return c;
}

// Crea una cuenta: genera las claves acá, registra la pública y descarga la privada (semilla) en <cuenta>.key.
async function crearCuenta(nombre) {
  const semilla = nacl.randomBytes(32);
  const par = nacl.sign.keyPair.fromSeed(semilla);
  const pub = hex(par.publicKey);
  await post("/accounts/register", { account: nombre, public_key: pub, signature: firmar(par, `register:${nombre}:${pub}`) });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([hex(semilla)], { type: "text/plain" }));
  a.download = nombre + ".key";
  a.click();
  return par;
}

// Abre una cuenta con su archivo .key: comprueba que esté registrada y que la clave sea la suya.
async function abrirCuenta(nombre, archivo) {
  const semilla = (await archivo.text()).trim();
  if (!/^[0-9a-f]{64}$/i.test(semilla)) throw new Error("Archivo de clave inválido.");
  const par = nacl.sign.keyPair.fromSeed(unhex(semilla));
  const info = await (await fetch("/balances/" + encodeURIComponent(nombre))).json();
  if (!info.registered) throw new Error(`La cuenta "${nombre}" no está registrada. Creala primero.`);
  if (info.public_key && info.public_key !== hex(par.publicKey)) throw new Error(`Esa clave no es la de "${nombre}".`);
  return par;
}

// Dirección del websocket del notifier (en local está en otro puerto; en GKE es /ws del mismo host).
async function notifierUrl() {
  try {
    const c = await (await fetch("/unirse/config")).json();
    if (c.notifier) return c.notifier;
  } catch { /* sin config: mismo host */ }
  return `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
}
