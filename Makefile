# Plone Operator Makefile
# Image name (without tag)
IMG_NAME ?= plone-operator
# Version derived from the nearest git tag, or the short commit SHA as a
# fallback when no tags exist yet.  Override with: make VERSION=0.2.0 ...
VERSION ?= $(shell git describe --tags --abbrev=0 2>/dev/null || git rev-parse --short HEAD)
# Full image reference used by all build/push/deploy targets.
IMG ?= $(IMG_NAME):$(VERSION)
# Namespace where PloneSite CRs and their resources live
PLONE_NS ?= plone

# Build the docker image
.PHONY: docker-build
docker-build:
	docker build -t ${IMG} .

# Push the docker image
.PHONY: docker-push
docker-push:
	docker push ${IMG}

# Deploy the operator to the K8s cluster.
# manager.yaml carries plone-operator:latest as a default placeholder; this
# target substitutes the actual versioned IMG before applying so the running
# Deployment always references the exact image that was built.
.PHONY: deploy
deploy:
	kubectl apply -f config/manager/namespace.yaml
	kubectl apply -f config/manager/plone-namespace.yaml
	kubectl apply -f config/crd/bases/
	kubectl apply -f config/rbac/
	sed 's|plone-operator:latest|$(IMG)|g' config/manager/manager.yaml | kubectl apply -f -

# Undeploy the operator from the K8s cluster
.PHONY: undeploy
undeploy:
	kubectl delete -f config/manager/ --ignore-not-found=true
	kubectl delete -f config/rbac/ --ignore-not-found=true
	kubectl delete -f config/crd/bases/ --ignore-not-found=true

# Install CRDs into the cluster
.PHONY: install
install:
	kubectl apply -f config/crd/bases/

# Uninstall CRDs from the cluster
.PHONY: uninstall
uninstall:
	kubectl delete -f config/crd/bases/ --ignore-not-found=true

# Create the admin secrets required by the sample PloneSite CRs in PLONE_NS.
# These are idempotent (--dry-run=client | apply).
.PHONY: create-secrets
create-secrets:
	kubectl create secret generic simple-plone-admin \
		-n ${PLONE_NS} \
		--from-literal=username=admin --from-literal=password=admin \
		--dry-run=client -o yaml | kubectl apply -f -
	kubectl create secret generic classic-plone-admin \
		-n ${PLONE_NS} \
		--from-literal=username=admin --from-literal=password=admin \
		--dry-run=client -o yaml | kubectl apply -f -
	kubectl create secret generic pg-plone-admin \
		-n ${PLONE_NS} \
		--from-literal=username=admin --from-literal=password=admin \
		--dry-run=client -o yaml | kubectl apply -f -

# Deploy sample PloneSite CRs (requires secrets — run create-secrets first)
.PHONY: deploy-sample
deploy-sample:
	kubectl apply -f config/samples/simple_plonesite.yaml
	kubectl apply -f config/samples/classic_minikube_test.yaml
	kubectl apply -f config/samples/pg_minikube_test.yaml

# Delete sample PloneSite CRs
.PHONY: undeploy-sample
undeploy-sample:
	kubectl delete -f config/samples/simple_plonesite.yaml --ignore-not-found=true
	kubectl delete -f config/samples/classic_minikube_test.yaml --ignore-not-found=true
	kubectl delete -f config/samples/pg_minikube_test.yaml --ignore-not-found=true

# Lint: check Python code with ruff
.PHONY: lint
lint:
	uv run ruff check plone_operator.py

# Type-check Python code with ty
.PHONY: typecheck
typecheck:
	uv run ty check plone_operator.py

# Generate bundle
.PHONY: bundle
bundle:
	@echo "Generating operator bundle..."

# Build the operator image directly inside minikube's Docker daemon so that
# the versioned tag is immediately available to the kubelet without a registry
# push or image-load step.  Patches the running Deployment to the new tag so
# the change takes effect immediately.  Use minikube-deploy for fresh clusters.
.PHONY: minikube-load
minikube-load:
	eval $$(minikube docker-env) && docker build -t $(IMG) .
	kubectl set image deployment/plone-operator-controller-manager \
		manager=$(IMG) -n plone-operator-system

# Full local minikube deployment for fresh clusters: build image inside
# minikube's Docker daemon first so the image is available before the
# Deployment is applied, then deploy all manifests with the correct tag.
.PHONY: minikube-deploy
minikube-deploy:
	eval $$(minikube docker-env) && docker build -t $(IMG) .
	$(MAKE) deploy
