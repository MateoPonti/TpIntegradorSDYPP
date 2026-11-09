"""Turns results.csv (from bench.py) into the comparative charts of section 3.3.

    python plot.py results.csv --out charts/

One PNG per experiment: time vs bulk size (one line per difficulty), time vs
difficulty, time vs fragment size, and time vs number of GPU miners.
"""

from __future__ import annotations

import argparse
import csv
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def load(path: Path) -> list[dict]:
    with path.open() as fh:
        rows = [r for r in csv.DictReader(fh) if r["confirm_seconds"]]
    for r in rows:
        r["bulk"] = int(r["bulk"])
        r["fragment_percent"] = float(r["fragment_percent"])
        r["gpu_miners"] = int(r["gpu_miners"])
        r["confirm_seconds"] = float(r["confirm_seconds"])
    return rows


def median_by(rows: list[dict], x: str, series: str) -> dict:
    groups: dict = defaultdict(lambda: defaultdict(list))
    for r in rows:
        groups[r[series]][r[x]].append(r["confirm_seconds"])
    return {s: sorted((k, statistics.median(v)) for k, v in pts.items()) for s, pts in groups.items()}


def chart(rows: list[dict], x: str, series: str, title: str, out: Path, logx: bool = False) -> None:
    fig, ax = plt.subplots(figsize=(7, 4))
    for name, pts in sorted(median_by(rows, x, series).items(), key=lambda kv: str(kv[0])):
        ax.plot([p[0] for p in pts], [p[1] for p in pts], marker="o", label=f"{series}={name}")
    if logx:
        ax.set_xscale("log")
    ax.set_xlabel(x)
    ax.set_ylabel("confirm time, median (s)")
    ax.set_title(title)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("csv", type=Path)
    p.add_argument("--out", type=Path, default=Path("charts"))
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    rows = load(a.csv)
    chart(rows, "bulk", "difficulty", "Scalability by bulk size", a.out / "bulk.png", logx=True)
    chart(rows, "difficulty", "bulk", "Effect of the hash prefix", a.out / "difficulty.png")
    chart(rows, "fragment_percent", "difficulty", "Pool fragment size", a.out / "fragment.png")
    chart(rows, "gpu_miners", "difficulty", "GPU miners joining and leaving", a.out / "gpu.png")
    print("charts written to", a.out)


if __name__ == "__main__":
    main()
