"""Topología de RabbitMQ (decisiones.docx §4). La declaran todos los servicios al arrancar (es idempotente)."""
import asyncio
import json
import logging

import aio_pika

from .config import RABBITMQ_URL
from .logs import log

EXCHANGE = "blockchain"          # topic: tareas, resultados y eventos
DLX = "blockchain.dlx"           # fanout: lo que agota los reintentos
TASKS_QUEUE = "mining.tasks"     # quorum, task.#
RESULTS_QUEUE = "mining.results"  # quorum, result.#
DLQ = "mining.dlq"


async def connect(logger: logging.Logger) -> aio_pika.abc.AbstractRobustConnection:
    while True:
        try:
            return await aio_pika.connect_robust(RABBITMQ_URL)
        except (OSError, aio_pika.exceptions.AMQPConnectionError) as exc:
            log(logger, "rabbitmq no responde, reintento en 3 s", error=str(exc))
            await asyncio.sleep(3)


async def declare_topology(channel: aio_pika.abc.AbstractChannel):
    exchange = await channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
    dlx = await channel.declare_exchange(DLX, aio_pika.ExchangeType.FANOUT, durable=True)
    dlq = await channel.declare_queue(DLQ, durable=True, arguments={"x-queue-type": "quorum"})
    await dlq.bind(dlx)
    quorum = {"x-queue-type": "quorum", "x-dead-letter-exchange": DLX}
    tasks = await channel.declare_queue(TASKS_QUEUE, durable=True, arguments=quorum)
    await tasks.bind(exchange, routing_key="task.#")
    results = await channel.declare_queue(RESULTS_QUEUE, durable=True, arguments=quorum)
    await results.bind(exchange, routing_key="result.#")
    return exchange, dlx, tasks, results


async def declare_events_queue(channel: aio_pika.abc.AbstractChannel, exchange, pod: str):
    """block.events.<pod>: classic, exclusiva y auto-delete. Cada réplica recibe todos los block.*."""
    queue = await channel.declare_queue(f"block.events.{pod}", exclusive=True, auto_delete=True)
    await queue.bind(exchange, routing_key="block.*")
    return queue


def message(payload: dict) -> aio_pika.Message:
    return aio_pika.Message(
        body=json.dumps(payload).encode(),
        content_type="application/json",
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
    )


async def queue_length(channel: aio_pika.abc.AbstractChannel, name: str) -> int:
    """Mensajes listos en la cola (no cuenta los prestados por el gateway)."""
    q = await channel.declare_queue(name, passive=True)
    return q.declaration_result.message_count or 0
