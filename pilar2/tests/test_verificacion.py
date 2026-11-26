"""Verificación de mineros que no se pueda engañar (pedido B1 del profe, Task 10).

* Canarios indistinguibles: mismo formato que una tarea real (con share_prefix), sobre el bloque actual, y su
  solución no sale de GET /blocks/{i}: es un rango que otro minero ya resolvió y cuyas shares ya se verificaron.
* Leases atados a su dueño: /results, /renew y /release solo los puede usar el minero al que se le prestó la tarea,
  y from/to/block_id/share_prefix salen del lease, no del pedido.
* Límite de identidades: rate limit por IP en el alta de cuentas y de workers (granjas Sybil).

Los que usan Redis necesitan REDIS_URL (ver test_lua.py).
"""
import asyncio
import json
import time

import pytest
from fastapi import HTTPException

from conftest import import_gateway
from coordinator import blocks
from tpi_common import keys, pow, ratelimit

BASE = "ab" * 16 + "cd" * 32  # previous_hash (32 hex) + tx_digest (64 hex), como una base real
CUR = {"index": 5, "block_id": "5-a", "base": BASE, "prefix": "0000", "range_size": 4096}


def _shares(lo: int, hi: int, sp: str) -> list[int]:
    return [n for n in range(lo, hi + 1) if pow.md5_hex(BASE, n).startswith(sp)]


# ---- Canarios ----

def test_canario_mismo_formato_que_tarea():
    gw = import_gateway()
    real = blocks.new_task(CUR, 0, 4096)
    sp = real["share_prefix"]
    rng = {"from": 8192, "to": 12287, "share_prefix": sp, "shares": _shares(8192, 12287, sp), "worker": "otro"}
    canary, known = gw.make_canary(CUR, [rng], "yo")
    assert set(canary) == set(real)  # mismas claves: nada que lo delate
    assert canary["block_id"] == CUR["block_id"] and canary["base"] == CUR["base"] and canary["prefix"] == CUR["prefix"]
    assert canary["share_prefix"] == sp and (canary["from"], canary["to"]) == (8192, 12287)
    assert canary["to"] - canary["from"] + 1 == real["to"] - real["from"] + 1  # mismo tamaño
    assert known == rng["shares"]


def test_canario_no_usa_un_rango_propio():
    gw = import_gateway()
    rng = {"from": 0, "to": 4095, "share_prefix": "0", "shares": [1], "worker": "yo"}
    assert gw.make_canary(CUR, [rng], "yo") is None  # se lo devolvería a quien lo resolvió: lo repetiría de memoria


def test_canario_no_usa_share_prefix_que_no_encaja_con_el_bloque():
    gw = import_gateway()
    rearmado = dict(CUR, prefix="0")  # se rearmó con un prefijo más corto que el share_prefix guardado
    rng = {"from": 0, "to": 4095, "share_prefix": "00", "shares": [1], "worker": "otro"}
    assert gw.make_canary(rearmado, [rng], "yo") is None  # una tarea real nunca tiene share_prefix más largo


def test_canario_pasa_con_todas_las_shares():
    gw = import_gateway()
    assert gw.canary_passed([3, 9, 27], [27, 3, 9])
    assert gw.canary_passed([3, 9], [3, 9, 11])  # encontró una más que el original (el original se la salteó)


def test_canario_falla_si_falta_una():
    gw = import_gateway()
    assert not gw.canary_passed([3, 9, 27], [3, 9])
    assert not gw.canary_passed([3], [])


def test_canario_con_shares_truncadas_alcanza_con_el_maximo():
    """Si el original mandó MAX_SHARES (pudo haber cortado), el GPU puede devolver otras: alcanza con MAX_SHARES
    válidas (cada una es trabajo que se verificó con MD5)."""
    gw = import_gateway()
    known = list(range(pow.MAX_SHARES))
    got = list(range(1000, 1000 + pow.MAX_SHARES))
    assert gw.canary_passed(known, got)
    assert not gw.canary_passed(known, got[:-1])


def test_coordinator_guarda_el_rango_verificado():
    sp = "0"
    good = _shares(0, 255, sp)
    entry = blocks.verified_range("m1", 0, 255, sp, good)
    assert entry == {"from": 0, "to": 255, "share_prefix": "0", "shares": good, "worker": "m1"}


# ---- Leases atados a su dueño ----

def test_lease_owner_ok():
    gw = import_gateway()
    lease = {"miner_id": "acc.w1"}
    assert gw.lease_owner_ok(lease, {"miner_id": "acc.w1", "account": "acc"}, None)
    assert not gw.lease_owner_ok(lease, {"miner_id": "acc.w2", "account": "acc"}, "acc.w1")  # el token manda
    assert gw.lease_owner_ok({"miner_id": "cpu-a"}, None, "cpu-a")  # token compartido: el nombre que manda
    assert not gw.lease_owner_ok({"miner_id": "cpu-a"}, None, "cpu-b")
    assert not gw.lease_owner_ok({"miner_id": "cpu-a"}, None, None)


class FakeExchange:
    def __init__(self):
        self.published = []

    async def publish(self, message, routing_key):
        self.published.append((json.loads(message.body), routing_key))


class FakeRequest:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


def _state(gw, monkeypatch, r):
    ex = FakeExchange()
    monkeypatch.setattr(gw, "REQUIRE_SIGNED_RESULTS", False)  # la firma de resultados se prueba en test_push.py
    monkeypatch.setattr(gw, "state", {"r": r, "exchange": ex, "dlx": FakeExchange(), "solved": set(),
                                      "sockets": set()})
    return ex


async def _setup(r, gw, task_id="t1", owner="cpu-a", lo=4096, hi=8191):
    await r.set(keys.MINING_CURRENT, json.dumps(CUR))
    task = blocks.new_task(CUR, lo, hi + 1)
    task["task_id"] = task_id
    await gw.lend(task, owner, "cpu")
    return task


def test_results_de_otro_dueno_409(redis_db, monkeypatch):
    gw = import_gateway()

    async def go():
        r = redis_db()
        _state(gw, monkeypatch, r)
        task = await _setup(r, gw)
        body = {"task_id": "t1", "block_id": task["block_id"], "worker": "cpu-b", "from": task["from"],
                "to": task["to"], "found": False, "shares": []}
        with pytest.raises(HTTPException) as exc:
            await gw.post_result(FakeRequest(body), None)
        still = await r.exists(keys.lease("t1"))
        await r.aclose()
        return exc.value.status_code, still
    code, still = asyncio.run(go())
    assert code == 409 and still == 1  # el préstamo sigue siendo del dueño


def test_results_ignora_from_to_del_body(redis_db, monkeypatch):
    gw = import_gateway()

    async def go():
        r = redis_db()
        ex = _state(gw, monkeypatch, r)
        task = await _setup(r, gw)
        body = {"task_id": "t1", "block_id": "5-otro", "worker": "cpu-a", "from": 0, "to": 10**12, "found": False,
                "shares": [], "share_prefix": ""}
        await gw.post_result(FakeRequest(body), None)
        await r.aclose()
        return task, ex.published
    task, published = asyncio.run(go())
    (res, rk), = published
    assert (res["from"], res["to"], res["block_id"]) == (task["from"], task["to"], task["block_id"])
    assert res["share_prefix"] == task["share_prefix"] and rk == "result.cpu"


def test_results_sin_lease_no_encontrado_410(redis_db, monkeypatch):
    """Un "no encontré" sin préstamo (vencido y re-publicado) no vale: el rango ya lo está haciendo otro."""
    gw = import_gateway()

    async def go():
        r = redis_db()
        ex = _state(gw, monkeypatch, r)
        await r.set(keys.MINING_CURRENT, json.dumps(CUR))
        body = {"task_id": "nadie", "block_id": CUR["block_id"], "worker": "cpu-a", "from": 0, "to": 99,
                "found": False}
        with pytest.raises(HTTPException) as exc:
            await gw.post_result(FakeRequest(body), None)
        await r.aclose()
        return exc.value.status_code, ex.published
    code, published = asyncio.run(go())
    assert code == 410 and published == []


def test_results_sin_lease_encontrado_se_reenvia(redis_db, monkeypatch):
    """Un "encontré" tardío sí se reenvía: el coordinator lo verifica con MD5 y no se pierde un bloque."""
    gw = import_gateway()

    async def go():
        r = redis_db()
        ex = _state(gw, monkeypatch, r)
        await r.set(keys.MINING_CURRENT, json.dumps(CUR))
        body = {"task_id": "nadie", "block_id": CUR["block_id"], "worker": "cpu-a", "from": 0, "to": 99,
                "found": True, "nonce": 1234, "hash": "00"}
        await gw.post_result(FakeRequest(body), None)
        await r.aclose()
        return ex.published
    (res, _), = asyncio.run(go())
    assert res["from"] == res["to"] == 1234  # el rango no se le cree: solo el nonce


def test_renew_y_release_de_otro_dueno_409(redis_db, monkeypatch):
    gw = import_gateway()

    async def go():
        r = redis_db()
        _state(gw, monkeypatch, r)
        await _setup(r, gw)
        codes = []
        for call in (gw.renew("t1", "cpu-b", None), gw.release("t1", "cpu-b", None)):
            try:
                await call
                codes.append(200)
            except HTTPException as exc:
                codes.append(exc.status_code)
        ok = await gw.renew("t1", "cpu-a", None)
        still = await r.exists(keys.lease("t1"))
        await r.aclose()
        return codes, ok, still
    codes, ok, still = asyncio.run(go())
    assert codes == [409, 409] and "lease_seconds" in ok and still == 1


def test_canario_en_el_gateway_pasa_y_falla(redis_db, monkeypatch):
    gw = import_gateway()

    async def go():
        r = redis_db()
        _state(gw, monkeypatch, r)
        await r.set(keys.MINING_CURRENT, json.dumps(CUR))
        sp = blocks.new_task(CUR, 0, 4096)["share_prefix"]
        known = _shares(8192, 12287, sp)
        await r.lpush(keys.block_verified_ranges(CUR["index"]),
                      json.dumps(blocks.verified_range("otro", 8192, 12287, sp, known)))
        canary, known_canary = await gw.canary_task(CUR, "honesto")
        assert known_canary == known
        await gw.lend(canary, "honesto", "cpu", special="canary", extra={"canary_shares": known})
        await gw.post_result(FakeRequest({"task_id": canary["task_id"], "block_id": canary["block_id"],
                                          "worker": "honesto", "from": canary["from"], "to": canary["to"],
                                          "found": False, "shares": known}), None)
        canary2, known_canary2 = await gw.canary_task(CUR, "vago")
        assert known_canary2 == known
        await gw.lend(canary2, "vago", "cpu", special="canary", extra={"canary_shares": known})
        await gw.post_result(FakeRequest({"task_id": canary2["task_id"], "block_id": canary2["block_id"],
                                          "worker": "vago", "from": canary2["from"], "to": canary2["to"],
                                          "found": False, "shares": known[:-1]}), None)
        out = (await r.exists(keys.quarantine("honesto")), await r.exists(keys.quarantine("vago")))
        await r.aclose()
        return known, out
    known, (honesto, vago) = asyncio.run(go())
    assert known, "el rango de prueba tiene que tener shares"
    assert honesto == 0 and vago == 1


# ---- Límite de identidades (Sybil) ----

def test_ratelimit_corta_en_el_sexto(redis_db):
    async def go():
        r = redis_db()
        out = [await ratelimit.allow(r, "ratelimit:test:1.2.3.4", 5, 3600) for _ in range(6)]
        other = await ratelimit.allow(r, "ratelimit:test:5.6.7.8", 5, 3600)
        ttl = await r.ttl("ratelimit:test:1.2.3.4")
        await r.aclose()
        return out, other, ttl
    out, other, ttl = asyncio.run(go())
    assert out == [True] * 5 + [False]
    assert other is True and 0 < ttl <= 3600


def test_client_ip_prefiere_cloudflare_y_despues_traefik():
    class Req:
        def __init__(self, headers, host="10.0.0.9"):
            self.headers = headers
            self.client = type("C", (), {"host": host})()
    assert ratelimit.client_ip(Req({"cf-connecting-ip": "1.1.1.1", "x-forwarded-for": "2.2.2.2"})) == "1.1.1.1"
    assert ratelimit.client_ip(Req({"x-forwarded-for": "2.2.2.2, 10.0.0.1"})) == "2.2.2.2"
    assert ratelimit.client_ip(Req({})) == "10.0.0.9"



# ---- Hashrate medido y Vardiff en el gateway (B1.2 y A3-d, Task 11) ----

def test_canario_con_vardiff_usa_el_largo_del_minero():
    """Con Vardiff cada minero tiene su largo de share: el canario usa el del minero (si no, se distinguiría) y las
    shares conocidas se filtran a ese largo (una share de largo 4 también es de largo 3)."""
    gw = import_gateway()
    sp = "00"
    known = _shares(0, 65535, sp)
    rng = {"from": 0, "to": 65535, "share_prefix": sp, "shares": known, "worker": "otro"}
    canary, k = gw.make_canary(CUR, [rng], "yo", share_len=3)
    assert canary["share_prefix"] == "000"
    assert k == [n for n in known if pow.md5_hex(BASE, n).startswith("000")] and k
    assert gw.make_canary(CUR, [rng], "yo", share_len=1) is None  # no se conocen las shares de largo 1


def test_record_result_cuenta_solo_shares_verificadas(redis_db, monkeypatch):
    gw = import_gateway()

    async def go():
        r = redis_db()
        _state(gw, monkeypatch, r)
        task = await _setup(r, gw)
        sp = task["share_prefix"]
        good = _shares(task["from"], task["to"], sp)
        bad = [n for n in range(task["from"], task["to"]) if n not in good][:5]
        await gw.post_result(FakeRequest({"task_id": "t1", "block_id": task["block_id"], "worker": "cpu-a",
                                          "from": task["from"], "to": task["to"], "found": False,
                                          "shares": good + bad, "hashes": 10**15, "engine_seconds": 1e-9}), None)
        entry = json.loads((await r.lrange(keys.miner_results("cpu-a"), 0, 0))[0])
        await r.aclose()
        return entry, good, sp
    entry, good, sp = asyncio.run(go())
    assert entry["valid_shares"] == len(good) and entry["share_len"] == len(sp)  # las 5 inventadas no cuentan


def test_lend_aplica_vardiff_con_hashrate_medido(redis_db, monkeypatch):
    from tpi_common import vardiff
    gw = import_gateway()

    async def go():
        r = redis_db()
        _state(gw, monkeypatch, r)
        cur = dict(CUR, prefix="00000000", range_size=1 << 36)
        await r.set(keys.MINING_CURRENT, json.dumps(cur))
        now = time.time()
        for i in range(3):  # un CPU: 1e6 H/s medidos con shares de largo 4 (16^4 hashes cada una)
            await r.lpush(keys.miner_results("cpu-1"), json.dumps(
                {"ts": now - i, "seconds": 1.0, "share_len": 4, "valid_shares": round(1e6 / 16**4), "hashes": 0}))
        slow = await gw.vardiff_len("cpu-1", cur)
        nuevo = await gw.vardiff_len("nuevo", cur)  # sin medición: queda la del coordinator
        task = blocks.new_task(cur, 0, 1 << 36)
        lent = await gw.lend(dict(task), "cpu-1", "cpu", share_len=slow)
        await r.aclose()
        return slow, nuevo, lent, task
    slow, nuevo, lent, task = asyncio.run(go())
    assert slow == vardiff.share_len(round(1e6 / 16**4) * 16**4, 8, 1 << 36) == 7  # el más corto que no trunca
    assert task["share_prefix"] == "0" * 8  # la del coordinator: ~32 shares por tarea
    assert lent["share_prefix"] == "0" * 7  # Vardiff: más fáciles para el lento
    assert nuevo is None
