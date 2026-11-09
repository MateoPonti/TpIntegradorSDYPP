#!/usr/bin/env python3
"""CPU reference miner (the "oracle") for TPI Pilar 1.

It implements the SAME command line contract that your CUDA miner must
implement, so every tool in this starter kit (contract checker, energy meter,
coordinator adapter) works with either binary.

Contract
--------
    miner --base <str> (--prefix <hex> | --difficulty <d>) --from <n0> --to <n1>
    miner --hash <str>

* The candidate string is ``base + str(nonce)`` (decimal, no separators, UTF-8),
  exactly as the cátedra coordinator computes it.
* The search covers the closed interval ``[from, to]``.
* Output: ONE JSON line on stdout, exit code 0 whether or not a nonce is found:
      {"found": true,  "nonce": 123, "hash": "000a...", "hashes": 124, "seconds": 0.01}
      {"found": false, "hashes": 1001, "seconds": 0.02}
* ``--hash`` prints {"hash": "<md5 hex>"} (TPI Hit #4).
* Any usage error: message on stderr and exit code 2.

This oracle is deliberately simple (single process, lowest nonce first). It is
the ground truth for tests, not a competitor.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time

HEX = set("0123456789abcdef")


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def search(base: str, prefix: str, start: int, end: int) -> dict:
    """Scan [start, end] and return the first nonce whose hash starts with prefix."""
    t0 = time.perf_counter()
    hashes = 0
    for nonce in range(start, end + 1):
        h = md5_hex(f"{base}{nonce}")
        hashes += 1
        if h.startswith(prefix):
            return {
                "found": True,
                "nonce": nonce,
                "hash": h,
                "hashes": hashes,
                "seconds": round(time.perf_counter() - t0, 6),
            }
    return {
        "found": False,
        "hashes": hashes,
        "seconds": round(time.perf_counter() - t0, 6),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="CPU MD5 nonce miner (reference oracle)")
    p.add_argument("--hash", dest="hash_input", help="only print md5(<str>)")
    p.add_argument("--base", help="previous_hash + data, as sent by the coordinator")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--prefix", help="required hex prefix, e.g. 0000 or 00ab")
    g.add_argument("--difficulty", type=int, help="number of leading hex zeros")
    p.add_argument("--from", dest="start", type=int, default=0)
    p.add_argument("--to", dest="end", type=int, default=2**32 - 1)
    a = p.parse_args(argv)
    if a.hash_input is not None:
        return a
    if a.base is None or (a.prefix is None and a.difficulty is None):
        p.error("--base and one of --prefix/--difficulty are required")
    if a.difficulty is not None:
        if a.difficulty < 0 or a.difficulty > 32:
            p.error("--difficulty must be between 0 and 32")
        a.prefix = "0" * a.difficulty
    a.prefix = a.prefix.lower()
    if not set(a.prefix) <= HEX or len(a.prefix) > 32:
        p.error("--prefix must be at most 32 hex characters")
    if a.start < 0 or a.end < a.start:
        p.error("need 0 <= --from <= --to")
    return a


def main(argv: list[str] | None = None) -> int:
    a = parse_args(sys.argv[1:] if argv is None else argv)
    if a.hash_input is not None:
        print(json.dumps({"hash": md5_hex(a.hash_input)}))
        return 0
    print(json.dumps(search(a.base, a.prefix, a.start, a.end)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
