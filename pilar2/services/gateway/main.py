"""Gateway HTTP: la única puerta de los mineros (que están FUERA de GCP, detrás de NAT).

  GET  /tasks/next             saca una tarea de mining.tasks y se la presta al minero (204 si no hay)
  POST /tasks/{task_id}/renew  "¿sigo?": renueva el préstamo, o 410 si el bloque ya se cerró
  POST /results                publica el resultado en mining.results
  POST /keepalive              SET worker:<id> EX 15 (Redis no se expone a Internet)
  GET  /scaling                señal del pool para KEDA del k3s

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
"""
import asyncio
import contextlib
import hashlib
import hmac
import json
import random
import time
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from prometheus_client import Counter

from tpi_common import keys, pow, rabbit, store
from tpi_common.config import POD_NAME, WORKER_TTL_SECONDS, env_int, env_str
from tpi_common.logs import log, setup_logging
from tpi_common.web import install_common_routes

logger = setup_logging("gateway")
MINER_TOKEN = env_str("MINER_TOKEN", "")
LEASE_SECONDS = env_int("TASK_LEASE_SECONDS", 60)
MAX_ATTEMPTS = env_int("TASK_MAX_ATTEMPTS", 3)
REAPER_PERIOD = env_int("REAPER_PERIOD_SECONDS", 5)
CALIB_RANGE = {"gpu": env_int("CALIB_RANGE_GPU", 256_000_000), "cpu": env_int("CALIB_RANGE_CPU", 2_000_000)}
CALIB_BASE = hashlib.md5(b"calibracion").hexdigest() + hashlib.sha256(b"calibracion").hexdigest()
CALIB_PREFIX = "f" * 20  # no aparece nunca: el minero recorre el rango entero
CANARY_EVERY = env_int("CANARY_EVERY", 20)
QUARANTINE_AFTER = env_int("QUARANTINE_AFTER_EXPIRED", 3)
QUARANTINE_SECONDS = env_int("QUARANTINE_SECONDS", 300)
RESULTS_KEPT = 20

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
REPUBLISHED = Counter("tasks_republished_total", "Rangos re-publicados porque el minero falló un canario")

state: dict = {"solved": set()}


def auth(authorization: str = Header(default="")) -> None:
    if not MINER_TOKEN or not hmac.compare_digest(authorization, f"Bearer {MINER_TOKEN}"):
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


async def put_in_quarantine(miner_id: str, reason: str) -> None:
    await state["r"].set(keys.quarantine(miner_id), reason, ex=QUARANTINE_SECONDS)
    QUARANTINED.labels(reason).inc()
    log(logger, "minero en cuarentena", miner_id=miner_id, reason=reason, seconds=QUARANTINE_SECONDS)


async def reaper_loop() -> None:
    r = state["r"]
    while True:
        await asyncio.sleep(REAPER_PERIOD)
        try:
            if not await r.set(keys.REAPER_LOCK, POD_NAME, nx=True, ex=REAPER_PERIOD * 2):
                continue
            current = await current_block_id()
            for task_id in await r.zrangebyscore(keys.LEASE_DEADLINES, 0, time.time()):
                if not await r.zrem(keys.LEASE_DEADLINES, task_id):
                    continue  # otro lo tomó
                raw = await r.getdel(keys.lease(task_id))
                if raw is None:
                    continue
                lease = json.loads(raw)
                task = lease["task"]
                EXPIRED.inc()
                await count_expired(lease["miner_id"])
                if lease.get("special"):
                    continue  # calibración o canario: no vuelven a la cola
                if task["block_id"] != current:
                    continue  # bloque ya sellado: no hace falta re-minar ese rango
                task["attempts"] = task.get("attempts", 0) + 1
                if task["attempts"] >= MAX_ATTEMPTS:
                    await state["dlx"].publish(rabbit.message(task), routing_key="dlq")
                    DLQ.inc()
                    log(logger, "tarea a la DLQ", task_id=task_id, attempts=task["attempts"])
                else:
                    await state["exchange"].publish(rabbit.message(task), routing_key="task.any")
                    REQUEUED.inc()
                    log(logger, "préstamo vencido, tarea devuelta", task_id=task_id, attempts=task["attempts"])
        except Exception:  # noqa: BLE001
            logger.exception("error en el reaper")


async def count_expired(miner_id: str) -> None:
    """Préstamos vencidos seguidos. El pool usa el `ts` para no contar el hashrate de un minero zombie."""
    r = state["r"]
    prev = json.loads(await r.get(keys.miner_expired(miner_id)) or '{"streak": 0}')
    streak = prev["streak"] + 1
    await r.set(keys.miner_expired(miner_id), json.dumps({"streak": streak, "ts": time.time()}), ex=3600)
    if streak >= QUARANTINE_AFTER and not await r.exists(keys.quarantine(miner_id)):
        await put_in_quarantine(miner_id, "vencidos")


async def on_event(message) -> None:
    async with message.process():
        ev = json.loads(message.body)
        state["solved"].add(ev["block_id"])
        log(logger, "block.solved recibido", block_id=ev["block_id"])


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    r = await store.connect(logger)
    conn = await rabbit.connect(logger)
    channel = await conn.channel()
    exchange, dlx, tasks, _ = await rabbit.declare_topology(channel)
    events = await rabbit.declare_events_queue(channel, exchange, POD_NAME)
    await events.consume(on_event)
    state.update(r=r, conn=conn, exchange=exchange, dlx=dlx, tasks=tasks, get_lock=asyncio.Lock())
    reaper = asyncio.create_task(reaper_loop())
    if not MINER_TOKEN:
        log(logger, "MINER_TOKEN vacío: se rechazan todos los mineros")
    log(logger, "gateway listo", pod=POD_NAME)
    yield
    reaper.cancel()
    await conn.close()
    await r.aclose()


app = FastAPI(title="TPI gateway", lifespan=lifespan)
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


@app.get("/tasks/next", dependencies=[Depends(auth)])
async def next_task(miner_id: str, kind: str = "cpu"):
    r = state["r"]
    if await r.exists(keys.quarantine(miner_id)):
        return Response(status_code=204)
    if not await r.exists(keys.miner_calibration(miner_id)):
        return await lend(calibration_task(kind), miner_id, kind, special="calib")
    cur = await current_block()
    if cur and CANARY_EVERY > 0 and await r.incr(keys.CANARY_COUNTER) % CANARY_EVERY == 0:
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
    await r.zadd(keys.LEASE_DEADLINES, {task_id: time.time() + LEASE_SECONDS}, xx=True)
    return {"lease_seconds": LEASE_SECONDS}


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
        return
    log(logger, "canario fallado", miner_id=miner, task_from=task["from"], task_to=task["to"])
    await put_in_quarantine(miner, "canario")
    cur = await current_block()
    if cur is None:
        return
    r = state["r"]
    await r.hdel(keys.block_shares(cur["index"]), miner)  # su trabajo de este bloque no cobra
    key = keys.block_notfound(cur["block_id"], miner)
    for raw in await r.lrange(key, 0, -1):  # lo que dijo "no encontré" puede tener la solución
        rng = json.loads(raw)
        again = {"v": 1, "task_id": uuid.uuid4().hex, "block_id": cur["block_id"], "base": cur["base"],
                 "prefix": cur["prefix"], "from": rng["from"], "to": rng["to"], "attempts": 0}
        await state["exchange"].publish(rabbit.message(again), routing_key="task.any")
        REPUBLISHED.inc()
    await r.delete(key)


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


@app.post("/results", dependencies=[Depends(auth)])
async def post_result(request: Request):
    res = await request.json()
    for f in ("task_id", "block_id", "worker", "from", "to", "found"):
        if f not in res:
            raise HTTPException(400, f"falta {f}")
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
    if lease:
        await record_result(lease, res)
    kind = lease["kind"] if lease else "cpu"
    cur = await current_block()
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


@app.post("/keepalive", dependencies=[Depends(auth)])
async def keepalive(request: Request):
    body = await request.json()
    miner_id, kind = body.get("miner_id"), body.get("type")
    if not isinstance(miner_id, str) or kind not in ("cpu", "gpu"):
        raise HTTPException(400, "se espera {miner_id, type: cpu|gpu, hashrate}")
    value = {"miner_id": miner_id, "type": kind, "hashrate": float(body.get("hashrate") or 0), "ts": time.time()}
    await state["r"].set(keys.worker(miner_id), json.dumps(value), ex=WORKER_TTL_SECONDS)
    KEEPALIVES.labels(kind).inc()
    return {"ttl": WORKER_TTL_SECONDS}


@app.get("/scaling", dependencies=[Depends(auth)])
async def scaling():
    """Señal del pool. Si el pool murió la clave venció: 503 en vez de un número viejo."""
    raw = await state["r"].get(keys.POOL_SCALING)
    if raw is None:
        raise HTTPException(503, "el pool no publicó señal")
    return json.loads(raw)
