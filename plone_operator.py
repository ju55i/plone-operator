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
from typing import Any

import aiohttp
import kopf
import kubernetes
from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Startup: load kubeconfig (in-cluster or local for development)
# ---------------------------------------------------------------------------

@kopf.on.startup()  # ty: ignore[invalid-argument-type]
def configure(settings: kopf.OperatorSettings, **_):
    """Load Kubernetes credentials and configure operator settings."""
    try:
        kubernetes.config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes config")
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()
        logger.info("Loaded local kubeconfig (development mode)")

    # Kopf health-check endpoint (used by manager.yaml probes)
    settings.peering.enabled = False  # ty: ignore[unresolved-attribute]
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


def _labels(name: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/managed-by": "plone-operator",
        "app.kubernetes.io/part-of": name,
    }


def _make_env_list(env_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert a plain dict of env vars to a k8s env list."""
    return [{"name": k, "value": str(v)} for k, v in env_dict.items()]


def _vhm_rewrite_path(vhm_url: str, vhm_path: str) -> str:
    """
    Build the Zope VirtualHostMonster traversal path prefix.

    Given a public URL like ``https://example.com`` and Plone site path
    ``Plone``, returns::

        /VirtualHostBase/https/example.com/Plone/VirtualHostRoot

    This prefix is used as-is in Traefik's ``replacePathRegex`` middleware
    replacement (the caller appends ``/$1`` for the captured request path).
    Equivalent to the ``proxy_pass`` target in nginx::

        proxy_pass http://backend:8080/VirtualHostBase/https/example.com/Plone/VirtualHostRoot/;

    Port handling:
    - https on 443  → omitted (Plone standard)
    - http  on 80   → omitted (Plone standard)
    - any other explicit port → preserved
    """
    from urllib.parse import urlparse

    parsed = urlparse(vhm_url.rstrip("/"))
    scheme = parsed.scheme or "http"
    hostname = parsed.hostname or ""
    port = parsed.port

    # Omit standard ports so Plone doesn't include them in generated URLs.
    if port and not (scheme == "http" and port == 80) and not (scheme == "https" and port == 443):
        host_part = f"{hostname}:{port}"
    else:
        host_part = hostname

    return f"/VirtualHostBase/{scheme}/{host_part}/{vhm_path}/VirtualHostRoot"


def _secret_env(var_name: str, secret_name: str, secret_key: str) -> dict[str, Any]:
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

def build_zeo_pvc(name: str, namespace: str, spec: dict[str, Any]) -> dict[str, Any]:
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


def build_zeo_statefulset(name: str, namespace: str, spec: dict[str, Any]) -> dict[str, Any]:
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

    manifest: dict[str, Any] = {
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
        manifest["spec"]["volumeClaimTemplates"] = [  # ty: ignore[invalid-assignment]
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


def build_zeo_service(name: str, namespace: str, spec: dict[str, Any]) -> dict[str, Any]:
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


def build_cnpg_cluster(name: str, namespace: str, spec: dict[str, Any]) -> dict[str, Any]:
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
    spec: dict[str, Any],
    db_env: dict[str, Any],
    db_secret_envs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
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

    # Build plain env vars
    env = {
        "SITE": site_id,
        **db_env,
    }
    if vhm_url:
        env["CORS_ALLOW_ORIGIN"] = vhm_url
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


def build_backend_service(name: str, namespace: str) -> dict[str, Any]:
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


def build_frontend_deployment(name: str, namespace: str, spec: dict[str, Any]) -> dict[str, Any]:
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


def build_frontend_service(name: str, namespace: str, spec: dict[str, Any]) -> dict[str, Any]:
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


def build_traefik_vhm_middleware(name: str, namespace: str, spec: dict[str, Any]) -> dict[str, Any]:
    """
    Build a Traefik Middleware manifest that rewrites request paths for
    Zope's VirtualHostMonster.

    Only used for Classic UI deployments where the Ingress routes directly
    to the Zope backend.  Traefik applies this middleware before proxying,
    transforming e.g.::

        GET /news  →  GET /VirtualHostBase/https/example.com/Plone/VirtualHostRoot/news

    This is equivalent to the nginx proxy_pass pattern::

        proxy_pass http://backend:8080/VirtualHostBase/https/example.com/Plone/VirtualHostRoot/;

    Traefik v3 uses ``traefik.io/v1alpha1``; Traefik v2 uses
    ``traefik.containo.us/v1alpha1`` — adjust ``apiVersion`` if needed.
    """
    vhm_url = spec.get("vhmUrl", "")
    site_id = spec.get("siteId", "plone")
    vhm_path = spec.get("vhmPath", site_id)
    rewrite_base = _vhm_rewrite_path(vhm_url, vhm_path)

    return {
        "apiVersion": "traefik.io/v1alpha1",
        "kind": "Middleware",
        "metadata": {
            "name": f"{name}-vhm",
            "namespace": namespace,
            "labels": _labels(name),
        },
        "spec": {
            "replacePathRegex": {
                # Capture everything after the leading slash and prepend the
                # full VHM traversal prefix.  An empty path (bare "/") maps
                # to the VirtualHostRoot itself, which Zope handles correctly.
                "regex": "^/(.*)",
                "replacement": f"{rewrite_base}/$1",
            }
        },
    }


def build_ingress(name: str, namespace: str, spec: dict[str, Any]) -> dict[str, Any]:
    vhm_url = spec.get("vhmUrl", "")
    ingress_cfg = spec.get("ingress", {})
    ingress_class = ingress_cfg.get("className", "traefik")
    tls_enabled = ingress_cfg.get("tls", False)
    deployment_type = spec.get("deploymentType", "volto")

    # Extract hostname from public URL (strip scheme and trailing slash).
    from urllib.parse import urlparse
    host = urlparse(vhm_url.rstrip("/")).hostname or ""

    if deployment_type == "volto":
        # Volto: Ingress routes to the frontend; Node.js SSR proxies /++api++/
        # to the backend internally.  No VHM rewrite needed on the Ingress.
        backend_port = 3000
        backend_svc_name = f"{name}-frontend"
        annotations: dict[str, str] = {}
    else:
        # Classic UI: Ingress routes directly to Zope.  The Traefik Middleware
        # <name>-vhm (built by build_traefik_vhm_middleware) rewrites the path
        # to the VirtualHostMonster traversal URL before proxying.
        backend_port = 8080
        backend_svc_name = f"{name}-backend"
        # Middleware reference format: <namespace>-<middleware-name>@kubernetescrd
        annotations = {
            "traefik.ingress.kubernetes.io/router.middlewares": f"{namespace}-{name}-vhm@kubernetescrd",
        }

    manifest = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": _labels(name),
            "annotations": annotations,
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

def _apply_manifest(manifest: dict[str, Any]) -> None:
    """
    Apply a single manifest using server-side apply (SSA).

    SSA uses field ownership with force_conflicts=True so the operator can
    reclaim fields previously owned by other managers (e.g. changing
    Service.spec.type).

    StatefulSet caveat: spec.selector, spec.serviceName, spec.podManagementPolicy,
    and spec.volumeClaimTemplates are immutable after creation.  Kubernetes
    rejects SSA attempts to take ownership of them even with force_conflicts,
    so we strip those fields from the patch body.  They are only present in the
    full manifest used for initial creation.
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

    # Strip immutable StatefulSet fields so SSA does not try to claim ownership
    # of fields it can never legally change.
    body = manifest
    if kind == "StatefulSet":
        spec = {k: v for k, v in manifest.get("spec", {}).items()
                if k not in ("volumeClaimTemplates", "selector", "serviceName", "podManagementPolicy")}
        body = {**manifest, "spec": spec}

    kwargs: dict = dict(
        body=body,
        name=name,
        field_manager="plone-operator",
        force_conflicts=True,
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
@kopf.on.resume("plone.org", "v1alpha1", "plonesites")  # ty: ignore[invalid-argument-type]
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
            "lastTransitionTime": datetime.datetime.now(datetime.UTC).isoformat(),
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
    # Ingress + Traefik VHM Middleware (when vhmUrl is set and ingress.enabled)
    # -------------------------------------------------------------------
    vhm_url = spec.get("vhmUrl", "")
    ingress_enabled = spec.get("ingress", {}).get("enabled", False)
    if vhm_url and ingress_enabled:
        ingress = build_ingress(name, namespace, spec)
        kopf.adopt(ingress)
        manifests.append(ingress)
        # Classic UI: add the Traefik Middleware that rewrites paths for VHM.
        # (Volto routes to the frontend; no VHM rewrite needed on the Ingress.)
        if deployment_type != "volto":
            vhm_middleware = build_traefik_vhm_middleware(name, namespace, spec)
            kopf.adopt(vhm_middleware)
            manifests.append(vhm_middleware)

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
            "lastTransitionTime": datetime.datetime.now(datetime.UTC).isoformat(),
            "reason": "ReconcileComplete",
            "message": "PloneSite is running",
        }
    ]
    logger.info("PloneSite %s/%s is Running at %s", namespace, name, site_url)


# ---------------------------------------------------------------------------
# Delete handler
# ---------------------------------------------------------------------------

@kopf.on.delete("plone.org", "v1alpha1", "plonesites")  # ty: ignore[invalid-argument-type]
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

@kopf.timer("plone.org", "v1alpha1", "plonesites", interval=604800.0, idle=300.0)  # ty: ignore[invalid-argument-type]
async def db_pack(spec, meta, logger, **kwargs):
    """Create a one-off Job to pack the database weekly."""
    name = _name(meta)
    namespace = _namespace(meta)
    db_type = spec.get("database", {}).get("type", "zodb")

    ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M%S")
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
