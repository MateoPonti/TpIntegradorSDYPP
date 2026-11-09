"""Coordinator (NCT) + API pública. Todas las réplicas atienden HTTP y verifican resultados;
solo el líder (coord:leader) arma bloques y publica tareas."""
import asyncio
import contextlib
import json
import time
import urllib.request

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Gauge

from tpi_common import keys, rabbit, store
from tpi_common.config import POD_NAME, env_float, env_str
from tpi_common.logs import log, setup_logging
from tpi_common.web import install_common_routes

from . import ledger
from .blocks import Chain

logger = setup_logging("coordinator")
POLL = env_float("BLOCK_POLL_SECONDS", 1.0)  # duda 5 (propuesta): loop cada 1 s
GATEWAY_HEALTH_URL = env_str("GATEWAY_HEALTH_URL", "http://gateway:8080/health")
MAX_BULK = 10_000

TX_ACCEPTED = Counter("tx_accepted_total", "Transacciones aceptadas")
TX_REJECTED = Counter("tx_rejected_total", "Transacciones rechazadas", ["reason"])
PENDING = Gauge("pending_transactions", "Tx que todavía no están en un bloque sellado")
IS_LEADER = Gauge("coordinator_is_leader", "1 si esta réplica es la líder")

state: dict = {}


async def leader_loop(chain: Chain, leader: store.Leader) -> None:
    while True:
        try:
            is_leader = await leader.check()
            IS_LEADER.set(1 if is_leader else 0)
            if is_leader:
                await chain.leader_tick()
            PENDING.set(await chain.pending_count())
        except Exception:  # noqa: BLE001 - el loop no se puede morir
            logger.exception("error en el loop del líder")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(chain.wake.wait(), POLL)
        chain.wake.clear()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    r = await store.connect(logger)
    conn = await rabbit.connect(logger)
    channel = await conn.channel()
    await channel.set_qos(prefetch_count=1)
    exchange, _, _, results = await rabbit.declare_topology(channel)
    chain = Chain(r, exchange, channel, logger)
    await chain.ensure_genesis()
    await results.consume(chain.on_result)
    loop = asyncio.create_task(leader_loop(chain, store.Leader(r, keys.COORD_LEADER, POD_NAME)))
    state.update(r=r, conn=conn, chain=chain, reserve=r.register_script(ledger.RESERVE_LUA))
    log(logger, "coordinator listo", pod=POD_NAME)
    yield
    loop.cancel()
    await conn.close()
    await r.aclose()


app = FastAPI(title="TPI coordinator", lifespan=lifespan)
install_common_routes(app)


async def _submit(body) -> tuple[int, dict]:
    try:
        tx = ledger.normalize(body)
    except ledger.InvalidTx as exc:
        TX_REJECTED.labels("formato").inc()
        return 400, {"error": str(exc)}
    rc = await state["reserve"](keys=ledger.reserve_keys(tx), args=[tx["tx_id"], tx["amount"], json.dumps(tx)])
    if rc < 0:
        reason = ledger.RESERVE_ERRORS[rc]
        TX_REJECTED.labels(reason).inc()
        return 400, {"error": reason}
    TX_ACCEPTED.inc()
    return 202, {"tx_id": tx["tx_id"]}


async def _json(request: Request):
    try:
        return await request.json()
    except ValueError as exc:
        raise HTTPException(400, "JSON inválido") from exc


@app.post("/transactions")
async def post_transaction(request: Request):
    code, body = await _submit(await _json(request))
    return JSONResponse(body, status_code=code)


@app.post("/transactions/bulk")
async def post_bulk(request: Request):
    """Carga masiva: lista de tx. Responde 202 con el resultado de cada una (aceptada o motivo)."""
    items = await _json(request)
    if not isinstance(items, list) or len(items) > MAX_BULK:
        raise HTTPException(400, f"se esperaba una lista de hasta {MAX_BULK} tx")
    results = [await _submit(it) for it in items]
    accepted = sum(1 for code, _ in results if code == 202)
    return JSONResponse({"accepted": accepted, "rejected": len(results) - accepted,
                         "results": [b for _, b in results]}, status_code=202)


@app.get("/transactions/{tx_id}")
async def get_transaction(tx_id: str):
    where = await state["r"].hget(keys.TX_STATUS, tx_id)
    if where is None:
        raise HTTPException(404, "no existe")
    return {"tx_id": tx_id, "status": "pending" if where == "pending" else "confirmed",
            "block": None if where == "pending" else int(where)}


@app.get("/balances/{user}")
async def get_balance(user: str):
    r = state["r"]
    bal = int(await r.get(keys.balance(user)) or 0)
    out = int(await r.get(keys.pending_out(user)) or 0)
    return {"user": user, "balance_cents": bal, "pending_out_cents": out, "available_cents": bal - out}


@app.get("/blocks/{index}")
async def get_block(index: int):
    raw = await state["r"].get(keys.block(index))
    if raw is None:
        raise HTTPException(404, "no existe")
    return json.loads(raw)


@app.get("/chain")
async def get_chain(start: int = 0, limit: int = 50):
    r = state["r"]
    hashes = await r.lrange(keys.CHAIN, start, start + min(limit, 500) - 1)
    return {"height": await r.llen(keys.CHAIN), "start": start, "hashes": hashes}


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return resp.status == 200
    except OSError:
        return False


@app.get("/status")
async def status():
    """Endpoint público de estado ({servicio: estado}). Lo usa bench.py (pending_transactions)."""
    r, chain = state["r"], state["chain"]
    services = {"coordinator": "ok"}
    try:
        await r.ping()
        services["redis"] = "ok"
    except Exception:  # noqa: BLE001
        services["redis"] = "down"
    services["rabbitmq"] = "ok" if not state["conn"].is_closed else "down"
    signal = await r.get(keys.POOL_SCALING)
    services["pool"] = "ok" if signal else "sin señal"
    services["gateway"] = "ok" if await asyncio.to_thread(_http_ok, GATEWAY_HEALTH_URL) else "down"
    workers = await store.alive_workers(r)
    cur = await chain.current()
    return {
        "services": services,
        "pending_transactions": await chain.pending_count(),
        "chain_height": await r.llen(keys.CHAIN),
        "chain_tip": await r.get(keys.CHAIN_TIP),
        "mining": None if cur is None else {k: cur[k] for k in ("block_id", "index", "prefix", "range_size")}
        | {"seconds": round(time.time() - cur["created"], 1)},
        "miners": {"gpu": sum(w.get("type") == "gpu" for w in workers),
                   "cpu": sum(w.get("type") == "cpu" for w in workers)},
        "pool_signal": json.loads(signal) if signal else None,
        "leader": await r.get(keys.COORD_LEADER),
    }
