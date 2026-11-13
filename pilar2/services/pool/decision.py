"""Decisiones del pool (P5) como funciones puras, para testearlas sin Redis ni RabbitMQ.

Decidido el 13/11 (decisiones.docx §7):
  * Prefijo automático: el más largo con el que un bloque tarda menos de BLOCK_TARGET_SECONDS con el
    hashrate real de los mineros vivos. Con histéresis: baja apenas se pasa, sube solo con margen.
    DIFFICULTY_MODE=fixed lo deja fijo para las pruebas de carga (prefijos 1 a 8).
  * Tamaño de rango: fragmentación (16^largo x %), con un mínimo según quién mina (GPU o CPU).
  * Mineros CPU: todo o nada. Sin GPU y con trabajo, el máximo; 60 s sin trabajo, 0; con GPU, 0.
  * Hashrate de cada minero: el que entrega de verdad (sus resultados), no el que declara.
"""
import math

from tpi_common.pow import expected_seconds

MIN_RANGE_SIZE = 10_000


def choose_prefix_len(current: int, hashrate: float, target_seconds: float, min_len: int, max_len: int,
                      raise_margin: float = 0.8) -> int:
    """Sin mineros, el mínimo: el primero que llegue lo resuelve enseguida."""
    if hashrate <= 0:
        return min_len
    best = min_len
    for n in range(min_len, max_len + 1):
        if expected_seconds(n, hashrate) <= target_seconds:
            best = n
    current = max(min_len, min(max_len, current))
    if best < current:
        return best  # con el actual ya se pasa del objetivo: bajar enseguida
    if best > current and expected_seconds(best, hashrate) <= target_seconds * raise_margin:
        return best  # subir solo con margen, para no oscilar en el borde
    return current


def range_size(prefix_len: int, fragment_percent: float, min_range: int) -> int:
    """Fragmentación del 1 % al 50 % del trabajo esperado, sin bajar del mínimo del tipo de minero."""
    expected = 16**prefix_len
    return max(MIN_RANGE_SIZE, min_range, math.ceil(expected * fragment_percent / 100))


def desired_cpu_miners(gpu_alive: int, has_work: bool, idle_seconds: float, max_miners: int,
                       idle_limit: float) -> int:
    """No mira la cola de tareas: con las tandas, la cola siempre tiene el tamaño justo para los mineros que ya hay."""
    if gpu_alive > 0:
        return 0
    if has_work or idle_seconds < idle_limit:
        return max_miners
    return 0


def counts_as_alive(miner: dict) -> bool:
    """Cuenta como minero vivo (para el prefijo, el tamaño de rango y si hay GPU) solo si no está en
    cuarentena ni es un zombie (keep-alive sin entregar trabajo)."""
    return not miner["quarantined"] and miner["source"] != "zombie"


def miner_rates(results: list[dict], now: float, window: float) -> tuple[float, float] | None:
    """(velocidad del motor, rendimiento efectivo) con los resultados de la ventana, o None si no hay.

    Motor = hashes / segundos minando. Efectivo = hashes / segundos de punta a punta (red y arranque).
    Solo cuentan las tareas que recorrieron el rango entero ("no encontré"): con un prefijo corto el nonce
    aparece a los pocos miles de hashes y el arranque del motor domina el tiempo, así que esa tarea daría un
    hashrate bajísimo, el prefijo no subiría nunca y el pool quedaría trabado ahí.
    """
    recent = [x for x in results if now - x["ts"] <= window and x.get("hashes") and not x.get("found")]
    if not recent:
        return None
    hashes = sum(x["hashes"] for x in recent)
    engine = sum(x.get("engine_seconds") or x["seconds"] for x in recent)
    total = sum(x["seconds"] for x in recent)
    return hashes / max(engine, 1e-6), hashes / max(total, 1e-6)


def miner_hashrate(results: list[dict], calibration: dict | None, declared: float, expired_ts: float | None,
                   quarantined: bool, now: float, window: float) -> tuple[float, str]:
    """Hashrate que cuenta para el prefijo y de dónde salió.

    Un minero que dejó vencer un préstamo después de su último resultado es un zombie (manda keep-alive
    pero no mina): cuenta 0 hasta que vuelva a entregar algo.
    """
    if quarantined:
        return 0.0, "cuarentena"
    last_result = max((x["ts"] for x in results), default=None)
    if expired_ts is not None and (last_result is None or expired_ts > last_result):
        return 0.0, "zombie"
    rates = miner_rates(results, now, window)
    if rates is not None:
        return rates[0], "motor"
    if calibration:
        return float(calibration["hashrate"]), "calibracion"
    return float(declared or 0), "declarado"
