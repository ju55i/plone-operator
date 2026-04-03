# Plone Operator — Quick Reference

## Deploy the operator

```bash
make deploy          # CRDs + RBAC + operator Deployment (includes plone namespace)
make install         # CRDs only
```

## Create a Plone site

All PloneSite CRs and their child resources live in the **`plone`** namespace.

```bash
# 1. Admin credentials Secret  (always named <cr-name>-admin, in plone namespace)
kubectl create secret generic my-plone-admin \
  --from-literal=username=admin \
  --from-literal=password=changeme \
  -n plone

# 2. Apply the CR
kubectl apply -f config/samples/simple_plonesite.yaml

# 3. Watch status
kubectl get plonesites -n plone -w
```

Or use the Makefile helper which handles namespace and secret creation:

```bash
make create-secrets   # creates <name>-admin secret in plone namespace
make deploy-sample    # applies default sample CRs
```

## Minimal CR examples

### Volto + ZEO (development)

```yaml
apiVersion: plone.org/v1alpha1
kind: PloneSite
metadata:
  name: my-plone
  namespace: plone
spec:
  deploymentType: "volto"
  database:
    type: "zodb"
  persistence:
    enabled: true
    size: "10Gi"
```

### Volto + CNPG PostgreSQL + Ingress (production)

CNPG automatically generates the database credentials secret (`<cr-name>-db-app`).
No `credentialsSecret` is needed for in-cluster CNPG deployments.

```yaml
apiVersion: plone.org/v1alpha1
kind: PloneSite
metadata:
  name: my-plone
  namespace: plone
spec:
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
```

### Classic UI + ZEO

```yaml
apiVersion: plone.org/v1alpha1
kind: PloneSite
metadata:
  name: my-plone
  namespace: plone
spec:
  deploymentType: "classic"
  publicUrl: "https://classic.example.com"
  ingress:
    enabled: true
    className: "traefik"
    tls: true
  database:
    type: "zodb"
```

## Secrets reference

```bash
# Admin credentials  (name must be <cr-name>-admin, namespace: plone)
kubectl create secret generic my-plone-admin \
  --from-literal=username=admin \
  --from-literal=password=changeme \
  -n plone

# External PostgreSQL credentials  (used when cnpg: false)
kubectl create secret generic my-db-secret \
  --from-literal=host=postgres.example.com \
  --from-literal=port=5432 \
  --from-literal=dbname=plone \
  --from-literal=username=plone \
  --from-literal=password=dbpassword \
  -n plone
```

For in-cluster CNPG (`cnpg: true`), no database secret is required — CNPG creates
`<cr-name>-db-app` automatically after cluster bootstrap completes.

## Accessing the site

```bash
# Volto frontend
kubectl port-forward service/my-plone-frontend 3000:3000 -n plone
# → http://localhost:3000

# Backend / Classic UI
kubectl port-forward service/my-plone-backend 8080:8080 -n plone
# → http://localhost:8080/Plone
```

## Troubleshooting

```bash
# Operator logs
kubectl logs -n plone-operator-system \
  deployment/plone-operator-controller-manager -f

# CR status and events
kubectl describe plonesite <name> -n plone

# Child resources
kubectl get deploy,sts,svc,ingress,jobs \
  -l app.kubernetes.io/part-of=<name> -n plone

# Check Secrets exist
kubectl get secret <name>-admin -n plone
kubectl get secret <name>-db-app -n plone   # CNPG only — created after bootstrap

# Last db-pack run
kubectl get plonesite <name> -n plone \
  -o jsonpath='{.status.lastPackTime}'
kubectl get jobs -l app.kubernetes.io/component=db-pack -n plone
```

## Cleanup

```bash
# Delete a site (child resources GC'd via ownerReferences; PVCs are retained)
kubectl delete plonesite <name> -n plone

# Delete PVCs manually if no longer needed (data loss!)
kubectl delete pvc -l app.kubernetes.io/part-of=<name> -n plone

# Remove operator
make undeploy
make uninstall
```

## Minikube workflow

```bash
make minikube-load     # rebuild image inside minikube + rollout restart
make minikube-deploy   # rebuild + deploy everything from scratch
```
