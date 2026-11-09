"""Rutas que tienen todos los servicios: /health (probes), /metrics (Prometheus) y /logs (memoria)."""
from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import logs


def install_common_routes(app: FastAPI) -> None:
    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/logs")
    async def recent_logs(n: int = 200):
        return logs.recent(min(n, 1000))
