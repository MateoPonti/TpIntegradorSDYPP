"""Gateway HTTP: la única puerta de los mineros (que están FUERA de GCP, detrás de NAT).

  GET  /tasks/next             saca una tarea de mining.tasks y se la presta al minero (204 si no hay)
  POST /tasks/{task_id}/renew  "¿sigo?": renueva el préstamo, o 410 si el bloque ya se cerró o está en cuarentena
  POST /tasks/{task_id}/release "no pude": la tarea vuelve a la cola al instante (se le rompió el motor).
                               Con ?reason=shutdown es un minero apagándose: no cuenta como falla (C5)
  POST /results                publica el resultado en mining.results
  POST /keepalive              SET worker:<id> EX 15 (Redis no se expone a Internet)
  GET  /scaling                señal del pool para KEDA del k3s
  POST /workers/register       alta pública de un worker (página /unirse): token propio atado a una cuenta
  WS   /ws/miners              avisos al minero: "hay tareas" y "bloque sellado" (punto 4 del contrato)

Tokens: el MINER_TOKEN compartido queda para los pods del k3s. Un worker dado de alta en /unirse usa su
token propio: su miner_id (cuenta.worker) y su cuenta de cobro salen del token, no de lo que mande, y si
falla un canario el token se revoca.

Préstamos (decisiones §4): basic_get + ack inmediato y lease:<task_id> en Redis. El reaper
(una sola réplica, con lock) devuelve a la cola lo vencido; al 3er intento va a la DLQ.

Decidido el 13/11 (decisiones §8), todo dentro del gateway, sin pasar por RabbitMQ ni por el coordinator:
  * Calibración: la primera tarea de cada minero es una prueba fija (base fija, prefijo imposible,
    tamaño según GPU o CPU). Con eso se guarda su hashrate inicial y una clase.
  * Hashrate real: cada resultado guarda hashes, segundos del motor y segundos de punta a punta.
  * Canarios: 1 de cada CANARY_EVERY tareas es un rango de un bloque ya sellado que contiene su nonce.
    Si el minero contesta "no encontré", está roto o miente: cuarentena, su trabajo de este bloque no
    cobra y sus rangos "no encontré" se vuelven a publicar para otros mineros.
  * Cuarentena: también con QUARANTINE_AFTER_EXPIRED préstamos vencidos seguidos.

Decidido el 20/11 (punto 4 del contrato):
  * En prueba: un worker de alta pública empieza con más canarios (1 de cada TRIAL_CANARY_EVERY tareas) y sin
    premio hasta pasar TRIAL_CANARIES canarios. Los pods del k3s (token compartido) no pasan por la prueba.
  * Límite de MAX_WORKERS_PER_ACCOUNT altas por cuenta (los revocados siguen contando).
  * Shares: el prefijo de las shares lo fija la tarea (no el minero) y solo se reenvían las de un préstamo.
  * WebSocket: solo avisa; las tareas se siguen pidiendo y entregando por HTTP.
  * Hasta MAX_LEASES_PER_MINER (2) préstamos abiertos por minero: uno que pide sin parar no se lleva la tanda.
  * La cuarentena crece (5, 10, 20 min… hasta 1 h) con cada una en la última hora, y con token propio la 3.ª
    revoca el token: pedir tareas y dejarlas vencer a propósito sale caro.
"""
import asyncio
import contextlib
import hashlib
import hmac
import json
import random
import re
import secrets
import time
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Counter, Gauge, Histogram

from tpi_common import firmas, keys, pow, quarantine, rabbit, ratelimit, store, vardiff
from tpi_common.config import CHAIN_ID, POD_NAME, WORKER_TTL_SECONDS, env_float, env_int, env_str
from tpi_common.logs import log, setup_logging
from tpi_common.web import install_common_routes

logger = setup_logging("gateway")
MINER_TOKEN = env_str("MINER_TOKEN", "")
SCALING_TOKEN = env_str("SCALING_TOKEN", "")
WORKER_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")
LEASE_SECONDS = env_int("TASK_LEASE_SECONDS", 60)
MAX_ATTEMPTS = env_int("TASK_MAX_ATTEMPTS", 3)
REAPER_PERIOD = env_int("REAPER_PERIOD_SECONDS", 5)
WORKERS_PER_IP_HOUR = env_int("WORKERS_PER_IP_HOUR", 20)  # altas de workers por IP y por hora (B1.4, 26/11)
CALIB_RANGE = {"gpu": env_int("CALIB_RANGE_GPU", 1_000_000_000), "cpu": env_int("CALIB_RANGE_CPU", 2_000_000)}
CALIB_BASE = hashlib.md5(b"calibracion").hexdigest() + hashlib.sha256(b"calibracion").hexdigest()
CALIB_PREFIX = "f" * 20  # no aparece nunca: el minero recorre el rango entero
CANARY_EVERY = env_int("CANARY_EVERY", 20)
QUARANTINE_AFTER = env_int("QUARANTINE_AFTER_EXPIRED", 3)
QUARANTINE_SECONDS = env_int("QUARANTINE_SECONDS", 300)
QUARANTINE_MAX_SECONDS = env_int("QUARANTINE_MAX_SECONDS", 3600)  # tope de la cuarentena que crece
REVOKE_AFTER_STRIKES = env_int("REVOKE_AFTER_STRIKES", 3)  # con token propio: a la 3.ª cuarentena en 1 h, sin token
RESULTS_KEPT = 20
MEASURE_WINDOW = env_float("HASHRATE_WINDOW_SECONDS", 300)  # la misma ventana que usa el pool
TRIAL_CANARY_EVERY = env_int("TRIAL_CANARY_EVERY", 5)
TRIAL_CANARIES = env_int("TRIAL_CANARIES", 3)
MAX_WORKERS_PER_ACCOUNT = env_int("MAX_WORKERS_PER_ACCOUNT", 10)
MAX_LEASES_PER_MINER = env_int("MAX_LEASES_PER_MINER", 2)  # la que mina y una de colchón
NOTICE_PERIOD = float(env_str("WS_NOTICE_SECONDS", "1"))
MAX_WS = env_int("WS_MAX_MINERS", 500)
# A3-f (26/11): todo resultado va firmado con la clave que el worker ató en su authorize. En 0 solo para una
# transición (agentes viejos sin clave); el valor del TP es 1.
REQUIRE_SIGNED_RESULTS = env_str("REQUIRE_SIGNED_RESULTS", "1") == "1"
WORKER_KEY_TTL = 7 * 86400  # la clave atada vence a los 7 días sin authorize (un pod que no vuelve no deja basura)
AUTHORIZE_TIMEOUT = 10.0  # segundos para contestar el desafío del WebSocket
PUSH_POLL = env_float("WS_PUSH_POLL_SECONDS", 0.5)  # cada cuánto mira si el worker ya entregó (sin aviso local)

LEASED = Counter("tasks_leased_total", "Tareas prestadas a mineros", ["kind"])
DROPPED = Counter("tasks_dropped_stale_total", "Tareas de bloques viejos descartadas al sacarlas")
EXPIRED = Counter("leases_expired_total", "Préstamos vencidos (el minero no contestó a tiempo)")
REQUEUED = Counter("tasks_requeued_total", "Préstamos vencidos devueltos a la cola")
DLQ = Counter("tasks_dlq_total", "Tareas mandadas a la DLQ por agotar intentos")
RESULTS = Counter("results_forwarded_total", "Resultados publicados", ["found"])
QUEUE_TO_LEASE = Histogram("task_queue_to_lease_seconds", "Espera desde publicación hasta préstamo",
                           ["kind"], buckets=(.01, .05, .1, .25, .5, 1, 2, 5, 10, 30, 60, 120))
KEEPALIVES = Counter("keepalives_total", "Keep-alives recibidos", ["kind"])
CALIBRATIONS = Counter("calibrations_total", "Tareas de calibración terminadas", ["kind", "class"])
CANARIES = Counter("canaries_total", "Canarios resueltos", ["result"])
QUARANTINED = Counter("quarantined_total", "Mineros puestos en cuarentena", ["reason"])
REPUBLISHED = Counter("tasks_republished_total", "Rangos re-publicados de mineros en cuarentena")
RELEASED = Counter("tasks_released_total", "Tareas devueltas por el minero", ["reason"])
REVOKED = Counter("tokens_revoked_total", "Tokens de workers revocados", ["reason"])
LIMITED = Counter("tasks_limited_total", "Pedidos de tarea rechazados porque el minero ya tenía el máximo prestado")
TRIAL_PASSED = Counter("workers_trial_passed_total", "Workers que pasaron la prueba")
WS_CONNECTED = Gauge("gateway_ws_miners", "Mineros conectados por WebSocket a esta réplica")
WS_NOTICES = Counter("gateway_ws_notices_total", "Avisos mandados por WebSocket", ["type"])
AUTHORIZED = Counter("miners_authorize_total", "authorize de workers (desafío firmado)", ["result"])
PUSHED = Counter("tasks_pushed_total", "Tareas empujadas por WebSocket (mining.notify)", ["kind"])

state: dict = {"solved": set(), "sockets": set()}

# KEYS: miner:<id>:leases (ZSET de task_id y reservas "r:<uuid>" -> hora)   ARGV: máximo, reserva, ahora
# Limpia lo que ya no está prestado (el lease se borra al entregar, devolver, recibir 410 o vencer) y las reservas
# de más de 10 s; si quedan menos que el máximo, agrega la reserva. Un solo Lua: pedidos simultáneos no se cuelan.
SLOT_LUA = """
local now = tonumber(ARGV[3])
local items = redis.call('ZRANGE', KEYS[1], 0, -1, 'WITHSCORES')
for i = 1, #items, 2 do
  local m, t = items[i], tonumber(items[i + 1])
  if string.sub(m, 1, 2) == 'r:' then
    if now - t > 10 then redis.call('ZREM', KEYS[1], m) end
  elseif redis.call('EXISTS', 'lease:' .. m) == 0 then
    redis.call('ZREM', KEYS[1], m)
  end
end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
redis.call('ZADD', KEYS[1], now, ARGV[2])
redis.call('EXPIRE', KEYS[1], 3600)
return 1
"""

# KEYS: workers:of:<cuenta>   ARGV: miner_id, máximo. Agrega el worker si la cuenta no llegó al máximo.
LIMIT_LUA = """
if redis.call('SISMEMBER', KEYS[1], ARGV[1]) == 1 then return 1 end
if redis.call('SCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
redis.call('SADD', KEYS[1], ARGV[1])
return 1
"""


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def auth(authorization: str = Header(default="")) -> dict | None:
    """Token compartido (pods del k3s) -> None. Token propio (alta pública) -> {miner_id, account}."""
    if MINER_TOKEN and hmac.compare_digest(authorization, f"Bearer {MINER_TOKEN}"):
        return None
    if authorization.startswith("Bearer ") and (raw := await state["r"].get(keys.worker_token(token_hash(authorization[7:])))):
        return json.loads(raw)
    raise HTTPException(401, "token inválido")


async def current_block() -> dict | None:
    raw = await state["r"].get(keys.MINING_CURRENT)
    return json.loads(raw) if raw else None


async def current_block_id() -> str | None:
    cur = await current_block()
    return cur["block_id"] if cur else None


async def revoke_token(miner_id: str, why: str) -> None:
    """Alta pública: se borra el token del worker. Para volver tiene que darse de alta con otro nombre (y la cuenta
    tiene un máximo de altas, contando los revocados)."""
    if h := await state["r"].get(keys.worker_token_of(miner_id)):
        if await state["r"].delete(keys.worker_token(h)):
            REVOKED.labels(why).inc()
            log(logger, "token revocado", miner_id=miner_id, motivo=why)


async def put_in_quarantine(miner_id: str, reason: str, extra: list[tuple[int, int]] = (),
                            distrust: bool = True) -> None:
    """Cuarentena (tpi_common.quarantine). `distrust`: además no cobra el bloque y se re-publican sus rangos.
    Crece con cada una en la última hora (20/11), y a la REVOKE_AFTER_STRIKES-ésima se revoca el token propio:
    así no conviene pedir tareas y dejarlas vencer a propósito."""
    strikes, seconds = await quarantine.strike(state["r"], miner_id, QUARANTINE_SECONDS, QUARANTINE_MAX_SECONDS)
    n = await quarantine.penalize(state["r"], state["exchange"], await current_block(), miner_id, reason,
                                  seconds, extra, distrust)
    QUARANTINED.labels(reason).inc()
    REPUBLISHED.inc(n)
    log(logger, "minero en cuarentena", miner_id=miner_id, reason=reason, seconds=seconds, vez=strikes,
        rangos_republicados=n)
    if strikes >= REVOKE_AFTER_STRIKES:
        await revoke_token(miner_id, "cuarentenas")


async def give_back(lease: dict, why: str, count_attempt: bool = True) -> None:
    """La tarea vuelve a la cola con un intento más (préstamo vencido o devuelta por el minero).
    Al MAX_ATTEMPTS-ésimo va a la DLQ. Las de calibración y los canarios no vuelven.

    `count_attempt=False` la devuelve sin gastar un intento. Lo usa el release con reason=shutdown
    (C5): con TASK_MAX_ATTEMPTS=3, tres bajadas de escala sobre el mismo rango lo mandaban a la DLQ
    —que no se reprocesa sola— sin que ningún minero hubiera fallado, y ese rango no se minaba más."""
    task = lease["task"]
    if lease.get("special") or task["block_id"] != await current_block_id():
        return  # bloque ya sellado o rearmado: no hace falta re-minar ese rango
    task["attempts"] = task.get("attempts", 0) + (1 if count_attempt else 0)
    if task["attempts"] >= MAX_ATTEMPTS:
        await state["dlx"].publish(rabbit.message(task), routing_key="dlq")
        DLQ.inc()
        log(logger, "tarea a la DLQ", task_id=task["task_id"], attempts=task["attempts"])
    else:
        task["published_at"] = time.time()
        await state["exchange"].publish(rabbit.message(task), routing_key="task.any")
        REQUEUED.inc()
        log(logger, f"{why}, tarea devuelta", task_id=task["task_id"], attempts=task["attempts"])


async def reap_once() -> int:
    """Una pasada del reaper: devuelve a la cola los préstamos vencidos. Devuelve cuántos devolvió."""
    r, n = state["r"], 0
    if not await r.set(keys.REAPER_LOCK, POD_NAME, nx=True, ex=REAPER_PERIOD * 2):
        return 0  # esta vuelta la hace otra réplica
    for task_id in await r.zrangebyscore(keys.LEASE_DEADLINES, 0, time.time()):
        if not await r.zrem(keys.LEASE_DEADLINES, task_id):
            continue  # otro lo tomó
        raw = await r.getdel(keys.lease(task_id))
        if raw is None:
            continue
        lease = json.loads(raw)
        EXPIRED.inc()
        await count_expired(lease["miner_id"])
        await give_back(lease, "préstamo vencido")
        n += 1
    return n


async def reaper_loop() -> None:
    while True:
        await asyncio.sleep(REAPER_PERIOD)
        try:
            await reap_once()
        except Exception:  # noqa: BLE001
            logger.exception("error en el reaper")


async def count_expired(miner_id: str) -> None:
    """Préstamos vencidos seguidos. El pool usa el `ts` para no contar el hashrate de un minero zombie."""
    r = state["r"]
    prev = json.loads(await r.get(keys.miner_expired(miner_id)) or '{"streak": 0}')
    streak = prev["streak"] + 1
    await r.set(keys.miner_expired(miner_id), json.dumps({"streak": streak, "ts": time.time()}), ex=3600)
    if streak >= QUARANTINE_AFTER and not await r.exists(keys.quarantine(miner_id)):
        await put_in_quarantine(miner_id, "vencidos", distrust=False)  # se colgó, no mintió


async def broadcast(msg: dict) -> None:
    """Manda un aviso a todos los mineros conectados a esta réplica. Uno que no contesta se descarta."""
    for conn in state.get("workers_ws", {}).values():
        conn["wake"].set()  # hay novedades: que el push loop mire ya
    for ws in list(state["sockets"]):
        try:
            await asyncio.wait_for(ws.send_json(msg), 2)
        except Exception:  # noqa: BLE001 - conexión muerta o cliente lento
            state["sockets"].discard(ws)
    WS_NOTICES.labels(msg["type"]).inc()


async def notice_loop() -> None:
    """Cada NOTICE_PERIOD, si hay tareas en la cola y mineros conectados, avisa "tareas". Los que están minando
    lo ignoran; los que esperan piden una por HTTP al instante (sin aviso esperan hasta 15 s)."""
    while True:
        await asyncio.sleep(NOTICE_PERIOD)
        try:
            if state["sockets"] and await rabbit.queue_length(state["channel"], rabbit.TASKS_QUEUE) > 0:
                await broadcast({"type": "tareas"})
        except Exception:  # noqa: BLE001
            logger.exception("error en el loop de avisos")


async def on_event(message) -> None:
    async with message.process():
        ev = json.loads(message.body)
        state["solved"].add(ev["block_id"])
        log(logger, "block.solved recibido", block_id=ev["block_id"])
        await broadcast({"type": "block.solved", "block_id": ev["block_id"]})


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    r = await store.connect(logger)
    conn = await rabbit.connect(logger)
    channel = await conn.channel()
    exchange, dlx, tasks, _ = await rabbit.declare_topology(channel)
    events = await rabbit.declare_events_queue(channel, exchange, POD_NAME)
    await events.consume(on_event)
    state.update(r=r, conn=conn, channel=channel, exchange=exchange, dlx=dlx, tasks=tasks, get_lock=asyncio.Lock(),
                 limit=r.register_script(LIMIT_LUA), slot=r.register_script(SLOT_LUA))
    reaper = asyncio.create_task(reaper_loop())
    notices = asyncio.create_task(notice_loop())
    if not MINER_TOKEN:
        log(logger, "MINER_TOKEN vacío: se rechazan todos los mineros")
    log(logger, "gateway listo", pod=POD_NAME)
    yield
    reaper.cancel()
    notices.cancel()
    await conn.close()
    await r.aclose()


app = FastAPI(title="TPI gateway", lifespan=lifespan)
# La página /unirse (servida por el coordinator, otro origen) hace el alta desde el navegador. Sin el header
# Authorization permitido, ningún otro endpoint se puede usar desde otra página.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["POST"], allow_headers=["Content-Type"])
install_common_routes(app)


async def lend(task: dict, miner_id: str, kind: str, special: str | None = None, extra: dict | None = None,
               share_len: int | None = None) -> dict:
    if share_len and task.get("share_prefix") and special is None:
        # Vardiff (A3-d, 26/11): shares de la dificultad de este minero. El prefijo del bloque es el mismo; el peso
        # de cada share (16^largo) compensa, así que el premio no cambia.
        task["share_prefix"] = task["prefix"][:share_len]
    lease = {"task": task, "miner_id": miner_id, "kind": kind, "since": time.time(), "special": special,
             **(extra or {})}
    await state["r"].set(keys.lease(task["task_id"]), json.dumps(lease))
    await state["r"].zadd(keys.LEASE_DEADLINES, {task["task_id"]: time.time() + LEASE_SECONDS})
    LEASED.labels(kind).inc()
    published = task.get("published_at")
    if special is None and isinstance(published, (int, float)) and 0 <= lease["since"] - published < float("inf"):
        QUEUE_TO_LEASE.labels(kind if kind in ("cpu", "gpu") else "unknown").observe(lease["since"] - published)
    # Es metadata de observabilidad interna: exponerla permitiría reconocer un canario por su espera nula.
    return {**{k: v for k, v in task.items() if k != "published_at"}, "lease_seconds": LEASE_SECONDS}


def calibration_task(kind: str) -> dict:
    return {"v": 1, "task_id": uuid.uuid4().hex, "block_id": "calib", "base": CALIB_BASE, "prefix": CALIB_PREFIX,
            "from": 0, "to": CALIB_RANGE.get(kind, CALIB_RANGE["cpu"]) - 1, "attempts": 0}


def make_canary(cur: dict, ranges: list[dict], miner_id: str,
                share_len: int | None = None) -> tuple[dict, list[int]] | None:
    """Canario (B1.1, 26/11): un rango del bloque **actual** que otro minero ya resolvió y cuyas shares ya verificó
    el coordinator con MD5. Para el minero es una tarea más: mismo block_id, base, prefijo, share_prefix y tamaño
    (mismo formato que blocks.new_task). No sabe qué rangos les tocaron a otros, y la respuesta no es pública: las
    shares no salen en GET /blocks (antes el canario era un bloque ya sellado, con el nonce a la vista).
    Con Vardiff (`share_len`), el canario lleva el largo de share de ese minero, como sus tareas: sirven los rangos
    con shares más cortas o iguales (una share de largo 4 también es de largo 3) y se filtran a ese largo.
    Devuelve (tarea, shares conocidas) o None si no hay ningún rango que sirva."""
    options = []
    for v in ranges:
        sp = v.get("share_prefix") or "-"
        if v.get("worker") == miner_id or not v.get("shares") or not cur["prefix"].startswith(sp):
            continue
        if share_len is None:
            options.append((v, sp, [int(n) for n in v["shares"]]))
        elif len(sp) <= share_len:
            mine = cur["prefix"][:share_len]
            known = [int(n) for n in v["shares"] if pow.md5_hex(cur["base"], int(n)).startswith(mine)]
            if known:
                options.append((v, mine, known))
    if not options:
        return None
    v, sp, known = random.choice(options)
    task = {"v": 1, "task_id": uuid.uuid4().hex, "block_id": cur["block_id"], "base": cur["base"],
            "prefix": cur["prefix"], "from": int(v["from"]), "to": int(v["to"]), "attempts": 0,
            "share_prefix": sp, "published_at": time.time()}
    return task, known


def canary_passed(known: list[int], got: list[int]) -> bool:
    """Pasa si devolvió todas las shares conocidas (`got` ya viene verificado con MD5). Si el original mandó el
    máximo, pudo haber cortado y un GPU devuelve otras: alcanza con el máximo de shares válidas."""
    known_set, got_set = set(known), set(got)
    if len(known_set) >= pow.MAX_SHARES:
        return len(got_set) >= pow.MAX_SHARES
    return known_set <= got_set


async def canary_task(cur: dict, miner_id: str, share_len: int | None = None) -> tuple[dict, list[int]] | None:
    raws = await state["r"].lrange(keys.block_verified_ranges(cur["index"]), 0, -1)
    return make_canary(cur, [json.loads(x) for x in raws], miner_id, share_len)


async def vardiff_len(miner_id: str, cur: dict) -> int | None:
    """Largo de share de este minero (Vardiff), con su hashrate **medido** por shares (B1.2). None si todavía no hay
    medición: queda el share_prefix que puso el coordinator (~32 shares por tarea)."""
    results = [json.loads(x) for x in await state["r"].lrange(keys.miner_results(miner_id), 0, -1)]
    rate = vardiff.shares_rate(results, time.time(), MEASURE_WINDOW)
    if rate is None:
        return None
    return vardiff.share_len(rate, len(cur["prefix"]), int(cur["range_size"]))


async def authorize_worker(miner_id: str, pubkey, challenge: str, signature) -> str | None:
    """authorize (A3-f, 26/11): el worker firma el desafío con su clave Ed25519. La primera vez la clave queda atada al
    worker (TOFU, como known_hosts de SSH: el token ya dice quién es; la clave prueba que es el mismo de antes y firma
    sus resultados). Devuelve None si está bien o el motivo del rechazo."""
    if not isinstance(pubkey, str) or len(pubkey) != 64:
        return "pubkey: 32 bytes en hex"
    if not firmas.verify(pubkey, firmas.authorize_message(miner_id, challenge), signature):
        return "firma del desafío inválida"
    r = state["r"]
    key = keys.worker_pubkey(miner_id)
    if not await r.set(key, pubkey.lower(), nx=True, ex=WORKER_KEY_TTL) and await r.get(key) != pubkey.lower():
        return "ese worker ya tiene otra clave (si se perdió, dar de alta otro worker)"
    await r.expire(key, WORKER_KEY_TTL)
    await r.delete(keys.miner_draining(miner_id))  # C5: volvió a arrancar (p. ej. la GPU de la PC tras un Ctrl+C)
    return None


@app.get("/miners/challenge")
async def miners_challenge(miner_id: str, ident: dict | None = Depends(auth)):
    """Desafío para autorizarse por HTTP (fallback sin WebSocket). Un solo uso y 60 s."""
    if ident:
        miner_id = ident["miner_id"]
    challenge = secrets.token_hex(16)
    await state["r"].set(keys.miner_challenge(challenge), miner_id, ex=60)
    return {"challenge": challenge, "chain_id": CHAIN_ID}


@app.post("/miners/authorize")
async def miners_authorize(request: Request, ident: dict | None = Depends(auth)):
    body = await request.json()
    miner_id = ident["miner_id"] if ident else body.get("miner_id")
    challenge = body.get("challenge")
    if not isinstance(miner_id, str) or not isinstance(challenge, str):
        raise HTTPException(400, "se espera {miner_id, pubkey, challenge, signature}")
    if await state["r"].getdel(keys.miner_challenge(challenge)) != miner_id:
        raise HTTPException(401, "desafío inexistente, vencido o de otro minero")
    if reason := await authorize_worker(miner_id, body.get("pubkey"), challenge, body.get("signature")):
        AUTHORIZED.labels("rechazado").inc()
        raise HTTPException(401, reason)
    AUTHORIZED.labels("ok").inc()
    return {"authorized": True}


async def check_result_signature(res: dict) -> None:
    """Resultados firmados (A3-f): con la clave atada al worker en su authorize. 401 si falta o no verifica."""
    if not REQUIRE_SIGNED_RESULTS:
        return
    pubkey = await state["r"].get(keys.worker_pubkey(res["worker"]))
    if pubkey is None:
        raise HTTPException(401, "worker sin authorize: primero mining.authorize (o POST /miners/authorize)")
    msg = firmas.result_message(res["task_id"], bool(res["found"]), res.get("nonce") if res["found"] else None,
                                res.get("shares") if isinstance(res.get("shares"), list) else [])
    if not firmas.verify(pubkey, msg, res.get("signature")):
        raise HTTPException(401, "firma del resultado inválida")


def lease_owner_ok(lease: dict, ident: dict | None, miner_id: str | None) -> bool:
    """El préstamo es de quien llama (B1.3, 26/11). Con token propio, el nombre sale del token (no se puede mentir);
    con el token compartido de los pods del k3s, del miner_id que manda el agente."""
    who = ident["miner_id"] if ident else miner_id
    return bool(who) and who == lease.get("miner_id")


@app.get("/tasks/next")
async def next_task(miner_id: str, kind: str = "cpu", ident: dict | None = Depends(auth)):
    if ident:  # worker con token propio: el nombre sale del token
        miner_id = ident["miner_id"]
    return await take_task(miner_id, kind)


async def take_task(miner_id: str, kind: str) -> dict | Response:
    """Presta una tarea al minero (o 204). La usan GET /tasks/next (pull) y el WebSocket (push, mining.notify)."""
    r = state["r"]
    if await r.exists(keys.quarantine(miner_id), keys.miner_draining(miner_id)):
        return Response(status_code=204)
    # Límite de préstamos abiertos por minero (20/11): sin esto, uno que pide sin parar se lleva la tanda entera y
    # el bloque se demora hasta que vencen sus préstamos. Se reserva el lugar antes de sacar la tarea (Lua), así
    # muchos pedidos al mismo tiempo no pasan todos el control.
    slot, leases = "r:" + uuid.uuid4().hex, keys.miner_leases(miner_id)
    if not await state["slot"](keys=[leases], args=[MAX_LEASES_PER_MINER, slot, time.time()]):
        LIMITED.inc()
        return Response(status_code=204)
    try:
        out = await pick_task(miner_id, kind)
        if isinstance(out, dict):  # primero se anota el préstamo y recién después se suelta la reserva
            await r.zadd(leases, {out["task_id"]: time.time()})
            await r.expire(leases, 3600)
    finally:
        await r.zrem(leases, slot)
    return out


async def pick_task(miner_id: str, kind: str) -> dict | Response:
    r = state["r"]
    if not await r.exists(keys.miner_calibration(miner_id)):
        return await lend(calibration_task(kind), miner_id, kind, special="calib")
    cur = await current_block()
    if await r.exists(keys.worker_trial(miner_id)):  # en prueba: un canario cada TRIAL_CANARY_EVERY tareas
        canary_due = await r.incr(keys.miner_tasks(miner_id)) % TRIAL_CANARY_EVERY == 0
    else:
        canary_due = CANARY_EVERY > 0 and await r.incr(keys.CANARY_COUNTER) % CANARY_EVERY == 0
    share_len = await vardiff_len(miner_id, cur) if cur else None
    if cur and canary_due:
        found = await canary_task(cur, miner_id, share_len)
        if found:
            canary, known = found
            return await lend(canary, miner_id, kind, special="canary", extra={"canary_shares": known})
    current = cur["block_id"] if cur else None
    for _ in range(50):  # descarta de una las tareas de bloques viejos
        async with state["get_lock"]:
            msg = await state["tasks"].get(no_ack=False, fail=False)
        if msg is None:
            return Response(status_code=204)
        task = json.loads(msg.body)
        if task["block_id"] != current or task["block_id"] in state["solved"]:
            await msg.ack()
            DROPPED.inc()
            continue
        out = await lend(task, miner_id, kind, share_len=share_len)
        await msg.ack()  # desde acá la tarea vive en el lease, no en RabbitMQ
        return out
    return Response(status_code=204)


@app.post("/tasks/{task_id}/renew")
async def renew(task_id: str, miner_id: str | None = None, ident: dict | None = Depends(auth)):
    """El wrapper pregunta entre tramo y tramo. 410 = el bloque se cerró: abandonar. 409 = no es tuyo."""
    r = state["r"]
    raw = await r.get(keys.lease(task_id))
    if raw is None:
        raise HTTPException(410, "préstamo inexistente o vencido")
    lease = json.loads(raw)
    if not lease_owner_ok(lease, ident, miner_id):
        raise HTTPException(409, "ese préstamo es de otro minero")
    block_id = lease["task"]["block_id"]
    if lease.get("special") != "calib" and (block_id in state["solved"] or block_id != await current_block_id()):
        await r.zrem(keys.LEASE_DEADLINES, task_id)
        await r.delete(keys.lease(task_id))
        raise HTTPException(410, "el bloque ya se cerró")
    if await r.exists(keys.quarantine(lease["miner_id"])):
        # En cuarentena no sigue con lo que tenía: la tarea vuelve a la cola para otro minero.
        await r.zrem(keys.LEASE_DEADLINES, task_id)
        await r.delete(keys.lease(task_id))
        await give_back(lease, "minero en cuarentena")
        raise HTTPException(410, "minero en cuarentena")
    await r.zadd(keys.LEASE_DEADLINES, {task_id: time.time() + LEASE_SECONDS}, xx=True)
    return {"lease_seconds": LEASE_SECONDS}


@app.post("/tasks/{task_id}/release")
async def release(task_id: str, miner_id: str | None = None, ident: dict | None = Depends(auth),
                  reason: str | None = None):
    """El minero devuelve la tarea. Vuelve a la cola al instante, sin esperar los 60 s del préstamo.

    Sin `reason` (o con cualquier otro) es "no pude": se le rompió el motor y cuenta como una falla
    (a las 3 seguidas, cuarentena). Con `reason=shutdown` (C5, 27/11) es un minero que se está
    apagando porque KEDA bajó las réplicas: devuelve el rango de onda, así que **no** cuenta como
    falla. Si contara, bajar de 8 a 0 mineros dejaría a los 8 en cuarentena y el próximo bloque
    arrancaría sin nadie.

    `reason` va último en la firma a propósito, para que las llamadas que ya existían
    (`release(task_id, miner_id, ident)`) sigan andando por posición. A FastAPI el orden de los
    parámetros no le importa: lo lee de la query igual."""
    r = state["r"]
    raw = await r.get(keys.lease(task_id))
    if raw is None:
        raise HTTPException(410, "préstamo inexistente o vencido")
    if not lease_owner_ok(json.loads(raw), ident, miner_id):
        raise HTTPException(409, "ese préstamo es de otro minero")
    apagandose = reason == "shutdown"
    if apagandose:
        # Antes de soltar el préstamo: el push loop (en cualquier réplica) le manda otra tarea apenas ve que el
        # préstamo ya no existe, y esa quedaría colgada en un pod que se está yendo hasta que venza.
        await r.set(keys.miner_draining(json.loads(raw)["miner_id"]), 1, ex=LEASE_SECONDS)
    raw = await r.getdel(keys.lease(task_id))
    await r.zrem(keys.LEASE_DEADLINES, task_id)
    if raw is None:
        raise HTTPException(410, "préstamo inexistente o vencido")
    lease = json.loads(raw)
    RELEASED.labels("shutdown" if apagandose else "falla").inc()
    if not apagandose:
        await count_expired(lease["miner_id"])
    await give_back(lease, "minero apagándose" if apagandose else "tarea devuelta por el minero",
                    count_attempt=not apagandose)
    return {"released": True, "reason": reason or "falla"}


async def finish_calibration(lease: dict, res: dict) -> None:
    hashes = int(res.get("hashes") or 0)
    seconds = float(res.get("engine_seconds") or res.get("seconds") or 0)
    rate = hashes / seconds if seconds > 0 else 0.0
    value = {"hashrate": rate, "class": vardiff.classify(rate), "kind": lease["kind"], "hashes": hashes,
             "seconds": round(seconds, 3), "ts": time.time()}
    await state["r"].set(keys.miner_calibration(lease["miner_id"]), json.dumps(value), ex=86400)
    CALIBRATIONS.labels(lease["kind"], value["class"]).inc()
    log(logger, "minero calibrado", miner_id=lease["miner_id"], kind=lease["kind"], hashrate=round(rate),
        clase=value["class"])


async def finish_canary(lease: dict, res: dict) -> None:
    task, miner = lease["task"], lease["miner_id"]
    got = pow.valid_shares(task["base"], task["share_prefix"], int(task["from"]), int(task["to"]), res.get("shares"))
    ok = canary_passed(lease.get("canary_shares") or [], got)
    CANARIES.labels("ok" if ok else "falla").inc()
    if res.get("found"):
        # El canario es un rango del bloque actual: si el otro minero dijo "no encontré" y acá aparece la solución,
        # es la del bloque. Se reenvía como un resultado más (el coordinator la verifica con MD5).
        await state["exchange"].publish(rabbit.message({"v": 1, **res, "from": task["from"], "to": task["to"],
                                                        "block_id": task["block_id"], "worker": miner}),
                                        routing_key=f"result.{lease['kind']}")
    if ok:
        if await state["r"].exists(keys.worker_trial(miner)) and await state["r"].incr(keys.worker_trial(miner)) >= TRIAL_CANARIES:
            await state["r"].delete(keys.worker_trial(miner), keys.miner_tasks(miner))
            TRIAL_PASSED.inc()
            log(logger, "worker pasó la prueba: ya cobra", miner_id=miner)
        return
    log(logger, "canario fallado", miner_id=miner, task_from=task["from"], task_to=task["to"],
        esperadas=len(lease.get("canary_shares") or []), devueltas=len(got))
    await put_in_quarantine(miner, "canario")  # no cobra el bloque y sus "no encontré" van a otros
    await revoke_token(miner, "canario")  # alta pública: mentir le cuesta el token


async def record_result(lease: dict, res: dict) -> None:
    """Hashrate real: el motor informa sus segundos; el de punta a punta lo mide el gateway."""
    r, miner = state["r"], lease["miner_id"]
    task = lease["task"]
    sp = task.get("share_prefix") or ""
    # B1.2 (26/11): el trabajo que cuenta son las shares que verifica el gateway con MD5, no los hashes que informa.
    valid = pow.valid_shares(task["base"], sp, int(task["from"]), int(task["to"]), res.get("shares")) if sp else []
    entry = {"share_len": len(sp), "valid_shares": len(valid),
             "hashes": int(res.get("hashes") or 0), "found": bool(res.get("found")),
             "engine_seconds": float(res.get("engine_seconds") or res.get("seconds") or 0),
             "seconds": round(time.time() - lease["since"], 3), "ts": time.time()}
    await r.lpush(keys.miner_results(miner), json.dumps(entry))
    await r.ltrim(keys.miner_results(miner), 0, RESULTS_KEPT - 1)
    await r.expire(keys.miner_results(miner), 600)
    await r.delete(keys.miner_expired(miner))  # volvió a entregar: no es zombie


@app.post("/results")
async def post_result(request: Request, ident: dict | None = Depends(auth)):
    res = await request.json()
    for f in ("task_id", "block_id", "worker", "from", "to", "found"):
        if f not in res:
            raise HTTPException(400, f"falta {f}")
    if ident:  # token propio: el resultado es de ese worker, diga lo que diga (si no, cobra otro o se saltea la prueba)
        res["worker"] = ident["miner_id"]
    r = state["r"]
    await check_result_signature(res)  # A3-f: antes de tocar el préstamo (uno sin firma no le saca la tarea al dueño)
    raw = await r.get(keys.lease(res["task_id"]))
    if raw is not None and not lease_owner_ok(json.loads(raw), ident, res["worker"]):
        raise HTTPException(409, "ese préstamo es de otro minero")  # B1.3: el préstamo sigue siendo del dueño
    raw = await r.getdel(keys.lease(res["task_id"]))
    await r.zrem(keys.LEASE_DEADLINES, res["task_id"])
    lease = json.loads(raw) if raw else None
    if lease:
        # B1.3 (26/11): el rango, el bloque y el prefijo de shares son los que se prestaron, no los que dice el pedido.
        task = lease["task"]
        res.update({"from": task["from"], "to": task["to"], "block_id": task["block_id"]})
    elif res["found"]:
        # Sin préstamo (venció y se re-publicó): un "encontré" se reenvía igual, el coordinator lo verifica con MD5.
        # Del rango no se le cree nada: solo el nonce.
        try:
            res["from"] = res["to"] = int(res.get("nonce"))
        except (TypeError, ValueError):
            raise HTTPException(400, "nonce inválido") from None
    else:
        raise HTTPException(410, "préstamo inexistente o vencido: ese rango ya lo está haciendo otro minero")
    if lease and lease.get("special") == "calib":
        await finish_calibration(lease, res)
        return Response(status_code=202)
    if lease and lease.get("special") == "canary":
        await finish_canary(lease, res)
        return Response(status_code=202)
    kind = lease["kind"] if lease else "cpu"
    # Shares: solo las de un préstamo, con el prefijo que fijó la tarea (el minero no lo puede elegir).
    if lease and lease["task"].get("share_prefix") and isinstance(res.get("shares"), list):
        res["share_prefix"] = lease["task"]["share_prefix"]
        res["shares"] = res["shares"][:pow.MAX_SHARES]
    else:
        res.pop("shares", None)
        res.pop("share_prefix", None)
    cur = await current_block()
    if not res["found"] and await r.exists(keys.quarantine(res["worker"])):
        # Un "no encontré" de un minero en cuarentena no vale: no cobra y el rango lo hace otro.
        # Un "encontré" sí se reenvía: el coordinator lo verifica con MD5.
        if cur is not None and res["block_id"] == cur["block_id"]:
            await quarantine.republish(state["exchange"], cur, res["from"], res["to"])
            REPUBLISHED.inc()
        raise HTTPException(410, "minero en cuarentena")
    if lease:
        await record_result(lease, res)
    if cur is None or res["block_id"] != cur["block_id"]:
        if cur is not None and str(res["block_id"]).startswith(f"{cur['index']}-"):
            # Bloque rearmado: el coordinator cuenta ese rango para el premio y lo descarta.
            await state["exchange"].publish(rabbit.message({"v": 1, **res}), routing_key=f"result.{kind}")
        raise HTTPException(410, "el bloque ya se cerró")
    if not res["found"]:
        key = keys.block_notfound(res["block_id"], res["worker"])
        await r.rpush(key, json.dumps({"from": res["from"], "to": res["to"]}))
        await r.expire(key, 3600)
    # El coordinator verifica todo de nuevo (NCT.3): acá solo se reenvía.
    await state["exchange"].publish(rabbit.message({"v": 1, **res}), routing_key=f"result.{kind}")
    RESULTS.labels(str(bool(res["found"])).lower()).inc()
    return Response(status_code=202)


@app.post("/keepalive")
async def keepalive(request: Request, ident: dict | None = Depends(auth)):
    body = await request.json()
    if ident:  # token propio: nombre y cuenta de cobro salen del alta, no de lo que mande el agente
        body.update(miner_id=ident["miner_id"], account=ident["account"])
    miner_id, kind = body.get("miner_id"), body.get("type")
    if not isinstance(miner_id, str) or kind not in ("cpu", "gpu"):
        raise HTTPException(400, "se espera {miner_id, type: cpu|gpu, hashrate, account?}")
    account = body.get("account") or f"miner:{miner_id}"
    if not isinstance(account, str) or len(account) > 64 or account == "recompensa":
        raise HTTPException(400, "account inválida")
    if not account.startswith("miner:") and not await state["r"].exists(keys.account_pubkey(account)):
        # El premio solo va a una cuenta con dueño (20/11): si no, el primero que registre ese nombre se lo queda.
        raise HTTPException(400, f"MINER_ACCOUNT {account} no es una cuenta registrada (crearla en /cuenta)")
    value = {"miner_id": miner_id, "type": kind, "hashrate": float(body.get("hashrate") or 0),
             "account": account, "ts": time.time(),
             "trial": bool(await state["r"].exists(keys.worker_trial(miner_id)))}
    await state["r"].set(keys.worker(miner_id), json.dumps(value), ex=WORKER_TTL_SECONDS)
    await state["r"].set(keys.miner_account(miner_id), account, ex=86400)  # el premio se paga en el bloque siguiente
    KEEPALIVES.labels(kind).inc()
    return {"ttl": WORKER_TTL_SECONDS}


@app.post("/workers/register", status_code=201)
async def register_worker(request: Request):
    """Alta pública: la cuenta (registrada con su clave en el coordinator) firma worker:<cuenta>:<worker> y
    recibe un token propio. El token se muestra una sola vez; en Redis queda solo su sha256."""
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "JSON inválido") from exc
    account, worker = body.get("account"), body.get("worker")
    if not isinstance(account, str) or not isinstance(worker, str) or not WORKER_NAME.fullmatch(worker):
        raise HTTPException(400, "worker: 1 a 32 letras, números, - o _")
    r = state["r"]
    pubkey = await r.get(keys.account_pubkey(account))
    if pubkey is None:
        raise HTTPException(400, "cuenta no registrada: primero creá la cuenta")
    if not firmas.verify(pubkey, firmas.worker_message(account, worker), body.get("signature")):
        raise HTTPException(400, firmas.INVALID)
    ip = ratelimit.client_ip(request)
    if not await ratelimit.allow(r, f"ratelimit:workers:{ip}", WORKERS_PER_IP_HOUR, 3600):  # B1.4: Sybil
        log(logger, "alta de worker frenada por límite", ip=ip)
        raise HTTPException(429, f"demasiados workers nuevos desde esta IP (máximo {WORKERS_PER_IP_HOUR} por hora)")
    miner_id, token = f"{account}.{worker}", secrets.token_urlsafe(32)
    if await r.exists(keys.worker_token_of(miner_id)):
        raise HTTPException(409, "ese worker ya existe: elegí otro nombre")
    if not await state["limit"](keys=[keys.account_workers(account)], args=[miner_id, MAX_WORKERS_PER_ACCOUNT]):
        raise HTTPException(429, f"la cuenta ya dio de alta {MAX_WORKERS_PER_ACCOUNT} workers (es el máximo)")
    if not await r.set(keys.worker_token_of(miner_id), token_hash(token), nx=True):
        raise HTTPException(409, "ese worker ya existe: elegí otro nombre")
    await r.set(keys.worker_token(token_hash(token)), json.dumps({"miner_id": miner_id, "account": account}))
    await r.set(keys.worker_trial(miner_id), 0)  # arranca en prueba
    log(logger, "worker dado de alta (en prueba)", miner_id=miner_id)
    return {"miner_id": miner_id, "account": account, "token": token, "trial": True,
            "trial_canaries": TRIAL_CANARIES}


@app.websocket("/ws/miners")
async def ws_miners(ws: WebSocket):
    """Canal de trabajo de los mineros (A3-c y A3-f, 26/11), JSON-RPC 2.0 como Stratum:
    gateway → {"method": "mining.challenge", "params": {challenge, chain_id}}
    worker  → {"id": 1, "method": "mining.authorize", "params": {worker, pubkey, signature}}  (firma del desafío)
    gateway → {"id": 1, "result": true}  o un error -32001 y se cierra
    gateway → {"method": "mining.notify", "params": <tarea>}  apenas tiene lugar (una a la vez por conexión)
    gateway → {"method": "mining.block_solved", "params": {block_id}}  (y los avisos de antes, {"type": ...})
    La entrega (POST /results firmado), renew y release siguen por HTTP: ya tienen préstamo, reintentos y dueño. Si el
    WebSocket se corta, la tarea prestada vence a los 60 s y el reaper la re-publica, como siempre."""
    try:
        ident = await auth(ws.headers.get("authorization", ""))
    except HTTPException:
        await ws.close(code=1008)
        return
    if len(state["sockets"]) >= MAX_WS:
        await ws.close(code=1013)
        return
    miner_id = ident["miner_id"] if ident else ws.query_params.get("miner_id")
    kind = ws.query_params.get("kind") or "cpu"
    if not miner_id or kind not in ("cpu", "gpu"):
        await ws.close(code=1008)
        return
    await ws.accept()
    challenge = secrets.token_hex(16)
    await ws.send_json({"jsonrpc": "2.0", "method": "mining.challenge",
                        "params": {"challenge": challenge, "chain_id": CHAIN_ID}})
    try:
        msg = json.loads(await asyncio.wait_for(ws.receive_text(), AUTHORIZE_TIMEOUT))
    except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
        await ws.close(code=1008)
        return
    params = msg.get("params") if isinstance(msg, dict) else None
    if not isinstance(params, dict) or msg.get("method") != "mining.authorize" or params.get("worker") != miner_id:
        reason = "se esperaba mining.authorize de este worker"
    else:
        reason = await authorize_worker(miner_id, params.get("pubkey"), challenge, params.get("signature"))
    if reason:
        AUTHORIZED.labels("rechazado").inc()
        log(logger, "authorize rechazado", miner_id=miner_id, reason=reason)
        await ws.send_json({"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                            "error": {"code": -32001, "message": reason}})
        await ws.close(code=1008)
        return
    AUTHORIZED.labels("ok").inc()
    await ws.send_json({"jsonrpc": "2.0", "id": msg.get("id"), "result": True})
    wake = asyncio.Event()
    conn = {"ws": ws, "miner_id": miner_id, "wake": wake}
    state["sockets"].add(ws)
    state.setdefault("workers_ws", {})[ws] = conn
    WS_CONNECTED.set(len(state["sockets"]))
    pusher = asyncio.create_task(push_loop(conn, kind))
    try:
        while True:
            await ws.receive_text()  # el worker no manda nada más por acá: solo para enterarse si se cierra
    except WebSocketDisconnect:
        pass
    finally:
        pusher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pusher
        state["sockets"].discard(ws)
        state.get("workers_ws", {}).pop(ws, None)
        WS_CONNECTED.set(len(state["sockets"]))


async def push_loop(conn: dict, kind: str) -> None:
    """mining.notify (A3-c): le manda una tarea apenas el worker no tiene ninguna empujada viva. Que la entregó (o que
    venció) se ve en que su préstamo ya no existe. Se despierta con el aviso de "hay tareas" o cada PUSH_POLL."""
    r, ws, current = state["r"], conn["ws"], None
    while True:
        try:
            if current is None or not await r.exists(keys.lease(current)):
                out = await take_task(conn["miner_id"], kind)
                if isinstance(out, dict):
                    current = out["task_id"]
                    await ws.send_json({"jsonrpc": "2.0", "method": "mining.notify", "params": out})
                    PUSHED.labels(kind).inc()
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - Redis o la red: se reintenta en la próxima vuelta
            logger.exception("error empujando tareas")
        conn["wake"].clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(conn["wake"].wait(), PUSH_POLL)


async def scaling_auth(authorization: str = Header(default="")) -> None:
    """GET /scaling: el token de solo lectura de KEDA (SCALING_TOKEN, 21/11: separado de la identidad de los
    mineros, así el escalador no tiene un token con el que se pueda minar) o, como antes, uno de minero."""
    if SCALING_TOKEN and hmac.compare_digest(authorization, f"Bearer {SCALING_TOKEN}"):
        return
    await auth(authorization)


@app.get("/scaling", dependencies=[Depends(scaling_auth)])
async def scaling():
    """Señal del pool. Si el pool murió la clave venció: 503 en vez de un número viejo."""
    raw = await state["r"].get(keys.POOL_SCALING)
    if raw is None:
        raise HTTPException(503, "el pool no publicó señal")
    return json.loads(raw)
