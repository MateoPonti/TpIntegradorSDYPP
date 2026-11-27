"""B4: métricas por tipo, espera en cola y validación del ganador."""
import asyncio
from contextlib import asynccontextmanager
import json
import logging
import time
from unittest.mock import AsyncMock

import pytest
from prometheus_client import REGISTRY

from conftest import import_gateway
from coordinator import blocks
from tpi_common import keys, pow


def sample(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {}) or 0


@pytest.mark.parametrize("routing_key,body,status,kind", [
    ("result.gpu", "{}", "empty", "gpu"), ("result.cpu", "{}", "empty", "cpu"),
    ("result.gpu", "{", "malformed", "gpu"), ("result.ajeno", "{}", "empty", "unknown")])
def test_results_total_tiene_label_kind(routing_key, body, status, kind):
    class Message:
        @asynccontextmanager
        async def process(self, **kwargs):
            yield
    message = Message()
    message.body, message.routing_key = body, routing_key
    chain = object.__new__(blocks.Chain)
    chain._handle = AsyncMock(return_value="empty")
    labels = {"status": status, "kind": kind}
    before = sample("results_total", labels)
    asyncio.run(chain.on_result(message))
    assert sample("results_total", labels) == before + 1


def test_latencia_cola_no_incluye_canario_y_no_sale_al_minero(monkeypatch):
    gw = import_gateway()
    monkeypatch.setitem(gw.state, "r", AsyncMock())
    monkeypatch.setattr(gw.time, "time", lambda: 100.0)
    task = {"task_id": "t", "published_at": 97.5}
    before = sample("task_queue_to_lease_seconds_sum", {"kind": "gpu"})
    count = sample("task_queue_to_lease_seconds_count", {"kind": "gpu"})
    response = asyncio.run(gw.lend(dict(task), "gpu-test", "gpu"))
    assert sample("task_queue_to_lease_seconds_sum", {"kind": "gpu"}) == before + 2.5
    assert sample("task_queue_to_lease_seconds_count", {"kind": "gpu"}) == count + 1
    assert "published_at" not in response  # no permite distinguir canarios por su espera casi nula
    asyncio.run(gw.lend(dict(task), "gpu-test", "gpu", special="canary"))
    asyncio.run(gw.lend({"task_id": "vieja"}, "gpu-test", "gpu"))
    assert sample("task_queue_to_lease_seconds_count", {"kind": "gpu"}) == count + 1


def test_tarea_tiene_hora_de_publicacion(monkeypatch):
    monkeypatch.setattr(blocks.time, "time", lambda: 123.0)
    task = blocks.new_task({"block_id": "1-a", "base": "abc", "prefix": "00", "range_size": 1000}, 0, 1000)
    assert task["published_at"] == 123.0


def test_validacion_y_minado_por_prefijo(redis_db, monkeypatch):
    async def go():
        r = redis_db()
        try:
            chain = blocks.Chain(r, AsyncMock(), None, logging.getLogger("test.metricas"))
            await chain.ensure_genesis()
            tx = {"tx_id": "metricas-tx", "sender": "user-1", "receiver": "user-2", "amount": 100, "ts": 0}
            prev, digest = await r.get(keys.CHAIN_TIP), pow.tx_digest([tx])
            cur = {"index": 1, "block_id": "1-actual", "base": prev + digest, "previous_hash": prev,
                   "tx_digest": digest, "prefix": "0000", "created": time.time() - 3}
            await r.set(keys.MINING_CURRENT, json.dumps(cur))
            await r.rpush(keys.tx_inflight(1), json.dumps(tx))
            await r.set(keys.pending_out("user-1"), 100)
            nonce = next(n for n in range(2000000) if pow.md5_hex(cur["base"], n).startswith("0000"))
            res = {"block_id": "1-actual", "from": 0, "to": 2000000, "found": True, "nonce": nonce,
                   "hash": pow.md5_hex(cur["base"], nonce), "worker": "gpu-test"}
            before = sample("block_validation_seconds_count")
            mined = sample("block_seconds_count", {"prefix_len": "4"})

            async def flush():
                # Publicar el outbox no forma parte de validar el PoW y sellar en Redis.
                assert sample("block_validation_seconds_count") == before + 1
            monkeypatch.setattr(chain, "flush_outbox", flush)
            assert await chain._handle(res) == "sealed"
            assert sample("block_validation_seconds_count") == before + 1
            assert sample("block_seconds_count", {"prefix_len": "4"}) == mined + 1
        finally:
            await r.aclose()
    asyncio.run(go())
