#!/usr/bin/env python3
"""Barrido de un parámetro del minero GPU (--threads, --blocks, --poll o --batch) para elegirlo midiendo.

Para cada valor: 1 corrida de calentamiento y la mediana de 5, con la misma base y el mismo rango, y un
prefijo imposible para que recorra el rango entero (así se compara velocidad, no suerte). Mide la
velocidad del motor (`seconds` que informa) y agrega las filas a barrido.csv.

    python pilar1/rendimiento/barrido.py --param threads --valores 64,128,256,512 -- pilar1/build/miner_gpu
    python pilar1/rendimiento/barrido.py --param blocks  --valores 24,48,96,192 --fijos --threads 256 -- ...
"""
from __future__ import annotations

import argparse
import csv
import datetime
import statistics
import sys
from pathlib import Path

from hashrate import correr

SALIDA = Path(__file__).with_name("barrido.csv")
CAMPOS = ["fecha", "param", "valor", "fijos", "rango", "mhs_mediana", "mhs_min", "mhs_max"]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--param", required=True, choices=["threads", "blocks", "poll", "batch"])
    p.add_argument("--valores", required=True, help="lista separada por comas")
    p.add_argument("--rango", type=int, default=1_000_000_000)
    p.add_argument("--corridas", type=int, default=5)
    p.add_argument("--fijos", default="", help="otros flags que quedan fijos, ej. '--threads 256'")
    p.add_argument("motor", nargs=argparse.REMAINDER, help="-- <comando del motor>")
    a = p.parse_args()
    motor = a.motor[1:] if a.motor[:1] == ["--"] else a.motor
    if not motor:
        p.error("falta el comando del motor después de --")

    fecha = datetime.datetime.now().isoformat(timespec="seconds")
    filas = []
    for valor in a.valores.split(","):
        cmd = [*motor, *a.fijos.split(), f"--{a.param}", valor]
        correr(cmd, a.rango)  # calentamiento
        mhs = []
        for _ in range(a.corridas):
            hashes, seg_motor, _ = correr(cmd, a.rango)
            mhs.append(hashes / seg_motor / 1e6)
        fila = {"fecha": fecha, "param": a.param, "valor": valor, "fijos": a.fijos, "rango": a.rango,
                "mhs_mediana": round(statistics.median(mhs), 1), "mhs_min": round(min(mhs), 1),
                "mhs_max": round(max(mhs), 1)}
        filas.append(fila)
        print(f"--{a.param} {valor:>10}: {fila['mhs_mediana']:8.1f} MH/s (min {fila['mhs_min']}, max {fila['mhs_max']})",
              flush=True)

    nuevo = not SALIDA.exists()
    with SALIDA.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        if nuevo:
            w.writeheader()
        w.writerows(filas)
    return 0


if __name__ == "__main__":
    sys.exit(main())
