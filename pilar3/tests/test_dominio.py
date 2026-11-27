"""A2 del profe: un dominio propio detrás de Cloudflare, y ninguna otra puerta de entrada.

El punto del A2 no es tener un nombre lindo: es que **todo** el tráfico de afuera pase por Cloudflare. Un
dominio con la IP del cluster todavía abierta no cambia nada, porque el que quiera se saltea el proxy pidiéndole
a la IP. Estos tests revisan el repo (no el cluster): que no quede ninguna URL vieja, que el Ingress sirva solo
el dominio, que el certificado salga del issuer de DNS-01 sin el token adentro, que al balanceador solo entren
los rangos de Cloudflare y que ningún Service nuestro vuelva a pedir una IP pública propia.

Lo que solo se puede ver con el cluster arriba (que el certificado quede READY, que la IP no conteste, que
`bench.py` aguante el bulk grande por Cloudflare) está en el paso de verificación de `pilar3/README.md`.

    pytest -q pilar3/tests/test_dominio.py
"""
import ipaddress
import re
from pathlib import Path

import pytest
import yaml

PILAR3 = Path(__file__).resolve().parents[1]
REPO = PILAR3.parents[0]

DOMINIO = "sdmer.dpdns.org"
GATEWAY = f"gateway.{DOMINIO}"

# Dos rangos grandes de Cloudflare: si la lista del values quedó a medias, estos son los que faltan primero.
ANCLAS_DE_CLOUDFLARE = {"104.16.0.0/13", "172.64.0.0/13"}

# URLs que se dejaron de usar el 27/11. Se busca el patrón de URL (con esquema) y no el nombre suelto, porque
# los comentarios que explican el cambio nombran lo viejo a propósito.
URLS_VIEJAS = re.compile(r"https?://[^\s\"'`)\]]*(?:sslip\.io|34\.63\.209\.225|35\.223\.237\.60)")

# Dónde sí puede quedar una URL vieja: el plan de la entrega (es el plan, no configuración) y los resultados de
# las pruebas de carga, que son el registro de una medición del 20/11. Un registro no se reescribe.
PERDONADOS = ("docs/", "pilar3/loadtest/resultados/")


def archivos_de_texto() -> list[Path]:
    out = []
    for patron in ("*.yaml", "*.yml", "*.py", "*.sh", "*.md", "*.html", "*.json"):
        for f in REPO.rglob(patron):
            rel = f.relative_to(REPO).as_posix()
            if rel.startswith((".venv/", ".git/")) or "__pycache__" in rel:
                continue
            if rel.startswith(PERDONADOS):
                continue
            out.append(f)
    return sorted(out)


def docs_de(archivo: Path) -> list[dict]:
    with archivo.open(encoding="utf-8") as fh:
        return [d for d in yaml.safe_load_all(fh) if isinstance(d, dict) and d.get("kind")]


def valores(archivo: Path) -> dict:
    with archivo.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


HTTPS = PILAR3 / "k8s" / "https"
INGRESS = next(d for d in docs_de(HTTPS / "dominio.yaml") if d["kind"] == "Ingress")
ISSUERS = {d["metadata"]["name"]: d for d in docs_de(HTTPS / "cluster-issuer.yaml")}
TRAEFIK = valores(HTTPS / "traefik-values.yaml")

TODOS_LOS_DOCS = [(a, d)
                  for a in sorted((PILAR3 / "k8s").rglob("*.yaml")) + sorted((PILAR3 / "k3s-local").rglob("*.yaml"))
                  if a.name != "kustomization.yaml" and "values" not in a.name
                  for d in docs_de(a)]
SERVICES = [(a, d) for a, d in TODOS_LOS_DOCS if d["kind"] == "Service"]


def test_no_queda_ninguna_url_vieja():
    """sslip.io y las dos IPs fijas: si sobrevive una, algo del sistema sigue entrando sin pasar por Cloudflare."""
    encontradas = []
    for f in archivos_de_texto():
        for n, linea in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if URLS_VIEJAS.search(linea):
                encontradas.append(f"{f.relative_to(REPO).as_posix()}:{n}: {linea.strip()}")
    assert not encontradas, encontradas


def test_el_ingress_sirve_el_dominio_y_nada_mas():
    reglas = INGRESS["spec"]["rules"]
    assert [r.get("host") for r in reglas] == [DOMINIO, GATEWAY]
    # La regla sin host del Ingress viejo servía http://35.223.237.60 a cualquiera que supiera la IP.
    assert all(r.get("host") for r in reglas), "una regla sin host vuelve a servir la IP pelada"
    assert [h for t in INGRESS["spec"]["tls"] for h in t["hosts"]] == [DOMINIO, GATEWAY]


def test_el_front_manda_ws_al_notifier_y_el_resto_al_coordinator():
    front = next(r for r in INGRESS["spec"]["rules"] if r["host"] == DOMINIO)
    rutas = {p["path"]: p["backend"]["service"]["name"] for p in front["http"]["paths"]}
    assert rutas == {"/ws": "notifier", "/": "coordinator"}
    gateway = next(r for r in INGRESS["spec"]["rules"] if r["host"] == GATEWAY)
    assert {p["backend"]["service"]["name"] for p in gateway["http"]["paths"]} == {"gateway"}


def test_el_certificado_sale_del_issuer_de_dns01():
    nombre = INGRESS["metadata"]["annotations"]["cert-manager.io/cluster-issuer"]
    issuer = ISSUERS[nombre]            # el issuer que pide el Ingress tiene que estar en el repo
    solver = issuer["spec"]["acme"]["solvers"][0]
    assert "dns01" in solver and "http01" not in solver
    assert solver["dns01"]["cloudflare"]["apiTokenSecretRef"]["name"]
    assert solver["selector"]["dnsZones"] == [DOMINIO]


def test_el_token_de_cloudflare_no_esta_en_el_repo():
    """El secret se carga a mano en el cluster. Acá solo puede haber una referencia por nombre."""
    issuer = ISSUERS[INGRESS["metadata"]["annotations"]["cert-manager.io/cluster-issuer"]]
    cloudflare = issuer["spec"]["acme"]["solvers"][0]["dns01"]["cloudflare"]
    assert set(cloudflare) == {"apiTokenSecretRef"}, "no va el token literal"
    for f in sorted(HTTPS.rglob("*.yaml")):
        # apiToken: es el token en claro; apiTokenSecretRef: es la referencia al secret del cluster.
        assert not re.search(r"apiToken:\s*\S", f.read_text(encoding="utf-8")), f


def test_al_balanceador_solo_entra_cloudflare():
    rangos = TRAEFIK["service"]["spec"]["loadBalancerSourceRanges"]
    assert len(rangos) >= 15, rangos
    redes = [ipaddress.ip_network(r) for r in rangos]        # explota si alguno está mal escrito
    assert all(r.version == 4 for r in redes)
    assert not any(r.prefixlen == 0 for r in redes), "0.0.0.0/0 deja entrar a todo el mundo"
    assert ANCLAS_DE_CLOUDFLARE <= set(rangos)


def test_lo_que_entra_por_http_se_va_a_https():
    redirect = TRAEFIK["ports"]["web"]["redirections"]["entryPoint"]
    assert redirect["to"] == "websecure" and redirect["scheme"] == "https"
    assert redirect["permanent"] is True      # 301, que es lo que espera la verificación del profe


@pytest.mark.parametrize("archivo,doc", SERVICES,
                         ids=[f"{a.name}:{d['metadata']['name']}" for a, d in SERVICES])
def test_ningun_service_nuestro_pide_una_ip_publica(archivo, doc):
    """La única IP pública es la de Traefik (la crea Helm). Un LoadBalancer más es una puerta más."""
    assert archivo and doc["spec"].get("type") != "LoadBalancer", archivo
    assert "loadBalancerIP" not in doc["spec"], archivo


def test_los_mineros_de_afuera_entran_por_el_dominio():
    k3s = valores(PILAR3 / "k3s-local" / "config.yaml")["data"]
    assert k3s["GATEWAY_URL"] == f"https://{GATEWAY}"
    keda = next(d for d in docs_de(PILAR3 / "k3s-local" / "keda-scaledobject.yaml") if d["kind"] == "ScaledObject")
    assert keda["spec"]["triggers"][0]["metadata"]["url"] == f"https://{GATEWAY}/scaling"
    apps = valores(PILAR3 / "k8s" / "apps" / "config.yaml")["data"]
    assert apps["GATEWAY_PUBLIC_URL"] == f"https://{GATEWAY}"
    gpu = next(d for d in docs_de(REPO / "pilar2" / "services" / "miners" / "gpu" / "inferhive.yaml")
               if d["kind"] == "Deployment")
    env = {e["name"]: e.get("value") for e in gpu["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["GATEWAY_URL"] == f"https://{GATEWAY}"
