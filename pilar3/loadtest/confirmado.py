"""Throughput confirmado de transacciones de usuario en bloques sellados dentro de [desde, hasta]."""
import argparse
import json
import math
from urllib.request import urlopen


def get_json(api: str, path: str):
    with urlopen(api.rstrip("/") + path, timeout=30) as response:
        return json.load(response)


def summarize(blocks, desde: float, hasta: float) -> dict:
    if not math.isfinite(desde) or not math.isfinite(hasta) or desde < 0 or hasta <= desde:
        raise ValueError("intervalo inválido: 0 <= desde < hasta, ambos finitos")
    first, last, count = None, None, 0
    for block in blocks:
        sealed = float(block["timestamp"])
        if block["index"] == 0 or not desde <= sealed <= hasta:
            continue
        users = [tx for tx in block["transactions"] if tx["sender"] not in ("recompensa", "genesis")]
        if not users:
            continue
        stamps = [float(tx["ts"]) for tx in users]
        if any(not math.isfinite(ts) or ts < 0 or ts > sealed for ts in stamps):
            raise ValueError(f"timestamps de transacciones inválidos en bloque {block['index']}")
        count += len(users)
        first = min(stamps) if first is None else min(first, *stamps)
        last = sealed if last is None else max(last, sealed)
    elapsed = last - first if count else 0
    if count and elapsed <= 0:
        raise ValueError("duración de confirmación nula: no se puede calcular throughput")
    return {"tx_confirmadas": count, "primera_tx_ts": first, "ultimo_sello_ts": last,
            "throughput_confirmado_tx_s": count / elapsed if count else 0.0}


def measure(api: str, desde: float, hasta: float, fetch=get_json) -> dict:
    summarize([], desde, hasta)  # validar antes de hacer pedidos
    blocks, start, height = [], 0, None
    while height is None or start < height:
        page = fetch(api, f"/blocks?start={start}&limit=100")
        if height is None:
            height = int(page["height"])
        for summary in page["blocks"]:
            if 0 < summary["index"] < height and desde <= float(summary["timestamp"]) <= hasta:
                # /blocks devuelve encabezados; las tx y sus ts están en /blocks/{index}.
                blocks.append(fetch(api, f"/blocks/{summary['index']}"))
        start += 100
    return summarize(blocks, desde, hasta)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True)
    parser.add_argument("--desde", type=float, required=True, help="Unix timestamp, segundos")
    parser.add_argument("--hasta", type=float, required=True, help="Unix timestamp, segundos")
    args = parser.parse_args(argv)
    try:
        result = measure(args.api, args.desde, args.hasta)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
