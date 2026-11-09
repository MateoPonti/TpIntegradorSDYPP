# TP Integrador · Blockchain distribuida con minero CUDA

Sistemas Distribuidos y Programación Paralela · UNLu · Mercedes 2C 2026.
Grupo: Mateo Ponti, María Agustina Ortiz, Federico Kasparian, Justino Bernal.
Entrega y defensa: **viernes 04/12/2026**.

| Carpeta | Pilar | Qué hay |
|---|---|---|
| [`pilar1/`](pilar1/) | 1 · Minero CUDA | Kit de la cátedra + `gpu/miner_gpu.cu` (a completar) + minero CPU multiproceso con el mismo contrato |
| [`pilar2/`](pilar2/) | 2 · Sistema distribuido | coordinator, pool, gateway, mineros (agente compartido) + `docker compose` local |
| [`pilar3/`](pilar3/) | 3 · GKE y escalado | OpenTofu, manifiestos de GKE y del k3s local (KEDA), pruebas de carga |
| [`docs/`](docs/) | — | Decisiones, componentes, dudas y diagramas del grupo |
| `.github/workflows/` | — | CI (gitleaks + tests), Pilar 1 (nvcc), Pipelines 1 a 4 |

```
                      Internet
     usuarios ──► LB coordinator          LB gateway ◄── mineros (pull + token)
+──────────────────── GKE (GCP) ─────────────────────+      │
│ infra: RabbitMQ, Redis(AOF)                         │      ├─ k3s local: miner-cpu x0..N (KEDA)
│ apps:  coordinator x2, pool x2, gateway x2          │      └─ PC: wrapper + miner_gpu (GTX 1050 Ti)
│ SIN mineros (la prueba gratuita prohíbe minar)      │
+─────────────────────────────────────────────────────+
```

Reglas para cualquier IA que use el grupo: [`AGENTS.md`](AGENTS.md).

## Arranque rápido

```bash
# Pilar 1 (en la PC con CUDA 12.x)
cd pilar1 && make test && make check-oracle && make ARCH=sm_61 && make check

# Pilar 2 (local)
cd pilar2 && cp .env.example .env && docker compose up -d --build
curl http://127.0.0.1:18080/status
```
