# Plone Kubernetes Operator

A [Kopf](https://kopf.readthedocs.io/)-based Python operator for managing Plone 6 CMS deployments on Kubernetes. It manages the full lifecycle of a `PloneSite` custom resource — provisioning, site initialisation, ingress, and scheduled database maintenance — without Helm.

## Features

- **Two deployment types**: Volto (React frontend + REST API backend) and Classic (backend only)
- **Two database backends**: ZODB via ZEO (in-cluster StatefulSet) and PostgreSQL via RelStorage (external or [CloudNativePG](https://cloudnative-pg.io/))
- **Traefik ingress**: single Ingress with correct path routing for both deployment types
- **Virtual Host Monster**: Classic UI uses a Traefik Middleware to rewrite paths; Volto relies on `X-Forwarded-Host` headers
- **Automatic site initialisation**: polls the REST API after deploy and creates the Plone site if it does not exist
- **Scheduled database packing**: configurable interval (default weekly); skippable via `packIntervalDays: 0`
- **Server-side apply**: all child resources are managed via Kubernetes SSA — no drift, no ownership conflicts
- **Status tracking**: `phase`, `siteUrl`, `deploymentType`, `ingressConfigured`, `lastPackTime` surfaced on the CR

## Architecture

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
    ├── Traefik Middleware                 (deploymentType: classic + publicUrl)
    ├── Ingress                            (ingress.enabled: true)
    │
    └── db-pack Job (one-off, weekly)      (kopf.timer, batch/v1 Job)
```

All child resources carry `ownerReferences` pointing to the `PloneSite` — Kubernetes garbage-collects them automatically when the CR is deleted.

## Prerequisites

- Kubernetes 1.25+
- [Traefik](https://traefik.io/) ingress controller
- [CloudNativePG](https://cloudnative-pg.io/) operator (only if `database.cnpg: true`)
- `kubectl` configured against your cluster
- [uv](https://docs.astral.sh/uv/) (for local development)

## Quick Start

### 1. Install CRDs and deploy the operator

```bash
# Apply CRDs, RBAC, and the operator Deployment
make deploy
```

### 2. Create the admin credentials Secret

The operator always reads admin credentials from a Secret named `<cr-name>-admin`
with keys `username` and `password`:

```bash
kubectl create secret generic my-plone-admin \
  --from-literal=username=admin \
  --from-literal=password=changeme
```

### 3. Apply a PloneSite CR

```bash
kubectl apply -f config/samples/simple_plonesite.yaml
```

### 4. Check status

```bash
kubectl get plonesites
kubectl describe plonesite my-plone
```

## PloneSite CR Reference

### Minimal example — Volto + ZEO

```yaml
apiVersion: plone.org/v1alpha1
kind: PloneSite
metadata:
  name: my-plone
  namespace: default
spec:
  siteName: "My Plone Site"
  siteId: "Plone"
  deploymentType: "volto"
  database:
    type: "zodb"
  persistence:
    enabled: true
    size: "10Gi"
```

Create the required Secret first:

```bash
kubectl create secret generic my-plone-admin \
  --from-literal=username=admin --from-literal=password=changeme
```

### Production example — Volto + CNPG PostgreSQL + Ingress

```yaml
apiVersion: plone.org/v1alpha1
kind: PloneSite
metadata:
  name: production-plone
  namespace: default
spec:
  siteName: "Production Plone"
  siteId: "Plone"
  deploymentType: "volto"
  image: "plone/plone-backend:6.0"
  replicas: 3
  publicUrl: "https://www.example.com"
  ingress:
    enabled: true
    className: "traefik"
    tls: true
  database:
    type: "postgresql"
    cnpg: true
    credentialsSecret: "production-plone-db"   # keys: username, password
    packIntervalDays: 7
  persistence:
    enabled: true
    storageClass: "fast-ssd"
    size: "50Gi"
  resources:
    limits:
      cpu: "4000m"
      memory: "4Gi"
    requests:
      cpu: "1000m"
      memory: "1Gi"
  environment:
    TZ: "America/New_York"
```

Create the required Secrets first:

```bash
# Admin credentials
kubectl create secret generic production-plone-admin \
  --from-literal=username=admin --from-literal=password=changeme

# CNPG bootstrap credentials (CNPG creates the runtime <name>-db-app Secret)
kubectl create secret generic production-plone-db \
  --from-literal=username=plone --from-literal=password=dbpassword
```

### Classic UI example

```yaml
apiVersion: plone.org/v1alpha1
kind: PloneSite
metadata:
  name: classic-plone
  namespace: default
spec:
  siteName: "Classic Plone"
  siteId: "Plone"
  deploymentType: "classic"
  publicUrl: "https://classic.example.com"
  ingress:
    enabled: true
    className: "traefik"
    tls: true
  database:
    type: "zodb"
  persistence:
    enabled: true
    size: "20Gi"
```

## Field Reference

### Top-level

| Field | Type | Default | Description |
|---|---|---|---|
| `siteName` | string | `"Plone"` | Display name of the Plone site |
| `siteId` | string | `"plone"` | URL path component (e.g. `/Plone`) |
| `deploymentType` | string | `"volto"` | `volto` or `classic` |
| `image` | string | `plone/plone-backend:latest` | Backend container image |
| `replicas` | integer | `1` | Backend replica count |
| `publicUrl` | string | — | Public URL of the site (e.g. `https://example.com`). Enables Ingress and sets CORS / Volto API path. |
| `sitePath` | string | `siteId` | Zope traversal path to the site object; used for Classic UI path rewriting. |
| `addons` | array | `[]` | Plone add-ons to activate |
| `environment` | object | `{}` | Extra environment variables injected into the backend |

Admin credentials are always read from a Secret named `<cr-name>-admin`
(keys: `username`, `password`). There is no field for this — the name is derived
from the CR name to avoid collisions.

### `ingress`

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | boolean | `false` | Create a Traefik Ingress |
| `className` | string | `"traefik"` | Ingress class |
| `tls` | boolean | `false` | Enable TLS on the Ingress |

### `database`

| Field | Type | Default | Description |
|---|---|---|---|
| `type` | string | `"zodb"` | `zodb` or `postgresql` |
| `cnpg` | boolean | `false` | Use CloudNativePG for in-cluster PostgreSQL |
| `credentialsSecret` | string | — | Secret name for PostgreSQL credentials (see below) |
| `packIntervalDays` | integer | `7` | Days between db-pack jobs; `0` disables packing |

**`credentialsSecret` key requirements:**

| Scenario | Required keys |
|---|---|
| External PostgreSQL (`cnpg: false`) | `host`, `port`, `dbname`, `username`, `password` |
| CNPG in-cluster (`cnpg: true`) | `username`, `password` (bootstrap only; runtime uses `<name>-db-app`) |

### `persistence`

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | boolean | `false` | Create PVCs |
| `size` | string | `"10Gi"` | PVC capacity |
| `storageClass` | string | `""` | Storage class (empty = cluster default) |

### `resources`

Standard Kubernetes `resources` block (`limits` / `requests` with `cpu` and `memory`).

## Ingress and URL Rewriting

### Volto (`deploymentType: volto`)

When `publicUrl` is set the operator:
1. Sets `RAZZLE_API_PATH=<publicUrl>` on the Volto frontend (browser-side API base URL).
2. Sets `RAZZLE_INTERNAL_API_PATH=http://<name>-backend:8080/<siteId>` (Node.js SSR).
3. Sets `CORS_ALLOW_ORIGIN=<publicUrl>` on the backend.
4. Creates a single Ingress with two `Prefix` paths:
   - `/++api++` → `<name>-backend:8080` (REST API)
   - `/` → `<name>-frontend:3000`

No path rewriting middleware is used for Volto — Traefik forwards `X-Forwarded-Host` and `X-Forwarded-Proto`, which the Plone backend uses to generate correct `@id` URLs.

### Classic (`deploymentType: classic`)

When `publicUrl` is set the operator:
1. Creates a Traefik `Middleware` that rewrites every incoming path to the Zope traversal URL:
   ```
   /VirtualHostBase/<scheme>/<host>/Plone/VirtualHostRoot/<original-path>
   ```
2. Creates an Ingress with a single `/` → `<name>-backend:8080` rule, annotated with the middleware.

## Status Fields

| Field | Description |
|---|---|
| `phase` | `Pending`, `Running`, or `Failed` |
| `siteUrl` | Internal cluster URL of the Plone site |
| `deploymentType` | Reflects the active `deploymentType` |
| `ingressConfigured` | `true` if a `publicUrl` was provided |
| `lastPackTime` | ISO-8601 timestamp of the last db-pack job |
| `conditions` | Standard Kubernetes condition array (`Ready`) |

## Database Packing

A Kopf timer fires daily and creates a one-off Kubernetes `Job` if
`packIntervalDays` days have elapsed since `status.lastPackTime`.

- **ZEO**: runs `zeopack` from `plone/plone-zeo:6`
- **PostgreSQL**: runs a RelStorage `pack(days=0)` call via the backend image

Set `packIntervalDays: 0` to disable packing entirely.
Jobs are auto-deleted 24 hours after completion (`ttlSecondsAfterFinished: 86400`).

## Development

### Project structure

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
    └── samples/               # Example PloneSite CRs
```

### Running locally (out-of-cluster)

```bash
# Install dependencies
uv sync

# Install CRDs into your current cluster context
make install

# Run operator locally (uses ~/.kube/config)
uv run kopf run plone_operator.py --verbose
```

### Linting and type checking

```bash
make lint       # ruff
make typecheck  # ty
```

### Minikube workflow

```bash
# Build image inside minikube's Docker daemon and restart the operator
make minikube-load

# Or: build + deploy everything from scratch
make minikube-deploy
```

### Makefile targets

| Target | Description |
|---|---|
| `make deploy` | Apply CRDs, RBAC, and operator Deployment |
| `make undeploy` | Remove operator Deployment and RBAC |
| `make install` | Apply CRDs only |
| `make uninstall` | Remove CRDs |
| `make deploy-sample` | Apply all files under `config/samples/` |
| `make undeploy-sample` | Delete sample CRs |
| `make minikube-load` | Build image in minikube daemon + rollout restart |
| `make minikube-deploy` | `minikube-load` + `deploy` |
| `make lint` | `ruff check plone_operator.py` |
| `make typecheck` | `ty check plone_operator.py` |

## Troubleshooting

### Operator logs

```bash
kubectl logs -n plone-operator-system deployment/plone-operator-controller-manager -f
```

### PloneSite status

```bash
kubectl get plonesites
kubectl describe plonesite <name>
```

### Pod not starting

```bash
# Check that the admin Secret exists with the correct name and keys
kubectl get secret <cr-name>-admin -o yaml

# For PostgreSQL: check credentialsSecret and (for CNPG) <cr-name>-db-app
kubectl get secret <credentialsSecret> -o yaml
kubectl get secret <cr-name>-db-app -o yaml
```

### Ingress not routing correctly

```bash
kubectl get ingress <cr-name>-ingress -o yaml
# For Classic UI: verify the Traefik Middleware exists
kubectl get middleware <cr-name>-rewrite -o yaml
```

### Database pack job not running

```bash
# Check status.lastPackTime and packIntervalDays
kubectl get plonesite <name> -o jsonpath='{.status.lastPackTime}'

# List completed pack jobs
kubectl get jobs -l app.kubernetes.io/component=db-pack
```

## License

Apache License 2.0
