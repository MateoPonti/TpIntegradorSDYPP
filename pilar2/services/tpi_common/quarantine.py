"""Cuarentena de un minero roto o mentiroso. La usan el gateway (canario fallado, préstamos vencidos,
resultados de un minero en cuarentena) y el coordinator (un "encontré" que no verifica).

Cuarentena = el gateway no le da tareas por un rato (le contesta 204) y su hashrate cuenta 0. Si además hay
prueba de que está roto o miente (`distrust`: canario fallado o "encontré" inválido), su trabajo del bloque
actual no cobra premio y sus rangos "no encontré" del bloque se le dan a otros, porque la solución pudo haber
estado ahí. Si solo se colgó (préstamos vencidos), lo que ya terminó sigue valiendo.
"""
import json
import uuid

from . import keys, rabbit


async def republish(exchange, cur: dict, lo: int, hi: int) -> None:
    task = {"v": 1, "task_id": uuid.uuid4().hex, "block_id": cur["block_id"], "base": cur["base"],
            "prefix": cur["prefix"], "from": int(lo), "to": int(hi), "attempts": 0}
    await exchange.publish(rabbit.message(task), routing_key="task.any")


async def penalize(r, exchange, cur: dict | None, miner_id: str, reason: str, seconds: int,
                   extra: list[tuple[int, int]] = (), distrust: bool = True) -> int:
    """Pone al minero en cuarentena y re-publica `extra` y, con `distrust`, sus rangos "no encontré" del
    bloque actual. Devuelve cuántos rangos re-publicó."""
    await r.set(keys.quarantine(miner_id), reason, ex=seconds)
    if cur is None:
        return 0
    ranges = list(extra)
    if distrust:
        await r.hdel(keys.block_shares(cur["index"]), miner_id)  # su trabajo de este bloque no cobra
        key = keys.block_notfound(cur["block_id"], miner_id)
        ranges += [(int(x["from"]), int(x["to"])) for x in map(json.loads, await r.lrange(key, 0, -1))]
        await r.delete(key)
    for lo, hi in ranges:
        await republish(exchange, cur, lo, hi)
    return len(ranges)
