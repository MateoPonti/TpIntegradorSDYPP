"""Pruebas del sistema andando (local con docker compose o en GKE). Necesitan al menos un minero conectado
al gateway (el agente CPU de la PC, el k3s o la GPU), porque esperan que las tx se confirmen.

    TPI_E2E_URL=http://35.223.237.60 python -m pytest -v pilar2/tests/test_sistema.py -k doble
    TPI_E2E_URL=http://35.223.237.60 TPI_K8S_CONTEXT=gke_tp3distribuidos_us-central1-a_tp3 \
      python -m pytest -v pilar2/tests/test_sistema.py -k caida

* Doble gasto: dos tx simultáneas que gastan el mismo saldo. Una entra (202) y la otra no (400).
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

import verify_chain

URL = (os.getenv("TPI_E2E_URL") or "").rstrip("/")
K8S_CONTEXT = os.getenv("TPI_K8S_CONTEXT")
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


def new_user(tag: str) -> str:
    return f"test-{tag}-{uuid.uuid4().hex[:6]}"


def send_ok(sender: str, receiver: str, amount) -> str:
    code, body = post("/transactions", {"sender": sender, "receiver": receiver, "amount": amount})
    for _ in range(20):  # durante una caída el coordinator puede tardar en volver
        if code == 202:
            return body["tx_id"]
        time.sleep(3)
        code, body = post("/transactions", {"sender": sender, "receiver": receiver, "amount": amount})
    pytest.fail(f"la tx no fue aceptada: {code} {body}")


def test_doble_gasto_simultaneo():
    ana = new_user("ana")
    wait_confirmed(send_ok("user-1", ana, 100))
    barrier, out = threading.Barrier(2), []

    def spend(receiver: str) -> None:
        barrier.wait()  # las dos salen en el mismo instante
        out.append(post("/transactions", {"sender": ana, "receiver": receiver, "amount": 80}))

    threads = [threading.Thread(target=spend, args=(new_user(n),)) for n in ("beto", "caro")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    codes = sorted(code for code, _ in out)
    assert codes == [202, 400], out
    accepted = next(body for code, body in out if code == 202)
    wait_confirmed(accepted["tx_id"])
    balance = get(f"/balances/{ana}")
    assert balance["balance_cents"] == 2000 and balance["pending_out_cents"] == 0
    assert verify_chain.verify(URL) == 0


def kubectl(*args: str) -> str:
    cmd = ["kubectl", "--context", K8S_CONTEXT, "-n", NAMESPACE, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


@pytest.mark.skipif(not K8S_CONTEXT, reason="definir TPI_K8S_CONTEXT para matar pods del cluster")
@pytest.mark.parametrize("app", ["coordinator", "pool", "gateway", "rabbitmq", "redis"])
def test_caida_de_un_servicio(app):
    pods = kubectl("get", "pods", "-l", f"app={app}", "-o", "jsonpath={.items[*].metadata.name}").split()
    assert pods, f"no hay pods de {app}"
    kubectl("delete", "pod", pods[0], "--wait=false")
    tx_id = send_ok("user-2", new_user(f"caida-{app}"), 1)
    wait_confirmed(tx_id)
    kind = "statefulset" if app in ("rabbitmq", "redis") else "deployment"
    kubectl("rollout", "status", f"{kind}/{app}", "--timeout=300s")
    assert verify_chain.verify(URL) == 0
