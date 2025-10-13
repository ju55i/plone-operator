FROM quay.io/operator-framework/ansible-operator:latest

# Copy CRDs
COPY config/crd/bases /opt/ansible/config/crd/bases

# Copy roles
COPY roles/ ${HOME}/roles/

# Copy watches configuration
COPY watches.yaml ${HOME}/watches.yaml

# Copy requirements
COPY requirements.yml ${HOME}/requirements.yml

# Install Ansible collections
RUN ansible-galaxy collection install -r ${HOME}/requirements.yml

USER ${USER_UID}
