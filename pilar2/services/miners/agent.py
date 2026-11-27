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
  * WebSocket (/ws/miners, otro hilo): el gateway avisa "bloque sellado" (corta el motor al instante).

Desde el 26/11 (A3-c y A3-f del contrato, como Stratum):
  * authorize: el worker tiene una clave Ed25519 propia (MINER_KEY_FILE; si no existe se crea) y firma el desafío
    que le manda el gateway en cada conexión. La primera vez la clave queda atada al worker.
  * mining.notify: con el WebSocket autorizado, el gateway **empuja** las tareas; el agente ya no pide. Si el
    WebSocket se cae, vuelve a pedir por HTTP (GET /tasks/next) como antes.
  * Resultados firmados: cada POST /results lleva la firma de la tarea, el nonce y sus shares.
  * Ed25519 con solo stdlib (este agente corre en Colab sin instalar nada): el código de referencia del RFC 8032.

Desde el 27/11 (C5): con SIGTERM (KEDA bajando las réplicas, `kubectl scale`, rolling update) el agente
termina el tramo que está minando, devuelve lo que tenga prestado con `release?reason=shutdown` —que el
gateway no cuenta como falla— y sale con 0. Sin eso, el bloque esperaba a que vencieran los préstamos.

    GATEWAY_URL=http://<ip> MINER_TOKEN=... MINER_KIND=gpu MINER_ID=pc-mateo MINER_ACCOUNT=mateo \
      python agent.py -- ../../../pilar1/build/miner_gpu
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import logging.handlers
import os
import queue
import signal
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


# ---- Ed25519 (RFC 8032, sección 6: código de referencia en Python, solo para firmar) ----
# Fuente: https://www.rfc-editor.org/rfc/rfc8032#section-6 . Se usa solo stdlib porque el agente corre en Colab y en
# una imagen sin dependencias. No es de tiempo constante (no importa acá: firma unas pocas veces por tarea y la clave
# solo identifica al worker). test_push.py verifica que da exactamente lo mismo que la librería cryptography.
_P = 2**255 - 19
_Q = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P


def _add(a, b):
    x = (a[1] - a[0]) * (b[1] - b[0]) % _P
    y = (a[1] + a[0]) * (b[1] + b[0]) % _P
    c, d = 2 * a[3] * b[3] * _D % _P, 2 * a[2] * b[2] % _P
    e, f, g, h = y - x, d - c, d + c, y + x
    return e * f, g * h, f * g, e * h


def _mul(s, pt):
    acc = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            acc = _add(acc, pt)
        pt = _add(pt, pt)
        s >>= 1
    return acc


def _recover_x(y, sign):
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * pow(2, (_P - 1) // 4, _P) % _P
    return _P - x if (x & 1) != sign else x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _compress(pt) -> bytes:
    zinv = pow(pt[2], _P - 2, _P)
    x, y = pt[0] * zinv % _P, pt[1] * zinv % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _expand(seed: bytes):
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def ed25519_public(seed: bytes) -> bytes:
    return _compress(_mul(_expand(seed)[0], _G))


def ed25519_sign(seed: bytes, msg: bytes) -> bytes:
    a, prefix = _expand(seed)
    pub = _compress(_mul(a, _G))
    r = int.from_bytes(hashlib.sha512(prefix + msg).digest(), "little") % _Q
    big_r = _compress(_mul(r, _G))
    h = int.from_bytes(hashlib.sha512(big_r + pub + msg).digest(), "little") % _Q
    return big_r + int.to_bytes((r + h * a) % _Q, 32, "little")


def shares_digest(shares) -> str:
    """Igual que tpi_common.firmas.shares_digest (el agente no importa nada del servidor)."""
    return hashlib.sha256(",".join(str(int(n)) for n in sorted(shares or [])).encode()).hexdigest()


def sign_result(seed: bytes, chain_id: int, res: dict) -> str:
    """Firma de un resultado (A3-f): sdmer:<chain>:result:<tarea>:<1|0>:<nonce>:<sha256 de las shares>."""
    nonce = "" if not res.get("found") else int(res["nonce"])
    text = f"result:{res['task_id']}:{1 if res.get('found') else 0}:{nonce}:{shares_digest(res.get('shares'))}"
    return ed25519_sign(seed, f"sdmer:{chain_id}:{text}".encode()).hex()


def load_key(miner_id: str) -> bytes:
    """Semilla Ed25519 del worker (32 bytes, hex en un archivo). MINER_KEY_FILE o ~/.sdmer/<miner_id>.key (si el home
    no se puede escribir, como en el pod, /tmp). Si no existe, se crea. Perderla obliga a dar de alta otro worker:
    el gateway la ata al worker en el primer authorize."""
    path = os.getenv("MINER_KEY_FILE")
    if not path:
        home = os.path.expanduser("~")
        base = os.path.join(home, ".sdmer") if os.access(home, os.W_OK) else os.path.join("/tmp", "sdmer")
        path = os.path.join(base, f"{miner_id}.key")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return bytes.fromhex(fh.read().strip())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    seed = os.urandom(32)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(seed.hex())
    log.info("clave nueva del worker en %s (no perderla: queda atada al worker)", path)
    return seed


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
        self.authorized = False  # authorize aceptado: las tareas llegan empujadas (mining.notify)

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
        try:
            while True:
                op, data = self.frame(f)
                if op == 1:
                    msg = json.loads(data)
                    if msg.get("method") == "mining.challenge":  # A3-f: firmar el desafío de esta conexión
                        self.miner.chain_id = int(msg["params"].get("chain_id", self.miner.chain_id))
                        text = f"sdmer:{self.miner.chain_id}:authorize:{self.miner.id}:{msg['params']['challenge']}"
                        self.send(sock, 1, json.dumps({"jsonrpc": "2.0", "id": 1, "method": "mining.authorize", "params": {
                            "worker": self.miner.id, "pubkey": ed25519_public(self.miner.key).hex(),
                            "signature": ed25519_sign(self.miner.key, text.encode()).hex()}}).encode())
                    elif msg.get("id") == 1 and "error" in msg:
                        log.error("authorize rechazado: %s", msg["error"].get("message"))
                        return
                    elif msg.get("id") == 1:
                        self.authorized = True
                        log.info("websocket autorizado: el gateway me empuja las tareas")
                    elif msg.get("method") == "mining.notify":
                        self.miner.pushed.put(msg["params"])  # A3-c: tarea empujada
                    else:
                        self.miner.on_notice(msg)
                elif op == 9:
                    self.send(sock, 10, data)  # pong
                elif op == 8:
                    return
        finally:
            self.authorized = False
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
        """Del cliente al servidor los frames van enmascarados (RFC 6455, 5.3). Largo de 7, 16 o 64 bits (5.2)."""
        mask, n = os.urandom(4), len(data)
        if n < 126:
            head = bytes([0x80 | op, 0x80 | n])
        elif n < 1 << 16:
            head = bytes([0x80 | op, 0x80 | 126]) + struct.pack(">H", n)
        else:
            head = bytes([0x80 | op, 0x80 | 127]) + struct.pack(">Q", n)
        sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


class Miner:
    def __init__(self, gw: Gateway, cmd: list[str], miner_id: str, kind: str, chunk: int, keepalive: float,
                 idle: float = 1.0, idle_ws: float = 15.0):
        self.gw, self.cmd, self.id, self.kind, self.chunk, self.keepalive = gw, cmd, miner_id, kind, chunk, keepalive
        self.idle, self.idle_ws = idle, idle_ws
        self.hashrate = 0.0
        # Cuenta de cobro del premio: todos los workers de una persona pueden cobrar en la misma.
        self.account = os.getenv("MINER_ACCOUNT") or None
        self.stop = threading.Event()
        self.stopping = False               # C5: llegó SIGTERM, hay que salir ordenado
        self.wake = threading.Event()       # aviso "hay tareas"
        self.notices: Notices | None = None
        self.block_id: str | None = None    # bloque de la tarea que está minando
        self.task_id: str | None = None     # tarea prestada que está minando (C5: hay que devolverla)
        self.solved: str | None = None      # último block.solved avisado
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.key = load_key(miner_id)        # Ed25519 del worker (A3-f)
        self.chain_id = 2026                 # lo confirma el gateway en el desafío
        self.pushed: queue.Queue = queue.Queue()  # tareas empujadas por el WebSocket (A3-c)

    def handle_signal(self, signum, frame) -> None:  # noqa: ARG002 - firma que pide signal.signal
        """SIGTERM o SIGINT (C5). Kubernetes manda SIGTERM al bajar las réplicas (KEDA a 0, `scale`,
        rolling update). Acá solo se marca la salida: el handler no hace red ni mata el motor, porque
        corre en medio de cualquier instrucción. El loop termina el tramo que está minando, devuelve
        los préstamos abiertos con reason=shutdown y sale con 0.

        Por eso MINER_CHUNK tiene que ser un tramo que termine holgadamente dentro del
        terminationGracePeriodSeconds del pod (20 s en k3s-local/miner-cpu.yaml); si no, llega el
        SIGKILL antes del release y la tarea vuelve a la cola recién cuando vence el préstamo."""
        log.info("señal %s: termino el tramo, devuelvo lo prestado y salgo", signum)
        self.stopping = True
        self.wake.set()  # si estaba esperando trabajo, que no agote el timeout

    def release(self, task_id: str, reason: str = "") -> bool:
        """Devuelve una tarea prestada. `reason=shutdown` no cuenta como falla en el gateway."""
        query = f"?miner_id={self.id}" + (f"&reason={reason}" if reason else "")
        try:
            status, _ = self.gw.call("POST", f"/tasks/{task_id}/release{query}")
        except OSError as exc:
            log.warning("no pude devolver la tarea %s: %s (vence el préstamo)", task_id[:8], exc)
            return False
        return status == 200

    def release_pending(self, reason: str = "shutdown") -> int:
        """Devuelve todo lo que el gateway nos prestó y no vamos a minar: la tarea en curso y las que
        quedaron empujadas en la cola del WebSocket (el gateway las presta al empujarlas, así que si
        no se devuelven quedan 60 s colgadas aunque el pod ya no exista)."""
        devueltas = 0
        for task_id in self.borrowed():
            if self.release(task_id, reason):
                devueltas += 1
        if devueltas:
            log.info("devolví %d tarea(s) con reason=%s", devueltas, reason)
        return devueltas

    def borrowed(self) -> list[str]:
        """Ids de las tareas que tenemos prestadas ahora mismo."""
        ids = []
        with self.lock:
            if self.task_id is not None:
                ids.append(self.task_id)
        while True:
            try:
                ids.append(self.pushed.get_nowait()["task_id"])
            except (queue.Empty, KeyError):
                break
        return ids

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
                status, _ = self.gw.call("POST", "/keepalive", {"miner_id": self.id, "type": self.kind,
                                                                "hashrate": self.hashrate, "account": self.account})
                if status == 400:  # sin keep-alive el pool no lo cuenta como vivo
                    log.error("keep-alive rechazado: MINER_ACCOUNT=%s tiene que ser una cuenta registrada (/cuenta)",
                              self.account)
            except OSError as exc:
                log.warning("keep-alive falló: %s", exc)
            self.stop.wait(self.keepalive)

    def mine(self, task: dict) -> str:
        """Recorre [from, to] por tramos. Devuelve found / exhausted / abandoned / shutdown."""
        lo, hi = int(task["from"]), int(task["to"])
        hashes, engine_seconds, t0 = 0, 0.0, time.perf_counter()
        share = task.get("share_prefix") or ""
        shares: list[int] = []
        result = {"task_id": task["task_id"], "block_id": task["block_id"], "worker": self.id,
                  "from": lo, "to": hi, "found": False}
        self.block_id = task["block_id"]
        with self.lock:
            self.task_id = task["task_id"]
        while lo <= hi:
            end = min(lo + self.chunk - 1, hi)
            try:
                out = self.run_engine(task["base"], task["prefix"], lo, end, share if len(shares) < MAX_SHARES else "",
                                      MAX_SHARES - len(shares))
            except Abandoned:
                self.gw.call("POST", f"/tasks/{task['task_id']}/renew?miner_id={self.id}")  # 410: el gateway borra el préstamo
                with self.lock:
                    self.task_id = None
                return "abandoned"
            shares += out.get("shares") or []
            hashes += int(out["hashes"])
            engine_seconds += float(out.get("seconds") or 0)  # solo minando: sin arrancar el proceso ni la red
            self.hashrate = hashes / max(time.perf_counter() - t0, 1e-6)
            if out["found"]:
                result.update(found=True, nonce=out["nonce"], hash=out["hash"])
                break
            lo = end + 1
            if self.stopping and lo <= hi:
                # C5: el tramo terminó y nos estamos apagando. El resto del rango no lo vamos a
                # minar, así que se devuelve ya y otro minero lo agarra al instante, en vez de
                # quedar 60 s esperando que venza el préstamo.
                with self.lock:
                    self.task_id = None
                self.release(task["task_id"], "shutdown")
                return "shutdown"
            if lo <= hi:
                status, _ = self.gw.call("POST", f"/tasks/{task['task_id']}/renew?miner_id={self.id}")
                if status == 410:
                    with self.lock:
                        self.task_id = None
                    return "abandoned"
        result.update(hashes=hashes, seconds=round(time.perf_counter() - t0, 3),
                      engine_seconds=round(engine_seconds, 6))
        if share:
            result["shares"] = shares
        result["signature"] = sign_result(self.key, self.chain_id, result)  # A3-f
        with self.lock:
            self.task_id = None  # la entrega consume el préstamo: ya no hay nada que devolver
        status, _ = self.gw.call("POST", "/results", result)
        if status == 401:
            log.error("el gateway rechazó la firma del resultado: me vuelvo a autorizar")
            self.authorize_http()
        log.info("tarea %s rango %d-%d found=%s shares=%d -> %s (%.0f H/s)", task["task_id"][:8],
                 int(task["from"]), hi, result["found"], len(shares), status, self.hashrate)
        return "found" if result["found"] else "exhausted"

    def wait_idle(self) -> None:
        """Sin tarea: con el WebSocket conectado espera el aviso (y pregunta igual cada idle_ws por si se perdió
        uno); sin WebSocket, pregunta cada `idle` como antes."""
        self.wake.wait(self.idle_ws if self.notices and self.notices.connected else self.idle)
        self.wake.clear()

    def authorize_http(self) -> bool:
        """authorize por HTTP (A3-f): ata la clave aunque el WebSocket no ande, así los resultados se pueden firmar."""
        try:
            status, ch = self.gw.call("GET", f"/miners/challenge?miner_id={self.id}")
            if status != 200:
                return False
            self.chain_id = int(ch.get("chain_id", self.chain_id))
            text = f"sdmer:{self.chain_id}:authorize:{self.id}:{ch['challenge']}"
            status, body = self.gw.call("POST", "/miners/authorize", {
                "miner_id": self.id, "pubkey": ed25519_public(self.key).hex(), "challenge": ch["challenge"],
                "signature": ed25519_sign(self.key, text.encode()).hex()})
        except OSError as exc:
            log.warning("authorize falló: %s", exc)
            return False
        if status != 200:
            log.error("authorize rechazado (%s): ¿la clave de este worker cambió? %s", status, body)
        return status == 200

    def next_task(self) -> tuple[int, dict]:
        """Con el WebSocket autorizado, la tarea llega empujada (mining.notify). Sin él, se pide por HTTP."""
        if self.notices and self.notices.authorized:
            # Se espera en tramos cortos y no de un saque: así un SIGTERM no tiene que aguantar
            # los idle_ws segundos enteros antes de que el loop se entere (C5).
            end = time.monotonic() + self.idle_ws
            while not self.stopping and time.monotonic() < end:
                try:
                    return 200, self.pushed.get(timeout=0.5)
                except queue.Empty:
                    continue
            return 204, {}
        try:
            return 200, self.pushed.get_nowait()  # alguna que llegó antes de que se cortara
        except queue.Empty:
            return self.gw.call("GET", f"/tasks/next?miner_id={self.id}&kind={self.kind}")

    def run(self, websocket: bool = True) -> None:
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        while not self.authorize_http():
            if self.stopping:
                return
            time.sleep(5)
        if websocket:
            self.notices = Notices(self.gw.url, self.gw.token, self)
            self.notices.start()
        while not self.stopping:
            try:
                status, task = self.next_task()
            except OSError as exc:
                log.warning("gateway inaccesible: %s", exc)
                time.sleep(self.idle)
                continue
            if status == 204 and self.notices and self.notices.authorized:
                continue  # empujadas: esperar la próxima
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
                with self.lock:
                    self.task_id = None
                self.release(task["task_id"])  # sin reason: el gateway lo cuenta como falla, como antes
                if not self.stopping:
                    time.sleep(max(self.idle, 2))
                continue
            if outcome == "abandoned":
                log.info("tarea %s abandonada: el bloque ya se cerró", task["task_id"][:8])
            elif outcome == "shutdown":
                break
        # Salida ordenada (C5): se devuelve todo lo prestado y se sale con 0, así el bloque no espera
        # los 60 s del préstamo. Apagarse no es una falla: va con reason=shutdown.
        self.release_pending("shutdown")
        self.stop.set()  # corta el keep-alive y el hilo del WebSocket
        log.info("minero %s apagado", self.id)


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
    miner = Miner(gw, cmd, miner_id, kind, a.chunk, a.keepalive, a.idle, a.idle_ws)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, miner.handle_signal)  # C5: KEDA baja las réplicas con SIGTERM
    miner.run(not a.no_websocket)
    return 0


if __name__ == "__main__":
    sys.exit(main())
