import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

from pool_scaler import (  # noqa: E402
    SIGNAL_KEY,
    desired_cpu_miners,
    effective_prefix,
    publish_signal,
    scaling_signal,
)


def test_prefix_kept_with_gpu():
    assert effective_prefix("00000", gpu_alive=1, min_without_gpu=2) == "00000"


def test_prefix_lowered_without_gpu():
    assert effective_prefix("00000", gpu_alive=0, min_without_gpu=2) == "00"


def test_prefix_never_raised_nor_empty():
    assert effective_prefix("0", gpu_alive=0, min_without_gpu=3) == "0"
    assert effective_prefix("000", gpu_alive=0, min_without_gpu=0) == "0"


def test_gpu_alive_scales_cpu_miners_to_zero():
    assert (
        desired_cpu_miners(gpu_alive=2, pending_tasks=500, current=4, max_miners=5) == 0
    )


def test_no_gpu_scales_with_pending_work_and_caps():
    assert desired_cpu_miners(0, pending_tasks=45, current=0, max_miners=5) == 3
    assert desired_cpu_miners(0, pending_tasks=10_000, current=0, max_miners=5) == 5
    assert desired_cpu_miners(0, pending_tasks=1, current=0, max_miners=5) == 1


def test_no_gpu_idle_keeps_current_count():
    assert desired_cpu_miners(0, pending_tasks=0, current=3, max_miners=5) == 3


def test_scaling_signal_without_gpu():
    s = scaling_signal("00000", 0, 1, 45, max_miners=8, min_without_gpu=3)
    assert s == {
        "gpu_alive": 0,
        "cpu_alive": 1,
        "pending_tasks": 45,
        "prefix": "000",
        "desired_cpu_miners": 3,
    }


def test_scaling_signal_with_gpu():
    s = scaling_signal("00000", 1, 4, 45, max_miners=8, min_without_gpu=3)
    assert s["prefix"] == "00000"
    assert s["desired_cpu_miners"] == 0


class FakeRedis:
    def __init__(self):
        self.calls = []

    def set(self, key, value, ex=None):
        self.calls.append((key, value, ex))


def test_publish_signal_sets_key_with_ttl():
    r = FakeRedis()
    publish_signal(r, {"desired_cpu_miners": 2}, ttl_seconds=30)
    key, value, ex = r.calls[0]
    assert key == SIGNAL_KEY and ex == 30
    assert json.loads(value) == {"desired_cpu_miners": 2}
