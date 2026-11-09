"""Transacciones y saldos (decisiones.docx §6 y §8).

Modelo de cuentas: disponible = balance - pending_out. La reserva es atómica en Lua para que
dos tx simultáneas del mismo usuario no gasten dos veces la misma plata. Montos en centavos enteros.
"""
import time
import uuid
from decimal import Decimal, InvalidOperation

from tpi_common import keys

MAX_AMOUNT_CENTS = 10**12  # tope: el Lua de sellado convierte números con %.14g
MAX_NAME = 64

# KEYS: balance:<sender>, pending_out:<sender>, tx:pending, tx:seen, tx:status
# ARGV: tx_id, amount_cents, tx_json
RESERVE_LUA = """
if redis.call('SISMEMBER', KEYS[4], ARGV[1]) == 1 then return -1 end
local bal = tonumber(redis.call('GET', KEYS[1]) or '0')
local out = tonumber(redis.call('GET', KEYS[2]) or '0')
if bal - out < tonumber(ARGV[2]) then return -2 end
redis.call('INCRBY', KEYS[2], ARGV[2])
redis.call('SADD', KEYS[4], ARGV[1])
redis.call('HSET', KEYS[5], ARGV[1], 'pending')
redis.call('RPUSH', KEYS[3], ARGV[3])
return 1
"""

RESERVE_ERRORS = {-1: "duplicada", -2: "saldo insuficiente"}


class InvalidTx(ValueError):
    pass


def to_cents(value) -> int:
    """bench.py manda montos decimales (12.34). Se pasan a centavos sin pasar por float."""
    if isinstance(value, bool):
        raise InvalidTx("monto inválido")
    try:
        d = Decimal(str(value))
    except InvalidOperation as exc:
        raise InvalidTx("monto inválido") from exc
    cents = d * 100
    if cents != cents.to_integral_value():
        raise InvalidTx("monto con más de 2 decimales")
    cents = int(cents)
    if not 0 < cents <= MAX_AMOUNT_CENTS:
        raise InvalidTx("monto fuera de rango")
    return cents


def _name(body: dict, field: str) -> str:
    v = body.get(field)
    if not isinstance(v, str) or not v or len(v) > MAX_NAME:
        raise InvalidTx(f"{field} inválido")
    return v


def normalize(body) -> dict:
    """Valida el formato y devuelve la tx tal como entra al bloque. sender == receiver se permite."""
    if not isinstance(body, dict):
        raise InvalidTx("se esperaba un objeto JSON")
    tx_id = body.get("tx_id") or uuid.uuid4().hex  # opcional: permite reintentos idempotentes
    if not isinstance(tx_id, str) or len(tx_id) > MAX_NAME:
        raise InvalidTx("tx_id inválido")
    return {
        "tx_id": tx_id,
        "sender": _name(body, "sender"),
        "receiver": _name(body, "receiver"),
        "amount": to_cents(body.get("amount")),
        "ts": round(time.time(), 3),  # duda 6 (propuesta): hora de llegada en cada tx
    }


def reserve_keys(tx: dict) -> list[str]:
    return [keys.balance(tx["sender"]), keys.pending_out(tx["sender"]), keys.TX_PENDING, keys.TX_SEEN, keys.TX_STATUS]
