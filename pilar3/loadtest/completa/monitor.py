"""Anota /status cada 2 s en un CSV (experimento de entrada y salida de la GPU)."""
import csv
import json
import sys
import time
import urllib.request

URL, OUT, SECONDS = sys.argv[1], sys.argv[2], float(sys.argv[3])
t0 = time.time()
with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["t", "height", "pending", "prefix_block", "prefix_plan", "hashrate", "gpu", "cpu", "rearmed", "mining_s"])
    while time.time() - t0 < SECONDS:
        try:
            s = json.load(urllib.request.urlopen(URL + "/status", timeout=10))
            m, plan = s.get("mining") or {}, s.get("pool_plan") or {}
            w.writerow([round(time.time() - t0, 1), s["chain_height"], s["pending_transactions"], m.get("prefix", ""),
                        plan.get("prefix", ""), round(plan.get("hashrate", 0)), s["miners"]["gpu"], s["miners"]["cpu"],
                        m.get("rearmed", ""), m.get("seconds", "")])
            f.flush()
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(2)
