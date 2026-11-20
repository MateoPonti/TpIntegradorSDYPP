"""Manda tx sin parar (RATE por segundo) durante SECONDS. Corre en el pod bench, solo stdlib."""
import json
import sys
import time
import urllib.request

URL, RATE, SECONDS = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
t0, i = time.time(), 0
while time.time() - t0 < SECONDS:
    body = json.dumps({"sender": f"user-{i % 97}", "receiver": f"user-{(i * 7 + 3) % 97}", "amount": 0.01}).encode()
    req = urllib.request.Request(URL + "/transactions", data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except OSError:
        pass
    i += 1
    time.sleep(max(0.0, t0 + i / RATE - time.time()))
print("enviadas", i)
