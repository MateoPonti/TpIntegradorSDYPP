"""Decisiones del pool (P5) como funciones puras, para testearlas sin Redis ni RabbitMQ.

Base: examples/pool_scaler.py del starter del Pilar 3.
OJO, duda 4 abierta (docs/dudas.docx): las fórmulas finales (PREFIX_GPU / PREFIX_CPU_ONLY,
rango = hashrate x 5 s, mineros CPU con o sin GPU viva) se deciden midiendo. Por ahora se usan
las perillas del starter, que son las que cambia loadtest/matrix.sh.
"""
import math

MIN_RANGE_SIZE = 10_000


def effective_prefix(configured: str, gpu_alive: int, min_without_gpu: int) -> str:
    """Sin GPU viva se acorta el prefijo (el bloque guarda con qué prefijo se minó)."""
    if gpu_alive > 0:
        return configured
    return configured[: max(1, min(len(configured), min_without_gpu))]


def range_size(prefix: str, fragment_percent: float) -> int:
    """Fragmentación del 1 % al 50 % del trabajo esperado (16^largo hashes en promedio)."""
    expected = 16 ** len(prefix)
    return max(MIN_RANGE_SIZE, math.ceil(expected * fragment_percent / 100))


def desired_cpu_miners(gpu_alive: int, pending_tasks: int, current: int, max_miners: int,
                       tasks_per_miner: int = 20) -> int:
    if gpu_alive > 0:
        return 0  # con GPU no hacen falta mineros CPU (duda 4, opción A)
    if pending_tasks <= 0:
        return min(current, max_miners)  # entre bloques: no oscilar
    return max(1, min(max_miners, math.ceil(pending_tasks / tasks_per_miner)))


def plan(configured_prefix: str, fragment_percent: float, gpu_alive: int, min_without_gpu: int) -> dict:
    prefix = effective_prefix(configured_prefix, gpu_alive, min_without_gpu)
    return {"prefix": prefix, "range_size": range_size(prefix, fragment_percent)}


def scaling_signal(configured_prefix: str, gpu_alive: int, cpu_alive: int, pending_tasks: int,
                   max_miners: int, min_without_gpu: int, tasks_per_miner: int = 20) -> dict:
    """Lo que devuelve GET /scaling del gateway (KEDA lee desired_cpu_miners)."""
    return {
        "gpu_alive": gpu_alive,
        "cpu_alive": cpu_alive,
        "pending_tasks": pending_tasks,
        "prefix": effective_prefix(configured_prefix, gpu_alive, min_without_gpu),
        "desired_cpu_miners": desired_cpu_miners(gpu_alive, pending_tasks, cpu_alive, max_miners, tasks_per_miner),
    }
