#!/usr/bin/env python3
"""Measure hashes per joule for a miner run and print the leaderboard payload.

Usage (on the machine that has the GPU):
    python3 tools/measure_energy.py --equipo g-amarillo -- \
        ./build/miner_gpu --base "$(printf 'x%.0s' {1..40})" \
        --prefix ffffffffffffffff --from 0 --to 20000000000

The miner must follow the Pilar 1 contract (one JSON line with "hashes" and
"seconds"). While it runs, this script samples board power with
    nvidia-smi --query-gpu=power.draw --format=csv,noheader,nounits -lms 200
and integrates it with the trapezoidal rule: joules = sum(avg(P_i, P_i+1) * dt).

It NEVER posts anything. It prints the JSON body for
https://gpu-sd.impulsate.lat/api/resultado and you decide whether to send it.
Tip: use an impossible prefix (16 f's) and a huge range so the run lasts
~30 s and the kernel never exits early.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

NVSMI_POWER = [
    "nvidia-smi",
    "--query-gpu=power.draw",
    "--format=csv,noheader,nounits",
    "-lms",
    "200",
]


@dataclass
class PowerSampler:
    """Reads one watt value per line from a long-running command."""

    cmd: list[str]
    samples: list[tuple[float, float]] = field(default_factory=list)
    _proc: subprocess.Popen | None = None
    _thread: threading.Thread | None = None

    def start(self) -> None:
        self._proc = subprocess.Popen(
            self.cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            try:
                self.samples.append((time.monotonic(), float(line.strip())))
            except ValueError:
                continue  # "[N/A]" or an empty line

    def stop(self) -> None:
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._thread:
            self._thread.join(timeout=3)


def integrate(samples: list[tuple[float, float]], t0: float, t1: float) -> float:
    """Trapezoidal integral of power (W) over [t0, t1], in joules."""
    pts = [(t, w) for t, w in samples if t0 <= t <= t1]
    if len(pts) < 2:
        raise ValueError("not enough power samples inside the run window; run longer")
    return sum(
        (w0 + w1) / 2 * (ta1 - ta0) for (ta0, w0), (ta1, w1) in zip(pts, pts[1:])
    )


def gpu_name(index: int) -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.splitlines()
        return out[index].strip()
    except (OSError, subprocess.SubprocessError, IndexError):
        return "unknown GPU"


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--equipo", required=True, help="team name, e.g. g-amarillo")
    p.add_argument("--gpu", help="GPU label (default: nvidia-smi name)")
    p.add_argument("--gpu-index", type=int, default=0)
    p.add_argument("--algoritmo", default="md5")
    p.add_argument("--nota", default="", help="free text, max 200 chars")
    p.add_argument("--power-cmd", help="override the power sampler command (testing)")
    p.add_argument("cmd", nargs=argparse.REMAINDER, help="miner command, after --")
    a = p.parse_args()
    cmd = [c for c in a.cmd if c != "--"]
    if not cmd:
        p.error("give the miner command after --")

    label = a.gpu or gpu_name(a.gpu_index)
    sampler = PowerSampler(
        shlex.split(a.power_cmd)
        if a.power_cmd
        else [*NVSMI_POWER, "-i", str(a.gpu_index)]
    )
    sampler.start()
    time.sleep(0.6)  # let the sampler produce its first readings
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    t1 = time.monotonic()
    time.sleep(0.3)
    sampler.stop()

    if proc.returncode != 0:
        print(
            f"miner failed ({proc.returncode}): {proc.stderr.strip()}", file=sys.stderr
        )
        return 1
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    result = json.loads(lines[-1])
    if result.get("found"):
        print(
            "warning: the miner found a nonce and stopped early; use an impossible prefix",
            file=sys.stderr,
        )

    joules = integrate(sampler.samples, t0, t1)
    hashes = int(result["hashes"])
    seconds = float(result["seconds"])
    payload = {
        "equipo": a.equipo,
        "gpu": label,
        "algoritmo": a.algoritmo,
        "hashes": hashes,
        "segundos": round(seconds, 3),
        "joules": round(joules, 1),
        "potencia_media_w": round(joules / (t1 - t0), 1),
        "nota": a.nota[:200],
    }
    print(f"# wall time {t1 - t0:.2f} s, miner time {seconds:.2f} s", file=sys.stderr)
    print(
        f"# {hashes / seconds:,.0f} H/s   {hashes / joules:,.1f} H/J", file=sys.stderr
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
