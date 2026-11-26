"""Límite de pedidos por IP en ventanas fijas (B1.4, 26/11): corta las granjas de identidades (Sybil) en el alta
de cuentas (coordinator) y de workers (gateway). Un contador en Redis por clave y ventana, atómico en Lua."""

# KEYS: el contador   ARGV: ventana en segundos.   Devuelve cuántos pedidos van en la ventana (este incluido).
# El EXPIRE va solo con el primero: así la ventana es fija y no se estira con cada pedido.
COUNT_LUA = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return n
"""


async def allow(r, key: str, limit: int, window_s: int) -> bool:
    """True si este pedido entra en el límite (`limit` por `window_s` segundos para esa clave)."""
    return int(await r.eval(COUNT_LUA, 1, key, window_s)) <= limit


def client_ip(request) -> str:
    """IP del cliente. Detrás de Cloudflare viene en CF-Connecting-IP. Traefik pone la que ve en X-Forwarded-For
    (y descarta la que mande el cliente, porque no es un proxy de confianza). Sin proxy, la de la conexión."""
    headers = request.headers
    if ip := (headers.get("cf-connecting-ip") or "").strip():
        return ip
    if xff := (headers.get("x-forwarded-for") or "").strip():
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "desconocida"
