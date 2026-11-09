# Pilar 1 · Minero CUDA

## Estado del grupo

| Pieza | Estado |
|---|---|
| `gpu/miner_gpu.cu` | **Pendiente**: los 3 TODO del kit (MD5 multi-bloque, grid-stride `uint64_t`, corte con `atomicCAS`). Compilar con `make ARCH=sm_61` (GTX 1050 Ti, **CUDA 12.x**). |
| `cpu/miner_cpu.py` | Hecho. Minero CPU multiproceso con el mismo contrato: `python tools/check_contract.py -- python cpu/miner_cpu.py` → 17/17. Es la "mejor CPU" para el speedup (`--workers 12`) y el motor de la imagen `miner-cpu` del Pilar 2. |
| Mediciones | Pendiente: mismas bases y rangos, calentamiento, mediana de 5 corridas. |
| Integración | El wrapper del Pilar 2 es `pilar2/services/miners/agent.py` (+ `miners/gpu/run.ps1`). Para el coordinador de la cátedra (clase del 13/11) se usa `tools/worker_adapter.py`. |

Diagrama, decisiones y fuente del MD5: a completar cuando esté el kernel.
Abajo sigue el README original del kit.

---

# Kit de arranque · TPI Pilar 1 (minero CUDA)

Sistemas Distribuidos y Programación Paralela · UNLu · Comisión Mercedes 2026.

Este kit **no** es la solución del Pilar 1. Les da el andamiaje para que el
trabajo de ustedes se concentre en lo que se evalúa: el kernel, la partición
del espacio de nonces, el corte temprano y las mediciones.

```
kit-pilar1/
├── gpu/miner_gpu.cu        esqueleto del minero CUDA (3 TODO para completar)
├── oracle/miner_cpu.py     minero CPU de referencia (el "oráculo" de los tests)
├── tools/check_contract.py verifica que su binario cumple el contrato
├── tools/measure_energy.py mide hashes/s y hashes/J (payload del leaderboard)
├── tools/worker_adapter.py conecta su minero con el coordinador (modo pull)
├── tools/make_vectors.py   regenera tests/vectors.json
├── tests/                  vectores de prueba + tests del kit (pytest)
├── .github/workflows/      CI mínimo: gitleaks + compilar con nvcc + tests
└── Makefile
```

## El contrato del binario

Todo el kit (y la corrección) asume que su minero se invoca así:

```bash
miner_gpu --hash "abc"
# {"hash": "900150983cd24fb0d6963f7d28e17f72"}

miner_gpu --base "<previous_hash><data>" --difficulty 5 --from 0 --to 99999999
# {"found": true, "nonce": 1234567, "hash": "00000...", "hashes": 1300000, "seconds": 0.01}
# {"found": false, "hashes": 100000000, "seconds": 0.05}
```

- La cadena candidata es `base + str(nonce)`: decimal, sin separadores, UTF-8.
  Es exactamente lo que calcula el coordinador de la cátedra.
- `--prefix <hex>` acepta cualquier prefijo hexadecimal (Hit #5 del enunciado);
  `--difficulty d` equivale a `--prefix` con `d` ceros.
- El rango `[from, to]` es cerrado (Hit #7). Si no hay solución: `"found": false`.
- Una sola línea JSON por stdout, código de salida 0 en ambos casos.
  Errores de uso: mensaje por stderr y código 2.

## Pasos

```bash
# 0. Los tests del kit tienen que pasar en su máquina (Python 3.10+ y pytest)
make test
make check-oracle                # el oráculo cumple el contrato: 17/17

# 1. Completen los TODO de gpu/miner_gpu.cu y compilen
make ARCH=sm_89                  # RTX 40xx; sm_86 RTX 30xx; sm_75 Colab T4
make check                       # su minero contra los mismos 17 casos

# 2. Midan energía (en la máquina con la GPU), ~30 s de cómputo sin corte temprano
python3 tools/measure_energy.py --equipo g-su-grupo -- \
  ./build/miner_gpu --base "$(python3 -c 'print("0"*32+"bloque 1")')" \
  --prefix ffffffffffffffff --from 0 --to 60000000000

# 3. Conéctenlo al coordinador de la cátedra (credencial: foro del aula)
export COORDINADOR_URL=https://cadena-sd.impulsate.lat
export CADENA_TOKEN=...          # nunca lo commiteen
python3 tools/worker_adapter.py --minero g-su-grupo-gpu1 --once -- ./build/miner_gpu
```

`-arch=native` necesita CUDA 11.6 o posterior; si su `nvcc` es más viejo,
indiquen la arquitectura a mano.

## Trampas que los vectores detectan a propósito

| Caso | Qué rompe |
|---|---|
| `big-nonces` | usar `int` o `uint32` para el nonce: el rango cruza 2^32 |
| `long-base-d3` | un MD5 que solo procesa un bloque de 64 bytes |
| `utf8-base` | contar caracteres en vez de bytes (`ñ` y `·` ocupan 2 bytes) |
| `none-in-range` | inventar un resultado cuando no hay solución |
| `single-nonce-hit` | errores por uno en los bordes del rango |
| `d4-offset` | ignorar `--from` |

El checker no exige el nonce **más chico**: cualquier nonce del rango que
cumpla el prefijo es válido. Piensen por qué la GPU casi nunca devuelve el
menor, y si eso le importa al coordinador.

## CI

`.github/workflows/pilar1.yml` corre gitleaks, instala solo el compilador
`nvcc` (los runners de GitHub no tienen GPU, pero compilar no la necesita),
compila el minero y corre `make test` y `make check-oracle`. GitHub solo lee
workflows de `.github/workflows/` en la **raíz** del repo: si el kit vive en
`pilar1/`, muevan el archivo a la raíz y agreguen
`defaults: run: working-directory: pilar1` al job de build.

## Seguridad

La credencial del coordinador va por variable de entorno. `.gitignore` ya
excluye `.env` y `build/`. El pipeline de CI del TPI debe correr gitleaks.
