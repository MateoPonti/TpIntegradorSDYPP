#!/usr/bin/env python3
"""Thin adapter: coordinator task -> nonce ranges -> your miner binary -> solution.

This is the "integration" half of Pilar 1. It speaks the protocol of the
cátedra coordinator (https://cadena-sd.impulsate.lat, see /como-unirse) in
PULL mode, which is enough to validate your miner end to end. In Pilar 2 you
replace the polling with a RabbitMQ subscription and the cátedra coordinator
with your own NCT.

    export COORDINADOR_URL=https://cadena-sd.impulsate.lat
    export CADENA_TOKEN=...            # class credential, from the aula forum
    python3 tools/worker_adapter.py --minero g-amarillo-gpu1 -- ./build/miner_gpu

Protocol (same as the cátedra coordinator):
    GET  /api/tarea     -> {"task_id","block_index","previous_hash","data","difficulty"}
    POST /api/solucion  <- {"task_id","block_index","nonce","hash","minero","hashrate"}
                           201 sealed, 409 late (someone else won), 400 invalid
The candidate string is previous_hash + data + str(nonce).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

MAX_NONCE = 2**64 - 1  # nonces are uint64 in the CUDA miner


@dataclass
class Coordinator:
    url: str
    token: str | None = None
    timeout: float = 10.0

    def _request(
        self, method: str, path: str, body: dict | None = None
    ) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.url.rstrip('/')}{path}", data=data, method=method
        )
        req.add_header("content-type", "application/json")
        if self.token:
            req.add_header("authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b"{}")
            except json.JSONDecodeError:
                payload = {}
            return exc.code, payload

    def task(self) -> dict:
        status, body = self._request("GET", "/api/tarea")
        if status != 200:
            raise RuntimeError(f"GET /api/tarea -> {status} {body}")
        return body

    def submit(self, solution: dict) -> tuple[int, dict]:
        return self._request("POST", "/api/solucion", solution)


def run_miner(cmd: list[str], base: str, difficulty: int, lo: int, hi: int) -> dict:
    args = [
        "--base",
        base,
        "--difficulty",
        str(difficulty),
        "--from",
        str(lo),
        "--to",
        str(hi),
    ]
    proc = subprocess.run(cmd + args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"miner exited {proc.returncode}: {proc.stderr.strip()[:300]}"
        )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return json.loads(lines[-1])


def mine_task(
    coord: Coordinator,
    cmd: list[str],
    task: dict,
    minero: str,
    chunk: int,
    start: int,
    recheck_s: float,
) -> str:
    """Mine one task range by range. Returns 'sealed', 'late', 'stale', 'invalid' or 'exhausted'."""
    base = task["previous_hash"] + task["data"]
    lo = start
    hashes = 0
    t0 = time.perf_counter()
    last_check = t0
    while True:
        hi = min(lo + chunk - 1, MAX_NONCE)
        res = run_miner(cmd, base, int(task["difficulty"]), lo, hi)
        hashes += int(res["hashes"])
        if res["found"]:
            rate = hashes / max(time.perf_counter() - t0, 1e-6)
            status, body = coord.submit(
                {
                    "task_id": task["task_id"],
                    "block_index": task["block_index"],
                    "nonce": res["nonce"],
                    "hash": res["hash"],
                    "minero": minero,
                    "hashrate": round(rate),
                }
            )
            print(
                f"[{minero}] block {task['block_index']} nonce={res['nonce']} -> {status}",
                flush=True,
            )
            match status:
                case 201:
                    return "sealed"
                case 409:
                    return "late"
                case _:
                    print(f"[{minero}] rejected: {body}", file=sys.stderr)
                    return "invalid"
        lo = hi + 1
        if lo > MAX_NONCE:
            return "exhausted"
        # Someone else may have sealed the block: do not keep mining a dead task.
        if time.perf_counter() - last_check >= recheck_s:
            last_check = time.perf_counter()
            if coord.task()["task_id"] != task["task_id"]:
                return "stale"


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--url", default=os.environ.get("COORDINADOR_URL", "http://localhost:8000")
    )
    p.add_argument("--minero", required=True, help="miner name shown in the ranking")
    p.add_argument(
        "--chunk", type=int, default=50_000_000, help="nonces per miner call"
    )
    p.add_argument(
        "--start",
        type=int,
        default=0,
        help="first nonce (split ranges between workers)",
    )
    p.add_argument(
        "--recheck", type=float, default=2.0, help="seconds between task checks"
    )
    p.add_argument("--once", action="store_true", help="mine a single task and exit")
    p.add_argument("cmd", nargs=argparse.REMAINDER, help="miner command, after --")
    a = p.parse_args()
    cmd = [c for c in a.cmd if c != "--"]
    if not cmd:
        p.error("give the miner command after --")
    coord = Coordinator(a.url, os.environ.get("CADENA_TOKEN"))

    while True:
        task = coord.task()
        outcome = mine_task(coord, cmd, task, a.minero, a.chunk, a.start, a.recheck)
        print(f"[{a.minero}] task {task['task_id']}: {outcome}", flush=True)
        if a.once:
            return 0 if outcome == "sealed" else 1


if __name__ == "__main__":
    sys.exit(main())
