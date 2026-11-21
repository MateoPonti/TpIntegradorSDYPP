# Minero GPU como contenedor, para correrlo en el namespace de InferHive (GPUs de la cátedra), nunca en GCP.
# Agente compartido + kernel CUDA del Pilar 1. Se construye desde la RAÍZ del repo:
#   docker build -f pilar2/services/miners/gpu/Dockerfile -t miner-gpu .
# Se compila para la 1050 Ti del grupo (sm_61) y las GPUs que alquila InferHive: GTX 1650 SUPER (sm_75),
# RTX 3090 (sm_86) y RTX 4060 (sm_89), más PTX para las que vengan. CUDA 12.x: la 13 no soporta Pascal.
FROM nvidia/cuda:12.6.3-devel-ubuntu22.04 AS build
WORKDIR /src
COPY pilar1/gpu/miner_gpu.cu .
RUN nvcc -O3 \
      -gencode arch=compute_61,code=sm_61 -gencode arch=compute_75,code=sm_75 \
      -gencode arch=compute_86,code=sm_86 -gencode arch=compute_89,code=sm_89 \
      -gencode arch=compute_89,code=compute_89 \
      -o miner_gpu miner_gpu.cu

# Solo el runtime: el binario trae cudart estático y el driver lo pone el nodo.
FROM nvidia/cuda:12.6.3-base-ubuntu22.04
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /src/miner_gpu /app/miner_gpu
COPY pilar2/services/miners/agent.py .
# Solo stdlib. MINER_CHUNK de 1.000 M como en run.ps1 (~0,9 s por llamada a 1,1 GH/s).
ENV PYTHONUNBUFFERED=1 LOG_DIR=/tmp/logs MINER_KIND=gpu MINER_CHUNK=1000000000
USER 1000
CMD ["python3", "agent.py", "--", "/app/miner_gpu"]
