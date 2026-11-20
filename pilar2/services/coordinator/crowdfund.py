"""Colectas "todo o nada" (crowdfunding, punto 2 del Contrato SDMER, decidido el 20/11/2026).

Alguien con cuenta crea una colecta con una meta, un beneficiario y una hora de cierre. Aportar es una tx más
(firmada, con su seq y su comisión) a la cuenta de la colecta, `colecta:<id>`, que nadie puede gastar a mano.
Al cierre (hora del servidor, como el vencimiento de la cuenta compartida) el líder la liquida:

- llegó a la meta: una tx con todo lo juntado de `colecta:<id>` al beneficiario;
- no llegó: una tx de devolución a cada uno por lo que aportó.

Como en Bitcoin, Ethereum o los crowdloans de Polkadot, la comisión del aporte no se devuelve (ya la cobró el
minero). Las tx de la liquidación las arma el sistema y no pagan comisión, así nadie paga dos veces. Tienen tx_id
fijo (`colecta-<id>-<cuenta>`), y la reserva de saldo descarta tx_id repetidos: si el líder se cae a mitad de la
liquidación, el siguiente la repite sin pagar dos veces.

Se liquida recién cuando todos los aportes están en un bloque (la plata ya está en la cuenta de la colecta) y
pasó CROWDFUND_GRACE_SECONDS del cierre, por si un aporte justo de la hora todavía se estaba admitiendo.
"""
import re
import time

from tpi_common.config import env_int

PREFIX = "colecta:"
ID = re.compile(r"[a-z0-9-]{3,32}")
MAX_DAYS = env_int("CROWDFUND_MAX_DAYS", 7)
MIN_SECONDS = env_int("CROWDFUND_MIN_SECONDS", 60)
GRACE = env_int("CROWDFUND_GRACE_SECONDS", 30)
MAX_TITLE = 80
INDEX = "crowdfunds"            # ZSET id -> hora de creación (para listarlas)
OPEN_INDEX = "crowdfunds:open"  # ZSET id -> hora de cierre (las que falta liquidar)


def account(cid: str) -> str:
    """Cuenta donde se junta la plata de la colecta."""
    return PREFIX + cid


def key(cid: str) -> str:
    """HASH: id, title, creator, beneficiary, goal, deadline, created, status, raised, result, settled."""
    return f"crowdfund:{cid}"


def contrib_key(cid: str) -> str:
    """HASH cuenta -> centavos aportados (para devolver)."""
    return f"crowdfund:{cid}:contrib"


def txs_key(cid: str) -> str:
    """SET de tx_id de los aportes (se liquida cuando todos están confirmados)."""
    return f"crowdfund:{cid}:txs"


def of_account(name: str) -> str | None:
    """Si `name` es la cuenta de una colecta, su id."""
    return name[len(PREFIX):] if name.startswith(PREFIX) else None


def create_message(cid: str, beneficiary: str, goal_cents: int, deadline: int) -> bytes:
    return f"crowdfund:create:{cid}:{beneficiary}:{goal_cents}:{deadline}".encode()


def check_deadline(deadline, now: float) -> int:
    if isinstance(deadline, bool) or not isinstance(deadline, int):
        raise ValueError("deadline: hora de cierre en segundos (unix)")
    if not now + MIN_SECONDS <= deadline <= now + MAX_DAYS * 86400:
        raise ValueError(f"el cierre tiene que ser entre {MIN_SECONDS} s y {MAX_DAYS} días desde ahora")
    return deadline


def is_open(raw: dict, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return raw.get("status") == "abierta" and float(raw.get("deadline", 0)) > now


def view(raw: dict, contrib: dict | None = None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    status = raw.get("status", "abierta")
    if status == "abierta" and float(raw.get("deadline", 0)) <= now:
        status = "cerrando"  # pasó la hora y todavía no se liquidó
    out = {"id": raw.get("id"), "account": account(raw.get("id", "")), "title": raw.get("title") or "",
           "creator": raw.get("creator"), "beneficiary": raw.get("beneficiary"),
           "goal_cents": int(raw.get("goal", 0)), "raised_cents": int(raw.get("raised", 0)),
           "deadline": float(raw.get("deadline", 0)), "created": float(raw.get("created", 0)), "status": status,
           "result": raw.get("result") or None, "settled": float(raw["settled"]) if raw.get("settled") else None}
    if contrib is not None:
        out["contributors"] = sorted(({"account": a, "amount_cents": int(c)} for a, c in contrib.items()),
                                     key=lambda x: -x["amount_cents"])
    return out


def outcome(raw: dict, contrib: dict) -> str:
    """'exitosa' si lo aportado llegó a la meta; si no, 'fallida'."""
    raised = sum(int(c) for c in contrib.values())
    return "exitosa" if raised > 0 and raised >= int(raw["goal"]) else "fallida"


def payments(result: str, raw: dict, contrib: dict) -> list[tuple[str, int]]:
    """Exitosa: todo al beneficiario. Fallida: a cada uno lo que aportó."""
    if result == "exitosa":
        return [(raw["beneficiary"], sum(int(c) for c in contrib.values()))]
    return sorted((a, int(c)) for a, c in contrib.items() if int(c) > 0)


def settlement_tx(cid: str, receiver: str, amount: int, result: str, now: float) -> dict:
    return {"tx_id": f"colecta-{cid}-{receiver}", "sender": account(cid), "receiver": receiver, "amount": amount,
            "ts": round(now, 3), "fee": 0,
            "crowdfund": {"id": cid, "kind": "pago" if result == "exitosa" else "devolucion"}}


# KEYS: crowdfund:<id>   ARGV: ahora, resultado
# Pasa de 'abierta' a 'liquidando' una sola vez (si dos líderes lo intentan, uno solo gana).
START_LUA = """
local status = redis.call('HGET', KEYS[1], 'status')
if status == 'liquidando' then return 1 end
if status ~= 'abierta' then return 0 end
if tonumber(redis.call('HGET', KEYS[1], 'deadline')) > tonumber(ARGV[1]) then return 0 end
redis.call('HSET', KEYS[1], 'status', 'liquidando', 'result', ARGV[2])
return 1
"""
