"""Proof of Work y hashing. Mismo contrato que el kit del Pilar 1: md5(base + str(nonce))."""
import hashlib
import math
import json

MAX_NONCE = 2**64 - 1  # los nonces son uint64 en el minero CUDA
HEX = set("0123456789abcdef")


def canonical_json(obj) -> str:
    """JSON canónico: si coordinator y verificador serializan distinto, el hash no coincide nunca."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tx_digest(transactions: list[dict]) -> str:
    """SHA-256 del JSON canónico de las tx (64 hex). Es el `data` que ve el minero."""
    return sha256_hex(canonical_json(transactions))


def md5_hex(base: str, nonce: int) -> str:
    return hashlib.md5(f"{base}{nonce}".encode("utf-8")).hexdigest()


def expected_seconds(prefix_len: int, hashrate: float) -> float:
    """Con un prefijo de n caracteres hacen falta 16^n hashes en promedio."""
    return float("inf") if hashrate <= 0 else 16**prefix_len / hashrate


def is_valid_prefix(prefix: str) -> bool:
    return len(prefix) <= 32 and set(prefix) <= HEX


def verify(base: str, prefix: str, nonce: int, claimed_hash: str) -> bool:
    """NCT.3: nunca confiar en el minero. Se recalcula el hash y se mira el prefijo."""
    if not 0 <= nonce <= MAX_NONCE:
        return False
    real = md5_hex(base, nonce)
    return real == claimed_hash and real.startswith(prefix)


# ---- Shares (punto 4 del contrato, decidido el 20/11: shares con reparto PROP) ----
# Una share es un nonce del rango cuyo hash empieza con un prefijo MÁS CORTO que el del bloque. Encontrarla
# cuesta 16^largo hashes en promedio, así que cada una prueba ese trabajo y se paga con ese peso (ponderada por
# dificultad, como P2Pool): con cualquier largo, lo que se cobra en promedio es lo que se minó.
MAX_SHARES = 256  # por resultado: el minero manda a lo sumo estas


def share_prefix(prefix: str, range_size: int, target: int = 32) -> str:
    """Prefijo de las shares de una tarea: el largo con el que en el rango salen ~`target` shares, entre 1 y el
    largo del prefijo del bloque (toda solución del bloque también es una share)."""
    if not prefix:
        return ""
    n = round(math.log(max(range_size / target, 1), 16))
    return prefix[:max(1, min(len(prefix), n))]


def share_weight(prefix: str) -> int:
    return 16 ** len(prefix)


def valid_shares(base: str, prefix: str, lo: int, hi: int, nonces) -> list[int]:
    """Las shares que de verdad cumplen: dentro de [lo, hi], sin repetir y con el hash recalculado acá."""
    if not prefix or not isinstance(nonces, list):
        return []
    out = []
    for n in dict.fromkeys(nonces[:MAX_SHARES]):
        if isinstance(n, int) and not isinstance(n, bool) and lo <= n <= hi and md5_hex(base, n).startswith(prefix):
            out.append(n)
    return out
