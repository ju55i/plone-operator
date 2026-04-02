"""
Plone Operator — Kopf-based Kubernetes operator for managing Plone 6 CMS.

Manages the full lifecycle of a PloneSite custom resource:
  - ZEO (ZODB) or external/in-cluster PostgreSQL (RelStorage via CNPG)
  - Volto (frontend + backend) or Classic (backend only) deployment types
  - Automatic Plone site initialisation via REST API polling
  - Weekly database packing via one-off Kubernetes Jobs
"""

import asyncio
import datetime
import logging

import aiohttp
import kopf
import kubernetes
from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Startup: load kubeconfig (in-cluster or local for development)
# ---------------------------------------------------------------------------

@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    """Load Kubernetes credentials and configure operator settings."""
    try:
        kubernetes.config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes config")
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()
        logger.info("Loaded local kubeconfig (development mode)")

    # Kopf health-check endpoint (used by manager.yaml probes)
    settings.peering.enabled = False
    settings.posting.enabled = True
    # Suppress Kopf's attempt to watch its own peering CRD (not installed)
    settings.watching.server_timeout = 270
    settings.watching.client_timeout = 300


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _name(meta):
    return meta["name"]


def _namespace(meta):
    return meta["namespace"]


def _labels(name: str) -> dict:
    return {
        "app.kubernetes.io/managed-by": "plone-operator",
        "app.kubernetes.io/part-of": name,
    }


def _make_env_list(env_dict: dict) -> list:
    """Convert a plain dict of env vars to a k8s env list."""
    return [{"name": k, "value": str(v)} for k, v in env_dict.items()]


def _secret_env(var_name: str, secret_name: str, secret_key: str) -> dict:
    return {
        "name": var_name,
        "valueFrom": {
            "secretKeyRef": {
                "name": secret_name,
                "key": secret_key,
            }
        },
    }



# ---------------------------------------------------------------------------
# Resource manifests
# ---------------------------------------------------------------------------

def build_zeo_pvc(name: str, namespace: str, spec: dict) -> dict:
    persistence = spec.get("persistence", {})
    storage_class = persistence.get("storageClass")
    size = persistence.get("size", "10Gi")

    manifest = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": f"{name}-zeo-data",
            "namespace": namespace,
            "labels": _labels(name),
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": size}},
        },
    }
    if storage_class:
        manifest["spec"]["storageClassName"] = storage_class
    return manifest


def build_zeo_statefulset(name: str, namespace: str, spec: dict) -> dict:
    persistence = spec.get("persistence", {})
    persistence_enabled = persistence.get("enabled", True)

    container = {
        "name": "zeo",
        "image": "plone/plone-zeo:6",
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"name": "zeo", "containerPort": 8100, "protocol": "TCP"}],
        "env": [{"name": "ZEO_PORT", "value": "8100"}],
        "resources": {
            "limits": {"cpu": "500m", "memory": "512Mi"},
            "requests": {"cpu": "100m", "memory": "128Mi"},
        },
        "livenessProbe": {
            "tcpSocket": {"port": 8100},
            "initialDelaySeconds": 10,
            "periodSeconds": 30,
        },
        "readinessProbe": {
            "tcpSocket": {"port": 8100},
            "initialDelaySeconds": 5,
            "periodSeconds": 10,
        },
    }

    if persistence_enabled:
        container["volumeMounts"] = [{"name": "zeo-data", "mountPath": "/data"}]

    manifest = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {
            "name": f"{name}-zeo",
            "namespace": namespace,
            "labels": {**_labels(name), "app.kubernetes.io/component": "zeo"},
        },
        "spec": {
            "serviceName": f"{name}-zeo",
            "replicas": 1,
            "selector": {"matchLabels": {"app.kubernetes.io/part-of": name, "app.kubernetes.io/component": "zeo"}},
            "template": {
                "metadata": {"labels": {**_labels(name), "app.kubernetes.io/component": "zeo"}},
                "spec": {"containers": [container]},
            },
        },
    }

    if persistence_enabled:
        manifest["spec"]["volumeClaimTemplates"] = [
            {
                "metadata": {"name": "zeo-data"},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": persistence.get("size", "10Gi")}},
                    **({"storageClassName": persistence["storageClass"]} if persistence.get("storageClass") else {}),
                },
            }
        ]

    return manifest


def build_zeo_service(name: str, namespace: str, spec: dict) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": f"{name}-zeo",
            "namespace": namespace,
            "labels": {**_labels(name), "app.kubernetes.io/component": "zeo"},
        },
        "spec": {
            "clusterIP": "None",  # Headless for StatefulSet
            "selector": {"app.kubernetes.io/part-of": name, "app.kubernetes.io/component": "zeo"},
            "ports": [{"name": "zeo", "port": 8100, "targetPort": 8100, "protocol": "TCP"}],
        },
    }


def build_cnpg_cluster(name: str, namespace: str, spec: dict) -> dict:
    """Build a CloudNativePG Cluster CR for in-cluster PostgreSQL."""
    persistence = spec.get("persistence", {})
    storage_class = persistence.get("storageClass")
    size = persistence.get("size", "10Gi")

    cluster_spec = {
        "instances": 1,
        "storage": {
            "size": size,
            **({"storageClass": storage_class} if storage_class else {}),
        },
        "bootstrap": {
            "initdb": {
                "database": "plone",
                "owner": "plone",
                "secret": {"name": spec.get("database", {}).get("passwordSecret", "plonedb-credentials")},
            }
        },
    }

    return {
        "apiVersion": "postgresql.cnpg.io/v1",
        "kind": "Cluster",
        "metadata": {
            "name": f"{name}-db",
            "namespace": namespace,
            "labels": {**_labels(name), "app.kubernetes.io/component": "postgresql"},
        },
        "spec": cluster_spec,
    }


def build_backend_deployment(
    name: str,
    namespace: str,
    spec: dict,
    db_env: dict,
    db_secret_envs: list | None = None,
) -> dict:
    """
    Build the backend Deployment manifest.

    db_env: plain key/value env vars (may reference $(VAR_NAME) for k8s substitution)
    db_secret_envs: pre-built env entries that use secretKeyRef (injected BEFORE db_env
                    so that k8s variable substitution works for values like $(DB_PASSWORD))
    """
    site_id = spec.get("siteId", "plone")
    admin_password_secret = spec.get("adminPasswordSecret", "plone-admin-password")
    backend_image = spec.get("image", "plone/plone-backend:latest")
    replicas = spec.get("replicas", 1)
    resources = spec.get("resources", {})
    addons = spec.get("addons", [])
    extra_env = spec.get("environment", {})
    vhm_url = spec.get("vhmUrl", "")
    vhm_path = spec.get("vhmPath", site_id)

    # Build plain env vars
    env = {
        "SITE": site_id,
        **db_env,
    }
    if vhm_url:
        env["CORS_ALLOW_ORIGIN"] = vhm_url
        env["VHM_URL"] = vhm_url
        env["VHM_PATH"] = f"/{vhm_path}"
    if addons:
        env["ADDONS"] = " ".join(addons)
    env.update(extra_env)

    # Order: secret refs first (so $(VAR_NAME) substitution works), then plain vars
    env_list = list(db_secret_envs or [])
    env_list += _make_env_list(env)
    # Admin credentials from Secret
    env_list.append(_secret_env("ADMIN_USER", admin_password_secret, "username"))
    env_list.append(_secret_env("ADMIN_PASSWORD", admin_password_secret, "password"))

    resources_spec = {
        "limits": {
            "cpu": resources.get("limits", {}).get("cpu", "2000m"),
            "memory": resources.get("limits", {}).get("memory", "2Gi"),
        },
        "requests": {
            "cpu": resources.get("requests", {}).get("cpu", "500m"),
            "memory": resources.get("requests", {}).get("memory", "512Mi"),
        },
    }

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": f"{name}-backend",
            "namespace": namespace,
            "labels": {**_labels(name), "app.kubernetes.io/component": "backend"},
        },
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app.kubernetes.io/part-of": name, "app.kubernetes.io/component": "backend"}},
            "template": {
                "metadata": {"labels": {**_labels(name), "app.kubernetes.io/component": "backend"}},
                "spec": {
                    "containers": [
                        {
                            "name": "backend",
                            "image": backend_image,
                            "imagePullPolicy": "IfNotPresent",
                            "ports": [{"name": "http", "containerPort": 8080, "protocol": "TCP"}],
                            "env": env_list,
                            "resources": resources_spec,
                            "livenessProbe": {
                                "httpGet": {"path": f"/{site_id}", "port": 8080},
                                "initialDelaySeconds": 60,
                                "periodSeconds": 30,
                                "timeoutSeconds": 10,
                                "failureThreshold": 5,
                            },
                            "readinessProbe": {
                                "httpGet": {"path": f"/{site_id}", "port": 8080},
                                "initialDelaySeconds": 30,
                                "periodSeconds": 10,
                                "timeoutSeconds": 5,
                                "failureThreshold": 3,
                            },
                        }
                    ]
                },
            },
        },
    }


def build_backend_service(name: str, namespace: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": f"{name}-backend",
            "namespace": namespace,
            "labels": {**_labels(name), "app.kubernetes.io/component": "backend"},
        },
        "spec": {
            "selector": {"app.kubernetes.io/part-of": name, "app.kubernetes.io/component": "backend"},
            "ports": [{"name": "http", "port": 8080, "targetPort": 8080, "protocol": "TCP"}],
        },
    }


def build_frontend_deployment(name: str, namespace: str, spec: dict) -> dict:
    site_id = spec.get("siteId", "plone")
    vhm_url = spec.get("vhmUrl", "")
    frontend_image = spec.get("frontendImage", "plone/plone-frontend:latest")
    replicas = spec.get("replicas", 1)

    backend_svc = f"{name}-backend.{namespace}.svc.cluster.local"
    internal_api = f"http://{backend_svc}:8080/{site_id}"
    # RAZZLE_API_PATH is used by the browser, so it must be a publicly reachable URL.
    # When vhmUrl is set, use it directly (scheme+host only, no path).
    # When vhmUrl is not set, use the frontend's own cluster URL so Volto's Node.js
    # server can proxy /++api++/ requests to RAZZLE_INTERNAL_API_PATH.
    frontend_svc = f"{name}-frontend.{namespace}.svc.cluster.local"
    public_api = vhm_url if vhm_url else f"http://{frontend_svc}:3000"

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": f"{name}-frontend",
            "namespace": namespace,
            "labels": {**_labels(name), "app.kubernetes.io/component": "frontend"},
        },
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app.kubernetes.io/part-of": name, "app.kubernetes.io/component": "frontend"}},
            "template": {
                "metadata": {"labels": {**_labels(name), "app.kubernetes.io/component": "frontend"}},
                "spec": {
                    "containers": [
                        {
                            "name": "frontend",
                            "image": frontend_image,
                            "imagePullPolicy": "IfNotPresent",
                            "ports": [{"name": "http", "containerPort": 3000, "protocol": "TCP"}],
                            "env": _make_env_list({
                                "RAZZLE_INTERNAL_API_PATH": internal_api,
                                "RAZZLE_API_PATH": public_api,
                            }),
                            "resources": {
                                "limits": {"cpu": "1000m", "memory": "1Gi"},
                                "requests": {"cpu": "200m", "memory": "256Mi"},
                            },
                            "livenessProbe": {
                                "httpGet": {"path": "/", "port": 3000},
                                "initialDelaySeconds": 30,
                                "periodSeconds": 30,
                                "timeoutSeconds": 10,
                            },
                            "readinessProbe": {
                                "httpGet": {"path": "/", "port": 3000},
                                "initialDelaySeconds": 15,
                                "periodSeconds": 10,
                                "timeoutSeconds": 5,
                            },
                        }
                    ]
                },
            },
        },
    }


def build_frontend_service(name: str, namespace: str, spec: dict) -> dict:
    vhm_url = spec.get("vhmUrl", "")
    # Use NodePort when no vhmUrl so `minikube service` can provide an accessible
    # URL without requiring manual port-forwarding in local dev.
    svc_type = "ClusterIP" if vhm_url else "NodePort"
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": f"{name}-frontend",
            "namespace": namespace,
            "labels": {**_labels(name), "app.kubernetes.io/component": "frontend"},
        },
        "spec": {
            "type": svc_type,
            "selector": {"app.kubernetes.io/part-of": name, "app.kubernetes.io/component": "frontend"},
            "ports": [{"name": "http", "port": 3000, "targetPort": 3000, "protocol": "TCP"}],
        },
    }


def build_ingress(name: str, namespace: str, spec: dict) -> dict:
    vhm_url = spec.get("vhmUrl", "")
    ingress_cfg = spec.get("ingress", {})
    ingress_class = ingress_cfg.get("className", "")
    tls_enabled = ingress_cfg.get("tls", False)

    # Strip scheme from vhmUrl to get hostname
    host = vhm_url.replace("https://", "").replace("http://", "").rstrip("/")
    backend_port = 3000 if spec.get("deploymentType", "volto") == "volto" else 8080
    backend_svc_name = f"{name}-frontend" if spec.get("deploymentType", "volto") == "volto" else f"{name}-backend"

    manifest = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": _labels(name),
            "annotations": {},
        },
        "spec": {
            "rules": [
                {
                    "host": host,
                    "http": {
                        "paths": [
                            {
                                "path": "/",
                                "pathType": "Prefix",
                                "backend": {
                                    "service": {
                                        "name": backend_svc_name,
                                        "port": {"number": backend_port},
                                    }
                                },
                            }
                        ]
                    },
                }
            ]
        },
    }
    if ingress_class:
        manifest["spec"]["ingressClassName"] = ingress_class
    if tls_enabled:
        manifest["spec"]["tls"] = [{"hosts": [host], "secretName": f"{name}-tls"}]
    return manifest


# ---------------------------------------------------------------------------
# Apply helpers using kubernetes python client
# ---------------------------------------------------------------------------

def _apply_manifest(manifest: dict) -> None:
    """
    Apply a single manifest using server-side apply (SSA).

    SSA uses a strategic merge with field ownership and force=True so that the
    operator can reclaim fields it previously set.  This correctly handles cases
    that merge-patch cannot (e.g. changing Service.spec.type, adding new env
    vars to a Deployment, etc.).
    """
    kind = manifest["kind"]
    name = manifest["metadata"]["name"]
    namespace = manifest["metadata"].get("namespace")

    dyn_client = kubernetes.dynamic.DynamicClient(
        kubernetes.client.ApiClient()
    )

    resource = dyn_client.resources.get(
        api_version=manifest["apiVersion"], kind=kind
    )

    # SSA requires a fieldManager and force=True so we own all fields we set.
    kwargs = dict(
        body=manifest,
        name=name,
        field_manager="plone-operator",
        force=True,
        content_type="application/apply-patch+yaml",
    )
    if namespace:
        kwargs["namespace"] = namespace

    resource.server_side_apply(**kwargs)
    logger.info("Applied %s/%s", kind, name)


# ---------------------------------------------------------------------------
# Readiness polling
# ---------------------------------------------------------------------------

async def _wait_for_plone(backend_svc: str, namespace: str, site_id: str, timeout: int = 300) -> None:
    """
    Poll the Plone REST API until the site responds with HTTP 200.
    Raises kopf.TemporaryError if not ready within timeout.
    """
    url = f"http://{backend_svc}.{namespace}.svc.cluster.local:8080/{site_id}"
    headers = {"Accept": "application/json"}
    deadline = asyncio.get_event_loop().time() + timeout

    logger.info("Waiting for Plone site at %s", url)

    async with aiohttp.ClientSession() as session:
        while asyncio.get_event_loop().time() < deadline:
            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        logger.info("Plone site is ready at %s", url)
                        return
                    logger.debug("Plone not ready yet (HTTP %d), retrying...", resp.status)
            except Exception as exc:
                logger.debug("Plone not reachable yet (%s), retrying...", exc)
            await asyncio.sleep(15)

    raise kopf.TemporaryError(
        f"Plone site at {url} did not become ready within {timeout}s", delay=30
    )


# ---------------------------------------------------------------------------
# CNPG readiness: wait for the CNPG cluster service to exist
# ---------------------------------------------------------------------------

async def _wait_for_cnpg_service(name: str, namespace: str, timeout: int = 300) -> str:
    """
    Wait until the CNPG-managed Service (name-db-rw) appears, then return its hostname.
    """
    svc_name = f"{name}-db-rw"
    core_v1 = k8s_client.CoreV1Api()
    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        try:
            core_v1.read_namespaced_service(svc_name, namespace)
            logger.info("CNPG service %s/%s is ready", namespace, svc_name)
            return svc_name
        except ApiException as e:
            if e.status == 404:
                logger.debug("CNPG service %s not ready yet, waiting...", svc_name)
                await asyncio.sleep(10)
            else:
                raise

    raise kopf.TemporaryError(
        f"CNPG service {svc_name} did not appear within {timeout}s", delay=30
    )


# ---------------------------------------------------------------------------
# Main reconcile handler
# ---------------------------------------------------------------------------

@kopf.on.create("plone.org", "v1alpha1", "plonesites")
@kopf.on.update("plone.org", "v1alpha1", "plonesites")
@kopf.on.resume("plone.org", "v1alpha1", "plonesites")
async def reconcile(spec, meta, status, patch, logger, **kwargs):
    name = _name(meta)
    namespace = _namespace(meta)

    logger.info("Reconciling PloneSite %s/%s", namespace, name)

    # Mark as Pending
    patch.status["phase"] = "Pending"
    patch.status["conditions"] = [
        {
            "type": "Reconciling",
            "status": "True",
            "lastTransitionTime": datetime.datetime.utcnow().isoformat() + "Z",
            "reason": "ReconcileStarted",
            "message": "Operator is reconciling the PloneSite",
        }
    ]

    db_type = spec.get("database", {}).get("type", "zodb")
    db_env = {}
    db_secret_envs = []  # secretKeyRef entries injected before plain db_env
    manifests = []

    # -------------------------------------------------------------------
    # Database layer
    # -------------------------------------------------------------------
    if db_type == "zodb":
        # ZEO StatefulSet + headless Service (PVC managed via volumeClaimTemplates)
        zeo_ss = build_zeo_statefulset(name, namespace, spec)
        zeo_svc = build_zeo_service(name, namespace, spec)
        kopf.adopt(zeo_ss)
        kopf.adopt(zeo_svc)
        manifests += [zeo_ss, zeo_svc]
        db_env["ZEO_ADDRESS"] = f"{name}-zeo.{namespace}.svc.cluster.local:8100"

    elif db_type == "postgresql":
        db_cfg = spec.get("database", {})
        db_host = db_cfg.get("host", "")
        password_secret = db_cfg.get("passwordSecret", "plonedb-credentials")

        # DB_PASSWORD is injected from the Secret first so that Kubernetes
        # variable substitution resolves $(DB_PASSWORD) in RELSTORAGE_DSN.
        db_secret_envs.append(_secret_env("DB_PASSWORD", password_secret, "password"))

        if db_host:
            # External PostgreSQL
            db_port = db_cfg.get("port", 5432)
            db_name = db_cfg.get("name", "plone")
            db_user = db_cfg.get("user", "plone")
            db_env["RELSTORAGE_DSN"] = (
                f"host={db_host} port={db_port} dbname={db_name} "
                f"user={db_user} password=$(DB_PASSWORD)"
            )
        else:
            # In-cluster PostgreSQL via CloudNativePG
            cnpg_cluster = build_cnpg_cluster(name, namespace, spec)
            kopf.adopt(cnpg_cluster)
            manifests.append(cnpg_cluster)
            # Apply CNPG cluster first so it starts provisioning
            _apply_manifest(cnpg_cluster)
            # Wait for the CNPG read-write service to appear
            svc_name = await _wait_for_cnpg_service(name, namespace)
            db_env["RELSTORAGE_DSN"] = (
                f"host={svc_name}.{namespace}.svc.cluster.local port=5432 "
                f"dbname=plone user=plone password=$(DB_PASSWORD)"
            )

    # -------------------------------------------------------------------
    # Backend Deployment + Service
    # -------------------------------------------------------------------
    backend_deploy = build_backend_deployment(name, namespace, spec, db_env, db_secret_envs)
    backend_svc = build_backend_service(name, namespace)
    kopf.adopt(backend_deploy)
    kopf.adopt(backend_svc)
    manifests += [backend_deploy, backend_svc]

    # -------------------------------------------------------------------
    # Frontend Deployment + Service (Volto only)
    # -------------------------------------------------------------------
    deployment_type = spec.get("deploymentType", "volto")
    if deployment_type == "volto":
        frontend_deploy = build_frontend_deployment(name, namespace, spec)
        frontend_svc = build_frontend_service(name, namespace, spec)
        kopf.adopt(frontend_deploy)
        kopf.adopt(frontend_svc)
        manifests += [frontend_deploy, frontend_svc]

    # -------------------------------------------------------------------
    # Ingress (when vhmUrl is set and ingress.enabled is true)
    # -------------------------------------------------------------------
    vhm_url = spec.get("vhmUrl", "")
    ingress_enabled = spec.get("ingress", {}).get("enabled", False)
    if vhm_url and ingress_enabled:
        ingress = build_ingress(name, namespace, spec)
        kopf.adopt(ingress)
        manifests.append(ingress)

    # -------------------------------------------------------------------
    # Apply all manifests (skipping CNPG which was already applied above)
    # -------------------------------------------------------------------
    for manifest in manifests:
        if manifest.get("apiVersion", "").startswith("postgresql.cnpg.io"):
            continue  # Already applied
        _apply_manifest(manifest)

    # -------------------------------------------------------------------
    # Wait for Plone backend to initialise the site
    # -------------------------------------------------------------------
    site_id = spec.get("siteId", "plone")
    backend_svc_name = f"{name}-backend"
    await _wait_for_plone(backend_svc_name, namespace, site_id)

    # -------------------------------------------------------------------
    # Determine site URL for status
    # -------------------------------------------------------------------
    if vhm_url:
        site_url = vhm_url
    elif deployment_type == "volto":
        site_url = f"http://{name}-frontend.{namespace}.svc.cluster.local:3000"
    else:
        site_url = f"http://{name}-backend.{namespace}.svc.cluster.local:8080/{site_id}"

    # -------------------------------------------------------------------
    # Update status
    # -------------------------------------------------------------------
    patch.status["phase"] = "Running"
    patch.status["siteUrl"] = site_url
    patch.status["deploymentType"] = deployment_type
    patch.status["vhmConfigured"] = bool(vhm_url)
    patch.status["conditions"] = [
        {
            "type": "Ready",
            "status": "True",
            "lastTransitionTime": datetime.datetime.utcnow().isoformat() + "Z",
            "reason": "ReconcileComplete",
            "message": "PloneSite is running",
        }
    ]
    logger.info("PloneSite %s/%s is Running at %s", namespace, name, site_url)


# ---------------------------------------------------------------------------
# Delete handler
# ---------------------------------------------------------------------------

@kopf.on.delete("plone.org", "v1alpha1", "plonesites")
async def on_delete(meta, spec, logger, **kwargs):
    """
    Owner references (set by kopf.adopt) handle garbage collection of child
    resources automatically. This handler is a no-op but logs the deletion.
    """
    name = _name(meta)
    namespace = _namespace(meta)
    logger.info(
        "PloneSite %s/%s deleted — child resources will be garbage-collected via ownerReferences",
        namespace,
        name,
    )


# ---------------------------------------------------------------------------
# Weekly database pack job
# ---------------------------------------------------------------------------

@kopf.timer("plone.org", "v1alpha1", "plonesites", interval=604800.0, idle=300.0)
async def db_pack(spec, meta, logger, **kwargs):
    """Create a one-off Job to pack the database weekly."""
    name = _name(meta)
    namespace = _namespace(meta)
    db_type = spec.get("database", {}).get("type", "zodb")

    ts = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    job_name = f"{name}-pack-{ts}"

    backend_image = spec.get("image", "plone/plone-backend:latest")
    db_cfg = spec.get("database", {})

    if db_type == "zodb":
        # plone/plone-zeo ships zeopack at /app/bin/zeopack.
        # It accepts servers as positional args: host:port
        zeo_host = f"{name}-zeo.{namespace}.svc.cluster.local"
        pack_image = "plone/plone-zeo:6"
        pack_env = []
        pack_command = ["/app/bin/zeopack", f"{zeo_host}:8100"]
    elif db_type == "postgresql":
        # Use the backend image which has RelStorage installed.
        # /app/bin/python is the venv Python inside the backend image.
        db_host = db_cfg.get("host", f"{name}-db-rw.{namespace}.svc.cluster.local")
        db_port = db_cfg.get("port", 5432)
        db_name = db_cfg.get("name", "plone")
        db_user = db_cfg.get("user", "plone")
        password_secret = db_cfg.get("passwordSecret", "plonedb-credentials")
        # DB_PASSWORD must come before RELSTORAGE_DSN for k8s substitution
        pack_image = backend_image
        pack_env = [
            _secret_env("DB_PASSWORD", password_secret, "password"),
            *_make_env_list({
                "RELSTORAGE_DSN": (
                    f"host={db_host} port={db_port} "
                    f"dbname={db_name} user={db_user} password=$(DB_PASSWORD)"
                ),
            }),
        ]
        pack_command = [
            "/app/bin/python", "-c",
            (
                "import os; "
                "from relstorage.options import Options; "
                "from relstorage.adapters.postgresql import PostgreSQLAdapter; "
                "from relstorage.storage import RelStorage; "
                "dsn = os.environ['RELSTORAGE_DSN']; "
                "opts = Options(); "
                "adapter = PostgreSQLAdapter(dsn=dsn, options=opts); "
                "storage = RelStorage(adapter, options=opts); "
                "storage.pack(days=0); "
                "storage.close(); "
                "print('RelStorage pack complete')"
            ),
        ]
    else:
        logger.warning("Unknown db_type %s, skipping pack job", db_type)
        return

    job_manifest = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": namespace,
            "labels": {**_labels(name), "app.kubernetes.io/component": "db-pack"},
        },
        "spec": {
            "ttlSecondsAfterFinished": 86400,  # Auto-clean after 24h
            "backoffLimit": 2,
            "template": {
                "metadata": {"labels": {**_labels(name), "app.kubernetes.io/component": "db-pack"}},
                "spec": {
                    "restartPolicy": "OnFailure",
                    "containers": [
                        {
                            "name": "pack",
                            "image": pack_image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": pack_command,
                            **({"env": pack_env} if pack_env else {}),
                        }
                    ],
                },
            },
        },
    }

    kopf.adopt(job_manifest)
    _apply_manifest(job_manifest)
    logger.info("Created DB pack job %s/%s", namespace, job_name)
