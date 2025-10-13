# Plone Kubernetes Operator

An Ansible-based Kubernetes operator for managing Plone sites using the official [Plone Helm Charts](https://github.com/plone/helm-charts). This operator provides a declarative way to deploy and manage Plone 6 installations with support for Virtual Host Monster (VHM) configuration, multiple deployment types, and various database backends.

## Features

- **Automated Plone Deployment**: Deploy Plone sites using official Helm charts with a simple Custom Resource
- **Multiple Deployment Types**: 
  - **Volto**: Modern Plone 6 frontend with React-based Volto UI
  - **Classic**: Traditional Plone Classic UI
- **Virtual Host Monster Support**: Configure VHM URLs for proper URL generation behind proxies
- **Database Options**: 
  - **ZODB with ZEO**: Default option with integrated ZEO server
  - **PostgreSQL**: RelStorage with PostgreSQL (integrated or external)
- **Persistent Storage**: Automatic PVC creation for both Plone data and databases
- **Scalability**: Configure replica count for horizontal scaling
- **Ingress Support**: Built-in ingress configuration with TLS support
- **Add-on Management**: Specify Plone add-ons to install
- **Resource Management**: Configure CPU and memory limits/requests
- **Status Tracking**: Monitor deployment status through the CR status field

## Architecture

This operator leverages the official Plone Helm charts:
- `plone/plone6-volto-zeo`: Plone 6 with Volto frontend and ZEO backend
- `plone/plone6-volto-pg`: Plone 6 with Volto frontend and PostgreSQL
- `plone/plone6-classic-pg`: Plone 6 Classic UI with PostgreSQL

The operator translates PloneSite Custom Resources into Helm chart deployments, providing a simpler, more declarative interface while leveraging the community-maintained Helm charts.

### Benefits of Using Helm Charts

1. **Community Maintained**: The Helm charts are officially maintained by the Plone community
2. **Battle Tested**: Charts have been tested across various scenarios and environments
3. **Best Practices**: Charts implement Kubernetes and Plone best practices
4. **Regular Updates**: Charts are updated with new Plone releases and improvements
5. **Flexibility**: Underlying Helm charts can be updated independently of the operator
6. **Documentation**: Extensive documentation available in the [helm-charts repository](https://github.com/plone/helm-charts)

### How It Works

1. User creates a `PloneSite` custom resource
2. Operator watches for `PloneSite` resources
3. Operator adds the Plone Helm repository
4. Operator translates CR spec to Helm values
5. Operator deploys/updates using appropriate Helm chart
6. Operator updates CR status with deployment information

## Prerequisites

- Kubernetes cluster (v1.19+)
- kubectl configured to access your cluster
- Helm 3.x installed in the cluster
- Docker (for building the operator image)

## Installation

### 1. Build the Operator Image

```bash
make docker-build IMG=your-registry/plone-operator:latest
make docker-push IMG=your-registry/plone-operator:latest
```

### 2. Deploy the Operator

```bash
# Create namespace and install CRDs
kubectl create namespace plone-operator-system

# Install CRDs
make install

# Deploy the operator
make deploy
```

## Usage

### Creating a Plone Site

#### Simple Example with Volto and ZEO (Default)

```yaml
apiVersion: plone.org/v1alpha1
kind: PloneSite
metadata:
  name: my-plone
  namespace: default
spec:
  siteName: "My Plone Site"
  siteId: "Plone"
  deploymentType: "volto"  # Modern Plone with Volto frontend
  adminUser: "admin"
  adminPasswordSecret: "plone-admin-password"
  replicas: 1
  database:
    type: "zodb"  # Uses ZEO server (deployed automatically)
  persistence:
    enabled: true
    size: "10Gi"
```

#### Production Example with VHM, PostgreSQL, and Ingress

```yaml
apiVersion: plone.org/v1alpha1
kind: PloneSite
metadata:
  name: production-plone
  namespace: production
spec:
  siteName: "Production Plone"
  siteId: "Plone"
  
  # Use Volto frontend
  deploymentType: "volto"
  
  # Admin credentials
  adminUser: "admin"
  adminPasswordSecret: "plone-admin-password"
  
  # Plone configuration
  image: "plone/plone-backend:6.0"
  replicas: 3
  
  # Virtual Host Monster configuration
  # Critical for proper URL generation when behind ingress/proxy
  vhmUrl: "https://www.example.com"
  vhmPath: "Plone"
  
  # Ingress configuration
  ingress:
    enabled: true
    className: "nginx"  # or "traefik"
    tls: true
  
  # Resources
  resources:
    limits:
      cpu: "4000m"
      memory: "4Gi"
    requests:
      cpu: "1000m"
      memory: "1Gi"
  
  # Database (PostgreSQL with RelStorage)
  database:
    type: "postgresql"
  
  # Persistence
  persistence:
    enabled: true
    storageClass: "fast-ssd"
    size: "50Gi"
  
  # Add-ons
  addons:
    - "plone.restapi"
    - "plone.volto"
  
  # Environment variables
  environment:
    TZ: "America/New_York"
    CORS_ALLOW_ORIGIN: "https://www.example.com"
```

### Creating Required Secrets

Before deploying a PloneSite, create the admin password secret:

```bash
kubectl create secret generic plone-admin-password \
  --from-literal=password='your-secure-password' \
  --namespace=default
```

For PostgreSQL deployments, if using the integrated PostgreSQL from Helm chart:

```bash
kubectl create secret generic plonedb \
  --from-literal=database-name='plone' \
  --from-literal=database-user='plone' \
  --from-literal=database-password='plone-password' \
  --namespace=default
```

### Applying the Configuration

```bash
kubectl apply -f config/samples/plone_v1alpha1_plonesite.yaml
```

### Checking Status

```bash
# View PloneSite resources
kubectl get plonesites

# Detailed status
kubectl describe plonesite my-plone

# Check the underlying Helm release
helm list -n default

# Check operator logs
kubectl logs -n plone-operator-system -l control-plane=controller-manager -f
```

## Deployment Types

### Volto (Default)

Modern Plone 6 deployment with:
- Volto React-based frontend (port 3000)
- Plone REST API backend (port 8080)
- Suitable for headless CMS scenarios

```yaml
spec:
  deploymentType: "volto"
```

### Classic

Traditional Plone deployment with:
- Classic UI served directly from backend
- Single service on port 8080
- Suitable for traditional Plone sites

```yaml
spec:
  deploymentType: "classic"
```

## Virtual Host Monster (VHM) Configuration

The VHM configuration is essential when Plone is accessed through an Ingress or LoadBalancer with a public domain name. Without proper VHM configuration, Plone will generate internal URLs instead of the public ones.

### VHM Parameters

- **vhmUrl**: The public URL where your Plone site is accessible (e.g., `https://www.example.com`)
- **vhmPath**: The path component for your Plone site (defaults to `siteId`)

### Example with Ingress

The operator automatically configures ingress when `vhmUrl` is set:

```yaml
apiVersion: plone.org/v1alpha1
kind: PloneSite
metadata:
  name: my-plone
spec:
  siteName: "My Site"
  siteId: "Plone"
  deploymentType: "volto"
  
  # VHM Configuration
  vhmUrl: "https://myplone.example.com"
  vhmPath: "Plone"
  
  # Ingress will be automatically configured
  ingress:
    enabled: true
    className: "nginx"  # Use your ingress controller
    tls: true
```

This will:
1. Configure Plone backend to generate URLs with `https://myplone.example.com`
2. Create an Ingress resource pointing to your domain
3. Route traffic appropriately to frontend (Volto) and backend (API)

### Without VHM (Internal Access Only)

If you don't need external access, simply omit `vhmUrl`:

```yaml
spec:
  siteName: "Internal Plone"
  siteId: "Plone"
  # No vhmUrl specified
```

Access the site via port-forward:
```bash
# For Volto deployment
kubectl port-forward -n default service/my-plone-frontend 3000:3000

# For Classic deployment
kubectl port-forward -n default service/my-plone-backend 8080:8080
```

## Database Configuration

The operator supports multiple database backends through the underlying Helm charts.

### ZODB with ZEO (Default)

Uses the ZEO server for ZODB storage. The Helm chart deploys a ZEO server automatically:

```yaml
spec:
  database:
    type: "zodb"
  persistence:
    enabled: true
    size: "10Gi"
```

### PostgreSQL (Integrated)

Deploys a PostgreSQL StatefulSet alongside Plone using RelStorage:

```yaml
spec:
  database:
    type: "postgresql"
  persistence:
    enabled: true
    size: "20Gi"  # For both Plone and PostgreSQL
```

**Note**: Create the database credentials secret before deployment:

```bash
kubectl create secret generic plonedb \
  --from-literal=database-name='plone' \
  --from-literal=database-user='plone' \
  --from-literal=database-password='secure-password'
```

### PostgreSQL (External)

To use an external PostgreSQL database:

```yaml
spec:
  database:
    type: "postgresql"
    host: "postgres.example.com"
    port: 5432
    name: "plone"
    user: "plone"
    passwordSecret: "external-db-credentials"
```

## Resource Specifications

All fields in the PloneSite CRD:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `siteName` | string | "Plone" | Display name of the Plone site |
| `siteId` | string | "plone" | URL path component for the site |
| `deploymentType` | string | "volto" | Deployment type (volto or classic) |
| `adminUser` | string | "admin" | Admin username |
| `adminPasswordSecret` | string | "plone-admin-password" | Secret containing admin password |
| `image` | string | "plone/plone-backend:6.0" | Plone backend container image |
| `replicas` | integer | 1 | Number of backend replicas |
| `vhmUrl` | string | "" | Virtual Host Monster URL |
| `vhmPath` | string | siteId | VHM path component |
| `ingress.enabled` | boolean | false | Enable ingress (auto-enabled if vhmUrl is set) |
| `ingress.className` | string | "" | Ingress class (nginx, traefik, etc.) |
| `ingress.tls` | boolean | false | Enable TLS for ingress |
| `resources` | object | See CRD | CPU/memory limits and requests |
| `database.type` | string | "zodb" | Database type (zodb or postgresql) |
| `database.host` | string | "" | External database host (optional) |
| `database.port` | integer | 5432 | External database port |
| `persistence.enabled` | boolean | true | Enable persistent storage |
| `persistence.size` | string | "10Gi" | PVC size |
| `persistence.storageClass` | string | "" | Storage class name |
| `addons` | array | [] | List of Plone add-ons |
| `environment` | object | {} | Additional environment variables |

## Helm Charts Used

The operator uses the official Plone Helm charts:

- **plone6-volto-zeo**: For `deploymentType: volto` with `database.type: zodb`
- **plone6-volto-pg**: For `deploymentType: volto` with `database.type: postgresql`
- **plone6-classic-pg**: For `deploymentType: classic` with `database.type: postgresql`

For more information about these charts, see the [Plone Helm Charts repository](https://github.com/plone/helm-charts).

## Development

### Project Structure

```
plone-operator/
├── Dockerfile                      # Operator container image
├── Makefile                        # Build and deployment targets
├── watches.yaml                    # Ansible operator watches configuration
├── requirements.yml                # Ansible collection dependencies
├── config/
│   ├── crd/bases/                 # CustomResourceDefinition
│   │   └── plone.org_plonesites.yaml
│   ├── rbac/                      # RBAC configuration
│   │   ├── service_account.yaml
│   │   ├── role.yaml
│   │   └── role_binding.yaml
│   ├── manager/                   # Operator deployment
│   │   ├── namespace.yaml
│   │   └── manager.yaml
│   └── samples/                   # Example PloneSite CRs
│       ├── plone_v1alpha1_plonesite.yaml
│       └── simple_plonesite.yaml
└── roles/
    └── plonesite/                 # Ansible role for PloneSite
        ├── defaults/
        │   └── main.yml
        └── tasks/
            └── main.yml
```

### Running Locally

```bash
# Install Ansible collections
ansible-galaxy collection install -r requirements.yml

# Install CRDs
make install

# Run operator locally (without building image)
ansible-operator run
```

### Uninstalling

```bash
# Remove sample CRs
make undeploy-sample

# Remove operator
make undeploy

# Remove CRDs
make uninstall
```

## Troubleshooting

### Check Operator Logs

```bash
kubectl logs -n plone-operator-system deployment/plone-operator-controller-manager -f
```

### Check PloneSite Status

```bash
kubectl describe plonesite <name> -n <namespace>
```

### Check Helm Release

```bash
# List Helm releases
helm list -n <namespace>

# Get Helm release details
helm status <plonesite-name> -n <namespace>

# Get Helm release values
helm get values <plonesite-name> -n <namespace>
```

### Check Deployed Resources

```bash
# For Volto deployment
kubectl get pods,svc,ingress -n <namespace> -l app.kubernetes.io/instance=<plonesite-name>

# Check ZEO server (for ZODB)
kubectl get statefulset -n <namespace>

# Check PostgreSQL (if using integrated PostgreSQL)
kubectl get statefulset -n <namespace> -l app.kubernetes.io/component=postgresql
```

### Common Issues

1. **Pod not starting**: 
   - Check if secrets exist: `kubectl get secret -n <namespace>`
   - Verify the secret contains correct keys (`password` for admin, `database-*` for PostgreSQL)

2. **VHM not working**: 
   - Verify `vhmUrl` and `vhmPath` are correctly set
   - Check ingress is created: `kubectl get ingress -n <namespace>`
   - Ensure ingress controller is installed and working

3. **Helm deployment fails**:
   - Check Helm repository is accessible: `helm repo list`
   - Update Helm repos: `helm repo update`
   - Check operator logs for Helm errors

4. **Persistence issues**: 
   - Check if StorageClass exists: `kubectl get storageclass`
   - Verify PVC is bound: `kubectl get pvc -n <namespace>`
   - Check storage capacity

5. **Database connection issues**:
   - For PostgreSQL: Verify secret has correct keys (database-name, database-user, database-password)
   - Check PostgreSQL pod is running: `kubectl get pods -n <namespace> | grep postgres`

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the Apache License 2.0.

## API Reference

### PloneSite v1alpha1

The PloneSite custom resource represents a Plone site deployment in Kubernetes.

**API Group**: `plone.org`  
**API Version**: `v1alpha1`  
**Kind**: `PloneSite`

For the complete API specification, see the CRD definition in `config/crd/bases/plone.org_plonesites.yaml`.
