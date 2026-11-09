"""Lectura de variables de entorno. Todo se configura así (ConfigMap tpi-config en GKE)."""
import os
import socket


def env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


# Identidad de esta réplica: en Kubernetes el hostname es el nombre del pod.
POD_NAME = os.getenv("POD_NAME", socket.gethostname())

RABBITMQ_URL = env_str("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
REDIS_HOST = env_str("REDIS_HOST", "localhost")
REDIS_PORT = env_int("REDIS_PORT", 6379)
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None

# Parámetros del ConfigMap que comparten varios servicios (nombres del starter del Pilar 3).
DIFFICULTY_PREFIX = env_str("DIFFICULTY_PREFIX", "00000")
WORKER_TTL_SECONDS = env_int("GPU_KEEPALIVE_TIMEOUT_SECONDS", 15)
