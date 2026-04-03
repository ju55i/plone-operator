# Plone Operator — Quick Reference

## Deploy the operator

```bash
make deploy          # CRDs + RBAC + operator Deployment
make install         # CRDs only
```

## Create a Plone site

```bash
# 1. Admin credentials Secret  (always named <cr-name>-admin)
kubectl create secret generic my-plone-admin \
  --from-literal=username=admin \
  --from-literal=password=changeme

# 2. Apply the CR
kubectl apply -f config/samples/simple_plonesite.yaml

# 3. Watch status
kubectl get plonesites -w
```

## Minimal CR examples

### Volto + ZEO (development)

```yaml
apiVersion: plone.org/v1alpha1
kind: PloneSite
metadata:
  name: my-plone
spec:
  deploymentType: "volto"
  database:
    type: "zodb"
  persistence:
    enabled: true
    size: "10Gi"
```

### Volto + CNPG PostgreSQL + Ingress (production)

```yaml
apiVersion: plone.org/v1alpha1
kind: PloneSite
metadata:
  name: my-plone
spec:
  deploymentType: "volto"
  image: "plone/plone-backend:6.0"
  replicas: 3
  vhmUrl: "https://www.example.com"
  ingress:
    enabled: true
    className: "traefik"
    tls: true
  database:
    type: "postgresql"
    cnpg: true
    credentialsSecret: "my-plone-db"   # keys: username, password
```

### Classic UI + ZEO

```yaml
apiVersion: plone.org/v1alpha1
kind: PloneSite
metadata:
  name: my-plone
spec:
  deploymentType: "classic"
  vhmUrl: "https://classic.example.com"
  ingress:
    enabled: true
    className: "traefik"
    tls: true
  database:
    type: "zodb"
```

## Secrets reference

```bash
# Admin credentials  (name must be <cr-name>-admin)
kubectl create secret generic my-plone-admin \
  --from-literal=username=admin \
  --from-literal=password=changeme

# External PostgreSQL credentials  (keys must match exactly)
kubectl create secret generic my-db-secret \
  --from-literal=host=postgres.example.com \
  --from-literal=port=5432 \
  --from-literal=dbname=plone \
  --from-literal=username=plone \
  --from-literal=password=dbpassword

# CNPG bootstrap credentials  (CNPG creates <cr-name>-db-app at runtime)
kubectl create secret generic my-plone-db \
  --from-literal=username=plone \
  --from-literal=password=dbpassword
```

## Accessing the site

```bash
# Volto frontend
kubectl port-forward service/my-plone-frontend 3000:3000
# → http://localhost:3000

# Backend / Classic UI
kubectl port-forward service/my-plone-backend 8080:8080
# → http://localhost:8080/Plone
```

## Troubleshooting

```bash
# Operator logs
kubectl logs -n plone-operator-system \
  deployment/plone-operator-controller-manager -f

# CR status and events
kubectl describe plonesite <name>

# Child resources
kubectl get deploy,sts,svc,ingress,jobs \
  -l app.kubernetes.io/part-of=<name>

# Check Secrets exist
kubectl get secret <name>-admin
kubectl get secret <name>-db-app   # CNPG only

# Last db-pack run
kubectl get plonesite <name> -o jsonpath='{.status.lastPackTime}'
kubectl get jobs -l app.kubernetes.io/component=db-pack
```

## Cleanup

```bash
# Delete a site (child resources GC'd via ownerReferences; PVCs are retained)
kubectl delete plonesite <name>

# Delete PVCs manually if no longer needed (data loss!)
kubectl delete pvc -l app.kubernetes.io/part-of=<name>

# Remove operator
make undeploy
make uninstall
```

## Minikube workflow

```bash
make minikube-load     # rebuild image inside minikube + rollout restart
make minikube-deploy   # rebuild + deploy everything from scratch
```
