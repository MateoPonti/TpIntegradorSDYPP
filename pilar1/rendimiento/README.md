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
- Falta la GPU (los 3 TODO de `gpu/miner_gpu.cu` y CUDA 12.x). Con eso se fijan `MIN_RANGE_GPU`,
  `CALIB_RANGE_GPU` y el `--chunk` del agente.
