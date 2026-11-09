"""Gateway HTTP: la única puerta de los mineros (que están FUERA de GCP, detrás de NAT).

  GET  /tasks/next             saca una tarea de mining.tasks y se la presta al minero (204 si no hay)
  POST /tasks/{task_id}/renew  "¿sigo?": renueva el préstamo, o 410 si el bloque ya se cerró
  POST /results                publica el resultado en mining.results
  POST /keepalive              SET worker:<id> EX 15 (Redis no se expone a Internet)
  GET  /scaling                señal del pool para KEDA del k3s

Préstamos (decisiones §4): basic_get + ack inmediato y lease:<task_id> en Redis. El reaper
(una sola réplica, con lock) devuelve a la cola lo vencido; al 3er intento va a la DLQ.
"""
import asyncio
import contextlib
import hmac
import json
import time

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from prometheus_client import Counter

from tpi_common import keys, rabbit, store
from tpi_common.config import POD_NAME, WORKER_TTL_SECONDS, env_int, env_str
from tpi_common.logs import log, setup_logging
from tpi_common.web import install_common_routes

logger = setup_logging("gateway")
MINER_TOKEN = env_str("MINER_TOKEN", "")
LEASE_SECONDS = env_int("TASK_LEASE_SECONDS", 60)
MAX_ATTEMPTS = env_int("TASK_MAX_ATTEMPTS", 3)
REAPER_PERIOD = env_int("REAPER_PERIOD_SECONDS", 5)

LEASED = Counter("tasks_leased_total", "Tareas prestadas a mineros", ["kind"])
DROPPED = Counter("tasks_dropped_stale_total", "Tareas de bloques viejos descartadas al sacarlas")
REQUEUED = Counter("tasks_requeued_total", "Préstamos vencidos devueltos a la cola")
DLQ = Counter("tasks_dlq_total", "Tareas mandadas a la DLQ por agotar intentos")
RESULTS = Counter("results_forwarded_total", "Resultados publicados", ["found"])
KEEPALIVES = Counter("keepalives_total", "Keep-alives recibidos", ["kind"])

state: dict = {"solved": set()}


def auth(authorization: str = Header(default="")) -> None:
    if not MINER_TOKEN or not hmac.compare_digest(authorization, f"Bearer {MINER_TOKEN}"):
        raise HTTPException(401, "token inválido")


async def current_block_id() -> str | None:
    raw = await state["r"].get(keys.MINING_CURRENT)
    return json.loads(raw)["block_id"] if raw else None


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
                task = json.loads(raw)["task"]
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


@app.get("/tasks/next", dependencies=[Depends(auth)])
async def next_task(miner_id: str, kind: str = "cpu"):
    r = state["r"]
    current = await current_block_id()
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
        lease = {"task": task, "miner_id": miner_id, "kind": kind, "since": time.time()}
        await r.set(keys.lease(task["task_id"]), json.dumps(lease))
        await r.zadd(keys.LEASE_DEADLINES, {task["task_id"]: time.time() + LEASE_SECONDS})
        await msg.ack()  # desde acá la tarea vive en el lease, no en RabbitMQ
        LEASED.labels(kind).inc()
        return {**task, "lease_seconds": LEASE_SECONDS}
    return Response(status_code=204)


@app.post("/tasks/{task_id}/renew", dependencies=[Depends(auth)])
async def renew(task_id: str):
    """El wrapper pregunta entre tramo y tramo. 410 = el bloque se cerró: abandonar."""
    r = state["r"]
    raw = await r.get(keys.lease(task_id))
    if raw is None:
        raise HTTPException(410, "préstamo inexistente o vencido")
    block_id = json.loads(raw)["task"]["block_id"]
    if block_id in state["solved"] or block_id != await current_block_id():
        await r.zrem(keys.LEASE_DEADLINES, task_id)
        await r.delete(keys.lease(task_id))
        raise HTTPException(410, "el bloque ya se cerró")
    await r.zadd(keys.LEASE_DEADLINES, {task_id: time.time() + LEASE_SECONDS}, xx=True)
    return {"lease_seconds": LEASE_SECONDS}


@app.post("/results", dependencies=[Depends(auth)])
async def post_result(request: Request):
    res = await request.json()
    for f in ("task_id", "block_id", "worker", "from", "to", "found"):
        if f not in res:
            raise HTTPException(400, f"falta {f}")
    r = state["r"]
    raw = await r.getdel(keys.lease(res["task_id"]))
    await r.zrem(keys.LEASE_DEADLINES, res["task_id"])
    kind = json.loads(raw)["kind"] if raw else "cpu"
    if res["block_id"] != await current_block_id():
        raise HTTPException(410, "el bloque ya se cerró")
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
