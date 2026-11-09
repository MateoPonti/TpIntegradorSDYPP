"""Option b) for the group k3s: your own autoscaler instead of KEDA.

Polls GET {GATEWAY_URL}/scaling (the signal the pool publishes) and runs
`kubectl scale` on the miner-cpu Deployment of the LOCAL k3s. Run it on the
machine that hosts the k3s, never in GKE:

    GATEWAY_URL=http://<gateway-ip> MINER_TOKEN=... \
    KUBECONFIG=~/.kube/k3s.yaml python3 examples/local_autoscaler.py

If you use it, remove keda-scaledobject.yaml from k3s-local/kustomization.yaml:
two autoscalers on the same Deployment fight each other.
Standard library only.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request


def replicas_for(signal: dict, min_replicas: int, max_replicas: int) -> int:
    """Clamp the pool's desired_cpu_miners to the limits of this machine."""
    desired = int(signal.get("desired_cpu_miners", 0))
    return max(min_replicas, min(max_replicas, desired))


def read_signal(gateway_url: str, token: str, timeout: float = 5.0) -> dict:
    req = urllib.request.Request(
        f"{gateway_url.rstrip('/')}/scaling",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def scale(replicas: int, namespace: str = "tpi-miners") -> None:
    subprocess.run(
        [
            "kubectl",
            "-n",
            namespace,
            "scale",
            "deployment/miner-cpu",
            f"--replicas={replicas}",
        ],
        check=True,
    )


def main() -> None:
    gateway = os.environ["GATEWAY_URL"]
    token = os.environ["MINER_TOKEN"]
    lo = int(os.environ.get("MIN_REPLICAS", "0"))
    hi = int(os.environ.get("MAX_REPLICAS", "8"))
    period = float(os.environ.get("PERIOD_SECONDS", "15"))
    current = None
    while True:
        try:
            wanted = replicas_for(read_signal(gateway, token), lo, hi)
            if wanted != current:
                scale(wanted)
                print(f"{time.strftime('%H:%M:%S')} miner-cpu -> {wanted}", flush=True)
                current = wanted
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            # Gateway down or kubectl failed: keep the current size, retry later.
            print(f"{time.strftime('%H:%M:%S')} skip: {exc}", flush=True)
        time.sleep(period)


if __name__ == "__main__":
    main()
