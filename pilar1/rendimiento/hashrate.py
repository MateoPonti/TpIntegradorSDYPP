#!/usr/bin/env python3
"""Hashrate MD5 de un motor con el contrato del kit, para elegir los valores iniciales del sistema
(prefijo, tamaño de rango, calibración).

Recorre un rango fijo con un prefijo imposible (así no corta antes), con 1 corrida de calentamiento
y la mediana de 5. Separa la velocidad del motor (`seconds` que informa el motor) del rendimiento
efectivo (tiempo de punta a punta, con el arranque del proceso). Al final estima cuánto tardaría un
bloque con cada largo de prefijo: 16^n / hashrate.

    python pilar1/rendimiento/hashrate.py --nombre cpu-1  --rango 2000000  -- python pilar1/cpu/miner_cpu.py --workers 1
    python pilar1/rendimiento/hashrate.py --nombre cpu-12 --rango 20000000 -- python pilar1/cpu/miner_cpu.py --workers 12
    python pilar1/rendimiento/hashrate.py --nombre gpu    --rango 1000000000 -- pilar1/build/miner_gpu
"""
from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

# Misma forma que la base real: previous_hash (32 hex) + tx_digest (64 hex) = 96 bytes.
BASE = hashlib.md5(b"rendimiento").hexdigest() + hashlib.sha256(b"rendimiento").hexdigest()
IMPOSIBLE = "f" * 32  # ningún md5 empieza así en la práctica: el motor recorre el rango entero
SALIDA = Path(__file__).with_name("resultados.csv")
CAMPOS = ["fecha", "nombre", "rango", "corrida", "hashes", "seg_motor", "seg_total", "mhs_motor", "mhs_efectivo"]


def correr(motor: list[str], rango: int) -> tuple[int, float, float]:
    cmd = [*motor, "--base", BASE, "--prefix", IMPOSIBLE, "--from", "0", "--to", str(rango - 1)]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    total = time.perf_counter() - t0
    if proc.returncode != 0:
        sys.exit(f"el motor falló ({proc.returncode}): {proc.stderr.strip()}")
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    if out.get("found"):
        sys.exit(f"el motor encontró un nonce con prefijo imposible: {out}")
    return int(out["hashes"]), float(out["seconds"]), total


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--nombre", required=True, help="etiqueta de la corrida (cpu-1, cpu-12, gpu, ...)")
    p.add_argument("--rango", type=int, required=True, help="nonces por corrida")
    p.add_argument("--corridas", type=int, default=5)
    p.add_argument("motor", nargs=argparse.REMAINDER, help="-- <comando del motor>")
    a = p.parse_args()
    motor = a.motor[1:] if a.motor[:1] == ["--"] else a.motor
    if not motor:
        p.error("falta el comando del motor después de --")

    correr(motor, a.rango)  # calentamiento: no se guarda
    filas = []
    fecha = datetime.datetime.now().isoformat(timespec="seconds")
    for i in range(1, a.corridas + 1):
        hashes, motor_s, total_s = correr(motor, a.rango)
        fila = {"fecha": fecha, "nombre": a.nombre, "rango": a.rango, "corrida": i, "hashes": hashes,
                "seg_motor": round(motor_s, 3), "seg_total": round(total_s, 3),
                "mhs_motor": round(hashes / motor_s / 1e6, 3), "mhs_efectivo": round(hashes / total_s / 1e6, 3)}
        filas.append(fila)
        print(f"corrida {i}: {fila['mhs_motor']} MH/s motor, {fila['mhs_efectivo']} MH/s efectivo")

    nuevo = not SALIDA.exists()
    with SALIDA.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        if nuevo:
            w.writeheader()
        w.writerows(filas)

    motor_hs = statistics.median(f["mhs_motor"] for f in filas) * 1e6
    efectivo_hs = statistics.median(f["mhs_efectivo"] for f in filas) * 1e6
    print(f"\n{a.nombre}: mediana {motor_hs / 1e6:.2f} MH/s motor, {efectivo_hs / 1e6:.2f} MH/s efectivo")
    print("\nprefijo | hashes esperados | tiempo esperado por bloque")
    for n in range(1, 9):
        print(f"{n:7} | {16**n:16,} | {16**n / motor_hs:10.2f} s")
    print(f"\nguardado en {SALIDA}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
