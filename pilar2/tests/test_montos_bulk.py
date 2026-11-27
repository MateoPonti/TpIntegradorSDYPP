"""C3: los montos inválidos no interrumpen transacciones independientes del bulk."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from coordinator import ledger
from coordinator import main as api
from tpi_common import keys


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"),
                                 "Infinity", "-Infinity", "NaN", "sNaN", "1e999999"])
def test_to_cents_no_finito_o_exponente_extremo(value):
    with pytest.raises(ledger.InvalidTx):
        ledger.to_cents(value)


@pytest.mark.parametrize("value", ["1e-999999999", "1.00000000000000000000000000001",
                                 "10000000000.0000000000000000001"])
def test_to_cents_no_redondea_ni_admite_cero_por_underflow(value):
    with pytest.raises(ledger.InvalidTx):
        ledger.to_cents(value)


def test_to_cents_limites_exactos():
    assert ledger.to_cents("0.01") == 1
    assert ledger.to_cents("10000000000.00") == ledger.MAX_AMOUNT_CENTS


@pytest.mark.parametrize("value", ["Infinity", "-Infinity", "NaN", "sNaN", "1e999999"])
def test_monto_invalido_devuelve_400(value):
    async def go():
        async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://test") as client:
            response = await client.post("/transactions", json={
                "sender": "user-1", "receiver": "user-2", "amount": value})
            assert response.status_code == 400
            assert "monto" in response.json()["error"]
    asyncio.run(go())


def test_bulk_monto_invalido_no_corta_reservas(redis_db, monkeypatch):
    async def go():
        r = redis_db()
        try:
            monkeypatch.setitem(api.state, "r", r)
            monkeypatch.setitem(api.state, "reserve", r.register_script(ledger.RESERVE_LUA))
            await r.set(keys.balance("user-1"), 10000)
            items = [{"sender": "user-1", "receiver": "user-2", "amount": value}
                     for value in (1, "NaN", 2)]
            async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://test") as client:
                response = await client.post("/transactions/bulk", json=items)
            assert response.status_code == 202
            body = response.json()
            assert (body["accepted"], body["rejected"]) == (2, 1)
            assert body["results"][1]["error"] == "monto inválido"
            pending = [json.loads(tx) for tx in await r.lrange(keys.TX_PENDING, 0, -1)]
            assert [tx["amount"] for tx in pending] == [100, 200]
            assert int(await r.get(keys.pending_out("user-1"))) == 300 + 2 * ledger.TX_FEE_CENTS
        finally:
            await r.aclose()
    asyncio.run(go())


def test_bulk_error_interno_no_corta_siguientes(monkeypatch, caplog):
    submit = AsyncMock(side_effect=[(202, {"tx_id": "primera"}), RuntimeError("fallo simulado"),
                                   (202, {"tx_id": "ultima"})])
    monkeypatch.setattr(api, "_submit", submit)

    async def go():
        async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://test") as client:
            response = await client.post("/transactions/bulk", json=[{}, {}, {}])
            assert response.status_code == 202
            body = response.json()
            assert (body["accepted"], body["rejected"]) == (2, 1)
            assert body["results"] == [{"tx_id": "primera"},
                                       {"error": "error interno con esta tx"}, {"tx_id": "ultima"}]
    asyncio.run(go())
    assert submit.await_count == 3
    assert "fallo simulado" in caplog.text
