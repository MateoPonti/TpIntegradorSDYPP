"""El ejecutor usa contextos explícitos y restaura configuración incluso si falla un punto."""
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "loadtest/escalado.sh"
FAKE_KUBECTL = r'''
import json, os, pathlib, sys
p = pathlib.Path(os.environ["FAKE_CLUSTER"])
s = json.loads(p.read_text())
args = sys.argv[1:]
ctx = args[args.index("--context") + 1]
del args[args.index("--context"):args.index("--context") + 2]
if "-n" in args:
    del args[args.index("-n"):args.index("-n") + 2]
with p.with_suffix(".log").open("a") as log:
    log.write(json.dumps([ctx, args]) + "\n")
cmd = args[0]
if cmd == "version": print(json.dumps({"serverVersion":{"gitVersion":"v1.34.0+k3s1"}}))
elif args[:2] == ["get", "nodes"]: print(json.dumps({"items":[{"spec":{"providerID":"k3s://local"}}]}))
elif args[:2] == ["get", "configmap"]: print(json.dumps({"data":s["data"]}))
elif args[:2] == ["get", "scaledobject"]: print(json.dumps({"metadata":{"annotations":s["annotations"]}}))
elif args[:2] == ["get", "deployment"]: print(s["cpu"])
elif cmd == "patch":
    patch = json.loads(pathlib.Path(args[args.index("--patch-file")+1]).read_text()) if "--patch-file" in args else json.loads(args[args.index("-p")+1])
    if args[1] == "configmap":
        for k,v in patch["data"].items():
            if v is None: s["data"].pop(k,None)
            else: s["data"][k] = v
    else:
        for k,v in patch["metadata"]["annotations"].items():
            if v is None: s["annotations"].pop(k,None)
            else: s["annotations"][k] = v
elif cmd == "annotate":
    field = next(a for a in args if a.startswith("autoscaling."))
    k,v = field.split("=",1); s["annotations"][k] = v
elif cmd == "scale":
    n = int(next(a.split("=")[1] for a in args if a.startswith("--replicas=")))
    if n == 8 and os.environ.get("FAIL_SCALE"): sys.exit(3)
    s["cpu"] = n
elif cmd != "rollout": raise RuntimeError(args)
p.with_suffix(".new").write_text(json.dumps(s)); p.with_suffix(".new").replace(p)
'''


@pytest.mark.parametrize("fail", [False, True])
def test_escalado_restaura_contextos_y_estado(tmp_path, fail):
    initial = {"data": {"DIFFICULTY_MODE": "auto", "DIFFICULTY_PREFIX": "000", "POOL_FRAGMENT_PERCENT": "5"},
               "annotations": {}, "cpu": 2}
    state_file = tmp_path / "cluster.json"
    state_file.write_text(json.dumps(initial))
    executable = tmp_path / "kubectl"
    executable.write_text(f"#!{sys.executable}\n" + FAKE_KUBECTL)
    executable.chmod(0o755)
    chain = [{"index": 0, "timestamp": 0, "transactions": []}]
    lock = threading.Lock()

    class API(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, data, status=200):
            payload = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == "/status":
                s = json.loads(state_file.read_text())
                self.reply({"chain_height": len(chain), "pending_transactions": 0, "mining": None,
                            "miners": {"cpu": s["cpu"], "gpu": 0},
                            "pool_plan": {"mode": s["data"]["DIFFICULTY_MODE"], "prefix": s["data"]["DIFFICULTY_PREFIX"]}})
            elif self.path.startswith("/blocks?"):
                self.reply({"height": len(chain), "blocks": [{"index": b["index"], "timestamp": b["timestamp"]} for b in chain]})
            else:
                self.reply(chain[int(self.path.rsplit("/", 1)[1])])

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                stamp = time.time()
                chain.append({"index": len(chain), "timestamp": stamp + .001,
                              "transactions": [dict(body, ts=stamp)]})
            time.sleep(.002)
            self.reply({"tx_id": str(stamp)}, 202)

    server = ThreadingHTTPServer(("127.0.0.1", 0), API)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    out = tmp_path / "resultados"
    env = os.environ | {"PATH": str(tmp_path) + os.pathsep + os.environ["PATH"], "FAKE_CLUSTER": str(state_file),
                        "K3S_CONTEXT": "test-k3s", "GKE_CONTEXT": "test-gke", "PYTHON_BIN": sys.executable,
                        "BASE_URL": f"http://127.0.0.1:{server.server_port}", "OUT": str(out), "BULK": "2",
                        "MPLCONFIGDIR": str(tmp_path / "matplotlib")}
    if fail:
        env["FAIL_SCALE"] = "1"
    try:
        result = subprocess.run(["bash", str(SCRIPT), "--run"], env=env, capture_output=True, text=True, timeout=90)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert result.returncode == (3 if fail else 0), result.stdout + result.stderr
    assert json.loads(state_file.read_text()) == initial
    calls = [json.loads(line) for line in state_file.with_suffix(".log").read_text().splitlines()]
    assert all(ctx == "test-k3s" for ctx, args in calls if args[0] in ("scale", "annotate"))
    assert all(ctx == "test-gke" for ctx, args in calls if args[:2] == ["patch", "configmap"])
    assert not any("monitoring" in args for _, args in calls)
    if not fail:
        for cpu in (4, 8):
            with (out / f"cpu-{cpu}/confirmados.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            assert len(rows) == 3 and all(row["tx_confirmadas"] == "2" for row in rows)
        assert (out / "escalado.png").is_file()
