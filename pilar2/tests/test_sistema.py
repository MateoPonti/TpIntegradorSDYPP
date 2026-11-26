"""Pruebas del sistema andando (local con docker compose o en GKE). Necesitan al menos un minero conectado
al gateway (el agente CPU de la PC, el k3s o la GPU), porque esperan que las tx se confirmen.

    TPI_E2E_URL=http://127.0.0.1:18080 python -m pytest -v pilar2/tests/test_sistema.py -k doble     # compose
    TPI_E2E_URL=http://35.223.237.60 TPI_K8S_CONTEXT=gke_tp3distribuidos_us-central1-a_tp3 \
      python -m pytest -v pilar2/tests/test_sistema.py -k caida

Actualizadas el 21/11: las cuentas de prueba se registran con clave Ed25519 y gastan firmando con su seq, y el
saldo esperado descuenta la comisión de 0,10.

* Doble gasto: dos tx simultáneas y firmadas que gastan el mismo saldo. Una entra (202) y la otra no (400).
  Desde el 26/11 (A1) el génesis no le puede mandar plata a una cuenta propia, así que la cuenta de prueba se fondea
  desde una cuenta registrada con saldo de minado: TPI_FONDOS_CUENTA=<cuenta> TPI_FONDOS_KEY=<archivo .key>
  (por ejemplo la MINER_ACCOUNT del grupo). Sin esas dos variables, los tests de doble gasto se saltean.
* Caídas: se mata un pod de cada servicio (y RabbitMQ y Redis). Después de cada una, una tx nueva tiene
  que terminar confirmada y la cadena tiene que seguir íntegra (verify_chain).
"""
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import verify_chain

URL = (os.getenv("TPI_E2E_URL") or "").rstrip("/")
K8S_CONTEXT = os.getenv("TPI_K8S_CONTEXT")
FONDOS_CUENTA = os.getenv("TPI_FONDOS_CUENTA")
FONDOS_KEY = os.getenv("TPI_FONDOS_KEY")
needs_funds = pytest.mark.skipif(not (FONDOS_CUENTA and FONDOS_KEY),
                                 reason="definir TPI_FONDOS_CUENTA y TPI_FONDOS_KEY (cuenta registrada con saldo)")
NAMESPACE = os.getenv("TPI_NAMESPACE", "tpi")
pytestmark = pytest.mark.skipif(not URL, reason="definir TPI_E2E_URL con el sistema levantado")


def post(path: str, body) -> tuple[int, dict]:
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        return 0, {"error": str(exc)}  # se cortó mientras se reiniciaba un pod: send_ok reintenta


def get(path: str) -> dict:
    for _ in range(30):  # mientras se reinicia un pod, el LoadBalancer puede cortar alguna conexión
        try:
            return verify_chain.get(URL + path)
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(2)
    return verify_chain.get(URL + path)


def wait_confirmed(tx_id: str, limit: float = 240) -> dict:
    deadline = time.time() + limit
    while time.time() < deadline:
        tx = get(f"/transactions/{tx_id}")
        if tx["status"] == "confirmed":
            return tx
        time.sleep(2)
    pytest.fail(f"la tx {tx_id} no se confirmó en {limit} s (¿hay algún minero conectado?)")


def sig_text(texto: str) -> bytes:
    """Lo que se firma desde el 26/11 (A3-g): el texto con el chain id del sistema adelante."""
    return f"sdmer:{get('/chain-id')['chain_id']}:{texto}".encode()


def new_user(tag: str) -> tuple[str, Ed25519PrivateKey]:
    """Cuenta registrada con su clave (17/11: sin registro no hay cuenta propia, y desde el 20/11 el destino de una
    tx tiene que existir)."""
    name, key = f"test-{tag}-{uuid.uuid4().hex[:6]}", Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    code, body = post("/accounts/register", {"account": name, "public_key": pub,
                                             "signature": key.sign(sig_text(f"register:{name}:{pub}")).hex()})
    assert code == 201, body
    return name, key


def signed(sender: str, key: Ed25519PrivateKey, receiver: str, amount_cents: int, seq: int) -> dict:
    """Tx de una cuenta registrada: firma sobre sdmer:<chain_id>:tx:<de>:<para>:<centavos>:<seq>."""
    return {"sender": sender, "receiver": receiver, "amount": f"{amount_cents / 100:.2f}", "seq": seq,
            "signature": key.sign(sig_text(f"tx:{sender}:{receiver}:{amount_cents}:{seq}")).hex()}


def fund(receiver: str, amount_cents: int) -> str:
    """Le manda plata a una cuenta de prueba desde la cuenta con saldo de minado (firmada con su seq siguiente).
    Desde el 26/11 (A1) el génesis no puede: su plata no sale de user-0..96."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as Key
    from pathlib import Path
    key = Key.from_private_bytes(bytes.fromhex(Path(FONDOS_KEY).read_text().strip()))
    seq = get(f"/balances/{FONDOS_CUENTA}")["seq"] + 1
    code, body = post("/transactions", signed(FONDOS_CUENTA, key, receiver, amount_cents, seq))
    assert code == 202, f"no se pudo fondear la cuenta de prueba desde {FONDOS_CUENTA}: {code} {body}"
    return body["tx_id"]


def send_ok(sender: str, receiver: str, amount) -> str:
    code, body = post("/transactions", {"sender": sender, "receiver": receiver, "amount": amount})
    for _ in range(20):  # durante una caída el coordinator puede tardar en volver
        if code == 202:
            return body["tx_id"]
        time.sleep(3)
        code, body = post("/transactions", {"sender": sender, "receiver": receiver, "amount": amount})
    pytest.fail(f"la tx no fue aceptada: {code} {body}")


@needs_funds
def test_doble_gasto_simultaneo():
    ana, key = new_user("ana")
    wait_confirmed(fund(ana, 10000))
    # Dos gastos de 80,00 firmados con el mismo seq, a destinos distintos, en el mismo instante: alcanza para uno.
    bodies = [signed(ana, key, new_user(n)[0], 8000, 1) for n in ("beto", "caro")]
    barrier, out = threading.Barrier(2), []

    def spend(body: dict) -> None:
        barrier.wait()  # las dos salen en el mismo instante
        out.append(post("/transactions", body))

    threads = [threading.Thread(target=spend, args=(b,)) for b in bodies]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    codes = sorted(code for code, _ in out)
    assert codes == [202, 400], out
    accepted = next(body for code, body in out if code == 202)
    wait_confirmed(accepted["tx_id"])
    balance = get(f"/balances/{ana}")
    # 100,00 − 80,00 − 0,10 de comisión
    assert balance["balance_cents"] == 1990 and balance["pending_out_cents"] == 0
    assert verify_chain.verify(URL) == 0


@needs_funds
def test_doble_gasto_con_seq_distinto():
    """Mismo caso con seq 1 y 2: las dos firmas son válidas, pero el saldo alcanza para una sola."""
    ana, key = new_user("ana")
    wait_confirmed(fund(ana, 10000))
    codes = [post("/transactions", signed(ana, key, new_user(n)[0], 8000, seq))[0]
             for n, seq in (("beto", 1), ("caro", 2))]
    assert codes == [202, 400]


def kubectl(*args: str) -> str:
    cmd = ["kubectl", "--context", K8S_CONTEXT, "-n", NAMESPACE, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


@pytest.mark.skipif(not K8S_CONTEXT, reason="definir TPI_K8S_CONTEXT para matar pods del cluster")
@pytest.mark.parametrize("app", ["coordinator", "pool", "gateway", "rabbitmq", "redis"])
def test_caida_de_un_servicio(app):
    pods = kubectl("get", "pods", "-l", f"app={app}", "-o", "jsonpath={.items[*].metadata.name}").split()
    assert pods, f"no hay pods de {app}"
    kubectl("delete", "pod", pods[0], "--wait=false")
    tx_id = send_ok("user-2", "user-3", 1)  # génesis → génesis (A1): lo que importa es que se confirme
    wait_confirmed(tx_id)
    kind = "statefulset" if app in ("rabbitmq", "redis") else "deployment"
    kubectl("rollout", "status", f"{kind}/{app}", "--timeout=300s")
    assert verify_chain.verify(URL) == 0


def redis_master(via: str = "redis-0") -> str:
    """Pod que Sentinel tiene como principal (la clave se lee adentro del pod, no hace falta tenerla acá)."""
    out = kubectl("exec", via, "-c", "sentinel", "--", "sh", "-c",
                  'redis-cli -p 26379 -a "$REDIS_PASSWORD" --no-auth-warning sentinel get-master-addr-by-name tpi')
    return out.split()[0].split(".")[0]


@pytest.mark.skipif(not K8S_CONTEXT, reason="definir TPI_K8S_CONTEXT para matar pods del cluster")
def test_caida_del_redis_principal():
    old = redis_master()
    via = next(p for p in ("redis-0", "redis-1", "redis-2") if p != old)  # el Sentinel del pod muerto se va con él
    kubectl("delete", "pod", old, "--wait=false")
    deadline = time.time() + 60
    while redis_master(via) == old:
        assert time.time() < deadline, "Sentinel no ascendió a ninguna réplica"
        time.sleep(2)
    tx_id = send_ok("user-2", "user-3", 1)
    wait_confirmed(tx_id)
    kubectl("rollout", "status", "statefulset/redis", "--timeout=300s")
    assert verify_chain.verify(URL) == 0
