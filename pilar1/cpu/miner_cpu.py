#!/usr/bin/env python3
"""Minero CPU multiproceso con el MISMO contrato CLI que miner_gpu (y que oracle/miner_cpu.py).

Sirve para dos cosas:
  * Pilar 1: la "mejor CPU" contra la que se calcula el speedup de la GPU (--workers 12 en el i5-12400F).
  * Pilar 2/3: es el motor de la imagen miner-cpu que corre en el k3s local (nunca en GCP).

    python cpu/miner_cpu.py --base "<prev><data>" --difficulty 5 --from 0 --to 99999999 [--workers N]

Reparte el rango en trozos chicos entre procesos (como schedule(dynamic) de OpenMP) y corta
apenas uno encuentra. Devuelve cualquier nonce válido, no necesariamente el menor (igual que la GPU).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import os
import sys
import time

HEX = set("0123456789abcdef")
SLICE = 200_000          # nonces por trozo que toma cada proceso
SEQUENTIAL_BELOW = 400_000  # rangos chicos: no vale la pena levantar procesos


def scan(args: tuple[str, str, int, int]) -> tuple[int | None, str | None, int]:
    """Busca en [lo, hi]. Devuelve (nonce, hash, hashes_hechos)."""
    base, prefix, lo, hi = args
    seed = hashlib.md5(base.encode("utf-8"))  # el prefijo de la base se procesa una sola vez
    for nonce in range(lo, hi + 1):
        h = seed.copy()
        h.update(str(nonce).encode())
        digest = h.hexdigest()
        if digest.startswith(prefix):
            return nonce, digest, nonce - lo + 1
    return None, None, hi - lo + 1


def search(base: str, prefix: str, start: int, end: int, workers: int) -> dict:
    t0 = time.perf_counter()
    if workers <= 1 or end - start + 1 < SEQUENTIAL_BELOW:
        nonce, digest, hashes = scan((base, prefix, start, end))
    else:
        slices = ((base, prefix, lo, min(lo + SLICE - 1, end)) for lo in range(start, end + 1, SLICE))
        nonce, digest, hashes = None, None, 0
        with mp.Pool(workers) as pool:
            for n, d, h in pool.imap_unordered(scan, slices):
                hashes += h
                if n is not None:
                    nonce, digest = n, d
                    pool.terminate()  # corte temprano
                    break
    out = {"found": nonce is not None}
    if nonce is not None:
        out.update(nonce=nonce, hash=digest)
    out.update(hashes=hashes, seconds=round(time.perf_counter() - t0, 6))
    return out


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="CPU MD5 nonce miner (multiproceso)")
    p.add_argument("--hash", dest="hash_input", help="solo imprime md5(<str>)")
    p.add_argument("--base")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--prefix")
    g.add_argument("--difficulty", type=int)
    p.add_argument("--from", dest="start", type=int, default=0)
    p.add_argument("--to", dest="end", type=int, default=2**32 - 1)
    p.add_argument("--workers", type=int, default=int(os.getenv("CPU_WORKERS", os.cpu_count() or 1)))
    a = p.parse_args(argv)
    if a.hash_input is not None:
        return a
    if a.base is None or (a.prefix is None and a.difficulty is None):
        p.error("--base and one of --prefix/--difficulty are required")
    if a.difficulty is not None:
        if not 0 <= a.difficulty <= 32:
            p.error("--difficulty must be between 0 and 32")
        a.prefix = "0" * a.difficulty
    a.prefix = a.prefix.lower()
    if not set(a.prefix) <= HEX or len(a.prefix) > 32:
        p.error("--prefix must be at most 32 hex characters")
    if a.start < 0 or a.end < a.start or a.end > 2**64 - 1:
        p.error("need 0 <= --from <= --to < 2^64")
    return a


def main(argv: list[str] | None = None) -> int:
    a = parse_args(sys.argv[1:] if argv is None else argv)
    if a.hash_input is not None:
        print(json.dumps({"hash": hashlib.md5(a.hash_input.encode("utf-8")).hexdigest()}))
        return 0
    print(json.dumps(search(a.base, a.prefix, a.start, a.end, a.workers)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
