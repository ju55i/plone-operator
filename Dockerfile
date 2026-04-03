FROM python:3.12-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy lockfile and project metadata first for layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies (no project itself, just deps)
RUN uv sync --frozen --no-dev --no-install-project

# Copy operator source
COPY plone_operator.py .

# Run as non-root
RUN useradd -u 1000 -m plone-operator
USER 1000

ENTRYPOINT ["uv", "run", "--frozen", "--no-dev", "kopf", "run", "--all-namespaces", "--liveness=http://0.0.0.0:8080/healthz", "plone_operator.py"]
