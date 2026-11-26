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

from tpi_common import keys, pow, quarantine, rabbit
from tpi_common.config import DIFFICULTY_PREFIX, env_float, env_int, env_str
from tpi_common.logs import log
from tpi_common.store import alive_workers

from .reward import REWARD_SENDER, block_subsidy, miner_account, reward_transactions, shares_by_account

MAX_TX_PER_BLOCK = env_int("MAX_TX_PER_BLOCK", 1000)
BLOCK_WINDOW = env_float("BLOCK_WINDOW_SECONDS", 10)
BLOCK_REWARD_CENTS = env_int("BLOCK_REWARD_CENTS", 5000)  # subsidio inicial
HALVING_BLOCKS = env_int("HALVING_BLOCKS", 1000)          # el subsidio baja a la mitad cada tantos bloques
MODE = env_str("DIFFICULTY_MODE", "auto")
TARGET = env_float("BLOCK_TARGET_SECONDS", 10)
REARM_FACTOR = env_float("REARM_FACTOR", 6)          # rearmar si va a tardar más de 6 x el objetivo
REARM_MIN_AGE = env_float("REARM_MIN_AGE_SECONDS", 20)  # y el bloque lleva al menos esto (no oscilar)
QUARANTINE_SECONDS = env_int("QUARANTINE_SECONDS", 300)
QUARANTINE_MAX_SECONDS = env_int("QUARANTINE_MAX_SECONDS", 3600)
DEFAULT_RANGE_SIZE = env_int("DEFAULT_RANGE_SIZE", 1_000_000)  # si todavía no hay pool:plan
TASKS_PER_MINER = env_int("TASKS_PER_MINER_BATCH", 2)  # tamaño de tanda = mineros vivos x esto
SHARES_PER_TASK = env_int("SHARES_PER_TASK", 32)  # el prefijo de las shares se elige para que salgan ~tantas
GENESIS_USERS = env_int("GENESIS_USERS", 97)  # user-0..user-96, como bench.py
GENESIS_BALANCE_CENTS = env_int("GENESIS_BALANCE_CENTS", 100_000_000)
NONCE_LIMIT = 2**63 - 1  # INCRBY de Redis es int64 con signo

BLOCKS_SEALED = Counter("blocks_sealed_total", "Bloques sellados")
BLOCK_SECONDS = Histogram("block_seconds", "Tiempo desde que se arma el bloque hasta que se sella",
                          ["prefix_len"],
                          buckets=(1, 2, 5, 10, 20, 30, 60, 120, 300, 600, 1800))
BLOCK_VALIDATION_SECONDS = Histogram("block_validation_seconds", "Validación del PoW, shares y sellado en Redis",
                                     buckets=(.001, .005, .01, .025, .05, .1, .25, .5, 1, 2, 5))
RESULTS = Counter("results_total", "Resultados recibidos", ["status", "kind"])
TASKS_PUBLISHED = Counter("tasks_published_total", "Tareas publicadas en mining.tasks")
EVENTS_PUBLISHED = Counter("outbox_events_published_total", "Eventos del outbox publicados en RabbitMQ")
REARMED = Counter("blocks_rearmed_total", "Bloques rearmados con un prefijo más corto (cayó el hashrate)")
QUARANTINED = Counter("quarantined_total", "Mineros puestos en cuarentena", ["reason"])
SHARES = Counter("shares_total", "Shares recibidas", ["result"])
BLOCK_TXS = Histogram("block_transactions", "Transacciones por bloque (sin las de premio)",
                      buckets=(1, 10, 50, 100, 250, 500, 1000, 2500, 5000, 10000))

# KEYS: tx:pending, tx:inflight:<i>   ARGV: max tx, inicio de la ventana, fin de la ventana, tx de premio (JSON)...
# Toma desde el principio solo las tx de la ventana (inicio <= ts < fin), hasta el tope. Las de premio van primero.
# Corta en la primera tx de otra ventana, también si es de una anterior (21/11): dos tx que llegan casi juntas pueden
# quedar en la cola en orden inverso a su hora, y antes la de la ventana anterior se colaba en este bloque.
# Si ya hay tx en vuelo para ese índice (el líder anterior murió a mitad), se reusan.
TAKE_LUA = """
if redis.call('EXISTS', KEYS[2]) == 1 then return redis.call('LRANGE', KEYS[2], 0, -1) end
local txs = redis.call('LRANGE', KEYS[1], 0, tonumber(ARGV[1]) - 1)
local n = 0
for i, raw in ipairs(txs) do
  local ts = tonumber(cjson.decode(raw)['ts'])
  if ts < tonumber(ARGV[2]) or ts >= tonumber(ARGV[3]) then break end
  n = i
end
if n == 0 then return {} end
redis.call('LTRIM', KEYS[1], n, -1)
for i = 4, #ARGV do redis.call('RPUSH', KEYS[2], ARGV[i]) end
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
#       block:<i-1>:shares (ya pagado en este bloque), events:outbox, block:<i-1>:sharenonces
# ARGV: prev_hash, block_id, block_json, block_hash, index, evento block.solved (JSON)
# El evento se guarda en el outbox en el mismo script que sella (transactional outbox): si el pod muere antes
# de publicarlo en RabbitMQ, el bloque y su evento quedan los dos, y otro lo publica después.
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
    local fee = tonumber(tx['fee'] or 0)
    if tx['sender'] ~= tx['receiver'] then
      redis.call('DECRBY', 'balance:' .. tx['sender'], tx['amount'])
      redis.call('INCRBY', 'balance:' .. tx['receiver'], tx['amount'])
    end
    -- la comisión sale del sender acá y vuelve como parte del premio del bloque siguiente
    if fee > 0 then redis.call('DECRBY', 'balance:' .. tx['sender'], fee) end
    redis.call('DECRBY', 'pending_out:' .. tx['sender'], tx['amount'] + fee)
  end
  redis.call('HSET', KEYS[7], tx['tx_id'], ARGV[5])
end
redis.call('DEL', KEYS[6], KEYS[2], KEYS[8], KEYS[9], KEYS[11])
redis.call('RPUSH', KEYS[10], ARGV[6])
return #txs
"""
SEAL_ERRORS = {-1: "tip cambió", -2: "bloque viejo", -3: "hash repetido"}

# KEYS: block:<i>:sharenonces, block:<i>:shares   ARGV: minero, peso, nonces...
# Suma el peso de cada share que todavía no se había pagado en ese bloque (la misma share no cobra dos veces).
# Sin vencimiento (21/11): son el derecho a cobrar el premio, que se paga al armar el bloque siguiente, y ese bloque
# puede salir mucho después (sin tx no hay bloques). Se borran en SEAL_LUA del bloque siguiente, ya pagadas.
SHARES_LUA = """
local n = 0
for i = 3, #ARGV do
  if redis.call('SADD', KEYS[1], ARGV[i]) == 1 then n = n + 1 end
end
if n > 0 then redis.call('HINCRBY', KEYS[2], ARGV[1], n * tonumber(ARGV[2])) end
return n
"""

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


def is_rearm_of(res_block_id: str, cur: dict) -> bool:
    """Un rearmado conserva índice y base; cambia el identificador y acorta el prefijo."""
    return (isinstance(res_block_id, str) and res_block_id != cur["block_id"]
            and res_block_id.startswith(f"{cur['index']}-"))


def solved_event(block: dict, block_id: str) -> dict:
    """Evento block.solved: lo usan el gateway (descarta tareas de ese block_id) y el notifier (avisa a los
    navegadores). Lleva cada tx con origen y destino para que el notifier sepa a quién avisar sin leer el bloque."""
    return {"v": 1, "event_id": f"block-{block['index']}", "type": "block.solved", "block_id": block_id,
            "index": block["index"], "block_hash": block["block_hash"], "timestamp": block["timestamp"],
            "miner": block["miner"],
            "txs": [{"tx_id": t["tx_id"], "sender": t["sender"], "receiver": t["receiver"], "amount": t["amount"]}
                    for t in block["transactions"]]}


def genesis_block() -> dict:
    """Bloque 0: no se mina. Le da saldo inicial a los 97 usuarios de bench.py."""
    txs = [{"tx_id": f"genesis-{i}", "sender": "genesis", "receiver": f"user-{i}",
            "amount": GENESIS_BALANCE_CENTS, "ts": 0} for i in range(GENESIS_USERS)]
    prev, digest = "0" * 32, pow.tx_digest(txs)
    return {"index": 0, "previous_hash": prev, "transactions": txs, "tx_digest": digest,
            "prefix": "", "nonce": 0, "block_hash": pow.md5_hex(prev + digest, 0), "timestamp": 0, "miner": None}


def new_task(cur: dict, start: int, end: int) -> dict:
    """Tarea del rango [start, end) del bloque en curso. Los canarios del gateway copian este formato (B1.1)."""
    return {"v": 1, "task_id": uuid.uuid4().hex, "block_id": cur["block_id"], "base": cur["base"],
            "prefix": cur["prefix"], "from": start, "to": min(end - 1, NONCE_LIMIT), "attempts": 0,
            "share_prefix": pow.share_prefix(cur["prefix"], cur["range_size"], SHARES_PER_TASK),
            "published_at": time.time()}


def verified_range(worker: str, lo: int, hi: int, share_prefix: str, shares: list[int]) -> dict:
    """Un rango ya resuelto con sus shares verificadas con MD5: el gateway lo usa de canario para otro minero."""
    return {"from": lo, "to": hi, "share_prefix": share_prefix, "shares": shares, "worker": worker}


VERIFIED_KEPT = 200  # rangos verificados que se guardan por bloque (de ahí salen los canarios)


class Chain:
    def __init__(self, r, exchange: aio_pika.abc.AbstractExchange, channel, logger):
        self.r, self.exchange, self.channel, self.logger = r, exchange, channel, logger
        self._take = r.register_script(TAKE_LUA)
        self._rearm = r.register_script(REARM_LUA)
        self._seal = r.register_script(SEAL_LUA)
        self._genesis = r.register_script(GENESIS_LUA)
        self._shares = r.register_script(SHARES_LUA)
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
        # El lote en vuelo se cuenta por el índice que sigue en la cadena, no por mining:current: si el líder murió
        # entre sacar el lote de pendientes y guardar el candidato, el lote existe aunque no haya candidato.
        index = await self.r.llen(keys.CHAIN)
        return await self.r.llen(keys.TX_PENDING) + await self.r.llen(keys.tx_inflight(index))

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
        """Premio del bloque anterior (subsidio + comisiones, su `reward_cents`), repartido según los nonces que
        completó cada minero."""
        prev_index = index - 1
        if prev_index < 1:
            return []
        shares = await self.r.hgetall(keys.block_shares(prev_index))
        raw = await self.r.get(keys.block(prev_index))
        prev = json.loads(raw) if raw else {}
        finder = prev.get("miner")
        miners = list(shares) + ([finder] if finder else [])
        accounts = {m: a for m in miners if (a := await self.r.get(keys.miner_account(m)))}
        finder_account = (accounts.get(finder) or miner_account(finder)) if finder else None
        return reward_transactions(prev_index, shares_by_account(shares, accounts),
                                   int(prev.get("reward_cents", BLOCK_REWARD_CENTS)), finder_account, round(time.time(), 3))

    async def _assemble(self) -> dict | None:
        index = await self.r.llen(keys.CHAIN)  # el génesis es el 0
        # Un lote en vuelo sin candidato: el líder anterior murió entre sacarlo de pendientes y guardar
        # mining:current. Se retoma ya (su ventana cerró), aunque la cola de pendientes esté vacía; TAKE_LUA lo
        # devuelve tal cual, con sus premios, sin tomar nada nuevo.
        resume = await self.r.exists(keys.tx_inflight(index))
        end = 0.0
        if not resume:
            first = await self.r.lindex(keys.TX_PENDING, 0)
            if first is None:
                return None  # sin tx no hay bloque (no se minan bloques vacíos)
            end = window_end(json.loads(first)["ts"], BLOCK_WINDOW)
            if time.time() < end:
                return None  # la ventana de la primera pendiente todavía no cerró
        prev = await self.r.get(keys.CHAIN_TIP)
        rewards = await self._rewards(index)
        raw = await self._take(keys=[keys.TX_PENDING, keys.tx_inflight(index)],
                               args=[MAX_TX_PER_BLOCK, end - BLOCK_WINDOW, end,
                                     *(pow.canonical_json(t) for t in rewards)])
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
            prefix=cur["prefix"], range_size=cur["range_size"], retomado=bool(resume))
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
            task = new_task(cur, start, end)
            await self.exchange.publish(rabbit.message(task), routing_key="task.any")
            TASKS_PUBLISHED.inc()

    # ---- NCT.2 a NCT.4: resultados (cualquier réplica; el Lua garantiza un solo sellado) ----

    async def on_result(self, message: aio_pika.abc.AbstractIncomingMessage) -> None:
        async with message.process(requeue=True):  # ack recién después de verificar
            kind = {"result.cpu": "cpu", "result.gpu": "gpu"}.get(message.routing_key, "unknown")
            try:
                res = json.loads(message.body)
            except ValueError:
                RESULTS.labels("malformed", kind).inc()
                return
            status = await self._handle(res)
            RESULTS.labels(status, kind).inc()

    async def _handle(self, res: dict) -> str:
        cur = await self.current()
        lo, hi = int(res["from"]), int(res["to"])
        if cur is None:
            return "stale"
        if res.get("block_id") != cur["block_id"]:
            if not is_rearm_of(res.get("block_id"), cur):
                return "stale"
            # Misma base: verificar un ganador tardío contra el prefijo vigente antes de sellar.
            if not res.get("found"):
                await self._add_shares(cur, res, lo, hi)
                return "stale"
        if not res.get("found"):
            await self._add_shares(cur, res, lo, hi)
            return "empty"
        validation_started = time.perf_counter()
        try:
            nonce = int(res.get("nonce"))
        except (TypeError, ValueError):
            nonce = -1  # "encontré" sin un nonce que sea un número: inválido
        if not (lo <= nonce <= hi) or not pow.verify(cur["base"], cur["prefix"], nonce, str(res.get("hash", ""))):
            BLOCK_VALIDATION_SECONDS.observe(time.perf_counter() - validation_started)
            # Un "encontré" que no verifica es la prueba más clara de un motor roto (o de un tramposo):
            # cuarentena, no cobra el bloque, y su rango y sus "no encontré" los hace otro minero.
            worker = res.get("worker")
            if worker:
                strikes, seconds = await quarantine.strike(self.r, worker, QUARANTINE_SECONDS, QUARANTINE_MAX_SECONDS)
                n = await quarantine.penalize(self.r, self.exchange, cur, worker, "resultado_invalido",
                                              seconds, extra=[(lo, hi)])
                QUARANTINED.labels("resultado_invalido").inc()
                log(self.logger, "resultado inválido: minero en cuarentena", worker=worker, nonce=nonce,
                    seconds=seconds, vez=strikes, rangos_republicados=n)
                if h := await self.r.get(keys.worker_token_of(worker)):  # mentira comprobada: como un canario
                    await self.r.delete(keys.worker_token(h))
                    log(self.logger, "token revocado", miner_id=worker, motivo="resultado_invalido")
            return "invalid"
        await self._add_shares(cur, res, lo, hi)
        return await self._seal_block(cur, res, nonce, validation_started)

    async def _add_shares(self, cur: dict, res: dict, lo: int, hi: int) -> int:
        """Trabajo de cada minero en el bloque (shares, decidido el 20/11): cada share se verifica con MD5 acá y
        pesa 16^largo de su prefijo, que es lo que cuesta encontrarla. Un minero que dice "no encontré" sin haber
        minado no tiene shares y no cobra. Un worker en prueba todavía no cobra. Se suma por índice, así cuenta
        aunque el bloque se rearme (la base es la misma)."""
        worker, sp = res.get("worker"), res.get("share_prefix") or ""
        # Las shares del prefijo anterior (más difícil) siguen siendo trabajo verificable tras rearmar.
        if not worker or not sp or not (cur["prefix"].startswith(sp) or sp.startswith(cur["prefix"])):
            return 0
        good = pow.valid_shares(cur["base"], sp, lo, hi, res.get("shares"))
        sent = len(res.get("shares") or [])
        SHARES.labels("ok").inc(len(good))
        SHARES.labels("invalida").inc(max(0, sent - len(good)))
        if good and not res.get("found"):
            # Rango resuelto con shares verificadas: sirve de canario para otro minero (B1.1, 26/11). Se guarda aunque
            # el worker esté en prueba: lo que se guarda es lo que verificó el MD5 de acá, no lo que dijo él.
            vkey = keys.block_verified_ranges(cur["index"])
            await self.r.lpush(vkey, json.dumps(verified_range(worker, lo, hi, sp, good)))
            await self.r.ltrim(vkey, 0, VERIFIED_KEPT - 1)
            await self.r.expire(vkey, 3600)
        if not good or await self.r.exists(keys.worker_trial(worker)):
            return 0
        return await self._shares(keys=[keys.block_share_nonces(cur["index"]), keys.block_shares(cur["index"])],
                                  args=[worker, pow.share_weight(sp), *good])

    async def _seal_block(self, cur: dict, res: dict, nonce: int, validation_started: float | None = None) -> str:
        index = cur["index"]
        txs = [json.loads(t) for t in await self.r.lrange(keys.tx_inflight(index), 0, -1)]
        subsidy = block_subsidy(index, BLOCK_REWARD_CENTS, HALVING_BLOCKS)
        fees = sum(int(t.get("fee", 0)) for t in txs if t["sender"] != REWARD_SENDER)
        block = {"index": index, "previous_hash": cur["previous_hash"], "transactions": txs,
                 "tx_digest": cur["tx_digest"], "prefix": cur["prefix"], "nonce": nonce,
                 "block_hash": res["hash"], "timestamp": time.time(), "miner": res.get("worker"),
                 "subsidy_cents": subsidy, "fees_cents": fees, "reward_cents": subsidy + fees,
                 "rearmed": cur.get("rearmed", 0)}
        rc = await self._seal(
            keys=[keys.CHAIN_TIP, keys.MINING_CURRENT, keys.CHAIN, keys.block(index), keys.hash_key(res["hash"]),
                  keys.tx_inflight(index), keys.TX_STATUS, keys.block_next_from(index),
                  keys.block_shares(index - 1), keys.EVENTS_OUTBOX, keys.block_share_nonces(index - 1)],
            args=[cur["previous_hash"], cur["block_id"], pow.canonical_json(block), res["hash"], index,
                  json.dumps(solved_event(block, cur["block_id"]))])
        if validation_started is not None:
            BLOCK_VALIDATION_SECONDS.observe(time.perf_counter() - validation_started)
        if rc < 0:
            log(self.logger, "sellado rechazado", block_id=cur["block_id"], reason=SEAL_ERRORS.get(rc, rc))
            return "stale" if rc in (-1, -2) else "rejected"
        await self.flush_outbox()
        BLOCKS_SEALED.inc()
        seconds = time.time() - cur.get("assembled", cur["created"])
        BLOCK_SECONDS.labels(str(len(cur["prefix"]))).observe(seconds)
        log(self.logger, "bloque sellado", block_id=cur["block_id"], txs=rc, nonce=nonce,
            block_hash=res["hash"], miner=res.get("worker"), seconds=round(seconds, 2), prefix=cur["prefix"])
        self.wake.set()
        return "sealed"

    async def flush_outbox(self) -> int:
        """Publica los eventos guardados al sellar y los saca del outbox recién después de publicarlos. Si dos
        réplicas lo hacen a la vez, un evento puede salir dos veces: los que escuchan descartan repetidos por
        event_id (el gateway por block_id)."""
        sent = 0
        for raw in await self.r.lrange(keys.EVENTS_OUTBOX, 0, 49):
            await self.exchange.publish(rabbit.message(json.loads(raw)), routing_key="block.solved")
            await self.r.lrem(keys.EVENTS_OUTBOX, 1, raw)
            EVENTS_PUBLISHED.inc()
            sent += 1
        return sent
