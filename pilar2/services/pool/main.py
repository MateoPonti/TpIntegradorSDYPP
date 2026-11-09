"""Pool de transacciones (P5). Cada POOL_PERIOD_SECONDS, solo el líder (pool:leader):
cuenta mineros vivos, mira la cola y publica dos salidas:
  pool:plan     -> prefijo efectivo + tamaño de rango (lo usa el coordinator al armar el bloque)
  pool:scaling  -> cuántos mineros CPU (lo sirve el gateway en GET /scaling para KEDA del k3s)
No crea máquinas ni toca la cadena: ningún minero corre en GCP."""
import asyncio
import contextlib
import json
import time

from fastapi import FastAPI
from prometheus_client import Gauge

from tpi_common import keys, rabbit, store
from tpi_common.config import DIFFICULTY_PREFIX, POD_NAME, env_float, env_int
from tpi_common.logs import log, setup_logging
from tpi_common.web import install_common_routes

from . import decision

logger = setup_logging("pool")
PERIOD = env_float("POOL_PERIOD_SECONDS", 15)
FRAGMENT = env_float("POOL_FRAGMENT_PERCENT", 10)
MIN_WITHOUT_GPU = env_int("MIN_DIFFICULTY_WITHOUT_GPU", 2)
CPU_MAX = env_int("CPU_MINERS_MAX", 8)
TASKS_PER_CPU = env_int("TASKS_PER_CPU_MINER", 20)
SIGNAL_TTL = env_int("POOL_SIGNAL_TTL_SECONDS", 60)  # si el pool muere, la señal vence

MINERS = Gauge("miners_alive", "Mineros vivos", ["kind"])
HASHRATE = Gauge("hashrate_total", "Suma de hashrates informados", ["kind"])
DESIRED = Gauge("desired_cpu_miners", "Mineros CPU pedidos al k3s")
QUEUE = Gauge("mining_tasks_ready", "Tareas esperando en mining.tasks")

state: dict = {}


async def tick(r, channel) -> None:
    workers = await store.alive_workers(r)
    gpu = [w for w in workers if w.get("type") == "gpu"]
    cpu = [w for w in workers if w.get("type") == "cpu"]
    pending = await rabbit.queue_length(channel, rabbit.TASKS_QUEUE)
    p = decision.plan(DIFFICULTY_PREFIX, FRAGMENT, len(gpu), MIN_WITHOUT_GPU)
    signal = decision.scaling_signal(DIFFICULTY_PREFIX, len(gpu), len(cpu), pending, CPU_MAX, MIN_WITHOUT_GPU,
                                     TASKS_PER_CPU)
    await r.set(keys.POOL_PLAN, json.dumps({**p, "gpu_alive": len(gpu), "cpu_alive": len(cpu), "ts": time.time()}),
                ex=SIGNAL_TTL)
    await r.set(keys.POOL_SCALING, json.dumps(signal), ex=SIGNAL_TTL)
    for kind, ws in (("gpu", gpu), ("cpu", cpu)):
        MINERS.labels(kind).set(len(ws))
        HASHRATE.labels(kind).set(sum(w.get("hashrate", 0) for w in ws))
    DESIRED.set(signal["desired_cpu_miners"])
    QUEUE.set(pending)
    log(logger, "plan publicado", **p, gpu=len(gpu), cpu=len(cpu), pending=pending,
        desired_cpu=signal["desired_cpu_miners"])


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
    log(logger, "pool listo", pod=POD_NAME, period=PERIOD, fragment=FRAGMENT)
    yield
    task.cancel()
    await conn.close()
    await r.aclose()


app = FastAPI(title="TPI pool", lifespan=lifespan)
install_common_routes(app)
