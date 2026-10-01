FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 ca-certificates build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency install
RUN pip install --no-cache-dir uv

# Copy dependency files first (layer cache)
COPY pyproject.toml ./
COPY pixi.toml ./

# Install dependencies via uv from pyproject.toml (PEP 517)
RUN uv pip install --system .

# Copy the rest of the app
COPY . .

# 746f5244 -- explicitly install the detachable meridian-codeindex extension
# (extensions/meridian-codeindex, its own standalone pyproject.toml) so
# `import meridian_codeindex` resolves via normal site-packages in this image,
# not only via meridian/code_index.py's vendored-sys.path fallback (d5e60791).
# That fallback is a correct safety net for a stale/pre-existing process whose
# env predates this wiring, but a freshly built image should install the
# package properly rather than relying on it as the only path. meridian-outputs
# is intentionally NOT installed here -- it runs as its own separate `uvx`
# subprocess (see extensions/meridian-outputs), never imported into the main
# server process.
RUN uv pip install --system ./extensions/meridian-codeindex

# c11c5117 -- install the detachable meridian-docparse package
# (packages/docparse, pure stdlib, its own standalone pyproject.toml).
# meridian/docs_intel.py and meridian/latex_intel.py are thin shims over it
# (`from docparse import ...`), so without this the hosted image fails every
# get_latex_structure / docx-structure call with "No module named 'docparse'".
# Locally it only resolves via the editable path install in pixi.toml
# ([pypi-dependencies] meridian-docparse); the `uv pip install --system .` above
# installs pyproject.toml's dependency list, which does not (and, being a
# separate detachable package, should not) pull it in. It must run AFTER
# `COPY . .` because ./packages/docparse only exists in the image from that
# point, and packages/ must stay out of .dockerignore's exclusions.
RUN uv pip install --system ./packages/docparse

# Build-time smoke check: fail the image build (not a prod request, later) if
# docparse is not importable from the system interpreter uvicorn runs under.
# WORKDIR is /app, which has no top-level docparse/ dir (the source lives at
# /app/packages/docparse/docparse), so this only passes when the install above
# really put the package into site-packages.
RUN python -c "import docparse; from docparse import docs_intel, latex_intel"

EXPOSE 8000

# Cache-bust query param for static assets. git is not installed in this
# image, so the SHA is baked in at build time by the deploy pipeline.
ARG MERIDIAN_GIT_SHA=""
ENV MERIDIAN_GIT_SHA=$MERIDIAN_GIT_SHA

ENV MERIDIAN_DB=/app/data/meridian.db

# Startup: enforce Postgres when running on Fly
CMD ["sh", "-c", "\
  if [ -n \"$FLY_APP_NAME\" ] && [ -z \"$MERIDIAN_DB_URL\" ]; then \
    echo 'ERROR: Hosted mode requires MERIDIAN_DB_URL (Postgres). For local use: docker compose up OR pixi run start'; \
    exit 1; \
  fi; \
  exec uvicorn meridian.server:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=* \
"]
