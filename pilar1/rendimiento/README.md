# Rendimiento inicial (para elegir los valores de arranque)

Mediciones rápidas en la PC del grupo para elegir los valores iniciales del sistema: prefijo, tamaño
de rango, calibración y cuántos mineros CPU tiene sentido levantar. No reemplazan las mediciones
formales del Pilar 1 (Hit #6 y #7), pero siguen las mismas reglas: misma base, una corrida de
calentamiento y la mediana de 5.

| Archivo | Qué hace |
|---|---|
| `hashrate.py` | Mide el hashrate MD5 de cualquier motor con el contrato del kit (`miner_cpu.py` o `miner_gpu`). Recorre un rango fijo con un prefijo imposible, separa la **velocidad del motor** (`seconds` que informa el motor) del **rendimiento efectivo** (de punta a punta, con el arranque del proceso) y estima cuánto tardaría un bloque con cada prefijo. Agrega las corridas a `resultados.csv` |
| `prueba_catedra.py` | El script que dio la cátedra en clase, tal cual (SHA-256, busca secuencialmente hasta la primera solución) |
| `resultados.csv` | Una fila por corrida |
| `barrido.py` / `barrido.csv` | Barre un parámetro de la GPU (`--threads`, `--blocks`, `--poll`, `--batch`) y guarda la mediana de 5 |
| `comparativa.py` / `comparativa.csv` | Hit #6 (prefijos 1 a 8) y Hit #7 (rango con y sin solución), CPU vs GPU con las mismas bases y rangos |

```bash
python pilar1/rendimiento/prueba_catedra.py 5
python pilar1/rendimiento/hashrate.py --nombre cpu-1  --rango 2000000  -- python pilar1/cpu/miner_cpu.py --workers 1
python pilar1/rendimiento/hashrate.py --nombre cpu-12 --rango 20000000 -- python pilar1/cpu/miner_cpu.py --workers 12
python pilar1/rendimiento/hashrate.py --nombre gpu    --rango 1000000000 -- pilar1/build/miner_gpu   # cuando esté el kernel
```

## Resultados (13/11/2026, i5-12400F 6C/12T, Windows 11, Python 3.14)

### El script de la cátedra (SHA-256, una corrida por prefijo)

| Prefijo | Tiempo | Nonce encontrado | Esperado (16^n) |
|---|---|---|---|
| 1 | 0,04 s | 6 | 16 |
| 2 | 0,04 s | 111 | 256 |
| 3 | 0,04 s | 3.322 | 4.096 |
| 4 | 0,09 s | 53.340 | 65.536 |
| 5 | 2,06 s | 2.477.362 | 1.048.576 |
| 6 | 2,12 s | 2.724.980 | 16.777.216 |

Unos **1,2 a 1,3 MH/s** en SHA-256 con un solo proceso. **El prefijo 6 tardó lo mismo que el 5:**
con ese `data`, la solución de 6 ceros apareció en el nonce 2,7 millones, cuando lo esperado son
16,7 millones. El tiempo hasta una solución depende de la suerte con cada base, por eso para elegir
valores medimos el hashrate y calculamos el tiempo esperado (16^n / hashrate).

### Hashrate MD5 de nuestro minero CPU (`hashrate.py`, mediana de 5)

| Motor | Motor | Efectivo | Para qué sirve |
|---|---|---|---|
| `miner_cpu.py --workers 1` (= un pod del k3s) | **1,61 MH/s** | 1,49 MH/s | Tamaño de la calibración CPU y `MIN_RANGE_CPU` (2 M nonces ≈ 1,2 s) |
| `miner_cpu.py --workers 12` (la mejor CPU) | **8,07 MH/s** | 7,75 MH/s | Base del speedup de la GPU |

Con 12 procesos rinde **5 veces** lo de uno, no 12: el i5-12400F tiene 6 núcleos físicos y los 12 hilos
los comparten.

Tiempo esperado por bloque (16^n / hashrate del motor):

| Prefijo | 1 pod (1,61 MH/s) | CPU entera (8,07 MH/s) |
|---|---|---|
| 5 | 0,65 s | 0,13 s |
| 6 | 10,4 s | 2,1 s |
| 7 | 167 s | 33 s |
| 8 | 2.673 s (45 min) | 532 s (9 min) |

**Qué decidimos con esto:**
- Sin GPU, el prefijo automático va a quedar en **6** con varios pods (con 8 pods no pasa de ~12 MH/s,
  porque comparten la misma CPU), y en 5 con uno solo. El 7 y el 8 solo son viables con GPU.
- `MIN_RANGE_CPU` y `CALIB_RANGE_CPU` = **2 M nonces**: ~1,2 s por tarea en un pod, más que el costo fijo
  de pedir la tarea y arrancar el motor (la diferencia motor / efectivo es de ~8 %).
- GPU (17/11): **1.104 MH/s** de motor, 1.013 MH/s efectivo. Con eso `MIN_RANGE_GPU`, `CALIB_RANGE_GPU` y el
  `MINER_CHUNK` del agente pasaron a 1.000 M. El barrido de parámetros (`barrido.py`) y la comparativa CPU vs
  GPU de los Hits #6 y #7 (`comparativa.py`) están en `pilar1/README.md`.

## GPU prestada: RTX 4060 en InferHive (22/11/2026)

Medido en el namespace del grupo en InferHive (node `knode06`, RTX 4060 8 GB, `sm_89`, driver 595.99),
con la reserva GPU-NBDS, dentro del mismo pod del minero (`ghcr.io/mateoponti/tpi-miner-gpu`, que ya trae
`/app/miner_gpu`). Ahí no hay motor CPU, así que `gpu_remota.py` corre solo la parte GPU de `hashrate.py` y
`comparativa.py` con **las mismas bases y los mismos rangos** que la corrida del 17/11 (el rango con solución
del Hit #7 sale del nonce de esa corrida), y se compara contra la mejor CPU (12 procesos) ya medida.
Calentamiento + mediana de 5, como siempre. Los CSV están en `inferhive/`.

```bash
. ~/Downloads/connect.sh ~/Downloads/<alumno>.kubeconfig          # dentro de la franja reservada
kubectl cp pilar1/rendimiento/<script>.py <pod>:/tmp/rend/          # hashrate, comparativa, gpu_remota, barrido
kubectl exec <pod> -- sh -c 'cd /tmp/rend && python3 gpu_remota.py --nombre gpu-rtx4060 -- /app/miner_gpu'
```

**Hashrate** (rango de 10.000 M nonces): **4.970 MH/s** de motor, 4.465 MH/s efectivo. Es **4,5 veces** la
1050 Ti (1.104 MH/s) y **616 veces** la mejor CPU (8,07 MH/s).

**Hit #6 y #7** (segundos del motor, mediana de 5):

| Caso | CPU 12 procesos | GTX 1050 Ti | RTX 4060 | 4060 vs CPU | 4060 vs 1050 Ti |
|---|---|---|---|---|---|
| Prefijo 4 | 0,1869 | 0,0019 | 0,0005 | ×402 | ×4,1 |
| Prefijo 5 | 0,3030 | 0,0054 | 0,0024 | ×127 | ×2,3 |
| Prefijo 6 | 3,5636 | 0,0210 | 0,0095 | ×376 | ×2,2 |
| Prefijo 7 | 11,3572 | 0,0437 | 0,0156 | ×730 | ×2,8 |
| Prefijo 8 | — | 8,1744 | 1,7099 | — | ×4,8 |
| Hit #7 con solución | 5,6510 | 0,0316 | 0,0063 | ×894 | ×5,0 |
| Hit #7 sin solución | 11,7490 | 0,0980 | 0,0201 | ×586 | ×4,9 |

Con prefijos de 1 a 3 la solución aparece en los primeros cientos de nonces y todo tarda menos de 1 ms: lo que
se mide es el arranque del kernel, no el hashrate. Con prefijos cortos la ventaja de la 4060 es menor (×2 a ×3)
por lo mismo; con 8, o con el rango entero del Hit #7, se acerca al ×4,5 del hashrate.

**Barrido** (`barrido.py`, 10.000 M nonces): con `--threads` entre 64 y 1024 da de 4.940 a 5.320 MH/s (el
mejor, 512, solo un 8 % arriba de 256), y `--blocks` entre 48 y 768 da de 4.910 a 5.150 MH/s. Quedan los
valores por defecto: la diferencia es chica y la imagen tiene que andar igual en las otras GPUs. Un corte del
túnel hizo que dos barridos de bloques se pisaran en la GPU; esas cuatro filas (la mitad del hashrate) se
sacaron del CSV.

**Ojo con la calibración del gateway:** al conectarse, el gateway le midió al minero **12.744 MH/s**, 2,6
veces lo que da la 4060 medida acá. Sirve para la clase del worker, pero el pool elige el prefijo con el
hashrate **real** (lo que entrega el minero), así que no lo engaña; queda anotado para revisar cómo calibra.
