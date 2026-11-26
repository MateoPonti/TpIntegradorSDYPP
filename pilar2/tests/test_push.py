"""El gateway empuja el trabajo y los workers se autorizan con un desafío firmado (A3-c y A3-f, Task 14).

* `mining.challenge` → `mining.authorize` firmado con Ed25519 sobre sdmer:<chain>:authorize:<worker>:<desafío>.
  La clave queda atada al worker la primera vez (TOFU, como known_hosts de SSH); después tiene que ser la misma.
* `mining.notify`: el gateway le manda la tarea al worker apenas tiene lugar (una a la vez por conexión).
* Resultados firmados: POST /results exige la firma de la clave atada.

Los que usan Redis necesitan REDIS_URL (ver test_lua.py).
"""
import asyncio
import json
import os

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException

import agent
from conftest import import_gateway
from tpi_common import firmas, keys

SEED = bytes(range(32))


# ---- Ed25519 del agente (solo stdlib, RFC 8032) ----

def test_ed25519_del_agente_igual_a_cryptography():
    ref = Ed25519PrivateKey.from_private_bytes(SEED)
    for msg in (b"", b"hola", b"sdmer:2026:authorize:cpu-1:" + b"ab" * 16, os.urandom(300)):
        assert agent.ed25519_sign(SEED, msg) == ref.sign(msg)
    assert agent.ed25519_public(SEED) == ref.public_key().public_bytes_raw()


def test_result_message_formato():
    assert firmas.result_message("t1", True, 1234, [5, 3]) == \
        b"result:t1:1:1234:" + firmas.shares_digest([5, 3]).encode()
    assert firmas.result_message("t1", False, None, []) == b"result:t1:0::" + firmas.shares_digest([]).encode()
    assert firmas.shares_digest([5, 3]) == firmas.shares_digest([3, 5])  # el orden no importa


def test_agente_firma_resultados_que_verifica_el_gateway():
    pub = agent.ed25519_public(SEED).hex()
    res = {"task_id": "t1", "found": False, "shares": [7, 9]}
    sig = agent.sign_result(SEED, 2026, res)
    msg = firmas.result_message("t1", False, None, [7, 9])
    assert firmas.verify(pub, msg, sig)


# ---- Frames del WebSocket del agente (ahora también manda) ----

class Pipe:
    def __init__(self):
        self.buf = b""

    def sendall(self, data):
        self.buf += data

    def makefile(self):
        import io
        return io.BytesIO(self.buf)


@pytest.mark.parametrize("size", [5, 125, 126, 300, 70000])
def test_frame_de_cualquier_largo_va_y_vuelve(size):
    data = os.urandom(size)
    pipe = Pipe()
    agent.Notices.send(pipe, 1, data)
    op, back = agent.Notices.frame(pipe.makefile())
    assert op == 1 and back == data


# ---- Gateway: desafío, authorize y push ----

class FakeExchange:
    def __init__(self):
        self.published = []

    async def publish(self, message, routing_key):
        self.published.append((json.loads(message.body), routing_key))


class FakeMsg:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    async def ack(self):
        pass


class FakeQueue:
    def __init__(self, tasks):
        self.tasks = list(tasks)

    async def get(self, no_ack=False, fail=False):
        return FakeMsg(self.tasks.pop(0)) if self.tasks else None


class FakeWS:
    """Lo mínimo de starlette.WebSocket que usa ws_miners."""

    def __init__(self, token="secreto", miner_id="cpu-1", kind="cpu"):
        self.headers = {"authorization": f"Bearer {token}"}
        self.query_params = {"miner_id": miner_id, "kind": kind}
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = None

    async def accept(self):
        pass

    async def close(self, code=1000):
        self.closed = code

    async def send_json(self, msg):
        self.sent.append(msg)

    async def receive_text(self):
        msg = await self.inbox.get()
        if msg is None:
            from starlette.websockets import WebSocketDisconnect
            raise WebSocketDisconnect()
        return json.dumps(msg)

    async def wait_for(self, method, n=1, timeout=3.0):
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            got = [m for m in self.sent if m.get("method") == method or ("id" in m and method == "reply")]
            if len(got) >= n:
                return got
            await asyncio.sleep(0.02)
        raise AssertionError(f"no llegó {method} ×{n}: {self.sent}")


def _state(gw, monkeypatch, r, tasks=()):
    ex = FakeExchange()
    monkeypatch.setattr(gw, "MINER_TOKEN", "secreto")
    monkeypatch.setattr(gw, "state", {"r": r, "exchange": ex, "dlx": FakeExchange(), "solved": set(),
                                      "sockets": set(), "get_lock": asyncio.Lock(), "tasks": FakeQueue(tasks),
                                      "slot": r.register_script(gw.SLOT_LUA), "limit": r.register_script(gw.LIMIT_LUA)})
    return ex


def _authorize(ws: FakeWS, seed: bytes = SEED, worker="cpu-1", challenge=None):
    ch = challenge or next(m for m in ws.sent if m.get("method") == "mining.challenge")["params"]["challenge"]
    sig = agent.ed25519_sign(seed, firmas.domain(f"authorize:{worker}:{ch}".encode())).hex()
    ws.inbox.put_nowait({"jsonrpc": "2.0", "id": 1, "method": "mining.authorize",
                         "params": {"worker": worker, "pubkey": agent.ed25519_public(seed).hex(), "signature": sig}})


def test_authorize_firma_valida_ok_y_push_de_la_calibracion(redis_db, monkeypatch):
    gw = import_gateway()

    async def go():
        r = redis_db()
        _state(gw, monkeypatch, r)
        ws = FakeWS()
        run = asyncio.create_task(gw.ws_miners(ws))
        await ws.wait_for("mining.challenge")
        _authorize(ws)
        notify = (await ws.wait_for("mining.notify"))[0]
        reply = next(m for m in ws.sent if m.get("id") == 1)
        bound = await r.get(keys.worker_pubkey("cpu-1"))
        ws.inbox.put_nowait(None)
        await run
        await r.aclose()
        return reply, notify, bound
    reply, notify, bound = asyncio.run(go())
    assert reply["result"] is True
    assert notify["params"]["block_id"] == "calib"  # lo primero de un minero nuevo es la calibración
    assert bound == agent.ed25519_public(SEED).hex()


def test_authorize_desafio_de_otra_conexion_falla(redis_db, monkeypatch):
    gw = import_gateway()

    async def go():
        r = redis_db()
        _state(gw, monkeypatch, r)
        ws = FakeWS()
        run = asyncio.create_task(gw.ws_miners(ws))
        await ws.wait_for("mining.challenge")
        _authorize(ws, challenge="00" * 16)  # una firma vieja, de otro desafío
        await asyncio.wait_for(run, 3)
        await r.aclose()
        return ws
    ws = asyncio.run(go())
    err = next(m for m in ws.sent if m.get("id") == 1)
    assert err["error"]["code"] == -32001 and ws.closed == 1008
    assert not [m for m in ws.sent if m.get("method") == "mining.notify"]


def test_authorize_clave_distinta_falla(redis_db, monkeypatch):
    gw = import_gateway()

    async def go():
        r = redis_db()
        _state(gw, monkeypatch, r)
        await r.set(keys.worker_pubkey("cpu-1"), agent.ed25519_public(b"\x01" * 32).hex())  # ya tiene otra clave
        ws = FakeWS()
        run = asyncio.create_task(gw.ws_miners(ws))
        await ws.wait_for("mining.challenge")
        _authorize(ws)
        await asyncio.wait_for(run, 3)
        await r.aclose()
        return ws
    ws = asyncio.run(go())
    assert next(m for m in ws.sent if m.get("id") == 1)["error"]["code"] == -32001 and ws.closed == 1008


def test_authorize_por_http(redis_db, monkeypatch):
    """Sin WebSocket (fallback) el agente se autoriza igual por HTTP: GET /miners/challenge + POST /miners/authorize."""
    gw = import_gateway()

    async def go():
        r = redis_db()
        _state(gw, monkeypatch, r)
        ch = (await gw.miners_challenge("cpu-1", None))["challenge"]
        sig = agent.ed25519_sign(SEED, firmas.domain(f"authorize:cpu-1:{ch}".encode())).hex()
        body = {"miner_id": "cpu-1", "pubkey": agent.ed25519_public(SEED).hex(), "challenge": ch, "signature": sig}
        ok = await gw.miners_authorize(FakeRequest(body), None)
        with pytest.raises(HTTPException) as again:  # el mismo desafío no sirve dos veces
            await gw.miners_authorize(FakeRequest(body), None)
        await r.aclose()
        return ok, again.value.status_code
    ok, code = asyncio.run(go())
    assert ok == {"authorized": True} and code == 401


def test_notify_una_tarea_a_la_vez(redis_db, monkeypatch):
    gw = import_gateway()
    cur = {"index": 5, "block_id": "5-a", "base": "ab" * 48, "prefix": "0000", "range_size": 1000}
    tasks = [{"v": 1, "task_id": f"t{i}", "block_id": "5-a", "base": cur["base"], "prefix": "0000",
              "from": i * 1000, "to": i * 1000 + 999, "attempts": 0, "share_prefix": "00"} for i in range(3)]

    async def go():
        r = redis_db()
        _state(gw, monkeypatch, r, tasks)
        monkeypatch.setattr(gw, "CANARY_EVERY", 0)
        await r.set(keys.MINING_CURRENT, json.dumps(cur))
        await r.set(keys.miner_calibration("cpu-1"), "{}")  # ya calibrado
        ws = FakeWS()
        run = asyncio.create_task(gw.ws_miners(ws))
        await ws.wait_for("mining.challenge")
        _authorize(ws)
        first = (await ws.wait_for("mining.notify"))[0]["params"]
        await asyncio.sleep(0.5)
        still_one = len([m for m in ws.sent if m.get("method") == "mining.notify"])
        await r.delete(keys.lease(first["task_id"]))  # el agente entregó: el préstamo se cierra
        second = (await ws.wait_for("mining.notify", n=2))[1]["params"]
        ws.inbox.put_nowait(None)
        await run
        lease2 = await r.exists(keys.lease(second["task_id"]))
        await r.aclose()
        return first, still_one, second, lease2
    first, still_one, second, lease2 = asyncio.run(go())
    assert first["task_id"] == "t0" and still_one == 1  # no le manda otra mientras tiene una
    assert second["task_id"] == "t1" and lease2 == 1  # la empujada queda prestada como por HTTP


def test_ws_cerrado_lease_vence_y_reaper_republica(redis_db, monkeypatch):
    gw = import_gateway()
    cur = {"index": 5, "block_id": "5-a", "base": "ab" * 48, "prefix": "0000", "range_size": 1000}
    task = {"v": 1, "task_id": "t0", "block_id": "5-a", "base": cur["base"], "prefix": "0000",
            "from": 0, "to": 999, "attempts": 0, "share_prefix": "00"}

    async def go():
        r = redis_db()
        ex = _state(gw, monkeypatch, r, [task])
        monkeypatch.setattr(gw, "CANARY_EVERY", 0)
        await r.set(keys.MINING_CURRENT, json.dumps(cur))
        await r.set(keys.miner_calibration("cpu-1"), "{}")
        ws = FakeWS()
        run = asyncio.create_task(gw.ws_miners(ws))
        await ws.wait_for("mining.challenge")
        _authorize(ws)
        await ws.wait_for("mining.notify")
        ws.inbox.put_nowait(None)  # se corta la conexión con la tarea prestada
        await run
        await r.zadd(keys.LEASE_DEADLINES, {"t0": 0})  # pasaron los 60 s
        await gw.reap_once()
        await r.aclose()
        return ex.published
    published = asyncio.run(go())
    assert [(t["task_id"], rk) for t, rk in published] == [("t0", "task.any")]  # el rango no se pierde


# ---- Resultados firmados ----

class FakeRequest:
    def __init__(self, body):
        self.body = body
        self.headers = {}
        self.client = None

    async def json(self):
        return self.body


async def _lend_to_bound_worker(gw, r):
    cur = {"index": 5, "block_id": "5-a", "base": "ab" * 48, "prefix": "0000", "range_size": 1000}
    await r.set(keys.MINING_CURRENT, json.dumps(cur))
    await r.set(keys.worker_pubkey("cpu-1"), agent.ed25519_public(SEED).hex())
    task = {"v": 1, "task_id": "t0", "block_id": "5-a", "base": cur["base"], "prefix": "0000",
            "from": 0, "to": 999, "attempts": 0, "share_prefix": "00"}
    await gw.lend(task, "cpu-1", "cpu")
    return {"task_id": "t0", "block_id": "5-a", "worker": "cpu-1", "from": 0, "to": 999, "found": False, "shares": []}


def test_submit_sin_firma_rechazado(redis_db, monkeypatch):
    gw = import_gateway()

    async def go():
        r = redis_db()
        ex = _state(gw, monkeypatch, r)
        res = await _lend_to_bound_worker(gw, r)
        with pytest.raises(HTTPException) as exc:
            await gw.post_result(FakeRequest(dict(res)), None)
        still = await r.exists(keys.lease("t0"))
        res["signature"] = agent.sign_result(b"\x02" * 32, 2026, res)  # firmado con otra clave
        with pytest.raises(HTTPException) as exc2:
            await gw.post_result(FakeRequest(dict(res)), None)
        await r.aclose()
        return exc.value.status_code, exc2.value.status_code, still, ex.published
    code, code2, still, published = asyncio.run(go())
    assert code == 401 and code2 == 401 and still == 1 and published == []


def test_submit_firmado_pasa_una_sola_vez(redis_db, monkeypatch):
    gw = import_gateway()

    async def go():
        r = redis_db()
        ex = _state(gw, monkeypatch, r)
        res = await _lend_to_bound_worker(gw, r)
        res["signature"] = agent.sign_result(SEED, 2026, res)
        await gw.post_result(FakeRequest(dict(res)), None)
        with pytest.raises(HTTPException) as again:  # el mismo resultado otra vez: ya no hay préstamo
            await gw.post_result(FakeRequest(dict(res)), None)
        await r.aclose()
        return ex.published, again.value.status_code
    published, code = asyncio.run(go())
    assert len(published) == 1 and code == 410
