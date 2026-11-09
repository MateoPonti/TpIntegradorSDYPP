# Minero CPU para el k3s LOCAL (nunca en GCP). Agente compartido + motor CPU del Pilar 1.
# Se construye desde la RAÍZ del repo:
#   docker build -f pilar2/services/miners/cpu/Dockerfile -t miner-cpu .
# Solo stdlib: no hay requirements. CPU_WORKERS=1 porque el pod tiene limit de 1 CPU:
# se escala con réplicas (KEDA), no con procesos.
FROM python:3.12-slim
WORKDIR /app
COPY pilar2/services/miners/agent.py .
COPY pilar1/cpu/miner_cpu.py ./engine/miner_cpu.py
ENV PYTHONUNBUFFERED=1 LOG_DIR=/tmp/logs MINER_KIND=cpu CPU_WORKERS=1 \
    MINER_CMD="python /app/engine/miner_cpu.py" MINER_CHUNK=2000000
USER 1000
CMD ["python", "agent.py"]
