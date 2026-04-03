"""
Plone Operator — Kopf-based Kubernetes operator for managing Plone 6 CMS.

Manages the full lifecycle of a PloneSite custom resource:
  - ZEO (ZODB) or external/in-cluster PostgreSQL (RelStorage via CNPG)
  - Volto (frontend + backend) or Classic (backend only) deployment types
  - Automatic Plone site initialisation via REST API polling
  - Weekly database packing via one-off Kubernetes Jobs
"""

import asyncio
import base64
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


def _classic_rewrite_path(public_url: str, site_path: str) -> str:
    """
    Build the Zope VirtualHostMonster traversal path prefix for Classic UI.

    Given a public URL like ``https://example.com`` and Plone site path
    ``Plone``, returns::

        /VirtualHostBase/https/example.com/Plone/VirtualHostRoot

    This prefix is used as-is in Traefik's ``replacePathRegex`` middleware
    replacement (the caller appends ``/$1`` for the captured request path).

    Port handling:
    - https on 443  → omitted (Plone standard)
    - http  on 80   → omitted (Plone standard)
    - any other explicit port → preserved
    """
    from urllib.parse import urlparse

    parsed = urlparse(public_url.rstrip("/"))
    scheme = parsed.scheme or "http"
    hostname = parsed.hostname or ""
    port = parsed.port

    # Omit standard ports so Plone doesn't include them in generated URLs.
    if port and not (scheme == "http" and port == 80) and not (scheme == "https" and port == 443):
        host_part = f"{hostname}:{port}"
    else:
        host_part = hostname

    return f"/VirtualHostBase/{scheme}/{host_part}/{site_path}/VirtualHostRoot"


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

    # The ZEO container always mounts /data; the backing volume is either a
    # PVC (persistence.enabled: true, the default) or an emptyDir for
    # ephemeral/CI use cases (persistence.enabled: false).
    container["volumeMounts"] = [{"name": "zeo-data", "mountPath": "/data"}]

    pod_spec: dict[str, Any] = {"containers": [container]}
    if not persistence_enabled:
        pod_spec["volumes"] = [{"name": "zeo-data", "emptyDir": {}}]

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
                "spec": pod_spec,
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
    """Build a CloudNativePG Cluster CR for in-cluster PostgreSQL.

    CNPG auto-generates the application user credentials and writes them to a
    Secret named ``<name>-db-app`` with keys: host, port, dbname, username,
    password, uri, jdbc-uri, pgpass.  The operator reads that secret at runtime.

    Note: ``database.credentialsSecret`` in the PloneSite spec is intentionally
    NOT passed to CNPG bootstrap.  Passing an ``initdb.secret`` suppresses
    CNPG's automatic creation of the ``<name>-db-app`` Secret.  For external
    PostgreSQL (cnpg: false) the credentialsSecret is used directly.
    """
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
    admin_password_secret = f"{name}-admin"
    backend_image = spec.get("image", "plone/plone-backend:latest")
    replicas = spec.get("replicas", 1)
    resources = spec.get("resources", {})
    addons = spec.get("addons", [])
    extra_env = spec.get("environment", {})
    public_url = spec.get("publicUrl", "")

    # Build plain env vars
    env = {
        "SITE": site_id,
        **db_env,
    }
    if public_url:
        env["CORS_ALLOW_ORIGIN"] = public_url
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
    public_url = spec.get("publicUrl", "")
    frontend_image = spec.get("frontendImage", "plone/plone-frontend:latest")
    replicas = spec.get("replicas", 1)

    backend_svc = f"{name}-backend.{namespace}.svc.cluster.local"
    internal_api = f"http://{backend_svc}:8080/{site_id}"
    # RAZZLE_API_PATH is used by the browser, so it must be a publicly reachable URL.
    # When publicUrl is set, use it directly (scheme+host only, no path).
    # When publicUrl is not set, use the frontend's own cluster URL so Volto's Node.js
    # server can proxy /++api++/ requests to RAZZLE_INTERNAL_API_PATH.
    frontend_svc = f"{name}-frontend.{namespace}.svc.cluster.local"
    public_api = public_url if public_url else f"http://{frontend_svc}:3000"

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
    public_url = spec.get("publicUrl", "")
    # Use NodePort when no publicUrl so `minikube service` can provide an accessible
    # URL without requiring manual port-forwarding in local dev.
    svc_type = "ClusterIP" if public_url else "NodePort"
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


def build_traefik_middleware(name: str, namespace: str, spec: dict[str, Any]) -> dict[str, Any]:
    """
    Build a Traefik Middleware manifest that rewrites request paths for
    Zope's VirtualHostMonster.

    Used for both Classic UI and Volto deployments.  The VHM rewrite ensures
    Plone generates public URLs without the Plone site object ID in the path.

    Classic UI
        Rewrites every request path::

            GET /news  →  GET /VirtualHostBase/https/example.com/Plone/VirtualHostRoot/news

    Volto
        Rewrites only ``/++api++`` paths (other paths go to the React frontend
        and must not be rewritten).  ``replacePathRegex`` is a no-op when the
        regex does not match, so the ``/`` → frontend route is unaffected::

            GET /++api++/@config
              →  GET /VirtualHostBase/https/example.com/Plone/VirtualHostRoot/++api++/@config

    Traefik v3 uses ``traefik.io/v1alpha1``; Traefik v2 uses
    ``traefik.containo.us/v1alpha1`` — adjust ``apiVersion`` if needed.
    """
    public_url = spec.get("publicUrl", "")
    site_id = spec.get("siteId", "plone")
    site_path = spec.get("sitePath", site_id)
    deployment_type = spec.get("deploymentType", "volto")
    rewrite_base = _classic_rewrite_path(public_url, site_path)

    if deployment_type == "volto":
        # Only rewrite /++api++/* paths; everything else passes through unchanged.
        regex = r"^/\+\+api\+\+(.*)"
        replacement = f"{rewrite_base}/++api++$1"
    else:
        # Classic UI: rewrite all paths.
        regex = "^/(.*)"
        replacement = f"{rewrite_base}/$1"

    return {
        "apiVersion": "traefik.io/v1alpha1",
        "kind": "Middleware",
        "metadata": {
            "name": f"{name}-rewrite",
            "namespace": namespace,
            "labels": _labels(name),
        },
        "spec": {
            "replacePathRegex": {
                "regex": regex,
                "replacement": replacement,
            }
        },
    }


def build_ingress(name: str, namespace: str, spec: dict[str, Any]) -> dict[str, Any]:
    """
    Build the Ingress for this PloneSite.

    Classic UI
        Single path ``/`` → backend:8080.  The VHM Middleware annotation is
        attached so Traefik rewrites every request path to the Zope
        VirtualHostMonster traversal URL before proxying.

    Volto
        Two paths in one Ingress (pathType Prefix, most-specific wins):

        * ``/++api++`` → backend:8080  — REST API requests go straight to Zope.
          No VHM middleware annotation; Traefik passes ``X-Forwarded-Host`` /
          ``X-Forwarded-Proto`` headers that Zope uses to generate correct
          public URLs.
        * ``/``        → frontend:3000 — everything else goes to the Volto
          React frontend.
    """
    from urllib.parse import urlparse

    public_url = spec.get("publicUrl", "")
    ingress_cfg = spec.get("ingress", {})
    ingress_class = ingress_cfg.get("className", "traefik")
    tls_enabled = ingress_cfg.get("tls", False)
    deployment_type = spec.get("deploymentType", "volto")
    host = urlparse(public_url.rstrip("/")).hostname or ""

    if deployment_type == "volto":
        paths = [
            {
                "path": "/++api++",
                "pathType": "Prefix",
                "backend": {"service": {"name": f"{name}-backend", "port": {"number": 8080}}},
            },
            {
                "path": "/",
                "pathType": "Prefix",
                "backend": {"service": {"name": f"{name}-frontend", "port": {"number": 3000}}},
            },
        ]
        # VHM middleware rewrites /++api++/* paths before they reach the backend;
        # its regex does not match "/" so the frontend route is unaffected.
        annotations = {
            "traefik.ingress.kubernetes.io/router.middlewares": f"{namespace}-{name}-rewrite@kubernetescrd",
        }
    else:
        paths = [
            {
                "path": "/",
                "pathType": "Prefix",
                "backend": {"service": {"name": f"{name}-backend", "port": {"number": 8080}}},
            },
        ]
        # Middleware reference format: <namespace>-<middleware-name>@kubernetescrd
        annotations = {
            "traefik.ingress.kubernetes.io/router.middlewares": f"{namespace}-{name}-rewrite@kubernetescrd",
        }

    manifest: dict[str, Any] = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": _labels(name),
            "annotations": annotations,
        },
        "spec": {
            "rules": [{"host": host, "http": {"paths": paths}}],
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

    StatefulSet note: immutable fields (selector, serviceName,
    volumeClaimTemplates, podManagementPolicy) are included in every SSA
    patch unchanged.  Kubernetes validates the patch body for required-field
    consistency, so omitting them causes a 422.  With force_conflicts=True
    and unchanged values Kubernetes accepts them; it only rejects if the
    value differs from what is already stored.
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

    # SSA note for StatefulSets: immutable fields (selector, serviceName,
    # volumeClaimTemplates, podManagementPolicy) must be included in every
    # patch with their original values.  Kubernetes validates the patch body
    # for required-field consistency, so stripping them causes a 422 even on
    # updates.  With force_conflicts=True and unchanged values Kubernetes
    # accepts them fine — it only rejects if the value differs from what is
    # already stored.
    body = manifest

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
# Plone DB upgrade via REST API
# ---------------------------------------------------------------------------

async def _run_plone_upgrade(
    backend_svc: str,
    namespace: str,
    site_id: str,
    admin_secret_name: str,
    timeout: int = 300,
) -> bool:
    """Check for pending Plone DB migration steps and run them via the REST API.

    Calls ``GET /{site_id}/@upgrade`` to detect pending steps, then
    ``POST /{site_id}/@upgrade`` to execute them if needed.  Both calls use
    HTTP Basic auth with the credentials from the admin Secret.

    Returns True if an upgrade was performed, False if the site was already
    up to date.  Raises ``kopf.TemporaryError`` on unexpected HTTP responses.
    """
    core_v1 = k8s_client.CoreV1Api()
    secret = core_v1.read_namespaced_secret(admin_secret_name, namespace)
    username = base64.b64decode(secret.data["username"]).decode()
    password = base64.b64decode(secret.data["password"]).decode()

    upgrade_url = (
        f"http://{backend_svc}.{namespace}.svc.cluster.local:8080/{site_id}/@upgrade"
    )
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    auth = aiohttp.BasicAuth(username, password)

    async with aiohttp.ClientSession(auth=auth, headers=headers) as session:
        # --- Check whether an upgrade is needed ---
        async with session.get(
            upgrade_url, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise kopf.TemporaryError(
                    f"GET @upgrade returned HTTP {resp.status}: {text[:200]}", delay=30
                )
            data = await resp.json()

        # The REST API response shape changed between Plone versions:
        #   Plone 6.0-era:  {"needs_upgrading": bool, "upgrades": [...]}
        #   Plone 6.1-era:  {"versions": {"fs": "6111", "instance": "6026"},
        #                    "upgrade_steps": {"6026-6027": [...], ...}}
        # Support both shapes.
        versions = data.get("versions", {})
        fs_version = versions.get("fs")
        instance_version = versions.get("instance")
        if fs_version and instance_version:
            # New-style response
            needs_upgrading = fs_version != instance_version
            upgrade_steps: dict[str, Any] = data.get("upgrade_steps", {})
            step_count = sum(len(v) for v in upgrade_steps.values())
        else:
            # Old-style response
            needs_upgrading = data.get("needs_upgrading", False)
            step_count = len(data.get("upgrades", []))

        if not needs_upgrading:
            logger.info("Plone site %s/%s is already up to date", namespace, site_id)
            return False

        logger.info(
            "Plone site %s/%s needs upgrading (%d step(s)), running migration...",
            namespace,
            site_id,
            step_count,
        )

        # --- Run the upgrade ---
        async with session.post(
            upgrade_url,
            json={},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status not in (200, 204):
                text = await resp.text()
                raise kopf.TemporaryError(
                    f"POST @upgrade returned HTTP {resp.status}: {text[:200]}", delay=30
                )
            result = await resp.json()

    logger.info(
        "Plone DB upgrade complete for %s/%s: %s",
        namespace,
        site_id,
        result.get("message", "done"),
    )
    return True


# ---------------------------------------------------------------------------
# CNPG readiness: wait for the cluster service AND app secret to exist
# ---------------------------------------------------------------------------

async def _wait_for_cnpg_ready(name: str, namespace: str, timeout: int = 300) -> None:
    """Wait until CNPG has fully bootstrapped.

    Specifically waits for both:
    - The read-write Service ``<name>-db-rw`` (created early by CNPG)
    - The app credentials Secret ``<name>-db-app`` (created after bootstrap
      completes; CNPG only writes this when no ``initdb.secret`` is provided)

    The Service can appear minutes before the Secret, so checking only the
    Service is not sufficient.
    """
    svc_name = f"{name}-db-rw"
    secret_name = f"{name}-db-app"
    core_v1 = k8s_client.CoreV1Api()
    deadline = asyncio.get_event_loop().time() + timeout

    svc_ready = False
    secret_ready = False

    while asyncio.get_event_loop().time() < deadline:
        if not svc_ready:
            try:
                core_v1.read_namespaced_service(svc_name, namespace)
                svc_ready = True
                logger.info("CNPG service %s/%s is ready", namespace, svc_name)
            except ApiException as e:
                if e.status != 404:
                    raise
        if not secret_ready:
            try:
                core_v1.read_namespaced_secret(secret_name, namespace)
                secret_ready = True
                logger.info("CNPG app secret %s/%s is ready", namespace, secret_name)
            except ApiException as e:
                if e.status != 404:
                    raise
        if svc_ready and secret_ready:
            return
        logger.debug(
            "CNPG not ready yet (svc=%s, secret=%s), waiting...", svc_ready, secret_ready
        )
        await asyncio.sleep(10)

    missing = []
    if not svc_ready:
        missing.append(f"Service/{svc_name}")
    if not secret_ready:
        missing.append(f"Secret/{secret_name}")
    raise kopf.TemporaryError(
        f"CNPG resources did not appear within {timeout}s: {', '.join(missing)}", delay=30
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
        use_cnpg = db_cfg.get("cnpg", False)
        creds_secret = db_cfg.get("credentialsSecret", "plonedb-credentials")

        if not use_cnpg:
            # External PostgreSQL: all connection details come from credentialsSecret.
            # Keys expected: host, port (optional→5432), dbname (optional→plone),
            #                username, password.
            # Inject as env vars first so Kubernetes variable substitution resolves
            # $(DB_*) references inside RELSTORAGE_DSN.
            db_secret_envs += [
                _secret_env("DB_HOST",     creds_secret, "host"),
                _secret_env("DB_PORT",     creds_secret, "port"),
                _secret_env("DB_NAME",     creds_secret, "dbname"),
                _secret_env("DB_USER",     creds_secret, "username"),
                _secret_env("DB_PASSWORD", creds_secret, "password"),
            ]
            db_env["RELSTORAGE_DSN"] = (
                "host=$(DB_HOST) port=$(DB_PORT) dbname=$(DB_NAME) "
                "user=$(DB_USER) password=$(DB_PASSWORD)"
            )
        else:
            # In-cluster PostgreSQL via CloudNativePG.
            # CNPG auto-generates credentials and writes them to <name>-db-app
            # (host, port, dbname, username, password, uri, …).
            # We wait for both the Service and that Secret before continuing.
            cnpg_cluster = build_cnpg_cluster(name, namespace, spec)
            kopf.adopt(cnpg_cluster)
            manifests.append(cnpg_cluster)
            _apply_manifest(cnpg_cluster)
            await _wait_for_cnpg_ready(name, namespace)
            runtime_secret = f"{name}-db-app"
            db_secret_envs += [
                _secret_env("DB_HOST",     runtime_secret, "host"),
                _secret_env("DB_PORT",     runtime_secret, "port"),
                _secret_env("DB_NAME",     runtime_secret, "dbname"),
                _secret_env("DB_USER",     runtime_secret, "username"),
                _secret_env("DB_PASSWORD", runtime_secret, "password"),
            ]
            db_env["RELSTORAGE_DSN"] = (
                "host=$(DB_HOST) port=$(DB_PORT) dbname=$(DB_NAME) "
                "user=$(DB_USER) password=$(DB_PASSWORD)"
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
    # Ingress + Traefik VHM Middleware (when publicUrl is set and ingress.enabled)
    # Classic: rewrites all paths; Volto: rewrites only /++api++/* paths.
    # -------------------------------------------------------------------
    public_url = spec.get("publicUrl", "")
    ingress_enabled = spec.get("ingress", {}).get("enabled", False)
    if public_url and ingress_enabled:
        ingress = build_ingress(name, namespace, spec)
        kopf.adopt(ingress)
        manifests.append(ingress)
        rewrite_middleware = build_traefik_middleware(name, namespace, spec)
        kopf.adopt(rewrite_middleware)
        manifests.append(rewrite_middleware)

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
    # Run Plone DB upgrade if needed (idempotent: GET first, POST only if
    # needs_upgrading is True).  Uses the admin Secret for Basic auth.
    # -------------------------------------------------------------------
    admin_secret_name = f"{name}-admin"
    upgraded = await _run_plone_upgrade(backend_svc_name, namespace, site_id, admin_secret_name)
    if upgraded:
        patch.status["lastUpgradeTime"] = datetime.datetime.now(datetime.UTC).isoformat()

    # -------------------------------------------------------------------
    # Determine site URL for status
    # -------------------------------------------------------------------
    if public_url:
        site_url = public_url
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
    patch.status["ingressConfigured"] = bool(public_url)
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

@kopf.timer("plone.org", "v1alpha1", "plonesites", interval=86400.0)  # ty: ignore[invalid-argument-type]
async def db_pack(spec, meta, status, patch, logger, **kwargs):
    """Create a one-off Job to pack the database.

    Fires daily but respects ``database.packIntervalDays`` (default 7).
    Set ``packIntervalDays: 0`` to disable automatic packing entirely.
    Records ``status.lastPackTime`` after each job is created so the
    interval check works correctly across operator restarts.
    """
    name = _name(meta)
    namespace = _namespace(meta)
    db_cfg = spec.get("database", {})
    db_type = db_cfg.get("type", "zodb")

    # --- interval / disabled check -------------------------------------------
    pack_interval_days: int = db_cfg.get("packIntervalDays", 7)
    if pack_interval_days == 0:
        logger.debug("db_pack: packIntervalDays=0, skipping")
        return

    last_pack_str: str | None = status.get("lastPackTime")
    if last_pack_str:
        last_pack = datetime.datetime.fromisoformat(last_pack_str)
        elapsed = datetime.datetime.now(datetime.UTC) - last_pack
        if elapsed.days < pack_interval_days:
            logger.debug(
                "db_pack: %d day(s) since last pack, interval is %d — skipping",
                elapsed.days,
                pack_interval_days,
            )
            return

    # --- build job spec -------------------------------------------------------
    ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M%S")
    job_name = f"{name}-pack-{ts}"

    backend_image = spec.get("image", "plone/plone-backend:latest")

    if db_type == "zodb":
        # plone/plone-zeo ships zeopack at /app/bin/zeopack.
        # It accepts servers as positional args: host:port
        zeo_host = f"{name}-zeo.{namespace}.svc.cluster.local"
        pack_image = "plone/plone-zeo:6"
        pack_env: list[dict] = []
        pack_command = ["/app/bin/zeopack", f"{zeo_host}:8100"]
    elif db_type == "postgresql":
        # Use the backend image which has RelStorage installed.
        use_cnpg = db_cfg.get("cnpg", False)
        creds_secret = db_cfg.get("credentialsSecret", "plonedb-credentials")
        # CNPG runtime secret has the full connection info; external PG uses
        # credentialsSecret directly (same key names: host, port, dbname,
        # username, password).
        runtime_secret = f"{name}-db-app" if use_cnpg else creds_secret
        pack_image = backend_image
        # Inject individual DB_* vars first so Kubernetes variable substitution
        # resolves $(DB_*) references inside RELSTORAGE_DSN.
        pack_env = [
            _secret_env("DB_HOST",     runtime_secret, "host"),
            _secret_env("DB_PORT",     runtime_secret, "port"),
            _secret_env("DB_NAME",     runtime_secret, "dbname"),
            _secret_env("DB_USER",     runtime_secret, "username"),
            _secret_env("DB_PASSWORD", runtime_secret, "password"),
            *_make_env_list({
                "RELSTORAGE_DSN": (
                    "host=$(DB_HOST) port=$(DB_PORT) "
                    "dbname=$(DB_NAME) user=$(DB_USER) password=$(DB_PASSWORD)"
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
    patch.status["lastPackTime"] = datetime.datetime.now(datetime.UTC).isoformat()
    logger.info("Created DB pack job %s/%s", namespace, job_name)
