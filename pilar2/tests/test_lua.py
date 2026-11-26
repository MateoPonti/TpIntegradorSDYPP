"""Tests de los scripts Lua y del reaper del gateway contra un Redis real (pedido C7 del profe).

    docker run -d -p 6379:6379 redis:7.4-alpine
    REDIS_URL=redis://localhost:6379/15 python -m pytest -q pilar2/tests/test_lua.py

Los Lua son los que garantizan lo crítico (no gastar dos veces, una firma por dueño, liquidar una vez), y eso solo
se puede probar de verdad con varias llamadas al mismo tiempo sobre un Redis real.
"""
import asyncio
import json
import time

from coordinator import crowdfund, ledger, multisig
from tpi_common import keys


def run(coro):
    return asyncio.run(coro)


async def _reserve(r, script, sender: str, amount: int, seq: str = "") -> int:
    tx = {"tx_id": f"t-{time.time_ns()}-{amount}-{seq}", "sender": sender, "receiver": "b", "amount": amount}
    return await script(keys=ledger.reserve_keys(tx), args=[tx["tx_id"], amount, json.dumps(tx), seq])


# ---- Reserva de saldo (RESERVE_LUA) ----

def test_reserva_no_deja_saldo_negativo(redis_db):
    async def go():
        r = redis_db()
        reserve = r.register_script(ledger.RESERVE_LUA)
        await r.set(keys.balance("user-1"), 500)
        rcs = await asyncio.gather(*(_reserve(r, reserve, "user-1", 10) for _ in range(100)))
        out = int(await r.get(keys.pending_out("user-1")))
        await r.aclose()
        return rcs, out
    rcs, out = run(go())
    assert rcs.count(1) == 50 and rcs.count(-2) == 50
    assert out == 500


def test_reserva_seq_repetido_rechazado(redis_db):
    async def go():
        r = redis_db()
        reserve = r.register_script(ledger.RESERVE_LUA)
        await r.set(keys.balance("fede"), 1000)
        first = await _reserve(r, reserve, "fede", 10, seq="1")
        again = await _reserve(r, reserve, "fede", 10, seq="1")  # misma firma reenviada (replay)
        jump = await _reserve(r, reserve, "fede", 10, seq="3")   # salteó el 2
        nxt = await _reserve(r, reserve, "fede", 10, seq="2")
        await r.aclose()
        return first, again, jump, nxt
    assert run(go()) == (1, -3, -3, 1)


def test_reserva_tx_id_duplicado(redis_db):
    async def go():
        r = redis_db()
        reserve = r.register_script(ledger.RESERVE_LUA)
        await r.set(keys.balance("user-2"), 1000)
        tx = {"tx_id": "dup", "sender": "user-2", "receiver": "b", "amount": 5}
        args = [tx["tx_id"], 5, json.dumps(tx), ""]
        a = await reserve(keys=ledger.reserve_keys(tx), args=args)
        b = await reserve(keys=ledger.reserve_keys(tx), args=args)
        out = int(await r.get(keys.pending_out("user-2")))
        await r.aclose()
        return a, b, out
    assert run(go()) == (1, -1, 5)


# ---- Cuentas compartidas: aceptar (ACCEPT_LUA) y firmar propuestas (SIGN_LUA) ----

def _accept_args(account: str, owner: str, owners: int):
    return dict(keys=[multisig.status_key(account), multisig.accepted_key(account),
                      multisig.invites_index(owner), multisig.owner_index(owner)],
                args=[owner, owners, account])


def test_accept_lua_el_ultimo_activa(redis_db):
    async def go():
        r = redis_db()
        accept = r.register_script(multisig.ACCEPT_LUA)
        await r.set(multisig.status_key("caja"), "pendiente")
        out = [await accept(**_accept_args("caja", o, 3)) for o in ("a", "b", "a", "c")]
        await r.aclose()
        return out
    assert run(go()) == ["pendiente", "pendiente", "pendiente", "activa"]  # aceptar dos veces no suma


def test_accept_lua_no_revive_una_rechazada(redis_db):
    async def go():
        r = redis_db()
        accept = r.register_script(multisig.ACCEPT_LUA)
        await r.set(multisig.status_key("caja"), "rechazada")
        rc = await accept(**_accept_args("caja", "a", 1))
        await r.aclose()
        return rc
    assert run(go()) == "rechazada"


async def _new_proposal(r, pid: str, now: float) -> None:
    p = multisig.new_proposal(pid, "caja", "fede", 100, 10, "msg", now)
    await r.hset(multisig.proposal_key(pid), mapping=p)


def test_sign_lua_un_dueno_una_firma(redis_db):
    async def go():
        r = redis_db()
        sign = r.register_script(multisig.SIGN_LUA)
        now = time.time()
        await _new_proposal(r, "p1", now)
        k = [multisig.proposal_key("p1"), multisig.WAITING]
        a = await sign(keys=k, args=["ana", "sig1", 2, now])
        b = await sign(keys=k, args=["ana", "sig2", 2, now])  # la misma dueña otra vez: no llega al mínimo
        await r.aclose()
        return a, b
    assert run(go()) == ("pendiente", "pendiente")


def test_sign_lua_dos_firmas_simultaneas_una_admision(redis_db):
    async def go():
        r = redis_db()
        sign = r.register_script(multisig.SIGN_LUA)
        now = time.time()
        await _new_proposal(r, "p2", now)
        k = [multisig.proposal_key("p2"), multisig.WAITING]
        await sign(keys=k, args=["ana", "s", 2, now])
        out = await asyncio.gather(*(sign(keys=k, args=[f"o{i}", "s", 2, now]) for i in range(20)))
        waiting = await r.zcard(multisig.WAITING)
        await r.aclose()
        return out, waiting
    out, waiting = run(go())
    assert out.count("admitir") == 1  # el pago se admite una sola vez
    assert waiting == 1


def test_sign_lua_vencida(redis_db):
    async def go():
        r = redis_db()
        sign = r.register_script(multisig.SIGN_LUA)
        now = time.time()
        await _new_proposal(r, "p3", now)
        rc = await sign(keys=[multisig.proposal_key("p3"), multisig.WAITING],
                        args=["ana", "s", 2, now + multisig.TTL + 1])
        await r.aclose()
        return rc
    assert run(go()) == "vencida"


# ---- Colectas: empezar la liquidación una sola vez (START_LUA) ----

def test_cf_start_liquida_una_sola_vez(redis_db):
    async def go():
        r = redis_db()
        start = r.register_script(crowdfund.START_LUA)
        now = time.time()
        await r.hset(crowdfund.key("viaje"), mapping={"status": "abierta", "deadline": now - 1})
        antes = await start(keys=[crowdfund.key("viaje")], args=[now - 10, "lograda"])  # todavía no cerró
        out = await asyncio.gather(*(start(keys=[crowdfund.key("viaje")], args=[now, "lograda"]) for _ in range(10)))
        result = await r.hget(crowdfund.key("viaje"), "result")
        await r.aclose()
        return antes, out, result
    antes, out, result = run(go())
    assert antes == 0
    assert all(rc == 1 for rc in out)  # 'liquidando' devuelve 1: el líder retoma una liquidación a medias
    assert result == "lograda"


def test_cf_start_no_reabre_una_liquidada(redis_db):
    async def go():
        r = redis_db()
        start = r.register_script(crowdfund.START_LUA)
        await r.hset(crowdfund.key("viaje"), mapping={"status": "lograda", "deadline": 0})
        rc = await start(keys=[crowdfund.key("viaje")], args=[time.time(), "fallida"])
        await r.aclose()
        return rc
    assert run(go()) == 0


# ---- Reaper del gateway ----

class FakeExchange:
    def __init__(self):
        self.published = []

    async def publish(self, message, routing_key):
        self.published.append((json.loads(message.body), routing_key))


def _gateway_state(monkeypatch, r):
    from conftest import import_gateway
    gw = import_gateway()
    ex, dlx = FakeExchange(), FakeExchange()
    monkeypatch.setattr(gw, "state", {"r": r, "exchange": ex, "dlx": dlx, "solved": set(), "sockets": set()})
    return gw, ex, dlx


async def _lease(r, task_id: str, deadline: float, attempts: int = 0, block_id: str = "5-a") -> None:
    task = {"v": 1, "task_id": task_id, "block_id": block_id, "from": 0, "to": 99, "attempts": attempts}
    await r.set(keys.lease(task_id), json.dumps({"task": task, "miner_id": "m1", "kind": "cpu",
                                                 "since": time.time(), "special": None}))
    await r.zadd(keys.LEASE_DEADLINES, {task_id: deadline})


def test_reaper_republica_vencidos(redis_db, monkeypatch):
    async def go():
        r = redis_db()
        gw, ex, dlx = _gateway_state(monkeypatch, r)
        await r.set(keys.MINING_CURRENT, json.dumps({"block_id": "5-a", "index": 5}))
        await _lease(r, "vencida", time.time() - 1)
        await _lease(r, "vigente", time.time() + 60)
        n = await gw.reap_once()
        left = await r.zrange(keys.LEASE_DEADLINES, 0, -1)
        gone = await r.exists(keys.lease("vencida"))
        streak = json.loads(await r.get(keys.miner_expired("m1")))["streak"]
        await r.aclose()
        return n, ex.published, dlx.published, left, gone, streak
    n, published, dlq, left, gone, streak = run(go())
    assert n == 1
    assert [(t["task_id"], t["attempts"], rk) for t, rk in published] == [("vencida", 1, "task.any")]
    assert dlq == []
    assert left == ["vigente"] and gone == 0
    assert streak == 1  # cuenta para la cuarentena por préstamos vencidos


def test_reaper_manda_a_la_dlq_al_agotar_intentos(redis_db, monkeypatch):
    async def go():
        r = redis_db()
        gw, ex, dlx = _gateway_state(monkeypatch, r)
        await r.set(keys.MINING_CURRENT, json.dumps({"block_id": "5-a", "index": 5}))
        await _lease(r, "cansada", time.time() - 1, attempts=gw.MAX_ATTEMPTS - 1)
        await gw.reap_once()
        await r.aclose()
        return ex.published, dlx.published
    published, dlq = run(go())
    assert published == []
    assert [rk for _, rk in dlq] == ["dlq"]


def test_reaper_no_republica_tareas_de_un_bloque_cerrado(redis_db, monkeypatch):
    async def go():
        r = redis_db()
        gw, ex, _ = _gateway_state(monkeypatch, r)
        await r.set(keys.MINING_CURRENT, json.dumps({"block_id": "6-b", "index": 6}))
        await _lease(r, "vieja", time.time() - 1, block_id="5-a")
        n = await gw.reap_once()
        await r.aclose()
        return n, ex.published
    n, published = run(go())
    assert n == 1 and published == []  # el lease se limpia, pero ese rango ya no hace falta


def test_reaper_una_sola_replica_por_vuelta(redis_db, monkeypatch):
    async def go():
        r = redis_db()
        gw, ex, _ = _gateway_state(monkeypatch, r)
        await r.set(keys.MINING_CURRENT, json.dumps({"block_id": "5-a", "index": 5}))
        for i in range(5):
            await _lease(r, f"t{i}", time.time() - 1)
        counts = await asyncio.gather(*(gw.reap_once() for _ in range(4)))
        await r.aclose()
        return counts, len(ex.published)
    counts, published = run(go())
    assert sum(counts) == 5 and published == 5  # ningún rango se re-publica dos veces
