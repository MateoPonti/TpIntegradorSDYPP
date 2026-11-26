import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "loadtest/confirmado.py"
spec = importlib.util.spec_from_file_location("confirmado", PATH)
confirmado = importlib.util.module_from_spec(spec)
spec.loader.exec_module(confirmado)


def block(index, sealed, times):
    return {"index": index, "timestamp": sealed, "transactions": [
        {"sender": "recompensa", "ts": 0}, *[{"sender": "user-1", "ts": ts} for ts in times]]}


def test_throughput_confirmado_tres_bloques_dos_en_ventana():
    chain = [block(1, 9, [1]), block(2, 20, [10, 12]), block(3, 30, [22])]
    result = confirmado.summarize(chain, 15, 30)
    assert result == {"tx_confirmadas": 3, "primera_tx_ts": 10, "ultimo_sello_ts": 30,
                      "throughput_confirmado_tx_s": .15}


def test_sin_usuario_no_inventa_mediciones():
    assert confirmado.summarize([block(0, 0, [0]), block(1, 20, [])], 0, 30) == {
        "tx_confirmadas": 0, "primera_tx_ts": None, "ultimo_sello_ts": None, "throughput_confirmado_tx_s": 0.0}


@pytest.mark.parametrize("desde,hasta", [(5, 5), (5, 4), (-1, 5), (0, float("inf")), (float("nan"), 5)])
def test_intervalos_invalidos(desde, hasta):
    with pytest.raises(ValueError):
        confirmado.summarize([], desde, hasta)


def test_pagina_encabezados_y_trae_solo_bloques_en_intervalo():
    called = []
    def fetch(api, path):
        called.append(path)
        if path == "/blocks?start=0&limit=100":
            return {"height": 102, "blocks": [{"index": i, "timestamp": i} for i in range(100)]}
        if path == "/blocks?start=100&limit=100":
            return {"height": 102, "blocks": [{"index": 100, "timestamp": 100}, {"index": 101, "timestamp": 101}]}
        return block(int(path.rsplit("/", 1)[1]), 101, [99])
    assert confirmado.measure("http://test", 100.5, 102, fetch)["throughput_confirmado_tx_s"] == .5
    assert called == ["/blocks?start=0&limit=100", "/blocks?start=100&limit=100", "/blocks/101"]


def test_rechaza_timestamp_de_tx_posterior_al_sello():
    with pytest.raises(ValueError):
        confirmado.summarize([block(1, 20, [21])], 0, 30)


def test_resumen_medianas_rangos_y_speedup():
    spec = importlib.util.spec_from_file_location("resumen", PATH.with_name("resumen_escalado.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = [{"cpu_miners": cpu, "bulk": 100, "difficulty": "0000000", "fragment_percent": 10,
             "tx_confirmadas": 100, "throughput_ingesta_tx_s": 50,
             "throughput_confirmado_tx_s": value}
            for cpu, values in ((4, [9, 10, 11]), (8, [17, 18, 19])) for value in values]
    points, speedup = module.summarize(rows)
    assert speedup == 1.8
    assert points[0]["confirmado_mediana"] == 10
    assert (points[1]["confirmado_min"], points[1]["confirmado_max"]) == (17, 19)
    with pytest.raises(ValueError):
        module.summarize(rows[:-1])
