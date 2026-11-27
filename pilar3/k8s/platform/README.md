# Componentes de plataforma (B3, 27/11/2026)

Traefik, cert-manager y la observabilidad se instalaron **a mano con Helm**, no desde un pipeline. El problema
que marcó el profe en B3 es que así no queda en ningún lado con qué valores se instalaron: si el cluster se
cae, no se puede volver a armar igual, y nadie puede revisar en un PR lo que corre en el cluster.

Acá están los `values` de cada uno, y hay dos casos distintos:

- **Traefik** se instala con `helm upgrade -f traefik-values.yaml`: ese archivo es la fuente, con sus
  comentarios, y es el que hay que editar. El script lo **compara** contra el cluster y avisa si no coincide.
- **cert-manager** y **kube-prometheus-stack** se instalaron con `--set` desde la terminal, así que no había
  ningún archivo nuestro: esos dos se **exportan** del cluster y están generados (no editarlos a mano).

```bash
sh pilar3/k8s/platform/exportar-values.sh
git diff pilar3/k8s/platform   # si hay diferencias, alguien cambió algo a mano sin commitearlo
```

| Release | Namespace | Chart | Archivo |
|---|---|---|---|
| `traefik` | `traefik` | `traefik/traefik` | `traefik-values.yaml` (fuente, se compara) |
| `cert-manager` | `cert-manager` | `jetstack/cert-manager` | `cert-manager-values.yaml` (generado) |
| `kube-prometheus-stack` | `monitoring` | `prometheus-community/kube-prometheus-stack` | `observability-values.yaml` (generado) |

**La observabilidad no es nuestra:** `kube-prometheus-stack` lo instaló el TP3 y lo compartimos. Está acá para
poder reproducir el cluster y para que se vea de dónde salen Prometheus y Grafana, pero **no se le hace
`helm upgrade` desde el TPI**: tocarlo es tocar el `monitoring` de otro trabajo. Lo nuestro de observabilidad
son los `PodMonitor`, las alertas y el dashboard, que están en `pilar3/k8s/observability/` y sí los aplica el
Pipeline 3.

**Nada de secretos acá.** `helm get values` puede traer contraseñas si el chart las recibió por `--set`
(por ejemplo `grafana.adminPassword`). El script las saca antes de escribir el archivo y gitleaks corre en el
CI, pero conviene mirar el `git diff` antes de commitear.

## Por qué no hay un pipeline que los instale

Son componentes de plataforma: se instalan una vez por cluster y casi nunca cambian. Un pipeline que corra
`helm upgrade` de Traefik en cada push a `main` puede dejar el cluster sin Ingress controller por un error de
values, justo lo que más duele a días de la entrega. Si el cluster hay que rearmarlo, los comandos están en el
encabezado de cada archivo de values y en `pilar3/README.md`.
