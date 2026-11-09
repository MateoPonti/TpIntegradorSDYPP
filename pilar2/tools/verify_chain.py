#!/usr/bin/env python3
"""Recorre la cadena por la API pública del coordinator y verifica cada bloque:
enlace con el anterior, tx_digest, hash MD5 y prefijo. Sirve después de reiniciar Redis
(¿se perdió algo con appendfsync everysec?) y en la defensa.

    python pilar2/tools/verify_chain.py --url http://localhost:8000
Solo stdlib. Código de salida 0 si la cadena es válida, 1 si no.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request


def get(url: str):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.load(resp)


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def check_block(block: dict, prev_hash: str) -> list[str]:
    errors = []
    if block["previous_hash"] != prev_hash:
        errors.append("previous_hash no coincide con el bloque anterior")
    digest = hashlib.sha256(canonical(block["transactions"]).encode("utf-8")).hexdigest()
    if digest != block["tx_digest"]:
        errors.append("tx_digest no coincide con las transacciones")
    real = hashlib.md5(f"{block['previous_hash']}{digest}{block['nonce']}".encode("utf-8")).hexdigest()
    if real != block["block_hash"]:
        errors.append("block_hash no coincide con md5(base + nonce)")
    if not real.startswith(block["prefix"]):
        errors.append(f"el hash no cumple el prefijo {block['prefix']}")
    return errors


def verify(base_url: str) -> int:
    base_url = base_url.rstrip("/")
    height = get(f"{base_url}/chain?limit=1")["height"]
    prev, bad = "0" * 32, 0
    for i in range(height):
        block = get(f"{base_url}/blocks/{i}")
        errors = check_block(block, prev)
        status = "ok" if not errors else "; ".join(errors)
        print(f"#{i} {block['block_hash']} prefix={block['prefix']!r} txs={len(block['transactions'])} {status}")
        bad += bool(errors)
        prev = block["block_hash"]
    print(f"altura={height} bloques_invalidos={bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000")
    sys.exit(verify(p.parse_args().url))
