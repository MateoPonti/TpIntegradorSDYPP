#!/usr/bin/env python3
"""Cliente de cuentas con clave (punto 1 del Contrato SDMER): genera el par Ed25519, registra la cuenta y
manda tx firmadas. La clave privada queda en un archivo local y nunca se manda al servidor.

    python pilar2/tools/cuenta.py claves  --archivo mateo.key
    python pilar2/tools/cuenta.py registrar --url http://35.223.237.60 --cuenta mateo --archivo mateo.key
    python pilar2/tools/cuenta.py enviar --url http://35.223.237.60 --cuenta mateo --archivo mateo.key --a user-3 --monto 10.50

Colectas (punto 2): crear una y aportar (aportar = enviar a colecta:<id>).
    python pilar2/tools/cuenta.py colecta --cuenta mateo --archivo mateo.key --id viaje --meta 500 --horas 24 [--a centro]
    python pilar2/tools/cuenta.py enviar  --cuenta ana   --archivo ana.key   --a colecta:viaje --monto 100
"""
from __future__ import annotations

import argparse
import json
import sys
import time
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


def firmar(key: Ed25519PrivateKey, url: str, texto: str) -> str:
    """Firma `sdmer:<chain_id>:<texto>` (desde el 26/11, A3-g). El chain id lo da el coordinator (GET /chain-id)."""
    code, info = http("GET", f"{url}/chain-id")
    if code != 200 or "chain_id" not in info:
        raise SystemExit(f"no se pudo leer el chain id de {url}/chain-id ({code})")
    return key.sign(f"sdmer:{info['chain_id']}:{texto}".encode()).hex()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("accion", choices=["claves", "registrar", "enviar", "colecta"])
    p.add_argument("--archivo", required=True, help="archivo con la clave privada (hex); no commitearlo")
    p.add_argument("--url", default="http://127.0.0.1:18080")
    p.add_argument("--cuenta")
    p.add_argument("--a", dest="receiver")
    p.add_argument("--monto")
    p.add_argument("--id", help="colecta: su id")
    p.add_argument("--meta", help="colecta: monto a juntar")
    p.add_argument("--horas", type=float, default=24, help="colecta: en cuántas horas cierra")
    p.add_argument("--titulo", default="")
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
        firma = firmar(key, a.url, f"register:{a.cuenta}:{pub}")
        code, body = http("POST", f"{a.url}/accounts/register", {"account": a.cuenta, "public_key": pub, "signature": firma})
    elif a.accion == "colecta":
        if not a.id or not a.meta:
            p.error("faltan --id y --meta")
        para, goal, deadline = a.receiver or a.cuenta, int(Decimal(a.meta) * 100), int(time.time() + a.horas * 3600)
        firma = firmar(key, a.url, f"crowdfund:create:{a.id}:{para}:{goal}:{deadline}")
        code, body = http("POST", f"{a.url}/crowdfunds", {"id": a.id, "title": a.titulo, "creator": a.cuenta,
                                                          "beneficiary": para, "goal": a.meta, "deadline": deadline,
                                                          "signature": firma})
    else:
        if not a.receiver or not a.monto:
            p.error("faltan --a y --monto")
        _, info = http("GET", f"{a.url}/balances/{a.cuenta}")
        seq = (info.get("seq") or 0) + 1
        cents = int(Decimal(a.monto) * 100)  # mismo cálculo que el servidor (centavos enteros)
        firma = firmar(key, a.url, f"tx:{a.cuenta}:{a.receiver}:{cents}:{seq}")
        code, body = http("POST", f"{a.url}/transactions", {"sender": a.cuenta, "receiver": a.receiver,
                                                            "amount": a.monto, "seq": seq, "signature": firma})
    print(code, json.dumps(body, ensure_ascii=False))
    return 0 if code < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
