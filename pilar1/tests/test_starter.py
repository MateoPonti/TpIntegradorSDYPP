"""Tests for the Pilar 1 starter kit. Run: python3 -m pytest -q tests/"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ORACLE = [sys.executable, str(ROOT / "oracle" / "miner_cpu.py")]
sys.path.insert(0, str(ROOT / "oracle"))
sys.path.insert(0, str(ROOT / "tools"))

import measure_energy  # noqa: E402
import worker_adapter  # noqa: E402
from miner_cpu import search  # noqa: E402


def run_oracle(*args: str) -> dict:
    out = subprocess.run(
        ORACLE + list(args), capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_rfc1321_vector() -> None:
    assert run_oracle("--hash", "abc")["hash"] == "900150983cd24fb0d6963f7d28e17f72"


def test_found_nonce_is_valid_and_lowest() -> None:
    res = run_oracle(
        "--base", "abc", "--difficulty", "3", "--from", "0", "--to", "200000"
    )
    assert res["found"]
    h = hashlib.md5(f"abc{res['nonce']}".encode()).hexdigest()
    assert h == res["hash"] and h.startswith("000")
    assert not search("abc", "000", 0, res["nonce"] - 1)["found"]


def test_range_without_solution_reports_not_found() -> None:
    res = run_oracle(
        "--base", "abc", "--prefix", "000000", "--from", "0", "--to", "500"
    )
    assert res == {"found": False, "hashes": 501, "seconds": res["seconds"]}


def test_bad_arguments_exit_2() -> None:
    proc = subprocess.run(
        ORACLE + ["--base", "x", "--prefix", "zz"], capture_output=True
    )
    assert proc.returncode == 2


def test_contract_checker_passes_oracle() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_contract.py"), "--", *ORACLE],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout


def test_contract_checker_catches_lying_miner(tmp_path: Path) -> None:
    liar = tmp_path / "liar.py"
    liar.write_text(
        "import json\nprint(json.dumps({'found': True, 'nonce': 1, 'hash': '0'*32, "
        "'hashes': 1, 'seconds': 0.1}))\n"
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "check_contract.py"),
            "--skip-hash",
            "--",
            sys.executable,
            str(liar),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "hash mismatch" in proc.stdout


def test_trapezoid_integration() -> None:
    samples = [(0.0, 100.0), (1.0, 100.0), (2.0, 200.0)]
    assert measure_energy.integrate(samples, 0.0, 2.0) == pytest.approx(250.0)
    with pytest.raises(ValueError):
        measure_energy.integrate(samples[:1], 0.0, 2.0)


class FakeCoordinator(BaseHTTPRequestHandler):
    """Minimal stand-in for the cátedra coordinator protocol."""

    task = {
        "task_id": "t-1",
        "block_index": 7,
        "previous_hash": "000" + "a" * 29,
        "data": "bloque 7 · A->B:10",
        "difficulty": 3,
    }
    sealed: list[dict] = []

    def log_message(self, *_: object) -> None:
        return

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        self._send(200, self.task)

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        t = self.task
        h = hashlib.md5(
            f"{t['previous_hash']}{t['data']}{body['nonce']}".encode()
        ).hexdigest()
        if body["task_id"] != t["task_id"]:
            return self._send(409, {"detail": "late"})
        if h != body["hash"] or not h.startswith("0" * t["difficulty"]):
            return self._send(400, {"detail": "invalid"})
        self.sealed.append(body)
        return self._send(201, {"detail": "sealed"})


@pytest.fixture
def coordinator() -> str:
    FakeCoordinator.sealed = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeCoordinator)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_adapter_seals_block_with_small_chunks(coordinator: str) -> None:
    coord = worker_adapter.Coordinator(coordinator)
    task = coord.task()
    # Tiny chunks force several miner calls before the solution shows up.
    outcome = worker_adapter.mine_task(coord, ORACLE, task, "g-test", 2000, 0, 60.0)
    assert outcome == "sealed"
    sealed = FakeCoordinator.sealed[0]
    assert sealed["block_index"] == 7 and sealed["minero"] == "g-test"


def test_adapter_detects_stale_task(coordinator: str) -> None:
    coord = worker_adapter.Coordinator(coordinator)
    old = dict(FakeCoordinator.task, task_id="t-0", difficulty=8)
    outcome = worker_adapter.mine_task(coord, ORACLE, old, "g-test", 500, 0, 0.0)
    assert outcome == "stale"
