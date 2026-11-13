"""NCT.1 a NCT.4: armar el bloque, publicar las tareas por tandas, verificar y sellar.

Un solo bloque a la vez. El estado vive en Redis (mining:current, block:<i>:next_from,
tx:inflight:<i>), así que si el líder se cae otra réplica sigue desde donde quedó.

Armado (decidido el 13/11, comentario del profe): ventanas fijas de reloj de BLOCK_WINDOW_SECONDS.
Cuando cierra la ventana de la primera tx pendiente, sus tx salen en bloques de hasta MAX_TX_PER_BLOCK
que se minan uno detrás del otro. Un bloque nunca mezcla dos ventanas y no hay bloques vacíos.
"""
import asyncio
import json
import math
import time
import uuid

import aio_pika
from prometheus_client import Counter, Gauge, Histogram

from tpi_common import keys, pow, rabbit
from tpi_common.config import DIFFICULTY_PREFIX, env_float, env_int, env_str
from tpi_common.logs import log
from tpi_common.store import alive_workers

from .reward import REWARD_SENDER, miner_account, reward_transactions, shares_by_account

MAX_TX_PER_BLOCK = env_int("MAX_TX_PER_BLOCK", 1000)
BLOCK_WINDOW = env_float("BLOCK_WINDOW_SECONDS", 10)
BLOCK_REWARD_CENTS = env_int("BLOCK_REWARD_CENTS", 5000)
MODE = env_str("DIFFICULTY_MODE", "auto")
TARGET = env_float("BLOCK_TARGET_SECONDS", 10)
REARM_FACTOR = env_float("REARM_FACTOR", 6)          # rearmar si va a tardar más de 6 x el objetivo
REARM_MIN_AGE = env_float("REARM_MIN_AGE_SECONDS", 20)  # y el bloque lleva al menos esto (no oscilar)
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
REARMED = Counter("blocks_rearmed_total", "Bloques rearmados con un prefijo más corto (cayó el hashrate)")
BLOCK_TXS = Histogram("block_transactions", "Transacciones por bloque (sin las de premio)",
                      buckets=(1, 10, 50, 100, 250, 500, 1000, 2500, 5000, 10000))

# KEYS: tx:pending, tx:inflight:<i>   ARGV: max tx, fin de la ventana, tx de premio (JSON)...
# Toma desde el principio solo las tx de la ventana (ts < fin), hasta el tope. Las de premio van primero.
# Si ya hay tx en vuelo para ese índice (el líder anterior murió a mitad), se reusan.
TAKE_LUA = """
if redis.call('EXISTS', KEYS[2]) == 1 then return redis.call('LRANGE', KEYS[2], 0, -1) end
local txs = redis.call('LRANGE', KEYS[1], 0, tonumber(ARGV[1]) - 1)
local n = 0
for i, raw in ipairs(txs) do
  if tonumber(cjson.decode(raw)['ts']) >= tonumber(ARGV[2]) then break end
  n = i
end
if n == 0 then return {} end
redis.call('LTRIM', KEYS[1], n, -1)
for i = 3, #ARGV do redis.call('RPUSH', KEYS[2], ARGV[i]) end
for i = 1, n do redis.call('RPUSH', KEYS[2], txs[i]) end
return redis.call('LRANGE', KEYS[2], 0, -1)
"""

# KEYS: mining:current, block:<i>:next_from   ARGV: block_id viejo, candidato nuevo (JSON)
REARM_LUA = """
local cur = redis.call('GET', KEYS[1])
if not cur or cjson.decode(cur)['block_id'] ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[2])
redis.call('SET', KEYS[2], 0)
return 1
"""

# KEYS: chain:tip, mining:current, chain, block:<i>, hash:<h>, tx:inflight:<i>, tx:status, block:<i>:next_from,
#       block:<i-1>:shares (ya pagado en este bloque)
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
  if tx['sender'] == '""" + REWARD_SENDER + """' then
    redis.call('INCRBY', 'balance:' .. tx['receiver'], tx['amount'])
  else
    if tx['sender'] ~= tx['receiver'] then
      redis.call('DECRBY', 'balance:' .. tx['sender'], tx['amount'])
      redis.call('INCRBY', 'balance:' .. tx['receiver'], tx['amount'])
    end
    redis.call('DECRBY', 'pending_out:' .. tx['sender'], tx['amount'])
  end
  redis.call('HSET', KEYS[7], tx['tx_id'], ARGV[5])
end
redis.call('DEL', KEYS[6], KEYS[2], KEYS[8], KEYS[9])
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


def window_end(ts: float, window: float) -> float:
    """Ventanas fijas de reloj: [0, w), [w, 2w)... Devuelve cuándo cierra la ventana de `ts`."""
    return (math.floor(ts / window) + 1) * window


def should_rearm(prefix_len: int, planned_len: int, hashrate: float, target_seconds: float, factor: float,
                 age: float, min_age: float) -> bool:
    """Rearmar el bloque en curso si con el hashrate que queda va a tardar mucho más que el objetivo."""
    if planned_len >= prefix_len or age < min_age:
        return False
    return pow.expected_seconds(prefix_len, hashrate) > target_seconds * factor


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
        self._rearm = r.register_script(REARM_LUA)
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
        else:
            cur = await self._maybe_rearm(cur)
        if cur is not None:
            await self._publish_batch(cur)

    async def _rewards(self, index: int) -> list[dict]:
        """Premio del bloque anterior, repartido según los nonces que completó cada minero."""
        prev_index = index - 1
        if prev_index < 1:
            return []
        shares = await self.r.hgetall(keys.block_shares(prev_index))
        raw = await self.r.get(keys.block(prev_index))
        finder = json.loads(raw).get("miner") if raw else None
        miners = list(shares) + ([finder] if finder else [])
        accounts = {m: a for m in miners if (a := await self.r.get(keys.miner_account(m)))}
        finder_account = (accounts.get(finder) or miner_account(finder)) if finder else None
        return reward_transactions(prev_index, shares_by_account(shares, accounts), BLOCK_REWARD_CENTS,
                                   finder_account, round(time.time(), 3))

    async def _assemble(self) -> dict | None:
        first = await self.r.lindex(keys.TX_PENDING, 0)
        if first is None:
            return None  # sin tx no hay bloque (no se minan bloques vacíos)
        end = window_end(json.loads(first)["ts"], BLOCK_WINDOW)
        if time.time() < end:
            return None  # la ventana de la primera pendiente todavía no cerró
        index = await self.r.llen(keys.CHAIN)  # el génesis es el 0
        prev = await self.r.get(keys.CHAIN_TIP)
        rewards = await self._rewards(index)
        raw = await self._take(keys=[keys.TX_PENDING, keys.tx_inflight(index)],
                               args=[MAX_TX_PER_BLOCK, end, *(pow.canonical_json(t) for t in rewards)])
        txs = [json.loads(t) for t in raw]
        if not txs:
            return None
        plan = json.loads(await self.r.get(keys.POOL_PLAN) or "{}")
        digest = pow.tx_digest(txs)
        now = time.time()
        cur = {
            "v": 1,
            "block_id": f"{index}-{uuid.uuid4().hex[:8]}",
            "index": index,
            "previous_hash": prev,
            "tx_digest": digest,
            "base": prev + digest,
            # El prefijo y el rango se fijan al armar: un cambio del pool vale para el bloque siguiente
            # (salvo que se rearme porque cayó el hashrate).
            "prefix": plan.get("prefix", DIFFICULTY_PREFIX),
            "range_size": int(plan.get("range_size", DEFAULT_RANGE_SIZE)),
            "created": now,
            "assembled": now,
        }
        if not await self.r.set(keys.MINING_CURRENT, json.dumps(cur), nx=True):
            return await self.current()
        await self.r.set(keys.block_next_from(index), 0)
        user_txs = sum(t["sender"] != REWARD_SENDER for t in txs)
        BLOCK_TXS.observe(user_txs)
        log(self.logger, "bloque armado", block_id=cur["block_id"], txs=user_txs, rewards=len(txs) - user_txs,
            prefix=cur["prefix"], range_size=cur["range_size"])
        return cur

    async def _maybe_rearm(self, cur: dict) -> dict:
        """Si se fue la GPU a mitad del bloque: mismas tx y misma base, prefijo más corto y block_id nuevo.
        El gateway descarta solo las tareas del block_id viejo y el sellado ya exige el block_id vigente."""
        if MODE != "auto":
            return cur
        plan = json.loads(await self.r.get(keys.POOL_PLAN) or "{}")
        if "prefix" not in plan:
            return cur
        if not should_rearm(len(cur["prefix"]), len(plan["prefix"]), plan.get("hashrate", 0), TARGET,
                            REARM_FACTOR, time.time() - cur["created"], REARM_MIN_AGE):
            return cur
        new = {**cur, "block_id": f"{cur['index']}-{uuid.uuid4().hex[:8]}", "prefix": plan["prefix"],
               "range_size": int(plan["range_size"]), "created": time.time(),
               "rearmed": cur.get("rearmed", 0) + 1}
        if not await self._rearm(keys=[keys.MINING_CURRENT, keys.block_next_from(cur["index"])],
                                 args=[cur["block_id"], json.dumps(new)]):
            return await self.current() or cur
        REARMED.inc()
        log(self.logger, "bloque rearmado", old=cur["block_id"], block_id=new["block_id"], before=cur["prefix"],
            after=new["prefix"], hashrate=round(plan.get("hashrate", 0)))
        return new

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
        lo, hi = int(res["from"]), int(res["to"])
        if cur is None or res.get("block_id") != cur["block_id"]:
            # El bloque se rearmó (mismo índice, block_id viejo): el rango recorrido cuenta igual para el premio.
            if cur is not None and str(res.get("block_id", "")).startswith(f"{cur['index']}-") and not res.get("found"):
                await self._add_share(cur["index"], res.get("worker"), hi - lo + 1)
            return "stale"  # bloque ya sellado o resultado duplicado/tardío
        if not res.get("found"):
            await self._add_share(cur["index"], res.get("worker"), hi - lo + 1)
            return "empty"
        nonce = int(res["nonce"])
        if not (lo <= nonce <= hi) or not pow.verify(cur["base"], cur["prefix"], nonce, res["hash"]):
            log(self.logger, "resultado inválido", worker=res.get("worker"), nonce=nonce)
            return "invalid"
        await self._add_share(cur["index"], res.get("worker"), nonce - lo + 1)
        return await self._seal_block(cur, res, nonce)

    async def _add_share(self, index: int, worker: str | None, nonces: int) -> None:
        """Trabajo de cada minero en el bloque: el tamaño del rango que fijó el coordinator (hasta el nonce si
        lo encontró), no los `hashes` que informa el minero. Se suma por índice, así cuenta aunque se rearme."""
        if worker:
            await self.r.hincrby(keys.block_shares(index), worker, nonces)
            await self.r.expire(keys.block_shares(index), 86400)

    async def _seal_block(self, cur: dict, res: dict, nonce: int) -> str:
        index = cur["index"]
        txs = [json.loads(t) for t in await self.r.lrange(keys.tx_inflight(index), 0, -1)]
        block = {"index": index, "previous_hash": cur["previous_hash"], "transactions": txs,
                 "tx_digest": cur["tx_digest"], "prefix": cur["prefix"], "nonce": nonce,
                 "block_hash": res["hash"], "timestamp": time.time(), "miner": res.get("worker"),
                 "reward_cents": BLOCK_REWARD_CENTS, "rearmed": cur.get("rearmed", 0)}
        rc = await self._seal(
            keys=[keys.CHAIN_TIP, keys.MINING_CURRENT, keys.CHAIN, keys.block(index), keys.hash_key(res["hash"]),
                  keys.tx_inflight(index), keys.TX_STATUS, keys.block_next_from(index),
                  keys.block_shares(index - 1)],
            args=[cur["previous_hash"], cur["block_id"], pow.canonical_json(block), res["hash"], index])
        if rc < 0:
            log(self.logger, "sellado rechazado", block_id=cur["block_id"], reason=SEAL_ERRORS.get(rc, rc))
            return "rejected"
        await self.exchange.publish(
            rabbit.message({"v": 1, "block_id": cur["block_id"], "index": index, "block_hash": res["hash"]}),
            routing_key="block.solved")
        BLOCKS_SEALED.inc()
        seconds = time.time() - cur.get("assembled", cur["created"])
        BLOCK_SECONDS.observe(seconds)
        log(self.logger, "bloque sellado", block_id=cur["block_id"], txs=rc, nonce=nonce,
            block_hash=res["hash"], miner=res.get("worker"), seconds=round(seconds, 2), prefix=cur["prefix"])
        self.wake.set()
        return "sealed"
