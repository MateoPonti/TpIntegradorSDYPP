"""B2 del profe: que los manifiestos estén endurecidos, y que no se desendurezcan sin que nadie se entere.

Son tests del repo, no del cluster: parsean los YAML de `pilar3/k8s` y `pilar3/k3s-local` y revisan lo que se
puede revisar sin `kubectl`. Lo que sí necesita el cluster (que los pods vuelvan a Running, que `/logs` dé 403
desde afuera) está en el paso de verificación del PR y anotado en `pilar3/README.md`.

    pytest -q pilar3/tests/test_manifiestos.py
"""
from pathlib import Path

import pytest
import yaml

RAIZ = Path(__file__).resolve().parents[1]
K8S = RAIZ / "k8s"

# Redis y RabbitMQ no pueden tener la raíz de solo lectura: el entrypoint de cada uno escribe fuera de su PVC
# (RabbitMQ instala la cookie de Erlang y reescribe su config; Sentinel rearma la suya en cada arranque).
# Todo lo demás del securityContext sí les aplica, incluido correr sin root. Justificado en pilar3/README.md.
SIN_RAIZ_DE_SOLO_LECTURA = {"rabbitmq", "redis", "sentinel"}

# El coordinator no usa la API de Kubernetes, así que ninguno de los nuestros necesita el token montado.
CON_TOKEN_DE_LA_API: set[str] = set()

CONTROLADORES = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}


def documentos(*directorios: Path) -> list[tuple[Path, dict]]:
    out = []
    for d in directorios:
        for archivo in sorted(d.rglob("*.yaml")):
            if archivo.name in ("kustomization.yaml", "traefik-values.yaml") or "values" in archivo.name:
                continue  # no son manifiestos: uno es de kustomize y los otros son values de Helm
            with archivo.open(encoding="utf-8") as fh:
                for doc in yaml.safe_load_all(fh):
                    if isinstance(doc, dict) and doc.get("kind"):
                        out.append((archivo, doc))
    return out


DOCS = documentos(K8S, RAIZ / "k3s-local")
CARGAS = [(a, d) for a, d in DOCS if d["kind"] in CONTROLADORES]
CONTENEDORES = [(a, d, c) for a, d in CARGAS
                for c in d["spec"]["template"]["spec"].get("containers", [])]


def ids(items):
    return [f"{a.parent.name}/{a.name}:{d['metadata']['name']}" + (f":{c['name']}" if len(x) > 2 else "")
            for x in items for a, d, *resto in [x] for c in [resto[0] if resto else {}]]


def test_hay_manifiestos_para_revisar():
    """Si un cambio de nombres deja los globs en cero, los demás tests pasarían sin revisar nada."""
    assert len(CARGAS) >= 6, [str(a) for a, _ in CARGAS]
    assert len(CONTENEDORES) >= 7


@pytest.mark.parametrize("archivo,doc,cont", CONTENEDORES, ids=ids(CONTENEDORES))
def test_todo_contenedor_tiene_security_context(archivo, doc, cont):
    sc = cont.get("securityContext") or {}
    pod_sc = doc["spec"]["template"]["spec"].get("securityContext") or {}
    quien = f"{archivo.name}:{doc['metadata']['name']}:{cont['name']}"
    assert sc.get("allowPrivilegeEscalation") is False, f"{quien}: falta allowPrivilegeEscalation: false"
    assert (sc.get("capabilities") or {}).get("drop") == ["ALL"], f"{quien}: falta capabilities.drop: [ALL]"
    assert sc.get("runAsNonRoot") is True or pod_sc.get("runAsNonRoot") is True, f"{quien}: falta runAsNonRoot"
    if cont["name"] not in SIN_RAIZ_DE_SOLO_LECTURA:
        assert sc.get("readOnlyRootFilesystem") is True, f"{quien}: falta readOnlyRootFilesystem: true"


@pytest.mark.parametrize("archivo,doc,cont", CONTENEDORES, ids=ids(CONTENEDORES))
def test_todo_contenedor_tiene_limite_de_cpu(archivo, doc, cont):
    """Sin límite de CPU, un pod que se vuelve loco (o uno de los mineros) se come el nodo entero."""
    recursos = cont.get("resources") or {}
    quien = f"{archivo.name}:{doc['metadata']['name']}:{cont['name']}"
    assert (recursos.get("limits") or {}).get("cpu"), f"{quien}: falta resources.limits.cpu"
    assert (recursos.get("requests") or {}).get("cpu"), f"{quien}: falta resources.requests.cpu"


@pytest.mark.parametrize("archivo,doc", CARGAS, ids=ids(CARGAS))
def test_cada_carga_tiene_su_serviceaccount(archivo, doc):
    """Sin esto todos usan la ServiceAccount `default` del namespace: cualquier permiso que se le dé a una se
    lo queda todo el namespace."""
    spec = doc["spec"]["template"]["spec"]
    nombre = doc["metadata"]["name"]
    assert spec.get("serviceAccountName"), f"{archivo.name}:{nombre}: falta serviceAccountName"
    if spec["serviceAccountName"] not in CON_TOKEN_DE_LA_API:
        assert spec.get("automountServiceAccountToken") is False, \
            f"{archivo.name}:{nombre}: no usa la API de Kubernetes, así que el token no se monta"


def _politicas() -> list[dict]:
    return [d for _, d in DOCS if d["kind"] == "NetworkPolicy"]


def _politica(nombre: str) -> dict:
    encontradas = [p for p in _politicas() if p["metadata"]["name"] == nombre]
    assert encontradas, f"falta la NetworkPolicy {nombre}: {[p['metadata']['name'] for p in _politicas()]}"
    return encontradas[0]


@pytest.mark.parametrize("nombre,app,puertos", [
    ("redis-solo-apps", "redis", {6379, 26379}),
    ("rabbitmq-solo-apps", "rabbitmq", {5672, 15672, 15692}),
])
def test_networkpolicy_deja_entrar_solo_a_las_apps(nombre, app, puertos):
    """Redis y RabbitMQ son ClusterIP, pero sin NetworkPolicy cualquier pod del cluster les habla igual."""
    p = _politica(nombre)
    assert p["spec"]["podSelector"]["matchLabels"] == {"app": app}
    assert p["spec"]["policyTypes"] == ["Ingress"]
    reglas = p["spec"]["ingress"]
    permitidos = {v for regla in reglas for sel in regla.get("from", [])
                  for v in (sel.get("podSelector", {}).get("matchExpressions") or [{}])[0].get("values", [])}
    assert {"coordinator", "pool", "gateway", "notifier"} <= permitidos, permitidos
    # Prometheus scrapea /metrics desde el namespace monitoring: si no entra, se pierden las mediciones.
    namespaces = [sel.get("namespaceSelector") for regla in reglas for sel in regla.get("from", [])
                  if sel.get("namespaceSelector")]
    assert namespaces, f"{nombre}: Prometheus (namespace monitoring) tiene que poder entrar"
    abiertos = {int(pto["port"]) for regla in reglas for pto in regla.get("ports", [])}
    assert abiertos <= puertos, f"{nombre}: abre puertos que no son los suyos: {abiertos - puertos}"


def test_networkpolicy_no_deja_pasar_a_cualquiera():
    """Una política con un `from` vacío (o sin `from`) deja entrar a todo el mundo: es peor que no tenerla,
    porque parece que protege."""
    for p in _politicas():
        for regla in p["spec"].get("ingress", []):
            assert regla.get("from"), f"{p['metadata']['name']}: una regla sin `from` deja entrar a todos"


def test_logs_y_metrics_no_se_sirven_desde_afuera():
    """B2.6: /logs y /metrics tienen que dar 403 desde Internet. Lo hace un middleware de Traefik con una
    lista de IPs, en un IngressRoute con prioridad más alta que el que publica la API."""
    docs = [d for _, d in DOCS if d["kind"] in ("Middleware", "IngressRoute")]
    middlewares = [d for d in docs if d["kind"] == "Middleware" and "ipAllowList" in (d.get("spec") or {})]
    assert middlewares, "falta el Middleware ipAllowList de Traefik (pilar3/k8s/https/privado.yaml)"
    rutas = [d for d in docs if d["kind"] == "IngressRoute"
             and any("/logs" in str(r.get("match", "")) or "/metrics" in str(r.get("match", ""))
                     for r in d["spec"].get("routes", []))]
    assert rutas, "falta el IngressRoute que manda /logs y /metrics al middleware"
    publicas = [max(int(r.get("priority", 0)) for r in d["spec"]["routes"])
                for d in docs if d["kind"] == "IngressRoute" and d not in rutas]
    for ruta in rutas:
        prioridad = min(int(r.get("priority", 0)) for r in ruta["spec"]["routes"])
        assert all(prioridad > p for p in publicas), \
            f"{ruta['metadata']['name']}: prioridad {prioridad} no le gana a las públicas {publicas}"
        nombres = {m["name"] for r in ruta["spec"]["routes"] for m in r.get("middlewares", [])}
        assert nombres & {m["metadata"]["name"] for m in middlewares}, \
            f"{ruta['metadata']['name']}: no usa el middleware de la lista de IPs"
