"""Firmas Ed25519 (RFC 8032) de las cuentas registradas. Las usan el coordinator (tx firmadas) y el
gateway (alta de workers firmada por la cuenta)."""
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def verify(public_key_hex: str, message: bytes, signature_hex) -> bool:
    """True si la firma (hex, 64 bytes) es de esa clave pública (hex, 32 bytes) sobre el mensaje."""
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        key.verify(bytes.fromhex(signature_hex), message)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def worker_message(account: str, worker: str) -> bytes:
    """Lo que firma la cuenta para dar de alta un worker propio."""
    return f"worker:{account}:{worker}".encode()
