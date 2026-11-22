#!/usr/bin/env python3
"""Parte GPU de hashrate.py y comparativa.py para una GPU prestada (InferHive), donde no hay motor CPU.

Usa las mismas bases y los mismos rangos que la corrida CPU vs GPU del 17/11 (comparativa.csv), así las
filas nuevas se comparan contra la mejor CPU (12 procesos) sin volver a correrla. El rango con solución
del Hit #7 se reconstruye del nonce de esa corrida (373813508, ancho 100 M); el sin solución se busca
igual que en comparativa.py y se verifica recorriéndolo. Escribe dos CSV con el mismo formato que
resultados.csv y comparativa.csv, para agregarlos a esos archivos.

    python3 gpu_remota.py --nombre gpu-rtx4060 -- /app/miner_gpu
"""
from __future__ import annotations

import argparse
import csv
import datetime
import statistics
import sys
from pathlib import Path

import comparativa as comp
import hashrate as hr

SOL_17_11 = 373_813_508  # nonce del Hit #7 en la corrida del 17/11


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--nombre", required=True, help="etiqueta de la GPU (gpu-rtx4060, ...)")
    p.add_argument("--rango", type=int, default=10_000_000_000, help="nonces por corrida del hashrate")
    p.add_argument("--prefijos", default="1,2,3,4,5,6,7,8")
    p.add_argument("--corridas", type=int, default=5)
    p.add_argument("--salida", default=".", help="carpeta de los CSV")
    p.add_argument("motor", nargs=argparse.REMAINDER, help="-- <comando del motor>")
    a = p.parse_args()
    motor = a.motor[1:] if a.motor[:1] == ["--"] else a.motor
    fecha = datetime.datetime.now().isoformat(timespec="seconds")
    salida = Path(a.salida)

    # Hashrate (igual que hashrate.py).
    hr.correr(motor, a.rango)  # calentamiento
    filas_hr = []
    for i in range(1, a.corridas + 1):
        hashes, motor_s, total_s = hr.correr(motor, a.rango)
        filas_hr.append({"fecha": fecha, "nombre": a.nombre, "rango": a.rango, "corrida": i, "hashes": hashes,
                         "seg_motor": round(motor_s, 3), "seg_total": round(total_s, 3),
                         "mhs_motor": round(hashes / motor_s / 1e6, 3),
                         "mhs_efectivo": round(hashes / total_s / 1e6, 3)})
    print(f"hashrate {a.nombre}: mediana {statistics.median(f['mhs_motor'] for f in filas_hr):.1f} MH/s motor, "
          f"{statistics.median(f['mhs_efectivo'] for f in filas_hr):.1f} MH/s efectivo", flush=True)

    # Hit #6 y #7 (igual que comparativa.py, solo el motor GPU).
    filas = []

    def guardar(hit: str, caso: str, corrida: int, r: dict) -> None:
        filas.append({"fecha": fecha, "hit": hit, "motor": a.nombre, "caso": caso, "corrida": corrida,
                      "found": r["found"], "nonce": r.get("nonce", ""), "hashes": r["hashes"],
                      "seg_motor": r["seconds"], "seg_total": round(r["seg_total"], 4)})

    for n in (int(x) for x in a.prefijos.split(",")):
        comp.correr(motor, comp.base(99), "0" * n, 0, comp.tope(n))  # calentamiento
        segs = []
        for i in range(a.corridas):
            r = comp.correr(motor, comp.base(i), "0" * n, 0, comp.tope(n))
            guardar("6", f"prefijo-{n}", i + 1, r)
            segs.append(r["seconds"])
        print(f"Hit 6  prefijo {n}: mediana {statistics.median(segs):.4f} s", flush=True)

    b, pre, ancho = comp.base(7), "0000000", 100_000_000
    con = (SOL_17_11 - ancho // 2, SOL_17_11 - ancho // 2 + ancho - 1)
    desde = SOL_17_11 + ancho
    while True:
        r = comp.correr(motor, b, pre, desde, desde + ancho - 1)
        if not r["found"]:
            sin = (desde, desde + ancho - 1)
            break
        desde = r["nonce"] + 1
    print(f"Hit 7  con solución {con}  sin solución {sin}", flush=True)
    for caso, (d, h) in (("con-solucion", con), ("sin-solucion", sin)):
        comp.correr(motor, b, pre, d, h)  # calentamiento
        segs = []
        for i in range(a.corridas):
            r = comp.correr(motor, b, pre, d, h)
            assert r["found"] == (caso == "con-solucion"), r
            guardar("7", caso, i + 1, r)
            segs.append(r["seconds"])
        print(f"Hit 7  {caso}: mediana {statistics.median(segs):.4f} s", flush=True)

    for nombre, campos, datos in (("hashrate.csv", hr.CAMPOS, filas_hr), ("comparativa.csv", comp.CAMPOS, filas)):
        with (salida / f"{a.nombre}-{nombre}").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=campos)
            w.writeheader()
            w.writerows(datos)
    print("listo", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
