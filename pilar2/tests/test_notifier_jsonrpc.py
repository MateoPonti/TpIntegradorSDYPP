"""A3-a: contrato JSON-RPC 2.0 del notifier, con errores y suscripciones aisladas."""
import asyncio
import collections
from contextlib import asynccontextmanager
import json
import re
from unittest.mock import AsyncMock

import pytest

from notifier import main as notifier


def exchange(monkeypatch, request):
    async def go():
        r = AsyncMock()
        r.llen.return_value = 3
        r.get.return_value = "0"
        r.hget.return_value = "pending"
        monkeypatch.setitem(notifier.state, "r", r)
        client = notifier.Client(None)
        await notifier.handle(client, request if isinstance(request, str) else json.dumps(request))
        messages = []
        while not client.queue.empty():
            messages.append(client.queue.get_nowait())
        notifier.SUBSCRIPTIONS.dec(len(client.subs))
        return client, messages
    return asyncio.run(go())


def test_jsonrpc_subscribe_newBlocks(monkeypatch):
    client, messages = exchange(monkeypatch, {"jsonrpc": "2.0", "id": 1,
                                             "method": "sdmer_subscribe", "params": ["newBlocks"]})
    response, snapshot = messages
    assert response["jsonrpc"] == "2.0" and response["id"] == 1
    sid = response["result"]
    assert re.fullmatch(r"0x[0-9a-f]{16}", sid)
    assert client.subs == {sid: ("blocks", None)}
    assert snapshot == {"jsonrpc": "2.0", "method": "sdmer_subscription", "params": {
        "subscription": sid, "result": {"type": "snapshot", "height": 3, "tip": "0"}}}


@pytest.mark.parametrize("payload,code", [
    ("{", -32700), ("NaN", -32700), ([], -32600), (None, -32600),
    ({"id": 1, "method": "sdmer_subscribe", "params": ["newBlocks"]}, -32600),
    ({"jsonrpc": "2.0", "id": [], "method": "sdmer_subscribe"}, -32600),
    ({"jsonrpc": "2.0", "id": True, "method": "sdmer_subscribe"}, -32600),
    ({"jsonrpc": "2.0", "id": 1, "method": "desconocido"}, -32601),
    ({"jsonrpc": "2.0", "id": 1, "method": "sdmer_subscribe", "params": ["account"]}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": "sdmer_subscribe", "params": [["account"]]}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": "sdmer_subscribe", "params": ["newBlocks", "extra"]}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": "sdmer_subscribe", "params": ["tx", ""]}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": "sdmer_unsubscribe", "params": [{}]}, -32602),
])
def test_jsonrpc_errores(monkeypatch, payload, code):
    _, messages = exchange(monkeypatch, payload)
    assert len(messages) == 1
    assert messages[0]["jsonrpc"] == "2.0" and messages[0]["error"]["code"] == code
    assert "result" not in messages[0]


def test_jsonrpc_batch_y_notificaciones_sin_respuesta(monkeypatch):
    _, messages = exchange(monkeypatch, [
        {"jsonrpc": "2.0", "method": "desconocido"},
        {"jsonrpc": "2.0", "id": "a", "method": "desconocido"},
        {"jsonrpc": "2.0", "id": 2, "method": "sdmer_unsubscribe", "params": ["0xnoexiste"]},
    ])
    assert len(messages) == 1 and len(messages[0]) == 2
    assert messages[0][0]["id"] == "a"
    assert messages[0][1] == {"jsonrpc": "2.0", "id": 2, "result": False}
    _, messages = exchange(monkeypatch, {"jsonrpc": "2.0", "method": "desconocido"})
    assert messages == []


def test_jsonrpc_unsubscribe_y_limite(monkeypatch):
    async def go():
        monkeypatch.setattr(notifier, "MAX_SUBS", 1)
        r = AsyncMock()
        r.llen.return_value = 1
        r.get.return_value = "0"
        monkeypatch.setitem(notifier.state, "r", r)
        client = notifier.Client(None)
        subscribe = {"jsonrpc": "2.0", "id": 1, "method": "sdmer_subscribe", "params": ["newBlocks"]}
        await notifier.handle(client, json.dumps(subscribe))
        sid = client.queue.get_nowait()["result"]
        client.queue.get_nowait()
        await notifier.handle(client, json.dumps(subscribe))
        assert client.queue.get_nowait()["error"]["code"] == -32000
        unsubscribe = {"jsonrpc": "2.0", "id": 2, "method": "sdmer_unsubscribe", "params": [sid]}
        await notifier.handle(client, json.dumps(unsubscribe))
        assert client.queue.get_nowait()["result"] is True
        await notifier.handle(client, json.dumps(unsubscribe))
        assert client.queue.get_nowait()["result"] is False
        assert not client.subs
    asyncio.run(go())


def test_jsonrpc_notificacion_formato():
    ev = {"index": 4, "block_hash": "abc", "txs": [
        {"tx_id": "tx1", "sender": "ana", "receiver": "bob", "amount": 100}]}
    messages = notifier.route(ev, notifier.index_event(ev), {"0x123": ("account", "bob")})
    msg, = messages
    assert msg["jsonrpc"] == "2.0" and msg["method"] == "sdmer_subscription"
    assert msg["params"]["subscription"] == "0x123"
    assert msg["params"]["result"]["event"] == "account.changed"
    assert msg["params"]["result"]["account"] == "bob"
    assert "id" not in msg


def test_jsonrpc_evento_agrega_saldo_y_descarta_duplicado(monkeypatch):
    class Message:
        body = json.dumps({"type": "block.solved", "event_id": "block-4", "index": 4,
                           "block_hash": "abc", "txs": [{"tx_id": "tx1", "sender": "ana",
                                                          "receiver": "bob", "amount": 100}]}).encode()

        @asynccontextmanager
        async def process(self):
            yield

    async def go():
        r = AsyncMock()
        r.get.side_effect = ["100", "20"]
        monkeypatch.setitem(notifier.state, "r", r)
        client = notifier.Client(None)
        client.subs = {"0xa": ("account", "bob"), "0xb": ("blocks", None)}
        monkeypatch.setattr(notifier, "clients", {client})
        monkeypatch.setattr(notifier, "_seen", collections.deque(maxlen=1000))
        await notifier.on_event(Message())
        await notifier.on_event(Message())
        assert client.queue.qsize() == 2
        account = client.queue.get_nowait()["params"]["result"]
        assert account["available_cents"] == 80
        assert client.queue.get_nowait()["params"]["result"]["event"] == "block.solved"
    asyncio.run(go())
