# Plone Kubernetes Operator

A [Kopf](https://kopf.readthedocs.io/)-based Python operator for managing Plone 6 CMS deployments on Kubernetes. It manages the full lifecycle of a `PloneSite` custom resource — provisioning, site initialisation, ingress, and scheduled database maintenance — without Helm.

## Features

- **Two deployment types**: Volto (React frontend + REST API backend) and Classic (backend only)
- **Two database backends**: ZODB via ZEO (in-cluster StatefulSet) and PostgreSQL via RelStorage (external or [CloudNativePG](https://cloudnative-pg.io/))
- **Traefik ingress**: single Ingress with correct path routing for both deployment types
- **Virtual Host Monster**: both deployment types use a Traefik `replacePathRegex` Middleware so Plone generates correct public URLs; Classic rewrites all paths, Volto scopes the rewrite to `/++api++/*` only
- **Automatic site initialisation**: polls the REST API after deploy and creates the Plone site if it does not exist
- **Automatic database migration**: after every reconcile the operator calls `GET /@upgrade`; if pending steps are detected it calls `POST /@upgrade` to run the migration — idempotent and safe on a clean install
- **Scheduled database packing**: configurable interval (default weekly); skippable via `packIntervalDays: 0`
- **Server-side apply**: all child resources are managed via Kubernetes SSA — no drift, no ownership conflicts
- **Status tracking**: `phase`, `siteUrl`, `deploymentType`, `ingressConfigured`, `lastPackTime`, `lastUpgradeTime` surfaced on the CR

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
    ├── Traefik Middleware (<name>-rewrite) (publicUrl set)
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
# Creates plone-operator-system and plone namespaces, applies CRDs, RBAC, operator Deployment
make deploy
```

### 2. Create the admin credentials Secret

The operator always reads admin credentials from a Secret named `<cr-name>-admin`
with keys `username` and `password`. The Secret must be in the same namespace as the CR:

```bash
kubectl create secret generic my-plone-admin -n plone \
  --from-literal=username=admin \
  --from-literal=password=changeme
```

### 3. Apply a PloneSite CR

```bash
kubectl apply -f config/samples/simple_plonesite.yaml
```

### 4. Check status

```bash
kubectl get plonesites -n plone
kubectl describe plonesite my-plone -n plone
```

## Namespaces

The operator uses two namespaces:

| Namespace | Purpose |
|---|---|
| `plone-operator-system` | Operator Deployment, ServiceAccount, RBAC |
| `plone` | PloneSite CRs and all their child resources |

`make deploy` creates both namespaces. Admin secrets and the PloneSite CRs themselves must be created in the `plone` namespace (or whichever namespace you choose — the operator watches all namespaces).

## PloneSite CR Reference

### Minimal example — Volto + ZEO

```yaml
apiVersion: plone.org/v1alpha1
kind: PloneSite
metadata:
  name: my-plone
  namespace: plone
spec:
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
kubectl create secret generic my-plone-admin -n plone \
  --from-literal=username=admin --from-literal=password=changeme
```

### Production example — Volto + CNPG PostgreSQL + Ingress

```yaml
apiVersion: plone.org/v1alpha1
kind: PloneSite
metadata:
  name: production-plone
  namespace: plone
spec:
  siteId: "Plone"
  deploymentType: "volto"
  image: "plone/plone-backend:6.0"
  frontendImage: "plone/plone-frontend:18"
  replicas: 3
  publicUrl: "https://www.example.com"
  ingress:
    enabled: true
    className: "traefik"
    tls: true
  database:
    type: "postgresql"
    cnpg: true
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

Only the admin Secret is required — CNPG auto-generates the database credentials:

```bash
kubectl create secret generic production-plone-admin -n plone \
  --from-literal=username=admin --from-literal=password=changeme
```

### Classic UI example

```yaml
apiVersion: plone.org/v1alpha1
kind: PloneSite
metadata:
  name: classic-plone
  namespace: plone
spec:
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
| `siteId` | string | `"plone"` | URL path component (e.g. `/Plone`) |
| `deploymentType` | string | `"volto"` | `volto` or `classic` |
| `image` | string | `plone/plone-backend:latest` | Backend container image |
| `frontendImage` | string | `plone/plone-frontend:latest` | Volto frontend container image (only used when `deploymentType: volto`) |
| `replicas` | integer | `1` | Backend replica count |
| `publicUrl` | string | — | Public URL of the site (e.g. `https://example.com`). Enables Ingress and VHM rewriting. |
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
| `credentialsSecret` | string | — | Required for external PostgreSQL (`cnpg: false`) only — see below |
| `packIntervalDays` | integer | `7` | Days between db-pack jobs; `0` disables packing |

**`credentialsSecret` usage:**

| Scenario | Behaviour |
|---|---|
| External PostgreSQL (`cnpg: false`) | Required. Secret must contain: `host`, `port`, `dbname`, `username`, `password` |
| CNPG in-cluster (`cnpg: true`) | Not used. CNPG auto-generates credentials and writes `<name>-db-app` Secret |

### `persistence`

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | boolean | `false` | Create PVCs |
| `size` | string | `"10Gi"` | PVC capacity |
| `storageClass` | string | `""` | Storage class (empty = cluster default) |

### `resources`

Standard Kubernetes `resources` block (`limits` / `requests` with `cpu` and `memory`).

## Ingress and URL Rewriting

When `publicUrl` is set the operator creates a Traefik `replacePathRegex` Middleware
named `<name>-rewrite` that rewrites request paths to include the Zope Virtual Host
Monster (VHM) traversal prefix, so Plone generates correct absolute `@id` URLs.

### Volto (`deploymentType: volto`)

The operator:
1. Sets `RAZZLE_API_PATH=<publicUrl>` on the Volto frontend (browser-side API base URL).
2. Sets `RAZZLE_INTERNAL_API_PATH=http://<name>-backend.<ns>.svc.cluster.local:8080/<siteId>` (Node.js SSR).
3. Sets `CORS_ALLOW_ORIGIN=<publicUrl>` on the backend.
4. Creates a Traefik Middleware that rewrites only `/++api++/*` paths:
   ```
   /++api++/<rest>  →  /VirtualHostBase/<scheme>/<host>/<siteId>/VirtualHostRoot/++api++<rest>
   ```
5. Creates an Ingress with two rules, the `/++api++` rule annotated with the middleware:
   - `/++api++` → `<name>-backend:8080` (REST API, rewritten via VHM)
   - `/` → `<name>-frontend:3000` (Volto frontend, no rewrite)

### Classic (`deploymentType: classic`)

The operator:
1. Creates a Traefik Middleware that rewrites all paths:
   ```
   /<rest>  →  /VirtualHostBase/<scheme>/<host>/<siteId>/VirtualHostRoot/<rest>
   ```
2. Creates an Ingress with a single `/` → `<name>-backend:8080` rule, annotated with the middleware.

## Status Fields

| Field | Description |
|---|---|
| `phase` | `Pending`, `Running`, or `Failed` |
| `siteUrl` | Public URL of the running site |
| `deploymentType` | Reflects the active `deploymentType` |
| `ingressConfigured` | `true` if a `publicUrl` was provided |
| `lastPackTime` | ISO-8601 timestamp of the last db-pack job |
| `lastUpgradeTime` | ISO-8601 timestamp of the last automatic Plone DB upgrade |
| `conditions` | Standard Kubernetes condition array (`Ready`) |

## Automatic Database Upgrade

After every successful reconcile the operator runs a two-step upgrade check
against the live backend's REST API using the admin credentials from the
`<name>-admin` Secret:

1. **`GET /{siteId}/@upgrade`** — reads the current filesystem generation
   (`versions.fs`) and the instance generation (`versions.instance`).
2. If they differ, **`POST /{siteId}/@upgrade`** — executes all pending
   migration steps (timeout: 300 s).

The check is idempotent: on a fresh install or when the site is already
up to date it logs `"already up to date"` and returns immediately.  When
migration runs, the operator sets `status.lastUpgradeTime`.

### Upgrading Plone (e.g. 6.0 → 6.1)

Patch the backend (and optionally frontend) image:

```bash
kubectl patch plonesite <name> -n plone --type=merge \
  -p '{"spec":{"image":"plone/plone-backend:6.1","frontendImage":"plone/plone-frontend:18"}}'
```

The operator will:
1. Apply the new image to the backend Deployment (rolling update).
2. Wait for the backend to become healthy.
3. Detect pending upgrade steps via `GET /@upgrade`.
4. Run `POST /@upgrade` to migrate the ZODB or PostgreSQL schema.
5. Set `status.lastUpgradeTime` and mark the site `Running`.

Watch progress in the operator logs:

```bash
kubectl logs -n plone-operator-system \
  deployment/plone-operator-controller-manager -f
```

Expected output:

```
Plone site plone/Plone needs upgrading (18 step(s)), running migration...
Plone DB upgrade complete for plone/Plone: done
```

> **Warning**: Do not downgrade the backend image after running a migration.
> ZODB and RelStorage schemas are not backward-compatible.

### Checking upgrade status

```bash
# Was an upgrade performed, and when?
kubectl get plonesite <name> -n plone \
  -o jsonpath='{.status.lastUpgradeTime}'

# Confirm the site is up to date (fs == instance, no pending steps)
kubectl exec -n plone deployment/<name>-backend -- \
  python3 -c "
import urllib.request, base64, json
url = 'http://localhost:8080/<siteId>/@upgrade'
req = urllib.request.Request(url)
req.add_header('Accept', 'application/json')
req.add_header('Authorization', 'Basic ' + base64.b64encode(b'admin:password').decode())
print(json.dumps(json.loads(urllib.request.urlopen(req).read()).get('versions'), indent=2))
"
```

## Database Packing
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
    ├── manager/               # Operator Deployment + Namespaces
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

# Create admin secrets for all sample CRs in the plone namespace
make create-secrets

# Deploy the three minikube sample CRs
make deploy-sample
```

### Makefile targets

| Target | Description |
|---|---|
| `make deploy` | Create namespaces, apply CRDs, RBAC, and operator Deployment |
| `make undeploy` | Remove operator Deployment and RBAC |
| `make install` | Apply CRDs only |
| `make uninstall` | Remove CRDs |
| `make create-secrets` | Create sample admin secrets in `PLONE_NS` (default: `plone`) |
| `make deploy-sample` | Apply the three minikube sample PloneSite CRs |
| `make undeploy-sample` | Delete the sample CRs |
| `make minikube-load` | Build image in minikube daemon + `kubectl set image` (updates a running operator) |
| `make minikube-deploy` | Build image in minikube daemon + `make deploy` (use for fresh clusters) |
| `make lint` | `ruff check plone_operator.py` |
| `make typecheck` | `ty check plone_operator.py` |

## Troubleshooting

### Operator logs

```bash
kubectl logs -n plone-operator-system deployment/plone-operator-controller-manager -f
```

### PloneSite status

```bash
kubectl get plonesites -n plone
kubectl describe plonesite <name> -n plone
```

### Pod not starting

```bash
# Check that the admin Secret exists in the correct namespace
kubectl get secret <cr-name>-admin -n plone -o yaml

# For CNPG PostgreSQL: check the auto-generated runtime Secret
kubectl get secret <cr-name>-db-app -n plone -o yaml

# For external PostgreSQL: check credentialsSecret
kubectl get secret <credentialsSecret> -n plone -o yaml
```

### Ingress not routing correctly

```bash
kubectl get ingress <cr-name> -n plone -o yaml
# Verify the Traefik Middleware exists
kubectl get middleware <cr-name>-rewrite -n plone -o yaml
```

### Database pack job not running

```bash
# Check status.lastPackTime and packIntervalDays
kubectl get plonesite <name> -n plone -o jsonpath='{.status.lastPackTime}'

# List completed pack jobs
kubectl get jobs -n plone -l app.kubernetes.io/component=db-pack
```

## License

Apache License 2.0
