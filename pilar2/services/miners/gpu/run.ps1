# Minero GPU en la PC con la GTX 1050 Ti (Windows). NO es una imagen: corre nativo, nunca en GCP.
# Requisitos: pilar1\build\miner_gpu.exe compilado (make ARCH=sm_61) y Python 3.10+.
#   $env:GATEWAY_URL = "http://<ip-del-gateway>"   # local: http://127.0.0.1:18180
#   $env:MINER_TOKEN = "..."                         # nunca commitearlo
#   $env:MINER_ACCOUNT = "mateo"                     # cuenta de cobro del premio (opcional)
#   .\pilar2\services\miners\gpu\run.ps1
$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot\..\..\..\.."
$bin = Join-Path $root "pilar1\build\miner_gpu.exe"
if (-not (Test-Path $bin)) { $bin = Join-Path $root "pilar1\build\miner_gpu" }
if (-not (Test-Path $bin)) { throw "No existe pilar1\build\miner_gpu(.exe). Compilá el Pilar 1 primero (make ARCH=sm_61)." }
if (-not $env:GATEWAY_URL -or -not $env:MINER_TOKEN) { throw "Faltan GATEWAY_URL y/o MINER_TOKEN." }
$env:MINER_KIND = "gpu"
if (-not $env:MINER_ID) { $env:MINER_ID = "gpu-$env:COMPUTERNAME" }
if (-not $env:MINER_CHUNK) { $env:MINER_CHUNK = "200000000" }  # duda 8: se elige midiendo
python "$PSScriptRoot\..\agent.py" -- $bin
