"""Notifier (punto 3 del contrato): avisa por WebSocket cuando se sella un bloque, se confirma una tx o se mueve
una cuenta, para que el navegador no tenga que preguntar cada pocos segundos.

No decide nada: escucha block.solved en RabbitMQ (una cola por réplica, block.events.<pod>, así cada réplica
recibe todos los eventos y avisa a los navegadores conectados a ella) y lee Redis solo para la foto del estado.

Protocolo JSON-RPC 2.0 (https://www.jsonrpc.org/specification):
  cliente -> {"jsonrpc":"2.0","id":1,"method":"sdmer_subscribe","params":["newBlocks"]}
             params también puede ser ["tx", "id"] o ["account", "user-3"].
  servidor -> {"jsonrpc":"2.0","id":1,"result":"0x0123456789abcdef"}
              {"jsonrpc":"2.0","method":"sdmer_subscription",
               "params":{"subscription":"0x...","result":{"type":"snapshot","height":21}}}
  Baja: sdmer_unsubscribe, params ["0x..."] -> result true|false.
  Cada result de un aviso es una foto o un evento block.solved / tx.confirmed / account.changed.
Reconexión: primero se suscribe y después llega la foto (así no se pierde nada entre medio). Un evento con
index < height de la foto ya está incluido en ella. WebSocket no guarda los avisos perdidos mientras el cliente
estuvo desconectado: lo que se garantiza es recuperar el estado vigente.
"""
import asyncio
import collections
import contextlib
import json
import math
import secrets
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from prometheus_client import Counter, Gauge

from tpi_common import keys, rabbit, store
from tpi_common.config import POD_NAME, env_int, env_str
from tpi_common.logs import log, setup_logging
from tpi_common.web import install_common_routes

logger = setup_logging("notifier")
MAX_SUBS = env_int("NOTIFIER_MAX_SUBSCRIPTIONS", 50)        # por conexión
MAX_CONNECTIONS = env_int("NOTIFIER_MAX_CONNECTIONS", 1000)  # por réplica
MAX_MESSAGE = env_int("NOTIFIER_MAX_MESSAGE_BYTES", 4096)
QUEUE_SIZE = env_int("NOTIFIER_CLIENT_QUEUE", 200)           # avisos sin enviar antes de cortar a un cliente lento
SESSION_SECONDS = env_int("NOTIFIER_SESSION_SECONDS", 3600)  # después se corta y el cliente se reconecta
# Páginas que pueden abrir el websocket además de la del mismo host (ej. el explorador local en otro puerto).
ALLOWED_ORIGINS = {o.strip() for o in env_str("NOTIFIER_ALLOWED_ORIGINS", "").split(",") if o.strip()}
MAX_KEY = 128

CONNECTIONS = Gauge("notifier_connections", "Websockets abiertos en esta réplica")
SUBSCRIPTIONS = Gauge("notifier_subscriptions", "Suscripciones activas en esta réplica")
EVENTS = Counter("notifier_events_total", "Eventos recibidos de RabbitMQ", ["status"])
SENT = Counter("notifier_messages_total", "Avisos enviados a los navegadores", ["event"])
REJECTED = Counter("notifier_rejected_total", "Conexiones o pedidos rechazados", ["reason"])

CLOSE = object()  # marca en la cola de un cliente: cortar la conexión
clients: set["Client"] = set()
state: dict = {}
_seen: collections.deque = collections.deque(maxlen=1000)  # event_id ya avisados (el outbox puede repetir)


class Client:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.subs: dict[str, tuple[str, str | None]] = {}
        self.queue: asyncio.Queue = asyncio.Queue(QUEUE_SIZE)

    def push(self, msg) -> bool:
        """Encola un aviso. Si el cliente no da abasto se vacía su cola y se lo corta: al reconectarse recibe
        la foto del estado, en vez de acumular avisos sin límite en memoria."""
        try:
            self.queue.put_nowait(msg)
            return True
        except asyncio.QueueFull:
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(CLOSE)
            REJECTED.labels("cliente_lento").inc()
            return False


def origin_ok(origin: str | None, host: str | None, allowed: set[str]) -> bool:
    """Un navegador siempre manda Origin: solo se aceptan la misma página o las de la lista. Sin Origin es un
    cliente que no es un navegador (script, test), y no le sirve para robarle nada a nadie."""
    if not origin:
        return True
    return origin in allowed or urlparse(origin).netloc == (host or "")


def parse_subscription(params) -> tuple[str, str | None]:
    if not isinstance(params, list) or not params or not isinstance(params[0], str):
        raise ValueError('params: ["newBlocks"], ["tx", id] o ["account", cuenta]')
    if params == ["newBlocks"]:
        return "blocks", None
    if len(params) != 2 or params[0] not in ("tx", "account"):
        raise ValueError('params: ["newBlocks"], ["tx", id] o ["account", cuenta]')
    topic, key = params
    if not isinstance(key, str) or not key or len(key) > MAX_KEY:
        raise ValueError("identificador vacío o demasiado largo")
    return topic, key


def index_event(ev: dict) -> tuple[dict, dict]:
    """tx_id -> tx y cuenta -> movimientos del bloque. Se calcula una vez por evento, no por cliente."""
    by_tx, by_account = {}, collections.defaultdict(list)
    for t in ev.get("txs", []):
        by_tx[t["tx_id"]] = t
        by_account[t["sender"]].append({"tx_id": t["tx_id"], "dir": "out", "other": t["receiver"],
                                        "amount": t["amount"]})
        if t["receiver"] != t["sender"]:
            by_account[t["receiver"]].append({"tx_id": t["tx_id"], "dir": "in", "other": t["sender"],
                                              "amount": t["amount"]})
    return by_tx, by_account


def route(ev: dict, index: tuple[dict, dict], subs: dict) -> list[dict]:
    """Qué avisos le tocan a un cliente por un block.solved, según sus suscripciones."""
    by_tx, by_account = index
    base = {"type": "event", "index": ev["index"], "block_hash": ev["block_hash"]}
    out = []
    for sid, (topic, key) in subs.items():
        if topic == "blocks":
            out.append(base | {"sub": sid, "event": "block.solved", "timestamp": ev.get("timestamp"),
                               "miner": ev.get("miner"), "txs": len(by_tx)})
        elif topic == "tx" and key in by_tx:
            out.append(base | {"sub": sid, "event": "tx.confirmed", "tx_id": key})
        elif topic == "account" and key in by_account:
            out.append(base | {"sub": sid, "event": "account.changed", "account": key,
                               "movements": by_account[key]})
    return [subscription_notice(msg.pop("sub"), msg) for msg in out]


async def balance(r, account: str) -> dict:
    bal = int(await r.get(keys.balance(account)) or 0)
    out = int(await r.get(keys.pending_out(account)) or 0)
    return {"balance_cents": bal, "pending_out_cents": out, "available_cents": bal - out}


async def snapshot(r, topic: str, key: str | None) -> dict:
    """Foto del estado vigente. `height` es la versión: los eventos con index < height ya están incluidos."""
    snap = {"height": await r.llen(keys.CHAIN)}
    if topic == "blocks":
        snap["tip"] = await r.get(keys.CHAIN_TIP)
    elif topic == "tx":
        where = await r.hget(keys.TX_STATUS, key)
        snap |= {"tx_id": key, "status": "unknown" if where is None else "pending" if where == "pending"
                 else "confirmed", "block": None if where in (None, "pending") else int(where)}
    else:
        snap |= {"account": key} | await balance(r, key)
    return snap


async def on_event(message) -> None:
    async with message.process():
        try:
            ev = json.loads(message.body)
        except ValueError:
            EVENTS.labels("malformed").inc()
            return
        event_id = ev.get("event_id") or f"block-{ev.get('index')}"
        if ev.get("type") != "block.solved" or event_id in _seen:
            EVENTS.labels("duplicate").inc()
            return
        _seen.append(event_id)
        EVENTS.labels("new").inc()
        index, balances = index_event(ev), {}
        for client in list(clients):
            for msg in route(ev, index, client.subs):
                result = msg["params"]["result"]
                if result["event"] == "account.changed":
                    account = result["account"]
                    if account not in balances:
                        balances[account] = await balance(state["r"], account)
                    result.update(balances[account])
                if not client.push(msg):
                    break
                SENT.labels(result["event"]).inc()


def rpc_error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def subscription_notice(sid: str, result: dict) -> dict:
    return {"jsonrpc": "2.0", "method": "sdmer_subscription",
            "params": {"subscription": sid, "result": result}}


async def rpc_request(client: Client, msg) -> tuple[dict | None, list[dict]]:
    if not isinstance(msg, dict):
        return rpc_error(None, -32600, "pedido inválido"), []
    req_id = msg.get("id")
    if (msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str)
            or isinstance(req_id, bool) or not isinstance(req_id, (str, int, float, type(None)))
            or (isinstance(req_id, float) and not math.isfinite(req_id))):
        return rpc_error(None, -32600, "pedido inválido"), []
    notification = "id" not in msg

    def error(code, message):
        return (None if notification else rpc_error(req_id, code, message)), []

    method, params = msg["method"], msg.get("params")
    if method == "sdmer_subscribe":
        try:
            topic, key = parse_subscription(params)
        except ValueError as exc:
            return error(-32602, str(exc))
        if len(client.subs) >= MAX_SUBS:
            REJECTED.labels("demasiadas_suscripciones").inc()
            return error(-32000, f"máximo {MAX_SUBS} suscripciones")
        sid = "0x" + secrets.token_hex(8)
        while sid in client.subs:
            sid = "0x" + secrets.token_hex(8)
        client.subs[sid] = (topic, key)  # suscribir antes de tomar la foto: no se pierden eventos
        SUBSCRIPTIONS.inc()
        try:
            snap = await snapshot(state["r"], topic, key)
        except Exception:
            client.subs.pop(sid, None)
            SUBSCRIPTIONS.dec()
            logger.exception("falló la foto de la suscripción")
            return error(-32603, "no se pudo obtener el estado")
        response = None if notification else {"jsonrpc": "2.0", "id": req_id, "result": sid}
        return response, [subscription_notice(sid, {"type": "snapshot"} | snap)]
    if method == "sdmer_unsubscribe":
        if not isinstance(params, list) or len(params) != 1 or not isinstance(params[0], str):
            return error(-32602, "params: [identificador de suscripción]")
        removed = client.subs.pop(params[0], None) is not None
        if removed:
            SUBSCRIPTIONS.dec()
        return (None if notification else {"jsonrpc": "2.0", "id": req_id, "result": removed}), []
    return error(-32601, "método inexistente")


def invalid_constant(value):
    raise ValueError(f"constante JSON inválida: {value}")


async def handle(client: Client, text: str) -> None:
    try:
        msg = json.loads(text, parse_constant=invalid_constant)
    except (ValueError, RecursionError):
        client.push(rpc_error(None, -32700, "JSON inválido"))
        return
    batch = isinstance(msg, list)
    if batch and not msg:
        client.push(rpc_error(None, -32600, "pedido inválido"))
        return
    responses, notices = [], []
    for item in msg if batch else [msg]:
        response, pending = await rpc_request(client, item)
        if response is not None:
            responses.append(response)
        notices.extend(pending)
    if responses and not client.push(responses if batch else responses[0]):
        return
    for notice in notices:
        if notice["params"]["subscription"] in client.subs and not client.push(notice):
            break


async def send_loop(client: Client) -> None:
    while True:
        msg = await client.queue.get()
        if msg is CLOSE:
            await client.ws.close(code=1013, reason="cliente lento, reconectar")
            return
        await client.ws.send_text(json.dumps(msg))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    r = await store.connect(logger)
    conn = await rabbit.connect(logger)
    channel = await conn.channel()
    exchange, *_ = await rabbit.declare_topology(channel)
    events = await rabbit.declare_events_queue(channel, exchange, POD_NAME)
    await events.consume(on_event)
    state.update(r=r, conn=conn)
    log(logger, "notifier listo", pod=POD_NAME)
    yield
    await conn.close()
    await r.aclose()


app = FastAPI(title="TPI notifier", lifespan=lifespan)
install_common_routes(app)


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    if not origin_ok(ws.headers.get("origin"), ws.headers.get("host"), ALLOWED_ORIGINS):
        REJECTED.labels("origen").inc()
        await ws.close(code=1008)
        return
    if len(clients) >= MAX_CONNECTIONS:
        REJECTED.labels("lleno").inc()
        await ws.close(code=1013)
        return
    await ws.accept()
    client = Client(ws)
    clients.add(client)
    CONNECTIONS.inc()
    sender = asyncio.create_task(send_loop(client))
    try:
        async with asyncio.timeout(SESSION_SECONDS):
            while not sender.done():
                text = await ws.receive_text()
                if len(text.encode()) > MAX_MESSAGE:
                    REJECTED.labels("mensaje_grande").inc()
                    await ws.close(code=1009)
                    break
                await handle(client, text)
    except TimeoutError:
        with contextlib.suppress(RuntimeError):
            await ws.close(code=1000, reason="sesión vencida, reconectar")
    except (WebSocketDisconnect, RuntimeError):
        pass  # se fue el cliente (o lo cortamos por lento)
    finally:
        clients.discard(client)
        CONNECTIONS.dec()
        SUBSCRIPTIONS.dec(len(client.subs))
        sender.cancel()
