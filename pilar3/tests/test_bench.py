import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "loadtest"))

import bench  # noqa: E402


def test_percentile_nearest_rank():
    data = list(range(1, 101))
    assert bench.percentile(data, 50) == 50
    assert bench.percentile(data, 95) == 95
    assert bench.percentile(data, 100) == 100


def test_percentile_empty_and_invalid():
    assert bench.percentile([], 50) == 0.0
    with pytest.raises(ValueError):
        bench.percentile([1.0], 0)


def test_transaction_shape():
    tx = bench.make_transaction(5)
    assert set(tx) == {"sender", "receiver", "amount"}
    assert tx["sender"] != tx["receiver"]


def test_csv_header_written_once(tmp_path):
    out = tmp_path / "r.csv"
    r = bench.Result(1, "00", 10.0, 0, 1, 1, 0, 0.1, 0.5, 1.0, 1.0, 1.0, 10.0)
    bench.append_csv(out, r)
    bench.append_csv(out, r)
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("bulk,")
