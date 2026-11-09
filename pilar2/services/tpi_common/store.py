"""Conexión a Redis y elección de líder con lock (SET NX EX + renovación)."""
import asyncio
import json
import logging

import redis.asyncio as redis

from . import keys
from .config import REDIS_HOST, REDIS_PASSWORD, REDIS_PORT
from .logs import log

# Renueva el lock solo si sigue siendo nuestro (si no, otro pod ya es líder).
_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


async def connect(logger: logging.Logger) -> redis.Redis:
    client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)
    while True:
        try:
            await client.ping()
            return client
        except (OSError, redis.ConnectionError) as exc:
            log(logger, "redis no responde, reintento en 2 s", error=str(exc))
            await asyncio.sleep(2)


class Leader:
    """Una sola réplica hace el trabajo (coord:leader, pool:leader). Si muere, el lock vence y otra lo toma."""

    def __init__(self, r: redis.Redis, key: str, me: str, ttl: int = 10):
        self.r, self.key, self.me, self.ttl = r, key, me, ttl
        self._renew = r.register_script(_RENEW)

    async def check(self) -> bool:
        if await self.r.set(self.key, self.me, nx=True, ex=self.ttl):
            return True
        return bool(await self._renew(keys=[self.key], args=[self.me, self.ttl]))


async def alive_workers(r: redis.Redis) -> list[dict]:
    """Mineros vivos: SCAN worker:* (cada clave vence sola a los 15 s sin keep-alive)."""
    out = []
    async for k in r.scan_iter(match=keys.worker("*"), count=500):
        raw = await r.get(k)
        if raw:
            out.append(json.loads(raw))
    return out
