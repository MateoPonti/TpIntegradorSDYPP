#!/usr/bin/env python3
"""Cliente de cuentas con clave (punto 1 del Contrato SDMER): genera el par Ed25519, registra la cuenta y
manda tx firmadas. La clave privada queda en un archivo local y nunca se manda al servidor.

    python pilar2/tools/cuenta.py claves  --archivo mateo.key
    python pilar2/tools/cuenta.py registrar --url http://35.223.237.60 --cuenta mateo --archivo mateo.key
    python pilar2/tools/cuenta.py enviar --url http://35.223.237.60 --cuenta mateo --archivo mateo.key --a user-3 --monto 10.50
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def cargar(archivo: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(Path(archivo).read_text().strip()))


def publica(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def http(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except ValueError:
            return exc.code, {"error": raw.decode(errors="replace")}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("accion", choices=["claves", "registrar", "enviar"])
    p.add_argument("--archivo", required=True, help="archivo con la clave privada (hex); no commitearlo")
    p.add_argument("--url", default="http://127.0.0.1:18080")
    p.add_argument("--cuenta")
    p.add_argument("--a", dest="receiver")
    p.add_argument("--monto")
    a = p.parse_args()

    if a.accion == "claves":
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
        Path(a.archivo).write_text(raw.hex())
        print(json.dumps({"archivo": a.archivo, "public_key": publica(key)}))
        return 0

    key = cargar(a.archivo)
    if not a.cuenta:
        p.error("falta --cuenta")
    if a.accion == "registrar":
        pub = publica(key)
        firma = key.sign(f"register:{a.cuenta}:{pub}".encode()).hex()
        code, body = http("POST", f"{a.url}/accounts/register", {"account": a.cuenta, "public_key": pub, "signature": firma})
    else:
        if not a.receiver or not a.monto:
            p.error("faltan --a y --monto")
        _, info = http("GET", f"{a.url}/balances/{a.cuenta}")
        seq = (info.get("seq") or 0) + 1
        cents = int(Decimal(a.monto) * 100)  # mismo cálculo que el servidor (centavos enteros)
        firma = key.sign(f"tx:{a.cuenta}:{a.receiver}:{cents}:{seq}".encode()).hex()
        code, body = http("POST", f"{a.url}/transactions", {"sender": a.cuenta, "receiver": a.receiver,
                                                            "amount": a.monto, "seq": seq, "signature": firma})
    print(code, json.dumps(body, ensure_ascii=False))
    return 0 if code < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
