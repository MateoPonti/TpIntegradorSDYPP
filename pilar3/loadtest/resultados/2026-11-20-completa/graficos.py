"""Gráficos de las pruebas de carga del 20/11/2026: CPU contra GPU en cada experimento y la entrada y salida de la GPU.

    python graficos.py            (desde esta carpeta; deja los PNG en charts/ y la tabla en resumen.md)

Lee los CSV de bench.py (mediana de las corridas de cada punto) y entrada_salida.csv (monitor de /status cada 2 s).
"""
import csv
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "charts"
COLORS = {"CPU (8 pods)": "#2b6cb0", "GPU (1050 Ti)": "#c05621"}


def medians(name: str, x: str) -> dict:
    path = HERE / name
    if not path.exists():
        return {}
    acc = defaultdict(list)
    errors = defaultdict(int)
    for r in csv.DictReader(path.open(encoding="utf-8")):
        key = r[x] if x == "difficulty" else float(r[x])
        acc[key].append(float(r["confirm_seconds"]))
        errors[key] += int(r["submit_errors"])
    return {k: (statistics.median(v), len(v), errors[k]) for k, v in acc.items()}


def compare(files: dict, x: str, title: str, xlabel: str, out: str, logx=False, logy=False, labels=None):
    fig, ax = plt.subplots(figsize=(7, 4.2))
    for label, name in files.items():
        data = medians(name, x)
        if not data:
            continue
        keys = sorted(data, key=(len if x == "difficulty" else float))
        xs = [len(k) for k in keys] if x == "difficulty" else keys
        ax.plot(xs, [data[k][0] for k in keys], marker="o", label=label, color=COLORS[label])
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("segundos hasta confirmar todas (mediana)")
    if logx:
        ax.set_xscale("log")
    if logy:
        ax.set_yscale("log")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(OUT / out, dpi=130)
    plt.close(fig)


def timeline():
    path = HERE / "entrada_salida.csv"
    if not path.exists():
        return
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    t = [float(r["t"]) for r in rows]
    fig, (a1, a2, a3) = plt.subplots(3, 1, figsize=(8, 7), sharex=True)
    a1.plot(t, [len(r["prefix_plan"]) for r in rows], drawstyle="steps-post", color="#2f855a")
    a1.set_ylabel("prefijo del plan\n(ceros)")
    a2.plot(t, [int(r["gpu"]) for r in rows], drawstyle="steps-post", label="GPU", color=COLORS["GPU (1050 Ti)"])
    a2.plot(t, [int(r["cpu"]) for r in rows], drawstyle="steps-post", label="pods CPU", color=COLORS["CPU (8 pods)"])
    a2.set_ylabel("mineros vivos")
    a2.legend(loc="upper right")
    a3.plot(t, [int(r["pending"]) for r in rows], color="#6b46c1")
    a3.set_ylabel("tx pendientes")
    a3.set_xlabel("segundos desde el inicio (5 tx/s todo el tiempo)")
    for a in (a1, a2, a3):
        a.axvline(97, color="#c05621", ls="--", lw=1)
        a.axvline(245, color="#c05621", ls=":", lw=1)
        a.grid(True, alpha=0.3)
    a1.text(99, 7.3, "entra la GPU", color="#c05621", fontsize=9)
    a1.text(247, 7.3, "sale la GPU", color="#c05621", fontsize=9)
    fig.suptitle("Entrada y salida de la GPU (prefijo automático)")
    fig.tight_layout()
    fig.savefig(OUT / "entrada_salida.png", dpi=130)
    plt.close(fig)


def table() -> str:
    lines = []
    for title, x, files in (
        ("Bulks, prefijo automático, fragmentación 10 %", "bulk", ("bulks_cpu.csv", "bulks_gpu.csv")),
        ("Bulks, prefijo fijo 0000000, fragmentación 10 %", "bulk", ("mismo_prefijo_cpu.csv", "mismo_prefijo_gpu.csv")),
        ("Prefijos fijos, bulk 100, fragmentación 10 %", "difficulty", ("prefijos_cpu.csv", "prefijos_gpu.csv")),
        ("Fragmentación (CPU con prefijo 0000000, GPU con 00000000), bulk 100", "fragment_percent",
         ("fragmentos_cpu.csv", "fragmentos_gpu.csv")),
    ):
        cpu, gpu = medians(files[0], x), medians(files[1], x)
        keys = sorted(set(cpu) | set(gpu), key=(len if x == "difficulty" else float))
        name = {"bulk": "Bulk", "difficulty": "Prefijo", "fragment_percent": "Fragmentación %"}[x]
        lines += [f"### {title}", "", f"| {name} | CPU (s) | GPU (s) | Corridas CPU/GPU | Errores CPU/GPU |",
                  "|---|---|---|---|---|"]
        for k in keys:
            c, g = cpu.get(k), gpu.get(k)
            fmt = lambda v: f"{v[0]:.1f}".replace(".", ",") if v else "—"  # noqa: E731
            label = k if x == "difficulty" else (f"{int(k):,}".replace(",", ".") if x == "bulk" else f"{k:g}")
            lines.append(f"| {label} | {fmt(c)} | {fmt(g)} | {c[1] if c else 0}/{g[1] if g else 0} | "
                         f"{c[2] if c else 0}/{g[2] if g else 0} |")
        lines.append("")
    return "\n".join(lines)


def main():
    OUT.mkdir(exist_ok=True)
    compare({"CPU (8 pods)": "bulks_cpu.csv", "GPU (1050 Ti)": "bulks_gpu.csv"}, "bulk",
            "Bulks con prefijo automático", "tx en el bulk", "bulks_auto.png", logx=True, logy=True)
    compare({"CPU (8 pods)": "mismo_prefijo_cpu.csv", "GPU (1050 Ti)": "mismo_prefijo_gpu.csv"}, "bulk",
            "Bulks con el mismo prefijo (0000000)", "tx en el bulk", "bulks_mismo_prefijo.png", logx=True, logy=True)
    compare({"CPU (8 pods)": "prefijos_cpu.csv", "GPU (1050 Ti)": "prefijos_gpu.csv"}, "difficulty",
            "Prefijo fijo, bulk de 100 tx", "ceros del prefijo", "prefijos.png", logy=True)
    compare({"CPU (8 pods)": "fragmentos_cpu.csv", "GPU (1050 Ti)": "fragmentos_gpu.csv"}, "fragment_percent",
            "Fragmentación, bulk de 100 tx", "fragmentación (% de 16^largo)", "fragmentos.png")
    timeline()
    (HERE / "resumen.md").write_text(table(), encoding="utf-8")
    print("ok")


if __name__ == "__main__":
    main()
