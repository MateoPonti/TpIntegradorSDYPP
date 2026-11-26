"""Hashrate medido con shares y Vardiff (26/11, pedidos B1.2 y A3-d del profe).

* Hashrate medido: sale de las shares que verificó el gateway con MD5 (cada una cuesta 16^largo hashes en
  promedio) y de los segundos que midió él (desde que prestó la tarea hasta que llegó el resultado). Lo que el minero
  informa (hashes, engine_seconds, el hashrate del keep-alive o de la calibración) ya no decide nada: se puede mentir.
* Vardiff (como en Stratum): cada worker recibe shares de una dificultad acorde a su hashrate, para que mande
  ~VARDIFF_SHARES_PER_SECOND por segundo. Uno rápido recibe shares más difíciles (menos MD5 que verificar) y uno
  lento más fáciles (su trabajo se mide igual de bien). El peso de cada share es 16^largo, así que el premio no cambia.
  Límites por tarea: al menos MIN_SHARES_PER_TASK esperadas (si no, los canarios y la medición se quedan sin datos) y
  a lo sumo MAX_SHARES (lo que acepta un resultado; más se cortarían y se perdería trabajo).
"""
import math

from .config import env_float, env_int
from .pow import MAX_SHARES

VARDIFF_SHARES_PER_SECOND = env_float("VARDIFF_SHARES_PER_SECOND", 10.0)
MIN_SHARES_PER_TASK = env_int("VARDIFF_MIN_SHARES_PER_TASK", 8)
MIN_SHARES_TO_MEASURE = env_int("MIN_SHARES_TO_MEASURE", 20)  # menos es ruido (con 20, ~22 % de error típico)


def shares_rate(results: list[dict], now: float, window: float) -> float | None:
    """Hashrate medido: Σ shares válidas · 16^largo / Σ segundos medidos por el gateway, con los resultados de la
    ventana que recorrieron el rango entero (un "encontré" corta antes y el arranque del motor pesaría de más).
    None si no hay suficientes shares para que la medición signifique algo."""
    recent = [x for x in results if now - x["ts"] <= window and "valid_shares" in x and not x.get("found")]
    shares = sum(int(x["valid_shares"]) for x in recent)
    if shares < MIN_SHARES_TO_MEASURE:
        return None
    work = sum(int(x["valid_shares"]) * 16 ** int(x["share_len"]) for x in recent)
    return work / max(sum(float(x["seconds"]) for x in recent), 1e-6)


def share_bounds(block_len: int, range_size: int) -> tuple[int, int]:
    """Largos de share permitidos para una tarea de `range_size` nonces: entre el que da MAX_SHARES esperadas (más
    corto truncaría) y el que da MIN_SHARES_PER_TASK (más largo dejaría tareas sin shares). Nunca más largo que el
    prefijo del bloque (toda solución del bloque también es una share)."""
    lo = math.ceil(math.log(max(range_size / MAX_SHARES, 1), 16))
    hi = math.floor(math.log(max(range_size / MIN_SHARES_PER_TASK, 1), 16))
    lo = max(1, min(lo, block_len))
    return lo, max(lo, min(hi, block_len))


def share_len(hashrate: float, block_len: int, range_size: int,
              per_second: float = VARDIFF_SHARES_PER_SECOND) -> int:
    """Largo de share para un worker con ese hashrate (Vardiff): ~`per_second` shares por segundo, dentro de los
    límites de la tarea."""
    lo, hi = share_bounds(block_len, range_size)
    wanted = round(math.log(max(hashrate / per_second, 1), 16))
    return min(max(wanted, lo), hi)


def classify(hashrate: float) -> str:
    """Clase del minero (para /miners y Grafana), con el hashrate medido."""
    if hashrate >= 100e6:
        return "rapido"
    if hashrate >= 5e6:
        return "medio"
    return "lento"
