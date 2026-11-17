"""Conexión a Redis y elección de líder con lock (SET NX EX + renovación)."""
import asyncio
import json
import logging

import redis.asyncio as redis
from redis.asyncio.sentinel import Sentinel

from . import keys
from .config import REDIS_HOST, REDIS_MASTER_NAME, REDIS_PASSWORD, REDIS_PORT, REDIS_SENTINELS
from .logs import log

# Renueva el lock solo si sigue siendo nuestro (si no, otro pod ya es líder).
_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


def parse_sentinels(raw: str) -> list[tuple[str, int]]:
    out = []
    for item in raw.split(","):
        item = item.strip()
        if item:
            host, _, port = item.rpartition(":")
            out.append((host, int(port)) if host else (port, 26379))
    return out


def _client() -> redis.Redis:
    sentinels = parse_sentinels(REDIS_SENTINELS)
    if not sentinels:
        return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)
    # El cliente le pregunta a Sentinel quién es el principal y, si hay failover, se reconecta al nuevo
    # (si el viejo contesta READONLY, corta la conexión y vuelve a preguntar).
    sentinel = Sentinel(sentinels, sentinel_kwargs={"password": REDIS_PASSWORD}, socket_timeout=5)
    return sentinel.master_for(REDIS_MASTER_NAME, password=REDIS_PASSWORD, decode_responses=True, socket_timeout=5)


async def connect(logger: logging.Logger) -> redis.Redis:
    client = _client()
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
