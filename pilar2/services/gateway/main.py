"""Gateway HTTP: la única puerta de los mineros (que están FUERA de GCP, detrás de NAT).

  GET  /tasks/next             saca una tarea de mining.tasks y se la presta al minero (204 si no hay)
  POST /tasks/{task_id}/renew  "¿sigo?": renueva el préstamo, o 410 si el bloque ya se cerró o está en cuarentena
  POST /tasks/{task_id}/release "no pude": la tarea vuelve a la cola al instante (se le rompió el motor)
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
from prometheus_client import Counter, Gauge

from tpi_common import firmas, keys, pow, quarantine, rabbit, store
from tpi_common.config import POD_NAME, WORKER_TTL_SECONDS, env_int, env_str
from tpi_common.logs import log, setup_logging
from tpi_common.web import install_common_routes

logger = setup_logging("gateway")
MINER_TOKEN = env_str("MINER_TOKEN", "")
SCALING_TOKEN = env_str("SCALING_TOKEN", "")
WORKER_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")
LEASE_SECONDS = env_int("TASK_LEASE_SECONDS", 60)
MAX_ATTEMPTS = env_int("TASK_MAX_ATTEMPTS", 3)
REAPER_PERIOD = env_int("REAPER_PERIOD_SECONDS", 5)
CALIB_RANGE = {"gpu": env_int("CALIB_RANGE_GPU", 1_000_000_000), "cpu": env_int("CALIB_RANGE_CPU", 2_000_000)}
CALIB_BASE = hashlib.md5(b"calibracion").hexdigest() + hashlib.sha256(b"calibracion").hexdigest()
CALIB_PREFIX = "f" * 20  # no aparece nunca: el minero recorre el rango entero
CANARY_EVERY = env_int("CANARY_EVERY", 20)
QUARANTINE_AFTER = env_int("QUARANTINE_AFTER_EXPIRED", 3)
QUARANTINE_SECONDS = env_int("QUARANTINE_SECONDS", 300)
QUARANTINE_MAX_SECONDS = env_int("QUARANTINE_MAX_SECONDS", 3600)  # tope de la cuarentena que crece
REVOKE_AFTER_STRIKES = env_int("REVOKE_AFTER_STRIKES", 3)  # con token propio: a la 3.ª cuarentena en 1 h, sin token
RESULTS_KEPT = 20
TRIAL_CANARY_EVERY = env_int("TRIAL_CANARY_EVERY", 5)
TRIAL_CANARIES = env_int("TRIAL_CANARIES", 3)
MAX_WORKERS_PER_ACCOUNT = env_int("MAX_WORKERS_PER_ACCOUNT", 10)
MAX_LEASES_PER_MINER = env_int("MAX_LEASES_PER_MINER", 2)  # la que mina y una de colchón
NOTICE_PERIOD = float(env_str("WS_NOTICE_SECONDS", "1"))
MAX_WS = env_int("WS_MAX_MINERS", 500)

LEASED = Counter("tasks_leased_total", "Tareas prestadas a mineros", ["kind"])
DROPPED = Counter("tasks_dropped_stale_total", "Tareas de bloques viejos descartadas al sacarlas")
EXPIRED = Counter("leases_expired_total", "Préstamos vencidos (el minero no contestó a tiempo)")
REQUEUED = Counter("tasks_requeued_total", "Préstamos vencidos devueltos a la cola")
DLQ = Counter("tasks_dlq_total", "Tareas mandadas a la DLQ por agotar intentos")
RESULTS = Counter("results_forwarded_total", "Resultados publicados", ["found"])
KEEPALIVES = Counter("keepalives_total", "Keep-alives recibidos", ["kind"])
CALIBRATIONS = Counter("calibrations_total", "Tareas de calibración terminadas", ["kind", "class"])
CANARIES = Counter("canaries_total", "Canarios resueltos", ["result"])
QUARANTINED = Counter("quarantined_total", "Mineros puestos en cuarentena", ["reason"])
REPUBLISHED = Counter("tasks_republished_total", "Rangos re-publicados de mineros en cuarentena")
RELEASED = Counter("tasks_released_total", "Tareas devueltas por el minero (se le rompió el motor)")
REVOKED = Counter("tokens_revoked_total", "Tokens de workers revocados", ["reason"])
LIMITED = Counter("tasks_limited_total", "Pedidos de tarea rechazados porque el minero ya tenía el máximo prestado")
TRIAL_PASSED = Counter("workers_trial_passed_total", "Workers que pasaron la prueba")
WS_CONNECTED = Gauge("gateway_ws_miners", "Mineros conectados por WebSocket a esta réplica")
WS_NOTICES = Counter("gateway_ws_notices_total", "Avisos mandados por WebSocket", ["type"])

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


def classify(hashrate: float) -> str:
    """Clase del minero según la calibración (para /miners y Grafana)."""
    if hashrate >= 100e6:
        return "rapido"
    if hashrate >= 5e6:
        return "medio"
    return "lento"


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


async def give_back(lease: dict, why: str) -> None:
    """La tarea vuelve a la cola con un intento más (préstamo vencido o devuelta por el minero).
    Al MAX_ATTEMPTS-ésimo va a la DLQ. Las de calibración y los canarios no vuelven."""
    task = lease["task"]
    if lease.get("special") or task["block_id"] != await current_block_id():
        return  # bloque ya sellado o rearmado: no hace falta re-minar ese rango
    task["attempts"] = task.get("attempts", 0) + 1
    if task["attempts"] >= MAX_ATTEMPTS:
        await state["dlx"].publish(rabbit.message(task), routing_key="dlq")
        DLQ.inc()
        log(logger, "tarea a la DLQ", task_id=task["task_id"], attempts=task["attempts"])
    else:
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


async def lend(task: dict, miner_id: str, kind: str, special: str | None = None) -> dict:
    lease = {"task": task, "miner_id": miner_id, "kind": kind, "since": time.time(), "special": special}
    await state["r"].set(keys.lease(task["task_id"]), json.dumps(lease))
    await state["r"].zadd(keys.LEASE_DEADLINES, {task["task_id"]: time.time() + LEASE_SECONDS})
    LEASED.labels(kind).inc()
    return {**task, "lease_seconds": LEASE_SECONDS}


def calibration_task(kind: str) -> dict:
    return {"v": 1, "task_id": uuid.uuid4().hex, "block_id": "calib", "base": CALIB_BASE, "prefix": CALIB_PREFIX,
            "from": 0, "to": CALIB_RANGE.get(kind, CALIB_RANGE["cpu"]) - 1, "attempts": 0}


async def canary_task(cur: dict) -> dict | None:
    """Rango del tamaño de una tarea normal, de un bloque ya sellado, que contiene su nonce conocido.
    Lleva el block_id actual: para el minero es una tarea más (solo cambia la base)."""
    r = state["r"]
    height = await r.llen(keys.CHAIN)
    if height < 2:
        return None
    raw = await r.get(keys.block(random.randrange(1, height)))
    old = json.loads(raw) if raw else None
    if not old or not old.get("prefix"):
        return None
    size = int(cur["range_size"])
    lo = max(0, int(old["nonce"]) - random.randrange(size))
    return {"v": 1, "task_id": uuid.uuid4().hex, "block_id": cur["block_id"],
            "base": old["previous_hash"] + old["tx_digest"], "prefix": old["prefix"],
            "from": lo, "to": lo + size - 1, "attempts": 0}


@app.get("/tasks/next")
async def next_task(miner_id: str, kind: str = "cpu", ident: dict | None = Depends(auth)):
    if ident:  # worker con token propio: el nombre sale del token
        miner_id = ident["miner_id"]
    r = state["r"]
    if await r.exists(keys.quarantine(miner_id)):
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
    if cur and canary_due:
        canary = await canary_task(cur)
        if canary:
            return await lend(canary, miner_id, kind, special="canary")
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
        out = await lend(task, miner_id, kind)
        await msg.ack()  # desde acá la tarea vive en el lease, no en RabbitMQ
        return out
    return Response(status_code=204)


@app.post("/tasks/{task_id}/renew", dependencies=[Depends(auth)])
async def renew(task_id: str):
    """El wrapper pregunta entre tramo y tramo. 410 = el bloque se cerró: abandonar."""
    r = state["r"]
    raw = await r.get(keys.lease(task_id))
    if raw is None:
        raise HTTPException(410, "préstamo inexistente o vencido")
    lease = json.loads(raw)
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


@app.post("/tasks/{task_id}/release", dependencies=[Depends(auth)])
async def release(task_id: str):
    """El minero devuelve la tarea porque no pudo terminarla (se le rompió el motor). Vuelve a la cola al
    instante, sin esperar los 60 s del préstamo, y cuenta como una falla (a las 3 seguidas, cuarentena)."""
    r = state["r"]
    raw = await r.getdel(keys.lease(task_id))
    await r.zrem(keys.LEASE_DEADLINES, task_id)
    if raw is None:
        raise HTTPException(410, "préstamo inexistente o vencido")
    lease = json.loads(raw)
    RELEASED.inc()
    await count_expired(lease["miner_id"])
    await give_back(lease, "tarea devuelta por el minero")
    return {"released": True}


async def finish_calibration(lease: dict, res: dict) -> None:
    hashes = int(res.get("hashes") or 0)
    seconds = float(res.get("engine_seconds") or res.get("seconds") or 0)
    rate = hashes / seconds if seconds > 0 else 0.0
    value = {"hashrate": rate, "class": classify(rate), "kind": lease["kind"], "hashes": hashes,
             "seconds": round(seconds, 3), "ts": time.time()}
    await state["r"].set(keys.miner_calibration(lease["miner_id"]), json.dumps(value), ex=86400)
    CALIBRATIONS.labels(lease["kind"], value["class"]).inc()
    log(logger, "minero calibrado", miner_id=lease["miner_id"], kind=lease["kind"], hashrate=round(rate),
        clase=value["class"])


async def finish_canary(lease: dict, res: dict) -> None:
    task, miner = lease["task"], lease["miner_id"]
    ok = bool(res.get("found")) and int(task["from"]) <= int(res.get("nonce", -1)) <= int(task["to"]) \
        and pow.verify(task["base"], task["prefix"], int(res["nonce"]), res.get("hash", ""))
    CANARIES.labels("ok" if ok else "falla").inc()
    if ok:
        if await state["r"].exists(keys.worker_trial(miner)) and await state["r"].incr(keys.worker_trial(miner)) >= TRIAL_CANARIES:
            await state["r"].delete(keys.worker_trial(miner), keys.miner_tasks(miner))
            TRIAL_PASSED.inc()
            log(logger, "worker pasó la prueba: ya cobra", miner_id=miner)
        return
    log(logger, "canario fallado", miner_id=miner, task_from=task["from"], task_to=task["to"])
    await put_in_quarantine(miner, "canario")  # no cobra el bloque y sus "no encontré" van a otros
    await revoke_token(miner, "canario")  # alta pública: mentir le cuesta el token


async def record_result(lease: dict, res: dict) -> None:
    """Hashrate real: el motor informa sus segundos; el de punta a punta lo mide el gateway."""
    r, miner = state["r"], lease["miner_id"]
    entry = {"hashes": int(res.get("hashes") or 0), "found": bool(res.get("found")),
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
    raw = await r.getdel(keys.lease(res["task_id"]))
    await r.zrem(keys.LEASE_DEADLINES, res["task_id"])
    lease = json.loads(raw) if raw else None
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
    """Avisos al minero (punto 4): {"type": "tareas"} y {"type": "block.solved", "block_id"}. El minero no manda
    nada por acá: pedir y entregar tareas sigue siendo HTTP, así que si esto se corta no se pierde trabajo."""
    try:
        await auth(ws.headers.get("authorization", ""))
    except HTTPException:
        await ws.close(code=1008)
        return
    if len(state["sockets"]) >= MAX_WS:
        await ws.close(code=1013)
        return
    await ws.accept()
    state["sockets"].add(ws)
    WS_CONNECTED.set(len(state["sockets"]))
    try:
        while True:
            await ws.receive_text()  # solo para enterarse si se cierra
    except WebSocketDisconnect:
        pass
    finally:
        state["sockets"].discard(ws)
        WS_CONNECTED.set(len(state["sockets"]))


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
