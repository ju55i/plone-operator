# Copilot Instructions for plone-operator

## Project Overview

This is an **Ansible-based Kubernetes Operator** for managing Plone 6 deployments. It watches for `PloneSite` Custom Resources (CRs) and reconciles them by deploying official Plone Helm charts and creating supporting Kubernetes resources (Deployments, Services, PVCs, ConfigMaps).

There is **no Python or Go** in this project — everything is YAML and Ansible.

## Key Commands

```bash
# Install Ansible collections (required before running locally)
ansible-galaxy collection install -r requirements.yml

# Run operator locally (requires kubeconfig)
ansible-operator run

# Lint Ansible roles
make lint

# Build and push operator container image
make docker-build IMG=<registry/image:tag>
make docker-push IMG=<registry/image:tag>

# Deploy/undeploy operator to cluster (applies CRDs, RBAC, manager deployment)
make deploy IMG=<registry/image:tag>
make undeploy

# Install/uninstall only the CRD
make install
make uninstall

# Apply or remove sample PloneSite CRs
make deploy-sample
make undeploy-sample
```

There is no test suite — validation is done by deploying to a cluster and checking CR status.

## Architecture

The operator uses the [Ansible Operator SDK](https://sdk.operatorframework.io/docs/building-operators/ansible/). The flow:

1. `watches.yaml` configures the operator to watch `PloneSite` (plone.org/v1alpha1) and trigger the `plonesite` Ansible role on every create/update/delete (reconcile period: 30s).
2. `roles/plonesite/tasks/main.yml` is the sole reconciliation entrypoint. It:
   - Updates CR status to "Pending"
   - Adds the Plone Helm repository
   - Builds Helm values dynamically using `set_fact` + `combine()` based on CR spec
   - Selects the correct Helm chart based on `deploymentType` × `database.type`
   - Deploys the Helm chart
   - Creates a PVC, ConfigMap, Deployment, and Service directly via `kubernetes.core.k8s`
   - Waits for readiness then updates CR status to "Running"

### Helm Chart Selection Matrix

| `deploymentType` | `database.type` | Helm Chart |
|------------------|-----------------|------------|
| `volto` (default) | `zodb` (default) | `plone/plone6-volto-zeo` |
| `volto` | `postgresql` | `plone/plone6-volto-pg` |
| `classic` | `postgresql` | `plone/plone6-classic-pg` |

> `classic` + `zodb` is not a supported combination.

## Ansible Modules Used

- `kubernetes.core.helm_repository` / `helm` / `helm_info` — Helm operations
- `kubernetes.core.k8s` / `k8s_info` — direct Kubernetes resource management
- `operator_sdk.util.k8s_status` — update PloneSite CR `.status`

## Key Conventions

**Variable naming:** All role variables use a `plone_` prefix (e.g., `plone_site_name`, `plone_db_type`). This is intentional — `.ansible-lint` is configured to skip `var-naming[no-role-prefix]` to allow this.

**Building Helm values:** Values are constructed incrementally via `set_fact` + Jinja2 `combine()` for recursive dict merging. Sections (VHM env vars, PostgreSQL config, ingress) are merged in only when the relevant spec fields are set.

**Kubernetes labels:** All operator-created resources use `app: plone` and `plone-site: <site-name>` labels. Use these to filter resources: `kubectl get pods -l plone-site=my-plone`.

**Secrets:** The operator *references* secrets by name but does not create them. Admin password and PostgreSQL credentials must be pre-created in the same namespace.

**CR Status:** Updated at each phase (Pending → Running). Status includes `siteUrl`, `vhmConfigured`, `helmRelease`, and `helmChart`. Inspect with `kubectl describe plonesite <name>`.

**CRD location:** `config/crd/bases/plone.org_plonesites.yaml`. The `PloneSite` short name is `ps`.

## Samples

Four sample CRs in `config/samples/` demonstrate common scenarios:
- `simple_plonesite.yaml` — minimal Volto + ZODB
- `plone_v1alpha1_plonesite.yaml` — full-featured with VHM, ingress, add-ons
- `plonesite_with_postgresql.yaml` — production-grade with PostgreSQL
- `plonesite_classic.yaml` — Classic UI with PostgreSQL
