#!/usr/bin/env python3
"""Agente de minado: el "wrapper" que comparten el minero CPU y el GPU. Solo stdlib (corre en Colab).

Habla solo con el gateway (HTTP, Bearer MINER_TOKEN, modo pull) y llama a un MOTOR que cumple el
contrato CLI del kit del Pilar 1 (--base --prefix --from --to -> una línea JSON):
  * GPU: pilar1/build/miner_gpu          (en la PC con la 1050 Ti, nunca en GCP)
  * CPU: pilar1/cpu/miner_cpu.py         (imagen miner-cpu en el k3s local, nunca en GCP)

Ciclo: keep-alive cada 5 s (hilo aparte) -> GET /tasks/next -> motor por tramos -> entre tramo y
tramo POST /tasks/<id>/renew (410 = abandonar) -> POST /results.

    GATEWAY_URL=http://<ip> MINER_TOKEN=... MINER_KIND=gpu MINER_ID=pc-mateo \
      python agent.py -- ../../../pilar1/build/miner_gpu
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("miner")


class Gateway:
    def __init__(self, url: str, token: str, timeout: float = 10.0):
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            return exc.code, {}


def run_engine(cmd: list[str], base: str, prefix: str, lo: int, hi: int) -> dict:
    args = ["--base", base, "--prefix", prefix, "--from", str(lo), "--to", str(hi)]
    proc = subprocess.run(cmd + args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"motor salió con {proc.returncode}: {proc.stderr.strip()[:300]}")
    return json.loads([ln for ln in proc.stdout.splitlines() if ln.strip()][-1])


class Miner:
    def __init__(self, gw: Gateway, cmd: list[str], miner_id: str, kind: str, chunk: int, keepalive: float):
        self.gw, self.cmd, self.id, self.kind, self.chunk, self.keepalive = gw, cmd, miner_id, kind, chunk, keepalive
        self.hashrate = 0.0
        self.stop = threading.Event()

    def heartbeat_loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.gw.call("POST", "/keepalive", {"miner_id": self.id, "type": self.kind, "hashrate": self.hashrate})
            except OSError as exc:
                log.warning("keep-alive falló: %s", exc)
            self.stop.wait(self.keepalive)

    def mine(self, task: dict) -> str:
        """Recorre [from, to] por tramos. Devuelve found / exhausted / abandoned."""
        lo, hi = int(task["from"]), int(task["to"])
        hashes, engine_seconds, t0 = 0, 0.0, time.perf_counter()
        result = {"task_id": task["task_id"], "block_id": task["block_id"], "worker": self.id,
                  "from": lo, "to": hi, "found": False}
        while lo <= hi:
            end = min(lo + self.chunk - 1, hi)
            out = run_engine(self.cmd, task["base"], task["prefix"], lo, end)
            hashes += int(out["hashes"])
            engine_seconds += float(out.get("seconds") or 0)  # solo minando: sin arrancar el proceso ni la red
            self.hashrate = hashes / max(time.perf_counter() - t0, 1e-6)
            if out["found"]:
                result.update(found=True, nonce=out["nonce"], hash=out["hash"])
                break
            lo = end + 1
            if lo <= hi:
                status, _ = self.gw.call("POST", f"/tasks/{task['task_id']}/renew")
                if status == 410:
                    return "abandoned"
        result.update(hashes=hashes, seconds=round(time.perf_counter() - t0, 3),
                      engine_seconds=round(engine_seconds, 6))
        status, _ = self.gw.call("POST", "/results", result)
        log.info("tarea %s rango %d-%d found=%s -> %s (%.0f H/s)", task["task_id"][:8], int(task["from"]),
                 hi, result["found"], status, self.hashrate)
        return "found" if result["found"] else "exhausted"

    def run(self, idle: float) -> None:
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        while True:
            try:
                status, task = self.gw.call("GET", f"/tasks/next?miner_id={self.id}&kind={self.kind}")
            except OSError as exc:
                log.warning("gateway inaccesible: %s", exc)
                time.sleep(idle)
                continue
            if status != 200:
                if status in (401, 403):
                    log.error("token rechazado por el gateway")
                time.sleep(idle)
                continue
            outcome = self.mine(task)
            if outcome == "abandoned":
                log.info("tarea %s abandonada: el bloque ya se cerró", task["task_id"][:8])


def setup_logs(miner_id: str) -> None:
    fmt = logging.Formatter(f"%(asctime)s %(levelname)s [{miner_id}] %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    log_dir = os.getenv("LOG_DIR", "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, f"miner-{miner_id}.log"), maxBytes=5_000_000, backupCount=3, encoding="utf-8"))
    except OSError:
        pass
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--chunk", type=int, default=int(os.getenv("MINER_CHUNK", "5000000")),
                   help="nonces por llamada al motor (duda 8: se elige midiendo)")
    p.add_argument("--keepalive", type=float, default=float(os.getenv("KEEPALIVE_SECONDS", "5")))
    p.add_argument("--idle", type=float, default=float(os.getenv("IDLE_SECONDS", "1")))
    p.add_argument("cmd", nargs=argparse.REMAINDER, help="comando del motor, después de --")
    a = p.parse_args()
    cmd = [c for c in a.cmd if c != "--"] or os.getenv("MINER_CMD", "").split()
    if not cmd:
        p.error("falta el comando del motor (después de -- o en MINER_CMD)")
    kind = os.getenv("MINER_KIND", "cpu")
    if kind not in ("cpu", "gpu"):
        p.error("MINER_KIND tiene que ser cpu o gpu")
    miner_id = os.getenv("MINER_ID") or f"{kind}-{socket.gethostname()}"
    setup_logs(miner_id)
    gw = Gateway(os.environ["GATEWAY_URL"], os.environ["MINER_TOKEN"])
    log.info("minero %s (%s) contra %s, motor=%s", miner_id, kind, gw.url, " ".join(cmd))
    Miner(gw, cmd, miner_id, kind, a.chunk, a.keepalive).run(a.idle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
