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
EVENTS_OUTBOX = "events:outbox"    # LIST de eventos (JSON) que se guardan al sellar y todavía no se publicaron


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


def account_pubkey(user: str) -> str:
    """Clave pública Ed25519 (hex) de una cuenta registrada. Se escribe una vez (SET NX)."""
    return f"account:{user}:pubkey"


def worker_token(token_hash: str) -> str:
    """Token propio de un worker (alta pública): se guarda el sha256, nunca el token. Valor: {miner_id, account}."""
    return f"workertoken:{token_hash}"


def worker_token_of(miner_id: str) -> str:
    """Hash del token de un worker, para revocarlo y para que nadie registre dos veces el mismo nombre."""
    return f"workertoken:of:{miner_id}"


def account_seq(user: str) -> str:
    """Último seq usado por una cuenta registrada (anti-replay)."""
    return f"account:{user}:seq"


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


def miner_account(miner_id: str) -> str:
    """STRING con TTL de 1 día: a qué cuenta se le paga el premio de ese minero (MINER_ACCOUNT del agente).
    Dura más que el keep-alive porque el premio se paga en el bloque siguiente, y el pod ya puede no existir."""
    return f"miner:{miner_id}:account"


def quarantine(miner_id: str) -> str:
    """STRING con TTL: mientras exista, el gateway no le da tareas y su hashrate no cuenta."""
    return f"quarantine:{miner_id}"


def worker_trial(miner_id: str) -> str:
    """Worker 'en prueba' (alta pública, punto 4): canarios pasados hasta ahora. Mientras exista, recibe más
    canarios y su trabajo no cobra. No empieza con worker: porque alive_workers escanea worker:*."""
    return f"trial:{miner_id}"


def account_workers(account: str) -> str:
    """SET de workers dados de alta por una cuenta (límite MAX_WORKERS_PER_ACCOUNT; los revocados cuentan)."""
    return f"workers:of:{account}"


def block_share_nonces(index: int) -> str:
    """SET de los nonces de shares ya pagadas en ese bloque: la misma share no se cobra dos veces."""
    return f"block:{index}:sharenonces"


def miner_tasks(miner_id: str) -> str:
    """INCR por tarea prestada a ese minero (para darle un canario cada tantas)."""
    return f"miner:{miner_id}:tasks"
