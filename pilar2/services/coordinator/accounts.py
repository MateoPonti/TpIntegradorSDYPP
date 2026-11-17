"""Cuentas con clave (punto 1 del Contrato SDMER, opción A del 17/11/2026).

La única forma de tener una cuenta propia es registrarse con una clave pública Ed25519 (RFC 8032). Desde
ahí, toda tx de esa cuenta tiene que venir firmada con la privada y con `seq` = último + 1 (anti-replay).
Excepción fija: las cuentas del génesis (user-0..user-96), que usa bench.py sin firma y no se pueden registrar.
"""
from tpi_common.firmas import verify  # noqa: F401 - la usa main.py como cuentas.verify

from .blocks import GENESIS_USERS
from .reward import REWARD_SENDER

GENESIS = frozenset(f"user-{i}" for i in range(GENESIS_USERS))


def is_genesis(name: str) -> bool:
    return name in GENESIS


def reserved(name: str) -> bool:
    """Nombres que nadie puede registrar: los del génesis, el del premio y las cuentas por defecto de mineros."""
    return is_genesis(name) or name in (REWARD_SENDER, "genesis") or name.startswith("miner:")


def register_message(account: str, public_key: str) -> bytes:
    return f"register:{account}:{public_key}".encode()


def tx_message(sender: str, receiver: str, amount_cents: int, seq: int) -> bytes:
    return f"tx:{sender}:{receiver}:{amount_cents}:{seq}".encode()
