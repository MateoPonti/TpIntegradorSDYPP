"""Envuelve bench.py sin modificarlo: guarda confirmación real por cada una de sus corridas."""
import csv
import importlib.util
import json
from pathlib import Path
import sys
import time

import confirmado

# El sellado guarda su hora un instante después de que /status deja de mostrar el bloque en minado (27/11: el
# bloque 811 quedó 0,5 s después del cierre). La corrida siguiente no sella nada antes de 10 s, así que no se mezclan.
CLOCK_SLACK = 2.0


def wait_ready(api, cpu, prefix, timeout=180):
    deadline = time.monotonic() + timeout
    while True:
        state = confirmado.get_json(api, "/status")
        miners, plan = state.get("miners", {}), state.get("pool_plan") or {}
        if miners.get("gpu", 0):
            raise RuntimeError("hay GPU conectada: la comparación CPU no es válida")
        if (miners.get("cpu") == cpu and plan.get("mode") == "fixed" and plan.get("prefix") == prefix
                and state.get("pending_transactions") == 0 and not state.get("mining")):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("no se estabilizaron mineros, dificultad fija y cola vacía")
        time.sleep(2)


def wait_sealed(api, timeout=600):
    """bench.py termina cuando pending_transactions llega a 0, pero una tx deja de estar pendiente al entrar en el
    bloque que se está minando, no al sellarse: con prefijos largos el último bloque se sella después."""
    deadline = time.monotonic() + timeout
    while True:
        state = confirmado.get_json(api, "/status")
        if state.get("pending_transactions") == 0 and not state.get("mining"):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("el último bloque no se selló a tiempo")
        time.sleep(1)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    script, *args = argv
    spec = importlib.util.spec_from_file_location("tpi_bench_original", script)
    bench = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bench
    spec.loader.exec_module(bench)
    original = bench.run
    run_number = 0

    def measured(options, bulk):
        nonlocal run_number
        wait_ready(options.base_url, options.cpu_miners, options.difficulty)
        run_number += 1
        since = time.time()
        result = original(options, bulk)
        wait_sealed(options.base_url)
        until = time.time()
        if result.submit_errors or result.confirm_seconds is None:
            raise RuntimeError("corrida inválida: hubo errores de ingesta o timeout de confirmación")
        status = confirmado.get_json(options.base_url, "/status")
        if status["miners"]["gpu"] or status["miners"]["cpu"] != options.cpu_miners:
            raise RuntimeError("cambió la cantidad de mineros durante la corrida")
        confirmed = confirmado.measure(options.base_url, since, until + CLOCK_SLACK)
        row = {"cpu_miners": options.cpu_miners, "run": run_number, "bulk": bulk,
               "difficulty": options.difficulty, "fragment_percent": options.fragment,
               "desde": since, "hasta": until, "throughput_ingesta_tx_s": result.throughput_tx_per_s,
               **confirmed}
        Path(f"corrida-{run_number}.json").write_text(json.dumps(row, indent=2) + "\n")
        if confirmed["tx_confirmadas"] != result.submitted_ok:
            raise RuntimeError("cantidad confirmada distinta de la aceptada: revisar reloj y tráfico ajeno")
        output = Path("confirmados.csv")
        empty = not output.exists()
        with output.open("a", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row))
            if empty:
                writer.writeheader()
            writer.writerow(row)
        return result

    bench.run = measured
    bench.main(args)


if __name__ == "__main__":
    main()
