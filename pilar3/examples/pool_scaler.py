"""Reference logic for the transaction pool reaction to GPU miners (P5 + section 3.3).

Not a full pool: only the decision ("which prefix and how many CPU miners")
and the scaling signal the pool publishes. Copy what you need into services/pool.

TPI Google Cloud policy: no miner (CPU or GPU) runs in Google Cloud. The pool
lives in GKE but does not create machines: it publishes the signal in Redis,
the gateway serves it at GET /scaling, and the CPU miners of the group k3s
scale from it (KEDA metrics-api or examples/local_autoscaler.py).

The decision functions are pure (no network calls) so they can be unit tested.
"""

from __future__ import annotations

import json
import math

SIGNAL_KEY = "pool:scaling"


def effective_prefix(configured: str, gpu_alive: int, min_without_gpu: int) -> str:
    """Difficulty to publish: the configured prefix, or a shorter one with no GPU alive."""
    if gpu_alive > 0:
        return configured
    return configured[: max(1, min(len(configured), min_without_gpu))]


def desired_cpu_miners(
    gpu_alive: int,
    pending_tasks: int,
    current: int,
    max_miners: int,
    tasks_per_miner: int = 20,
) -> int:
    """How many CPU miners the group k3s should run.

    * GPU miners alive: CPU miners are not needed, scale to zero.
    * No GPU and work pending: one miner per `tasks_per_miner` pending tasks, at most max_miners.
    * No GPU and nothing pending: keep the current count (avoid flapping between blocks).
    """
    if gpu_alive > 0:
        return 0
    if pending_tasks <= 0:
        return min(current, max_miners)
    return max(1, min(max_miners, math.ceil(pending_tasks / tasks_per_miner)))


def scaling_signal(
    configured_prefix: str,
    gpu_alive: int,
    cpu_alive: int,
    pending_tasks: int,
    max_miners: int,
    min_without_gpu: int,
    tasks_per_miner: int = 20,
) -> dict:
    """Document the gateway returns at GET /scaling (KEDA reads desired_cpu_miners)."""
    return {
        "gpu_alive": gpu_alive,
        "cpu_alive": cpu_alive,
        "pending_tasks": pending_tasks,
        "prefix": effective_prefix(configured_prefix, gpu_alive, min_without_gpu),
        "desired_cpu_miners": desired_cpu_miners(
            gpu_alive, pending_tasks, cpu_alive, max_miners, tasks_per_miner
        ),
    }


def publish_signal(redis_client, signal: dict, ttl_seconds: int = 60) -> None:
    """Store the signal in Redis for the gateway. The TTL makes a dead pool visible:
    if nobody refreshes the key, the gateway can answer 503 instead of stale data."""
    redis_client.set(SIGNAL_KEY, json.dumps(signal), ex=ttl_seconds)
