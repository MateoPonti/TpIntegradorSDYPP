import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

from local_autoscaler import replicas_for  # noqa: E402


def test_follows_the_pool_signal():
    assert replicas_for({"desired_cpu_miners": 3}, 0, 8) == 3


def test_clamped_to_local_limits():
    assert replicas_for({"desired_cpu_miners": 20}, 0, 4) == 4
    assert replicas_for({"desired_cpu_miners": 0}, 1, 4) == 1


def test_missing_field_means_zero():
    assert replicas_for({}, 0, 4) == 0
