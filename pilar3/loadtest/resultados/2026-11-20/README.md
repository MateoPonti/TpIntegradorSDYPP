# Prueba de carga rápida · 20/11/2026

Prueba de humo, **no** es la medición final: sirve para ver que el sistema aguanta y dónde está el cuello de
botella antes de las pruebas completas (que van en otra carpeta con su fecha).

**Condiciones:** GKE (commit `a965c1e`), prefijo automático (quedó en `000000`), fragmentación 10 %, 8 mineros CPU en
el k3d de la PC del grupo (13,3 MH/s en total), sin GPU, comisión 0,10. Una corrida por bulk (`--repeat 1`), desde la
PC del grupo por HTTPS. Antes se mandó una tx para que KEDA levantara los 8 pods.

```bash
cd pilar3/loadtest
python bench.py --base-url https://35-223-237-60.sslip.io --bulks 1,10,100,1000 --repeat 1 \
  --difficulty auto-000000 --fragment 10 --cpu-miners 8 --gpu-miners 0 --out resultados/2026-11-20/results.csv
python bench.py ... --bulks 10000 ...          # mismos parámetros
python plot.py resultados/2026-11-20/results.csv --out resultados/2026-11-20/charts
```

| Bulk | Mandar todas (s) | Hasta confirmar todas (s) | tx/s al mandar | Latencia p50 | Errores |
|---|---|---|---|---|---|
| 1 | 0,6 | 14,0 | 1,6 | 617 ms | 0 |
| 10 | 0,6 | 5,9 | 16,9 | 578 ms | 0 |
| 100 | 2,3 | 9,4 | 43,1 | 593 ms | 0 |
| 1.000 | 18,6 | 28,8 | 53,7 | 582 ms | 0 |
| 10.000 | 183,2 | 198,1 | 54,6 | 583 ms | 0 |

`verify_chain` después: altura 52, 0 bloques inválidos.

**Qué se ve:**
- **El cuello de botella es mandar las tx, no minar.** Con 10.000, las tx terminan de confirmarse 15 s después de que
  se mandó la última. `bench.py` manda de a una tx por pedido HTTP, con 32 hilos, y cada pedido tarda ~580 ms (ida y
  vuelta a `us-central1` más el saludo de TLS, porque abre una conexión nueva por pedido): eso da ~55 tx/s como techo.
  Con 100.000 serían ~30 min solo para mandarlas.
- **Bulks chicos:** el tiempo lo marca la ventana de 10 s (el bloque sale cuando cierra la ventana) más el minado. El
  de 1 tx tardó más (14 s) porque cayó al principio de una ventana.
- **Nota del 27/11:** la opción (1) ya no existe. El A2 cerró el acceso por IP (todo entra por
  `https://sdmer.dpdns.org`, detrás de Cloudflare), así que para las pruebas finales queda la (2): `bench.py` en un
  pod del cluster. Lo de abajo es lo que se escribió ese día y se deja como quedó.
- Para las pruebas finales conviene: (1) usar `http://35.223.237.60` (sin TLS) o correr `bench.py` en un pod dentro de
  GKE, para medir el sistema y no la conexión de la PC; (2) `DIFFICULTY_MODE=fixed` para los prefijos 1 a 8;
  (3) `--repeat 3` y la mediana; (4) la GPU para los prefijos 7 y 8 y para el experimento de entrada y salida.
