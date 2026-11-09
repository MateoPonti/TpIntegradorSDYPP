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


def block(index: int) -> str:
    return f"block:{index}"


def block_next_from(index: int) -> str:
    return f"block:{index}:next_from"


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
