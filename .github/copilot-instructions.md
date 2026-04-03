# Copilot Instructions for plone-operator

## Project Overview

A **[Kopf](https://kopf.readthedocs.io/)-based Python operator** for managing Plone 6 CMS deployments on Kubernetes. It watches `PloneSite` custom resources (CRs) and reconciles them by creating and updating child Kubernetes resources directly via server-side apply — no Helm, no Ansible.

There is **no test suite** — validation is done by deploying to a cluster and checking CR status and pod logs.

## Key Commands

```bash
# Run operator locally (out-of-cluster, uses ~/.kube/config)
uv run kopf run plone_operator.py --verbose

# Lint
make lint        # ruff check plone_operator.py

# Type-check
make typecheck   # ty check plone_operator.py

# Install CRDs into current cluster context
make install

# Deploy operator (CRDs + RBAC + Deployment)
make deploy

# Undeploy operator
make undeploy

# Apply / remove sample CRs
make deploy-sample
make undeploy-sample

# Minikube: build image inside minikube's Docker daemon + rollout restart
make minikube-load

# Minikube: build + deploy everything from scratch
make minikube-deploy
```

## Architecture

Single-file operator: `plone_operator.py`.

```
PloneSite CR
    │
    ▼
plone_operator.py  (Kopf, asyncio)
    │
    ├── ZEO StatefulSet + Service          (database.type: zodb)
    ├── CNPG Cluster CR                    (database.type: postgresql, cnpg: true)
    │
    ├── Backend Deployment + Service       (always)
    ├── Frontend Deployment + Service      (deploymentType: volto)
    │
    ├── Traefik Middleware                 (deploymentType: classic + vhmUrl)
    ├── Ingress                            (ingress.enabled: true)
    │
    └── db-pack Job (one-off, weekly)      (kopf.timer → batch/v1 Job)
```

All child resources carry `ownerReferences` set via `kopf.adopt()` — Kubernetes garbage-collects them automatically on CR deletion.

## Kopf Handlers

| Handler | Trigger | What it does |
|---|---|---|
| `configure` | startup | Loads kubeconfig (in-cluster or local) |
| `reconcile` | create / update | Applies all child resources via SSA; polls REST API to init site |
| `on_delete` | delete | No-op (ownerReferences handle GC) |
| `db_pack` | daily timer | Creates a one-off pack Job if `packIntervalDays` days have elapsed |

## Key Conventions

**Resource naming:** All child resources are named `<cr-name>-<component>` (e.g. `my-plone-backend`, `my-plone-zeo`, `my-plone-ingress`).

**Labels:** All resources get `app.kubernetes.io/managed-by: plone-operator` and `app.kubernetes.io/part-of: <cr-name>`.

**Server-side apply:** All manifests go through `_apply_manifest()` which calls the Kubernetes dynamic client with `fieldManager="plone-operator"` and `force=True`.

**Admin credentials:** Always read from a Secret named `<cr-name>-admin` (keys: `username`, `password`). The name is derived from the CR — there is no field for it.

**Database credentials:**
- External PostgreSQL (`cnpg: false`): `database.credentialsSecret` (keys: `host`, `port`, `dbname`, `username`, `password`)
- CNPG in-cluster (`cnpg: true`): `database.credentialsSecret` (keys: `username`, `password`) for bootstrap; runtime uses the CNPG-auto-created `<cr-name>-db-app` Secret

**Environment variable injection:** DB connection details are injected as individual `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` env vars via `secretKeyRef`, then `RELSTORAGE_DSN` is built using Kubernetes `$(VAR_NAME)` substitution.

**VHM — Volto:** No Traefik Middleware. A single Ingress routes `/++api++` → backend and `/` → frontend. Traefik forwards `X-Forwarded-Host`/`X-Forwarded-Proto` so the backend generates correct public URLs.

**VHM — Classic:** A Traefik `Middleware` (type: `replacepathregex`) rewrites every incoming path to the VHM traversal URL. The Ingress is annotated to apply this middleware.

**Ingress class:** Traefik (`ingressClassName: traefik`). No support for nginx.

**`imagePullPolicy`:** Always `IfNotPresent` on all containers.

**`db_pack` timer:** Fires daily (`interval=86400.0`). Skips if `packIntervalDays == 0` or if fewer days than `packIntervalDays` have elapsed since `status.lastPackTime`. Writes `status.lastPackTime` after creating the Job.

**Status fields:** `phase`, `siteUrl`, `deploymentType`, `vhmConfigured`, `lastPackTime`, `conditions`.

## CRD

`config/crd/bases/plone.org_plonesites.yaml`

Key spec fields:

| Field | Default | Notes |
|---|---|---|
| `deploymentType` | `volto` | `volto` or `classic` |
| `siteId` | `plone` | Zope site path |
| `image` | `plone/plone-backend:latest` | Backend image |
| `replicas` | `1` | Backend replicas |
| `vhmUrl` | — | Public URL; enables VHM + Ingress |
| `vhmPath` | `siteId` | VHM Zope path override |
| `database.type` | `zodb` | `zodb` or `postgresql` |
| `database.cnpg` | `false` | Use CloudNativePG |
| `database.credentialsSecret` | — | Secret name for PG credentials |
| `database.packIntervalDays` | `7` | `0` = disabled |
| `ingress.enabled` | `false` | Create Traefik Ingress |
| `ingress.tls` | `false` | TLS on Ingress |
| `persistence.enabled` | `false` | Create PVCs |
| `persistence.size` | `10Gi` | PVC capacity |

## Project Structure

```
plone-operator/
├── plone_operator.py          # Kopf operator (single file)
├── pyproject.toml             # uv project / dependencies
├── uv.lock
├── Dockerfile
├── Makefile
└── config/
    ├── crd/bases/             # PloneSite CRD
    ├── rbac/                  # ServiceAccount, Role, RoleBinding
    ├── manager/               # Operator Deployment + Namespace
    └── samples/               # Example PloneSite CRs (no namespace field)
```

## Samples

| File | Scenario |
|---|---|
| `simple_plonesite.yaml` | Volto + ZEO, minikube nip.io URL |
| `plone_v1alpha1_plonesite.yaml` | Volto + ZEO, full-featured with VHM + Ingress |
| `plonesite_with_postgresql.yaml` | Volto + CNPG PostgreSQL, production |
| `plonesite_classic.yaml` | Classic UI + external PostgreSQL |
| `classic_minikube_test.yaml` | Classic UI + ZEO, minikube nip.io URL |

Sample CRs omit `metadata.namespace` — `kubectl apply` uses the current context namespace or `-n <ns>`.
