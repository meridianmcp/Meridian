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

EXPOSE 8000

ENV MERIDIAN_DB=/app/data/meridian.db

# Startup: enforce Postgres when running on Fly
CMD ["sh", "-c", "\
  if [ -n \"$FLY_APP_NAME\" ] && [ -z \"$MERIDIAN_DB_URL\" ]; then \
    echo 'ERROR: Hosted mode requires MERIDIAN_DB_URL (Postgres). For local use: docker compose up OR pixi run start'; \
    exit 1; \
  fi; \
  exec uvicorn meridian.server:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=* \
"]
