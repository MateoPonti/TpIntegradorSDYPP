"""Test de integración: flujo completo contra el docker compose levantado.

    cd pilar2 && docker compose up -d --build
    TPI_E2E_URL=http://localhost:8000 python -m pytest -q pilar2/tests/test_integration.py
Se saltea si TPI_E2E_URL no está definida (así el CI de unitarios no necesita Docker).
"""
import json
import os
import time
import urllib.error
import urllib.request

import pytest

import verify_chain

URL = os.getenv("TPI_E2E_URL")
pytestmark = pytest.mark.skipif(not URL, reason="definir TPI_E2E_URL con el compose levantado")


def post(path: str, body) -> tuple[int, dict]:
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_transactions_get_mined_and_chain_is_valid():
    start_height = verify_chain.get(f"{URL}/chain?limit=1")["height"]
    code, body = post("/transactions", {"sender": "user-1", "receiver": "user-2", "amount": 10.5})
    assert code == 202 and body["tx_id"]
    code, bulk = post("/transactions/bulk", [{"sender": f"user-{i}", "receiver": "user-0", "amount": 1}
                                             for i in range(1, 50)])
    assert code == 202 and bulk["accepted"] == 49

    assert post("/transactions", {"sender": "user-3", "receiver": "user-4", "amount": 10**9})[0] == 400

    deadline = time.time() + 180
    while time.time() < deadline:
        if verify_chain.get(f"{URL}/status")["pending_transactions"] == 0:
            break
        time.sleep(2)
    else:
        pytest.fail("las tx no se confirmaron en 180 s")

    assert verify_chain.get(f"{URL}/chain?limit=1")["height"] > start_height
    tx = verify_chain.get(f"{URL}/transactions/{body['tx_id']}")
    assert tx["status"] == "confirmed"
    assert verify_chain.verify(URL) == 0
