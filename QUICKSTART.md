# Plone Operator Quick Reference

## Quick Start

```bash
# 1. Build and deploy operator
make docker-build IMG=plone-operator:latest
make install
make deploy

# 2. Create admin secret
kubectl create secret generic plone-admin-password \
  --from-literal=password='admin123'

# 3. Deploy Plone
kubectl apply -f config/samples/simple_plonesite.yaml

# 4. Check status
kubectl get plonesites
kubectl describe plonesite simple-plone

# 5. Access Plone (Volto frontend)
kubectl port-forward service/simple-plone-frontend 3000:3000
# Open http://localhost:3000
```

## Common Commands

```bash
# List all Plone sites
kubectl get plonesites -A

# Get detailed information
kubectl describe plonesite <name> -n <namespace>

# Check Helm releases
helm list -n <namespace>

# View Helm values
helm get values <plonesite-name> -n <namespace>

# Check logs
kubectl logs -n plone-operator-system -l control-plane=controller-manager -f

# Delete a Plone site
kubectl delete plonesite <name> -n <namespace>
```

## Deployment Type Matrix

| Database | Deployment | Helm Chart | Frontend Port | Backend Port |
|----------|------------|------------|---------------|--------------|
| ZODB | Volto | plone6-volto-zeo | 3000 | 8080 |
| PostgreSQL | Volto | plone6-volto-pg | 3000 | 8080 |
| PostgreSQL | Classic | plone6-classic-pg | - | 8080 |

## Minimal Examples

### Volto with ZODB (Development)
```yaml
apiVersion: plone.org/v1alpha1
kind: PloneSite
metadata:
  name: dev-plone
spec:
  deploymentType: "volto"
  database:
    type: "zodb"
```

### Volto with PostgreSQL (Production)
```yaml
apiVersion: plone.org/v1alpha1
kind: PloneSite
metadata:
  name: prod-plone
spec:
  deploymentType: "volto"
  vhmUrl: "https://www.example.com"
  ingress:
    enabled: true
    className: "nginx"
    tls: true
  database:
    type: "postgresql"
  replicas: 3
```

### Classic UI with PostgreSQL
```yaml
apiVersion: plone.org/v1alpha1
kind: PloneSite
metadata:
  name: classic-plone
spec:
  deploymentType: "classic"
  vhmUrl: "https://classic.example.com"
  database:
    type: "postgresql"
```

## Secrets Reference

### Admin Password Secret
```bash
kubectl create secret generic plone-admin-password \
  --from-literal=password='your-password'
```

### PostgreSQL Secret (for integrated PostgreSQL)
```bash
kubectl create secret generic plonedb \
  --from-literal=database-name='plone' \
  --from-literal=database-user='plone' \
  --from-literal=database-password='db-password'
```

## Port Forwarding

### Volto Frontend
```bash
kubectl port-forward service/<plonesite-name>-frontend 3000:3000
```

### Backend API
```bash
kubectl port-forward service/<plonesite-name>-backend 8080:8080
```

### Classic UI
```bash
kubectl port-forward service/<plonesite-name>-backend 8080:8080
```

## Ingress Examples

### Nginx Ingress
```yaml
spec:
  vhmUrl: "https://www.example.com"
  ingress:
    enabled: true
    className: "nginx"
    tls: true
```

### Traefik Ingress
```yaml
spec:
  vhmUrl: "https://www.example.com"
  ingress:
    enabled: true
    className: "traefik"
    tls: true
```

## Troubleshooting Quick Checks

```bash
# 1. Check operator is running
kubectl get pods -n plone-operator-system

# 2. Check PloneSite status
kubectl get plonesites -A

# 3. Check Helm release
helm list -A

# 4. Check pods
kubectl get pods -n <namespace>

# 5. Check services
kubectl get svc -n <namespace>

# 6. Check ingress
kubectl get ingress -n <namespace>

# 7. Check PVCs
kubectl get pvc -n <namespace>

# 8. Check secrets
kubectl get secrets -n <namespace>

# 9. Describe PloneSite for events
kubectl describe plonesite <name> -n <namespace>

# 10. Check operator logs
kubectl logs -n plone-operator-system \
  deployment/plone-operator-controller-manager --tail=100
```

## Resource Cleanup

```bash
# Delete a Plone site (keeps PVCs)
kubectl delete plonesite <name> -n <namespace>

# Delete Helm release (if needed)
helm uninstall <plonesite-name> -n <namespace>

# Delete PVCs (data will be lost!)
kubectl delete pvc -n <namespace> -l app.kubernetes.io/instance=<plonesite-name>

# Uninstall operator
make undeploy
make uninstall
```
