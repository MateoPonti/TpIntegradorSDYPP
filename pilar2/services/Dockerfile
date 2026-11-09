# Una sola imagen base para los servicios de GKE (coordinator, pool, gateway).
# Se construye desde la RAÍZ del repo:
#   docker build -f pilar2/services/Dockerfile --build-arg SERVICE=coordinator -t coordinator .
FROM python:3.12-slim
ARG SERVICE
WORKDIR /app
COPY pilar2/services/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY pilar2/services/tpi_common ./tpi_common
COPY pilar2/services/${SERVICE} ./${SERVICE}
ENV PYTHONUNBUFFERED=1 LOG_DIR=/tmp/logs SERVICE=${SERVICE}
USER 1000
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn ${SERVICE}.main:app --host 0.0.0.0 --port 8080 --no-access-log"]
