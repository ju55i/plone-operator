# Plone Operator Makefile
# Image URL to use all building/pushing image targets
IMG ?= plone-operator:latest
# Operator-SDK version
OPERATOR_SDK_VERSION ?= v1.32.0

# Build the docker image
.PHONY: docker-build
docker-build:
	docker build -t ${IMG} .

# Push the docker image
.PHONY: docker-push
docker-push:
	docker push ${IMG}

# Deploy the operator to the K8s cluster
.PHONY: deploy
deploy:
	kubectl apply -f config/manager/namespace.yaml
	kubectl apply -f config/crd/bases/
	kubectl apply -f config/rbac/
	kubectl apply -f config/manager/

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

# Deploy sample PloneSite CR
.PHONY: deploy-sample
deploy-sample:
	kubectl apply -f config/samples/

# Delete sample PloneSite CR
.PHONY: undeploy-sample
undeploy-sample:
	kubectl delete -f config/samples/ --ignore-not-found=true

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
# the new :latest tag is immediately available to the kubelet without any
# image-load step.  A rollout restart is then enough to pick up the new image.
.PHONY: minikube-load
minikube-load:
	eval $$(minikube docker-env) && docker build -t ${IMG} .
	kubectl rollout restart deployment/plone-operator-controller-manager \
		-n plone-operator-system --ignore-not-found=true 2>/dev/null || true

# Full local minikube deployment: build image inside minikube then deploy operator manifests
.PHONY: minikube-deploy
minikube-deploy: minikube-load deploy
