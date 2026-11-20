"""Cuentas compartidas (punto 2 del Contrato SDMER, decidido el 20/11/2026).

Una cuenta con varios dueños (cuentas registradas) y un mínimo de firmas, m de n. Flujo de Safe (Ethereum) y del
multisig de Polkadot: un dueño propone un pago, los demás firman exactamente los mismos datos y, al llegar al
mínimo, el pago se admite (se reserva el saldo con el mismo Lua de siempre) y va a tx:pending.

- La firma es sobre `multisig:<cuenta>:<para>:<centavos>:<nonce>`; el id de la propuesta es el hash de ese texto
  (como Polkadot, que aprueba sobre el hash de la operación). Cambiar el monto o el destino cambia el id: las firmas
  de otra propuesta no sirven.
- Mientras falten firmas no se reserva saldo. Si al llegar al mínimo no alcanza, la propuesta queda rechazada.
- Vence a las MULTISIG_TTL_SECONDS (24 h) por hora del servidor: los bloques solo salen si hay tx, así que no sirven
  de reloj.
- Dueños y mínimo quedan fijos. La crea un dueño, sin que los demás acepten (como Safe).
"""
import hashlib
import json
import time

from tpi_common.config import env_int

MAX_OWNERS = 10
TTL = env_int("MULTISIG_TTL_SECONDS", 86400)
STATES = ("pendiente", "admitiendo", "admitida", "rechazada", "vencida")


def key(account: str) -> str:
    """STRING JSON {owners, threshold, creator, created}: la definición de la cuenta compartida (SET NX)."""
    return f"multisig:{account}"


def owner_index(owner: str) -> str:
    """SET de cuentas compartidas de las que es dueño (para listarlas en /cuenta)."""
    return f"multisig:of:{owner}"


def proposals_index(account: str) -> str:
    """ZSET id -> hora de creación: las propuestas de una cuenta compartida."""
    return f"multisig:{account}:proposals"


def proposal_key(pid: str) -> str:
    """HASH de una propuesta: account, receiver, amount, fee, message, created, expires, status, tx_id, reason,
    y un campo sig:<dueño> por cada firma."""
    return f"proposal:{pid}"


def create_message(account: str, threshold: int, owners: list[str]) -> bytes:
    return f"multisig:create:{account}:{threshold}:{','.join(owners)}".encode()


def payment_message(account: str, receiver: str, amount_cents: int, nonce: str) -> str:
    return f"multisig:{account}:{receiver}:{amount_cents}:{nonce}"


def proposal_id(message: str) -> str:
    return hashlib.sha256(message.encode()).hexdigest()[:32]


def check_definition(account: str, owners, threshold) -> tuple[list[str], int]:
    """Valida dueños y mínimo. Devuelve los dueños sin repetidos, en el orden en que vinieron."""
    if not isinstance(owners, list) or not all(isinstance(o, str) and o for o in owners):
        raise ValueError("owners: lista de cuentas")
    owners = list(dict.fromkeys(owners))
    if not 2 <= len(owners) <= MAX_OWNERS:
        raise ValueError(f"entre 2 y {MAX_OWNERS} dueños")
    if account in owners:
        raise ValueError("la cuenta compartida no puede ser dueña de sí misma")
    # Mínimo 2 firmas (20/11): el contrato pide una transferencia con aprobación extra; con 1, cualquier dueño gasta solo.
    if isinstance(threshold, bool) or not isinstance(threshold, int) or not 2 <= threshold <= len(owners):
        raise ValueError("threshold: entre 2 y la cantidad de dueños (con 1 no habría aprobación extra)")
    return owners, threshold


def view(raw: dict, threshold: int, now: float | None = None) -> dict:
    """Propuesta para la API: firmas, cuántas faltan y estado (una pendiente pasada de hora se muestra vencida)."""
    now = time.time() if now is None else now
    sigs = {k[4:]: v for k, v in raw.items() if k.startswith("sig:")}
    status = raw.get("status", "pendiente")
    if status == "pendiente" and float(raw.get("expires", 0)) < now:
        status = "vencida"
    return {"id": raw.get("id"), "account": raw.get("account"), "receiver": raw.get("receiver"),
            "amount_cents": int(raw.get("amount", 0)), "fee_cents": int(raw.get("fee", 0)),
            "message": raw.get("message"), "created": float(raw.get("created", 0)),
            "expires": float(raw.get("expires", 0)), "status": status, "signers": sorted(sigs),
            "signatures": len(sigs), "threshold": threshold, "missing": max(0, threshold - len(sigs)),
            "tx_id": raw.get("tx_id") or None, "reason": raw.get("reason") or None}


def tx_extra(raw: dict) -> dict:
    """Lo que queda en la tx del bloque: el mensaje y las firmas, así cualquiera puede volver a verificarlas."""
    return {"multisig": {"proposal": raw["id"], "message": raw["message"],
                         "signatures": {k[4:]: v for k, v in raw.items() if k.startswith("sig:")}}}


# KEYS: proposal:<id>   ARGV: dueño, firma, mínimo, ahora
# Agrega la firma (una por dueño) y, si se llegó al mínimo, pasa a 'admitiendo' y devuelve 'admitir'. Como es un
# solo Lua, si dos dueños firman a la vez solo uno recibe 'admitir': el pago no se puede admitir dos veces.
SIGN_LUA = """
local status = redis.call('HGET', KEYS[1], 'status')
if not status then return 'no_existe' end
if status ~= 'pendiente' then return status end
if tonumber(redis.call('HGET', KEYS[1], 'expires')) < tonumber(ARGV[4]) then
  redis.call('HSET', KEYS[1], 'status', 'vencida')
  return 'vencida'
end
redis.call('HSETNX', KEYS[1], 'sig:' .. ARGV[1], ARGV[2])
local n = 0
for _, f in ipairs(redis.call('HKEYS', KEYS[1])) do
  if string.sub(f, 1, 4) == 'sig:' then n = n + 1 end
end
if n >= tonumber(ARGV[3]) then
  redis.call('HSET', KEYS[1], 'status', 'admitiendo')
  return 'admitir'
end
return 'pendiente'
"""


def new_proposal(pid: str, account: str, receiver: str, amount: int, fee: int, message: str, now: float) -> dict:
    return {"id": pid, "account": account, "receiver": receiver, "amount": amount, "fee": fee,
            "message": message, "created": now, "expires": now + TTL, "status": "pendiente"}


def definition(raw: str | None) -> dict | None:
    return json.loads(raw) if raw else None
