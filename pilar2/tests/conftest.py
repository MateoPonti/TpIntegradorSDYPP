import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT / "pilar2" / "services", ROOT / "pilar2" / "services" / "miners", ROOT / "pilar2" / "tools",
          ROOT / "pilar1" / "cpu"):
    sys.path.insert(0, str(p))

import os

import pytest

REDIS_URL = os.getenv("REDIS_URL")  # ej. redis://localhost:6379/15 (docker run -p 6379:6379 redis:7.4-alpine)
TEST_DB = 15  # la base que se borra en cada test: nunca la 0, que es la que usan los servicios


@pytest.fixture
def redis_db():
    """Redis real para los tests de los Lua y del reaper (C7). Devuelve una función que crea un cliente asyncio:
    cada test lo crea adentro de su asyncio.run, porque un cliente asyncio queda atado a su event loop.
    Sin REDIS_URL el test se saltea; con una base que no sea la 15, también (para no borrar datos de verdad)."""
    if not REDIS_URL:
        pytest.skip("definir REDIS_URL=redis://localhost:6379/15 con un Redis levantado")
    import redis
    import redis.asyncio as aredis
    sync = redis.Redis.from_url(REDIS_URL)
    if sync.connection_pool.connection_kwargs.get("db") != TEST_DB:
        pytest.skip(f"REDIS_URL tiene que apuntar a la base {TEST_DB}: el test la borra")
    sync.flushdb()
    yield lambda: aredis.Redis.from_url(REDIS_URL, decode_responses=True)
    sync.flushdb()
    sync.close()


def import_gateway():
    """Importa gateway.main en el mismo proceso que el coordinator. Los dos definen la métrica `quarantined_total`
    (en producción son procesos distintos, cada uno con su /metrics), y prometheus_client no deja registrar dos veces
    el mismo nombre en un proceso. Se saca un momento la del coordinator, se importa el gateway y se vuelve a dejar la
    del coordinator registrada (la del gateway funciona igual, solo no queda en el registro global)."""
    import sys
    if "gateway.main" in sys.modules:
        return sys.modules["gateway.main"]
    from prometheus_client import REGISTRY
    clashing = {c for name, c in list(REGISTRY._names_to_collectors.items()) if name.startswith("quarantined")}
    for c in clashing:
        REGISTRY.unregister(c)
    try:
        from gateway import main as gw
    finally:
        if clashing:
            gateway_side = {c for name, c in list(REGISTRY._names_to_collectors.items())
                            if name.startswith("quarantined") and c not in clashing}
            for c in gateway_side:
                REGISTRY.unregister(c)
            for c in clashing:
                REGISTRY.register(c)
    return gw
