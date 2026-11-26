"""Cuentas con clave (punto 1 del Contrato SDMER, opción A del 17/11/2026).

La única forma de tener una cuenta propia es registrarse con una clave pública Ed25519 (RFC 8032). Desde
ahí, toda tx de esa cuenta tiene que venir firmada con la privada y con `seq` = último + 1 (anti-replay).
Excepción fija: las cuentas del génesis (user-0..user-96), que usa bench.py sin firma y no se pueden registrar.
Como no firman, desde el 26/11 (pedido A1 del profe) solo pueden transferir a otras cuentas del génesis: su plata
es de prueba y no sale de ese circuito. La plata de las cuentas propias sale solo del minado.
"""
from tpi_common.firmas import verify  # noqa: F401 - la usa main.py como cuentas.verify

from .blocks import GENESIS_USERS
from .reward import REWARD_SENDER

GENESIS = frozenset(f"user-{i}" for i in range(GENESIS_USERS))


def is_genesis(name: str) -> bool:
    return name in GENESIS


def genesis_transfer_ok(sender: str, receiver: str) -> bool:
    """Una cuenta del génesis solo le transfiere a otra del génesis (A1, 26/11). Cualquiera puede mandar en nombre
    de user-0..96 porque no firman: si pudieran mandarle a una cuenta propia, cualquiera se llevaba los ~97M.
    bench.py (que no se puede tocar) manda solo entre user-0..96, así que sigue andando igual."""
    return not is_genesis(sender) or is_genesis(receiver)


def reserved(name: str) -> bool:
    """Nombres que nadie puede registrar: los del génesis, el del premio, las cuentas por defecto de mineros y las
    de las colectas."""
    return (is_genesis(name) or name in (REWARD_SENDER, "genesis") or name.startswith("miner:")
            or name.startswith("colecta:"))


def register_message(account: str, public_key: str) -> bytes:
    return f"register:{account}:{public_key}".encode()


def tx_message(sender: str, receiver: str, amount_cents: int, seq: int) -> bytes:
    return f"tx:{sender}:{receiver}:{amount_cents}:{seq}".encode()
