"""FastAPI HTTP server and MCP stdio server for Meridian.

This module exposes two surfaces backed by the same async SQLite database:

* A FastAPI app (``app``) reachable on port 7878 by default. Used by the
  demo script and any HTTP client.
* An MCP server (built in :func:`build_mcp_server`) reachable via stdio.
  Wired up in :mod:`meridian.__main__` and consumed by Claude Desktop /
  Claude Code / Cursor / Windsurf.
"""

from __future__ import annotations

import hashlib
import json
import asyncio
import os
import re
import signal
from contextlib import asynccontextmanager
from pathlib import Path

# ---------------------------------------------------------------------------
# Version — read from pixi.toml so it never goes stale
# ---------------------------------------------------------------------------
def _read_version() -> str:
    # 1. Explicit env var (set in Dockerfile or fly secrets)
    v = os.environ.get("MERIDIAN_VERSION", "")
    if v:
        return v
    # 2. pixi.toml at repo root (local dev)
    try:
        import tomllib
        _root = Path(__file__).parent.parent
        with open(_root / "pixi.toml", "rb") as _f:
            data = tomllib.load(_f)
            return data.get("workspace", {}).get("version", "") or data.get("version", "dev")
    except Exception:
        return "1.0.0-beta"

_VERSION = _read_version()


def _read_git_sha() -> str:
    """Short git SHA for cache-busting static assets — falls back to version on errors."""
    env_sha = os.environ.get("MERIDIAN_GIT_SHA", "")
    if env_sha:
        return env_sha[:12]
    try:
        import subprocess
        out = subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=Path(__file__).parent.parent,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
        return out or _VERSION
    except Exception:
        return _VERSION


_GIT_SHA = _read_git_sha()
_ASSET_VERSION = f"{_VERSION}-{_GIT_SHA}" if _GIT_SHA != _VERSION else _VERSION


def _load_meridian_md() -> str:
    """v2.3 — load the MERIDIAN.md session-instructions file.

    Resolution order:
    1. ``MERIDIAN.md`` at the repo root (project-specific override)
    2. ``meridian/MERIDIAN.md`` bundled with the package (built-in default)
    3. Empty string if neither exists.

    Returns the file contents as a string. Cheap to call repeatedly — the
    file is tiny and the OS page cache makes re-reads nearly free.
    """
    pkg_dir = Path(__file__).parent
    repo_root = pkg_dir.parent
    for candidate in (repo_root / "MERIDIAN.md", pkg_dir / "MERIDIAN.md"):
        try:
            if candidate.exists():
                return candidate.read_text(encoding="utf-8")
        except OSError:
            continue
    return ""


from typing import Any

import aiosqlite
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import dashboard as dashboard_module
from . import db as db_module
from . import goal_md as goal_md_module
from . import enqueue as enqueue_module
from . import handoff as handoff_module
from . import toml_config as toml_config_module
from .models import (
    ChatHistoryItem,
    ChatRequest,
    ClaimTaskRequest,
    ClaimTaskResponse,
    EnqueueTask,
    FileContent,
    GoalModeSet,
    GoalSet,
    GoalState,
    HandoffResult,
    Project,
    ProjectCreate,
    Session,
    SessionRegister,
    SetNorthStarRequest,
    SetSprintRequest,
    StartSessionRequest,
    Task,
    TaskCreate,
    TaskUpdate,
)

# Default on-disk location. Overridable via MERIDIAN_DB env var, which the
# test suite uses to redirect to ``:memory:``.
DEFAULT_DB_PATH = os.environ.get(
    "MERIDIAN_DB", str(Path("data") / "meridian.db")
)
DEFAULT_DATA_DIR = Path(
    os.environ.get("MERIDIAN_DATA_DIR", "data")
)

_DEMO_COOKIE = "meridian_demo_access"
_DEMO_COOKIE_MAX_AGE = 24 * 3600


def _check_demo_cookie(request: Request) -> bool:
    """Return True when the demo access cookie is present and valid."""
    from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

    secret = (
        os.environ.get("SESSION_SECRET")
        or os.environ.get("MERIDIAN_SESSION_SECRET")
        or "demo-fallback-secret"
    )
    token = request.cookies.get(_DEMO_COOKIE, "")
    if not token:
        return False
    try:
        URLSafeTimedSerializer(secret).loads(token, max_age=_DEMO_COOKIE_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired, Exception):  # noqa: BLE001
        return False


def _demo_gate_html(error: str = "") -> str:
    """Return the password gate page HTML."""
    err_html = f'<div class="err">{error}</div>' if error else '<div class="err"></div>'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Meridian — Preview Access</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d0d0d;color:#e2e8f0;font-family:'IBM Plex Mono',monospace,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center}}
.card{{background:#161616;border:1px solid #2d2d2d;border-radius:8px;padding:40px;max-width:380px;width:100%;margin:20px}}
.logo{{font-size:22px;font-weight:600;color:#a78bfa;margin-bottom:6px}}
.sub{{color:#6b7280;font-size:12px;margin-bottom:28px}}
.err{{color:#f87171;font-size:12px;margin-bottom:12px;min-height:18px}}
label{{display:block;color:#9ca3af;font-size:11px;margin-bottom:6px}}
input{{width:100%;background:#0d0d0d;border:1px solid #2d2d2d;color:#e2e8f0;padding:8px 12px;border-radius:4px;font-family:inherit;font-size:13px;margin-bottom:16px;outline:none}}
input:focus{{border-color:#7c3aed}}
button{{width:100%;background:#7c3aed;color:#fff;border:none;padding:10px;border-radius:4px;cursor:pointer;font-size:13px;font-family:inherit}}
button:hover{{background:#6d28d9}}
.req{{text-align:center;margin-top:16px;font-size:11px;color:#6b7280}}
.req a{{color:#a78bfa;text-decoration:none}}
.req a:hover{{text-decoration:underline}}
</style>
</head>
<body>
<div class="card">
  <div class="logo">&#x1f9ed; Meridian</div>
  <div class="sub">Preview access required</div>
  {err_html}
  <form method="POST" action="/demo-auth">
    <label>Access password</label>
    <input type="password" name="password" autofocus placeholder="Enter password">
    <button type="submit">Enter preview &#x2192;</button>
  </form>
  <div class="req">Request access: <a href="mailto:hello@usemeridian.us">hello@usemeridian.us</a></div>
</div>
</body>
</html>"""


async def _seed_demo_data(db) -> None:
    """Seed realistic generic demo data into the demo DB.

    Always wipes and re-seeds so the demo is fresh on every restart.
    Projects: 'backend-api-v2' (backend refactor) and 'data-pipeline' (ETL).
    No Meridian-specific or customer-specific content — generic software demos.
    """
    # Always wipe and re-seed for a clean demo experience
    try:
        existing = await db_module.list_projects(db)
        for proj in existing:
            try:
                await db_module.delete_project(db, proj["id"])
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass

    from datetime import datetime, timezone

    now = datetime.now(tz=timezone.utc)

    # ---- Project 1: backend-api-v2 ----
    api = await db_module.create_project(db, "backend-api-v2")

    await db_module.set_north_star(
        db, api["id"],
        "Achieve 99.9% uptime and <200ms p95 latency for all API endpoints. "
        "Every service is observable, every failure is recoverable.",
    )
    await db_module.set_goal(
        db, api["id"],
        "BACKEND API v2 REFACTOR\n\n"
        "SHIPPED (12/12):\n"
        "Auth module decoupled from monolith. Rate limiting (100 req/min per key).\n"
        "N+1 query fixes across 8 endpoints. Redis caching layer (p95: 340ms→85ms).\n"
        "Integration test suite (94% coverage). OpenAPI docs auto-generated.\n"
        "DB index optimization — 3 slow queries eliminated. Security audit passed.\n\n"
        "CURRENT FOCUS:\n"
        "1. Load testing under 10k concurrent users\n"
        "2. Canary deploy to 5% traffic\n"
        "3. Deprecate v1 endpoints (grace period: 30 days)\n\n"
        "KEY FILES:\n"
        "src/api/v2/ — new versioned endpoints\n"
        "src/middleware/ — rate limiting, auth, logging\n"
        "tests/integration/ — full suite\n"
        "docs/openapi.yaml — auto-generated spec",
        minor=False,
    )
    await db_module.set_sprint(
        db, api["id"],
        "v2.3 week of May 26 — load test + canary deploy + v1 deprecation notice",
    )

    # 8 sessions for backend-api-v2
    s_auth   = await db_module.register_session(db, api["id"], "refactor-auth-module", human_id="sarah")
    s_rate   = await db_module.register_session(db, api["id"], "add-rate-limiting", human_id="marcus")
    s_n1     = await db_module.register_session(db, api["id"], "fix-n+1-queries", human_id="sarah")
    s_cache  = await db_module.register_session(db, api["id"], "implement-caching", human_id="marcus")
    s_tests  = await db_module.register_session(db, api["id"], "write-integration-tests", human_id="priya")
    s_docs   = await db_module.register_session(db, api["id"], "update-openapi-docs", human_id="priya")
    s_idx    = await db_module.register_session(db, api["id"], "db-index-optimization", human_id="marcus")
    s_sec    = await db_module.register_session(db, api["id"], "security-audit", human_id="sarah")

    api_tasks = [
        (s_auth,  "Extracted auth module from UserService — 847 lines → 3 focused classes", "done"),
        (s_auth,  "JWT validation moved to middleware — no more duplicate checks in handlers", "done"),
        (s_auth,  "OAuth2 refresh token rotation implemented — old tokens invalidated on use", "done"),
        (s_rate,  "SlowAPI rate limiter wired — 100 req/min per API key, 10 req/min anonymous", "done"),
        (s_rate,  "Rate limit headers added to all responses (X-RateLimit-Remaining etc.)", "done"),
        (s_n1,    "Identified 14 N+1 patterns via django-debug-toolbar query log analysis", "done"),
        (s_n1,    "Fixed 8 critical N+1s with select_related/prefetch_related — queries: 340→28", "done"),
        (s_n1,    "6 remaining N+1s in reporting endpoints — documented, deferred to v2.4", "backlog"),
        (s_cache, "Redis caching layer added — user profiles, permission sets, feature flags", "done"),
        (s_cache, "Cache invalidation strategy: event-driven via Celery signals, 15min TTL max", "done"),
        (s_cache, "p95 latency improved: 340ms → 85ms on cached endpoints", "done"),
        (s_tests, "Integration test suite: 247 tests, 94% line coverage, all green in CI", "done"),
        (s_tests, "Load test scenario written — 10k concurrent users via Locust", "pending"),
        (s_docs,  "OpenAPI 3.1 spec auto-generated from FastAPI route decorators", "done"),
        (s_docs,  "Postman collection exported and added to developer portal", "done"),
        (s_idx,   "EXPLAIN ANALYZE on top 20 slow queries — found 3 missing composite indexes", "done"),
        (s_idx,   "Added indexes: (user_id, created_at), (tenant_id, status), (email, verified)", "done"),
        (s_sec,   "OWASP top-10 audit completed — 0 critical, 2 medium findings fixed", "done"),
        (s_sec,   "SQL injection via ORM confirmed not applicable — parameterized everywhere", "done"),
        (s_sec,   "Dependency audit — 4 CVEs found, 3 patched, 1 tracked (low severity)", "done"),
        (s_auth,  "Canary deploy config — 5% traffic split via feature flag in LaunchDarkly", "pending"),
        (s_rate,  "v1 deprecation notice — add Sunset header + deprecation email to API key holders", "pending"),
    ]

    # Sprint items for backend-api-v2
    sprint_items_api = [
        ("Set up v2 project structure and routing", "v2.0", "done"),
        ("Auth module refactor", "v2.1", "done"),
        ("Rate limiting middleware", "v2.1", "done"),
        ("N+1 query fixes (8 endpoints)", "v2.2", "done"),
        ("Redis caching layer", "v2.2", "done"),
        ("Integration test suite (94% coverage)", "v2.2", "done"),
        ("OpenAPI docs auto-generation", "v2.2", "done"),
        ("DB index optimization", "v2.2", "done"),
        ("Security audit", "v2.2", "done"),
        ("Load test 10k concurrent users", "v2.3", "pending"),
        ("Canary deploy to 5% traffic", "v2.3", "pending"),
        ("v1 endpoint deprecation notices", "v2.3", "pending"),
    ]

    for (sess, desc, status_val) in api_tasks:
        await db_module.log_task(db, sess["id"], api["id"], desc, status_val)

    for (title, version, status_val) in sprint_items_api:
        item_id = str(__import__("uuid").uuid4())
        try:
            await db.execute(
                "INSERT INTO sprint_items (id, project_id, title, version, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, datetime('now'))",
                (item_id, api["id"], title, version, status_val),
            )
        except Exception:  # noqa: BLE001
            pass
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        pass

    # 3 decisions
    try:
        decisions_text = (
            "2026-05-10: Chose Redis over Memcached — better data structures for rate-limit counters, "
            "native pub/sub for cache invalidation events.\n"
            "2026-05-14: Deferred GraphQL support to v3 — REST covers 95% of use cases, "
            "complexity not justified yet.\n"
            "2026-05-19: Canary via feature flag (LaunchDarkly) over nginx split — "
            "faster rollback, per-user targeting for beta customers."
        )
        await db.execute(
            "UPDATE projects SET decisions=? WHERE id=?",
            (decisions_text, api["id"]),
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        pass

    # ---- Project 2: data-pipeline ----
    pipe = await db_module.create_project(db, "data-pipeline")

    await db_module.set_north_star(
        db, pipe["id"],
        "Process 10M events/day with <5 min end-to-end latency, "
        "zero data loss, and full lineage tracking for compliance.",
    )
    await db_module.set_goal(
        db, pipe["id"],
        "DATA PIPELINE ETL v1.4\n\n"
        "SHIPPED (8/8):\n"
        "Kafka consumer group with dead-letter queue. Schema registry integration.\n"
        "dbt transformation layer (34 models). Airflow DAG orchestration.\n"
        "Snowflake loader with COPY INTO — 2.4M rows/min throughput.\n"
        "Data quality checks (Great Expectations). Lineage via OpenLineage.\n\n"
        "CURRENT FOCUS:\n"
        "1. Real-time streaming to replace nightly batch for 3 critical feeds\n"
        "2. PII masking in transit (GDPR compliance by June 1)\n"
        "3. Cost optimization — Snowflake credits up 40% MoM\n\n"
        "KEY FILES:\n"
        "pipelines/ — Airflow DAGs\n"
        "transforms/ — dbt models\n"
        "consumers/ — Kafka consumer workers\n"
        "tests/ — Great Expectations suites",
        minor=False,
    )
    await db_module.set_sprint(
        db, pipe["id"],
        "v1.4 week of May 26 — real-time streaming pilot + PII masking + cost review",
    )

    # 5 sessions for data-pipeline
    p_kafka  = await db_module.register_session(db, pipe["id"], "kafka-consumer-setup", human_id="marcus")
    p_dbt    = await db_module.register_session(db, pipe["id"], "dbt-models", human_id="priya")
    p_snow   = await db_module.register_session(db, pipe["id"], "snowflake-loader", human_id="marcus")
    p_qual   = await db_module.register_session(db, pipe["id"], "data-quality-checks", human_id="priya")
    p_stream = await db_module.register_session(db, pipe["id"], "realtime-streaming", human_id="sarah")

    pipe_tasks = [
        (p_kafka,  "Kafka consumer group with 6 partitions — throughput: 45k events/sec", "done"),
        (p_kafka,  "Dead-letter queue for poison messages — alerts on >10 DLQ events/hr", "done"),
        (p_kafka,  "Schema registry integration — Avro schemas versioned and enforced", "done"),
        (p_dbt,    "34 dbt models built — staging, intermediate, marts layers", "done"),
        (p_dbt,    "dbt tests: not_null, unique, referential integrity on all FK columns", "done"),
        (p_snow,   "Snowflake COPY INTO loader — 2.4M rows/min, merge on natural key", "done"),
        (p_snow,   "Partitioning strategy: DATE_TRUNC day on event_at — 60% query cost reduction", "done"),
        (p_qual,   "Great Expectations suite: 89 checks across 12 datasets, runs post-load", "done"),
        (p_qual,   "OpenLineage integration — full job/dataset lineage in Marquez UI", "done"),
        (p_stream, "Flink job prototype for click-stream real-time feed — POC passing", "done"),
        (p_stream, "PII masking spec written — SHA-256 hash for email/phone in transit", "pending"),
        (p_stream, "Snowflake credit analysis — top 5 cost drivers identified", "pending"),
        (p_kafka,  "Consumer lag alerting — PagerDuty when lag >100k messages", "pending"),
        (p_dbt,    "Incremental dbt models for 3 high-volume tables — replace full refresh", "backlog"),
        (p_snow,   "Snowflake auto-clustering on 4 high-query tables", "backlog"),
    ]

    for (sess, desc, status_val) in pipe_tasks:
        await db_module.log_task(db, sess["id"], pipe["id"], desc, status_val)

    # Sprint items for data-pipeline
    sprint_items_pipe = [
        ("Kafka consumer + DLQ", "v1.2", "done"),
        ("Schema registry", "v1.2", "done"),
        ("dbt transformation layer (34 models)", "v1.3", "done"),
        ("Snowflake COPY INTO loader", "v1.3", "done"),
        ("Great Expectations data quality", "v1.3", "done"),
        ("OpenLineage lineage tracking", "v1.3", "done"),
        ("Real-time streaming pilot (Flink)", "v1.4", "in_progress"),
        ("PII masking in transit (GDPR)", "v1.4", "pending"),
        ("Snowflake cost optimization", "v1.4", "pending"),
    ]

    for (title, version, status_val) in sprint_items_pipe:
        item_id = str(__import__("uuid").uuid4())
        try:
            await db.execute(
                "INSERT INTO sprint_items (id, project_id, title, version, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, datetime('now'))",
                (item_id, pipe["id"], title, version, status_val),
            )
        except Exception:  # noqa: BLE001
            pass
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


async def _seed_decisions_from_file(db, project_id: str) -> None:
    """One-time seed: parse DECISIONS.md → decisions_pinned table.

    Only runs when the table is empty for this project. Each ## section
    becomes one pinned decision. Safe to re-run: noop if rows already exist.
    """
    import re as _re
    count = await db_module.count_decisions(db, project_id)
    if count > 0:
        return

    decisions_path = _REPO_ROOT / "DECISIONS.md"
    if not decisions_path.exists():
        return

    text = decisions_path.read_text(encoding="utf-8", errors="replace")
    sections = _re.split(r'\n(?=## )', text)

    seeded = 0
    for section in sections:
        section = section.strip()
        if len(section) < 20 or not section.startswith("## "):
            continue
        lines = section.split("\n", 1)
        # Strip leading "## " from title
        title = lines[0][3:].strip()[:200]
        body = lines[1].strip() if len(lines) > 1 else ""
        if not title:
            continue
        # Guess category from title/body keywords
        cat = "TECHNICAL"
        low = title.lower() + " " + body[:200].lower()
        if any(w in low for w in ("competi", "positioning", "market")):
            cat = "COMPETITIVE"
        elif any(w in low for w in ("strateg", "princip", "philosophy")):
            cat = "STRATEGIC"
        elif any(w in low for w in ("pricing", "billing", "stripe", "revenue", "business")):
            cat = "BUSINESS"
        elif any(w in low for w in ("product", "feature", "ux", "ui", "dashboard")):
            cat = "PRODUCT"
        try:
            await db_module.pin_decision(db, project_id, title, body, category=cat)
            seeded += 1
        except Exception:  # noqa: BLE001
            pass

    import logging as _log
    _log.getLogger(__name__).info("Seeded %d decisions from DECISIONS.md", seeded)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the SQLite connection on startup, close it on shutdown.

    Also: load environment variables from ``./.env`` if present so the
    dashboard chat proxy can find ``ANTHROPIC_API_KEY``. We do this here
    rather than at import time so test fixtures can override the env
    without an .env file leaking into them.
    """
    # psycopg3 requires SelectorEventLoop on Windows.
    # Uvicorn resets the loop policy in its worker process — patch it here
    # before any psycopg3 pool creation happens.
    import sys as _sys
    if _sys.platform == "win32":
        import selectors as _sel
        import asyncio as _aio
        _loop = _aio.get_event_loop()
        if not isinstance(_loop, _aio.SelectorEventLoop):
            _new_loop = _aio.SelectorEventLoop(_sel.SelectSelector())
            _aio.set_event_loop(_new_loop)
    try:
        from dotenv import load_dotenv
        load_dotenv(override=False)
    except ImportError:
        pass  # dotenv is optional — env can be set by the launcher.

    data_dir = Path(os.environ.get("MERIDIAN_DATA_DIR", str(DEFAULT_DATA_DIR)))
    data_dir.mkdir(parents=True, exist_ok=True)

    # v1.9.x — meridian.toml connection profiles. Loaded before env check so
    # MERIDIAN_DB_URL env always wins (CI/containers).  Skipped for :memory:
    # so the test suite isn't affected.
    _db_override = os.environ.get("MERIDIAN_DB", DEFAULT_DB_PATH)
    if _db_override != ":memory:":
        _toml_url, _toml_conn_name = toml_config_module.get_toml_db_url()
        if _toml_url:
            # toml explicitly says use postgres — set env var
            os.environ["MERIDIAN_DB_URL"] = _toml_url
        elif _toml_conn_name is not None:
            # toml explicitly says use sqlite (local) — clear env var override
            os.environ.pop("MERIDIAN_DB_URL", None)
        # else: no toml at all — respect existing env var or use SQLite default

    db_url = os.environ.get("MERIDIAN_DB_URL")
    db_path: str | None = None
    if db_url:
        db = await db_module.init_db(db_url)
    else:
        db_path = os.environ.get("MERIDIAN_DB", DEFAULT_DB_PATH)
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        db = await db_module.init_db(db_path)
    app.state.db = db
    app.state.data_dir = str(data_dir)
    app.state.ws_broadcaster = dashboard_module.WebSocketBroadcaster()

    # v2.2 — isolated demo DB.
    # Priority: MERIDIAN_DEMO_DB_URL (separate Neon) → in-memory SQLite fallback.
    # Always wipe-and-reseed on startup so the demo is always fresh.
    # NEVER falls through to production DB.
    demo_db_url = os.environ.get("MERIDIAN_DEMO_DB_URL")

    async def _init_demo(url: str) -> None:
        demo_db = await db_module.init_db(url)
        app.state.demo_db = demo_db
        # Skip seeding when using an external Postgres demo DB —
        # it was seeded manually via scripts/seed_demo.py
        # Only seed in-memory SQLite fallback

    async def _init_demo_inmemory() -> None:
        demo_db = await db_module.init_db(":memory:")
        app.state.demo_db = demo_db
        await _seed_demo_data(demo_db)

    skip_demo = os.environ.get("MERIDIAN_SKIP_DEMO", "").lower() in ("1", "true", "yes")

    if skip_demo:
        app.state.demo_db = None
    elif demo_db_url:
        try:
            # 10s timeout — if Neon is slow/down, don't hang startup
            await asyncio.wait_for(_init_demo(demo_db_url), timeout=10.0)
        except Exception:  # noqa: BLE001
            try:
                await asyncio.wait_for(_init_demo_inmemory(), timeout=5.0)
            except Exception:  # noqa: BLE001
                app.state.demo_db = None
    else:
        try:
            await asyncio.wait_for(_init_demo_inmemory(), timeout=5.0)
        except Exception:  # noqa: BLE001
            app.state.demo_db = None

    # Guard: MERIDIAN_DEMO=true requires MERIDIAN_DEMO_DB_URL to prevent
    # accidental production DB exposure via the demo route.
    if os.environ.get("MERIDIAN_DEMO", "").lower() in ("1", "true", "yes"):
        if not os.environ.get("MERIDIAN_DEMO_DB_URL") and not os.environ.get("MERIDIAN_SKIP_DEMO"):
            import logging as _log
            _log.getLogger(__name__).warning(
                "MERIDIAN_DEMO=true but MERIDIAN_DEMO_DB_URL not set — "
                "using in-memory SQLite demo DB. Set MERIDIAN_DEMO_DB_URL for persistence."
            )

    # v0.4.2 — periodic auto-summary task. Interval comes from env so
    # tests can run it on a sub-second cadence; default is ten minutes.
    interval_s = float(os.environ.get("MERIDIAN_AUTO_SUMMARY_INTERVAL", 600))
    app.state.last_storage_check_ts = 0.0  # epoch seconds

    async def _auto_summary_loop() -> None:
        import time as _time
        while True:
            try:
                await asyncio.sleep(interval_s)
                await db_module.run_auto_summary_cycle(db)
                # v1.0.1 — PID watchdog: mark orphaned in_progress tasks as failed
                try:
                    stale = await db_module.get_in_progress_tasks_with_pid(db)
                    for t in stale:
                        pid = t.get("worker_pid")
                        if pid is None:
                            continue
                        try:
                            os.kill(int(pid), 0)  # 0 = check existence only
                        except (ProcessLookupError, PermissionError, OSError):
                            # PID is dead — mark the task failed
                            # OSError covers Windows WinError 87 for non-existent PIDs
                            await db_module.update_task(
                                db, t["id"],
                                status="failed",
                                description=(
                                    f"[claude-error] worker process died "
                                    f"unexpectedly (PID {pid})"
                                ),
                            )
                except Exception:  # noqa: BLE001
                    pass
                # v1.0 — hourly storage overage check (hosted only)
                if os.environ.get("MERIDIAN_HOSTED", "").lower() in ("1", "true", "yes"):
                    now_ts = _time.monotonic()
                    if now_ts - app.state.last_storage_check_ts >= 3600:
                        app.state.last_storage_check_ts = now_ts
                        try:
                            from .hosted import run_storage_overage_check
                            await run_storage_overage_check(db)
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            from .hosted import run_dunning_cleanup
                            await run_dunning_cleanup(db)
                        except Exception:  # noqa: BLE001
                            pass
                        # Daily at 3am: compute + storage overage check
                        import time as _time2
                        _local_hour = _time2.localtime().tm_hour
                        if _local_hour == 3:
                            try:
                                from .hosted import run_overage_check
                                await run_overage_check(db)
                            except Exception:  # noqa: BLE001
                                pass
                # v2.4 — refresh the CLAUDE.md <current_state> block for
                # every project on each cycle. Local dev only — skipped
                # on hosted multi-tenant deployments where there is no
                # repo-root CLAUDE.md to write.
                if os.environ.get("MERIDIAN_HOSTED", "").lower() not in ("1", "true", "yes"):
                    try:
                        await _refresh_claude_md_current_state(db, _REPO_ROOT)
                    except Exception:  # noqa: BLE001
                        pass
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001 — never let the loop die
                continue

    summary_task = asyncio.create_task(_auto_summary_loop())
    app.state.auto_summary_task = summary_task

    # v2.5 — seed decisions_pinned from DECISIONS.md on first run
    if db_path is None or db_path != ":memory:":
        try:
            projects = await db_module.list_projects(db)
            for _p in projects:
                await _seed_decisions_from_file(db, _p["id"])
        except Exception:  # noqa: BLE001
            pass

    # v0.6.3 — GOAL.md startup sync: if the file exists and names a known
    # project, pull its contents into the DB before serving any requests.
    if db_path is None or db_path != ":memory:":
        try:
            await goal_md_module.sync_goal_md_to_db(db)
        except Exception:  # noqa: BLE001
            pass

    # v0.6.3 — optional live file-watch (no-op when watchfiles not installed,
    # and skipped on Windows due to ProactorEventLoop deadlock with awatch).
    if db_path != ":memory:":
        watch_task = asyncio.create_task(goal_md_module.watch_goal_md(db))
    else:
        async def _noop_watch() -> None:
            pass
        watch_task = asyncio.create_task(_noop_watch())
    app.state.watch_task = watch_task

    # v1.7.0 — server update detection: hash key files at startup,
    # recheck every 60s, broadcast update_available to all WS clients if changed.
    def _server_hash() -> str:
        h = hashlib.md5()
        for rel in [
            "meridian/server.py",
            "meridian/db.py",
            "meridian/static/dashboard.js",
        ]:
            try:
                h.update(Path(rel).read_bytes())
            except OSError:
                pass
        return h.hexdigest()

    app.state.startup_hash = _server_hash()

    async def _version_check_loop() -> None:
        while True:
            try:
                await asyncio.sleep(60)
                if _server_hash() != app.state.startup_hash:
                    db_module.publish_global({"type": "update_available"})
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                continue

    version_task = asyncio.create_task(_version_check_loop())
    app.state.version_task = version_task

    try:
        yield

    finally:
        summary_task.cancel()
        watch_task.cancel()
        version_task.cancel()
        try:
            await summary_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        try:
            await watch_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await db.close()


app = FastAPI(
    title="Meridian",
    description="Multi-session Claude coordinator MCP server.",
    version="0.1.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Password gate middleware
# ---------------------------------------------------------------------------
_GATE_COOKIE = "meridian_site_access"
_GATE_MAX_AGE = 24 * 3600

def _gate_html(error=""):
    err = f'<p style="color:#f87171;margin-top:8px;font-size:13px">{error}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Meridian</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d0d0f;color:#e8eaf0;font-family:-apple-system,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center}}
.card{{background:#16181c;border:1px solid #2a2d35;border-radius:10px;padding:40px;max-width:360px;width:100%;margin:20px}}
.logo{{font-size:20px;font-weight:700;color:#6c8fff;margin-bottom:4px}}
.sub{{color:#8b8fa8;font-size:12px;margin-bottom:28px}}
label{{display:block;font-size:13px;color:#8b8fa8;margin-bottom:6px}}
input{{width:100%;padding:10px 12px;background:#0d0d0f;border:1px solid #2a2d35;border-radius:6px;color:#e8eaf0;font-size:14px;outline:none}}
input:focus{{border-color:#6c8fff}}
button{{width:100%;margin-top:14px;padding:11px;background:#6c8fff;border:none;border-radius:6px;color:#fff;font-weight:700;font-size:14px;cursor:pointer}}</style>
</head><body><div class="card">
<div class="logo">⬡ Meridian</div>
<div class="sub">Preview access required</div>
<form method="post" action="/__gate__">
<label>Password</label>
<input type="password" name="password" autofocus placeholder="Enter preview password">
{err}<button type="submit">Continue</button></form></div></body></html>"""

@app.middleware("http")
async def site_password_gate(request: Request, call_next):
    site_pw = os.environ.get("SITE_PASSWORD", "")
    if not site_pw:
        return await call_next(request)
    path = request.url.path
    if path in ("/health", "/mcp/health", "/__gate__", "/config", "/static", "/mcp/tools-doc") or path.startswith("/static/") or path == "/demo" or path.startswith("/demo/"):
        return await call_next(request)
    # Demo cookie bypasses site password gate — demo users don't go through __gate__
    if request.cookies.get(_DEMO_CONTEXT_COOKIE):
        return await call_next(request)
    from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
    from fastapi.responses import HTMLResponse
    secret = os.environ.get("SESSION_SECRET", "fallback")
    token = request.cookies.get(_GATE_COOKIE, "")
    valid = False
    if token:
        try:
            URLSafeTimedSerializer(secret).loads(token, max_age=_GATE_MAX_AGE)
            valid = True
        except Exception:
            valid = False
    if not valid:
        return HTMLResponse(_gate_html())
    return await call_next(request)

@app.post("/__gate__")
async def gate_submit(request: Request):
    from fastapi.responses import HTMLResponse, RedirectResponse
    from itsdangerous import URLSafeTimedSerializer
    site_pw = os.environ.get("SITE_PASSWORD", "")
    secret = os.environ.get("SESSION_SECRET", "fallback")
    form = await request.form()
    entered = form.get("password", "")
    if entered != site_pw:
        return HTMLResponse(_gate_html("Incorrect password"))
    token = URLSafeTimedSerializer(secret).dumps("granted")
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(_GATE_COOKIE, token, max_age=_GATE_MAX_AGE, httponly=True, samesite="lax")
    return response

# ---------------------------------------------------------------------------
# v2.0 — Rate limiter (slowapi, in-memory, no Redis required)
# ---------------------------------------------------------------------------
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address

    _limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = _limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    _RATE_LIMIT = "60/minute"
except ImportError:
    _limiter = None  # type: ignore[assignment]
    _RATE_LIMIT = "60/minute"


# ---------------------------------------------------------------------------
# v2.0-fixes — Demo read-only middleware (MERIDIAN_DEMO=true)
# ---------------------------------------------------------------------------

_DEMO_WRITE_ALLOWLIST = {"/demo-auth", "/waitlist", "/auth/magic", "/auth/login", "/auth/logout", "/__gate__"}
_DEMO_CONTEXT_COOKIE = "meridian_demo"


@app.middleware("http")
async def _demo_read_only_middleware(request: Request, call_next):
    """Block all mutating requests when MERIDIAN_DEMO=true or demo cookie set.

    /demo-auth and /waitlist are exempted so the password gate and waitlist
    signup still function in preview mode.
    """
    env_demo = os.environ.get("MERIDIAN_DEMO", "").lower() in ("1", "true", "yes")
    cookie_demo = bool(request.cookies.get(_DEMO_CONTEXT_COOKIE))
    if (
        (env_demo or cookie_demo)
        and request.method in ("POST", "PUT", "PATCH", "DELETE")
        and request.url.path not in _DEMO_WRITE_ALLOWLIST
    ):
        return Response(
            content=json.dumps({"detail": "Demo mode — read only"}),
            status_code=403,
            media_type="application/json",
        )
    return await call_next(request)


# ---------------------------------------------------------------------------
# v1.0.2 — Static files + Jinja2 templates
# ---------------------------------------------------------------------------


def _resource_path(relative: str) -> str:
    """Resolve a resource path relative to the package root.

    Works in dev (relative to repo) and in frozen PyInstaller exe.
    In a frozen exe, ``sys._MEIPASS`` is the temp directory where PyInstaller
    unpacks the bundle; in development it falls back to the repo root
    (two levels above this file's directory).
    """
    import sys
    base = getattr(sys, "_MEIPASS", Path(__file__).parent.parent)
    return str(Path(base) / relative)


app.mount(
    "/static",
    StaticFiles(directory=_resource_path("meridian/static")),
    name="static",
)
_templates = Jinja2Templates(directory=_resource_path("meridian/templates"))


def _hosted_mode() -> bool:
    """Return True when running as a hosted service (MERIDIAN_HOSTED=1)."""
    return os.environ.get("MERIDIAN_HOSTED", "").lower() in ("1", "true", "yes")


def _db(request: Request) -> aiosqlite.Connection:
    """Pull the active DB connection off ``app.state``.

    Returns the demo DB when the meridian_demo cookie is set and a demo DB
    was initialised from MERIDIAN_DEMO_DB_URL.
    """
    if request.cookies.get(_DEMO_CONTEXT_COOKIE):
        demo_db = getattr(request.app.state, "demo_db", None)
        if demo_db is not None:
            return demo_db
        # HARD FAIL — never fall through to production DB under demo cookie
        raise HTTPException(
            status_code=503,
            detail="Demo DB not available. Set MERIDIAN_DEMO_DB_URL to enable the demo.",
        )
    return request.app.state.db


def _data_dir(request: Request) -> str:
    """Pull the active data directory off ``app.state``."""
    return request.app.state.data_dir


# ---------------------------------------------------------------------------
# FastAPI endpoints
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def landing_page(request: Request) -> HTMLResponse:
    """Landing page — headline, CTAs, waitlist form.

    When DEMO_PASSWORD is set the page is behind a password gate so the
    preview URL can be shared without exposing the live dashboard publicly.
    """
    if os.environ.get("DEMO_PASSWORD"):
        if not _check_demo_cookie(request):
            return HTMLResponse(_demo_gate_html())
    stripe_payment_link = os.environ.get("STRIPE_PAYMENT_LINK", "/auth/login")
    stripe_pro_checkout = "/checkout?plan=pro" if os.environ.get("STRIPE_PRO_PRICE_ID") else ""
    stripe_pro_payment_link = os.environ.get("STRIPE_PRO_PAYMENT_LINK", stripe_pro_checkout)
    return _templates.TemplateResponse(
        request, "landing.html", {
            "stripe_payment_link": stripe_payment_link,
            "stripe_pro_checkout": stripe_pro_checkout,
            "stripe_pro_payment_link": stripe_pro_payment_link,
        }
    )


@app.post("/demo-auth")
async def demo_auth_post(request: Request):
    """Validate demo password and set a signed access cookie.

    On success redirects to /dashboard; on failure re-renders the gate page
    with an error message.  Exempt from the MERIDIAN_DEMO read-only
    middleware so this endpoint is reachable in preview mode.
    """
    from fastapi.responses import RedirectResponse
    from itsdangerous import URLSafeTimedSerializer

    form = await request.form()
    password = str(form.get("password", ""))
    expected = os.environ.get("DEMO_PASSWORD", "")
    if not expected or password != expected:
        return HTMLResponse(
            _demo_gate_html("Incorrect password. Please try again."),
            status_code=401,
        )
    secret = (
        os.environ.get("SESSION_SECRET")
        or os.environ.get("MERIDIAN_SESSION_SECRET")
        or "demo-fallback-secret"
    )
    token = URLSafeTimedSerializer(secret).dumps("demo")
    resp = RedirectResponse(url="/dashboard", status_code=302)
    resp.set_cookie(
        _DEMO_COOKIE,
        token,
        max_age=_DEMO_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return resp


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "service": "meridian"}


@app.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request) -> HTMLResponse:
    """Static Terms of Service page."""
    return _templates.TemplateResponse(request, "terms.html")


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request) -> HTMLResponse:
    """Static Privacy Policy page."""
    return _templates.TemplateResponse(request, "privacy.html")


# ---------------------------------------------------------------------------
# v2.0 — Google OAuth routes
# ---------------------------------------------------------------------------

@app.get("/auth/login")
async def auth_login(request: Request):
    """Serve sign-in page with Google and GitHub OAuth buttons."""
    from .hosted import auth_login as _auth_login
    return await _auth_login(request)


@app.get("/auth/google/login")
async def auth_google_login(request: Request):
    """Redirect browser directly to Google OAuth consent page."""
    from .hosted import auth_google_login as _auth_google_login
    return await _auth_google_login(request)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    """Handle Google OAuth callback — create/update tenant, set session cookie."""
    from .hosted import auth_callback as _auth_callback
    return await _auth_callback(request)


@app.get("/auth/github/login")
async def auth_github_login(request: Request):
    """Redirect browser to GitHub OAuth consent page."""
    from .hosted import auth_github_login as _auth_github_login
    return await _auth_github_login(request)


@app.get("/auth/github/callback")
async def auth_github_callback(request: Request):
    """Handle GitHub OAuth callback — create/update tenant, set session cookie."""
    from .hosted import auth_github_callback as _auth_github_callback
    return await _auth_github_callback(request)


@app.get("/auth/microsoft/login")
async def auth_microsoft_login(request: Request):
    """Redirect browser to Microsoft OAuth consent page."""
    from .hosted import auth_microsoft_login as _auth_microsoft_login
    return await _auth_microsoft_login(request)


@app.get("/auth/microsoft/callback")
async def auth_microsoft_callback(request: Request):
    """Handle Microsoft OAuth callback — create/update tenant, set session cookie."""
    from .hosted import auth_microsoft_callback as _auth_microsoft_callback
    return await _auth_microsoft_callback(request)


@app.get("/auth/email-required")
async def auth_email_required(request: Request) -> HTMLResponse:
    """Shown when OAuth provider returned no usable email (e.g. GitHub with private email)."""
    provider = request.query_params.get("provider", "your provider")
    html = f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<title>Email required — Meridian</title>
<style>body{{font-family:system-ui,sans-serif;max-width:480px;margin:80px auto;padding:0 20px;color:#e8eaed;background:#0d1117}}
h2{{color:#58a6ff}}p{{color:#8b949e;line-height:1.6}}a{{color:#58a6ff}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:24px;margin-top:24px}}
</style></head><body>
<div class="card">
<h2>Email address required</h2>
<p>We couldn't get a verified email from {provider}. Meridian needs your email to create your account.</p>
<p>To fix this:<br>
&nbsp;&nbsp;1. Go to <a href="https://github.com/settings/emails" target="_blank" rel="noopener">github.com/settings/emails</a><br>
&nbsp;&nbsp;2. Add and verify a primary email address<br>
&nbsp;&nbsp;3. <a href="/auth/github/login">Try signing in again</a></p>
<p>Or <a href="/auth/login">use a magic link</a> to sign in with your email directly.</p>
</div>
</body></html>"""
    return HTMLResponse(html)


@app.get("/auth/logout")
async def auth_logout(request: Request):
    """Clear session cookie and delete DB session."""
    from .hosted import auth_logout as _auth_logout
    return await _auth_logout(request)


@app.post("/auth/magic")
async def auth_magic_request(request: Request):
    """v0.9 — request a magic-link email. Rate-limited.

    Body: ``{"email": "user@example.com"}``. Sends a single-use signed
    link via Resend. Idempotent within the 24-hour token window — if a
    valid unused token exists for this email, returns success without
    sending a duplicate email.
    """
    from .hosted import auth_magic_request as _impl
    return await _impl(request)


@app.get("/auth/magic/verify")
async def auth_magic_verify(request: Request, token: str = ""):
    """v0.9 — consume a magic-link token, create a session, redirect.

    Single-use: marks ``used_at`` on success so re-clicking the same
    link doesn't re-authenticate. New tenants flow through the OAuth
    paywall check — redirected to /pricing?signup=1 if no Stripe
    subscription yet.
    """
    from .hosted import auth_magic_verify as _impl
    return await _impl(request, token)


@app.get("/config")
async def server_config() -> dict[str, Any]:
    """v0.6.5 — expose runtime configuration to the dashboard.

    Allows the frontend to be location-agnostic: it reads the server URL
    from this endpoint rather than hardcoding localhost. Also used by the
    PyInstaller exe launcher to confirm the server is up.

    Fields:
      * ``server_url`` — the absolute URL the frontend should target.
        Defaults to ``http://{host}:{port}`` but is overridable via
        ``MERIDIAN_SERVER_URL`` for hosted / reverse-proxied deployments.
      * ``host`` / ``port`` — split form, useful for tooling.
      * ``version`` — current Meridian version, surfaced in the dashboard
        title bar so a user can see which build they're talking to.
      * ``db`` — ``memory`` or ``sqlite``; lets the dashboard show a
        scratch-DB warning during tests.
    """
    host = os.environ.get("MERIDIAN_HOST", "127.0.0.1")
    port = int(os.environ.get("MERIDIAN_PORT", "7878"))
    default_url = f"http://{host}:{port}"
    server_url = os.environ.get("MERIDIAN_SERVER_URL", default_url)
    _, conn_name = toml_config_module.get_active_db_url()
    return {
        "server_url": server_url,
        "host": host,
        "port": port,
        "version": _VERSION,
        "db": (
            "postgres" if os.environ.get("MERIDIAN_DB_URL")
            else "memory" if os.environ.get("MERIDIAN_DB") == ":memory:"
            else "sqlite"
        ),
        "toml_exists": toml_config_module.toml_exists(),
        "toml_path": str(toml_config_module._toml_path() or (Path.cwd() / "meridian.toml")),
        "connection_name": conn_name,
        "connections": toml_config_module.list_connections(),
        "demo_mode": os.environ.get("MERIDIAN_DEMO", "").lower() in ("1", "true", "yes"),
    }


@app.get("/tools")
async def list_tools_endpoint() -> list[dict[str, Any]]:
    """Return MCP tool definitions for the dashboard Docs vtab."""
    return _MCP_TOOLS_LIST


@app.get("/projects", response_model=list[Project])
async def list_projects(request: Request) -> list[dict[str, Any]]:
    """List every project."""
    return await db_module.list_projects(_db(request))


@app.post("/projects", response_model=Project, status_code=201)
async def create_project(
    body: ProjectCreate, request: Request
) -> dict[str, Any]:
    """Create a new project. 409 if the name is already in use."""
    existing = await db_module.get_project_by_name(_db(request), body.name)
    if existing is not None:
        raise HTTPException(
            status_code=409, detail=f"project '{body.name}' already exists"
        )
    return await db_module.create_project(
        _db(request), body.name, human_id=body.human_id
    )


@app.get("/setup/needed")
async def setup_needed(request: Request) -> dict[str, Any]:
    """Returns {needed: true} if no projects exist yet (first-run wizard trigger)."""
    projects = await db_module.list_projects(_db(request))
    return {"needed": len(projects) == 0}


@app.get("/projects/by-name/{name}")
async def get_project_by_name(name: str, request: Request) -> dict[str, Any]:
    """Look up a project by name (case-insensitive substring match).

    Returns the project row plus a brief goal summary so a cold session
    can confirm it found the right project without a second round-trip.
    """
    db = _db(request)
    # Exact match first (most common case).
    project = await db_module.get_project_by_name(db, name)
    if project is None:
        # Case-insensitive substring fallback.
        all_projects = await db_module.list_projects(db)
        lower = name.lower()
        matches = [p for p in all_projects if lower in p["name"].lower()]
        if matches:
            project = matches[0]
    if project is None:
        raise HTTPException(
            status_code=404, detail=f"no project found matching '{name}'"
        )
    goal = await db_module.get_goal(db, project["id"])
    return {
        "project": project,
        "goal_version": goal["version"] if goal else None,
        "goal_summary": (str(goal["content"])[:200] if goal else None),
    }


@app.get("/projects/{project_id}", response_model=Project)
async def get_project(project_id: str, request: Request) -> dict[str, Any]:
    """Look up a project by id."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@app.post("/projects/{project_id}/rename")
async def rename_project(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """v1.9.x — rename a project.  Broadcasts project_renamed WS event."""
    new_name = str(body.get("name") or "").strip()
    if not new_name:
        raise HTTPException(400, "name is required")
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    existing = await db_module.get_project_by_name(_db(request), new_name)
    if existing and existing["id"] != project_id:
        raise HTTPException(409, f"project '{new_name}' already exists")
    updated = await db_module.rename_project(_db(request), project_id, new_name)
    db_module.publish_global(
        {"type": "project_renamed", "project_id": project_id, "name": new_name}
    )
    return updated


@app.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: str, request: Request) -> None:
    """v1.9.x — delete a project and all data.

    Returns 409 if any tasks are in_progress, 404 if the project is unknown.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    try:
        await db_module.delete_project(_db(request), project_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/projects/{project_id}/goal", response_model=GoalState)
async def get_goal(project_id: str, request: Request) -> dict[str, Any]:
    """Read the latest goal state plus ambient task context.

    The response payload (v0.4.2+) includes ``ambient_tasks`` — the
    five most recent task rows, newest first, as ``{status, description,
    created_at}`` dicts. Cold sessions can render the directive *and*
    last activity from a single MCP call.

    404 if the project does not exist or the goal hasn't been set yet.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    goal = await db_module.get_goal(_db(request), project_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not set")
    recent = await db_module.get_tasks(_db(request), project_id, limit=5)
    goal["ambient_tasks"] = [
        {
            "status": t["status"],
            "description": t["description"],
            "created_at": t["created_at"],
        }
        for t in recent
    ]
    # v1.1.3 — per-field ages + coherence warning so the dashboard
    # can paint green / amber / red dots and so cold sessions see
    # which fields have gone stale before doing anything.
    field_ages = await db_module.get_goal_field_ages(
        _db(request), project_id
    )
    coherence = db_module.compute_coherence_warning(field_ages)
    goal["field_ages"] = field_ages
    goal["coherence_warning"] = coherence
    # v1.1.4 — append-only decisions log.
    decisions = await db_module.get_decisions(_db(request), project_id)
    goal["decisions"] = decisions
    # v0.6.1 — also serve the XML envelope so MCP / cache-aware consumers
    # don't have to re-stitch fields locally. The JSON keys stay for the
    # dashboard and the test suite.
    goal["xml"] = db_module.build_goal_xml(
        goal, project["name"], goal["ambient_tasks"], coherence,
        decisions=decisions,
    )
    # v0.6.2 — pre-built Anthropic content blocks with cache_control
    # markers on the static fields. Callers can pass these straight
    # into messages.create() to get prompt caching for free.
    goal["cache_blocks"] = db_module.build_goal_cache_blocks(
        goal, project["name"], goal["ambient_tasks"]
    )
    return goal


@app.patch("/projects/{project_id}/goal-mode")
async def patch_goal_mode(
    project_id: str, body: GoalModeSet, request: Request
) -> dict[str, str]:
    """Switch a project between 'manual' and 'auto' goal modes."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        await db_module.set_goal_mode(_db(request), project_id, body.mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"project_id": project_id, "goal_mode": body.mode}


@app.get("/projects/{project_id}/goal-mode")
async def get_goal_mode(project_id: str, request: Request) -> dict[str, str]:
    """Return the current goal mode for a project."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    mode = await db_module.get_goal_mode(_db(request), project_id)
    return {"project_id": project_id, "goal_mode": mode}


@app.post("/projects/{project_id}/goal", response_model=GoalState)
async def set_goal(
    project_id: str, body: GoalSet, request: Request
) -> dict[str, Any]:
    """Upsert the goal state, incrementing version.

    Goal-ownership rule (v0.3.2): if the project has a recorded
    ``creator_human_id`` *and* the request body supplies a ``human_id``
    that doesn't match, refuse with 403. Sessions without a human_id
    (legacy callers, MCP workers that don't claim an identity) keep
    their old write privilege.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    owner = await db_module.get_project_owner(_db(request), project_id)
    if owner is not None and body.human_id is not None and body.human_id != owner:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "goal_locked",
                "message": (
                    "Only the project owner can set the goal. "
                    "Use the HITL queue to propose changes."
                ),
            },
        )
    result = await db_module.set_goal(
        _db(request), project_id, body.content,
        north_star=body.north_star, sprint=body.sprint,
        minor=body.minor,
    )
    await goal_md_module.sync_db_to_goal_md(_db(request), project_id)
    return result


@app.post("/projects/{project_id}/goal/north-star", response_model=GoalState)
async def set_north_star(
    project_id: str, body: SetNorthStarRequest, request: Request
) -> dict[str, Any]:
    """v0.5.2 — update only the north star field.

    Owner-only: requires ``human_id`` matching the project creator.
    Returns the new goal version.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    owner = await db_module.get_project_owner(_db(request), project_id)
    if owner is not None and body.human_id != owner:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "goal_locked",
                "message": "Only the project owner can set the north star.",
            },
        )
    try:
        result = await db_module.set_north_star(
            _db(request), project_id, body.north_star
        )
        await goal_md_module.sync_db_to_goal_md(_db(request), project_id)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/projects/{project_id}/goal/sprint", response_model=GoalState)
async def set_sprint(
    project_id: str, body: SetSprintRequest, request: Request
) -> dict[str, Any]:
    """v0.5.2 — update only the sprint field.

    Any team member can update the sprint — no ownership check.
    Returns the new goal version.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        result = await db_module.set_sprint(
            _db(request), project_id, body.sprint
        )
        await goal_md_module.sync_db_to_goal_md(_db(request), project_id)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/projects/{project_id}/start-worker-session")
async def start_worker_session_endpoint(
    project_id: str, request: Request,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """v1.2.0 — REST mirror of the MCP ``start_worker_session`` tool.

    Optional body: ``{task_id}``. Returns
    ``{session_id, task, worker_context}`` or 404 when there's no
    claimable task / the named task doesn't belong to this project.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        return await db_module.start_worker_session(
            _db(request),
            project_id,
            task_id=(body or {}).get("task_id"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/projects/{project_id}/decisions")
async def post_decision_endpoint(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """v1.1.4 — append a decision entry to the project's append-only
    decisions log. Body: ``{text}``."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")
    updated = await db_module.set_decision(_db(request), project_id, text)
    return {"project_id": project_id, "decisions": updated}


@app.get("/projects/{project_id}/timeline")
async def get_timeline_endpoint(
    project_id: str, request: Request
) -> dict[str, Any]:
    """v1.1.1 — return the data needed to render the Activity Timeline.

    v1.6.x — filtered to only meaningful history: completed/failed tasks
    plus goal-change events. Session idle/active events were noise and
    have been dropped (the LIVE vtab covers active sessions instead).
    Pending/in_progress tasks belong on the LIVE tab, not in history.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    timeline = await db_module.get_timeline(_db(request), project_id)
    return {
        "tasks": [
            t for t in timeline.get("tasks", [])
            if t.get("status") in ("done", "failed")
        ],
        "sessions": [],
        "goal_events": timeline.get("goal_events", []),
    }


@app.get("/projects/{project_id}/rewind")
async def get_rewind(
    project_id: str,
    request: Request,
    days: int = 7,
    token: str | None = None,
) -> dict[str, Any]:
    """v1.3.0 — "Last X days" project rewind summary.

    Returns versions shipped, goal changes, decisions logged, session
    summaries, sprint items completed, and task counts for the period.
    When a ``token`` query param is supplied, it must match the project's
    stored ``rewind_token`` — letting an external link validate ownership
    without any other auth (Meridian is local-first; no token = no auth
    required, same as every other endpoint).
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if token is not None:
        stored = await db_module.get_rewind_token(_db(request), project_id)
        if not stored or token != stored:
            raise HTTPException(status_code=403, detail="invalid rewind token")
    if days <= 0:
        raise HTTPException(status_code=422, detail="days must be positive")
    return await db_module.get_rewind_data(_db(request), project_id, days)


@app.post("/projects/{project_id}/rewind-token")
async def post_rewind_token(
    project_id: str, request: Request
) -> dict[str, str]:
    """v1.3.0 — mint (or return) the project's shareable rewind token.

    The token is stored on the projects row so subsequent calls return
    the same value; teams can publish a link once without it rotating.
    Response: ``{"token": "<uuid4>", "expires": "never"}``.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    token = await db_module.get_or_create_rewind_token(_db(request), project_id)
    return {"token": token, "expires": "never"}


@app.get("/projects/{project_id}/goal-history")
async def get_goal_history(
    project_id: str, request: Request
) -> list[dict[str, Any]]:
    """Return meaningful goal versions for a project, newest first.

    AUTO BLOCKS-only versions are collapsed out so the history shows
    only real content changes. Each entry: version, north_star,
    version_goal, sprint, created_at. Used by the Rewind goal subtab.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.get_goal_history(_db(request), project_id)


@app.get("/projects/{project_id}/stats")
async def get_project_stats(
    project_id: str, request: Request, days: int = 30
) -> dict[str, Any]:
    """Return activity stats for the Charts subtab.

    Returns tasks/day series and sprint completion % per version.
    ``days`` defaults to 30, max 365.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    days = max(1, min(days, 365))
    return await db_module.get_project_stats(_db(request), project_id, days)


# ---------------------------------------------------------------------------
# Sprint items (v1.1) — checklist alongside the free-text sprint field.
# ---------------------------------------------------------------------------


@app.get("/projects/{project_id}/sprint-items")
async def list_sprint_items(
    project_id: str, request: Request, status: str | None = None
) -> list[dict[str, Any]]:
    """List sprint items, optionally filtered by status."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        return await db_module.get_sprint_items(
            _db(request), project_id, status=status
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post(
    "/projects/{project_id}/sprint-items", status_code=201
)
async def add_sprint_item_endpoint(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Append a todo sprint item.

    Body: ``{version, title, group?, human_id?}``.
    ``group`` (alias ``item_group``) groups the item under a named objective
    on the sprint board.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    version = (body.get("version") or "").strip()
    title = (body.get("title") or "").strip()
    if not version or not title:
        raise HTTPException(
            status_code=422, detail="version and title are required"
        )
    group = body.get("group") or body.get("item_group") or None
    human_id = body.get("human_id") or None
    depends_on = body.get("depends_on") or None
    failure_mode = body.get("failure_mode") or None
    return await db_module.add_sprint_item(
        _db(request), project_id, version, title,
        group=group, human_id=human_id,
        depends_on=depends_on, failure_mode=failure_mode,
    )


@app.post("/projects/{project_id}/sprint-items/{item_id}/complete")
async def complete_sprint_item_endpoint(
    project_id: str, item_id: str, request: Request,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark a sprint item ``done``. Optional body: ``{task_id}``."""
    item = await db_module.complete_sprint_item(
        _db(request),
        project_id,
        item_id,
        task_id=(body or {}).get("task_id"),
    )
    if item is None:
        raise HTTPException(status_code=404, detail="sprint item not found")
    await _update_roadmap_version_history(
        _db(request), project_id, item["version"], _REPO_ROOT
    )
    return item


@app.post("/projects/{project_id}/sprint-items/{item_id}/skip")
async def skip_sprint_item_endpoint(
    project_id: str, item_id: str, request: Request,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark a sprint item ``skipped``. Optional body: ``{reason}``."""
    item = await db_module.skip_sprint_item(
        _db(request),
        project_id,
        item_id,
        reason=(body or {}).get("reason"),
    )
    if item is None:
        raise HTTPException(status_code=404, detail="sprint item not found")
    return item


@app.post("/projects/{project_id}/sprint-items/{item_id}/fail")
async def fail_sprint_item_endpoint(
    project_id: str, item_id: str, request: Request,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark a sprint item ``failed``. Optional body: ``{reason}``."""
    item = await db_module.fail_sprint_item(
        _db(request),
        project_id,
        item_id,
        reason=(body or {}).get("reason"),
    )
    if item is None:
        raise HTTPException(status_code=404, detail="sprint item not found")
    return item


@app.delete("/projects/{project_id}/sprint-items/{item_id}", status_code=204)
async def delete_sprint_item_endpoint(
    project_id: str, item_id: str, request: Request
) -> None:
    """Delete a sprint item permanently."""
    db = _db(request)
    await db.execute(
        "DELETE FROM sprint_items WHERE id = ? AND project_id = ?",
        (item_id, project_id),
    )
    await db.commit()


@app.patch("/projects/{project_id}/sprint-items/{item_id}")
async def patch_sprint_item_endpoint(
    project_id: str, item_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Update editable fields (title, version) of a sprint item."""
    title = body.get("title")
    if title is not None:
        title = title.strip()
        if not title:
            raise HTTPException(status_code=422, detail="title cannot be empty")
    version = body.get("version")
    if version is not None:
        version = version.strip() or None
    feedback_thumb = body.get("feedback_thumb")
    if feedback_thumb is not None:
        try:
            feedback_thumb = int(feedback_thumb)
            if feedback_thumb not in (-1, 1):
                raise ValueError
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="feedback_thumb must be -1 or 1")
    feedback_note = body.get("feedback_note")
    item = await db_module.patch_sprint_item(
        _db(request), project_id, item_id, title=title, version=version,
        feedback_thumb=feedback_thumb, feedback_note=feedback_note,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="sprint item not found")
    return item


@app.post("/projects/{project_id}/sprint-items/{item_id}/push")
async def push_sprint_item_endpoint(
    project_id: str, item_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Push a sprint item to a future version. Body: ``{to_version}``."""
    to_version = (body.get("to_version") or "").strip()
    if not to_version:
        raise HTTPException(status_code=422, detail="to_version is required")
    try:
        item = await db_module.push_sprint_item(
            _db(request), project_id, item_id, to_version
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if item is None:
        raise HTTPException(status_code=404, detail="sprint item not found")
    return item


@app.get("/projects/{project_id}/sessions", response_model=list[Session])
async def get_sessions(
    project_id: str, request: Request, active_only: bool = True
) -> list[dict[str, Any]]:
    """List sessions attached to the project.

    Pass ``?active_only=false`` to include closed and archived sessions
    (useful for the LIVE tab showing recent session outcomes).
    Expires stale sessions (last_seen > 30 min ago) before returning so
    the dashboard doesn't accumulate ghost entries indefinitely.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    await _expire_and_generate_handoffs(_db(request), _data_dir(request))
    return await db_module.get_sessions(
        _db(request), project_id, active_only=active_only
    )


@app.get("/projects/{project_id}/tasks", response_model=list[Task])
async def get_tasks(
    project_id: str, request: Request, limit: int = 20
) -> list[dict[str, Any]]:
    """List recent tasks for a project, newest first."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.get_tasks(_db(request), project_id, limit=limit)


@app.get("/projects/{project_id}/tasks/claimable", response_model=list[Task])
async def get_claimable_tasks(
    project_id: str, request: Request, limit: int = 20
) -> list[dict[str, Any]]:
    """List unclaimed pending tasks for a project (v0.3.3).

    Workers poll this endpoint to find work that isn't already locked
    by another session.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.get_claimable_tasks(
        _db(request), project_id, limit=limit
    )


@app.post(
    "/projects/{project_id}/tasks/claim", response_model=ClaimTaskResponse
)
async def claim_task_endpoint(
    project_id: str, body: ClaimTaskRequest, request: Request
) -> dict[str, Any]:
    """Atomically claim a pending task. Returns ``claimed=False`` when
    another worker holds the lock — the worker should try the next row."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    claimed = await db_module.claim_task(
        _db(request), body.task_id, body.session_id
    )
    if claimed is None:
        existing = await db_module.get_task(_db(request), body.task_id)
        return {
            "task_id": body.task_id,
            "claimed": False,
            "claimed_by": existing["claimed_by"] if existing else None,
        }
    return {
        "task_id": body.task_id,
        "claimed": True,
        "claimed_by": claimed["claimed_by"],
    }


@app.post("/projects/{project_id}/tasks/release")
async def release_task_endpoint(
    project_id: str, body: ClaimTaskRequest, request: Request
) -> dict[str, Any]:
    """Release a previously-claimed task. 404 when no claim is held
    by the given session."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    released = await db_module.release_task(
        _db(request), body.task_id, body.session_id
    )
    if not released:
        raise HTTPException(
            status_code=404,
            detail="task not claimed by this session",
        )
    return {"task_id": body.task_id, "released": True}


@app.post("/projects/{project_id}/handoff", response_model=HandoffResult)
async def generate_handoff(
    project_id: str, request: Request
) -> dict[str, Any]:
    """Render and write the handoff file for a project."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    # v2.4 — skip the Haiku ai_summary call when ANTHROPIC_API_KEY is
    # missing. The generator already falls back gracefully but the
    # network round-trip on every dashboard click is wasteful when the
    # key obviously isn't configured. Local devs without ANTHROPIC_API_KEY
    # get instant handoffs; hosted deploys with the key get summaries.
    import os as _os
    skip_summary = not _os.environ.get("ANTHROPIC_API_KEY")
    db = _db(request)
    data_dir = _data_dir(request)
    try:
        path, content = await asyncio.wait_for(
            handoff_module.generate_handoff(
                db, project_id, data_dir, skip_ai_summary=skip_summary
            ),
            timeout=90.0,
        )
    except asyncio.TimeoutError:
        path, content = await handoff_module._generate_handoff_l0(
            db, project_id, data_dir
        )
    return {"path": path, "content": content}


@app.post("/sessions/register", response_model=Session, status_code=201)
async def register_session(
    body: SessionRegister, request: Request
) -> dict[str, Any]:
    """Create a session row tied to a project."""
    project = await db_module.get_project(_db(request), body.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.register_session(
        _db(request), body.project_id, body.name,
        human_id=body.human_id,
        agent_framework=body.agent_framework,
    )


@app.post("/sessions/{session_id}/close")
async def close_session(session_id: str, request: Request) -> dict[str, str]:
    """Mark a session closed."""
    async with _db(request).execute(
        "SELECT id, project_id FROM sessions WHERE id = ?", (session_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    project_id = row["project_id"]
    await db_module.close_session(_db(request), session_id)
    try:
        await db_module.summarize_session(_db(request), session_id)
    except Exception:
        pass
    await _regenerate_claude_md(_db(request), project_id, _REPO_ROOT)
    # v2.5 — auto-save handoff on session close so the file is always fresh.
    async def _auto_save_handoff() -> None:
        try:
            await asyncio.wait_for(
                handoff_module.generate_handoff(
                    _db(request), project_id, request.app.state.data_dir
                ),
                timeout=30.0,
            )
        except Exception:  # noqa: BLE001 — never block session close
            pass
    asyncio.create_task(_auto_save_handoff())
    return {"status": "closed", "session_id": session_id}


@app.post("/sessions/{session_id}/heartbeat")
async def heartbeat_session(
    session_id: str, request: Request
) -> dict[str, str]:
    """v0.5.1 — touch ``last_seen`` to keep this session out of the
    idle sweep. Long-running workers call this every few minutes so
    the 30 minute TTL doesn't expire them while they're still alive.
    404 when the session id is unknown or already closed."""
    ok = await db_module.heartbeat_session(_db(request), session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="session not found")
    return {"status": "ok", "session_id": session_id}


@app.post("/tasks", response_model=Task, status_code=201)
async def create_task(body: TaskCreate, request: Request) -> dict[str, Any]:
    """Append a task-log entry."""
    project = await db_module.get_project(_db(request), body.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    async with _db(request).execute(
        "SELECT id FROM sessions WHERE id = ?", (body.session_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return await db_module.log_task(
        _db(request),
        body.session_id,
        body.project_id,
        body.description,
        body.status,
        parent_task_id=body.parent_task_id,
    )


@app.patch("/tasks/{task_id}", response_model=Task)
async def patch_task(
    task_id: str, body: TaskUpdate, request: Request
) -> dict[str, Any]:
    """Update a task's status and/or description in place.

    Used by the dashboard to flip HITL tasks to done/failed when the
    human replies. 404 when the id is unknown, 422 on invalid status.
    """
    existing = await db_module.get_task(_db(request), task_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="task not found")
    try:
        updated = await db_module.update_task(
            _db(request),
            task_id,
            status=body.status,
            description=body.description,
            project_id=existing["project_id"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    assert updated is not None
    return updated


@app.delete("/projects/{project_id}/chat/history", status_code=204)
async def clear_chat_history(project_id: str, request: Request) -> None:
    """Delete all chat messages and session for a project."""
    database = _db(request)
    await database.execute("DELETE FROM chat_messages WHERE project_id = ?", (project_id,))
    await database.execute("DELETE FROM chat_sessions WHERE project_id = ?", (project_id,))
    await database.commit()


@app.get(
    "/projects/{project_id}/chat/history",
    response_model=list[ChatHistoryItem],
)
async def get_chat_history(
    project_id: str, request: Request, limit: int = 50
) -> list[dict[str, Any]]:
    """Return persisted chat messages for a project (oldest first).

    The dashboard calls this on tab open to restore conversation history
    across page refreshes. Returns an empty list when no messages exist yet.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.get_chat_history(_db(request), project_id, limit=limit)


@app.get("/projects/{project_id}/export/pdf")
async def export_project_pdf(project_id: str, request: Request):
    """Generate a tamper-evident IP attribution PDF for the project.

    Contains north star, version goal, sprint, full task log with
    timestamps and session names, and a SHA-256 hash of the content
    embedded in the footer.
    """
    import hashlib
    from fpdf import FPDF
    import io

    db = _db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    goal = await db_module.get_goal(db, project_id)
    tasks = await db_module.get_tasks(db, project_id, limit=200)
    sessions = await db_module.get_sessions(db, project_id, active_only=False)
    session_names = {s["id"]: s["name"] for s in sessions}

    # Build text content for hashing
    lines = [
        f"MERIDIAN IP ATTRIBUTION RECORD",
        f"Project: {project['name']} ({project['id']})",
        f"Generated: {__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}",
        "",
    ]
    if goal:
        lines += [
            f"Goal Version: {goal['version']}",
            f"North Star: {goal.get('north_star') or '(not set)'}",
            f"Version Goal: {goal['content']}",
            f"Sprint: {goal.get('sprint') or '(not set)'}",
            "",
        ]
    lines.append("TASK LOG:")
    for t in tasks:
        sname = session_names.get(t["session_id"], t["session_id"][:8])
        lines.append(f"[{t['created_at']}] [{t['status'].upper()}] {sname}: {t['description']}")

    full_text = "\n".join(lines)
    sha256 = hashlib.sha256(full_text.encode()).hexdigest()

    # Build PDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Meridian IP Attribution Record", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 7, f"Project: {project['name']}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    if goal:
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, "Goal Hierarchy", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        for label, val in [
            ("North Star", goal.get("north_star") or "(not set)"),
            ("Version Goal", str(goal["content"])),
            ("Sprint", goal.get("sprint") or "(not set)"),
        ]:
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(30, 6, f"{label}:", new_x="RIGHT", new_y="LAST")
            pdf.set_font("Helvetica", "", 9)
            # Multi-line safe: use multi_cell for value
            x, y = pdf.get_x(), pdf.get_y()
            pdf.multi_cell(0, 6, val[:300])
        pdf.ln(4)

    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, f"Task Log ({len(tasks)} entries)", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Courier", "", 7)
    for t in tasks:
        sname = session_names.get(t["session_id"], t["session_id"][:8])
        row = f"[{t['created_at']}] [{t['status'].upper()}] {sname}: {t['description']}"
        pdf.multi_cell(0, 5, row[:200])

    # Footer with SHA256
    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 7)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(0, 5, f"SHA-256: {sha256}")

    pdf_bytes = pdf.output()
    return Response(
        content=bytes(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{project["name"]}_ip_record.pdf"'
        },
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_html(request: Request) -> Any:
    """Serve the Meridian dashboard from a Jinja2 template."""
    if os.environ.get("DEMO_PASSWORD"):
        if not _check_demo_cookie(request):
            return HTMLResponse(_demo_gate_html())
    return _templates.TemplateResponse(
        request,
        "dashboard.html",
        {"version": _VERSION, "asset_version": _ASSET_VERSION, "demo_mode": False},
    )


@app.get("/demo", response_class=HTMLResponse)
async def demo_dashboard(request: Request) -> Any:
    """Public read-only demo dashboard backed by MERIDIAN_DEMO_DB_URL.

    Sets a short-lived cookie so subsequent API calls from this browser
    session are routed to the isolated demo DB and writes are blocked.
    Exempt from SITE_PASSWORD gate so the URL is always public.
    """
    response = _templates.TemplateResponse(
        request,
        "dashboard.html",
        {"version": _VERSION, "asset_version": _ASSET_VERSION, "demo_mode": True},
    )
    response.set_cookie(
        _DEMO_CONTEXT_COOKIE,
        "1",
        max_age=3600,
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/config/api-key")
async def api_key_status() -> dict:
    """Tell the dashboard which auth method is active.

    Returns ``{"configured": bool, "method": "oauth"|"api_key"|null}``.
    The token itself is never included in the response.
    """
    _, method = dashboard_module.get_auth_token()
    return {"configured": method is not None, "method": method}


@app.post("/dashboard/chat")
async def dashboard_chat(body: ChatRequest, request: Request):
    """Proxy a streaming Claude chat call as Server-Sent Events.

    The default backend (``mode="cli"``) spawns the ``claude`` CLI
    binary so the conversation draws from the user's Max-plan
    allowance via the OAuth token already on disk. Set ``mode="api"``
    in the request body to use the metered Anthropic API directly.

    Each text chunk is forwarded as a ``data: {"delta": "..."}`` line;
    the stream terminates with ``data: [DONE]``.

    Persistence: the user message is saved to ``chat_messages`` before
    streaming begins; the complete assistant reply is appended after the
    stream finishes. A ``chat_sessions`` row is created on first use so
    future CLI ``--resume`` support has a home for the session handle.
    """
    project = await db_module.get_project(_db(request), body.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    messages = [m.model_dump() for m in body.messages]
    # Inject model identity into system prompt so the model knows its version
    model_hint = f"You are {body.model} running in the Meridian dashboard."
    if body.system_prompt:
        body = body.model_copy(update={"system_prompt": model_hint + "\n\n" + body.system_prompt})
    else:
        body = body.model_copy(update={"system_prompt": model_hint})
    db = _db(request)
    project_id = body.project_id

    # Persist the user's message and ensure a chat session exists.
    if messages and messages[-1].get("role") == "user":
        await db_module.save_chat_message(db, project_id, "user", messages[-1]["content"])
    chat_session = await db_module.get_or_create_chat_session(db, project_id)

    # Select the streaming backend.
    if body.mode == "cli":
        # v0.4.1 — if the CLI session id is known from a previous turn
        # pass it as --resume so the conversation continues; capture
        # whatever id the CLI emits on this turn for the next one.
        resume_id = chat_session.get("cli_session_id") if chat_session else None

        async def _save_session_id(new_id: str) -> None:
            await db_module.update_chat_session_cli_id(db, project_id, new_id)

        raw_stream = dashboard_module.stream_claude_cli_chat(
            messages=messages,
            system_prompt=body.system_prompt,
            model=body.model,
            max_tokens=body.max_tokens,
            resume_session_id=resume_id,
            on_session_id=_save_session_id,
        )
    else:
        raw_stream = dashboard_module.stream_anthropic_chat(
            messages=messages,
            system_prompt=body.system_prompt,
            model=body.model,
            max_tokens=body.max_tokens,
        )

    async def _saving_stream():
        """Yield every SSE chunk and save the full assistant reply at the end."""
        acc: list[str] = []
        async for chunk in raw_stream:
            yield chunk
            # Each chunk is one complete SSE event: b"data: {...}\n\n" or b"data: [DONE]\n\n"
            if chunk != b"data: [DONE]\n\n" and chunk.startswith(b"data: "):
                try:
                    payload = json.loads(chunk[6:].decode("utf-8").strip())
                    if "delta" in payload:
                        acc.append(payload["delta"])
                except Exception:  # noqa: BLE001
                    pass
        full_text = "".join(acc).strip()
        if full_text:
            try:
                await db_module.save_chat_message(db, project_id, "assistant", full_text)
            except Exception:  # noqa: BLE001
                pass

    return StreamingResponse(_saving_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# File editing endpoints
# ---------------------------------------------------------------------------

# Repo root is the parent of this package directory (meridian/).
_REPO_ROOT = Path(__file__).parent.parent
# The dashboard only allows editing these specific files.
_EDITABLE_FILES: list[str] = ["AGENTS.md", "ROADMAP.md", "DEVLOG.md", "CLAUDE.md", "README.md", "DECISIONS.md"]


@app.get("/projects/{project_id}/files")
async def list_project_files(
    project_id: str, request: Request
) -> list[str]:
    """Return the list of editable markdown files for a project.

    Files that do not yet exist on disk are still listed so the user can
    create them from the dashboard. 403 is raised if the project is unknown.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return _EDITABLE_FILES


@app.get("/projects/{project_id}/files/{filename}")
async def get_project_file(
    project_id: str, filename: str, request: Request
) -> dict[str, str]:
    """Read one editable markdown file and return its content.

    Returns ``{"filename": ..., "content": ...}`` with an empty string when
    the file does not yet exist. 403 if the filename is not in the allow-list.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if filename not in _EDITABLE_FILES:
        raise HTTPException(status_code=403, detail="file not in allow-list")
    path = _REPO_ROOT / filename
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    from fastapi.responses import JSONResponse
    return JSONResponse(content={"filename": filename, "content": content}, headers={"Content-Type": "application/json; charset=utf-8"})


@app.put("/projects/{project_id}/files/{filename}")
async def put_project_file(
    project_id: str, filename: str, body: FileContent, request: Request
) -> dict[str, object]:
    """Write content to one editable markdown file.

    Creates the file if it does not exist. 403 if the filename is not in the
    allow-list. Returns ``{"filename": ..., "size": <bytes>}``.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if filename not in _EDITABLE_FILES:
        raise HTTPException(status_code=403, detail="file not in allow-list")
    path = _REPO_ROOT / filename
    path.write_text(body.content, encoding="utf-8")
    return {"filename": filename, "size": len(body.content.encode("utf-8"))}


def _render_context_block(
    project: dict,
    goal: dict | None,
    sprint_items: list[dict],
    pending_tasks: list[dict],
    sessions: list[dict],
    recent_decisions: list[str],
    *,
    mode: str = "full",
    repo_root: str | None = None,
) -> str:
    """v2.3 — render the project context as a single text block.

    ``mode='full'`` produces the Code Handoff variant — everything a fresh
    Claude Code session needs to continue work without re-deriving state.
    ``mode='chat'`` produces the shorter "new claude.ai conversation"
    variant — drops sessions and trims long fields so a paste into a new
    chat doesn't overflow the first message.

    Returns a plain-text block (no JSON, no markdown fences) suitable for
    direct clipboard copy + paste.
    """
    short_id = project["id"].split("-")[0]
    lines = [f"PROJECT: {project['name']} ({short_id})"]
    if goal:
        if goal.get("north_star"):
            ns = goal["north_star"]
            if mode == "chat" and len(ns) > 400:
                ns = ns[:400].rstrip() + "…"
            lines.append(f"NORTH STAR: {ns}")
        if goal.get("sprint"):
            lines.append(f"SPRINT: {goal['sprint']}")
        if mode == "full" and goal.get("content"):
            vg = goal["content"]
            if isinstance(vg, dict):
                vg = vg.get("content") or str(vg)
            if len(vg) > 2000:
                vg = vg[:2000].rstrip() + "…"
            lines += ["", "VERSION GOAL:", vg]
    if sprint_items:
        lines += ["", "PENDING SPRINT ITEMS:"]
        for it in sprint_items[:10]:
            lines.append(f"- [{it.get('status', '?')}] {it.get('title', '')}")
    if pending_tasks:
        cap = 10 if mode == "full" else 5
        lines += ["", f"RECENT TASKS (last {min(len(pending_tasks), cap)}):"]
        for t in pending_tasks[:cap]:
            stat = (t.get("status") or "?").upper()
            desc = t.get("description") or ""
            if len(desc) > 200:
                desc = desc[:200].rstrip() + "…"
            lines.append(f"- [{stat}] {desc}")
    if mode == "full" and sessions:
        lines += ["", "ACTIVE SESSIONS:"]
        for s in sessions[:5]:
            lines.append(f"- {s.get('name', '?')} ({s.get('status', '?')})")
    if recent_decisions:
        lines += ["", "RECENT DECISIONS:"]
        for d in recent_decisions[-5:]:
            text = d
            if len(text) > 240:
                text = text[:240].rstrip() + "…"
            lines.append(f"- {text}")
    if mode == "full":
        if repo_root:
            lines += ["", f"REPO: {repo_root}"]
        lines += ["TEST: pixi run test"]
    lines += [
        "",
        f"To continue: connect to Meridian and call start_session(project_id=\"{project['id']}\", session_name=\"<name>\").",
    ]
    return "\n".join(lines)


@app.get("/projects/{project_id}/context-block")
async def get_project_context_block(
    project_id: str, request: Request, mode: str = "full"
) -> Response:
    """v2.3 — plain-text context block suitable for direct clipboard paste.

    Query: ``?mode=full`` (default) or ``?mode=chat`` (shorter).
    Returns ``text/plain`` — the dashboard "Code Handoff" / "Copy chat
    context" buttons stream this straight into ``navigator.clipboard``.
    """
    if mode not in ("full", "chat"):
        raise HTTPException(status_code=400, detail="mode must be 'full' or 'chat'")
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    goal = await db_module.get_goal(_db(request), project_id)
    sprint_items = await db_module.get_sprint_items(
        _db(request), project_id, status="pending"
    )
    all_tasks = await db_module.get_tasks(_db(request), project_id, limit=20)
    pending_tasks = [
        t for t in all_tasks if t.get("status") in ("pending", "in_progress", "done")
    ][:10]
    sessions = await db_module.get_sessions(
        _db(request), project_id, active_only=True
    )
    decisions_raw = (project.get("decisions") or "").strip()
    recent_decisions = [
        l.strip() for l in decisions_raw.splitlines() if l.strip()
    ][-5:]
    text = _render_context_block(
        project, goal, sprint_items, pending_tasks, sessions, recent_decisions,
        mode=mode,
        repo_root=str(Path.cwd()) if mode == "full" else None,
    )
    return Response(content=text, media_type="text/plain; charset=utf-8")


@app.get("/projects/{project_id}/context")
async def get_project_context(
    project_id: str, request: Request
) -> dict[str, Any]:
    """Return a onboarding context payload for new chat sessions (v1.9.x).

    A new claude.ai chat can paste this JSON to get up to speed instantly.
    Returns: north_star, current_sprint, sprint_items (pending), recent_decisions
    (last 5), pending_tasks (last 10), recent_sessions (last 5), file_map.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    goal = await db_module.get_goal(_db(request), project_id)
    sprint_items = await db_module.get_sprint_items(
        _db(request), project_id, status="pending", show_blocked=False
    )
    all_tasks = await db_module.get_tasks(_db(request), project_id, limit=20)
    pending_tasks = [t for t in all_tasks if t["status"] in ("pending", "in_progress")][:10]
    sessions = await db_module.get_sessions(_db(request), project_id, active_only=True)
    decisions_raw = (project.get("decisions") or "").strip()
    recent_decisions = [l.strip() for l in decisions_raw.splitlines() if l.strip()][-5:]
    return {
        "project": {"id": project["id"], "name": project["name"]},
        "north_star": goal.get("north_star") if goal else None,
        "current_sprint": goal.get("sprint") if goal else None,
        "version_goal": goal.get("content") if goal else None,
        "sprint_items": sprint_items,
        "recent_decisions": recent_decisions,
        "pending_tasks": pending_tasks,
        "recent_sessions": sessions[:5],
        "file_map": list(_EDITABLE_FILES),
    }


# ---------------------------------------------------------------------------
# v2.4 — Pinned decisions (editable constitution)
# ---------------------------------------------------------------------------


@app.get("/projects/{project_id}/decisions-pinned")
async def list_pinned_decisions_endpoint(
    project_id: str, request: Request, include_superseded: bool = False
) -> list[dict[str, Any]]:
    """Active pinned decisions for a project (newest first).

    ``?include_superseded=true`` returns the full history. Default
    filters to ``status='active'`` so the dashboard renders just the
    live constitution.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.get_pinned_decisions(
        _db(request), project_id, include_superseded=include_superseded
    )


@app.post("/projects/{project_id}/decisions-pinned", status_code=201)
async def create_pinned_decision_endpoint(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Create a new pinned decision."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    title = (body.get("title") or "").strip()
    text = (body.get("body") or "").strip()
    category = body.get("category", "TECHNICAL")
    if not title or not text:
        raise HTTPException(status_code=400, detail="title and body required")
    try:
        return await db_module.pin_decision(
            _db(request), project_id, title, text, category
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/projects/{project_id}/decisions-pinned/{decision_id}")
async def update_pinned_decision_endpoint(
    project_id: str, decision_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Patch fields, or supersede (pass new_title + new_body to atomically retire+create)."""
    db = _db(request)
    new_title = body.get("new_title")
    new_body = body.get("new_body")
    if new_title and new_body:
        try:
            return await db_module.supersede_pinned_decision(
                db, decision_id, new_title, new_body, body.get("category")
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    result = await db_module.update_pinned_decision(
        db, decision_id,
        body=body.get("body"),
        title=body.get("title"),
        category=body.get("category"),
        status=body.get("status"),
        superseded_by=body.get("superseded_by"),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="decision not found")
    return result


# ---------------------------------------------------------------------------
# v2.4 — HITL (human-in-the-loop) queue
# ---------------------------------------------------------------------------


@app.get("/hitl")
async def list_all_hitl(
    request: Request, status: str = "pending", limit: int = 50
) -> list[dict[str, Any]]:
    """Pending HITL requests across all projects (top-level dashboard panel)."""
    try:
        return await db_module.list_hitl_requests(
            _db(request), None,
            status=status if status != "all" else None,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/projects/{project_id}/hitl")
async def list_project_hitl(
    project_id: str, request: Request, status: str = "pending", limit: int = 50
) -> list[dict[str, Any]]:
    """HITL requests scoped to a single project."""
    try:
        return await db_module.list_hitl_requests(
            _db(request), project_id,
            status=status if status != "all" else None,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/projects/{project_id}/hitl", status_code=201)
async def create_hitl_endpoint(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Create a HITL request. Sessions paused on blocking should POST then poll
    GET /hitl/{id} until status='answered'."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question required")
    try:
        return await db_module.request_hitl(
            _db(request), project_id, question,
            session_id=body.get("session_id"),
            context=body.get("context"),
            urgency=body.get("urgency", "normal"),
            assigned_to=body.get("assigned_to"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/hitl/{request_id}")
async def get_hitl_endpoint(request_id: str, request: Request) -> dict[str, Any]:
    """Single HITL request lookup — sessions poll this to get the answer."""
    r = await db_module.get_hitl_request(_db(request), request_id)
    if r is None:
        raise HTTPException(status_code=404, detail="hitl request not found")
    return r


@app.patch("/hitl/{request_id}")
async def patch_hitl_endpoint(
    request_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Answer or dismiss a HITL request."""
    db = _db(request)
    action = (body.get("action") or "answer").lower()
    if action == "answer":
        answer = body.get("answer", "").strip()
        if not answer:
            raise HTTPException(status_code=400, detail="answer required")
        result = await db_module.answer_hitl_request(
            db, request_id, answer, answered_by=body.get("answered_by")
        )
    elif action == "dismiss":
        result = await db_module.dismiss_hitl_request(db, request_id)
    else:
        raise HTTPException(status_code=400, detail="action must be 'answer' or 'dismiss'")
    if result is None:
        raise HTTPException(status_code=404, detail="hitl request not found")
    return result


# ---------------------------------------------------------------------------
# v0.9 — project_notes (per-project wiki)
# ---------------------------------------------------------------------------


@app.get("/projects/{project_id}/notes")
async def list_project_notes_endpoint(
    project_id: str, request: Request, tag: str | None = None
) -> list[dict[str, Any]]:
    """Project notes (newest first). ``?tag=X`` filters by substring match."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.get_project_notes(_db(request), project_id, tag=tag)


@app.post("/projects/{project_id}/notes", status_code=201)
async def create_project_note_endpoint(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Create a new note. Body: {title, body, tags?}."""
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    title = (body.get("title") or "").strip()
    text = (body.get("body") or "").strip()
    if not title or not text:
        raise HTTPException(status_code=400, detail="title and body required")
    return await db_module.add_project_note(
        _db(request), project_id, title, text, body.get("tags"),
    )


@app.patch("/projects/{project_id}/notes/{note_id}")
async def update_project_note_endpoint(
    project_id: str, note_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Patch title/body/tags."""
    result = await db_module.update_project_note(
        _db(request), note_id,
        title=body.get("title"),
        body=body.get("body"),
        tags=body.get("tags"),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="note not found")
    return result


@app.delete("/projects/{project_id}/notes/{note_id}", status_code=204)
async def delete_project_note_endpoint(
    project_id: str, note_id: str, request: Request
) -> Response:
    """Hard-delete a note. Returns 204 or 404."""
    ok = await db_module.delete_project_note(_db(request), note_id)
    if not ok:
        raise HTTPException(status_code=404, detail="note not found")
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# v2.4 — Team visibility (per-human swimlane + standup digest data)
# ---------------------------------------------------------------------------


@app.get("/team/summary")
async def get_team_summary_endpoint(
    request: Request, project_id: str | None = None, days: int = 1
) -> dict[str, Any]:
    """Aggregate task_log + sessions by human_id over the last N days.

    ``project_id`` optional — omit to roll up across all projects.
    Returns ``{period_days, humans:[...], active_count}``. Used by the
    Team tab cards, swimlane timeline, and standup digest.
    """
    return await db_module.get_team_summary(_db(request), project_id, days)


# ---------------------------------------------------------------------------
# v2.4 — Webhook intake for framework integrations
# ---------------------------------------------------------------------------


@app.post("/projects/{project_id}/events", status_code=201)
async def post_project_event(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Normalize a framework event into Meridian's task_log.

    Auth: ``X-Meridian-Token: <project_token>`` header.  The token grants
    write-access to a single project's task_log only — call
    ``ensure_project_token`` to mint one (returned by the dashboard's
    Project settings panel, by GET /projects/{id}/webhook-token).

    Body schema (all optional except description+session_name):
    ```
    {
      "type": "task_completed" | "checkpoint" | "hitl_request" | "session_start",
      "session_name": "langgraph-researcher",
      "human_id": "langgraph",
      "agent_framework": "langgraph",
      "description": "researcher agent fetched 3 sources",
      "status": "done",
      "parent_task_id": null,
      "metadata": {}
    }
    ```
    """
    db = _db(request)
    auth_token = request.headers.get("X-Meridian-Token", "")
    project_by_token = await db_module.get_project_by_token(db, auth_token) if auth_token else None
    if project_by_token is None or project_by_token["id"] != project_id:
        raise HTTPException(status_code=401, detail="invalid or missing X-Meridian-Token")

    event_type = body.get("type") or "task_completed"
    description = (body.get("description") or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="description required")
    session_name = body.get("session_name") or f"webhook/{body.get('agent_framework', 'custom')}"
    human_id = body.get("human_id")
    framework = body.get("agent_framework") or "custom"
    status = body.get("status") or "done"

    # Find-or-create a session for this framework/human/name combo so
    # bursty webhook traffic doesn't create one session per event.
    sessions = await db_module.get_sessions(db, project_id, active_only=False)
    target = next(
        (s for s in sessions if s.get("name") == session_name and s.get("agent_framework") == framework),
        None,
    )
    if target is None:
        target = await db_module.register_session(
            db, project_id, session_name,
            human_id=human_id, agent_framework=framework,
        )

    if event_type == "hitl_request":
        return await db_module.request_hitl(
            db, project_id, description,
            session_id=target["id"], context=body.get("context"),
            urgency=body.get("urgency", "normal"),
            assigned_to=body.get("assigned_to"),
        )

    task = await db_module.log_task(
        db, target["id"], project_id, description,
        status=status, parent_task_id=body.get("parent_task_id"),
    )
    return {"task": task, "session_id": target["id"], "event_type": event_type}


@app.get("/projects/{project_id}/webhook-token")
async def get_project_webhook_token(
    project_id: str, request: Request
) -> dict[str, Any]:
    """Mint-and-return the project webhook token. Shown ONCE in the UI."""
    token = await db_module.ensure_project_token(_db(request), project_id)
    if token is None:
        raise HTTPException(status_code=404, detail="project not found")
    return {"project_id": project_id, "token": token}


# ---------------------------------------------------------------------------
# CLAUDE.md auto-update helper
# ---------------------------------------------------------------------------

_CLAUDE_MD_STATE_MARKER = "\n\n---\n<!-- MERIDIAN STATE — auto-generated, do not edit below -->\n"


async def _regenerate_claude_md(
    db: aiosqlite.Connection,
    project_id: str,
    repo_root: Path,
) -> None:
    """Append/replace the MERIDIAN STATE section at the bottom of CLAUDE.md.

    The human-written content above the marker is preserved exactly.  Only
    the section below the marker is overwritten.  If CLAUDE.md does not yet
    have the marker it is appended.  Silently no-ops on any I/O error so a
    failing write never blocks the caller.
    """
    try:
        from datetime import datetime, timezone
        goal = await db_module.get_goal(db, project_id)
        sprint_items = await db_module.get_sprint_items(db, project_id, status="pending")
        project = await db_module.get_project(db, project_id)
        all_tasks = await db_module.get_tasks(db, project_id, limit=20)
        pending_tasks = [t for t in all_tasks if t["status"] in ("pending", "in_progress")][:5]
        decisions_raw = ((project or {}).get("decisions") or "").strip()
        recent_decisions = [l.strip() for l in decisions_raw.splitlines() if l.strip()][-5:]

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [f"## Current Sprint State  _(auto-updated {now})_", ""]
        if goal:
            if goal.get("north_star"):
                lines += [f"**North Star:** {goal['north_star']}", ""]
            if goal.get("sprint"):
                lines += [f"**Sprint:** {goal['sprint']}", ""]
        if sprint_items:
            lines.append("**Pending Sprint Items:**")
            for item in sprint_items[:10]:
                lines.append(f"- [ ] {item['title']}")
            lines.append("")
        if recent_decisions:
            lines.append("**Recent Decisions:**")
            for d in recent_decisions:
                lines.append(f"- {d}")
            lines.append("")
        if pending_tasks:
            lines.append("**Pending Tasks:**")
            for t in pending_tasks:
                lines.append(f"- [{t['status'].upper()}] {t['description'][:120]}")
            lines.append("")
        lines += [
            "**Key Files:**",
            "- `meridian/server.py` — FastAPI app + MCP handlers",
            "- `meridian/db.py` — all DB functions (SQLite + Postgres)",
            "- `meridian/static/dashboard.js` — dashboard UI",
            "- `tests/test_core.py` — full test suite",
            "- `data/meridian-build_handoff.md` — session handoff",
            "",
        ]
        new_state_section = _CLAUDE_MD_STATE_MARKER + "\n".join(lines)

        claude_md_path = repo_root / "CLAUDE.md"
        existing = claude_md_path.read_text(encoding="utf-8") if claude_md_path.exists() else ""
        if _CLAUDE_MD_STATE_MARKER.strip() in existing:
            base = existing.split(_CLAUDE_MD_STATE_MARKER.strip())[0].rstrip()
            updated = base + new_state_section
        else:
            updated = existing.rstrip() + new_state_section
        claude_md_path.write_text(updated, encoding="utf-8")
    except Exception:
        pass  # never block the caller on a write failure


# v2.4 — auto-refresh the <current_state>...</current_state> block. This
# is the dev-facing target the new CLAUDE.md layout uses (see CLAUDE.md
# top). Pre-v2.4 the bottom MERIDIAN STATE section was the only target;
# both are now kept in sync — the bottom section stays for backward compat
# with sessions reading the legacy marker.
import re as _re_module


async def _refresh_claude_md_current_state(
    db: aiosqlite.Connection,
    repo_root: Path,
) -> None:
    """Refresh the ``<current_state>...</current_state>`` block in CLAUDE.md.

    The default project is the most-recently-updated one. Body lines:
    sprint, last updated timestamp, and the 5 most recent task
    descriptions. The block content is regenerated from scratch every
    call; surrounding markdown stays untouched.

    Silently no-ops on any I/O error (file missing, permission denied,
    no projects) so a failing write never blocks the auto-summary loop.
    """
    try:
        from datetime import datetime, timezone
        claude_md_path = repo_root / "CLAUDE.md"
        if not claude_md_path.exists():
            return
        existing = claude_md_path.read_text(encoding="utf-8")
        if "<current_state>" not in existing or "</current_state>" not in existing:
            return
        projects = await db_module.list_projects(db)
        if not projects:
            return
        # Pick the project with the most recent task — that's the one
        # the human is actively working on.
        primary = projects[0]
        try:
            best = None
            best_ts = ""
            for p in projects:
                tasks = await db_module.get_tasks(db, p["id"], limit=1)
                if tasks and tasks[0]["created_at"] > best_ts:
                    best_ts = tasks[0]["created_at"]
                    best = p
            if best is not None:
                primary = best
        except Exception:  # noqa: BLE001
            pass

        goal = await db_module.get_goal(db, primary["id"])
        tasks = await db_module.get_tasks(db, primary["id"], limit=5)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        body_lines = [
            "<!-- Auto-updated by Meridian. Do not edit manually. -->",
            f"Project: {primary['name']} ({primary['id'][:8]})",
            f"Last updated: {now}",
        ]
        if goal:
            if goal.get("sprint"):
                body_lines.append(f"Sprint: {goal['sprint']}")
            if goal.get("north_star"):
                ns = goal["north_star"][:200].replace("\n", " ")
                body_lines.append(f"North Star: {ns}{'…' if len(goal['north_star']) > 200 else ''}")
        if tasks:
            body_lines.append("Recent:")
            for t in tasks:
                desc = (t.get("description") or "").replace("\n", " ")[:120]
                status = (t.get("status") or "?").upper()
                body_lines.append(f"  - [{status}] {desc}")

        new_block = "<current_state>\n" + "\n".join(body_lines) + "\n</current_state>"
        updated = _re_module.sub(
            r"<current_state>[\s\S]*?</current_state>",
            new_block,
            existing,
            count=1,
        )
        if updated != existing:
            claude_md_path.write_text(updated, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


_ROADMAP_AUTO_COMMENT = "<!-- meridian-auto: {version} -->"


async def _update_roadmap_version_history(
    db: aiosqlite.Connection,
    project_id: str,
    version: str,
    repo_root: Path,
) -> None:
    """Insert or replace a version-history bullet in ROADMAP.md.

    Reads all *done* sprint items for *version*, builds a one-line summary,
    and writes it into the ``## Version history`` section.  Each entry is
    tagged with an HTML comment so it can be found on the next update.
    Silently no-ops on any I/O or DB error.
    """
    try:
        items = await db_module.get_sprint_items(db, project_id)
        done = [i for i in items if i.get("version") == version and i.get("status") == "done"]
        if not done:
            return

        roadmap_path = repo_root / "ROADMAP.md"
        content = roadmap_path.read_text(encoding="utf-8") if roadmap_path.exists() else ""

        titles = "; ".join(i["title"] for i in done[:5])
        if len(done) > 5:
            titles += f"; +{len(done) - 5} more"
        marker = _ROADMAP_AUTO_COMMENT.format(version=version)
        new_line = f"- **{version}** — {titles} {marker}"

        lines = content.splitlines()
        in_history = False
        marker_idx: int | None = None
        history_end_idx: int | None = None

        for i, line in enumerate(lines):
            if line.strip() == "## Version history":
                in_history = True
            elif in_history and marker in line:
                marker_idx = i
                break
            elif in_history and line.startswith("---"):
                history_end_idx = i
                break

        if marker_idx is not None:
            lines[marker_idx] = new_line
        elif history_end_idx is not None:
            lines.insert(history_end_idx, new_line)
        else:
            content = content.rstrip() + "\n" + new_line + "\n"
            roadmap_path.write_text(content, encoding="utf-8")
            return

        roadmap_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass  # never block the caller on a write failure


@app.websocket("/ws/{project_id}")
async def ws_project(ws: WebSocket, project_id: str) -> None:
    """Push task-log events to dashboard clients for one project."""
    broadcaster: dashboard_module.WebSocketBroadcaster = (
        ws.app.state.ws_broadcaster
    )
    await broadcaster.serve(ws, project_id)




@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page() -> HTMLResponse:
    """Admin password gate for the /admin panel."""
    html = """<!DOCTYPE html><html><head><meta charset=utf-8>
<title>Admin login — Meridian</title>
<style>body{font-family:system-ui,sans-serif;background:#0d1117;color:#e8eaed;display:grid;place-items:center;min-height:100vh;margin:0}
form{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:32px;width:320px}
h2{margin:0 0 20px;font-size:1.1rem}
input{width:100%;padding:10px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#e8eaed;font-size:.95rem;box-sizing:border-box;margin-bottom:12px}
button{width:100%;padding:10px;background:#6c8fff;color:#fff;border:none;border-radius:4px;font-weight:700;cursor:pointer;font-size:.95rem}
</style></head><body>
<form method="post" action="/admin/login">
<h2>⬡ Admin access</h2>
<input type="password" name="password" placeholder="Admin password" autofocus autocomplete="current-password">
<button type="submit">Enter</button>
</form></body></html>"""
    return HTMLResponse(html)


@app.post("/admin/login")
async def admin_login_post(request: Request) -> Any:
    """Validate admin password and set signed cookie."""
    from fastapi.responses import RedirectResponse
    form = await request.form()
    password = str(form.get("password", ""))
    expected = os.environ.get("MERIDIAN_ADMIN_PASSWORD", "")
    if not expected:
        return RedirectResponse("/admin", status_code=303)
    import secrets
    if not secrets.compare_digest(password, expected):
        return HTMLResponse("""<!DOCTYPE html><html><head><meta charset=utf-8>
<title>Admin login — Meridian</title>
<style>body{font-family:system-ui,sans-serif;background:#0d1117;color:#e8eaed;display:grid;place-items:center;min-height:100vh;margin:0}
form{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:32px;width:320px}
h2{margin:0 0 8px;font-size:1.1rem}p.err{color:#f85149;margin:0 0 12px;font-size:.9rem}
input{width:100%;padding:10px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#e8eaed;font-size:.95rem;box-sizing:border-box;margin-bottom:12px}
button{width:100%;padding:10px;background:#6c8fff;color:#fff;border:none;border-radius:4px;font-weight:700;cursor:pointer;font-size:.95rem}
</style></head><body><form method="post" action="/admin/login">
<h2>⬡ Admin access</h2><p class="err">Incorrect password</p>
<input type="password" name="password" placeholder="Admin password" autofocus>
<button type="submit">Enter</button></form></body></html>""", status_code=401)
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        "meridian_admin", expected,
        httponly=True, samesite="strict",
        secure=os.environ.get("MERIDIAN_SERVER_URL", "").startswith("https://"),
    )
    return response


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request) -> Any:
    """Admin dashboard — restricted to MERIDIAN_ADMIN_EMAILS + optional password."""
    from .hosted import get_current_tenant, is_admin, check_admin_password

    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        return HTMLResponse(
            '<meta http-equiv="refresh" content="0;url=/auth/login">',
            status_code=302,
        )

    if not is_admin(tenant.get("email", "")):
        raise HTTPException(status_code=403, detail="admin access only")
    if not check_admin_password(request):
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/admin/login", status_code=302)

    db = request.app.state.db

    def _r(row: Any) -> dict[str, Any]:
        """Convert an aiosqlite.Row or dict row to a plain dict."""
        if row is None:
            return {}
        if isinstance(row, dict):
            return row
        return {k: row[k] for k in row.keys()}

    # Gather stats
    async with db.execute(
        "SELECT COUNT(*) as n FROM tenants WHERE plan='pro'"
    ) as cur:
        row = await cur.fetchone()
        active_count = _r(row).get("n", 0) if row else 0

    async with db.execute(
        "SELECT COUNT(*) as n FROM tenants WHERE plan='free'"
    ) as cur:
        row = await cur.fetchone()
        free_count = _r(row).get("n", 0) if row else 0

    async with db.execute(
        "SELECT email, plan, created_at, neon_project_id FROM tenants ORDER BY created_at DESC LIMIT 20"
    ) as cur:
        rows = await cur.fetchall()
        recent_tenants = [_r(r) for r in rows] if rows else []

    rows_html = "".join(
        f"<tr><td>{t['email']}</td><td>{t['plan']}</td><td>{t['created_at'][:10]}</td>"
        f"<td>{'✓' if t['neon_project_id'] else '—'}</td></tr>"
        for t in recent_tenants
    )

    html = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Meridian Admin</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d0d0f;color:#e8eaf0;font-family:-apple-system,sans-serif;padding:2rem}}
.wrap{{max-width:900px;margin:0 auto}}
h1{{font-size:1.5rem;font-weight:800;margin-bottom:1.5rem}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1rem;margin-bottom:2rem}}
.stat{{background:#16181c;border:1px solid #2a2d35;border-radius:8px;padding:1.2rem;text-align:center}}
.stat .n{{font-size:2rem;font-weight:800;color:#6c8fff}}
.stat .l{{font-size:.8rem;color:#8b8fa8;margin-top:.3rem}}
table{{width:100%;border-collapse:collapse;background:#16181c;border-radius:8px;overflow:hidden}}
th{{background:#0d0d0f;color:#8b8fa8;font-size:.8rem;padding:.6rem 1rem;text-align:left}}
td{{padding:.6rem 1rem;border-top:1px solid #2a2d35;font-size:.85rem;color:#e8eaf0}}
a{{color:#6c8fff}}
</style>
</head>
<body>
<div class="wrap">
<h1>&#x1f9ed; Meridian Admin <small style="font-size:.7em;color:#8b8fa8">{tenant['email']}</small></h1>
<div class="stats">
  <div class="stat"><div class="n">{active_count}</div><div class="l">Pro tenants</div></div>
  <div class="stat"><div class="n">{free_count}</div><div class="l">Free tenants</div></div>
  <div class="stat"><div class="n">{active_count + free_count}</div><div class="l">Total accounts</div></div>
  <div class="stat"><div class="n">1000</div><div class="l">Neon project cap</div></div>
</div>
<h2 style="margin-bottom:.8rem;font-size:1rem;color:#8b8fa8">Recent accounts (last 20)</h2>
<table>
<thead><tr><th>Email</th><th>Plan</th><th>Joined</th><th>Neon</th></tr></thead>
<tbody>{rows_html}</tbody>
</table>
<p style="margin-top:1.5rem;font-size:.8rem;color:#8b8fa8">
  <a href="/">← Home</a> &nbsp;·&nbsp; <a href="/auth/logout">Sign out</a>
</p>
</div>
</body>
</html>"""
    return HTMLResponse(html)


@app.patch("/settings/notifications")
async def update_notification_prefs(request: Request) -> dict[str, Any]:
    """v2.5 — save email notification preferences for the authenticated tenant."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import get_current_tenant
    tenant = await get_current_tenant(request)
    body = await request.json()
    allowed_prefs = {"hitl", "stalled", "storage", "sprint"}
    prefs = {k: bool(v) for k, v in body.items() if k in allowed_prefs}
    await db_module.update_tenant(
        _db(request), tenant["id"], notification_prefs=json.dumps(prefs)
    )
    return {"status": "ok", "prefs": prefs}


@app.get("/settings/notifications")
async def get_notification_prefs(request: Request) -> dict[str, Any]:
    """v2.5 — return current notification preferences for the authenticated tenant."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import get_current_tenant
    tenant = await get_current_tenant(request)
    prefs_raw = tenant.get("notification_prefs") or "{}"
    if isinstance(prefs_raw, dict):
        prefs = prefs_raw
    else:
        try:
            prefs = json.loads(prefs_raw)
        except Exception:  # noqa: BLE001
            prefs = {}
    return {"prefs": prefs}


_WORKSPACE_MEMBER_LIMITS: dict[str, int] = {"standard": 25, "pro": 50}


@app.post("/workspace/invite", status_code=201)
async def workspace_invite(request: Request) -> dict[str, Any]:
    """Invite a new workspace member. Sends invite email via Resend."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import get_current_tenant, send_invite_email
    import hashlib, secrets as _secrets
    tenant = await get_current_tenant(request)
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    role = (body.get("role") or "member").strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="valid email required")
    if role not in ("member", "viewer"):
        raise HTTPException(status_code=422, detail="role must be member or viewer")
    db = _db(request)
    limit = _WORKSPACE_MEMBER_LIMITS.get(tenant.get("plan", "standard"), 25)
    count = await db_module.count_workspace_members(db, tenant["id"])
    if count >= limit:
        raise HTTPException(status_code=402, detail=f"Team member limit ({limit}) reached for your plan")
    raw_token = _secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    invite = await db_module.create_workspace_invite(db, tenant["id"], email, role, token_hash)
    base = os.environ.get("MERIDIAN_SERVER_URL", "https://usemeridian.us").rstrip("/")
    invite_url = f"{base}/workspace/accept?token={raw_token}"
    try:
        await send_invite_email(email, invite_url, tenant["email"])
    except Exception:
        pass  # email failure doesn't block invite creation
    return {"id": invite["id"], "email": email, "role": role, "pending": True}


@app.get("/workspace/accept")
async def workspace_accept(request: Request, token: str = "") -> HTMLResponse:
    """Accept a workspace invite. Marks joined_at and redirects to dashboard."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    if not token:
        return HTMLResponse("<h2>Invalid invite link.</h2><p><a href='/auth/login'>Sign in</a></p>", status_code=400)
    import hashlib
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db = _db(request)
    invite = await db_module.get_workspace_invite_by_token_hash(db, token_hash)
    if not invite:
        return HTMLResponse(
            "<h2>Invite not found or already used.</h2><p><a href='/auth/login'>Sign in to Meridian</a></p>",
            status_code=404,
        )
    await db_module.accept_workspace_invite(db, invite["id"])
    # Ensure a tenant account exists for the invited email
    await db_module.upsert_tenant(db, email=invite["email"])
    from fastapi.responses import RedirectResponse as _Redir
    return _Redir("/auth/login", status_code=302)


@app.get("/workspace/members")
async def workspace_list_members(request: Request) -> list[dict[str, Any]]:
    """List all workspace members (pending and accepted) for the current tenant."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import get_current_tenant
    tenant = await get_current_tenant(request)
    members = await db_module.list_workspace_members(_db(request), tenant["id"])
    return [
        {
            "id": m["id"],
            "email": m["email"],
            "role": m["role"],
            "joined_at": m.get("joined_at"),
            "pending": m.get("joined_at") is None,
        }
        for m in members
    ]


@app.delete("/workspace/members/{member_id}", status_code=204)
async def workspace_remove_member(request: Request, member_id: str) -> None:
    """Remove a workspace member or revoke a pending invite."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import get_current_tenant
    tenant = await get_current_tenant(request)
    await db_module.delete_workspace_member(_db(request), member_id, tenant["id"])


def _is_demo_request(request: Request) -> bool:
    """Return True when the request is in demo mode (env flag or cookie)."""
    env_demo = os.environ.get("MERIDIAN_DEMO", "").lower() in ("1", "true", "yes")
    cookie_demo = bool(request.cookies.get(_DEMO_CONTEXT_COOKIE))
    return env_demo or cookie_demo


@app.get("/export/my-data")
async def export_my_data(request: Request) -> Response:
    """GDPR data portability — returns a JSON file of all account data."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    if _is_demo_request(request):
        return JSONResponse(
            {"detail": "Not available in demo mode. Sign up at usemeridian.us"},
            status_code=403,
        )
    from .hosted import get_current_tenant
    tenant = await get_current_tenant(request)
    data = await db_module.export_tenant_data(_db(request), tenant["id"])
    payload = json.dumps(data, indent=2, default=str).encode()
    email_slug = (tenant.get("email") or "user").split("@")[0][:20]
    filename = f"meridian-export-{email_slug}.json"
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/account/delete")
async def delete_account(request: Request) -> Response:
    """Self-service account deletion. Requires JSON body: {\"confirmation\": \"DELETE\"}."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    if _is_demo_request(request):
        return JSONResponse(
            {"detail": "Not available in demo mode. Sign up at usemeridian.us"},
            status_code=403,
        )
    from .hosted import get_current_tenant, cancel_stripe_subscription, _drop_tenant_neon_database, send_account_deleted_email
    from fastapi.responses import JSONResponse
    tenant = await get_current_tenant(request)
    body = await request.json()
    if body.get("confirmation") != "DELETE":
        raise HTTPException(status_code=400, detail="Type DELETE to confirm account deletion.")

    stripe_id = tenant.get("stripe_customer_id")
    if stripe_id:
        await cancel_stripe_subscription(stripe_id)

    if tenant.get("neon_project_id"):
        asyncio.create_task(_drop_tenant_neon_database(tenant))

    email = tenant.get("email", "")
    await db_module.delete_tenant_records(_db(request), tenant["id"])

    if email:
        asyncio.create_task(send_account_deleted_email(email))

    resp = JSONResponse({"deleted": True})
    resp.delete_cookie("meridian_session")
    return resp


@app.get("/settings/mcp-config")
async def get_mcp_config(request: Request) -> dict[str, Any]:
    """Return project list + base URL for building the MCP client config snippet."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import get_current_tenant
    tenant = await get_current_tenant(request)
    projects = await db_module.list_projects(_db(request))
    return {
        "projects": [{"id": p["id"], "name": p["name"]} for p in projects],
        "base_url": os.environ.get("MERIDIAN_SERVER_URL", "https://usemeridian.us"),
        "tenant_id": tenant["id"],
    }


@app.get("/settings/usage")
async def get_usage_settings(request: Request) -> dict[str, Any]:
    """Return current compute + storage usage and overage caps for the tenant."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import get_current_tenant, PLAN_LIMITS, COMPUTE_OVERAGE_RATE, STORAGE_OVERAGE_RATE
    tenant = await get_current_tenant(request)
    plan = tenant.get("plan") or "standard"
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
    return {
        "plan": plan,
        "compute": {
            "used": float(tenant.get("compute_cu_hours_used") or 0),
            "limit": limits["cu_hours"],
            "grace": limits["cu_hours"] + limits["grace_cu_hours"],
            "cap_usd": float(tenant.get("compute_overage_cap_usd") or 0),
            "throttled": bool(tenant.get("compute_throttled_at")),
            "rate": COMPUTE_OVERAGE_RATE,
        },
        "storage": {
            "used_gb": float(tenant.get("storage_gb_used") or 0),
            "limit_gb": limits["storage_gb"],
            "cap_usd": float(tenant.get("storage_overage_cap_usd") or 0),
            "rate": STORAGE_OVERAGE_RATE,
        },
    }


@app.patch("/settings/usage")
async def update_usage_caps(request: Request) -> dict[str, Any]:
    """Update compute and storage overage spending caps for the tenant."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import get_current_tenant
    tenant = await get_current_tenant(request)
    body = await request.json()
    compute_cap = float(body.get("compute_cap") or 0)
    storage_cap = float(body.get("storage_cap") or 0)
    await db_module.update_tenant(
        _db(request), tenant["id"],
        compute_overage_cap_usd=compute_cap,
        storage_overage_cap_usd=storage_cap,
    )
    return {"status": "ok", "compute_cap": compute_cap, "storage_cap": storage_cap}


@app.get("/mcp/tools-doc", response_class=PlainTextResponse)
async def mcp_tools_doc() -> str:
    """Generate markdown MCP tool reference from the live tool list."""
    lines = ["# MCP Tool Reference\n", f"_Auto-generated. {len(_MCP_TOOLS_LIST)} tools._\n"]
    for tool in _MCP_TOOLS_LIST:
        lines.append(f"\n## `{tool['name']}`\n")
        lines.append(f"{tool.get('description', '')}\n")
        props = (tool.get("inputSchema") or {}).get("properties") or {}
        required = set((tool.get("inputSchema") or {}).get("required") or [])
        if props:
            lines.append("\n| Parameter | Type | Required | Description |")
            lines.append("|-----------|------|----------|-------------|")
            for k, v in props.items():
                req = "required" if k in required else "optional"
                desc = (v.get("description") or "").replace("|", "\\|")
                lines.append(f"| `{k}` | {v.get('type', 'string')} | {req} | {desc} |")
        example = TOOL_EXAMPLES.get(tool["name"])
        if example:
            lines.append(f"\n**Example:**\n```\n{example}\n```")
        lines.append("")
    return "\n".join(lines)


@app.get("/admin/health")
async def admin_health_json(request: Request) -> dict[str, Any]:
    """JSON health check for ops/curl — restricted to MERIDIAN_ADMIN_EMAILS."""
    from .hosted import get_current_tenant, is_admin, check_admin_password
    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        raise HTTPException(status_code=403, detail="not authenticated")
    if not is_admin(tenant.get("email", "")):
        raise HTTPException(status_code=403, detail="admin only")
    if not check_admin_password(request):
        raise HTTPException(status_code=403, detail="admin password required")

    db = request.app.state.db

    async def _count(sql: str) -> int:
        async with db.execute(sql) as cur:
            row = await cur.fetchone()
        return (row[0] if row else 0) or 0

    tenants_total = await _count("SELECT COUNT(*) FROM tenants")
    tenants_pro = await _count("SELECT COUNT(*) FROM tenants WHERE plan='pro'")
    tasks_today = await _count(
        "SELECT COUNT(*) FROM task_log WHERE created_at >= date('now')"
    )
    sessions_active = await _count(
        "SELECT COUNT(*) FROM sessions WHERE status='in_progress'"
    )
    sprint_pending = await _count(
        "SELECT COUNT(*) FROM sprint_items WHERE status='pending'"
    )

    try:
        version_path = _REPO_ROOT / "pyproject.toml"
        ver_text = version_path.read_text(encoding="utf-8")
        import re as _re
        ver_m = _re.search(r'version\s*=\s*"([^"]+)"', ver_text)
        version = ver_m.group(1) if ver_m else "unknown"
    except Exception:  # noqa: BLE001
        version = "unknown"

    return {
        "version": version,
        "tenants_total": tenants_total,
        "tenants_pro": tenants_pro,
        "sessions_active": sessions_active,
        "tasks_today": tasks_today,
        "sprint_pending": sprint_pending,
        "hosted_mode": _hosted_mode(),
    }


@app.get("/admin/git-status")
async def git_status() -> dict[str, Any]:
    """Check if local repo is behind/ahead of remote."""
    import subprocess as sp
    try:
        cwd = str(Path(__file__).parent.parent)
        # Fetch without merging
        sp.run(["git", "fetch", "origin"], cwd=cwd, capture_output=True, timeout=10)
        # Count commits behind/ahead
        result = sp.run(
            ["git", "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
            cwd=cwd, capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split()
            ahead = int(parts[0]) if parts else 0
            behind = int(parts[1]) if len(parts) > 1 else 0
        else:
            ahead, behind = 0, 0
        # Get current branch
        branch = sp.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, capture_output=True, text=True
        ).stdout.strip()
        # Get latest local + remote commit hashes
        local_hash = sp.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd, capture_output=True, text=True
        ).stdout.strip()
        remote_hash = sp.run(
            ["git", "rev-parse", "--short", "@{upstream}"],
            cwd=cwd, capture_output=True, text=True
        ).stdout.strip()
        return {
            "ok": True, "branch": branch,
            "ahead": ahead, "behind": behind,
            "local_hash": local_hash, "remote_hash": remote_hash,
            "up_to_date": behind == 0,
            "warning": f"{behind} commit(s) behind origin/{branch}" if behind > 0 else None,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "behind": 0, "ahead": 0}


@app.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: str, request: Request) -> None:
    """Delete a single task_log entry by ID. Permanent, no undo."""
    db = _db(request)
    await db.execute("DELETE FROM task_log WHERE id = ?", (task_id,))
    await db.commit()

@app.post("/tasks/enqueue", response_model=Task, status_code=202)
async def enqueue_task(body: EnqueueTask, request: Request) -> dict[str, Any]:
    """Paid-tier: queue a Claude subprocess and return the pending task row.

    Responds with 202 Accepted so clients can distinguish this from a
    synchronous task creation. The worker runs in the background; poll
    ``GET /projects/{id}/tasks`` to see the result land.
    """
    project = await db_module.get_project(_db(request), body.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    async with _db(request).execute(
        "SELECT id FROM sessions WHERE id = ?", (body.session_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return await enqueue_module.enqueue_claude_task(
        _db(request),
        body.session_id,
        body.project_id,
        body.prompt,
        timeout=body.timeout,
    )


# ---------------------------------------------------------------------------
# v0.4.5 — expire sessions and auto-generate handoffs
# ---------------------------------------------------------------------------


async def _expire_and_generate_handoffs(
    db: aiosqlite.Connection, data_dir: str
) -> dict[str, Any]:
    """Expire stale sessions and regenerate handoffs for affected projects.

    Returns ``{"count": n, "auto_handoff_generated": bool}``. Handoff
    generation failures are swallowed so a bad project never blocks the
    sessions endpoint from returning.
    """
    result = await db_module.expire_idle_sessions(db)
    generated = False
    import os as _os
    _skip = not _os.environ.get("ANTHROPIC_API_KEY")
    for pid in result["project_ids"]:
        try:
            # v2.4 — auto-generated handoffs from the idle-expire loop
            # skip the Haiku ai_summary unless the key is set.
            await handoff_module.generate_handoff(
                db, pid, data_dir, skip_ai_summary=_skip
            )
            generated = True
        except Exception:  # noqa: BLE001
            pass
        try:
            await _regenerate_claude_md(db, pid, _REPO_ROOT)
        except Exception:  # noqa: BLE001
            pass
    return {"count": result["count"], "auto_handoff_generated": generated}


# ---------------------------------------------------------------------------
# v0.4.4 — start_session composite helper + endpoint
# ---------------------------------------------------------------------------


async def _start_session_composite(
    db: aiosqlite.Connection,
    project_id: str,
    session_name: str,
    data_dir: str,
    human_id: str | None = None,
) -> dict[str, Any]:
    """Register + goal + tasks + sessions + handoff-check in one shot.

    Replaces the four-call cold-start sequence (register_session, get_goal,
    get_tasks, check handoff file) with a single call that returns everything
    a new session needs before touching anything.
    """
    # v1.8.x — archive sessions silent for 7+ days so they don't crowd
    # the active list seen by new sessions.
    await db_module.archive_stale_sessions(db, project_id)

    session = await db_module.register_session(
        db, project_id, session_name, human_id=human_id
    )

    goal = await db_module.get_goal(db, project_id)
    if goal is not None:
        recent_5 = await db_module.get_tasks(db, project_id, limit=5)
        goal["ambient_tasks"] = [
            {
                "status": t["status"],
                "description": t["description"],
                "created_at": t["created_at"],
            }
            for t in recent_5
        ]

    recent_tasks = await db_module.get_tasks(db, project_id, limit=10)

    await _expire_and_generate_handoffs(db, data_dir)
    active_sessions = await db_module.get_sessions(db, project_id, active_only=True)

    project = await db_module.get_project(db, project_id)
    project_name = project["name"] if project else project_id
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", project_name).strip("-") or "project"
    handoff_path_str = str(Path(data_dir) / f"{slug}_handoff.md")
    handoff_exists = Path(handoff_path_str).exists()

    # v0.6.1 — stamp the XML envelope onto the goal so MCP consumers
    # get a single ready-to-prompt block. Always present (even when
    # goal is None) so the contract is uniform.
    ambient_for_xml = goal.get("ambient_tasks") if goal else []
    # v1.1.3 — coherence warning + per-field ages.
    field_ages = await db_module.get_goal_field_ages(db, project_id)
    coherence = db_module.compute_coherence_warning(field_ages)
    # v1.1.4 — append-only decisions log (skipped for worker sessions
    # since v1.2.0's start_worker_session builds its own slim
    # worker_context block).
    decisions = await db_module.get_decisions(db, project_id)
    if goal is not None:
        goal["field_ages"] = field_ages
        goal["coherence_warning"] = coherence
        goal["decisions"] = decisions
    goal_xml = db_module.build_goal_xml(
        goal, project_name, ambient_for_xml, coherence,
        decisions=decisions,
    )
    # v0.6.2 — Anthropic-API content blocks with cache_control on
    # the two static fields. Same ambient slice used by the XML.
    goal_cache_blocks = db_module.build_goal_cache_blocks(
        goal, project_name, ambient_for_xml
    )
    if goal is not None:
        goal["xml"] = goal_xml
        goal["cache_blocks"] = goal_cache_blocks

    # v1.1 — surface the active sprint checklist so cold sessions see
    # what's in flight before doing anything. "Active" = todo/pending/in_progress.
    active_statuses = ("todo", "pending", "in_progress")
    all_sprint_items = await db_module.get_sprint_items(db, project_id)
    pending_items = [
        it for it in all_sprint_items if it.get("status") in active_statuses
    ]
    sprint_items_xml = db_module.build_sprint_items_xml(pending_items)

    # v2.3 — every cold session reads the coordination protocol on entry.
    meridian_instructions = _load_meridian_md()

    return {
        "session_id": session["id"],
        "goal": goal,
        "goal_xml": goal_xml,  # v0.6.1 — always present
        "goal_cache_blocks": goal_cache_blocks,  # v0.6.2 — ready for Anthropic
        "sprint_items": pending_items,  # v1.1 — active checklist
        "sprint_items_xml": sprint_items_xml,
        "recent_tasks": recent_tasks,
        "active_sessions": active_sessions,
        "handoff_exists": handoff_exists,
        "handoff_path": handoff_path_str,
        "files": list(_EDITABLE_FILES),
        "meridian_instructions": meridian_instructions,  # v2.3
    }


@app.post("/projects/{project_id}/start-session")
async def start_session_endpoint(
    project_id: str, body: StartSessionRequest, request: Request
) -> dict[str, Any]:
    """v0.4.4 — one call to start a coordinated session.

    Registers the caller, fetches goal + ambient tasks, fetches the last 10
    tasks, lists active sessions, and reports whether a handoff file already
    exists on disk. Replaces 4 separate MCP calls at session cold-start.
    """
    project = await db_module.get_project(_db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await _start_session_composite(
        _db(request),
        project_id,
        body.session_name,
        _data_dir(request),
        human_id=body.human_id,
    )


# ---------------------------------------------------------------------------
# Admin — shutdown
# ---------------------------------------------------------------------------


@app.post("/admin/shutdown")
async def admin_shutdown(request: Request) -> Response:
    """v1.7.0 — gracefully stop the server process.

    Returns immediately with ``{"ok": True}`` then sends SIGINT to the
    current process after a short delay so the HTTP response has time to
    flush. The dashboard shows a "restart required" message on receipt.
    """
    if _is_demo_request(request):
        return JSONResponse(
            {"detail": "Not available in demo mode. Sign up at usemeridian.us"},
            status_code=403,
        )

    async def _delayed_shutdown() -> None:
        await asyncio.sleep(0.5)
        os.kill(os.getpid(), signal.SIGINT)

    asyncio.create_task(_delayed_shutdown())
    return JSONResponse({"ok": True})


@app.post("/admin/restart")
async def admin_restart(request: Request) -> Response:
    """v1.9.x — restart the server by spawning a new process then shutting down.

    Spawns ``pixi run start`` (falling back to the current Python interpreter)
    in the repo root, then sends SIGINT to itself after a short delay so the
    HTTP response flushes first.  The dashboard polls ``/health`` and reloads
    when the new process is ready.
    """
    if _is_demo_request(request):
        return JSONResponse(
            {"detail": "Not available in demo mode. Sign up at usemeridian.us"},
            status_code=403,
        )

    import subprocess
    import sys

    async def _delayed_restart() -> None:
        await asyncio.sleep(0.5)
        # Kill self first so the port is freed before new process starts
        cwd = str(Path(__file__).parent.parent)

        async def _spawn_after_death() -> None:
            # Wait for port to free up then spawn new process
            import time
            time.sleep(2.0)
            try:
                kwargs: dict[str, Any] = {"cwd": cwd}
                if os.name == "nt":
                    kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
                subprocess.Popen(["pixi", "run", "start"], **kwargs)  # noqa: S603
            except FileNotFoundError:
                subprocess.Popen(  # noqa: S603
                    [sys.executable, "-m", "meridian"],
                    cwd=cwd,
                    env={**os.environ},
                )

        # Spawn a detached helper that will restart after we die
        # Use python directly + clean env to avoid PATH nesting on repeated restarts
        python_exe = sys.executable
        spawn_script = (
            f"import time, subprocess, os; time.sleep(2); "
            f"env = {{k: v for k, v in os.environ.items()}}; "
            f"env['CONDA_SHLVL'] = '1'; "
            f"[env.pop(k, None) for k in list(env) if k.startswith('CONDA_ENV_SHLVL_')]; "
            f"subprocess.Popen([{python_exe!r}, '-m', 'meridian'], cwd={cwd!r}, env=env)"
        )
        subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", spawn_script],
            cwd=cwd,
        )
        await asyncio.sleep(0.5)
        os.kill(os.getpid(), signal.SIGINT)

    asyncio.create_task(_delayed_restart())
    return JSONResponse({"ok": True})


@app.get("/admin/snapshot")
async def download_snapshot(request: Request) -> Response:
    """v1.9.x — download the current DB as a SQLite file.

    For a SQLite backend, streams the ``.db`` file directly.
    For a Postgres backend, exports all tables into a fresh in-memory SQLite
    database and streams that as a downloadable file.
    """
    headers = {"Content-Disposition": "attachment; filename=meridian-snapshot.db"}
    db = _db(request)
    db_url = os.environ.get("MERIDIAN_DB_URL")

    if not db_url:
        # SQLite — serve the file directly.
        db_path = os.environ.get("MERIDIAN_DB", DEFAULT_DB_PATH)
        if db_path == ":memory:":
            raise HTTPException(400, "Cannot snapshot in-memory database")
        try:
            data = Path(db_path).read_bytes()
        except OSError as exc:
            raise HTTPException(500, f"Could not read DB file: {exc}") from exc
        return Response(content=data, media_type="application/x-sqlite3", headers=headers)

    # Postgres — export to a fresh in-memory SQLite then return its bytes.
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        async with aiosqlite.connect(tmp.name) as sdb:
            await db_module.init_db(tmp.name)  # create schema
            tables = [
                "projects", "goal_states", "sessions", "sessions_archived",
                "task_log", "sprint_items", "chat_sessions", "chat_messages",
            ]
            for table in tables:
                try:
                    rows = await db.execute_fetchall(f"SELECT * FROM {table}")  # type: ignore[attr-defined]
                    if rows:
                        cols = list(rows[0].keys())
                        placeholders = ",".join("?" * len(cols))
                        sql = (
                            f"INSERT OR IGNORE INTO {table} "
                            f"({','.join(cols)}) VALUES ({placeholders})"
                        )
                        await sdb.executemany(sql, [list(r.values()) for r in rows])
                except Exception:  # noqa: BLE001 — skip missing tables
                    pass
            await sdb.commit()
        data = Path(tmp.name).read_bytes()
    finally:
        try:
            Path(tmp.name).unlink()
        except OSError:
            pass
    return Response(content=data, media_type="application/x-sqlite3", headers=headers)


@app.post("/config/connections")
async def save_connection(body: dict[str, Any]) -> dict[str, Any]:
    """v1.9.x — save a new connection profile to meridian.toml.

    Body fields:
      * ``name``      — profile name (e.g. "local", "neon")
      * ``type``      — "sqlite" or "postgres"
      * ``url``       — Postgres URL (required when type == "postgres")
      * ``activate``  — if true, set as the active connection (default true)
    """
    name = str(body.get("name", "local")).strip()
    conn_type = body.get("type")  # optional — if omitted, reuse existing
    url = str(body.get("url", "")).strip()
    activate = bool(body.get("activate", True))

    if not name:
        raise HTTPException(400, "name is required")

    # Load existing toml or start fresh.
    data = toml_config_module.load_toml() or {}
    connections: dict[str, dict[str, str]] = {}
    for cname, ccfg in data.get("connections", {}).items():
        connections[cname] = dict(ccfg)

    if conn_type is not None:
        # Creating or updating a connection profile
        if conn_type not in ("sqlite", "postgres"):
            raise HTTPException(400, "type must be 'sqlite' or 'postgres'")
        if conn_type == "postgres" and not url:
            raise HTTPException(400, "url is required for postgres connections")
        new_cfg: dict[str, str] = {"type": conn_type}
        if conn_type == "postgres":
            new_cfg["url"] = url
        connections[name] = new_cfg
    elif name not in connections and name != "local":
        raise HTTPException(404, f"connection '{name}' not found in meridian.toml")

    current_default = data.get("default", {}).get("connection", "local")
    toml_config_module.save_toml(
        default_connection=name if activate else current_default,
        connections=connections,
    )
    return {
        "ok": True,
        "connection_name": name,
        "restart_required": activate and conn_type == "postgres",
    }



@app.delete("/config/connections/{name}")
async def delete_connection(name: str) -> dict[str, Any]:
    """v1.9.x — remove a named connection profile from meridian.toml."""
    data = toml_config_module.load_toml() or {}
    connections: dict[str, dict[str, str]] = {
        cname: dict(ccfg)
        for cname, ccfg in data.get("connections", {}).items()
    }
    if name not in connections:
        raise HTTPException(404, f"connection '{name}' not found")
    del connections[name]
    current_default = data.get("default", {}).get("connection", "local")
    # If we deleted the active connection, fall back to local
    if current_default == name:
        current_default = "local"
    toml_config_module.save_toml(
        default_connection=current_default,
        connections=connections,
    )
    return {"ok": True, "deleted": name}

# ---------------------------------------------------------------------------
# Waitlist
# ---------------------------------------------------------------------------


@app.post("/waitlist", status_code=201)
async def join_waitlist(request: Request) -> dict[str, Any]:
    """POST {"email": "...", "note": "..."} — add to hosted-tier waitlist.

    Returns the created entry. 409 on duplicate email.
    """
    body = await request.json()
    email = (body.get("email") or "").strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="valid email required")
    note = (body.get("note") or "").strip() or None
    db = _db(request)
    try:
        entry = await db_module.add_waitlist_entry(db, email, note)
    except Exception as exc:
        if "UNIQUE" in str(exc) or "unique" in str(exc):
            raise HTTPException(status_code=409, detail="email already on waitlist")
        raise
    return entry


@app.get("/waitlist")
async def list_waitlist(request: Request) -> list[dict[str, Any]]:
    """GET all waitlist entries, newest first. Admin use only."""
    db = _db(request)
    return await db_module.get_waitlist(db)


# ---------------------------------------------------------------------------
# v1.0 — Stripe Checkout (API-based, plan-aware)
# ---------------------------------------------------------------------------


@app.get("/checkout")
async def checkout_redirect(request: Request, plan: str = "standard") -> RedirectResponse:
    """Create a Stripe Checkout Session and redirect to it.

    Requires an active session cookie. ``plan`` must be ``standard`` or ``pro``.
    Falls back to the payment link if Stripe API is not configured.
    """
    from .hosted import create_stripe_checkout_session, get_current_tenant

    if plan not in ("standard", "pro"):
        raise HTTPException(status_code=400, detail="plan must be standard or pro")

    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        return RedirectResponse(f"/auth/login?next=/checkout%3Fplan%3D{plan}", status_code=302)

    try:
        url = await create_stripe_checkout_session(tenant, plan)
    except RuntimeError:
        # Stripe not configured — fall back to payment link
        fallback = os.environ.get("STRIPE_PAYMENT_LINK", "/auth/login")
        return RedirectResponse(fallback, status_code=302)

    return RedirectResponse(url, status_code=302)


# ---------------------------------------------------------------------------
# v2.0 — Stripe webhook
# ---------------------------------------------------------------------------


@app.post("/webhooks/stripe", status_code=200)
async def stripe_webhook(request: Request) -> dict[str, str]:
    """Handle Stripe webhook events.

    Verifies the ``Stripe-Signature`` header against ``STRIPE_WEBHOOK_SECRET``.
    On ``checkout.session.completed`` or ``invoice.paid``:
      1. Upserts the tenant by email.
      2. Provisions a Neon DB if not already done.
      3. Creates an API bearer token and sends a welcome email.

    Returns ``{"status": "ok"}`` on success or if the event type is ignored.
    Returns 400 on signature verification failure.
    """
    import hmac as _hmac
    import hashlib as _hashlib
    import time as _time

    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    raw_body = await request.body()
    sig_header = request.headers.get("Stripe-Signature", "")

    # Verify signature if secret is configured
    if webhook_secret:
        try:
            _verify_stripe_signature(raw_body, sig_header, webhook_secret)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        event = json.loads(raw_body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc

    event_type = event.get("type", "")
    _HANDLED = {
        "checkout.session.completed",
        "invoice.paid",
        "customer.subscription.created",
        "invoice.payment_failed",
        "customer.subscription.past_due",
    }
    if event_type not in _HANDLED:
        return {"status": "ignored"}

    event_obj = event.get("data", {}).get("object", {})
    email = (
        event_obj.get("customer_email")
        or event_obj.get("customer_details", {}).get("email")
        or ""
    )
    stripe_customer_id = event_obj.get("customer", "")

    db = _db(request)

    # Dunning: payment failed → stamp payment_failed_at and return early.
    if event_type in ("invoice.payment_failed", "customer.subscription.past_due"):
        if stripe_customer_id:
            tenant = await db_module.get_tenant_by_stripe_customer(db, stripe_customer_id)
            if tenant and not tenant.get("payment_failed_at"):
                from datetime import datetime, timezone as _tz
                ts = datetime.now(_tz.utc).isoformat()
                await db_module.update_tenant(db, tenant["id"], payment_failed_at=ts, dunning_email_sent=0)
        return {"status": "dunning_started"}

    if not email:
        return {"status": "no_email"}

    # Resolve plan from checkout metadata (standard or pro); default to standard
    plan = event_obj.get("metadata", {}).get("plan", "standard")
    if plan not in ("standard", "pro"):
        plan = "standard"

    tenant = await db_module.upsert_tenant(db, email=email)
    if stripe_customer_id:
        tenant = await db_module.update_tenant(
            db, tenant["id"], stripe_customer_id=stripe_customer_id, plan=plan
        )
        # Payment recovered — clear any dunning state
        if tenant and tenant.get("payment_failed_at"):
            tenant = await db_module.update_tenant(
                db, tenant["id"], payment_failed_at=None, dunning_email_sent=0
            )

    # Extract metered subscription item ID when overage price is configured.
    # Best-effort — never block provisioning if this fails.
    from .hosted import STRIPE_OVERAGE_PRICE_ID as _OVERAGE_PRICE_ID
    subscription_id = event_obj.get("subscription")
    if subscription_id and _OVERAGE_PRICE_ID and stripe_customer_id:
        try:
            import stripe as _stripe
            _stripe.api_key = os.environ.get("STRIPE_API_KEY", "")
            sub = _stripe.Subscription.retrieve(subscription_id)
            metered_item_id = next(
                (i.id for i in sub.items.data if i.price.id == _OVERAGE_PRICE_ID),
                None,
            )
            if metered_item_id:
                await db_module.update_tenant(
                    db, tenant["id"], stripe_metered_item_id=metered_item_id
                )
        except Exception:  # noqa: BLE001
            pass

    # Capacity check before provisioning
    from .hosted import check_capacity, provision_neon_db, send_welcome_email
    try:
        await check_capacity(db)
    except RuntimeError as cap_exc:
        import logging
        logging.getLogger(__name__).error("Capacity exceeded: %s", cap_exc)
        return {"status": "capacity_exceeded"}

    # Provision Neon DB
    try:
        tenant = await provision_neon_db(tenant["id"], db)
    except Exception as exc:
        # Log but don't fail the webhook — Stripe will retry
        import logging
        logging.getLogger(__name__).error("Neon provisioning failed for %s: %s", email, exc)
        return {"status": "provisioning_queued"}

    # Create API token + send welcome email
    raw_token, _token_row = await db_module.create_api_token(db, tenant["id"], label="welcome")
    try:
        await send_welcome_email(email, raw_token, tenant)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Welcome email failed for %s: %s", email, exc)

    return {"status": "ok"}


def _verify_stripe_signature(raw_body: bytes, sig_header: str, secret: str) -> None:
    """Verify a Stripe webhook signature. Raises ValueError on failure."""
    import hmac
    import hashlib
    import time

    parts = {k: v for part in sig_header.split(",") for k, v in [part.split("=", 1)] if "=" in part}
    timestamp = parts.get("t", "")
    sig = parts.get("v1", "")
    if not timestamp or not sig:
        raise ValueError("missing signature components")

    try:
        ts = int(timestamp)
    except ValueError:
        raise ValueError("invalid timestamp")

    tolerance = 300  # 5 minutes
    if abs(time.time() - ts) > tolerance:
        raise ValueError("webhook timestamp too old")

    payload = f"{timestamp}.{raw_body.decode()}"
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise ValueError("signature mismatch")


# ---------------------------------------------------------------------------
# v2.0 — Bearer token management
# ---------------------------------------------------------------------------


@app.post("/auth/tokens", status_code=201)
async def create_api_token(request: Request) -> dict[str, Any]:
    """Generate a new API bearer token for the authenticated tenant.

    Requires a valid session cookie (browser flow) or existing bearer token.
    Returns ``{"token": "sk_meridian_...", "id": "...", "label": "..."}``
    where ``token`` is shown exactly once and never stored in plain text.
    """
    from .hosted import get_current_tenant, get_tenant_from_bearer

    # Accept either session cookie or existing bearer token
    tenant = None
    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        pass
    if tenant is None:
        try:
            tenant = await get_tenant_from_bearer(request)
        except HTTPException:
            pass
    if tenant is None:
        raise HTTPException(status_code=401, detail="authentication required")

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    label = (body.get("label") or "").strip() or None
    db = _db(request)
    raw_token, token_row = await db_module.create_api_token(db, tenant["id"], label)
    return {
        "token": raw_token,
        "id": token_row["id"],
        "label": token_row["label"],
        "created_at": token_row["created_at"],
    }


@app.get("/auth/me")
async def get_me(request: Request) -> dict[str, Any]:
    """Return the authenticated tenant's profile (session cookie or bearer)."""
    from .hosted import get_current_tenant, get_tenant_from_bearer

    tenant = None
    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        pass
    if tenant is None:
        tenant = await get_tenant_from_bearer(request)
    return {k: v for k, v in tenant.items() if k not in ("neon_db_url",)}


# ---------------------------------------------------------------------------
# v2.0 — Remote MCP endpoint (HTTP JSON-RPC 2.0 transport)
# ---------------------------------------------------------------------------

_MCP_PROTOCOL_VERSION = "2024-11-05"
_MCP_SERVER_INFO = {"name": "meridian", "version": _VERSION}


def _jsonrpc_ok(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_err(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


TOOL_EXAMPLES: dict[str, str] = {
    "create_project": 'create_project(name="my-app")',
    "start_session": 'start_session(project_id="abc-123", session_name="feature-x", human_id="alice")',
    "register_session": 'register_session(project_id="abc-123", session_name="feature-x", human_id="alice")',
    "log_task": 'log_task(project_id="abc-123", description="Fixed auth bug", status="done")',
    "get_context_block": 'get_context_block(project_id="abc-123", mode="chat")',
    "claim_task": 'claim_task(task_id="task-uuid-here")',
    "complete_task": 'complete_task(task_id="task-uuid-here")',
    "get_tasks": 'get_tasks(project_id="abc-123")',
    "get_goal": 'get_goal(project_id="abc-123")',
    "set_goal": 'set_goal(project_id="abc-123", content="Build a great product")',
    "set_sprint": 'set_sprint(project_id="abc-123", sprint="v2.0 — auth + dashboard")',
    "set_north_star": 'set_north_star(project_id="abc-123", north_star="Ship by Q3")',
    "pin_decision": 'pin_decision(project_id="abc-123", decision="Use psycopg3", rationale="asyncpg has DLL issues on Windows", category="TECHNICAL")',
    "get_pinned_decisions": 'get_pinned_decisions(project_id="abc-123")',
    "generate_handoff": 'generate_handoff(project_id="abc-123", session_id="session-uuid")',
    "get_session_brief": 'get_session_brief(project_id="abc-123")',
    "request_hitl": 'request_hitl(project_id="abc-123", question="Should we add rate limiting here?", urgency="normal")',
    "get_hitl_request": 'get_hitl_request(request_id="hitl-uuid")',
    "add_note": 'add_note(project_id="abc-123", content="Reminder: update env vars before deploy")',
    "get_notes": 'get_notes(project_id="abc-123")',
    "add_sprint_item": 'add_sprint_item(project_id="abc-123", title="Add OAuth login", item_group="auth")',
    "get_sprint_items": 'get_sprint_items(project_id="abc-123")',
    "complete_sprint_item": 'complete_sprint_item(item_id="item-uuid")',
    "heartbeat": 'heartbeat(session_id="session-uuid")',
    "list_projects": 'list_projects()',
    "get_sessions": 'get_sessions(project_id="abc-123")',
}

_MCP_TOOLS_LIST: list[dict[str, Any]] = [
    {"name": "create_project", "description": "Create a new Meridian project.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "register_session", "description": "Register this Claude session. Call at session start.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "session_name": {"type": "string"},
         "human_id": {"type": "string"}}, "required": ["project_id", "session_name"]}},
    {"name": "start_session", "description": "Register session and return goal + recent tasks in one call.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "session_name": {"type": "string"},
         "human_id": {"type": "string"}}, "required": ["project_id", "session_name"]}},
    {"name": "get_goal", "description": "Read the current goal state.",
     "inputSchema": {"type": "object", "properties": {"project_id": {"type": "string"}}, "required": ["project_id"]}},
    {"name": "set_goal", "description": "Set or update the goal state.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "content": {"type": "string"}}, "required": ["project_id", "content"]}},
    {"name": "log_task", "description": "Log a task this session completed or is working on. Valid statuses: pending, in_progress, done, failed, backlog, future, backburner.",
     "inputSchema": {"type": "object", "properties": {
         "session_id": {"type": "string"}, "project_id": {"type": "string"},
         "description": {"type": "string"}, "status": {"type": "string"}},
         "required": ["session_id", "project_id", "description"]}},
    {"name": "get_tasks", "description": "Get recent tasks across all sessions.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["project_id"]}},
    {"name": "generate_handoff", "description": "Generate a context handoff file.",
     "inputSchema": {"type": "object", "properties": {"project_id": {"type": "string"}}, "required": ["project_id"]}},
    {"name": "get_context_block", "description":
        "Return a compact plain-text project context block (north star, sprint, "
        "pending sprint items, recent tasks, recent decisions, active sessions). "
        "mode='full' (default) for Code Handoff into a fresh Claude Code session; "
        "mode='chat' for a shorter paste into a new claude.ai conversation.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "mode": {"type": "string", "enum": ["full", "chat"]}},
         "required": ["project_id"]}},
    {"name": "pin_decision", "description":
        "Create a pinned decision (editable constitution row). Use for the "
        "current authoritative truth that supersedes earlier statements. "
        "Category: STRATEGIC, COMPETITIVE, TECHNICAL, TACTICAL, BUSINESS, "
        "PRODUCT, ARCHITECTURAL.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "title": {"type": "string"},
         "body": {"type": "string"},
         "category": {"type": "string"}},
         "required": ["project_id", "title", "body"]}},
    {"name": "update_decision", "description":
        "Patch a pinned decision. Pass new_title + new_body to atomically "
        "supersede (creates a new active row, marks old as superseded with "
        "back-link). Otherwise patches body/title/category/status in place.",
     "inputSchema": {"type": "object", "properties": {
         "decision_id": {"type": "string"},
         "new_title": {"type": "string"},
         "new_body": {"type": "string"},
         "title": {"type": "string"},
         "body": {"type": "string"},
         "category": {"type": "string"},
         "status": {"type": "string"}},
         "required": ["decision_id"]}},
    {"name": "get_pinned_decisions", "description":
        "List pinned decisions (active only by default, newest first).",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "include_superseded": {"type": "boolean"}},
         "required": ["project_id"]}},
    {"name": "request_hitl", "description":
        "Surface a question to the human-in-the-loop queue. urgency='blocking' "
        "means this session pauses until answered (poll get_hitl_request). "
        "urgency='normal'/'high' lands in the dashboard but doesn't block. "
        "assigned_to routes to a specific human_id (null = broadcast).",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "question": {"type": "string"},
         "session_id": {"type": "string"},
         "context": {"type": "string"},
         "urgency": {"type": "string", "enum": ["normal", "high", "blocking"]},
         "assigned_to": {"type": "string"}},
         "required": ["project_id", "question"]}},
    {"name": "get_hitl_request", "description":
        "Poll a HITL request for the human's answer. Returns the row including "
        "status ('pending'|'answered'|'dismissed') and answer text.",
     "inputSchema": {"type": "object", "properties": {
         "request_id": {"type": "string"}},
         "required": ["request_id"]}},
    {"name": "add_note", "description":
        "Add a per-project wiki note (setup, gotcha, howto, env, ...). "
        "Free-form title/body; comma-separated tags optional.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "title": {"type": "string"},
         "body": {"type": "string"},
         "tags": {"type": "string"}},
         "required": ["project_id", "title", "body"]}},
    {"name": "get_notes", "description":
        "List project notes (newest first). Optional ?tag substring filter.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "tag": {"type": "string"}},
         "required": ["project_id"]}},
    {"name": "delete_note", "description":
        "Hard-delete a project note by id.",
     "inputSchema": {"type": "object", "properties": {
         "note_id": {"type": "string"}},
         "required": ["note_id"]}},
    {"name": "get_session_brief", "description":
        "Single-call session orientation — returns sprint focus, pending sprint items, "
        "recent tasks, any blocking failures, and pending HITL requests in a compact "
        "XML envelope (<500 tokens). Replaces the start_session + get_context_block "
        "two-call pattern for worker/automation sessions.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": {"type": "string"},
         "role": {"type": "string", "enum": ["worker", "planner", "review"],
                  "description": "Controls verbosity. 'worker'=sprint+tasks only, 'planner'=full context."}},
         "required": ["project_id"]}},
]


async def _handle_mcp_request(body: dict[str, Any], db: Any, data_dir: str) -> dict[str, Any]:
    """Dispatch one JSON-RPC 2.0 MCP request and return the response dict."""
    req_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params") or {}

    if method == "initialize":
        return _jsonrpc_ok(req_id, {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "serverInfo": _MCP_SERVER_INFO,
            "capabilities": {"tools": {}},
        })

    if method in ("notifications/initialized", "ping"):
        return _jsonrpc_ok(req_id, {})

    if method == "tools/list":
        return _jsonrpc_ok(req_id, {"tools": _MCP_TOOLS_LIST})

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            result = await _dispatch_mcp_tool(name, args, db, data_dir)
            return _jsonrpc_ok(req_id, {"content": [{"type": "text", "text": json.dumps(result)}]})
        except Exception as exc:
            return _jsonrpc_err(req_id, -32603, str(exc))

    return _jsonrpc_err(req_id, -32601, f"method not found: {method}")


async def _dispatch_mcp_tool(name: str, args: dict[str, Any], db: Any, data_dir: str) -> Any:
    """Route a tools/call to the appropriate db_module function."""
    if name == "create_project":
        return await db_module.create_project(db, args["name"])
    if name == "register_session":
        return await db_module.register_session(
            db, args["project_id"], args["session_name"],
            args.get("human_id"),
            agent_framework=args.get("agent_framework", "claude_code"),
        )
    if name == "start_session":
        session = await db_module.register_session(
            db, args["project_id"], args["session_name"],
            args.get("human_id"),
            agent_framework=args.get("agent_framework", "claude_code"),
        )
        goal = await db_module.get_goal(db, args["project_id"])
        tasks = await db_module.get_tasks(db, args["project_id"], limit=10)
        return {"session": session, "goal": goal, "recent_tasks": tasks}
    if name == "get_goal":
        goal = await db_module.get_goal(db, args["project_id"])
        if goal and goal.get("decisions") and len(goal["decisions"]) > 3000:
            goal["decisions"] = goal["decisions"][-3000:]
        return goal
    if name == "set_goal":
        return await db_module.set_goal(db, args["project_id"], args["content"])
    if name == "log_task":
        return await db_module.log_task(
            db, args["session_id"], args["project_id"],
            args["description"], args.get("status", "done"),
            parent_task_id=args.get("parent_task_id"),
        )
    if name == "get_tasks":
        return await db_module.get_tasks(db, args["project_id"], args.get("limit", 20))
    if name == "generate_handoff":
        from . import handoff as handoff_module_local
        try:
            path, content = await asyncio.wait_for(
                handoff_module_local.generate_handoff(db, args["project_id"], data_dir),
                timeout=90.0,
            )
        except asyncio.TimeoutError:
            path, content = await handoff_module_local._generate_handoff_l0(
                db, args["project_id"], data_dir
            )
        return {"file_path": path, "content": content}
    if name == "pin_decision":
        return await db_module.pin_decision(
            db, args["project_id"], args["title"], args["body"],
            args.get("category", "TECHNICAL"),
        )
    if name == "update_decision":
        new_title = args.get("new_title")
        new_body = args.get("new_body")
        if new_title and new_body:
            return await db_module.supersede_pinned_decision(
                db, args["decision_id"], new_title, new_body, args.get("category"),
            )
        result = await db_module.update_pinned_decision(
            db, args["decision_id"],
            body=args.get("body"),
            title=args.get("title"),
            category=args.get("category"),
            status=args.get("status"),
            superseded_by=args.get("superseded_by"),
        )
        if result is None:
            raise ValueError("decision not found")
        return result
    if name == "get_pinned_decisions":
        return await db_module.get_pinned_decisions(
            db, args["project_id"],
            include_superseded=bool(args.get("include_superseded", False)),
        )
    if name == "request_hitl":
        return await db_module.request_hitl(
            db, args["project_id"], args["question"],
            session_id=args.get("session_id"),
            context=args.get("context"),
            urgency=args.get("urgency", "normal"),
            assigned_to=args.get("assigned_to"),
        )
    if name == "get_hitl_request":
        result = await db_module.get_hitl_request(db, args["request_id"])
        if result is None:
            raise ValueError("hitl request not found")
        return result
    if name == "add_note":
        return await db_module.add_project_note(
            db, args["project_id"], args["title"], args["body"],
            args.get("tags"),
        )
    if name == "get_notes":
        return await db_module.get_project_notes(
            db, args["project_id"], tag=args.get("tag"),
        )
    if name == "delete_note":
        ok = await db_module.delete_project_note(db, args["note_id"])
        return {"deleted": ok}
    if name == "get_context_block":
        # v2.3 — assemble the same shape as /projects/{id}/context-block but
        # return both the rendered text AND the source dict so MCP clients
        # can choose to render their own variant.
        # v2.5 — wrap in semantic XML for better Claude Code parsing.
        project_id = args["project_id"]
        mode = args.get("mode", "full")
        project = await db_module.get_project(db, project_id)
        if project is None:
            raise ValueError("project not found")
        goal = await db_module.get_goal(db, project_id)
        sprint_items = await db_module.get_sprint_items(
            db, project_id, status="pending"
        )
        all_tasks = await db_module.get_tasks(db, project_id, limit=20)
        pending_tasks = [
            t for t in all_tasks if t.get("status") in ("pending", "in_progress", "done")
        ][:10]
        sessions = await db_module.get_sessions(db, project_id, active_only=True)
        decisions_raw = (project.get("decisions") or "").strip()
        recent_decisions = [
            l.strip() for l in decisions_raw.splitlines() if l.strip()
        ][-5:]
        text = _render_context_block(
            project, goal, sprint_items, pending_tasks, sessions, recent_decisions,
            mode=mode,
        )
        xml_text = f'<meridian_context project_id="{project_id}" mode="{mode}">\n{text}\n</meridian_context>'
        return {"mode": mode, "text": xml_text, "project_id": project_id}
    if name == "get_session_brief":
        # v2.5 — single-call orientation, <500 tokens, XML output.
        project_id = args["project_id"]
        role = args.get("role", "worker")
        goal = await db_module.get_goal(db, project_id)
        tasks = await db_module.get_tasks(db, project_id, limit=5)
        hitl_rows = await db_module.list_hitl_requests(db, project_id, status="pending")
        sprint_items = await db_module.get_sprint_items(db, project_id, status="pending")
        blocking = [t for t in tasks if t.get("status") == "failed"]
        sprint_str = (goal.get("sprint") or "") if goal else ""
        tasks_xml = "\n".join(
            f'  <task status="{t.get("status","?")}">{(t.get("description") or "")[:80]}</task>'
            for t in tasks
        )
        sprint_items_xml = "\n".join(
            f'  <item version="{it.get("version","")}">{(it.get("title") or "")[:80]}</item>'
            for it in sprint_items[:5]
        )
        hitl_attr = f' count="{len(hitl_rows)}"' if hitl_rows else ""
        blocking_xml = f'<blocking>{(blocking[0].get("description") or "")[:100]}</blocking>' if blocking else ""
        brief = (
            f'<session_brief project_id="{project_id}" role="{role}">\n'
            f'<sprint>{sprint_str[:200]}</sprint>\n'
            f'<pending_items>\n{sprint_items_xml}\n</pending_items>\n'
            f'<last_tasks>\n{tasks_xml}\n</last_tasks>\n'
            f'{blocking_xml}\n'
            f'{"<hitl_pending" + hitl_attr + "/>" if hitl_rows else ""}\n'
            f'</session_brief>'
        )
        return {"text": brief, "project_id": project_id, "role": role}
    raise ValueError(f"unknown tool: {name}")


_MCP_RATE_LIMIT = "100/minute"


@app.post("/mcp")
async def remote_mcp(request: Request) -> Any:
    """Remote MCP endpoint — JSON-RPC 2.0 over HTTP.

    Requires ``Authorization: Bearer sk_meridian_...`` header.
    Rate-limited to 100 requests/minute per IP.
    Accepts a single JSON-RPC 2.0 message or a batch (list).
    """
    from .hosted import get_tenant_from_bearer
    from fastapi.responses import JSONResponse

    if _limiter is not None:
        try:
            await _limiter._check_request_limit(request, None, False)
        except Exception:
            pass  # rate limiting is best-effort; don't block on errors

    # Bearer auth required
    tenant = await get_tenant_from_bearer(request)  # raises 401 if invalid

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_jsonrpc_err(None, -32700, "parse error"), status_code=400)

    db = _db(request)
    data_dir = _data_dir(request)

    if isinstance(body, list):
        results = [await _handle_mcp_request(item, db, data_dir) for item in body]
        return JSONResponse(results)

    result = await _handle_mcp_request(body, db, data_dir)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------


def build_mcp_server():
    """Construct the MCP server with all eight Meridian tools.

    The server opens its own dedicated SQLite connection because MCP runs in
    a separate event-loop context from FastAPI. Tools return JSON-serialisable
    dicts; descriptions are written verbosely so Claude knows when to use
    them without further prompting.
    """
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
    import json

    server: Server = Server("meridian")

    # Lazy holder for the DB connection — opened on first use because the
    # stdio entrypoint is sync up to the point we hit asyncio.run().
    state: dict[str, Any] = {"db": None, "data_dir": None}

    async def _ensure_db() -> aiosqlite.Connection:
        if state["db"] is None:
            try:
                from dotenv import load_dotenv

                load_dotenv(override=False)
            except ImportError:
                pass
            data_dir = Path(
                os.environ.get("MERIDIAN_DATA_DIR", str(DEFAULT_DATA_DIR))
            )
            data_dir.mkdir(parents=True, exist_ok=True)
            db_url = os.environ.get("MERIDIAN_DB_URL")
            if db_url:
                state["db"] = await db_module.init_db(db_url)
            else:
                db_path = os.environ.get("MERIDIAN_DB", DEFAULT_DB_PATH)
                if db_path != ":memory:":
                    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
                state["db"] = await db_module.init_db(db_path)
            state["data_dir"] = str(data_dir)
        return state["db"]

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """Advertise every Meridian tool to the MCP client."""
        return [
            Tool(
                name="create_project",
                description=(
                    "Create a new Meridian project to coordinate sessions "
                    "around. Returns the project id and name. Project names "
                    "must be unique."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            ),
            Tool(
                name="register_session",
                description=(
                    "Register this Claude session with a project. Call at "
                    "the START of every session before using any other "
                    "tools. Store the returned session_id — you need it "
                    "for log_task. Optionally pass human_id to attach "
                    "the session to a teammate (e.g. \"adam\") so the "
                    "dashboard groups sessions per human and the goal "
                    "ownership rule can recognise the writer."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "session_name": {"type": "string"},
                        "human_id": {
                            "type": "string",
                            "description": "Optional human owner identifier.",
                        },
                    },
                    "required": ["project_id", "session_name"],
                },
            ),
            Tool(
                name="get_goal",
                description=(
                    "Read the current goal state plus ambient context "
                    "for a project. Returns all three goal levels "
                    "(north_star, content/version goal, sprint) plus the "
                    "last 5 task descriptions so a cold session knows the "
                    "directive AND recent activity from one call. Read "
                    "this after registering."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"project_id": {"type": "string"}},
                    "required": ["project_id"],
                },
            ),
            Tool(
                name="set_goal",
                description=(
                    "Set or update the version goal (content). All "
                    "sessions see this immediately. Version increments on "
                    "each update. Content may be a JSON object or a plain "
                    "string. Optionally supply north_star or sprint to "
                    "update those fields at the same time; omit to "
                    "preserve existing values."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "content": {
                            "oneOf": [
                                {"type": "object"},
                                {"type": "string"},
                            ]
                        },
                        "north_star": {"type": "string"},
                        "sprint": {"type": "string"},
                    },
                    "required": ["project_id", "content"],
                },
            ),
            Tool(
                name="set_north_star",
                description=(
                    "Update only the north star — the long-lived product "
                    "vision that rarely changes. Owner-only: pass the "
                    "same human_id used when creating the project. "
                    "Returns 403 if the human_id doesn't match."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "north_star": {"type": "string"},
                        "human_id": {"type": "string"},
                    },
                    "required": ["project_id", "north_star", "human_id"],
                },
            ),
            Tool(
                name="set_sprint",
                description=(
                    "Update only the sprint — the short-term focus that "
                    "changes each session or week. Any team member can "
                    "call this; no ownership check."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "sprint": {"type": "string"},
                    },
                    "required": ["project_id", "sprint"],
                },
            ),
            Tool(
                name="log_task",
                description=(
                    "Log what this session just did, is doing, or failed "
                    "at. Call frequently to keep all sessions informed of "
                    "progress. Status is one of 'pending', 'done', "
                    "'failed' (default 'done')."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "project_id": {"type": "string"},
                        "description": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "done", "failed"],
                            "default": "done",
                        },
                    },
                    "required": [
                        "session_id",
                        "project_id",
                        "description",
                    ],
                },
            ),
            Tool(
                name="get_tasks",
                description=(
                    "Get recent tasks across all sessions. Shows what "
                    "everyone has done. Newest first."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "limit": {"type": "integer", "default": 20},
                    },
                    "required": ["project_id"],
                },
            ),
            Tool(
                name="get_sessions",
                description=(
                    "List all active sessions connected to this project."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"project_id": {"type": "string"}},
                    "required": ["project_id"],
                },
            ),
            Tool(
                name="generate_handoff",
                description=(
                    "Generate a context handoff file. Call when context is "
                    "filling up or before ending a session. A new session "
                    "can read this file to resume with full context. "
                    "Returns file path and rendered content."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"project_id": {"type": "string"}},
                    "required": ["project_id"],
                },
            ),
            Tool(
                name="get_context_block",
                description=(
                    "Return a compact plain-text context block — north star, "
                    "sprint, pending sprint items, recent tasks, recent "
                    "decisions, active sessions. mode='full' (default) for "
                    "the Code Handoff variant into a fresh Claude Code "
                    "session; mode='chat' for a shorter paste into a new "
                    "claude.ai conversation."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "mode": {
                            "type": "string",
                            "enum": ["full", "chat"],
                            "default": "full",
                        },
                    },
                    "required": ["project_id"],
                },
            ),
            Tool(
                name="pin_decision",
                description=(
                    "v2.4 — create a pinned decision (editable constitution). "
                    "Use for authoritative current truth that supersedes "
                    "earlier statements. The append-only set_decision log "
                    "captures every micro-decision; pin_decision holds the "
                    "live constitution. category: STRATEGIC, COMPETITIVE, "
                    "TECHNICAL, TACTICAL, BUSINESS, PRODUCT, ARCHITECTURAL."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "category": {"type": "string"},
                    },
                    "required": ["project_id", "title", "body"],
                },
            ),
            Tool(
                name="update_decision",
                description=(
                    "v2.4 — patch a pinned decision. Pass new_title + new_body "
                    "to atomically supersede (new active row created, old "
                    "marked superseded with back-link). Otherwise patches "
                    "body/title/category/status in place."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "decision_id": {"type": "string"},
                        "new_title": {"type": "string"},
                        "new_body": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "category": {"type": "string"},
                        "status": {"type": "string"},
                    },
                    "required": ["decision_id"],
                },
            ),
            Tool(
                name="get_pinned_decisions",
                description=(
                    "v2.4 — list pinned decisions for a project (active only "
                    "by default, newest first). Pass include_superseded=true "
                    "for the full history."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "include_superseded": {"type": "boolean"},
                    },
                    "required": ["project_id"],
                },
            ),
            Tool(
                name="request_hitl",
                description=(
                    "v2.4 — surface a question to the human-in-the-loop queue. "
                    "urgency='blocking' pauses this session until answered "
                    "(poll get_hitl_request). 'normal' / 'high' land in the "
                    "dashboard but don't block. assigned_to routes to a "
                    "specific human_id; null = broadcast."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "question": {"type": "string"},
                        "session_id": {"type": "string"},
                        "context": {"type": "string"},
                        "urgency": {
                            "type": "string",
                            "enum": ["normal", "high", "blocking"],
                            "default": "normal",
                        },
                        "assigned_to": {"type": "string"},
                    },
                    "required": ["project_id", "question"],
                },
            ),
            Tool(
                name="get_hitl_request",
                description=(
                    "v2.4 — poll a HITL request for the human's answer. "
                    "Returns the row with status ('pending'|'answered'|"
                    "'dismissed') and answer text."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"request_id": {"type": "string"}},
                    "required": ["request_id"],
                },
            ),
            Tool(
                name="add_note",
                description=(
                    "v0.9 — add a per-project wiki note. Use for setup steps, "
                    "gotchas, env var reference, how-tos — anything a future "
                    "session would want to grep. Tags are comma-separated."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "tags": {"type": "string"},
                    },
                    "required": ["project_id", "title", "body"],
                },
            ),
            Tool(
                name="get_notes",
                description=(
                    "v0.9 — list project notes (newest first). Optional "
                    "``tag`` filter matches any comma-separated tag."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "tag": {"type": "string"},
                    },
                    "required": ["project_id"],
                },
            ),
            Tool(
                name="delete_note",
                description=(
                    "v0.9 — hard-delete a project note by id."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"note_id": {"type": "string"}},
                    "required": ["note_id"],
                },
            ),
            Tool(
                name="enqueue_claude_task",
                description=(
                    "PAID-TIER. Queue a long-running Claude Code subprocess "
                    "without blocking this session. Returns immediately with "
                    "a pending task row; the worker writes its result back "
                    "into the same row when it finishes. Poll get_tasks to "
                    "see the result. Use this when an MCP tool call would "
                    "otherwise time out waiting for a Claude subprocess to "
                    "complete."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "project_id": {"type": "string"},
                        "prompt": {"type": "string"},
                        "timeout": {
                            "type": "number",
                            "default": 600.0,
                            "description": (
                                "Seconds before the worker is killed. Pass "
                                "0 or a negative number to disable."
                            ),
                        },
                    },
                    "required": ["session_id", "project_id", "prompt"],
                },
            ),
            Tool(
                name="claim_task",
                description=(
                    "Atomically claim a pending task so no other worker "
                    "picks it up. Returns claimed=True on success or "
                    "claimed=False (with the current holder) when another "
                    "session already holds the lock. Call this before "
                    "doing the work; pair with release_task on completion "
                    "or failure."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "task_id": {"type": "string"},
                        "session_id": {"type": "string"},
                    },
                    "required": ["project_id", "task_id", "session_id"],
                },
            ),
            Tool(
                name="heartbeat",
                description=(
                    "Touch this session's last_seen so the idle sweep "
                    "doesn't expire it. Long-running workers should call "
                    "this every ~5 minutes between log_task calls. "
                    "Returns ok=True when the session exists and is "
                    "still open."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "session_id": {"type": "string"},
                    },
                    "required": ["project_id", "session_id"],
                },
            ),
            Tool(
                name="release_task",
                description=(
                    "Release a task previously claimed by this session. "
                    "Returns success=True when the claim was held by the "
                    "calling session, False otherwise (someone else's lock "
                    "is left untouched)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "task_id": {"type": "string"},
                        "session_id": {"type": "string"},
                    },
                    "required": ["project_id", "task_id", "session_id"],
                },
            ),
            Tool(
                name="complete_task",
                description=(
                    "Mark a claimed task as done and log an optional "
                    "completion note. Call this after finishing the work "
                    "described in the task. Pair with claim_task at the "
                    "start of work. If the task was already marked done "
                    "or failed by another process, the call is safe and "
                    "returns the current task state."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "task_id": {"type": "string"},
                        "session_id": {"type": "string"},
                        "note": {
                            "type": "string",
                            "description": "optional completion note appended to the task description",
                        },
                    },
                    "required": ["project_id", "task_id", "session_id"],
                },
            ),
            Tool(
                name="start_worker_session",
                description=(
                    "v1.2.0 — register a worker session and claim its "
                    "task in one call. Returns a slim worker_context "
                    "XML block (version_goal + claimed task + repo + "
                    "test_cmd + commit_pattern + done_when) under ~500 "
                    "tokens. Use this for Claude Code subprocess workers "
                    "that should NOT see north_star, decisions, sprint "
                    "history, or ambient task log. If task_id is "
                    "omitted, the oldest unclaimed pending task is "
                    "picked automatically."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "task_id": {"type": "string"},
                    },
                    "required": ["project_id"],
                },
            ),
            Tool(
                name="set_decision",
                description=(
                    "Append a decision entry to the project's "
                    "append-only decisions log (v1.1.4). Each entry "
                    "is prepended with a UTC date stamp so newest "
                    "decisions appear first. Use this to record "
                    "architectural calls, scope reductions, key "
                    "trade-offs — anything a future session must "
                    "know before doing the work."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["project_id", "text"],
                },
            ),
            Tool(
                name="add_sprint_item",
                description=(
                    "Append a todo item to the project's machine-trackable "
                    "sprint checklist (v1.1). Use this when you start work on "
                    "a new version so the next session sees what's in flight. "
                    "Optional: group items under a named objective with "
                    "'group'; attribute the item to a person with 'human_id'. "
                    "Use 'depends_on' to block this item until another item "
                    "finishes; 'failure_mode=stop' stops the chain if the "
                    "parent fails. Returns the new item."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "version": {"type": "string"},
                        "title": {"type": "string"},
                        "group": {
                            "type": "string",
                            "description": (
                                "Optional objective name to group this item "
                                "under on the sprint board."
                            ),
                        },
                        "human_id": {
                            "type": "string",
                            "description": "Optional: person this item is assigned to.",
                        },
                        "depends_on": {
                            "type": "string",
                            "description": "Sprint item id that must complete before this item is claimable.",
                        },
                        "failure_mode": {
                            "type": "string",
                            "enum": ["continue", "stop"],
                            "description": "'stop' blocks this item if the parent fails. Default: 'continue'.",
                        },
                    },
                    "required": ["project_id", "version", "title"],
                },
            ),
            Tool(
                name="complete_sprint_item",
                description=(
                    "Mark a sprint item done. Pass task_id to link the "
                    "task that shipped it; the timeline correlates them. "
                    "Returns the updated item or null if the id is unknown."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "item_id": {"type": "string"},
                        "task_id": {"type": "string"},
                    },
                    "required": ["project_id", "item_id"],
                },
            ),
            Tool(
                name="skip_sprint_item",
                description=(
                    "Mark a sprint item skipped (intentionally not shipped). "
                    "Provide a one-line ``reason`` so a future session can "
                    "understand the call."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "item_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["project_id", "item_id"],
                },
            ),
            Tool(
                name="fail_sprint_item",
                description=(
                    "Mark a sprint item failed — attempted but could not "
                    "be shipped. Provide a one-line ``reason`` so the next "
                    "session knows what went wrong. The item stays on the "
                    "board in 'failed' state so it isn't silently lost."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "item_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["project_id", "item_id"],
                },
            ),
            Tool(
                name="push_sprint_item",
                description=(
                    "Push a sprint item to a future version. Use this when "
                    "scope creep means the item won't fit this sprint. "
                    "``to_version`` records where it was moved (e.g. 'v2.0'). "
                    "The item status becomes 'pushed'; the next sprint can "
                    "add it fresh with add_sprint_item."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "item_id": {"type": "string"},
                        "to_version": {
                            "type": "string",
                            "description": "Target version string, e.g. 'v2.0'.",
                        },
                    },
                    "required": ["project_id", "item_id", "to_version"],
                },
            ),
            Tool(
                name="get_sprint_items",
                description=(
                    "List sprint items for a project. Optional status filter "
                    "(todo|pending|in_progress|done|failed|skipped|pushed). "
                    "Cold sessions read this to know what's still owed."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": [
                                "pending", "todo", "in_progress",
                                "done", "failed", "skipped", "pushed",
                            ],
                        },
                    },
                    "required": ["project_id"],
                },
            ),
            Tool(
                name="start_session",
                description=(
                    "Single call to start a coordinated session. Registers "
                    "you, reads goal + ambient context, shows recent work, "
                    "lists active sessions, and tells you where the handoff "
                    "file is. Call this INSTEAD of register_session + "
                    "get_goal + get_tasks separately. Returns: session_id, "
                    "goal (with ambient_tasks), recent_tasks (last 10), "
                    "active_sessions, handoff_exists, handoff_path, files."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "session_name": {"type": "string"},
                        "human_id": {
                            "type": "string",
                            "description": "Optional human owner identifier.",
                        },
                    },
                    "required": ["project_id", "session_name"],
                },
            ),
            Tool(
                name="list_projects",
                description=(
                    "List all Meridian projects with their names and ids. "
                    "No parameters required. Use this when you don't know "
                    "the project_id — find the project by name, then pass "
                    "its id to register_session or start_session. Returns "
                    "[{id, name, created_at}] newest first."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="get_project_by_name",
                description=(
                    "Look up a project by name (case-insensitive, substring "
                    "match). Returns the project id plus a brief goal "
                    "summary. Use this for cold starts when you know the "
                    "project name but not the UUID: call this first, then "
                    "pass the returned id to start_session."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "Full or partial project name — "
                                "case-insensitive substring match."
                            ),
                        }
                    },
                    "required": ["name"],
                },
            ),
            Tool(
                name="get_session_brief",
                description=(
                    "Single-call session orientation — sprint focus, pending sprint "
                    "items, recent tasks, blocking failures, and pending HITL in a "
                    "compact XML envelope (<500 tokens). Use instead of start_session "
                    "+ get_context_block for worker/automation sessions."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "role": {
                            "type": "string",
                            "enum": ["worker", "planner", "review"],
                            "description": "Context verbosity. worker=sprint+tasks only.",
                        },
                    },
                    "required": ["project_id"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(
        name: str, arguments: dict[str, Any]
    ) -> list[TextContent]:
        """Dispatch an MCP tool call to the matching db/handoff function."""
        db = await _ensure_db()
        result: Any
        try:
            if name == "create_project":
                existing = await db_module.get_project_by_name(
                    db, arguments["name"]
                )
                if existing is not None:
                    result = {
                        "error": f"project '{arguments['name']}' already exists",
                        "project": existing,
                    }
                else:
                    result = await db_module.create_project(
                        db, arguments["name"]
                    )
            elif name == "register_session":
                result = await db_module.register_session(
                    db,
                    arguments["project_id"],
                    arguments["session_name"],
                    human_id=arguments.get("human_id"),
                )
            elif name == "get_goal":
                _goal_timed_out = False
                try:
                    goal = await asyncio.wait_for(
                        db_module.get_goal(db, arguments["project_id"]),
                        timeout=30.0,
                    )
                except asyncio.TimeoutError:
                    result = {"error": "timeout", "message": "get_goal timed out. Try get_context_block instead."}
                    _goal_timed_out = True
                if not _goal_timed_out and goal is None:
                    # Even an unset goal returns a valid XML skeleton so
                    # cold sessions don't have to special-case 404.
                    project = await db_module.get_project(
                        db, arguments["project_id"]
                    )
                    project_name = project["name"] if project else ""
                    result = {
                        "error": "goal not set",
                        "xml": db_module.build_goal_xml(
                            None, project_name, []
                        ),
                        "cache_blocks": db_module.build_goal_cache_blocks(
                            None, project_name, []
                        ),
                    }
                elif not _goal_timed_out:
                    # v0.4.2/3 — surface the last five task descriptions
                    # alongside the goal so cold sessions get ambient
                    # context inline with the directive.
                    recent = await db_module.get_tasks(
                        db, arguments["project_id"], limit=5
                    )
                    goal["ambient_tasks"] = [
                        {
                            "status": t["status"],
                            "description": t["description"],
                            "created_at": t["created_at"],
                        }
                        for t in recent
                    ]
                    project = await db_module.get_project(
                        db, arguments["project_id"]
                    )
                    project_name = project["name"] if project else ""
                    field_ages = await db_module.get_goal_field_ages(
                        db, arguments["project_id"]
                    )
                    coherence = db_module.compute_coherence_warning(field_ages)
                    goal["field_ages"] = field_ages
                    goal["coherence_warning"] = coherence
                    decisions_raw = await db_module.get_decisions(
                        db, arguments["project_id"]
                    )
                    # Truncate to last 3000 chars — MCP context has hard limits
                    if decisions_raw and len(decisions_raw) > 3000:
                        decisions_raw = decisions_raw[-3000:]
                    goal["decisions"] = decisions_raw
                    goal["xml"] = db_module.build_goal_xml(
                        goal, project_name, goal["ambient_tasks"], coherence,
                        decisions=decisions_raw,
                    )
                    goal["cache_blocks"] = db_module.build_goal_cache_blocks(
                        goal, project_name, goal["ambient_tasks"]
                    )
                    # v2.3 — inject MERIDIAN.md session instructions so
                    # every cold session learns the coordination protocol
                    # without explicit prompting. Project-root override
                    # wins over the built-in default.
                    meridian_md = _load_meridian_md()
                    if meridian_md:
                        goal["meridian_instructions"] = meridian_md
                    result = goal
            elif name == "set_goal":
                result = await db_module.set_goal(
                    db,
                    arguments["project_id"],
                    arguments["content"],
                    north_star=arguments.get("north_star"),
                    sprint=arguments.get("sprint"),
                    minor=bool(arguments.get("minor", False)),
                )
            elif name == "set_north_star":
                owner = await db_module.get_project_owner(
                    db, arguments["project_id"]
                )
                if owner is not None and arguments["human_id"] != owner:
                    result = {
                        "error": "goal_locked",
                        "message": "Only the project owner can set the north star.",
                    }
                else:
                    try:
                        result = await db_module.set_north_star(
                            db, arguments["project_id"], arguments["north_star"]
                        )
                        await goal_md_module.sync_db_to_goal_md(
                            db, arguments["project_id"]
                        )
                    except ValueError as exc:
                        result = {"error": str(exc)}
            elif name == "set_sprint":
                try:
                    result = await db_module.set_sprint(
                        db, arguments["project_id"], arguments["sprint"]
                    )
                    await goal_md_module.sync_db_to_goal_md(
                        db, arguments["project_id"]
                    )
                except ValueError as exc:
                    result = {"error": str(exc)}
            elif name == "log_task":
                result = await db_module.log_task(
                    db,
                    arguments["session_id"],
                    arguments["project_id"],
                    arguments["description"],
                    arguments.get("status", "done"),
                    parent_task_id=arguments.get("parent_task_id"),
                )
            elif name == "get_tasks":
                result = await db_module.get_tasks(
                    db,
                    arguments["project_id"],
                    limit=int(arguments.get("limit", 20)),
                )
            elif name == "get_sessions":
                result = await db_module.get_sessions(
                    db, arguments["project_id"], active_only=True
                )
            elif name == "generate_handoff":
                try:
                    path, content = await asyncio.wait_for(
                        handoff_module.generate_handoff(
                            db, arguments["project_id"], state["data_dir"]
                        ),
                        timeout=90.0,
                    )
                except asyncio.TimeoutError:
                    path, content = await handoff_module._generate_handoff_l0(
                        db, arguments["project_id"], state["data_dir"]
                    )
                result = {"path": path, "content": content}
            elif name == "get_context_block":
                # v2.3 — reuse the dispatch impl so HTTP and stdio share one path.
                result = await _dispatch_mcp_tool(
                    "get_context_block", arguments, db, state["data_dir"]
                )
            elif name in (
                "pin_decision", "update_decision", "get_pinned_decisions",
                "request_hitl", "get_hitl_request",
                "add_note", "get_notes", "delete_note",
            ):
                # v2.4/v0.9 — share dispatch with HTTP MCP so both surfaces stay in sync.
                result = await _dispatch_mcp_tool(
                    name, arguments, db, state["data_dir"]
                )
            elif name == "enqueue_claude_task":
                raw_timeout = arguments.get("timeout", 600.0)
                # Treat 0 / negative as "no timeout" — Claude jobs can be
                # genuinely open-ended.
                timeout: float | None
                try:
                    timeout = float(raw_timeout)
                    if timeout <= 0:
                        timeout = None
                except (TypeError, ValueError):
                    timeout = 600.0
                result = await enqueue_module.enqueue_claude_task(
                    db,
                    arguments["session_id"],
                    arguments["project_id"],
                    arguments["prompt"],
                    timeout=timeout,
                )
            elif name == "claim_task":
                claimed = await db_module.claim_task(
                    db,
                    arguments["task_id"],
                    arguments["session_id"],
                )
                if claimed is None:
                    existing = await db_module.get_task(
                        db, arguments["task_id"]
                    )
                    result = {
                        "task_id": arguments["task_id"],
                        "claimed": False,
                        "claimed_by": (
                            existing["claimed_by"] if existing else None
                        ),
                    }
                else:
                    result = {
                        "task_id": arguments["task_id"],
                        "claimed": True,
                        "claimed_by": claimed["claimed_by"],
                    }
            elif name == "release_task":
                released = await db_module.release_task(
                    db,
                    arguments["task_id"],
                    arguments["session_id"],
                )
                result = {
                    "task_id": arguments["task_id"],
                    "success": released,
                }
            elif name == "heartbeat":
                ok = await db_module.heartbeat_session(
                    db, arguments["session_id"]
                )
                result = {"session_id": arguments["session_id"], "ok": ok}
            elif name == "start_session":
                result = await _start_session_composite(
                    db,
                    arguments["project_id"],
                    arguments["session_name"],
                    state["data_dir"],
                    human_id=arguments.get("human_id"),
                )
            elif name == "list_projects":
                result = await db_module.list_projects(db)
            elif name == "get_project_by_name":
                name_arg = arguments["name"]
                project = await db_module.get_project_by_name(db, name_arg)
                if project is None:
                    all_projects = await db_module.list_projects(db)
                    lower = name_arg.lower()
                    matches = [
                        p for p in all_projects
                        if lower in p["name"].lower()
                    ]
                    project = matches[0] if matches else None
                if project is None:
                    result = {
                        "error": f"no project found matching '{name_arg}'"
                    }
                else:
                    goal = await db_module.get_goal(db, project["id"])
                    result = {
                        "project": project,
                        "goal_version": goal["version"] if goal else None,
                        "goal_summary": (
                            str(goal["content"])[:200] if goal else None
                        ),
                    }
            elif name == "start_worker_session":
                try:
                    result = await db_module.start_worker_session(
                        db,
                        arguments["project_id"],
                        task_id=arguments.get("task_id"),
                    )
                except ValueError as exc:
                    result = {"error": str(exc)}
            elif name == "set_decision":
                try:
                    updated = await db_module.set_decision(
                        db,
                        arguments["project_id"],
                        arguments["text"],
                    )
                    result = {
                        "project_id": arguments["project_id"],
                        "decisions": updated,
                    }
                except ValueError as exc:
                    result = {"error": str(exc)}
            elif name == "add_sprint_item":
                result = await db_module.add_sprint_item(
                    db,
                    arguments["project_id"],
                    arguments["version"],
                    arguments["title"],
                    group=arguments.get("group"),
                    human_id=arguments.get("human_id"),
                    depends_on=arguments.get("depends_on"),
                    failure_mode=arguments.get("failure_mode"),
                )
            elif name == "complete_sprint_item":
                item = await db_module.complete_sprint_item(
                    db,
                    arguments["project_id"],
                    arguments["item_id"],
                    task_id=arguments.get("task_id"),
                )
                result = item or {"error": "sprint item not found"}
            elif name == "skip_sprint_item":
                item = await db_module.skip_sprint_item(
                    db,
                    arguments["project_id"],
                    arguments["item_id"],
                    reason=arguments.get("reason"),
                )
                result = item or {"error": "sprint item not found"}
            elif name == "fail_sprint_item":
                item = await db_module.fail_sprint_item(
                    db,
                    arguments["project_id"],
                    arguments["item_id"],
                    reason=arguments.get("reason"),
                )
                result = item or {"error": "sprint item not found"}
            elif name == "push_sprint_item":
                try:
                    item = await db_module.push_sprint_item(
                        db,
                        arguments["project_id"],
                        arguments["item_id"],
                        arguments["to_version"],
                    )
                    result = item or {"error": "sprint item not found"}
                except ValueError as exc:
                    result = {"error": str(exc)}
            elif name == "get_sprint_items":
                result = await db_module.get_sprint_items(
                    db,
                    arguments["project_id"],
                    status=arguments.get("status"),
                )
            elif name == "complete_task":
                task = await db_module.get_task(db, arguments["task_id"])
                if task is None:
                    result = {"error": f"task {arguments['task_id']} not found"}
                else:
                    note = arguments.get("note", "")
                    new_desc = (
                        f"{task['description']} — {note}" if note else task["description"]
                    )
                    updated = await db_module.update_task(
                        db,
                        arguments["task_id"],
                        status="done",
                        description=new_desc,
                    )
                    db_module._publish_task("task_updated", updated or task)
                    result = updated or task
                    # Update CLAUDE.md with current sprint state.
                    await _regenerate_claude_md(db, task["project_id"], _REPO_ROOT)
            elif name == "get_session_brief":
                result = await _dispatch_mcp_tool(
                    "get_session_brief", arguments, db, state["data_dir"]
                )
            else:
                result = {"error": f"unknown tool: {name}"}
        except Exception as exc:  # noqa: BLE001 — surface to MCP client
            result = {"error": f"{type(exc).__name__}: {exc}"}

        return [TextContent(type="text", text=json.dumps(result, default=str))]

    async def run_stdio() -> None:
        """Run the MCP server over stdio until the client disconnects."""
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    return server, run_stdio
