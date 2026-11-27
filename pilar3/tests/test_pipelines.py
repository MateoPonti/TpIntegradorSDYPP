"""B3 del profe: que los pipelines desplieguen lo que corre.

Lo que había era que el Pipeline 3 publicaba las imágenes en un repositorio de Artifact Registry
(`.../tpi/<servicio>`) distinto del que tienen las que están andando (`.../tp3/tpi-<servicio>`), así que correr
el pipeline no habría desplegado lo que estaba en el cluster. Son cosas que se ven solo cuando se las compara,
porque cada archivo por separado parece bien.

Estos tests comparan entre sí los tres lugares donde el nombre de la imagen aparece: el `docker build` del
pipeline, el `images:` del kustomization y el `image:` de cada Deployment.

    pytest -q pilar3/tests/test_pipelines.py
"""
import re
from pathlib import Path

import pytest
import yaml

RAIZ = Path(__file__).resolve().parents[2]
APPS = RAIZ / "pilar3" / "k8s" / "apps"
WORKFLOWS = RAIZ / ".github" / "workflows"


def cargar(archivo: Path) -> dict:
    with archivo.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def apps_wf() -> dict:
    return cargar(WORKFLOWS / "3-apps.yml")


@pytest.fixture(scope="module")
def kustomization() -> dict:
    return cargar(APPS / "kustomization.yaml")


def pasos(job: dict) -> str:
    return "\n".join(paso["run"] for paso in job["steps"] if "run" in paso)


def imagen_que_construye(wf: dict) -> str:
    """El nombre de imagen que arma el `docker build` del pipeline, con `${{ matrix.service }}` adentro."""
    build = pasos(wf["jobs"]["build"])
    m = re.search(r'-t "\$REGISTRY/([^"]+?):\$\{\{ github\.sha \}\}"', build)
    assert m, f"no encontré el `docker build -t` en el pipeline:\n{build}"
    return m.group(1)


def test_el_pipeline_publica_y_despliega_la_misma_imagen(apps_wf, kustomization):
    servicios = apps_wf["jobs"]["build"]["strategy"]["matrix"]["service"]
    patron = imagen_que_construye(apps_wf)
    entradas = {i["name"]: i for i in kustomization["images"]}
    assert len(servicios) == len(entradas), f"servicios {servicios} vs images {list(entradas)}"
    for svc in servicios:
        construida = patron.replace("${{ matrix.service }}", svc)
        entrada = entradas.get(f"REGISTRY/{svc}")
        assert entrada, f"falta la entrada images de {svc} en kustomization.yaml: {list(entradas)}"
        assert entrada["newName"] == f"REGISTRY/{construida}", (
            f"{svc}: el pipeline publica `{construida}` y el kustomization despliega "
            f"`{entrada['newName'].removeprefix('REGISTRY/')}`")


def test_el_sed_del_pipeline_le_pega_a_los_newName(apps_wf, kustomization):
    """El pipeline reemplaza el prefijo `REGISTRY/` por el registro real con un sed. Si cambia la forma del
    kustomization y el sed no, el apply queda con el literal `REGISTRY/` y no baja ninguna imagen."""
    deploy = pasos(apps_wf["jobs"]["deploy"])
    assert "s#newName: REGISTRY/#newName: $REGISTRY/#" in deploy
    assert "s#newTag: latest#newTag: ${{ github.sha }}#" in deploy
    for entrada in kustomization["images"]:
        assert entrada["newName"].startswith("REGISTRY/"), entrada
        assert entrada["newTag"] == "latest", entrada


def test_el_registro_del_pipeline_es_el_que_corre(apps_wf):
    """El repositorio de Artifact Registry y el prefijo de las imágenes tienen que ser los que figuran en
    pilar3/README.md como los que están corriendo. Si alguien cambia uno de los dos, salta acá."""
    registry = apps_wf["env"]["REGISTRY"]
    repo = registry.rsplit("/", 1)[-1]
    prefijo = imagen_que_construye(apps_wf).split("${{")[0]
    readme = (RAIZ / "pilar3" / "README.md").read_text(encoding="utf-8")
    documentadas = [l for l in readme.splitlines() if "docker.pkg.dev" in l and "{coordinator" in l]
    assert documentadas, "el README del Pilar 3 ya no dice qué imágenes corren"
    for linea in documentadas:
        assert f"/{repo}/{prefijo}" in linea, (
            f"el pipeline publica en `{repo}` con prefijo `{prefijo}` y el README dice otra cosa:\n{linea}")


@pytest.mark.parametrize("svc", ["coordinator", "pool", "gateway", "notifier"])
def test_el_deployment_usa_el_nombre_que_busca_el_kustomization(svc, kustomization):
    """`images[].name` es la clave con la que kustomize encuentra la imagen: si no es idéntica al `image:` del
    Deployment, kustomize no reemplaza nada y al cluster va el literal `REGISTRY/...`."""
    with (APPS / f"{svc}.yaml").open(encoding="utf-8") as fh:
        docs = [d for d in yaml.safe_load_all(fh) if isinstance(d, dict)]
    contenedores = [c for d in docs if d.get("kind") == "Deployment"
                    for c in d["spec"]["template"]["spec"]["containers"]]
    assert contenedores, f"{svc}.yaml no tiene Deployment"
    claves = {i["name"] for i in kustomization["images"]}
    for c in contenedores:
        imagen, _, tag = c["image"].rpartition(":")
        assert imagen in claves, f"{svc}: `{c['image']}` no lo matchea ninguna entrada de images: {claves}"
        assert tag == "latest", f"{svc}: el manifiesto no debería fijar un tag ({tag}); lo pone el pipeline"


def test_el_estado_de_tofu_no_es_local():
    """B3: dos corridas del pipeline con el estado en el disco del runner duplican el cluster. El bucket no va
    en el archivo (es global en todo GCP y dice en qué proyecto vive el estado): entra por -backend-config."""
    provider = (RAIZ / "pilar3" / "infra" / "terraform" / "provider.tf").read_text(encoding="utf-8")
    activas = [l for l in provider.splitlines() if not l.strip().startswith("#")]
    cuerpo = "\n".join(activas)
    assert 'backend "gcs"' in cuerpo, "el backend de GCS está comentado: el estado queda local"
    assert "bucket" not in cuerpo, "el nombre del bucket no se commitea: va por -backend-config"
    assert 'prefix = "tpi/pilar3"' in cuerpo

    infra = (WORKFLOWS / "1-infra.yml").read_text(encoding="utf-8")
    inits = [l for l in infra.splitlines() if "tofu init" in l]
    assert inits, "1-infra.yml ya no corre tofu init"
    for linea in inits:
        assert "-backend-config=" in linea and "TF_STATE_BUCKET" in linea, (
            f"tofu init sin el bucket del backend: {linea.strip()}")
