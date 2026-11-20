# Pruebas de carga completas · 20/11/2026

Las mediciones de la sección 3.3 del enunciado, con CPU y con GPU. (La de humo del mismo día, solo CPU y desde la PC,
está en `../2026-11-20/`.)

**Condiciones:** GKE, commit `51c9f96` (shares, colectas, websocket de mineros). `bench.py` de la cátedra, sin cambios,
corrido **desde un pod dentro de GKE** (`bench`, solo manda tx, no mina): así se mide el sistema y no la conexión
de la PC (desde la PC daba ~55 tx/s; desde adentro, ~300 tx/s). Cada punto es la **mediana de 3 corridas**, salvo el
bulk de 100.000 (1 corrida). Comisión 0,10 por tx.
- **CPU:** 8 pods `miner-cpu` en el k3d de la PC del grupo (i5-12400F), 1 proceso cada uno: ~12 MH/s en total.
- **GPU:** GTX 1050 Ti en la misma PC, `miner_gpu` con tareas de 1.000 M: ~930–1.040 MH/s. Con la GPU conectada,
  el pool pide 0 mineros CPU.

Lo que el tiempo mide: desde que se manda la primera tx hasta que `GET /status` dice 0 pendientes. Incluye mandar
las tx, esperar que cierre la **ventana de 10 s** (decisión del 13/11: un bloque sale cuando cierra la ventana de su
primera tx), minar, verificar y sellar. Por eso nada baja de ~8–10 s.

```bash
# Cómo se corrió (script corregido en pilar3/loadtest/completa/; ver "Qué salió mal" abajo)
kubectl -n tpi run bench --image=python:3.12-slim --restart=Never --command -- sleep 86400
bash pilar3/loadtest/completa/correr.sh cpu gpu mismo_prefijo entrada
kubectl -n tpi delete pod bench
python pilar3/loadtest/resultados/2026-11-20-completa/graficos.py   # charts/*.png y resumen.md
```

## Resultados (segundos hasta confirmar todas, mediana)

### 1. Bulks con prefijo automático, fragmentación 10 % — `charts/bulks_auto.png`

| Bulk | CPU | GPU | Errores CPU / GPU |
|---|---|---|---|
| 1 | 11,6 | 18,6 | 0 / 0 |
| 10 | 11,3 | 8,4 | 0 / 0 |
| 100 | 12,7 | 33,1 | 0 / 0 |
| 1.000 | 14,7 | 16,7 | 0 / 0 |
| 10.000 | 57,0 | 168,0 | 0 / **142** (ver hallazgo 2) |
| 100.000 | 569 | 1.377 | 0 / 0 |

**Con la GPU tarda más, y no es un error.** En automático el pool elige el prefijo más largo con el que, *en
promedio*, un bloque se mina en menos de 10 s con el hashrate real. Con CPU (12 MH/s) eso es `000000` (1,4 s de
minado promedio); con GPU (1 GH/s) es `00000000` (4,3 s). La GPU tiene ~80 veces más hashrate, pero el bloque es
256 veces más difícil: el sistema usa la potencia extra para **subir la dificultad**, no para ir más rápido. Con
100.000 tx (~100 bloques que se minan de a uno), con GPU cada bloque llevó ~14 s y con CPU ~6 s.

### 2. Bulks con el mismo prefijo (`0000000`), fragmentación 10 % — `charts/bulks_mismo_prefijo.png`

La comparación justa: mismo trabajo por bloque para los dos.

| Bulk | CPU | GPU | Errores CPU / GPU |
|---|---|---|---|
| 1 | 51,8 | 13,4 | 0 / 0 |
| 10 | 84,4 | 10,3 | 0 / 0 |
| 100 | 27,5 | 9,7 | 0 / 0 |
| 1.000 | 55,9 | 8,5 | 0 / 0 |
| 10.000 | no medido | 52,0 | — / 0 |

Con 7 ceros la CPU necesita ~22 s de minado por bloque (16^7 / 12 MH/s) con mucha variación (en el bulk de 10 sus
3 corridas dieron 13,5, 84 y 178 s). La GPU necesita ~0,3 s, así que queda en la ventana de 10 s: **entre 3 y 8
veces más rápido de punta a punta**. El bulk de 10.000 con CPU no se midió: Claude Code cortó la corrida porque la
PC se quedó sin memoria (Docker con los 8 pods); el grupo decidió no repetirlo.

### 3. Prefijos fijos 1 a 8, bulk de 100, fragmentación 10 % — `charts/prefijos.png`

| Prefijo | CPU | GPU |
|---|---|---|
| 1 a 6 ceros | 8,6 – 10,7 | 9,5 – 10,5 |
| 7 ceros | 19,0 | 8,5 |
| 8 ceros | **250,8** | **19,0** |

Hasta 6 ceros manda la ventana de 10 s y los dos dan igual. Con 7 la CPU ya no entra en la ventana; con 8 la CPU
tarda ~4 min por bloque y la GPU ~19 s: **13 veces más rápido**. 0 errores en todas.

### 4. Fragmentación 1 a 50 %, bulk de 100 — `charts/fragmentos.png`

| Fragmentación | CPU (`0000000`) | GPU (`00000000`) |
|---|---|---|
| 1 % | 32,3 | 14,4 |
| 5 % | 46,7 | 12,9 |
| 10 % | 98,2 | 17,8 |
| 25 % | 23,0 | 16,8 |
| 50 % | 90,8 | 15,0 |

- **GPU:** casi no cambia, porque el tamaño de tarea tiene un mínimo de 1.000 M para GPU (`MIN_RANGE_GPU`, para
  llenar los hilos y que el arranque del motor pese poco). Con 8 ceros, 1 % a 10 % dan 43 M a 430 M: se usa el
  mínimo. Recién con 25 % (1.074 M) y 50 % (2.147 M) la tarea es más grande, y tampoco cambia mucho.
- **CPU:** no hay una tendencia clara. Con 3 corridas y bloques que necesitan ~22 s de minado promedio, la suerte
  (en qué tarea cae el nonce) pesa más que el tamaño de la tarea. Con tareas grandes (25–50 %, 67–134 M) cada pod
  tarda 45–90 s en terminar una, así que el que tiene la tarea con la solución puede tardar más en llegar a ella.

### 5. Entrada y salida de la GPU (prefijo automático, 5 tx/s todo el tiempo) — `charts/entrada_salida.png`

| Momento | Qué pasó |
|---|---|
| 0–95 s, solo CPU | Prefijo 6, un bloque cada ~10 s, 20–65 tx pendientes |
| **Entra la GPU** (97 s) | 5 s después el pool sube el prefijo a 8 |
| Con GPU (100–245 s) | Cada bloque tarda ~14 s, más que la ventana: las pendientes **suben** de 60 a 262. KEDA baja los 8 pods CPU a 0 recién a los 143 s |
| **Sale la GPU** (245 s, se corta el proceso) | A los 13 s vence su keep-alive; el pool baja el plan a 1 cero (sin mineros vivos), KEDA levanta CPU (1 → 4 → 8 pods) y el bloque en curso se **rearma** de 8 a 1 cero a los 19 s |
| Recuperación | 276 s: el plan vuelve a 6 ceros. 350 s: 0 pendientes |

## Hallazgos

1. **El prefijo automático con GPU queda demasiado alto para una carga continua.** Elige 8 ceros porque el minado
   promedio es 4,3 s, pero de punta a punta cada bloque tarda ~14 s (tareas de 1.000 M de ~1 s cada una, la espera
   de los resultados y la publicación por tandas). Con 5 tx/s el sistema se atrasa. Mejora posible (**no decidida**):
   que el pool use el tiempo real de los últimos bloques, o un margen mayor, para elegir el prefijo.
2. **El HPA del coordinator tiraba pedidos al bajar réplicas.** Los 142 errores salieron en una sola corrida de
   10.000 (las otras 14 de bulks con GPU dieron 0). A las 14:59:30 el HPA bajó de 5 a 2 réplicas en plena carga (y
   25 s después volvió a 5): los pods se apagaban sin esperar, y los pedidos que les llegaron mientras Kubernetes los
   sacaba del Service fallaron (no hubo ningún rechazo: `tx_rejected_total` en 0). **Arreglo** (en
   `pilar3/k8s/apps/coordinator.yaml` y `gateway.yaml`): `preStop` con `sleep 10` y `scaleDown.stabilizationWindowSeconds`
   de 120 a 300. **Después del arreglo:** el bulk de 10.000 con GPU dio **0 errores** en las 3 corridas (fase 2).
3. **Hasta 6 ceros, el límite es la ventana de 10 s, no el minado.** Para bajar la latencia habría que achicar la
   ventana (decisión del grupo y comentario del profe del 13/11), no sumar potencia.
4. **El techo de entrada:** ~300 tx/s desde adentro de GKE (`bench.py` manda de a una tx por pedido HTTP). Con
   ventanas de 10 s y bloques de hasta 1.000 tx, el sistema confirma como mucho ~100 tx/s; por eso 100.000 tx
   tardan entre 10 y 23 min.

## Qué salió mal (y quedó corregido)

- **La primera tanda nunca usó la GPU.** El script exportaba `MSYS_NO_PATHCONV=1` (para que `kubectl` de Git Bash
  no convierta `/tmp/...`), y eso hizo que el Python de Windows no encontrara `agent.py`: el agente se cerró al
  arrancar y los "bulks y prefijos con GPU" los minaron los 8 pods CPU. Se detectó porque el prefijo 8 tardaba
  minutos y `/status` decía `gpu: 0`. Esos resultados son válidos y se guardaron como CPU (`bulks_cpu.csv`,
  `prefijos_cpu.csv`); la GPU se midió en la segunda tanda. El script del repo usa la variable solo para `kubectl`
  y además se corta si la GPU no aparece en `/status`.
- La fase de entrada y salida tuvo el mismo problema con `stream.py` y `monitor.py`: se arrancaron a mano a los
  25 s y la GPU se prendió y apagó a mano en los mismos horarios (97 s y 245 s del monitor).
- Los pods CPU tardan en bajar a 0 cuando entra la GPU (143 s), así que al principio de algunas mediciones "con GPU"
  pudo haber CPU minando también.

## Archivos

`bulks_{cpu,gpu}.csv`, `mismo_prefijo_{cpu,gpu}.csv`, `prefijos_{cpu,gpu}.csv`, `fragmentos_{cpu,gpu}.csv` (formato
de `bench.py`), `entrada_salida.csv` (`/status` cada 2 s), `log.txt` (lo que hizo el script, con la hora),
`resumen.md` (tablas generadas), `charts/` (gráficos) y `graficos.py`.
