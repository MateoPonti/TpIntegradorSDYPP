"""Proof of Work y hashing. Mismo contrato que el kit del Pilar 1: md5(base + str(nonce))."""
import hashlib
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
