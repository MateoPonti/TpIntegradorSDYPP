#!/usr/bin/env python3
"""Regenerate tests/vectors.json with the CPU oracle.

Every mining case stores the range, the prefix and whether a solution exists.
It does NOT force a specific nonce: a GPU miner may legally return any nonce in
the range that satisfies the prefix (not necessarily the lowest one).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "oracle"))
from miner_cpu import md5_hex, search  # noqa: E402

# RFC 1321, appendix A.5 test suite.
RFC1321 = [
    "",
    "a",
    "abc",
    "message digest",
    "abcdefghijklmnopqrstuvwxyz",
]

BASE = "0" * 32 + "bloque 1 · A->B:10"
LONG_BASE = "00000c8f1c2a6e0b8c7e2f5d9a1b3c4d" + "bloque 42 · " + "tx;" * 20

CASES = [
    # name, base, prefix, from, to
    ("d1-small", BASE, "0", 0, 1000),
    ("d2-small", BASE, "00", 0, 5000),
    ("d3", BASE, "000", 0, 100_000),
    ("d4", BASE, "0000", 0, 1_000_000),
    ("d4-offset", BASE, "0000", 5_000_000, 6_000_000),
    ("custom-prefix", BASE, "abc", 0, 200_000),
    ("long-base-d3", LONG_BASE, "000", 0, 100_000),
    ("utf8-base", "ñandú·ü" + BASE, "00", 0, 5000),
    ("big-nonces", BASE, "000", 4_294_967_000, 4_295_100_000),
    ("none-in-range", BASE, "000000", 0, 2000),
    ("single-nonce-miss", BASE, "0", 7, 7),
    ("single-nonce-hit", BASE, "0", 14, 14),
]


def main() -> None:
    vectors = {
        "hash": [{"input": s, "md5": md5_hex(s)} for s in RFC1321],
        "mining": [],
    }
    for name, base, prefix, lo, hi in CASES:
        r = search(base, prefix, lo, hi)
        vectors["mining"].append(
            {
                "name": name,
                "base": base,
                "prefix": prefix,
                "from": lo,
                "to": hi,
                "has_solution": r["found"],
                "lowest_nonce": r.get("nonce"),
            }
        )
    out = ROOT / "tests" / "vectors.json"
    out.write_text(
        json.dumps(vectors, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
