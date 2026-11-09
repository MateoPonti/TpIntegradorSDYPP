"""NCT.1 a NCT.4: armar el bloque, publicar las tareas por tandas, verificar y sellar.

Un solo bloque a la vez. El estado vive en Redis (mining:current, block:<i>:next_from,
tx:inflight:<i>), así que si el líder se cae otra réplica sigue desde donde quedó.
"""
import asyncio
import json
import time
import uuid

import aio_pika
from prometheus_client import Counter, Gauge, Histogram

from tpi_common import keys, pow, rabbit
from tpi_common.config import DIFFICULTY_PREFIX, env_float, env_int
from tpi_common.logs import log
from tpi_common.store import alive_workers

MAX_TX_PER_BLOCK = env_int("MAX_TX_PER_BLOCK", 1000)
BLOCK_TIMEOUT = env_float("BLOCK_TIMEOUT_SECONDS", 10)
DEFAULT_RANGE_SIZE = env_int("DEFAULT_RANGE_SIZE", 1_000_000)  # si todavía no hay pool:plan
TASKS_PER_MINER = env_int("TASKS_PER_MINER_BATCH", 2)  # tamaño de tanda = mineros vivos x esto
GENESIS_USERS = env_int("GENESIS_USERS", 97)  # user-0..user-96, como bench.py
GENESIS_BALANCE_CENTS = env_int("GENESIS_BALANCE_CENTS", 100_000_000)
NONCE_LIMIT = 2**63 - 1  # INCRBY de Redis es int64 con signo

BLOCKS_SEALED = Counter("blocks_sealed_total", "Bloques sellados")
BLOCK_SECONDS = Histogram("block_seconds", "Tiempo desde que se arma el bloque hasta que se sella",
                          buckets=(1, 2, 5, 10, 20, 30, 60, 120, 300, 600, 1800))
RESULTS = Counter("results_total", "Resultados recibidos", ["status"])
TASKS_PUBLISHED = Counter("tasks_published_total", "Tareas publicadas en mining.tasks")

# KEYS: tx:pending, tx:inflight:<i>   ARGV: max tx
# Si ya hay tx en vuelo para ese índice (el líder anterior murió a mitad), se reusan.
TAKE_LUA = """
if redis.call('EXISTS', KEYS[2]) == 1 then return redis.call('LRANGE', KEYS[2], 0, -1) end
local txs = redis.call('LRANGE', KEYS[1], 0, tonumber(ARGV[1]) - 1)
if #txs == 0 then return txs end
redis.call('LTRIM', KEYS[1], #txs, -1)
redis.call('RPUSH', KEYS[2], unpack(txs))
return txs
"""

# KEYS: chain:tip, mining:current, chain, block:<i>, hash:<h>, tx:inflight:<i>, tx:status, block:<i>:next_from
# ARGV: prev_hash, block_id, block_json, block_hash, index
SEAL_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return -1 end
local cur = redis.call('GET', KEYS[2])
if not cur or cjson.decode(cur)['block_id'] ~= ARGV[2] then return -2 end
if not redis.call('SET', KEYS[5], ARGV[5], 'NX') then return -3 end
redis.call('SET', KEYS[4], ARGV[3])
redis.call('RPUSH', KEYS[3], ARGV[4])
redis.call('SET', KEYS[1], ARGV[4])
local txs = redis.call('LRANGE', KEYS[6], 0, -1)
for _, raw in ipairs(txs) do
  local tx = cjson.decode(raw)
  if tx['sender'] ~= tx['receiver'] then
    redis.call('DECRBY', 'balance:' .. tx['sender'], tx['amount'])
    redis.call('INCRBY', 'balance:' .. tx['receiver'], tx['amount'])
  end
  redis.call('DECRBY', 'pending_out:' .. tx['sender'], tx['amount'])
  redis.call('HSET', KEYS[7], tx['tx_id'], ARGV[5])
end
redis.call('DEL', KEYS[6], KEYS[2], KEYS[8])
return #txs
"""
SEAL_ERRORS = {-1: "tip cambió", -2: "bloque viejo", -3: "hash repetido"}

# KEYS: chain:tip, chain, block:0, hash:<h>   ARGV: block_json, block_hash, users..., balance
GENESIS_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
redis.call('SET', KEYS[3], ARGV[1])
redis.call('SET', KEYS[4], '0')
redis.call('RPUSH', KEYS[2], ARGV[2])
redis.call('SET', KEYS[1], ARGV[2])
local bal = ARGV[#ARGV]
for i = 3, #ARGV - 1 do redis.call('SET', 'balance:' .. ARGV[i], bal) end
return 1
"""


def genesis_block() -> dict:
    """Bloque 0: no se mina. Le da saldo inicial a los 97 usuarios de bench.py."""
    txs = [{"tx_id": f"genesis-{i}", "sender": "genesis", "receiver": f"user-{i}",
            "amount": GENESIS_BALANCE_CENTS, "ts": 0} for i in range(GENESIS_USERS)]
    prev, digest = "0" * 32, pow.tx_digest(txs)
    return {"index": 0, "previous_hash": prev, "transactions": txs, "tx_digest": digest,
            "prefix": "", "nonce": 0, "block_hash": pow.md5_hex(prev + digest, 0), "timestamp": 0, "miner": None}


class Chain:
    def __init__(self, r, exchange: aio_pika.abc.AbstractExchange, channel, logger):
        self.r, self.exchange, self.channel, self.logger = r, exchange, channel, logger
        self._take = r.register_script(TAKE_LUA)
        self._seal = r.register_script(SEAL_LUA)
        self._genesis = r.register_script(GENESIS_LUA)
        self.wake = asyncio.Event()  # al sellar, el líder arma el siguiente sin esperar el loop

    async def ensure_genesis(self) -> None:
        g = genesis_block()
        users = [f"user-{i}" for i in range(GENESIS_USERS)]
        created = await self._genesis(
            keys=[keys.CHAIN_TIP, keys.CHAIN, keys.block(0), keys.hash_key(g["block_hash"])],
            args=[pow.canonical_json(g), g["block_hash"], *users, GENESIS_BALANCE_CENTS])
        if created:
            log(self.logger, "génesis creado", block_hash=g["block_hash"], users=GENESIS_USERS)

    async def current(self) -> dict | None:
        raw = await self.r.get(keys.MINING_CURRENT)
        return json.loads(raw) if raw else None

    async def pending_count(self) -> int:
        """Lo que mira bench.py: tx que todavía no están en un bloque sellado (pendientes + en vuelo)."""
        n = await self.r.llen(keys.TX_PENDING)
        cur = await self.current()
        if cur:
            n += await self.r.llen(keys.tx_inflight(cur["index"]))
        return n

    # ---- NCT.1: armado y publicación (solo el líder) ----

    async def leader_tick(self) -> None:
        cur = await self.current()
        if cur is None:
            cur = await self._assemble()
        if cur is not None:
            await self._publish_batch(cur)

    async def _assemble(self) -> dict | None:
        n = await self.r.llen(keys.TX_PENDING)
        if n == 0:
            return None
        first = json.loads(await self.r.lindex(keys.TX_PENDING, 0))
        # Regla de armado (decisiones §7): MAX_TX_PER_BLOCK tx o BLOCK_TIMEOUT desde la primera pendiente.
        if n < MAX_TX_PER_BLOCK and time.time() - first["ts"] < BLOCK_TIMEOUT:
            return None
        index = await self.r.llen(keys.CHAIN)  # el génesis es el 0
        prev = await self.r.get(keys.CHAIN_TIP)
        raw = await self._take(keys=[keys.TX_PENDING, keys.tx_inflight(index)], args=[MAX_TX_PER_BLOCK])
        txs = [json.loads(t) for t in raw]
        if not txs:
            return None
        plan = json.loads(await self.r.get(keys.POOL_PLAN) or "{}")
        digest = pow.tx_digest(txs)
        cur = {
            "v": 1,
            "block_id": f"{index}-{uuid.uuid4().hex[:8]}",
            "index": index,
            "previous_hash": prev,
            "tx_digest": digest,
            "base": prev + digest,
            # El prefijo y el rango se fijan al armar: un cambio del pool vale para el bloque siguiente.
            "prefix": plan.get("prefix", DIFFICULTY_PREFIX),
            "range_size": int(plan.get("range_size", DEFAULT_RANGE_SIZE)),
            "created": time.time(),
        }
        if not await self.r.set(keys.MINING_CURRENT, json.dumps(cur), nx=True):
            return await self.current()
        await self.r.set(keys.block_next_from(index), 0)
        log(self.logger, "bloque armado", block_id=cur["block_id"], txs=len(txs), prefix=cur["prefix"],
            range_size=cur["range_size"])
        return cur

    async def _publish_batch(self, cur: dict) -> None:
        """Rangos por tandas: si en la cola quedan menos tareas que mineros vivos, publica otra tanda."""
        miners = max(1, len(await alive_workers(self.r)))
        if await rabbit.queue_length(self.channel, rabbit.TASKS_QUEUE) >= miners:
            return
        size = cur["range_size"]
        for _ in range(miners * TASKS_PER_MINER):
            end = await self.r.incrby(keys.block_next_from(cur["index"]), size)
            start = end - size
            if start > NONCE_LIMIT:
                log(self.logger, "espacio de nonces agotado", block_id=cur["block_id"])
                return
            task = {"v": 1, "task_id": uuid.uuid4().hex, "block_id": cur["block_id"], "base": cur["base"],
                    "prefix": cur["prefix"], "from": start, "to": min(end - 1, NONCE_LIMIT), "attempts": 0}
            await self.exchange.publish(rabbit.message(task), routing_key="task.any")
            TASKS_PUBLISHED.inc()

    # ---- NCT.2 a NCT.4: resultados (cualquier réplica; el Lua garantiza un solo sellado) ----

    async def on_result(self, message: aio_pika.abc.AbstractIncomingMessage) -> None:
        async with message.process(requeue=True):  # ack recién después de verificar
            try:
                res = json.loads(message.body)
            except ValueError:
                RESULTS.labels("malformed").inc()
                return
            status = await self._handle(res)
            RESULTS.labels(status).inc()

    async def _handle(self, res: dict) -> str:
        cur = await self.current()
        if cur is None or res.get("block_id") != cur["block_id"]:
            return "stale"  # bloque ya sellado o resultado duplicado/tardío
        if not res.get("found"):
            return "empty"
        nonce = int(res["nonce"])
        if not (int(res["from"]) <= nonce <= int(res["to"])) or not pow.verify(cur["base"], cur["prefix"], nonce, res["hash"]):
            log(self.logger, "resultado inválido", worker=res.get("worker"), nonce=nonce)
            return "invalid"
        return await self._seal_block(cur, res, nonce)

    async def _seal_block(self, cur: dict, res: dict, nonce: int) -> str:
        index = cur["index"]
        txs = [json.loads(t) for t in await self.r.lrange(keys.tx_inflight(index), 0, -1)]
        block = {"index": index, "previous_hash": cur["previous_hash"], "transactions": txs,
                 "tx_digest": cur["tx_digest"], "prefix": cur["prefix"], "nonce": nonce,
                 "block_hash": res["hash"], "timestamp": time.time(), "miner": res.get("worker")}
        rc = await self._seal(
            keys=[keys.CHAIN_TIP, keys.MINING_CURRENT, keys.CHAIN, keys.block(index), keys.hash_key(res["hash"]),
                  keys.tx_inflight(index), keys.TX_STATUS, keys.block_next_from(index)],
            args=[cur["previous_hash"], cur["block_id"], pow.canonical_json(block), res["hash"], index])
        if rc < 0:
            log(self.logger, "sellado rechazado", block_id=cur["block_id"], reason=SEAL_ERRORS.get(rc, rc))
            return "rejected"
        await self.exchange.publish(
            rabbit.message({"v": 1, "block_id": cur["block_id"], "index": index, "block_hash": res["hash"]}),
            routing_key="block.solved")
        BLOCKS_SEALED.inc()
        BLOCK_SECONDS.observe(time.time() - cur["created"])
        log(self.logger, "bloque sellado", block_id=cur["block_id"], txs=rc, nonce=nonce,
            block_hash=res["hash"], miner=res.get("worker"), seconds=round(time.time() - cur["created"], 2))
        self.wake.set()
        return "sealed"
