"""C2: la última aceptación y el rechazo compiten por una sola transición."""
import asyncio
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException

from coordinator import main as api, multisig
from tpi_common import firmas, keys


class Request:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


async def setup(r, monkeypatch, account):
    definition = {"owners": ["ana", "bob"], "threshold": 2, "creator": "ana", "created": 1}
    await r.set(multisig.key(account), json.dumps(definition))
    await r.set(multisig.status_key(account), "pendiente")
    await r.sadd(multisig.accepted_key(account), "ana")
    await r.sadd(multisig.invites_index("bob"), account)
    await r.sadd(multisig.owner_index("ana"), account)
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    await r.set(keys.account_pubkey("bob"), key.public_key().public_bytes_raw().hex())
    monkeypatch.setitem(api.state, "r", r)
    monkeypatch.setitem(api.state, "accept", r.register_script(multisig.ACCEPT_LUA))
    if hasattr(multisig, "REJECT_LUA"):
        monkeypatch.setitem(api.state, "reject", r.register_script(multisig.REJECT_LUA))

    def request(message):
        return Request({"owner": "bob", "signature": key.sign(firmas.domain(message)).hex()})

    accept = request(multisig.create_message(account, 2, definition["owners"]))
    reject = request(multisig.reject_message(account))
    return accept, reject


async def call(handler, *args):
    try:
        await handler(*args)
        return 200
    except HTTPException as exc:
        return exc.status_code


def test_aceptar_y_rechazar_a_la_vez(redis_db, monkeypatch):
    async def go():
        r = redis_db()
        try:
            for i in range(50):
                account = f"caja-{i}"
                accept, reject = await setup(r, monkeypatch, account)
                rejecting, accepting = await asyncio.gather(
                    call(api.reject_multisig, account, reject), call(api.accept_multisig, account, accept))
                status = await r.get(multisig.status_key(account))
                accepted = await r.smembers(multisig.accepted_key(account))
                if status == "activa":
                    assert (accepting, rejecting, accepted) == (200, 409, {"ana", "bob"})
                else:
                    assert status == "rechazada"
                    assert (accepting, rejecting, accepted) == (409, 200, {"ana"})
                assert not await r.sismember(multisig.invites_index("bob"), account)
        finally:
            await r.aclose()
    asyncio.run(go())


def test_rechazo_no_activa_ni_modifica_accepted(redis_db, monkeypatch):
    async def go():
        r = redis_db()
        try:
            accept, reject = await setup(r, monkeypatch, "caja")
            assert await call(api.reject_multisig, "caja", reject) == 200
            assert await call(api.accept_multisig, "caja", accept) == 409
            assert await r.smembers(multisig.accepted_key("caja")) == {"ana"}
            assert not await r.sismember(multisig.owner_index("bob"), "caja")
            assert not await r.sismember(multisig.invites_index("bob"), "caja")
        finally:
            await r.aclose()
    asyncio.run(go())


def test_rechazo_de_activa_da_409(redis_db, monkeypatch):
    async def go():
        r = redis_db()
        try:
            accept, reject = await setup(r, monkeypatch, "caja")
            assert await call(api.accept_multisig, "caja", accept) == 200
            assert await call(api.reject_multisig, "caja", reject) == 409
            assert await r.get(multisig.status_key("caja")) == "activa"
        finally:
            await r.aclose()
    asyncio.run(go())
