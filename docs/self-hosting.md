# Self-Hosting

Run Meridian on your own infrastructure. Choose the setup that fits your team.

---

## Option 1: Docker Compose (Recommended)

The simplest production-ready setup. Persists data to a local volume.

### Prerequisites
- Docker and Docker Compose v2

### Setup

```bash
git clone https://github.com/ajc3xc/Meridian
cd Meridian
```

Start the server:
```bash
docker compose up -d
```

Verify it's running:
```bash
curl http://localhost:7878/health
# → {"status": "ok", "version": "1.9.0", "db": "sqlite"}
```

The dashboard is at **http://localhost:7878**.

### Data persistence

By default, the SQLite database is stored at `./data/meridian.db` via a volume mount.

```yaml
# docker-compose.yml (relevant section)
volumes:
  - ./data:/app/data
```

### Environment variables

Create `.env` in the project root:

```bash
# Optional: use Postgres instead of SQLite
# MERIDIAN_DB_URL=postgresql://user:pass@host/dbname

# Set a secure session secret
SESSION_SECRET=your-long-random-secret-here

# Optional: password gate for preview deployments
# SITE_PASSWORD=preview-password
```

Restart after changing `.env`:
```bash
docker compose down && docker compose up -d
```

---

## Option 2: pixi run start (Development)

Best for local development and testing.

```bash
git clone https://github.com/ajc3xc/Meridian
cd Meridian
pixi install
pixi run start
```

The server starts on `http://127.0.0.1:7878`.

For hot-reload during development:
```bash
pixi run dev
# Uses uvicorn --reload, watches meridian/ for changes
```

---

## Option 3: Manual pip Install

For environments where pixi isn't available.

```bash
git clone https://github.com/ajc3xc/Meridian
cd Meridian
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[full]"
python -m meridian
```

---

## Postgres Setup with Neon (Free)

Neon has a generous free tier — sufficient for personal use and small teams.

1. Create a free account at [neon.tech](https://neon.tech)
2. Create a new project
3. Copy the connection string from the Neon dashboard
4. Set the env var:

```bash
export MERIDIAN_DB_URL="postgresql://neondb_owner:...@ep-xxx.us-east-2.aws.neon.tech/neondb?sslmode=require"
pixi run start
```

Meridian auto-detects Postgres from the URL prefix and uses `asyncpg` instead of `aiosqlite`.

---

## Environment Variables

See the full [Configuration Reference](configuration.md).

Key variables for self-hosting:

| Variable | What to set |
|----------|-------------|
| `SESSION_SECRET` | Long random string — used to sign session cookies |
| `APP_URL` | Your public URL (e.g. `https://meridian.yourdomain.com`) |
| `MERIDIAN_DB_URL` | Postgres URL (optional — SQLite is fine for small teams) |
| `SITE_PASSWORD` | One-time password gate for private previews |

---

## Reverse Proxy Setup

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
        proxy_read_timeout 3600s;   # needed for WebSocket connections
    }
}
```

!!! important "WebSocket support"
    Meridian uses WebSockets for the live dashboard. The `Upgrade` and `Connection`
    headers are required — don't remove them.

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

Caddy handles SSL automatically via Let's Encrypt.

---

## Upgrading

```bash
cd Meridian
git pull origin main
pixi install        # or: docker compose build
pixi run start      # or: docker compose up -d
```

Meridian runs all database migrations automatically on startup — no manual SQL needed.

---

## Backup and Restore

### SQLite

```bash
# Backup
cp data/meridian.db "data/meridian-$(date +%Y%m%d).db"

# Restore
cp data/meridian-20260101.db data/meridian.db
```

For automated backups:
```bash
# Add to crontab — daily backup, keep last 7
0 2 * * * cp /path/to/Meridian/data/meridian.db \
  /backups/meridian-$(date +%Y%m%d).db && \
  find /backups -name "meridian-*.db" -mtime +7 -delete
```

### Postgres (Neon)

Neon handles backups automatically with point-in-time recovery. Manual backup:

```bash
pg_dump "$MERIDIAN_DB_URL" -Fc -f meridian-backup-$(date +%Y%m%d).dump
# Restore:
pg_restore -d "$MERIDIAN_DB_URL" meridian-backup-20260101.dump
```

---

## Running Multiple Instances

!!! warning "SQLite doesn't support multiple writers"
    If you need multiple Meridian instances (for load balancing or HA),
    use Postgres. SQLite will work with multiple readers but not concurrent writers.

### With Postgres + multiple instances

```yaml
# docker-compose.yml with 2 instances
services:
  meridian-1:
    build: .
    ports: ["7878:7878"]
    environment:
      - MERIDIAN_DB_URL=postgresql://...
  meridian-2:
    build: .
    ports: ["7879:7878"]
    environment:
      - MERIDIAN_DB_URL=postgresql://...
```

Use nginx upstream to load balance between them.
