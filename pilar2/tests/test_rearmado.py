"""C1: ganadores tardíos, shares y sellado concurrente contra Redis real."""
import asyncio
import json
import logging
import time
from unittest.mock import AsyncMock

import pytest

from coordinator import blocks
from tpi_common import keys, pow


async def candidate(r):
    chain = blocks.Chain(r, AsyncMock(), None, logging.getLogger("test.rearmado"))
    await chain.ensure_genesis()
    tx = {"tx_id": "test-tx", "sender": "user-1", "receiver": "user-2", "amount": 100,
          "fee": 10, "ts": time.time() - 20}
    digest = pow.tx_digest([tx])
    prev = await r.get(keys.CHAIN_TIP)
    cur = {"block_id": "1-nuevo", "index": 1, "previous_hash": prev, "base": prev + digest,
           "tx_digest": digest, "prefix": "0", "created": time.time() - 10, "rearmed": 1}
    await r.set(keys.MINING_CURRENT, json.dumps(cur))
    await r.rpush(keys.tx_inflight(1), json.dumps(tx))
    await r.set(keys.pending_out("user-1"), 110)
    nonce = next(n for n in range(100000) if pow.md5_hex(cur["base"], n).startswith("00"))
    res = {"block_id": "1-viejo", "from": 0, "to": 99999, "found": True, "nonce": nonce,
           "hash": pow.md5_hex(cur["base"], nonce), "worker": "gpu-justino",
           "share_prefix": "00", "shares": [nonce]}
    return chain, cur, res


def test_encontrado_de_block_id_rearmado_sella(redis_db):
    async def go():
        r = redis_db()
        try:
            chain, cur, res = await candidate(r)
            before = int(await r.get(keys.balance("user-1")))
            assert await chain._handle(res) == "sealed"
            assert await r.llen(keys.CHAIN) == 2
            sealed = json.loads(await r.get(keys.block(1)))
            assert sealed["prefix"] == cur["prefix"] and sealed["miner"] == "gpu-justino"
            assert sealed["nonce"] == res["nonce"]
            assert int(await r.get(keys.balance("user-1"))) == before - 110
            assert await r.hget(keys.TX_STATUS, "test-tx") == "1"
            assert int(await r.hget(keys.block_shares(1), "gpu-justino")) == 256
            assert await chain._handle(res) == "stale"
            assert await r.llen(keys.CHAIN) == 2
        finally:
            await r.aclose()
    asyncio.run(go())


@pytest.mark.parametrize("old_id", [False, True])
def test_dos_replicas_mismo_ganador_un_solo_sellado(redis_db, monkeypatch, old_id):
    async def go():
        r = redis_db()
        try:
            first, cur, res = await candidate(r)
            if not old_id:
                res["block_id"] = cur["block_id"]
            second = blocks.Chain(r, AsyncMock(), None, logging.getLogger("test.replica"))
            barrier = asyncio.Barrier(2)

            async def current():
                await barrier.wait()  # ambas réplicas leyeron el candidato antes de que una selle
                return dict(cur)

            monkeypatch.setattr(first, "current", current)
            monkeypatch.setattr(second, "current", current)
            before = int(await r.get(keys.balance("user-1")))
            statuses = await asyncio.gather(first._handle(dict(res)), second._handle(dict(res)))
            assert sorted(statuses) == ["sealed", "stale"]
            assert await r.llen(keys.CHAIN) == 2
            assert int(await r.get(keys.balance("user-1"))) == before - 110
            assert int(await r.hget(keys.block_shares(1), "gpu-justino")) == 256
        finally:
            await r.aclose()
    asyncio.run(go())


@pytest.mark.parametrize("change,status", [({"block_id": "2-otro"}, "stale"),
                                          ({"hash": "f" * 32}, "invalid"),
                                          ({"from": 100000}, "invalid")])
def test_rearmado_no_acepta_otro_bloque_ni_pow_invalido(redis_db, change, status):
    async def go():
        r = redis_db()
        try:
            chain, _, res = await candidate(r)
            res.update(change, worker=None)
            assert await chain._handle(res) == status
            assert await r.llen(keys.CHAIN) == 1
        finally:
            await r.aclose()
    asyncio.run(go())


def test_rearmado_sin_ganador_suma_shares_y_sigue_stale(redis_db):
    async def go():
        r = redis_db()
        try:
            chain, _, res = await candidate(r)
            res["found"] = False
            assert await chain._handle(res) == "stale"
            assert await r.llen(keys.CHAIN) == 1
            assert int(await r.hget(keys.block_shares(1), "gpu-justino")) == 256
        finally:
            await r.aclose()
    asyncio.run(go())
