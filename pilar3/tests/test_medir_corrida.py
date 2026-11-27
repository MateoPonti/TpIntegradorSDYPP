import importlib.util
from pathlib import Path
import sys

import pytest

LOADTEST = Path(__file__).resolve().parents[1] / "loadtest"
sys.path.insert(0, str(LOADTEST))  # medir_corrida importa confirmado por nombre
spec = importlib.util.spec_from_file_location("medir_corrida", LOADTEST / "medir_corrida.py")
medir = importlib.util.module_from_spec(spec)
spec.loader.exec_module(medir)


def test_wait_sealed_espera_el_bloque_en_minado(monkeypatch):
    # pending llega a 0 cuando las tx entran al bloque que se mina, no cuando se sella (27/11: 578 de 1000)
    states = iter([{"pending_transactions": 0, "mining": {"block_id": "800-a"}},
                   {"pending_transactions": 0, "mining": {"block_id": "800-a"}},
                   {"pending_transactions": 0, "mining": None}])
    calls = []
    monkeypatch.setattr(medir.confirmado, "get_json", lambda api, path: calls.append(path) or next(states))
    monkeypatch.setattr(medir.time, "sleep", lambda s: None)
    medir.wait_sealed("https://x")
    assert calls == ["/status"] * 3


def test_wait_sealed_corta_por_timeout(monkeypatch):
    monkeypatch.setattr(medir.confirmado, "get_json", lambda api, path: {"pending_transactions": 0, "mining": {"block_id": "x"}})
    monkeypatch.setattr(medir.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="no se selló"):
        medir.wait_sealed("https://x", timeout=0)
