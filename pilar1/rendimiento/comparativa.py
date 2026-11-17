#!/usr/bin/env python3
"""Comparativa CPU vs GPU del enunciado: Hit #6 (prefijos de distinto largo) y Hit #7 (rango con y sin
solución). Mismas bases y mismos rangos en los dos motores, 1 corrida de calentamiento y la mediana de 5.

Hit #6: para cada largo de prefijo, 5 bases distintas (la misma lista en CPU y GPU) buscando desde 0. El
tiempo hasta la solución depende de la suerte con cada base, por eso se usan 5 bases y no 5 veces la misma.
Hit #7: una base y un rango que SÍ contiene una solución (tiene que cortar antes) contra uno que NO (tiene
que recorrer el rango entero y devolver found: false). Los rangos se buscan una vez con la GPU y se
verifican con el oráculo.

    python pilar1/rendimiento/comparativa.py --cpu "python pilar1/cpu/miner_cpu.py --workers 12" \
        --gpu "pilar1/build/miner_gpu" --prefijos 1,2,3,4,5,6,7
"""
from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import json
import shlex
import statistics
import subprocess
import sys
import time
from pathlib import Path

SALIDA = Path(__file__).with_name("comparativa.csv")
CAMPOS = ["fecha", "hit", "motor", "caso", "corrida", "found", "nonce", "hashes", "seg_motor", "seg_total"]


def tope(n: int) -> int:
    """"Desde 0 hasta que aparezca": 20 veces lo esperado (16^n), la chance de no encontrar es e^-20."""
    return 20 * 16**n - 1


def base(i: int) -> str:
    """Misma forma que la base real: previous_hash (32 hex) + tx_digest (64 hex) = 96 bytes."""
    s = f"comparativa-{i}".encode()
    return hashlib.md5(s).hexdigest() + hashlib.sha256(s).hexdigest()


def correr(motor: list[str], b: str, prefijo: str, desde: int, hasta: int) -> dict:
    cmd = [*motor, "--base", b, "--prefix", prefijo, "--from", str(desde), "--to", str(hasta)]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    total = time.perf_counter() - t0
    if proc.returncode != 0:
        sys.exit(f"el motor falló ({proc.returncode}): {proc.stderr.strip()}")
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    if out["found"]:  # nunca confiar en el motor: se verifica cada resultado
        h = hashlib.md5((b + str(out["nonce"])).encode()).hexdigest()
        assert h == out["hash"] and h.startswith(prefijo) and desde <= out["nonce"] <= hasta, out
    out["seg_total"] = total
    return out


def rangos_hit7(gpu: list[str], b: str, prefijo: str, ancho: int) -> tuple[tuple[int, int], tuple[int, int]]:
    """Un rango de `ancho` nonces centrado en una solución, y otro sin ninguna (verificado recorriéndolo)."""
    sol = correr(gpu, b, prefijo, 0, tope(len(prefijo)))["nonce"]
    con = (max(0, sol - ancho // 2), max(0, sol - ancho // 2) + ancho - 1)
    desde = sol + ancho
    while True:
        r = correr(gpu, b, prefijo, desde, desde + ancho - 1)
        if not r["found"]:
            return con, (desde, desde + ancho - 1)
        desde = r["nonce"] + 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cpu", required=True, help="comando del motor CPU")
    p.add_argument("--gpu", required=True, help="comando del motor GPU")
    p.add_argument("--prefijos", default="1,2,3,4,5,6,7")
    p.add_argument("--prefijos-solo-gpu", default="8", help="largos que en CPU tardan demasiado")
    p.add_argument("--hit7-prefijo", default="0000000")  # 16^7 = 268 M: 100 M sin solución es probable
    p.add_argument("--hit7-ancho", type=int, default=100_000_000)
    p.add_argument("--corridas", type=int, default=5)
    a = p.parse_args()
    motores = {"cpu": shlex.split(a.cpu, posix=False), "gpu": shlex.split(a.gpu, posix=False)}
    fecha = datetime.datetime.now().isoformat(timespec="seconds")
    filas = []

    def guardar(hit: str, motor: str, caso: str, corrida: int, r: dict) -> None:
        filas.append({"fecha": fecha, "hit": hit, "motor": motor, "caso": caso, "corrida": corrida,
                      "found": r["found"], "nonce": r.get("nonce", ""), "hashes": r["hashes"],
                      "seg_motor": r["seconds"], "seg_total": round(r["seg_total"], 4)})

    # Hit #6: largos de prefijo.
    largos = [(int(n), m) for n in a.prefijos.split(",") for m in ("cpu", "gpu")]
    largos += [(int(n), "gpu") for n in a.prefijos_solo_gpu.split(",") if n]
    for n, m in largos:
        correr(motores[m], base(99), "0" * n, 0, tope(n))  # calentamiento
        segs = []
        for i in range(a.corridas):
            r = correr(motores[m], base(i), "0" * n, 0, tope(n))
            guardar("6", m, f"prefijo-{n}", i + 1, r)
            segs.append(r["seconds"])
        print(f"Hit 6  prefijo {n}  {m}: mediana {statistics.median(segs):.4f} s", flush=True)

    # Hit #7: rango con solución vs sin solución.
    b = base(7)
    con, sin = rangos_hit7(motores["gpu"], b, a.hit7_prefijo, a.hit7_ancho)
    print(f"Hit 7  base {b[:12]}…  con solución {con}  sin solución {sin}", flush=True)
    for m in ("cpu", "gpu"):
        for caso, (desde, hasta) in (("con-solucion", con), ("sin-solucion", sin)):
            correr(motores[m], b, a.hit7_prefijo, desde, hasta)  # calentamiento
            segs = []
            for i in range(a.corridas):
                r = correr(motores[m], b, a.hit7_prefijo, desde, hasta)
                assert r["found"] == (caso == "con-solucion"), r
                guardar("7", m, caso, i + 1, r)
                segs.append(r["seconds"])
            print(f"Hit 7  {caso:13} {m}: mediana {statistics.median(segs):.4f} s", flush=True)

    nuevo = not SALIDA.exists()
    with SALIDA.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        if nuevo:
            w.writeheader()
        w.writerows(filas)
    print(f"guardado en {SALIDA}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
