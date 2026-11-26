"""Compara ingesta y confirmación de las tres corridas con 4 y 8 mineros."""
import argparse
import csv
from pathlib import Path
import statistics


def summarize(rows):
    points = []
    conditions = {(row["bulk"], row["difficulty"], row["fragment_percent"]) for row in rows}
    if len(conditions) != 1:
        raise ValueError("todas las corridas deben usar igual carga, prefijo y fragmentación")
    for cpu in (4, 8):
        group = [row for row in rows if int(row["cpu_miners"]) == cpu]
        if len(group) != 3 or any(int(row["tx_confirmadas"]) != int(row["bulk"]) for row in group):
            raise ValueError(f"se necesitan 3 corridas completas con {cpu} mineros")
        point = {"cpu_miners": cpu}
        for name in ("ingesta", "confirmado"):
            values = [float(row[f"throughput_{name}_tx_s"]) for row in group]
            point.update({f"{name}_mediana": statistics.median(values),
                          f"{name}_min": min(values), f"{name}_max": max(values)})
        points.append(point)
    if len(rows) != 6 or points[0]["confirmado_mediana"] <= 0:
        raise ValueError("corridas extra o throughput de referencia nulo")
    return points, points[1]["confirmado_mediana"] / points[0]["confirmado_mediana"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    rows = []
    for cpu in (4, 8):
        with (args.directory / f"cpu-{cpu}/confirmados.csv").open() as handle:
            rows.extend(csv.DictReader(handle))
    points, speedup = summarize(rows)
    with (args.directory / "resumen.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(points[0]))
        writer.writeheader()
        writer.writerows(points)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    figure, axes = plt.subplots(1, 2, figsize=(10, 4), layout="constrained")
    for axis, name in zip(axes, ("ingesta", "confirmado")):
        medians = [p[f"{name}_mediana"] for p in points]
        errors = [[p[f"{name}_mediana"] - p[f"{name}_min"] for p in points],
                  [p[f"{name}_max"] - p[f"{name}_mediana"] for p in points]]
        axis.bar(["4 CPU", "8 CPU"], medians, yerr=errors, capsize=6, color=["#3376a8", "#1b8a72"])
        axis.set(title=f"Throughput {name}", ylabel="tx/s (mediana, mínimo–máximo)")
        axis.grid(axis="y", alpha=.2)
    figure.savefig(args.directory / "escalado.png", dpi=160)
    plt.close(figure)
    (args.directory / "README.md").write_text(
        "# Comparación de 4 y 8 mineros CPU\n\n"
        f"Carga: {rows[0]['bulk']} tx; prefijo fijo `{rows[0]['difficulty']}`; "
        f"fragmentación {rows[0]['fragment_percent']} %. Tres corridas por punto.\n\n"
        f"Speedup confirmado (8/4): **{speedup:.3f}×** frente al ideal de 2×; "
        f"eficiencia: **{100 * speedup / 2:.1f} %**.\n\n"
        "![Ingesta y confirmación](escalado.png)\n\n"
        "`bench.py` mide ingesta (HTTP 202). Confirmación cuenta tx de usuario en bloques sellados y divide "
        "por el tiempo desde la primera admisión hasta el último sello. Los premios y el génesis no cuentan.\n\n"
        "Interpretar junto a métricas: ventana fija de 10 s, un bloque en minado a la vez y sellado secuencial "
        "pueden limitar el escalado. Estos son factores a investigar, no causas demostradas por el gráfico. "
        "Completar el informe con CPU/RAM, imágenes desplegadas, relojes, métricas y limitaciones observadas.\n")
    print(f"Speedup confirmado: {speedup:.3f}×; archivos en {args.directory}")


if __name__ == "__main__":
    main()
