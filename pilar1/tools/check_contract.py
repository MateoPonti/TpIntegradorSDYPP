#!/usr/bin/env python3
"""Check that a miner binary honours the Pilar 1 contract.

Usage:
    python3 tools/check_contract.py -- ./build/miner_gpu
    python3 tools/check_contract.py -- python3 oracle/miner_cpu.py

It runs every case in tests/vectors.json and verifies, independently of the
miner, that:
  * --hash returns the RFC 1321 digest;
  * a reported nonce lies inside [from, to];
  * md5(base + str(nonce)) equals the reported hash and starts with the prefix;
  * "found": false is only reported when the range really has no solution;
  * stdout is exactly one JSON line and the exit code is 0.
Exit code: 0 if every case passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VECTORS = ROOT / "tests" / "vectors.json"


def run(cmd: list[str], args: list[str], timeout: float) -> tuple[dict | None, str]:
    try:
        proc = subprocess.run(
            cmd + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        return None, f"timeout after {timeout}s"
    except FileNotFoundError as exc:
        return None, f"cannot execute: {exc}"
    if proc.returncode != 0:
        return None, f"exit code {proc.returncode}: {proc.stderr.strip()[:200]}"
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if len(lines) != 1:
        return None, f"expected 1 JSON line on stdout, got {len(lines)}"
    try:
        return json.loads(lines[0]), ""
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"


def check_hash(cmd: list[str], case: dict, timeout: float) -> str:
    out, err = run(cmd, ["--hash", case["input"]], timeout)
    if out is None:
        return err
    if out.get("hash") != case["md5"]:
        return f"got {out.get('hash')!r}, want {case['md5']}"
    return ""


def check_mining(cmd: list[str], case: dict, timeout: float) -> str:
    args = [
        "--base", case["base"],
        "--prefix", case["prefix"],
        "--from", str(case["from"]),
        "--to", str(case["to"]),
    ]  # fmt: skip
    out, err = run(cmd, args, timeout)
    if out is None:
        return err
    for key in ("found", "hashes", "seconds"):
        if key not in out:
            return f"missing key {key!r}"
    if not out["found"]:
        return (
            "reported not found, but the range has a solution"
            if case["has_solution"]
            else ""
        )
    if not case["has_solution"]:
        return "reported a solution in a range that has none"
    nonce = out.get("nonce")
    if not isinstance(nonce, int):
        return f"nonce must be an integer, got {nonce!r}"
    if not case["from"] <= nonce <= case["to"]:
        return f"nonce {nonce} outside [{case['from']}, {case['to']}]"
    real = hashlib.md5(f"{case['base']}{nonce}".encode("utf-8")).hexdigest()
    if out.get("hash") != real:
        return (
            f"hash mismatch: md5(base+{nonce}) = {real}, miner said {out.get('hash')}"
        )
    if not real.startswith(case["prefix"]):
        return f"hash {real} does not start with {case['prefix']}"
    return ""


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--vectors", type=Path, default=DEFAULT_VECTORS)
    p.add_argument("--timeout", type=float, default=120.0, help="seconds per case")
    p.add_argument("--skip-hash", action="store_true", help="skip the --hash cases")
    p.add_argument("cmd", nargs=argparse.REMAINDER, help="miner command, after --")
    a = p.parse_args()
    cmd = [c for c in a.cmd if c != "--"]
    if not cmd:
        p.error("give the miner command after --")
    if len(cmd) == 1 and " " in cmd[0]:
        cmd = shlex.split(cmd[0])
    vectors = json.loads(a.vectors.read_text(encoding="utf-8"))

    failures = 0
    cases = [] if a.skip_hash else [("hash", c, check_hash) for c in vectors["hash"]]
    cases += [("mining", c, check_mining) for c in vectors["mining"]]
    for kind, case, fn in cases:
        label = case.get("name") or f"md5({case['input']!r})"
        err = fn(cmd, case, a.timeout)
        status = "ok  " if not err else "FAIL"
        failures += bool(err)
        print(f"[{status}] {kind:6} {label}" + (f"  -> {err}" if err else ""))
    total = len(cases)
    print(f"\n{total - failures}/{total} cases passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
