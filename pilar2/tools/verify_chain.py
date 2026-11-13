#!/usr/bin/env python3
"""Recorre la cadena por la API pública del coordinator y verifica cada bloque:
enlace con el anterior, tx_digest, hash MD5, prefijo y el premio a los mineros (las tx de
`recompensa` del bloque i pagan el bloque i-1 y suman exactamente su `reward_cents`).
Sirve después de reiniciar Redis (¿se perdió algo con appendfsync everysec?) y en la defensa.

    python pilar2/tools/verify_chain.py --url http://localhost:8000
Solo stdlib. Código de salida 0 si la cadena es válida, 1 si no.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request

REWARD_SENDER = "recompensa"


def get(url: str):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.load(resp)


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def check_rewards(block: dict, prev_block: dict | None) -> list[str]:
    rewards = [t for t in block["transactions"] if t["sender"] == REWARD_SENDER]
    if not rewards:
        return []
    if prev_block is None:
        return ["hay premio sin bloque anterior"]
    errors = []
    if any(t.get("reward_for") != prev_block["index"] for t in rewards):
        errors.append("un premio no es del bloque anterior")
    if any(not str(t["receiver"]).startswith("miner:") for t in rewards):
        errors.append("un premio no va a una cuenta de minero")
    if sum(t["amount"] for t in rewards) != prev_block.get("reward_cents", 0):
        errors.append("los premios no suman el reward_cents del bloque anterior")
    return errors


def check_block(block: dict, prev_hash: str, prev_block: dict | None = None) -> list[str]:
    errors = check_rewards(block, prev_block)
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
    prev, prev_block, bad = "0" * 32, None, 0
    for i in range(height):
        block = get(f"{base_url}/blocks/{i}")
        errors = check_block(block, prev, prev_block)
        status = "ok" if not errors else "; ".join(errors)
        print(f"#{i} {block['block_hash']} prefix={block['prefix']!r} txs={len(block['transactions'])} {status}")
        bad += bool(errors)
        prev, prev_block = block["block_hash"], block
    print(f"altura={height} bloques_invalidos={bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000")
    sys.exit(verify(p.parse_args().url))
