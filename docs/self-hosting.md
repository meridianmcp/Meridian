# Self-Hosting

Run Meridian on your own infrastructure. Choose the path that matches how much
control you want versus how much setup you are willing to do.

-> [Getting Started](quickstart.md)

---

## Choose a setup path

=== "Binary Install"

    The fastest path for local or small-team usage. No Python required.

    **Windows**

    1. Download [`meridian.exe`](https://github.com/meridianmcp/Meridian/releases/latest/download/meridian.exe)
    2. Double-click to run
    3. Meridian opens your browser at `http://localhost:7878`

    **macOS (Apple Silicon)**

    ```bash
    curl -Lo meridian https://github.com/meridianmcp/Meridian/releases/latest/download/meridian-mac-arm64
    chmod +x meridian
    xattr -d com.apple.quarantine meridian 2>/dev/null || true
    ./meridian
    ```

    **Linux**

    ```bash
    curl -Lo meridian https://github.com/meridianmcp/Meridian/releases/latest/download/meridian-linux-x86_64
    chmod +x meridian
    ./meridian
    ```

=== "Docker"

    Use the published Docker Hub image when you want a clean containerized setup.

    ```bash
    docker pull meridianmcp/meridian:latest
    docker run --rm -p 7878:7878 --env-file .env \
      -v "$(pwd)/data:/app/data" \
      meridianmcp/meridian:latest
    ```

    If you prefer source-based Compose:

    ```bash
    git clone https://github.com/meridianmcp/Meridian
    cd Meridian
    docker compose up -d
    ```

    By default, SQLite data is persisted in `./data/meridian.db`.

=== "pixi"

    Best for contributors and active development.

    ```bash
    git clone https://github.com/meridianmcp/Meridian
    cd Meridian
    pixi install
    pixi run start
    ```

    For hot reload:

    ```bash
    pixi run dev
    ```

=== "pip"

    Use this when `pixi` is not available.

    ```bash
    git clone https://github.com/meridianmcp/Meridian
    cd Meridian
    python -m venv .venv
    source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
    pip install -e ".[full]"
    python -m meridian
    ```

Confirm the server is healthy:

```bash
curl http://localhost:7878/health
# -> {"status": "ok", "version": "...", "db": "sqlite"}
```

The dashboard is at **http://localhost:7878**.

---

## Postgres setup with Neon

Neon is a good default when you want a hosted Postgres backend for a self-hosted
Meridian server.

1. Create a project at [neon.tech](https://neon.tech)
2. Copy the Postgres connection string
3. Set `MERIDIAN_DB_URL`
4. Start Meridian normally

```bash
export MERIDIAN_DB_URL="postgresql://neondb_owner:...@ep-xxx.us-east-2.aws.neon.tech/neondb?sslmode=require"
pixi run start
```

Meridian auto-detects Postgres from the URL prefix and switches to `psycopg3`
(the connection pool, autocommit, and adapter translation live in
`meridian/pg_adapter.py`; SQLite remains the default for local installs).

---

## Environment variables

See the full [Configuration Reference](configuration.md).

Key variables for self-hosting:

| Variable | What to set |
|----------|-------------|
| `SESSION_SECRET` | Long random string used to sign session cookies |
| `APP_URL` | Public base URL such as `https://meridian.yourdomain.com` |
| `MERIDIAN_DB_URL` | Optional Postgres URL; SQLite is fine for smaller installs |
| `SITE_PASSWORD` | Optional password gate for preview deployments |

Example `.env`:

```bash
SESSION_SECRET=replace-me
# APP_URL=https://meridian.example.com
# MERIDIAN_DB_URL=postgresql://user:pass@host/dbname
# SITE_PASSWORD=preview-password
```

---

## Expose localhost for browser clients

Claude remote connectors and ChatGPT custom apps need a **public HTTPS** MCP
endpoint. If you are running Meridian on `localhost`, a quick test path is a
Cloudflare Tunnel:

```bash
cloudflared tunnel --url http://localhost:7878
```

Cloudflare prints a temporary public URL such as `https://random-name.trycloudflare.com`.
Use that host plus `/mcp` in Claude or ChatGPT:

```text
https://random-name.trycloudflare.com/mcp
```

!!! note
    This is great for demos and short-lived testing. For a stable setup, put
    Meridian behind your own domain and reverse proxy, then set `APP_URL`
    accordingly.

---

## Reverse proxy setup

### nginx

```nginx
server {
    listen 80;
    server_name meridian.yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name meridian.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/meridian.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/meridian.yourdomain.com/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:7878;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;
    }
}
```

!!! important "WebSocket support"
    Meridian uses WebSockets for the live dashboard. Keep the `Upgrade` and
    `Connection` headers in place.

### Caddy

```caddy
meridian.yourdomain.com {
    reverse_proxy localhost:7878 {
        header_up Host {host}
        header_up X-Forwarded-For {remote}
        header_up X-Forwarded-Proto {scheme}
    }
}
```

Caddy handles TLS automatically.

---

## Upgrading

```bash
cd Meridian
git pull origin main
pixi install        # or: docker pull meridianmcp/meridian:latest
pixi run start      # or: docker compose up -d
```

Meridian runs database migrations automatically on startup.

---

## Backup and restore

### SQLite

```bash
# Backup
cp data/meridian.db "data/meridian-$(date +%Y%m%d).db"

# Restore
cp data/meridian-20260101.db data/meridian.db
```

Automated daily backup example:

```bash
0 2 * * * cp /path/to/Meridian/data/meridian.db \
  /backups/meridian-$(date +%Y%m%d).db && \
  find /backups -name "meridian-*.db" -mtime +7 -delete
```

### Postgres

```bash
pg_dump "$MERIDIAN_DB_URL" -Fc -f meridian-backup-$(date +%Y%m%d).dump
pg_restore -d "$MERIDIAN_DB_URL" meridian-backup-20260101.dump
```

---

## Running multiple instances

!!! warning "SQLite does not support multiple writers"
    If you need multiple Meridian instances for HA or load balancing, move to
    Postgres first.

```yaml
services:
  meridian-1:
    image: meridianmcp/meridian:latest
    ports: ["7878:7878"]
    environment:
      - MERIDIAN_DB_URL=postgresql://...
  meridian-2:
    image: meridianmcp/meridian:latest
    ports: ["7879:7878"]
    environment:
      - MERIDIAN_DB_URL=postgresql://...
```

Put nginx or Caddy in front once you scale past a single instance.
