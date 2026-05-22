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

# Install dependencies via uv
RUN uv pip install --system fastapi uvicorn[standard] aiosqlite asyncpg python-dotenv toml

# Copy the rest of the app
COPY . .

EXPOSE 8000

# Use MERIDIAN_DB_URL env var — Neon postgres in hosted mode, SQLite fallback
CMD ["uvicorn", "meridian.server:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
