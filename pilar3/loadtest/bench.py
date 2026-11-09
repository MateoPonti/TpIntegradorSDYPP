"""Load test for section 3.3 of the TPI (Pilar 3).

Sends a bulk of N transactions to the coordinator and measures:
  * submit time: how long it takes to get all of them accepted (HTTP 2xx)
  * confirm time: how long until the pool reports zero pending transactions
    (that is, until every transaction ended up inside a mined block)

It only needs two endpoints from your coordinator (adapt the paths with flags):
  POST {base}/transactions   body {"sender": str, "receiver": str, "amount": float}
  GET  {base}/status         body with an integer field "pending_transactions"

Standard library only, so it runs anywhere Python 3.10+ is installed.
Results are appended to a CSV that plot.py turns into the comparative charts.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Result:
    bulk: int
    difficulty: str
    fragment_percent: float
    gpu_miners: int
    cpu_miners: int
    submitted_ok: int
    submit_errors: int
    submit_seconds: float
    confirm_seconds: float | None
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    throughput_tx_per_s: float


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile; returns 0.0 for an empty list."""
    if not values:
        return 0.0
    if not 0 < pct <= 100:
        raise ValueError("pct must be in (0, 100]")
    ordered = sorted(values)
    rank = max(1, round(pct / 100 * len(ordered) + 0.4999))
    return ordered[min(rank, len(ordered)) - 1]


def make_transaction(i: int) -> dict:
    return {
        "sender": f"user-{i % 97}",
        "receiver": f"user-{(i * 7 + 3) % 97}",
        "amount": round(random.uniform(1, 100), 2),
    }


def post_json(url: str, payload: dict, timeout: float) -> tuple[bool, float]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = 200 <= resp.status < 300
    except OSError:
        ok = False
    return ok, (time.perf_counter() - start) * 1000


def pending(url: str, timeout: float) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return int(json.load(resp)["pending_transactions"])
    except (OSError, ValueError, KeyError):
        return None


def run(args: argparse.Namespace, bulk: int) -> Result:
    tx_url = args.base_url.rstrip("/") + args.tx_path
    status_url = args.base_url.rstrip("/") + args.status_path

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        outcomes = list(pool.map(lambda i: post_json(tx_url, make_transaction(i), args.timeout), range(bulk)))
    submit_seconds = time.perf_counter() - start

    latencies = [ms for ok, ms in outcomes if ok]
    ok_count = len(latencies)

    confirm_seconds = None
    deadline = start + args.confirm_timeout
    while time.perf_counter() < deadline:
        left = pending(status_url, args.timeout)
        if left == 0:
            confirm_seconds = time.perf_counter() - start
            break
        time.sleep(args.poll_interval)

    return Result(
        bulk=bulk,
        difficulty=args.difficulty,
        fragment_percent=args.fragment,
        gpu_miners=args.gpu_miners,
        cpu_miners=args.cpu_miners,
        submitted_ok=ok_count,
        submit_errors=bulk - ok_count,
        submit_seconds=round(submit_seconds, 3),
        confirm_seconds=None if confirm_seconds is None else round(confirm_seconds, 3),
        latency_p50_ms=round(percentile(latencies, 50), 1),
        latency_p95_ms=round(percentile(latencies, 95), 1),
        latency_p99_ms=round(percentile(latencies, 99), 1),
        throughput_tx_per_s=round(ok_count / submit_seconds, 1) if submit_seconds else 0.0,
    )


def append_csv(path: Path, result: Result) -> None:
    row = asdict(result)
    new = not path.exists()
    with path.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(row))
        if new:
            writer.writeheader()
        writer.writerow(row)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", required=True, help="public URL of the coordinator, e.g. http://34.1.2.3")
    p.add_argument("--bulks", default="1,10,100,1000,10000,100000", help="comma separated bulk sizes")
    p.add_argument("--repeat", type=int, default=3, help="runs per bulk size (report the median, not one run)")
    p.add_argument("--concurrency", type=int, default=32)
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--confirm-timeout", type=float, default=1800.0)
    p.add_argument("--poll-interval", type=float, default=1.0)
    p.add_argument("--tx-path", default="/transactions")
    p.add_argument("--status-path", default="/status")
    # the following are LABELS for the CSV: set them to what is really deployed
    p.add_argument("--difficulty", default="000", help="current hash prefix, as deployed")
    p.add_argument("--fragment", type=float, default=10.0, help="current pool fragment size in percent")
    p.add_argument("--gpu-miners", type=int, default=0)
    p.add_argument("--cpu-miners", type=int, default=0)
    p.add_argument("--out", type=Path, default=Path("results.csv"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    for bulk in (int(b) for b in args.bulks.split(",")):
        runs = [run(args, bulk) for _ in range(args.repeat)]
        for r in runs:
            append_csv(args.out, r)
        confirmed = [r.confirm_seconds for r in runs if r.confirm_seconds is not None]
        median = statistics.median(confirmed) if confirmed else None
        print(f"bulk={bulk:>6} difficulty={args.difficulty} fragment={args.fragment}% "
              f"confirm_median_s={median} errors={sum(r.submit_errors for r in runs)}")


if __name__ == "__main__":
    main()
