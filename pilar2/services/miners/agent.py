#!/usr/bin/env python3
"""Agente de minado: el "wrapper" que comparten el minero CPU y el GPU. Solo stdlib (corre en Colab).

Habla solo con el gateway (HTTP, Bearer MINER_TOKEN, modo pull) y llama a un MOTOR que cumple el
contrato CLI del kit del Pilar 1 (--base --prefix --from --to -> una línea JSON):
  * GPU: pilar1/build/miner_gpu          (en la PC con la 1050 Ti, nunca en GCP)
  * CPU: pilar1/cpu/miner_cpu.py         (imagen miner-cpu en el k3s local, nunca en GCP)

Ciclo: keep-alive cada 5 s (hilo aparte) -> GET /tasks/next -> motor por tramos -> entre tramo y
tramo POST /tasks/<id>/renew (410 = abandonar) -> POST /results. Si el motor falla, POST
/tasks/<id>/release (la tarea vuelve a la cola) y sigue con la próxima.

Punto 4 del contrato (20/11):
  * Shares: si la tarea trae `share_prefix`, el motor también junta los nonces cuyo hash empieza con ese
    prefijo más corto y van en el resultado. El premio se reparte según las shares que verifica el coordinator.
  * WebSocket (/ws/miners, otro hilo): el gateway avisa "hay tareas" (sin tarea, espera el aviso en vez de
    preguntar cada 1 s) y "bloque sellado" (corta el motor al instante). Las tareas se siguen pidiendo y
    entregando por HTTP: si el WebSocket se cae, el agente vuelve a preguntar cada 1 s como antes.

    GATEWAY_URL=http://<ip> MINER_TOKEN=... MINER_KIND=gpu MINER_ID=pc-mateo MINER_ACCOUNT=mateo \
      python agent.py -- ../../../pilar1/build/miner_gpu
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import logging.handlers
import os
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
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


MAX_SHARES = 256  # por resultado (lo mismo que acepta el coordinator)


class Abandoned(Exception):
    """El bloque de la tarea se selló mientras minaba (aviso por WebSocket)."""


class Notices(threading.Thread):
    """Cliente WebSocket mínimo (RFC 6455, solo stdlib) para los avisos del gateway. Solo recibe: texto,
    ping (contesta pong) y close. Si se corta, reintenta cada 5 s."""

    def __init__(self, url: str, token: str, miner: "Miner"):
        super().__init__(daemon=True)
        self.url, self.token, self.miner = url, token, miner
        self.connected = False

    def run(self) -> None:
        while not self.miner.stop.is_set():
            try:
                self.session()
            except (OSError, ValueError, ConnectionError) as exc:
                if self.connected:
                    log.warning("websocket cortado: %s (vuelvo a preguntar cada %.0f s)", exc, self.miner.idle)
            self.connected = False
            self.miner.stop.wait(5)

    def session(self) -> None:
        u = urllib.parse.urlsplit(self.url)
        secure = u.scheme == "https"
        sock = socket.create_connection((u.hostname, u.port or (443 if secure else 80)), timeout=10)
        if secure:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        query = urllib.parse.urlencode({"miner_id": self.miner.id, "kind": self.miner.kind})
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET {u.path.rstrip('/')}/ws/miners?{query} HTTP/1.1\r\nHost: {u.netloc}\r\n"
                      f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                      f"Sec-WebSocket-Version: 13\r\nAuthorization: Bearer {self.token}\r\n\r\n").encode())
        f = sock.makefile("rb")
        status = f.readline()
        if b" 101 " not in status:
            raise ValueError(f"el gateway no aceptó el websocket: {status.decode(errors='replace').strip()}")
        while f.readline() not in (b"\r\n", b""):
            pass
        sock.settimeout(90)  # uvicorn manda ping cada 20 s: si no llega nada en 90 s, la conexión murió
        self.connected = True
        log.info("websocket conectado: espero avisos del gateway")
        try:
            while True:
                op, data = self.frame(f)
                if op == 1:
                    self.miner.on_notice(json.loads(data))
                elif op == 9:
                    self.send(sock, 10, data)  # pong
                elif op == 8:
                    return
        finally:
            sock.close()

    @staticmethod
    def frame(f) -> tuple[int, bytes]:
        head = f.read(2)
        if len(head) < 2:
            raise ConnectionError("conexión cerrada")
        op, n = head[0] & 0x0F, head[1] & 0x7F
        if n == 126:
            n = struct.unpack(">H", f.read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", f.read(8))[0]
        mask = f.read(4) if head[1] & 0x80 else None
        data = f.read(n)
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return op, data

    @staticmethod
    def send(sock, op: int, data: bytes) -> None:
        """Del cliente al servidor los frames van enmascarados (RFC 6455, 5.3)."""
        mask = os.urandom(4)
        sock.sendall(bytes([0x80 | op, 0x80 | len(data)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


class Miner:
    def __init__(self, gw: Gateway, cmd: list[str], miner_id: str, kind: str, chunk: int, keepalive: float,
                 idle: float = 1.0, idle_ws: float = 15.0):
        self.gw, self.cmd, self.id, self.kind, self.chunk, self.keepalive = gw, cmd, miner_id, kind, chunk, keepalive
        self.idle, self.idle_ws = idle, idle_ws
        self.hashrate = 0.0
        # Cuenta de cobro del premio: todos los workers de una persona pueden cobrar en la misma.
        self.account = os.getenv("MINER_ACCOUNT") or None
        self.stop = threading.Event()
        self.wake = threading.Event()       # aviso "hay tareas"
        self.notices: Notices | None = None
        self.block_id: str | None = None    # bloque de la tarea que está minando
        self.solved: str | None = None      # último block.solved avisado
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def on_notice(self, msg: dict) -> None:
        """Lo llama el hilo del WebSocket."""
        if msg.get("type") == "tareas":
            self.wake.set()
        elif msg.get("type") == "block.solved":
            with self.lock:
                self.solved = msg.get("block_id")
                if self.solved == self.block_id and self.proc is not None:
                    self.proc.kill()  # el bloque ya está sellado: no tiene sentido terminar el tramo

    def run_engine(self, base: str, prefix: str, lo: int, hi: int, share: str = "", max_shares: int = 0) -> dict:
        args = ["--base", base, "--prefix", prefix, "--from", str(lo), "--to", str(hi)]
        if share:
            args += ["--share-prefix", share, "--max-shares", str(max_shares)]
        with self.lock:
            if self.solved is not None and self.solved == self.block_id:
                raise Abandoned()
            self.proc = subprocess.Popen(self.cmd + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = self.proc.communicate()
        with self.lock:
            code, self.proc = self.proc.returncode, None
            if self.solved is not None and self.solved == self.block_id:
                raise Abandoned()
        if code != 0:
            raise RuntimeError(f"motor salió con {code}: {err.strip()[:300]}")
        return json.loads([ln for ln in out.splitlines() if ln.strip()][-1])

    def heartbeat_loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.gw.call("POST", "/keepalive", {"miner_id": self.id, "type": self.kind, "hashrate": self.hashrate,
                                                    "account": self.account})
            except OSError as exc:
                log.warning("keep-alive falló: %s", exc)
            self.stop.wait(self.keepalive)

    def mine(self, task: dict) -> str:
        """Recorre [from, to] por tramos. Devuelve found / exhausted / abandoned."""
        lo, hi = int(task["from"]), int(task["to"])
        hashes, engine_seconds, t0 = 0, 0.0, time.perf_counter()
        share = task.get("share_prefix") or ""
        shares: list[int] = []
        result = {"task_id": task["task_id"], "block_id": task["block_id"], "worker": self.id,
                  "from": lo, "to": hi, "found": False}
        self.block_id = task["block_id"]
        while lo <= hi:
            end = min(lo + self.chunk - 1, hi)
            try:
                out = self.run_engine(task["base"], task["prefix"], lo, end, share if len(shares) < MAX_SHARES else "",
                                      MAX_SHARES - len(shares))
            except Abandoned:
                self.gw.call("POST", f"/tasks/{task['task_id']}/renew")  # 410: el gateway borra el préstamo
                return "abandoned"
            shares += out.get("shares") or []
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
        if share:
            result["shares"] = shares
        status, _ = self.gw.call("POST", "/results", result)
        log.info("tarea %s rango %d-%d found=%s shares=%d -> %s (%.0f H/s)", task["task_id"][:8],
                 int(task["from"]), hi, result["found"], len(shares), status, self.hashrate)
        return "found" if result["found"] else "exhausted"

    def wait_idle(self) -> None:
        """Sin tarea: con el WebSocket conectado espera el aviso (y pregunta igual cada idle_ws por si se perdió
        uno); sin WebSocket, pregunta cada `idle` como antes."""
        self.wake.wait(self.idle_ws if self.notices and self.notices.connected else self.idle)
        self.wake.clear()

    def run(self, websocket: bool = True) -> None:
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        if websocket:
            self.notices = Notices(self.gw.url, self.gw.token, self)
            self.notices.start()
        while True:
            try:
                status, task = self.gw.call("GET", f"/tasks/next?miner_id={self.id}&kind={self.kind}")
            except OSError as exc:
                log.warning("gateway inaccesible: %s", exc)
                time.sleep(self.idle)
                continue
            if status != 200:
                if status in (401, 403):
                    log.error("token rechazado por el gateway")
                self.wait_idle()
                continue
            try:
                outcome = self.mine(task)
            except Exception as exc:  # noqa: BLE001 - motor roto, JSON raro, red caída a mitad de la tarea
                # En vez de morir y dejar la tarea colgada 60 s, la devuelve: vuelve a la cola al instante.
                # El gateway lo cuenta como falla (a las 3 seguidas, cuarentena).
                log.error("tarea %s falló: %s", task["task_id"][:8], exc)
                try:
                    self.gw.call("POST", f"/tasks/{task['task_id']}/release")
                except OSError:
                    pass  # si tampoco llega al gateway, el préstamo vence solo
                time.sleep(max(self.idle, 2))
                continue
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
    p.add_argument("--idle-ws", type=float, default=float(os.getenv("IDLE_WS_SECONDS", "15")),
                   help="con el WebSocket conectado, cada cuánto pregunta igual si no llega un aviso")
    p.add_argument("--no-websocket", action="store_true", default=os.getenv("MINER_WEBSOCKET", "1") == "0")
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
    log.info("minero %s (%s) contra %s, cobra en %s, motor=%s", miner_id, kind, gw.url,
             os.getenv("MINER_ACCOUNT") or f"miner:{miner_id}", " ".join(cmd))
    Miner(gw, cmd, miner_id, kind, a.chunk, a.keepalive, a.idle, a.idle_ws).run(not a.no_websocket)
    return 0


if __name__ == "__main__":
    sys.exit(main())
