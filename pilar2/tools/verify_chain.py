#!/usr/bin/env python3
"""Recorre la cadena por la API pública del coordinator y verifica cada bloque:
enlace con el anterior, tx_digest, hash MD5, prefijo y el premio a los mineros (las tx de
`recompensa` del bloque i pagan el bloque i-1 y suman su `reward_cents` más lo que haya quedado acumulado de
bloques que no tuvieron a quién pagarle, ver PPLNS y `reward:carry`). Desde el 20/11 el premio es
subsidio con halving + comisiones (modelo de Bitcoin): se comprueba que el subsidio sea el que toca según la altura
(parámetros de GET /status) y que las comisiones sumen las de las tx del bloque.
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


def check_rewards(block: dict, prev_block: dict | None, carry: int = 0) -> tuple[list[str], int]:
    """Verifica el premio del bloque y devuelve también el carry que queda después (C4, 27/11).

    Con PPLNS un bloque puede no tener a quién pagarle (la cadena recién arranca, lo resolvió un worker en
    prueba, el trabajo de la ventana es de mineros en cuarentena). Entonces no emite premio y su `reward_cents`
    queda acumulado; el primer bloque con reparto paga lo suyo **más** todo lo acumulado. Por eso la suma no se
    compara contra el `reward_cents` del bloque anterior pelado, sino contra ese más el carry: si no, una cadena
    perfectamente válida daría inválida."""
    rewards = [t for t in block["transactions"] if t["sender"] == REWARD_SENDER]
    if prev_block is None:
        return (["hay premio sin bloque anterior"] if rewards else []), carry
    debido = prev_block.get("reward_cents", 0)
    if not rewards:
        return [], carry + debido  # nadie a quien pagarle: queda para el bloque siguiente
    errors = []
    if any(t.get("reward_for") != prev_block["index"] for t in rewards):
        errors.append("un premio no es del bloque anterior")
    if len({t["receiver"] for t in rewards}) != len(rewards):
        errors.append("una cuenta cobra dos veces el mismo premio")
    pagado = sum(t["amount"] for t in rewards)
    if pagado != debido + carry:
        errors.append(f"los premios suman {pagado} y tendrían que sumar {debido + carry} "
                      f"(reward_cents del bloque anterior {debido} + acumulado {carry})")
    return errors, 0  # se pagó todo lo acumulado


def subsidy(index: int, base_cents: int, halving_blocks: int) -> int:
    """Igual que coordinator/reward.py: block_subsidy (este script es solo stdlib y no lo importa)."""
    if index < 1:
        return 0
    halvings = (index - 1) // halving_blocks
    return base_cents >> halvings if halvings < 64 else 0


def check_economy(block: dict, economy: dict | None) -> list[str]:
    """Bloques sellados desde el 20/11: reward_cents = subsidio (con halving) + comisiones de sus tx."""
    if "subsidy_cents" not in block:
        return []  # bloque de antes del halving: premio fijo
    errors = []
    fees = sum(int(t.get("fee", 0)) for t in block["transactions"] if t["sender"] != REWARD_SENDER)
    if block.get("fees_cents") != fees:
        errors.append("fees_cents no suma las comisiones de las tx")
    if block.get("reward_cents") != block["subsidy_cents"] + fees:
        errors.append("reward_cents no es subsidio + comisiones")
    if economy and block["subsidy_cents"] != subsidy(block["index"], economy["base_reward_cents"], economy["halving_blocks"]):
        errors.append("el subsidio no es el que toca según la altura (halving)")
    return errors


def check_block(block: dict, prev_hash: str, prev_block: dict | None = None, economy: dict | None = None,
                carry: int = 0) -> tuple[list[str], int]:
    """Devuelve los errores del bloque y el carry del premio que queda para el bloque siguiente."""
    reward_errors, carry = check_rewards(block, prev_block, carry)
    errors = reward_errors + check_economy(block, economy)
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
    return errors, carry


def verify(base_url: str) -> int:
    base_url = base_url.rstrip("/")
    height = get(f"{base_url}/chain?limit=1")["height"]
    economy = get(f"{base_url}/status").get("economy")
    prev, prev_block, bad, carry = "0" * 32, None, 0, 0
    for i in range(height):
        block = get(f"{base_url}/blocks/{i}")
        errors, carry = check_block(block, prev, prev_block, economy, carry)
        status = "ok" if not errors else "; ".join(errors)
        pend = f" premio_acumulado={carry}" if carry else ""
        print(f"#{i} {block['block_hash']} prefix={block['prefix']!r} txs={len(block['transactions'])} {status}{pend}")
        bad += bool(errors)
        prev, prev_block = block["block_hash"], block
    print(f"altura={height} bloques_invalidos={bad} premio_sin_pagar={carry}")
    return 1 if bad else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000")
    sys.exit(verify(p.parse_args().url))
