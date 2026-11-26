"""Firmas Ed25519 (RFC 8032) de las cuentas registradas. Las usan el coordinator (tx firmadas) y el
gateway (alta de workers firmada por la cuenta).

Desde el 26/11 (A3-g, como EIP-155) todo lo que se firma lleva adelante el id de la cadena:
`sdmer:<CHAIN_ID>:<mensaje>`. Se agrega en un solo lugar de cada lado (`verify` acá, `firmar` en claves.js y
`domain` en los clientes de Python), así ningún tipo de mensaje queda sin él. Una firma de otra red, o una de antes
del cambio, no verifica."""
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .config import CHAIN_ID

# Error de firma: dice qué cambió, para que el que tiene el front o tools/cuenta.py viejo sepa qué hacer.
INVALID = "firma inválida (desde el 26/11 se firma con el chain id: actualizá la página o pilar2/tools/cuenta.py)"


def domain(message: bytes) -> bytes:
    """Lo que de verdad se firma: el mensaje con el id de la cadena adelante."""
    return f"sdmer:{CHAIN_ID}:".encode() + message


def verify(public_key_hex: str, message: bytes, signature_hex) -> bool:
    """True si la firma (hex, 64 bytes) es de esa clave pública (hex, 32 bytes) sobre domain(mensaje)."""
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        key.verify(bytes.fromhex(signature_hex), domain(message))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def worker_message(account: str, worker: str) -> bytes:
    """Lo que firma la cuenta para dar de alta un worker propio."""
    return f"worker:{account}:{worker}".encode()
