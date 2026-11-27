"""Coordinator (NCT) + API pública. Todas las réplicas atienden HTTP y verifican resultados;
solo el líder (coord:leader) arma bloques y publica tareas."""
import asyncio
import contextlib
import json
import time
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from prometheus_client import Counter, Gauge

from tpi_common import firmas, keys, pow, rabbit, ratelimit, store
from tpi_common.config import CHAIN_ID, POD_NAME, env_float, env_int, env_str
from tpi_common.logs import log, setup_logging
from tpi_common.web import install_common_routes

from . import accounts as cuentas
from . import crowdfund
from . import ledger
from . import multisig
from .blocks import BLOCK_REWARD_CENTS, HALVING_BLOCKS, Chain
from .reward import REWARD_SENDER, block_subsidy

logger = setup_logging("coordinator")
POLL = env_float("BLOCK_POLL_SECONDS", 1.0)  # duda 5 (propuesta): loop cada 1 s
GATEWAY_HEALTH_URL = env_str("GATEWAY_HEALTH_URL", "http://gateway:8080/health")
NOTIFIER_HEALTH_URL = env_str("NOTIFIER_HEALTH_URL", "http://notifier:8080/health")
GATEWAY_PUBLIC_URL = env_str("GATEWAY_PUBLIC_URL", "http://127.0.0.1:18180")  # lo usa la página /unirse
NOTIFIER_PUBLIC_URL = env_str("NOTIFIER_PUBLIC_URL", "")  # websocket del notifier; vacío = /ws del mismo host
REPO_URL = env_str("REPO_URL", "https://github.com/MateoPonti/TpIntegradorSDYPP")
MAX_BULK = 10_000
# Altas de cuentas por IP y por hora (B1.4, 26/11): una granja de identidades (Sybil) necesita muchas cuentas. 20 deja
# correr los tests de sistema (registran ~7 por corrida) y frena a un script que registra sin parar.
REGISTER_PER_IP_HOUR = env_int("REGISTER_PER_IP_HOUR", 20)

TX_ACCEPTED = Counter("tx_accepted_total", "Transacciones aceptadas")
TX_REJECTED = Counter("tx_rejected_total", "Transacciones rechazadas", ["reason"])
PENDING = Gauge("pending_transactions", "Tx que todavía no están en un bloque sellado")
# 21/11: la altura que guarda Redis (la misma de /status). El contador blocks_sealed_total vuelve a 0 cuando se
# reemplaza un pod, así que sumarlo daba una altura menor en Grafana; queda como métrica de actividad.
HEIGHT = Gauge("chain_height", "Altura de la cadena guardada en Redis (incluye el génesis)")
IS_LEADER = Gauge("coordinator_is_leader", "1 si esta réplica es la líder")
CF_SETTLE_ERRORS = Counter("crowdfund_settle_errors_total",
                           "Colectas que no se pudieron liquidar en una vuelta (sin saldo o error)")

state: dict = {}


async def leader_loop(chain: Chain, leader: store.Leader) -> None:
    while True:
        try:
            is_leader = await leader.check()
            IS_LEADER.set(1 if is_leader else 0)
            if is_leader:
                await chain.flush_outbox()  # lo que quedó sin publicar (se cayó RabbitMQ o un pod a mitad)
                await settle_crowdfunds()
                await retry_multisig()
                await chain.leader_tick()
            PENDING.set(await chain.pending_count())
            HEIGHT.set(await chain.r.llen(keys.CHAIN))
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
    state.update(r=r, conn=conn, chain=chain, reserve=r.register_script(ledger.RESERVE_LUA),
                 sign=r.register_script(multisig.SIGN_LUA), cf_start=r.register_script(crowdfund.START_LUA),
                 accept=r.register_script(multisig.ACCEPT_LUA), reject=r.register_script(multisig.REJECT_LUA),
                 retry=r.register_script(multisig.RETRY_LUA))
    log(logger, "coordinator listo", pod=POD_NAME)
    yield
    loop.cancel()
    await conn.close()
    await r.aclose()


app = FastAPI(title="TPI coordinator", lifespan=lifespan)
install_common_routes(app)


async def _crowdfund_closed(tx: dict) -> str | None:
    """Si la tx aporta a una colecta, que exista y siga abierta. Devuelve el motivo del rechazo."""
    cid = crowdfund.of_account(tx["receiver"])
    if cid is None:
        return None
    raw = await state["r"].hgetall(crowdfund.key(cid))
    if not raw:
        return "no existe esa colecta"
    return None if crowdfund.is_open(raw) else "la colecta ya cerró"


async def _receiver_exists(name: str) -> bool:
    """El destino tiene que existir (decidido el 20/11): cuenta del génesis, registrada con clave, compartida o una
    colecta (si está abierta lo controla _crowdfund_closed). Como el nombre se elige y la clave se asocia después,
    plata mandada a un nombre libre (un error de tipeo) se la quedaría el primero que lo registre."""
    if cuentas.is_genesis(name) or crowdfund.of_account(name) is not None:
        return True
    r = state["r"]
    if await r.exists(multisig.key(name)):
        return await _ms_status(name) == "activa"  # una compartida pendiente o rechazada todavía no recibe plata
    return bool(await r.exists(keys.account_pubkey(name)))


async def _claim_name(account: str, kind: str) -> bool:
    """Reserva el nombre en un registro único (21/11). La cuenta personal y la compartida se guardan en claves
    distintas, y antes cada alta solo miraba que no existiera la otra: dos altas simultáneas del mismo nombre, una de
    cada tipo, pasaban las dos. Devuelve True si el nombre quedó para este tipo (o ya era de este tipo)."""
    r = state["r"]
    return bool(await r.set(keys.account_name(account), kind, nx=True)) or await r.get(keys.account_name(account)) == kind


async def _ms_status(account: str) -> str:
    return await state["r"].get(multisig.status_key(account)) or "activa"


async def _submit(body) -> tuple[int, dict]:
    try:
        tx = ledger.normalize(body)
    except ledger.InvalidTx as exc:
        TX_REJECTED.labels("formato").inc()
        return 400, {"error": str(exc)}
    if not cuentas.genesis_transfer_ok(tx["sender"], tx["receiver"]):  # A1 (26/11): el génesis no firma
        TX_REJECTED.labels("genesis fuera del genesis").inc()
        return 400, {"error": "las cuentas del génesis solo transfieren a otras cuentas del génesis (user-0..96)"}
    if reason := await _crowdfund_closed(tx):
        TX_REJECTED.labels("colecta").inc()
        return 400, {"error": reason}
    if not await _receiver_exists(tx["receiver"]):
        TX_REJECTED.labels("destino inexistente").inc()
        return 400, {"error": f"la cuenta destino no existe: {tx['receiver']}"}
    seq = ""
    if not cuentas.is_genesis(tx["sender"]):
        # Cuenta propia: tiene que estar registrada y la tx firmada con su clave (opción A del 17/11).
        pubkey = await state["r"].get(keys.account_pubkey(tx["sender"]))
        if pubkey is None and await state["r"].exists(multisig.key(tx["sender"])):
            TX_REJECTED.labels("cuenta compartida").inc()
            return 400, {"error": "es una cuenta compartida: se gasta con una propuesta firmada por sus dueños"}
        if pubkey is None:
            TX_REJECTED.labels("no registrada").inc()
            return 400, {"error": "cuenta no registrada (registrarse con POST /accounts/register)"}
        seq = body.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            TX_REJECTED.labels("seq inválido").inc()
            return 400, {"error": "seq inválido"}
        msg = cuentas.tx_message(tx["sender"], tx["receiver"], tx["amount"], seq)
        if not cuentas.verify(pubkey, msg, body.get("signature")):
            TX_REJECTED.labels("firma inválida").inc()
            return 400, {"error": firmas.INVALID}
        tx.update(seq=seq, signature=body["signature"])  # quedan en el bloque: cualquiera puede re-verificar
    tx["fee"] = ledger.TX_FEE_CENTS  # no va en la firma: es fija y la pone el servidor
    total = tx["amount"] + tx["fee"]  # el disponible tiene que alcanzar para monto + comisión
    rc = await state["reserve"](keys=ledger.reserve_keys(tx), args=[tx["tx_id"], total, json.dumps(tx), str(seq)])
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
    results = []
    for index, item in enumerate(items):
        try:
            results.append(await _submit(item))
        except Exception:
            logger.exception("fallo al procesar tx del bulk", extra={"bulk_index": index})
            TX_REJECTED.labels("error interno").inc()
            results.append((400, {"error": "error interno con esta tx"}))
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
    pubkey = await r.get(keys.account_pubkey(user))
    registered = pubkey is not None
    shared = multisig.definition(await r.get(multisig.key(user)))
    return {"user": user, "balance_cents": bal, "pending_out_cents": out, "available_cents": bal - out,
            "registered": registered, "public_key": pubkey, "multisig": shared,
            "seq": int(await r.get(keys.account_seq(user)) or 0) if registered else None}


@app.post("/accounts/register", status_code=201)
async def register_account(request: Request):
    """Alta de una cuenta con su clave pública Ed25519. La firma sobre register:<cuenta>:<clave> prueba que
    quien registra tiene la clave privada. Una cuenta registrada no cambia de dueño (SET NX)."""
    body = await _json(request)
    if not isinstance(body, dict):
        raise HTTPException(400, "se esperaba un objeto JSON")
    account, pubkey = body.get("account"), body.get("public_key")
    if not isinstance(account, str) or not ledger.ACCOUNT_NAME.fullmatch(account):
        raise HTTPException(400, ledger.ACCOUNT_NAME_RULE)
    if cuentas.reserved(account):
        raise HTTPException(400, "nombre reservado (cuentas del génesis, premio o mineros)")
    if not isinstance(pubkey, str) or len(pubkey) != 64:
        raise HTTPException(400, "public_key: 32 bytes en hex")
    if not cuentas.verify(pubkey, cuentas.register_message(account, pubkey), body.get("signature")):
        raise HTTPException(400, firmas.INVALID)
    ip = ratelimit.client_ip(request)
    if not await ratelimit.allow(state["r"], f"ratelimit:register:{ip}", REGISTER_PER_IP_HOUR, 3600):
        log(logger, "alta de cuenta frenada por límite", ip=ip)
        raise HTTPException(429, f"demasiadas cuentas nuevas desde esta IP (máximo {REGISTER_PER_IP_HOUR} por hora)")
    if await state["r"].exists(multisig.key(account)):
        raise HTTPException(409, "ese nombre es una cuenta compartida")
    if await state["r"].exists(keys.balance(account)) or await state["r"].exists(keys.pending_out(account)):
        # Ya recibió plata sin tener dueño (de antes de exigir que el destino exista): registrarlo sería quedársela.
        raise HTTPException(409, "ese nombre ya tiene saldo sin dueño: elegí otro")
    if not await _claim_name(account, "personal"):
        raise HTTPException(409, "ese nombre ya es una cuenta compartida")
    if not await state["r"].set(keys.account_pubkey(account), pubkey.lower(), nx=True):
        raise HTTPException(409, "la cuenta ya está registrada")
    log(logger, "cuenta registrada", account=account)
    return {"account": account, "public_key": pubkey.lower()}


@app.get("/accounts")
async def accounts(limit: int = 1000):
    """Todas las cuentas con saldo o con plata reservada. `tipo` dice si es la cuenta de cobro de un minero
    (alguien la declaró como MINER_ACCOUNT, o es la de por defecto `miner:<id>`) o una cuenta de usuario."""
    r = state["r"]
    payout = {v async for k in r.scan_iter(match="miner:*:account", count=500) if (v := await r.get(k))}
    names = {k.split(":", 1)[1] async for k in r.scan_iter(match="balance:*", count=1000)}
    names |= {k.split(":", 1)[1] async for k in r.scan_iter(match="pending_out:*", count=1000)}
    out = []
    for name in names:
        bal = int(await r.get(keys.balance(name)) or 0)
        pend = int(await r.get(keys.pending_out(name)) or 0)
        if bal == 0 and pend == 0:
            continue
        kind = ("minero" if name in payout or name.startswith("miner:")
                else "colecta" if name.startswith(crowdfund.PREFIX)
                else "compartida" if await r.exists(multisig.key(name)) else "usuario")
        out.append({"user": name, "type": kind, "balance_cents": bal, "pending_out_cents": pend,
                    "available_cents": bal - pend})
    out.sort(key=lambda a: (a["type"] != "minero", -a["balance_cents"], a["user"]))
    return {"count": len(out), "accounts": out[:max(1, min(limit, 5000))]}


# ---- Cuentas compartidas (punto 2 del contrato): m de n firmas ----

async def _owner_key(name: str) -> str:
    pubkey = await state["r"].get(keys.account_pubkey(name))
    if pubkey is None:
        raise HTTPException(400, f"{name} no es una cuenta registrada")
    return pubkey


async def _admit(pid: str) -> None:
    """Se llegó al mínimo de firmas: el pago entra como una tx más (misma reserva de saldo y comisión que siempre).
    Si no alcanza el saldo, queda esperando saldo (como Safe, 21/11) y retry_multisig la vuelve a intentar."""
    r = state["r"]
    raw = await r.hgetall(multisig.proposal_key(pid))
    tx = {"tx_id": multisig.tx_id(pid), "sender": raw["account"], "receiver": raw["receiver"],
          "amount": int(raw["amount"]), "ts": round(time.time(), 3), "fee": int(raw["fee"]), **multisig.tx_extra(raw)}
    if reason := await _crowdfund_closed(tx):
        await r.hset(multisig.proposal_key(pid), mapping={"status": "rechazada", "reason": reason})
        await r.zrem(multisig.WAITING, pid)
        return
    rc = await state["reserve"](keys=ledger.reserve_keys(tx), args=[tx["tx_id"], tx["amount"] + tx["fee"], json.dumps(tx), ""])
    if rc == -2:  # saldo insuficiente: las firmas siguen valiendo hasta que vence
        await r.hset(multisig.proposal_key(pid), mapping={"status": "esperando_saldo", "reason": "esperando saldo"})
        await r.zadd(multisig.WAITING, {pid: float(raw["created"])})
        if raw.get("reason") != "esperando saldo":  # la primera vez (después se reintenta cada vuelta)
            log(logger, "propuesta esperando saldo", proposal=pid, account=tx["sender"])
        return
    await r.zrem(multisig.WAITING, pid)
    if rc == -1:  # ya había entrado (el líder se cayó antes de marcarla): no se paga dos veces
        await r.hset(multisig.proposal_key(pid), mapping={"status": "admitida", "tx_id": tx["tx_id"], "reason": ""})
        return
    if rc < 0:
        await r.hset(multisig.proposal_key(pid), mapping={"status": "rechazada", "reason": ledger.RESERVE_ERRORS[rc]})
        log(logger, "propuesta rechazada", proposal=pid, reason=ledger.RESERVE_ERRORS[rc])
        return
    await r.hset(multisig.proposal_key(pid), mapping={"status": "admitida", "tx_id": tx["tx_id"], "reason": ""})
    TX_ACCEPTED.inc()
    log(logger, "propuesta admitida", proposal=pid, account=tx["sender"], tx_id=tx["tx_id"])


async def retry_multisig() -> None:
    """Propuestas con las firmas juntas que esperan saldo (solo el líder, en cada vuelta): en orden de creación, la
    que ya alcanza se paga y la que pasó de hora queda vencida. También retoma las que quedaron en 'admitiendo' porque
    el coordinator que las admitía se cayó (pasados STUCK_SECONDS)."""
    r, now = state["r"], time.time()
    for pid in await r.zrange(multisig.WAITING, 0, -1):
        status = await state["retry"](keys=[multisig.proposal_key(pid)], args=[now, multisig.STUCK_SECONDS])
        if status == "admitir":
            await _admit(pid)
        elif status != "admitiendo":
            await r.zrem(multisig.WAITING, pid)  # vencida, o ya admitida o rechazada


async def _sign(pid: str, signer: str, signature: str, d: dict) -> dict:
    r = state["r"]
    raw = await r.hgetall(multisig.proposal_key(pid))
    if signer not in d["owners"]:
        raise HTTPException(400, f"{signer} no es dueño de {raw['account']}")
    if not cuentas.verify(await _owner_key(signer), raw["message"].encode(), signature):
        raise HTTPException(400, firmas.INVALID)
    if await state["sign"](keys=[multisig.proposal_key(pid), multisig.WAITING],
                           args=[signer, signature, d["threshold"], time.time()]) == "admitir":
        await _admit(pid)
    return multisig.view(await r.hgetall(multisig.proposal_key(pid)), d["threshold"])


@app.post("/multisig", status_code=201)
async def create_multisig(request: Request):
    """Crea una cuenta compartida. La firma uno de los dueños y queda pendiente hasta que acepten los demás."""
    body = await _json(request)
    if not isinstance(body, dict):
        raise HTTPException(400, "se esperaba un objeto JSON")
    account, creator = body.get("account"), body.get("creator")
    if not isinstance(account, str) or not ledger.ACCOUNT_NAME.fullmatch(account) or account == "recompensa":
        raise HTTPException(400, ledger.ACCOUNT_NAME_RULE)
    if cuentas.reserved(account):
        raise HTTPException(400, "nombre reservado (cuentas del génesis, premio o mineros)")
    try:
        owners, threshold = multisig.check_definition(account, body.get("owners"), body.get("threshold"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if creator not in owners:
        raise HTTPException(400, "el que la crea tiene que ser uno de los dueños")
    for owner in owners:
        await _owner_key(owner)
    if not cuentas.verify(await _owner_key(creator), multisig.create_message(account, threshold, owners), body.get("signature")):
        raise HTTPException(400, firmas.INVALID)
    r = state["r"]
    if await r.exists(keys.account_pubkey(account)) or await r.get(keys.balance(account)):
        raise HTTPException(409, "ese nombre ya es una cuenta")
    d = {"account": account, "owners": owners, "threshold": threshold, "creator": creator, "created": time.time()}
    if not await _claim_name(account, "compartida"):
        raise HTTPException(409, "ese nombre ya es una cuenta")
    if not await r.set(multisig.key(account), json.dumps(d), nx=True):
        raise HTTPException(409, "la cuenta compartida ya existe")
    await r.set(multisig.status_key(account), "pendiente")
    await r.sadd(multisig.accepted_key(account), creator)
    await r.sadd(multisig.owner_index(creator), account)
    for owner in owners:
        if owner != creator:
            await r.sadd(multisig.invites_index(owner), account)
    log(logger, "cuenta compartida creada (pendiente)", account=account, threshold=threshold, owners=len(owners))
    return d | await _ms_state(account, d)


async def _ms_state(account: str, d: dict) -> dict:
    accepted = sorted(await state["r"].smembers(multisig.accepted_key(account)))
    status = await _ms_status(account)
    if status == "activa" and not accepted:  # cuenta de antes del 20/11: todos cuentan como aceptados
        accepted = list(d["owners"])
    return {"status": status, "accepted": accepted, "waiting": [o for o in d["owners"] if o not in accepted]}


@app.post("/multisig/{account}/accept")
async def accept_multisig(account: str, request: Request):
    """Un dueño acepta firmando el mismo texto que el creador (esos dueños y ese mínimo)."""
    d, body = await _definition(account), await _json(request)
    owner = body.get("owner") if isinstance(body, dict) else None
    if owner not in d["owners"]:
        raise HTTPException(400, f"{owner} no es dueño de {account}")
    message = multisig.create_message(account, d["threshold"], d["owners"])
    if not cuentas.verify(await _owner_key(owner), message, body.get("signature")):
        raise HTTPException(400, firmas.INVALID)
    status = await state["accept"](keys=[multisig.status_key(account), multisig.accepted_key(account),
                                         multisig.invites_index(owner), multisig.owner_index(owner)],
                                   args=[owner, len(d["owners"]), account])
    if status == "rechazada":
        raise HTTPException(409, "la cuenta compartida fue rechazada por un dueño")
    log(logger, "dueño aceptó la cuenta compartida", account=account, owner=owner, status=status)
    return d | await _ms_state(account, d)


@app.post("/multisig/{account}/reject")
async def reject_multisig(account: str, request: Request):
    """Un dueño que no aceptó la rechaza (firma multisig:reject:<cuenta>): la cuenta no se activa nunca."""
    d, body = await _definition(account), await _json(request)
    owner = body.get("owner") if isinstance(body, dict) else None
    if owner not in d["owners"]:
        raise HTTPException(400, f"{owner} no es dueño de {account}")
    if not cuentas.verify(await _owner_key(owner), multisig.reject_message(account), body.get("signature")):
        raise HTTPException(400, firmas.INVALID)
    if await _ms_status(account) != "pendiente":
        raise HTTPException(409, "solo se puede rechazar mientras está pendiente")
    status = await state["reject"](
        keys=[multisig.status_key(account), *(multisig.invites_index(o) for o in d["owners"])],
        args=[account])
    if status != "rechazada":
        raise HTTPException(409, "solo se puede rechazar mientras está pendiente")
    log(logger, "cuenta compartida rechazada", account=account, owner=owner)
    return d | await _ms_state(account, d)


async def _definition(account: str) -> dict:
    d = multisig.definition(await state["r"].get(multisig.key(account)))
    if d is None:
        raise HTTPException(404, "no es una cuenta compartida")
    return d


@app.post("/multisig/{account}/proposals", status_code=201)
async def propose(account: str, request: Request):
    """Un dueño propone un pago y lo firma: su firma es la primera (el mínimo es 2 o más)."""
    d = await _definition(account)
    if (status := await _ms_status(account)) != "activa":
        raise HTTPException(409, f"la cuenta compartida está {status}: tienen que aceptar todos los dueños")
    body = await _json(request)
    if not isinstance(body, dict):
        raise HTTPException(400, "se esperaba un objeto JSON")
    try:
        tx = ledger.normalize({"sender": account, "receiver": body.get("receiver"), "amount": body.get("amount")})
    except ledger.InvalidTx as exc:
        raise HTTPException(400, str(exc)) from exc
    nonce = body.get("nonce")
    if not isinstance(nonce, str) or not 1 <= len(nonce) <= 64:
        raise HTTPException(400, "nonce inválido")
    if not await _receiver_exists(tx["receiver"]):
        raise HTTPException(400, f"la cuenta destino no existe: {tx['receiver']}")
    message = multisig.payment_message(account, tx["receiver"], tx["amount"], nonce)
    pid, r = multisig.proposal_id(message), state["r"]
    if await r.exists(multisig.proposal_key(pid)):
        raise HTTPException(409, "esa propuesta ya existe")
    signer = body.get("signer")
    if signer not in d["owners"] or not cuentas.verify(await _owner_key(signer), message.encode(), body.get("signature")):
        raise HTTPException(400, "firma inválida (tiene que firmar un dueño)")
    now = time.time()
    if not await r.hsetnx(multisig.proposal_key(pid), "id", pid):
        raise HTTPException(409, "esa propuesta ya existe")
    await r.hset(multisig.proposal_key(pid),
                 mapping=multisig.new_proposal(pid, account, tx["receiver"], tx["amount"], ledger.TX_FEE_CENTS, message, now))
    await r.zadd(multisig.proposals_index(account), {pid: now})
    log(logger, "propuesta creada", proposal=pid, account=account, signer=signer)
    return await _sign(pid, signer, body["signature"], d)


@app.post("/multisig/proposals/{pid}/sign")
async def sign_proposal(pid: str, request: Request):
    """Otro dueño firma la propuesta. Firmar dos veces no suma (una firma por dueño)."""
    body = await _json(request)
    raw = await state["r"].hgetall(multisig.proposal_key(pid))
    if not raw or not isinstance(body, dict):
        raise HTTPException(404, "no existe esa propuesta")
    return await _sign(pid, body.get("signer"), body.get("signature"), await _definition(raw["account"]))


@app.get("/multisig/proposals/{pid}")
async def get_proposal(pid: str):
    raw = await state["r"].hgetall(multisig.proposal_key(pid))
    if not raw:
        raise HTTPException(404, "no existe esa propuesta")
    return multisig.view(raw, (await _definition(raw["account"]))["threshold"])


@app.get("/multisig/{account}")
async def get_multisig(account: str, limit: int = 50):
    """La cuenta compartida: dueños, mínimo, saldo y sus últimas propuestas."""
    d, r = await _definition(account), state["r"]
    pids = await r.zrevrange(multisig.proposals_index(account), 0, max(1, min(limit, 200)) - 1)
    proposals = [multisig.view(raw, d["threshold"]) for pid in pids if (raw := await r.hgetall(multisig.proposal_key(pid)))]
    bal = int(await r.get(keys.balance(account)) or 0)
    out = int(await r.get(keys.pending_out(account)) or 0)
    return d | await _ms_state(account, d) | {"balance_cents": bal, "pending_out_cents": out,
                                              "available_cents": bal - out, "proposals": proposals}


@app.get("/multisig")
async def list_multisig(owner: str):
    """Las cuentas compartidas de las que `owner` es dueño (aceptadas) y en las que lo invitaron (sin responder)."""
    r = state["r"]
    return {"owner": owner, "accounts": sorted(await r.smembers(multisig.owner_index(owner))),
            "invites": sorted(await r.smembers(multisig.invites_index(owner)))}


# ---- Colectas "todo o nada" (punto 2 del contrato) ----

async def settle_crowdfunds() -> None:
    """Liquida las colectas cerradas (solo el líder, en cada vuelta del loop). Ver coordinator/crowdfund.py.

    C6 (27/11): cada colecta se liquida aislada de las demás. Antes, una colecta sin saldo hacía
    `return` y cortaba el recorrido entero, así que las que venían después no se liquidaban nunca
    (y una excepción en una, lo mismo). Ahora una que falla se saltea y se vuelve a intentar en la
    vuelta siguiente: queda en `liquidando`, que es justamente el estado para retomarla."""
    r, now = state["r"], time.time()
    for cid in await r.zrangebyscore(crowdfund.OPEN_INDEX, 0, now - crowdfund.GRACE):
        try:
            await _settle_one(cid, now)
        except Exception:  # noqa: BLE001 - Redis, un Lua o un dato raro de UNA colecta
            CF_SETTLE_ERRORS.inc()
            logger.exception("error liquidando la colecta %s: sigo con las demás", cid)


async def _settle_one(cid: str, now: float) -> None:
    """Liquida una sola colecta. Si no se puede todavía (aportes sin confirmar, sin saldo) vuelve sin
    tocarla: la próxima vuelta la retoma."""
    r = state["r"]
    raw = await r.hgetall(crowdfund.key(cid))
    if raw.get("status") not in ("abierta", "liquidando"):
        await r.zrem(crowdfund.OPEN_INDEX, cid)
        return
    txs = list(await r.smembers(crowdfund.txs_key(cid)))
    if txs and "pending" in await r.hmget(keys.TX_STATUS, txs):
        return  # falta que algún aporte entre en un bloque: la plata todavía no está en la colecta
    contrib = await r.hgetall(crowdfund.contrib_key(cid))
    if not await state["cf_start"](keys=[crowdfund.key(cid)], args=[now, crowdfund.outcome(raw, contrib)]):
        return
    result = await r.hget(crowdfund.key(cid), "result")  # si se retoma una liquidación, el que quedó guardado
    pays = crowdfund.payments(result, raw, contrib)
    for receiver, amount in pays:
        tx = crowdfund.settlement_tx(cid, receiver, amount, result, now)
        rc = await state["reserve"](keys=ledger.reserve_keys(tx), args=[tx["tx_id"], amount, json.dumps(tx), ""])
        if rc == -2:
            # Sin saldo: la colecta queda en `liquidando` y se retoma. Los pagos ya reservados no se
            # repiten, porque el tx_id de cada uno es fijo (colecta-<cid>-<receiver>).
            CF_SETTLE_ERRORS.inc()
            log(logger, "colecta sin saldo para liquidar", crowdfund=cid, receiver=receiver)
            return
    await r.hset(crowdfund.key(cid), mapping={"status": result, "settled": now})
    await r.zrem(crowdfund.OPEN_INDEX, cid)
    log(logger, "colecta liquidada", crowdfund=cid, result=result, txs=len(pays))


@app.post("/crowdfunds", status_code=201)
async def create_crowdfund(request: Request):
    """Crea una colecta. La firma el que la crea, con su clave (cuenta registrada)."""
    body = await _json(request)
    if not isinstance(body, dict):
        raise HTTPException(400, "se esperaba un objeto JSON")
    cid, creator, beneficiary = body.get("id"), body.get("creator"), body.get("beneficiary")
    if not isinstance(cid, str) or not crowdfund.ID.fullmatch(cid):
        raise HTTPException(400, "id: 3 a 32 minúsculas, números o -")
    title = body.get("title") or ""
    if not isinstance(title, str) or len(title) > crowdfund.MAX_TITLE:
        raise HTTPException(400, f"title: hasta {crowdfund.MAX_TITLE} caracteres")
    try:
        goal = ledger.to_cents(body.get("goal"))
        deadline = crowdfund.check_deadline(body.get("deadline"), time.time())
    except (ledger.InvalidTx, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    r = state["r"]
    if not isinstance(beneficiary, str) or crowdfund.of_account(beneficiary) is not None \
            or cuentas.is_genesis(beneficiary) or not await _receiver_exists(beneficiary):
        raise HTTPException(400, "beneficiary: tiene que ser una cuenta registrada o compartida (activa)")
    if not cuentas.verify(await _owner_key(creator if isinstance(creator, str) else ""),
                          crowdfund.create_message(cid, beneficiary, goal, deadline), body.get("signature")):
        raise HTTPException(400, firmas.INVALID)
    now = time.time()
    if not await r.hsetnx(crowdfund.key(cid), "id", cid):
        raise HTTPException(409, "ya existe una colecta con ese id")
    await r.hset(crowdfund.key(cid), mapping={"title": title, "creator": creator, "beneficiary": beneficiary,
                                              "goal": goal, "deadline": deadline, "created": now,
                                              "status": "abierta", "raised": 0})
    await r.zadd(crowdfund.INDEX, {cid: now})
    await r.zadd(crowdfund.OPEN_INDEX, {cid: deadline})
    log(logger, "colecta creada", crowdfund=cid, creator=creator, goal=goal, deadline=deadline)
    return crowdfund.view(await r.hgetall(crowdfund.key(cid)))


@app.get("/crowdfunds")
async def list_crowdfunds(limit: int = 50):
    """Las últimas colectas, abiertas y cerradas."""
    r = state["r"]
    ids = await r.zrevrange(crowdfund.INDEX, 0, max(1, min(limit, 200)) - 1)
    out = []
    for cid in ids:
        if raw := await r.hgetall(crowdfund.key(cid)):
            out.append(await _with_settlement(cid, raw, crowdfund.view(raw)))
    return {"crowdfunds": out, "fee_cents": ledger.TX_FEE_CENTS}


@app.get("/crowdfunds/{cid}")
async def get_crowdfund(cid: str):
    r = state["r"]
    raw = await r.hgetall(crowdfund.key(cid))
    if not raw:
        raise HTTPException(404, "no existe esa colecta")
    return await _with_settlement(cid, raw, crowdfund.view(raw, await r.hgetall(crowdfund.contrib_key(cid))))


async def _with_settlement(cid: str, raw: dict, view: dict) -> dict:
    """Si ya se liquidó, si las tx del pago o de las devoluciones están en un bloque (21/11). El estado pasa a
    exitosa/fallida al reservar esas tx, y antes la página decía "devuelta" mientras la plata todavía no se había
    acreditado (bloque 604: casi 12 h sin mineros)."""
    if view["status"] not in ("exitosa", "fallida"):
        return view
    r = state["r"]
    pays = crowdfund.payments(view["status"], raw, await r.hgetall(crowdfund.contrib_key(cid)))
    ids = [crowdfund.settlement_tx(cid, receiver, amount, view["status"], 0)["tx_id"] for receiver, amount in pays]
    where = await r.hmget(keys.TX_STATUS, ids) if ids else []
    blocks = [int(w) for w in where if w not in (None, "pending")]
    view["settlement"] = {"tx_ids": ids, "confirmed": len(blocks) == len(ids),
                          "block": max(blocks) if blocks and len(blocks) == len(ids) else None}
    return view


SUMMARY_FIELDS = ("index", "timestamp", "block_hash", "previous_hash", "prefix", "miner", "nonce", "reward_cents")


@app.get("/blocks")
async def block_summaries(start: int = 0, limit: int = 20):
    """Encabezados de bloques [start, start+limit) con la cantidad de tx y de premios, sin las tx (21/11): para la
    tabla del explorador alcanza con esto, y antes pedía bloques enteros (6.490 tx para 20 filas)."""
    r = state["r"]
    idx = range(max(0, start), max(0, start) + max(1, min(limit, 100)))
    out = []
    for raw in await r.mget([keys.block(i) for i in idx]):
        if raw is None:
            continue
        b = json.loads(raw)
        rewards = sum(t["sender"] == REWARD_SENDER for t in b["transactions"])
        out.append({k: b.get(k) for k in SUMMARY_FIELDS} | {"tx_count": len(b["transactions"]) - rewards,
                                                             "reward_count": rewards})
    return {"height": await r.llen(keys.CHAIN), "blocks": out}


@app.get("/blocks/{index}/verify")
async def verify_block(index: int):
    """Recalcula el bloque (21/11, para el explorador: antes mostraba la fórmula como texto fijo). Mismo criterio
    que tools/verify_chain.py, que hace lo mismo desde afuera y sirve para verificarlo sin confiar en el servidor."""
    r = state["r"]
    raw = await r.get(keys.block(index))
    if raw is None:
        raise HTTPException(404, "no existe")
    b = json.loads(raw)
    if index == 0:
        return {"index": 0, "ok": True, "checks": {"genesis": True}}
    prev = await r.get(keys.block(index - 1))
    digest = pow.tx_digest(b["transactions"])
    checks = {
        "tx_digest": digest == b["tx_digest"],
        "md5": pow.md5_hex(b["previous_hash"] + digest, int(b["nonce"])) == b["block_hash"],
        "prefijo": b["block_hash"].startswith(b["prefix"]),
        "enlace": prev is not None and json.loads(prev)["block_hash"] == b["previous_hash"],
    }
    return {"index": index, "ok": all(checks.values()), "checks": checks, "tx_digest": digest}


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


@app.get("/chain-id")
async def chain_id():
    """Id de la cadena (A3-g): el front y tools/cuenta.py lo leen de acá para armar lo que firman."""
    return {"chain_id": CHAIN_ID}


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
    gateway_ok, notifier_ok = await asyncio.gather(asyncio.to_thread(_http_ok, GATEWAY_HEALTH_URL),
                                                   asyncio.to_thread(_http_ok, NOTIFIER_HEALTH_URL))
    services["gateway"] = "ok" if gateway_ok else "down"
    services["notifier"] = "ok" if notifier_ok else "down"
    if services["redis"] != "ok":
        # Sin Redis no hay cadena ni señal del pool que leer (21/11): antes la lectura siguiente tiraba excepción y
        # /status devolvía 500 justo cuando más hace falta. Se informa lo que sí responde y lo demás queda en null.
        services["pool"] = "desconocido (sin Redis)"
        return {"services": services, "chain_id": CHAIN_ID, "pending_transactions": None, "chain_height": None, "chain_tip": None,
                "mining": None, "miners": None, "pool_plan": None, "pool_signal": None, "leader": None, "economy": None}
    signal = await r.get(keys.POOL_SCALING)
    services["pool"] = "ok" if signal else "sin señal"
    workers = await store.alive_workers(r)
    cur = await chain.current()
    return {
        "services": services,
        "chain_id": CHAIN_ID,
        "pending_transactions": await chain.pending_count(),
        "chain_height": await r.llen(keys.CHAIN),
        "chain_tip": await r.get(keys.CHAIN_TIP),
        "mining": None if cur is None else {k: cur[k] for k in ("block_id", "index", "prefix", "range_size")}
        | {"seconds": round(time.time() - cur.get("assembled", cur["created"]), 1), "rearmed": cur.get("rearmed", 0)},
        "miners": {"gpu": sum(w.get("type") == "gpu" for w in workers),
                   "cpu": sum(w.get("type") == "cpu" for w in workers)},
        "pool_plan": json.loads(plan) if (plan := await r.get(keys.POOL_PLAN)) else None,
        "pool_signal": json.loads(signal) if signal else None,
        "leader": await r.get(keys.COORD_LEADER),
        # Modelo de Bitcoin (20/11): subsidio con halving + comisión fija que cobran los mineros
        "economy": {"base_reward_cents": BLOCK_REWARD_CENTS, "halving_blocks": HALVING_BLOCKS,
                    "tx_fee_cents": ledger.TX_FEE_CENTS,
                    "next_subsidy_cents": block_subsidy(await r.llen(keys.CHAIN), BLOCK_REWARD_CENTS, HALVING_BLOCKS)},
    }


@app.get("/miners")
async def miners():
    """Mineros vivos con su hashrate (calibrado, del motor, efectivo y declarado) y su clase."""
    raw = await state["r"].get(keys.POOL_MINERS)
    return json.loads(raw) if raw else []


@app.get("/", include_in_schema=False)
async def frontend():
    """Explorador de la cadena: lee la misma API pública (no habla con Redis)."""
    return FileResponse(Path(__file__).with_name("static") / "index.html")


@app.get("/cuenta", include_in_schema=False)
async def cuenta():
    """Billetera: crear una cuenta (mine o no), ver el saldo en vivo y mandar transferencias firmadas."""
    return FileResponse(Path(__file__).with_name("static") / "cuenta.html")


@app.get("/unirse", include_in_schema=False)
async def unirse():
    """Portal "¿Querés minar con nosotros?": crear cuenta, dar de alta un worker y copiar el comando."""
    return FileResponse(Path(__file__).with_name("static") / "unirse.html")


@app.get("/unirse/config")
async def unirse_config():
    """Direcciones públicas que usan las dos páginas (el gateway para /unirse, el notifier para el explorador)."""
    return {"gateway": GATEWAY_PUBLIC_URL, "repo": REPO_URL, "notifier": NOTIFIER_PUBLIC_URL}


ASSETS = {"sdmer.css": "text/css", "nacl-fast.min.js": "text/javascript", "claves.js": "text/javascript"}


@app.get("/assets/{name}", include_in_schema=False)
async def assets(name: str):
    """Estilo común de las páginas, claves.js (firmas de /cuenta y /unirse) y TweetNaCl (Ed25519 en el navegador: crypto.subtle no existe sin HTTPS)."""
    if name not in ASSETS:
        raise HTTPException(404, "no existe")
    return FileResponse(Path(__file__).with_name("static") / "assets" / name, media_type=ASSETS[name],
                        headers={"Cache-Control": "public, max-age=300"})
