"""Nombres de las claves de Redis (decisiones.docx §5). Un solo lugar para no tipear mal."""

CHAIN = "chain"                    # LIST de hashes en orden
CHAIN_TIP = "chain:tip"            # hash del último bloque sellado
TX_PENDING = "tx:pending"          # LIST de tx (JSON) esperando bloque
TX_SEEN = "tx:seen"                # SET de tx_id ya aceptados (duplicados)
TX_STATUS = "tx:status"            # HASH tx_id -> "pending" | índice del bloque
MINING_CURRENT = "mining:current"  # STRING JSON del bloque que se está minando (uno a la vez)
POOL_PLAN = "pool:plan"            # STRING JSON {prefix, range_size, ...} (lo escribe el pool)
POOL_SCALING = "pool:scaling"      # STRING JSON que sirve el gateway en GET /scaling
COORD_LEADER = "coord:leader"
POOL_LEADER = "pool:leader"
REAPER_LOCK = "gateway:reaper:lock"
LEASE_DEADLINES = "leases:deadline"  # ZSET task_id -> vencimiento (lo recorre el reaper)
POOL_MINERS = "pool:miners"        # STRING JSON: hashrate de cada minero vivo (lo escribe el pool, lo lee /miners)
POOL_LAST_WORK = "pool:last_work"  # STRING: última vez que hubo trabajo (mineros CPU: 60 s sin trabajo -> 0)
CANARY_COUNTER = "gateway:canary:counter"  # INCR: 1 de cada CANARY_EVERY tareas es un canario


def block(index: int) -> str:
    return f"block:{index}"


def block_next_from(index: int) -> str:
    return f"block:{index}:next_from"


def block_shares(index: int) -> str:
    """HASH miner_id -> nonces completados en ese bloque (para repartir el premio en el bloque siguiente)."""
    return f"block:{index}:shares"


def block_notfound(block_id: str, miner_id: str) -> str:
    """LIST de rangos que el minero contestó "no encontré" en ese bloque (se re-publican si falla un canario)."""
    return f"block:{block_id}:notfound:{miner_id}"


def hash_key(block_hash: str) -> str:
    return f"hash:{block_hash}"


def tx_inflight(index: int) -> str:
    return f"tx:inflight:{index}"


def balance(user: str) -> str:
    return f"balance:{user}"


def pending_out(user: str) -> str:
    return f"pending_out:{user}"


def worker(worker_id: str) -> str:
    return f"worker:{worker_id}"


def lease(task_id: str) -> str:
    return f"lease:{task_id}"


def miner_results(miner_id: str) -> str:
    """LIST de los últimos resultados del minero {hashes, engine_seconds, seconds, ts}: su hashrate real."""
    return f"miner:{miner_id}:results"


def miner_calibration(miner_id: str) -> str:
    """STRING JSON {hashrate, class, kind}: la tarea de calibración que hizo al arrancar."""
    return f"miner:{miner_id}:calibration"


def miner_expired(miner_id: str) -> str:
    """STRING JSON {streak, ts}: préstamos vencidos seguidos y el último (minero zombie / cuarentena)."""
    return f"miner:{miner_id}:expired"


def quarantine(miner_id: str) -> str:
    """STRING con TTL: mientras exista, el gateway no le da tareas y su hashrate no cuenta."""
    return f"quarantine:{miner_id}"
