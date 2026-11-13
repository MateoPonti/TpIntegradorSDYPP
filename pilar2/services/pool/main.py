"""Pool de transacciones (P5). Cada POOL_PERIOD_SECONDS, solo el líder (pool:leader):
mide el hashrate real de los mineros vivos y publica tres salidas:
  pool:plan     -> prefijo + tamaño de rango + hashrate (lo usa el coordinator al armar y para rearmar)
  pool:scaling  -> cuántos mineros CPU (lo sirve el gateway en GET /scaling para KEDA del k3s)
  pool:miners   -> hashrate de cada minero (lo muestra GET /miners del coordinator)
No crea máquinas ni toca la cadena: ningún minero corre en GCP."""
import asyncio
import contextlib
import json
import time

from fastapi import FastAPI
from prometheus_client import Gauge

from tpi_common import keys, rabbit, store
from tpi_common.config import DIFFICULTY_PREFIX, POD_NAME, env_float, env_int, env_str
from tpi_common.logs import log, setup_logging
from tpi_common.web import install_common_routes

from . import decision

logger = setup_logging("pool")
PERIOD = env_float("POOL_PERIOD_SECONDS", 15)
FRAGMENT = env_float("POOL_FRAGMENT_PERCENT", 10)
MODE = env_str("DIFFICULTY_MODE", "auto")  # auto | fixed (pruebas de carga con DIFFICULTY_PREFIX)
TARGET = env_float("BLOCK_TARGET_SECONDS", 10)  # = la ventana: si un bloque tarda más, se hace fila
PREFIX_MIN = env_int("PREFIX_MIN_LEN", 1)
PREFIX_MAX = env_int("PREFIX_MAX_LEN", 8)
RAISE_MARGIN = env_float("PREFIX_RAISE_MARGIN", 0.8)
MIN_RANGE_GPU = env_int("MIN_RANGE_GPU", 100_000_000)  # que la tarea llene los hilos de la GPU
MIN_RANGE_CPU = env_int("MIN_RANGE_CPU", 2_000_000)    # ~1 s en un pod con 1 worker (1,6 MH/s medido)
CPU_MAX = env_int("CPU_MINERS_MAX", 8)
CPU_IDLE = env_float("CPU_IDLE_SECONDS", 60)
WINDOW = env_float("HASHRATE_WINDOW_SECONDS", 300)
SIGNAL_TTL = env_int("POOL_SIGNAL_TTL_SECONDS", 60)  # si el pool muere, la señal vence

MINERS = Gauge("miners_alive", "Mineros vivos (sin cuarentena)", ["kind"])
HASHRATE = Gauge("hashrate_total", "Hashrate real que cuenta para el prefijo", ["kind"])
MINER_HASHRATE = Gauge("miner_hashrate", "Hashrate de cada minero", ["miner_id", "kind", "source"])
PREFIX_LEN = Gauge("pool_prefix_length", "Largo del prefijo que publica el pool")
RANGE = Gauge("pool_range_size", "Nonces por tarea")
DESIRED = Gauge("desired_cpu_miners", "Mineros CPU pedidos al k3s")
QUEUE = Gauge("mining_tasks_ready", "Tareas esperando en mining.tasks")

state: dict = {}


async def measure(r, workers: list[dict], now: float) -> list[dict]:
    out = []
    for w in workers:
        mid = w["miner_id"]
        results = [json.loads(x) for x in await r.lrange(keys.miner_results(mid), 0, -1)]
        calib = await r.get(keys.miner_calibration(mid))
        expired = await r.get(keys.miner_expired(mid))
        quarantined = bool(await r.exists(keys.quarantine(mid)))
        calib = json.loads(calib) if calib else None
        rate, source = decision.miner_hashrate(results, calib, w.get("hashrate", 0),
                                               json.loads(expired)["ts"] if expired else None,
                                               quarantined, now, WINDOW)
        rates = decision.miner_rates(results, now, WINDOW)
        out.append({"miner_id": mid, "kind": w.get("type"), "hashrate": rate, "source": source,
                    "motor": rates[0] if rates else None, "efectivo": rates[1] if rates else None,
                    "calibrated": calib["hashrate"] if calib else None, "class": calib["class"] if calib else None,
                    "declared": w.get("hashrate", 0), "quarantined": quarantined})
    return out


async def tick(r, channel) -> None:
    now = time.time()
    miners = await measure(r, await store.alive_workers(r), now)
    active = [m for m in miners if not m["quarantined"]]
    gpu = [m for m in active if m["kind"] == "gpu"]
    cpu = [m for m in active if m["kind"] == "cpu"]
    total = sum(m["hashrate"] for m in active)

    previous = json.loads(await r.get(keys.POOL_PLAN) or "{}")
    if MODE == "fixed":
        prefix = DIFFICULTY_PREFIX
    else:
        current = len(previous.get("prefix", "0" * PREFIX_MIN))
        prefix = "0" * decision.choose_prefix_len(current, total, TARGET, PREFIX_MIN, PREFIX_MAX, RAISE_MARGIN)
    size = decision.range_size(len(prefix), FRAGMENT, MIN_RANGE_GPU if gpu else MIN_RANGE_CPU)

    has_work = bool(await r.exists(keys.MINING_CURRENT)) or await r.llen(keys.TX_PENDING) > 0
    if has_work:
        await r.set(keys.POOL_LAST_WORK, now)
    idle = now - float(await r.get(keys.POOL_LAST_WORK) or 0)
    desired = decision.desired_cpu_miners(len(gpu), has_work, idle, CPU_MAX, CPU_IDLE)
    pending = await rabbit.queue_length(channel, rabbit.TASKS_QUEUE)

    plan = {"prefix": prefix, "range_size": size, "hashrate": total, "mode": MODE,
            "gpu_alive": len(gpu), "cpu_alive": len(cpu), "ts": now}
    signal = {"gpu_alive": len(gpu), "cpu_alive": len(cpu), "pending_tasks": pending, "prefix": prefix,
              "hashrate": total, "has_work": has_work, "desired_cpu_miners": desired}
    await r.set(keys.POOL_PLAN, json.dumps(plan), ex=SIGNAL_TTL)
    await r.set(keys.POOL_SCALING, json.dumps(signal), ex=SIGNAL_TTL)
    await r.set(keys.POOL_MINERS, json.dumps(miners), ex=SIGNAL_TTL)

    MINER_HASHRATE.clear()  # los mineros que se fueron no quedan graficados con su último valor
    for m in miners:
        for source in ("hashrate", "motor", "efectivo", "calibrated", "declared"):
            if m[source] is not None:
                MINER_HASHRATE.labels(m["miner_id"], m["kind"], source).set(m[source])
    for kind, ms in (("gpu", gpu), ("cpu", cpu)):
        MINERS.labels(kind).set(len(ms))
        HASHRATE.labels(kind).set(sum(m["hashrate"] for m in ms))
    PREFIX_LEN.set(len(prefix))
    RANGE.set(size)
    DESIRED.set(desired)
    QUEUE.set(pending)
    if prefix != previous.get("prefix"):
        log(logger, "prefijo cambiado", before=previous.get("prefix"), after=prefix, hashrate=round(total))
    log(logger, "plan publicado", prefix=prefix, range_size=size, hashrate=round(total), gpu=len(gpu),
        cpu=len(cpu), pending=pending, desired_cpu=desired)


async def loop(r, channel, leader: store.Leader) -> None:
    while True:
        try:
            if await leader.check():
                await tick(r, channel)
        except Exception:  # noqa: BLE001
            logger.exception("error en el loop del pool")
        await asyncio.sleep(PERIOD)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    r = await store.connect(logger)
    conn = await rabbit.connect(logger)
    channel = await conn.channel()
    await rabbit.declare_topology(channel)
    task = asyncio.create_task(loop(r, channel, store.Leader(r, keys.POOL_LEADER, POD_NAME, ttl=int(PERIOD * 2))))
    log(logger, "pool listo", pod=POD_NAME, period=PERIOD, fragment=FRAGMENT, mode=MODE)
    yield
    task.cancel()
    await conn.close()
    await r.aclose()


app = FastAPI(title="TPI pool", lifespan=lifespan)
install_common_routes(app)
