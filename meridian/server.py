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
import html as html_module
import json
import asyncio
import os
import re
import signal
from contextlib import asynccontextmanager
from pathlib import Path

# ---------------------------------------------------------------------------
# Shared helpers (templates, _db, _hosted_mode, etc.) live in _deps.py so
# that routes/ modules can import them without circular-importing server.py.
# ---------------------------------------------------------------------------
from ._deps import (
    _VERSION,
    _ASSET_VERSION,
    _GIT_SHA,
    _resource_path,
    _templates,
    _hosted_mode,
    _DEMO_CONTEXT_COOKIE,
    _tenant_db_cache,
    _open_tenant_db_by_id,
    _db,
    _data_dir,
    _is_demo_request,
    _get_tenant_from_request,
)


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
from .executor_config import (
    build_executor_config_block,
    executor_config_for_output,
    normalize_executor_config,
)
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
    ProjectSettings,
    ProjectSettingsPatch,
    Session,
    SessionRegister,
    SetNorthStarRequest,
    SetSprintRequest,
    StartSessionRequest,
    Task,
    TaskCreate,
    TaskUpdate,
)
from .mcp_tools import _MCP_TOOLS_LIST, _TOOL_EXAMPLES

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

    # set_goal must come before set_north_star — north_star requires an existing goal row
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
    await db_module.set_north_star(
        db, api["id"],
        "Achieve 99.9% uptime and <200ms p95 latency for all API endpoints. "
        "Every service is observable, every failure is recoverable.",
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
                "INSERT INTO sprint_items (id, project_id, title, version, status, added_at) "
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

    # set_goal before set_north_star — north_star requires an existing goal row
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
    await db_module.set_north_star(
        db, pipe["id"],
        "Process 10M events/day with <5 min end-to-end latency, "
        "zero data loss, and full lineage tracking for compliance.",
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
                "INSERT INTO sprint_items (id, project_id, title, version, status, added_at) "
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
    await _hydrate_oauth_cache(db)

    # v2.2 — isolated demo DB.
    # Priority: MERIDIAN_DEMO_DB_URL → MERIDIAN_STANDARD_KEY (legacy secret
    # name on the hosted Fly app) → in-memory SQLite fallback.
    # NEVER falls through to production DB.
    demo_db_url = (
        os.environ.get("MERIDIAN_DEMO_DB_URL")
        or os.environ.get("MERIDIAN_STANDARD_KEY")
    )

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
            # 30s timeout — Neon cold-start can take ~15-20s; 30s gives it room
            await asyncio.wait_for(_init_demo(demo_db_url), timeout=30.0)
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
# Routers — extracted route modules (routes/ package)
# Include these BEFORE middleware so route matching is registered correctly.
# ---------------------------------------------------------------------------
from .routes.notes import router as _notes_router            # noqa: E402
from .routes.hitl import router as _hitl_router              # noqa: E402
from .routes.sprint import router as _sprint_router          # noqa: E402
from .routes.sessions import router as _sessions_router      # noqa: E402
from .routes.tasks import router as _tasks_router            # noqa: E402
from .routes.decisions import router as _decisions_router    # noqa: E402
from .routes.handoff import router as _handoff_router        # noqa: E402
from .routes.admin import router as _admin_router            # noqa: E402

app.include_router(_notes_router)
app.include_router(_hitl_router)
app.include_router(_sprint_router)
app.include_router(_sessions_router)
app.include_router(_tasks_router)
app.include_router(_decisions_router)
app.include_router(_handoff_router)
app.include_router(_admin_router)

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
    if path in ("/health", "/mcp/health", "/__gate__", "/config", "/static", "/mcp/tools-doc", "/mcp/quickstart", "/mcp/sse") or path.startswith("/static/") or path == "/demo" or path.startswith("/demo/"):
        return await call_next(request)
    # Demo cookie bypasses site password gate — demo users don't go through __gate__
    if request.cookies.get(_DEMO_CONTEXT_COOKIE):
        return await call_next(request)
    if _hosted_mode():
        from .hosted import _SESSION_COOKIE, _read_session_cookie
        from . import db as db_module
        auth_db = request.app.state.db
        cookie_val = request.cookies.get(_SESSION_COOKIE, "")
        if cookie_val:
            session_id = _read_session_cookie(cookie_val)
            if session_id and await db_module.get_user_session(auth_db, session_id):
                return await call_next(request)
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            import hashlib

            token_hash = hashlib.sha256(auth_header[7:].encode()).hexdigest()
            if await db_module.get_tenant_from_token_hash(auth_db, token_hash):
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

_DEMO_WRITE_ALLOWLIST = {"/__gate__", "/mcp/sse"}
_DEMO_WRITE_ALLOWLIST_PREFIXES = ("/auth/", "/demo", "/waitlist", "/health")
_DEMO_CONTEXT_COOKIE = "meridian_demo"


@app.middleware("http")
async def _demo_read_only_middleware(request: Request, call_next):
    """Block all mutating requests when MERIDIAN_DEMO=true or demo cookie set.

    /auth/*, /demo*, /waitlist, and /health are always allowed so OAuth,
    the password gate, and health checks function in demo mode.
    """
    env_demo = os.environ.get("MERIDIAN_DEMO", "").lower() in ("1", "true", "yes")
    cookie_demo = bool(request.cookies.get(_DEMO_CONTEXT_COOKIE))
    path = request.url.path
    allowed = (
        path in _DEMO_WRITE_ALLOWLIST
        or any(path.startswith(p) for p in _DEMO_WRITE_ALLOWLIST_PREFIXES)
    )
    if (
        (env_demo or cookie_demo)
        and request.method in ("POST", "PUT", "PATCH", "DELETE")
        and not allowed
    ):
        return Response(
            content=json.dumps({"error": "demo_readonly", "message": "Read-only demo — sign in for full access"}),
            status_code=403,
            media_type="application/json",
        )
    return await call_next(request)


# ---------------------------------------------------------------------------
# v1.0.2 — Static files + Jinja2 templates
# ---------------------------------------------------------------------------

app.mount(
    "/static",
    StaticFiles(directory=_resource_path("meridian/static")),
    name="static",
)

# Per-tenant DB connections cached by tenant_id (opened on first use, never closed).
# The dict lives in _deps.py; the import above brings it into this namespace.


# _open_tenant_db_by_id, _db, _data_dir — imported from ._deps above.


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
    resp = _templates.TemplateResponse(
        request, "landing.html", {
            "stripe_payment_link": stripe_payment_link,
            "stripe_pro_checkout": stripe_pro_checkout,
            "stripe_pro_payment_link": stripe_pro_payment_link,
        }
    )
    resp.headers["Cache-Control"] = "no-cache, no-store"
    return resp


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


@app.get("/pricing", response_class=HTMLResponse)
async def pricing_page(request: Request) -> HTMLResponse:
    """Pricing page — Free / Solo / Team tiers with waitlist forms when hosted launch is pending."""
    solo_link = os.environ.get("STRIPE_PAYMENT_LINK", "/auth/login?signup=1")
    team_link = os.environ.get("STRIPE_PRO_PAYMENT_LINK", "/auth/login?signup=1")
    launch_open = bool(os.environ.get("MERIDIAN_LAUNCH_OPEN"))
    waitlist_mode = not launch_open and not bool(os.environ.get("STRIPE_PAYMENT_LINK"))
    email = ""
    try:
        from .hosted import _SESSION_COOKIE, _read_session_cookie
        cookie_val = request.cookies.get(_SESSION_COOKIE)
        if cookie_val:
            session_id = _read_session_cookie(cookie_val)
            if session_id:
                db = await _db(request)
                user_session = await db_module.get_user_session(db, session_id)
                if user_session:
                    tenant = await db_module.get_tenant_by_id(db, user_session["tenant_id"]) or {}
                    email = tenant.get("email", "")
                    if email and not waitlist_mode:
                        sep = "&" if "?" in solo_link else "?"
                        solo_link += f"{sep}prefilled_email={email}"
                        sep = "&" if "?" in team_link else "?"
                        team_link += f"{sep}prefilled_email={email}"
    except Exception:
        pass
    return _templates.TemplateResponse(request, "pricing.html", {
        "solo_link": solo_link,
        "team_link": team_link,
        "waitlist_mode": waitlist_mode,
        "email": email,
    })


@app.get("/install-mcp", response_class=HTMLResponse)
async def install_mcp_page(request: Request) -> HTMLResponse:
    """Onboarding page: step-by-step guide to connect Claude to Meridian via MCP.

    Shows copy-ready Name + URL fields for both local and hosted configs.
    Token generation calls POST /auth/tokens client-side.
    Linked from README, docs quickstart, and dashboard Settings tab.
    """
    resp = _templates.TemplateResponse(request, "install_mcp.html", {})
    resp.headers["Cache-Control"] = "no-cache, no-store"
    return resp


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
    # Parse hostname from MERIDIAN_DB_URL for the connection label (1f92d344)
    import re as _re_cfg
    _raw_db_url = os.environ.get("MERIDIAN_DB_URL", "")
    _db_host = ""
    if _raw_db_url:
        _m = _re_cfg.search(r"@([^/:?]+)", _raw_db_url)
        if _m:
            _db_host = _m.group(1)
            if len(_db_host) > 22:
                _db_host = _db_host[:20] + "…"
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
        "db_host": _db_host,
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


@app.get("/me")
async def me_endpoint(request: Request) -> dict[str, Any]:
    """Return the current user's plan info. Returns {} for anonymous/self-hosted."""
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        return {}
    from datetime import datetime, timezone
    plan = tenant.get("plan") or "standard"
    expires_raw = tenant.get("inactivity_expires_at")
    days_remaining: int | None = None
    expired = False
    if expires_raw:
        try:
            expires_dt = datetime.strptime(expires_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            delta = expires_dt - datetime.now(timezone.utc)
            days_remaining = max(0, delta.days)
            expired = delta.total_seconds() <= 0
        except ValueError:
            pass
    return {
        "plan": plan,
        "email": tenant.get("email", ""),
        "trial_started_at": tenant.get("trial_started_at"),
        "inactivity_expires_at": expires_raw,
        "days_remaining": days_remaining,
        "expired": expired,
    }


@app.get("/projects", response_model=list[Project])
async def list_projects(request: Request) -> list[dict[str, Any]]:
    """List every project."""
    return await db_module.list_projects(await _db(request))


@app.post("/projects", response_model=Project, status_code=201)
async def create_project(
    body: ProjectCreate, request: Request
) -> dict[str, Any]:
    """Create a new project. 409 if the name is already in use."""
    existing = await db_module.get_project_by_name(await _db(request), body.name)
    if existing is not None:
        raise HTTPException(
            status_code=409, detail=f"project '{body.name}' already exists"
        )
    tenant = await _get_tenant_from_request(request)
    if tenant and tenant.get("plan") == "free":
        existing_projects = await db_module.list_projects(await _db(request))
        if len(existing_projects) >= 1:
            raise HTTPException(
                status_code=403,
                detail="Free tier is limited to 1 project. Upgrade to Solo ($20/mo) for unlimited projects.",
            )
    return await db_module.create_project(
        await _db(request), body.name, human_id=body.human_id
    )


@app.get("/setup/needed")
async def setup_needed(request: Request) -> dict[str, Any]:
    """Returns {needed: true} if no projects exist yet (first-run wizard trigger)."""
    projects = await db_module.list_projects(await _db(request))
    return {"needed": len(projects) == 0}


@app.get("/projects/by-name/{name}")
async def get_project_by_name(name: str, request: Request) -> dict[str, Any]:
    """Look up a project by name (case-insensitive substring match).

    Returns the project row plus a brief goal summary so a cold session
    can confirm it found the right project without a second round-trip.
    """
    db = await _db(request)
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
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@app.get("/projects/{project_id}/settings", response_model=ProjectSettings)
async def get_project_settings(project_id: str, request: Request) -> dict[str, Any]:
    """Return persisted per-project dashboard settings."""
    settings = await db_module.get_project_settings(await _db(request), project_id)
    if settings is None:
        raise HTTPException(status_code=404, detail="project not found")
    return settings


@app.patch("/projects/{project_id}/settings", response_model=ProjectSettings)
async def patch_project_settings(
    project_id: str, body: ProjectSettingsPatch, request: Request
) -> dict[str, Any]:
    """Update persisted per-project dashboard settings."""
    executor_config_dict = (
        normalize_executor_config(body.executor_config.model_dump(exclude_none=True))
        if body.executor_config is not None
        else None
    )
    settings = await db_module.update_project_settings(
        await _db(request),
        project_id,
        max_pinned_decisions=body.max_pinned_decisions,
        executor_config=executor_config_dict,
    )
    if settings is None:
        raise HTTPException(status_code=404, detail="project not found")
    return settings


@app.get("/projects/{project_id}/ntfy")
async def get_project_ntfy(project_id: str, request: Request) -> dict[str, Any]:
    """Return the ntfy push URL configured for this project."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    url = await db_module.get_project_ntfy_url(await _db(request), project_id)
    return {"ntfy_url": url or ""}


@app.patch("/projects/{project_id}/ntfy")
async def set_project_ntfy(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Save (or clear) the notify URL for this project.

    Accepts ``notify_url`` (preferred) or ``ntfy_url`` (legacy) key.
    After saving a non-empty URL, fires a welcome notification so ntfy.sh
    topics are created on first publish (avoids 404 on first real alert).
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    # Accept both the new canonical key and the legacy key for backwards compat
    notify_url = (
        str(body.get("notify_url") or body.get("ntfy_url") or "").strip() or None
    )
    db = await _db(request)
    await db_module.set_project_ntfy_url(db, project_id, notify_url)
    if notify_url:
        # Fire a welcome notification immediately so ntfy.sh creates the topic
        # (topics are auto-created on first publish; GET before any publish = 404)
        await _notify_project(
            db, project_id,
            "Notifications active",
            "You will receive alerts here for HITL requests and sprint completions.",
            event="setup",
        )
    return {"ntfy_url": notify_url or "", "notify_url": notify_url or ""}


@app.post("/projects/{project_id}/notify/test")
async def test_project_notification(
    project_id: str, request: Request
) -> dict[str, Any]:
    """Send a test notification to verify the configured notify URL."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    db = await _db(request)
    notify_url = await db_module.get_project_ntfy_url(db, project_id)
    if not notify_url:
        raise HTTPException(status_code=400, detail="No notify URL configured for this project")
    await _dispatch_notification(
        notify_url,
        "Meridian test notification",
        "Test from the Meridian dashboard. If you see this, notifications are working!",
        event="test",
    )
    return {"ok": True, "notify_url": notify_url}


async def _send_email_notification(to_email: str, subject: str, body_text: str) -> None:
    """Send a notification email via Resend.

    Silently skips if ``RESEND_API_KEY`` is not set. Raises ``HTTPException``
    with Resend's response body when the API rejects the request so callers
    like ``/notify/test`` can surface the real failure reason.
    """
    import httpx as _httpx

    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        return  # dev mode — skip
    base = os.environ.get("MERIDIAN_BASE_URL", "https://usemeridian.us").rstrip("/")
    from_addr = os.environ.get("MERIDIAN_FROM_EMAIL", "Meridian <noreply@usemeridian.us>")
    html_body = (
        f"<p>{body_text}</p>"
        f"<p><a href='{base}/dashboard'>Open Meridian dashboard →</a></p>"
        f"<hr style='border:none;border-top:1px solid #e5e7eb;margin:16px 0'>"
        f"<p style='color:#9ca3af;font-size:12px'>You're receiving this because you configured "
        f"<code>{to_email}</code> as your Meridian notification address. "
        f"<a href='{base}/dashboard'>Update in Settings →</a></p>"
    )
    async with _httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": from_addr,
                    "to": [to_email],
                    "subject": subject,
                    "text": body_text,
                    "html": html_body,
                },
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Resend request failed: {exc}") from exc
        if resp.status_code < 200 or resp.status_code >= 300:
            raise HTTPException(status_code=resp.status_code, detail=_response_error_detail(resp))


def _response_error_detail(resp: Any) -> str:
    """Best-effort extraction of a useful error message from an HTTP response."""
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        payload = None
    if isinstance(payload, dict):
        for key in ("detail", "message", "error"):
            value = payload.get(key)
            if isinstance(value, dict):
                value = value.get("detail") or value.get("message") or value.get("error")
            if value:
                return str(value)
        return json.dumps(payload, ensure_ascii=False)
    if payload is not None:
        return str(payload)
    text = getattr(resp, "text", "")
    if isinstance(text, str) and text.strip():
        return text.strip()
    return f"request failed with status {getattr(resp, 'status_code', 'unknown')}"


_NOTIFICATION_PREF_DEFAULTS: dict[str, bool] = {
    "hitl": True,
    "stalled": True,
    "storage": True,
    "sprint": True,
}


def _notification_prefs_from_raw(raw: Any) -> dict[str, bool]:
    """Normalize notification prefs to a complete {pref: bool} mapping."""
    prefs = dict(_NOTIFICATION_PREF_DEFAULTS)
    if isinstance(raw, dict):
        payload = raw
    else:
        try:
            payload = json.loads(raw or "{}")
        except Exception:  # noqa: BLE001
            payload = {}
    if isinstance(payload, dict):
        for key in _NOTIFICATION_PREF_DEFAULTS:
            if key in payload:
                prefs[key] = bool(payload[key])
    return prefs


def _notification_pref_enabled(
    tenant: dict[str, Any] | None,
    pref_key: str | None,
) -> bool:
    """Return True when the given preference is enabled or unset."""
    if pref_key is None:
        return True
    if tenant is None:
        return True
    prefs = _notification_prefs_from_raw(tenant.get("notification_prefs"))
    return bool(prefs.get(pref_key, True))


async def _dispatch_notification(
    notify_url: str, title: str, body_text: str, event: str = "notification"
) -> None:
    """Route a notification based on the URL format.

    Routing rules:
    - Contains ``@`` and no ``/``: treat as email → send via Resend.
    - Contains ``ntfy.sh`` or starts with ``ntfy://``: POST ntfy-style
      (body = plain text, Title/Priority/Tags headers).
    - Anything else (``https://…``): POST JSON webhook (Slack, Discord,
      custom webhooks all understand ``{"title": …, "body": …}``).

    Raises on error — callers that want best-effort should catch.
    """
    import httpx as _httpx

    url = notify_url.strip()
    if not url:
        return

    # Email address — no slashes, has @
    if "@" in url and "/" not in url:
        subject = f"[Meridian] {title}"
        await _send_email_notification(url, subject, body_text)
        return

    async with _httpx.AsyncClient(timeout=5.0) as client:
        if "ntfy.sh" in url or url.startswith("ntfy://"):
            # ntfy protocol: body = plain text, special headers carry metadata
            resp = await client.post(
                url,
                content=body_text.encode(),
                headers={"Title": title, "Priority": "high", "Tags": event},
            )
        else:
            # Generic webhook: POST JSON (compatible with Slack incoming webhooks,
            # Discord webhooks via {content: …}, and custom HTTP receivers)
            resp = await client.post(
                url,
                json={
                    "title": title,
                    "body": body_text,
                    "event": event,
                    "source": "meridian",
                },
            )
        if resp.status_code < 200 or resp.status_code >= 300:
            raise HTTPException(status_code=resp.status_code, detail=_response_error_detail(resp))


async def _notify_project(
    db: Any, project_id: str, title: str, body_text: str, event: str = "notification"
) -> None:
    """Best-effort notification for a project.  Silently ignores all errors."""
    try:
        notify_url = await db_module.get_project_ntfy_url(db, project_id)
        if not notify_url:
            return
        await _dispatch_notification(notify_url, title, body_text, event)
    except Exception:  # noqa: BLE001
        pass  # never let notifications crash the main flow


async def _maybe_notify(
    db: Any,
    project_id: str,
    title: str,
    body_text: str,
    event: str = "notification",
    *,
    tenant: dict[str, Any] | None = None,
    pref_key: str | None = None,
) -> None:
    """Send a project notification when the tenant has the event enabled."""
    try:
        if not _notification_pref_enabled(tenant, pref_key):
            return
        await _notify_project(db, project_id, title, body_text, event)
    except Exception:  # noqa: BLE001
        pass


@app.post("/projects/{project_id}/rename")
async def rename_project(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """v1.9.x — rename a project.  Broadcasts project_renamed WS event."""
    new_name = str(body.get("name") or "").strip()
    if not new_name:
        raise HTTPException(400, "name is required")
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    existing = await db_module.get_project_by_name(await _db(request), new_name)
    if existing and existing["id"] != project_id:
        raise HTTPException(409, f"project '{new_name}' already exists")
    updated = await db_module.rename_project(await _db(request), project_id, new_name)
    db_module.publish_global(
        {"type": "project_renamed", "project_id": project_id, "name": new_name}
    )
    return updated


@app.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: str, request: Request) -> None:
    """v1.9.x — delete a project and all data.

    Returns 409 if any tasks are in_progress, 404 if the project is unknown.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    try:
        await db_module.delete_project(await _db(request), project_id)
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
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    goal = await db_module.get_goal(await _db(request), project_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not set")
    recent = await db_module.get_tasks(await _db(request), project_id, limit=5)
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
        await _db(request), project_id
    )
    coherence = db_module.compute_coherence_warning(field_ages)
    goal["field_ages"] = field_ages
    goal["coherence_warning"] = coherence
    # v1.1.4 — append-only decisions log.
    decisions = await db_module.get_decisions(await _db(request), project_id)
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
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        await db_module.set_goal_mode(await _db(request), project_id, body.mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"project_id": project_id, "goal_mode": body.mode}


@app.get("/projects/{project_id}/goal-mode")
async def get_goal_mode(project_id: str, request: Request) -> dict[str, str]:
    """Return the current goal mode for a project."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    mode = await db_module.get_goal_mode(await _db(request), project_id)
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
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    owner = await db_module.get_project_owner(await _db(request), project_id)
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
        await _db(request), project_id, body.content,
        north_star=body.north_star, sprint=body.sprint,
        minor=body.minor,
    )
    await goal_md_module.sync_db_to_goal_md(await _db(request), project_id)
    return result


@app.post("/projects/{project_id}/goal/north-star", response_model=GoalState)
async def set_north_star(
    project_id: str, body: SetNorthStarRequest, request: Request
) -> dict[str, Any]:
    """v0.5.2 — update only the north star field.

    Owner-only: requires ``human_id`` matching the project creator.
    Returns the new goal version.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    owner = await db_module.get_project_owner(await _db(request), project_id)
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
            await _db(request), project_id, body.north_star
        )
        await goal_md_module.sync_db_to_goal_md(await _db(request), project_id)
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
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        result = await db_module.set_sprint(
            await _db(request), project_id, body.sprint
        )
        await goal_md_module.sync_db_to_goal_md(await _db(request), project_id)
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
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        return await db_module.start_worker_session(
            await _db(request),
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
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")
    updated = await db_module.set_decision(await _db(request), project_id, text)
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
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    timeline = await db_module.get_timeline(await _db(request), project_id)
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
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if token is not None:
        stored = await db_module.get_rewind_token(await _db(request), project_id)
        if not stored or token != stored:
            raise HTTPException(status_code=403, detail="invalid rewind token")
    if days <= 0:
        raise HTTPException(status_code=422, detail="days must be positive")
    return await db_module.get_rewind_data(await _db(request), project_id, days)


@app.post("/projects/{project_id}/rewind-token")
async def post_rewind_token(
    project_id: str, request: Request
) -> dict[str, str]:
    """v1.3.0 — mint (or return) the project's shareable rewind token.

    The token is stored on the projects row so subsequent calls return
    the same value; teams can publish a link once without it rotating.
    Response: ``{"token": "<uuid4>", "expires": "never"}``.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    token = await db_module.get_or_create_rewind_token(await _db(request), project_id)
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
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.get_goal_history(await _db(request), project_id)


@app.get("/projects/{project_id}/stats")
async def get_project_stats(
    project_id: str, request: Request, days: int = 30
) -> dict[str, Any]:
    """Return activity stats for the Charts subtab.

    Returns tasks/day series and sprint completion % per version.
    ``days`` defaults to 30, max 365.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    days = max(1, min(days, 365))
    return await db_module.get_project_stats(await _db(request), project_id, days)


# Sprint item routes → meridian/routes/sprint.py


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
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    await _expire_and_generate_handoffs(await _db(request), _data_dir(request))
    return await db_module.get_sessions(
        await _db(request), project_id, active_only=active_only
    )


# Tasks + claim/release routes → meridian/routes/tasks.py


# Handoff route → meridian/routes/handoff.py


# Session lifecycle routes → meridian/routes/sessions.py


# /tasks POST + PATCH → meridian/routes/tasks.py


@app.delete("/projects/{project_id}/chat/history", status_code=204)
async def clear_chat_history(project_id: str, request: Request) -> None:
    """Delete all chat messages and session for a project."""
    database = await _db(request)
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
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.get_chat_history(await _db(request), project_id, limit=limit)


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

    db = await _db(request)
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


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request) -> Any:
    """Plan selection page for new users after first login."""
    from .hosted import _SESSION_COOKIE, _read_session_cookie
    tenant: dict = {}
    try:
        cookie_val = request.cookies.get(_SESSION_COOKIE)
        if cookie_val:
            session_id = _read_session_cookie(cookie_val)
            if session_id:
                db = await _db(request)
                user_session = await db_module.get_user_session(db, session_id)
                if user_session:
                    tenant = await db_module.get_tenant_by_id(db, user_session["tenant_id"]) or {}
    except Exception:
        pass
    standard_link = os.environ.get("STRIPE_PAYMENT_LINK", "#")
    pro_link = os.environ.get("STRIPE_PRO_PAYMENT_LINK", "#")
    email = tenant.get("email", "")
    if email:
        sep = "&" if "?" in standard_link else "?"
        standard_link += f"{sep}prefilled_email={email}"
        sep = "&" if "?" in pro_link else "?"
        pro_link += f"{sep}prefilled_email={email}"
    return _templates.TemplateResponse(request, "onboarding.html", {
        "standard_link": standard_link,
        "pro_link": pro_link,
        "email": email,
    })


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_html(request: Request) -> Any:
    """Serve the Meridian dashboard from a Jinja2 template."""
    if os.environ.get("DEMO_PASSWORD"):
        if not _check_demo_cookie(request):
            return HTMLResponse(_demo_gate_html())
    is_admin = False
    if _hosted_mode():
        from .hosted import get_current_tenant, is_admin_db

        try:
            tenant = await get_current_tenant(request)
            is_admin = await is_admin_db(tenant.get("email", ""), request.app.state.db)
        except HTTPException:
            is_admin = False
    response = _templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "version": _VERSION,
            "asset_version": _ASSET_VERSION,
            "demo_mode": False,
            "hosted_mode": os.environ.get("MERIDIAN_HOSTED", "").lower() in ("1", "true", "yes"),
            "is_admin": is_admin,
        },
    )
    # Always clear the demo context cookie so /demo → /dashboard doesn't
    # leave the user stuck in read-only demo mode.
    response.delete_cookie(_DEMO_CONTEXT_COOKIE)
    return response


@app.get("/setup", response_class=HTMLResponse)
async def setup_redirect(request: Request) -> Any:
    """b6c9f20d — First-run setup alias for binary users.

    Redirects to /dashboard where the first-run wizard (ez-wizard modal)
    automatically detects no existing projects and walks the user through
    creating their first project and connecting an MCP client.
    """
    return RedirectResponse(url="/dashboard", status_code=302)


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
        {
            "version": _VERSION,
            "asset_version": _ASSET_VERSION,
            "demo_mode": True,
            "hosted_mode": False,
            "is_admin": False,
        },
    )
    response.set_cookie(
        _DEMO_CONTEXT_COOKIE,
        "1",
        max_age=3600,
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-cache, no-store"
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
    project = await db_module.get_project(await _db(request), body.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    messages = [m.model_dump() for m in body.messages]
    # Inject model identity into system prompt so the model knows its version
    model_hint = f"You are {body.model} running in the Meridian dashboard."
    if body.system_prompt:
        body = body.model_copy(update={"system_prompt": model_hint + "\n\n" + body.system_prompt})
    else:
        body = body.model_copy(update={"system_prompt": model_hint})
    db = await _db(request)
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

_DEMO_FILE_CONTENT: dict[str, dict[str, str]] = {
    "backend-api-v2": {
        "AGENTS.md": """\
# backend-api-v2 — Agent Session Instructions

## Connect to Meridian

Run `start_session(project_id="...", session_name="describe-what-youre-doing")` at session start.
This returns your sprint items, active sessions, and goal in one call.

## Project context

REST API in Python (FastAPI) + Postgres. Rate limiting via Redis token bucket.
API key auth: `mk_live_` / `mk_test_` prefixed keys, only the hash stored.

## Session rules

- `log_task(session_id, project_id, desc)` after each meaningful task
- `pin_decision(...)` for architectural choices
- `request_hitl(...)` when blocked on a human decision
- `checkpoint(...)` before ending

## Common gotchas

- Use `%s` placeholders in psycopg3 queries, never `?`
- Rate limiter tests: fakeredis for unit tests, real Redis for nightly suite
- API keys: always hash before storage, never log raw key value
- Migrations in `alembic/` — never edit schema directly in prod
""",
        "ROADMAP.md": """\
# backend-api-v2 Roadmap

## v1.2 (current sprint)
- [x] Redis token bucket rate limiting
- [x] POST /v1/api-keys — issue API keys
- [x] DELETE /v1/api-keys/{id} — revoke keys
- [x] OpenAPI spec generation + Redoc UI at /docs
- [x] Integration test suite (47 tests)
- [ ] TypeScript client SDK from OpenAPI spec
- [ ] Python client SDK from OpenAPI spec
- [ ] Load test rate limiter at 10k req/min

## v1.3
- [ ] Outbound webhooks — POST on entity change
- [ ] Event streaming via SSE for live updates

## v1.4
- [ ] Multi-tenant isolation (schema-per-tenant)
""",
        "DEVLOG.md": """\
# backend-api-v2 Dev Log

## 2026-06-04
**Bob** — Finished POST /v1/api-keys and DELETE revoke endpoint. All tests passing.
Worker 2 started TypeScript SDK generation from OpenAPI spec.

## 2026-06-03
**Alice** — Unblocked Bob's API keys PR — resolved merge conflict with worker 1's OpenAPI changes.
**Worker 1** — Merged api_keys endpoints into spec. 47 integration tests all green.

## 2026-05-28
**Bob** — Rate limiter shipped to staging. 1000 req/min load test holding steady.
Found off-by-one in window reset logic — fixed + test added.

## 2026-05-22
**Alice** — Architecture review: cursor pagination wins over offset.
URL versioning (/v1/) confirmed. Reviewed Bob's rate limiter PR.
Redis connection pool leak on exception path — fixed.
""",
        "CLAUDE.md": """\
# backend-api-v2 — Claude Session Instructions

## Meridian project ID
`PROJECT_ID=25946a49-2dc1-4c9f-bff7-e18827b391c7`

## Stack
Python 3.12, FastAPI, psycopg3 (asyncpg removed), Redis, Alembic migrations.
CI: GitHub Actions — tests + lint on PR, Docker build on merge.

## Key rules
- `%s` placeholders only in SQL — never `?`
- API key generation: 32-byte base64url, prefix `mk_live_`/`mk_test_`, store hash only
- Rate limiter: Redis EVAL scripts for atomic token bucket operations
- All endpoints under `/v1/` prefix

## Before pushing
Run `pytest -x` — all 47 integration tests must pass.
""",
        "README.md": """\
# backend-api-v2

Fast, observable REST API — rate limiting, API key auth, full OpenAPI spec.

## Quick start

```bash
docker compose up        # Postgres + Redis
uvicorn main:app --reload
```

Docs: http://localhost:8000/docs

## Auth

Issue an API key: `POST /v1/api-keys`
Authenticate: `Authorization: Bearer mk_live_...`

## Rate limits

100 req/min per API key.
Response headers: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`.
""",
        "DECISIONS.md": """\
# backend-api-v2 — Architectural Decisions

## Postgres over MySQL [2026-05-05]
Better JSON operators, pgvector for future embedding search, existing team familiarity.

## API versioning via URL prefix, not Accept header [2026-05-07]
/v1/ is visible in logs, easy to route in nginx, simpler for SDK consumers.

## Redis token bucket for rate limiting [2026-05-28]
Survives deploys, consistent across instances. In-memory rejected — state lost on restart.

## API keys prefixed mk_live_ / mk_test_ [2026-05-30]
Stripe-style prefixes identifiable in logs and support tickets.
Raw key shown once on creation, only hash stored. 32-byte base64url = 256 bits entropy.

## Cursor-based pagination over offset [2026-05-22]
Offset pagination breaks when rows are inserted during traversal.
Cursor (created_at + id) is stable under concurrent writes. Default 20, max 100.
""",
    },
    "data-pipeline": {
        "AGENTS.md": """\
# data-pipeline — Agent Session Instructions

## Connect to Meridian

Run `start_session(project_id="...", session_name="describe-what-youre-doing")` at session start.

## Project context

Kafka + Flink + Iceberg ETL pipeline ingesting 50M events/day from 8 sources.
Avro schemas with Confluent Schema Registry. Dead Letter Queue per source.

## Session rules

- `log_task(session_id, project_id, desc)` after each meaningful task
- `pin_decision(...)` for schema or architecture choices
- `request_hitl(...)` for any schema evolution change touching production consumers
- `checkpoint(...)` before ending

## Critical: schema changes

Any change to a registered Avro schema must go through HITL in Meridian before applying.
Incompatible changes break consumers — caught at Schema Registry only if FORWARD_TRANSITIVE is enforced.
""",
        "ROADMAP.md": """\
# data-pipeline Roadmap

## v0.3 (current sprint)
- [x] Dead Letter Queue per source topic
- [x] DLQ replay tooling (scripts/dlq_replay.py)
- [x] Consumer auto-scaling (HPA at 50k lag)
- [x] PagerDuty alerts: DLQ spike, consumer lag, disk
- [x] Iceberg table compaction nightly job
- [x] E2E test suite (34 tests)
- [x] On-call runbook — all alert types documented
- [ ] Load test: 50M events/day sustained simulation

## v0.4
- [ ] Schema evolution: backward compatibility CI gate
- [ ] Data lineage tracking (OpenLineage)
- [ ] Cost dashboard: Kafka + compute + storage by source
- [ ] DLQ self-service replay UI

## v0.5
- [ ] Debezium CDC for Postgres change data capture
""",
        "DEVLOG.md": """\
# data-pipeline Dev Log

## 2026-06-04
**Jordan** — Updated on-call runbooks for DLQ, Kafka broker failure, schema registry outage.
**Worker 1** — E2E test suite: 34 tests, all 8 sources covered. Load test running now (48M/day, investigating).
**Worker 2** — Snapshot expiry policy live: 30 snapshots retained, storage costs projected -60%.

## 2026-06-03
**Jordan** — PagerDuty alerts wired: DLQ spike, consumer lag >100k, broker disk >80%.
Silence rules: IoT sensor source excluded during 02:00-04:00 maintenance window.
**Worker 2** — Iceberg compaction live. Query latency -40% on 30-day lookbacks.

## 2026-05-30
**Maya** — Fixed consumer offset commit race on rebalance. Was causing duplicate processing at partition handoff.
Consumer auto-scaling live: HPA triggers above 50k lag, tested under 3x load.

## 2026-05-28
**Jordan** — DLQ consumer: logs error, emits metric, archives to S3 Parquet. Replay tooling complete.
Stress test: 10k bad events injected, all landed in DLQ, zero main pipeline impact.

## 2026-05-28
**Maya** — Schema Registry deployed. All 8 sources registered with Avro schemas. FORWARD_TRANSITIVE policy set.
""",
        "CLAUDE.md": """\
# data-pipeline — Claude Session Instructions

## Meridian project ID
`PROJECT_ID=<see-dashboard>`

## Stack
Apache Kafka (3-broker, k8s), Confluent Schema Registry (Avro),
Apache Flink (streaming, 5-min tumbling windows), Apache Iceberg + S3,
Python consumers (Faust), PagerDuty, Grafana.

## Key rules
- Schema changes: ALWAYS request_hitl before modifying a registered schema
- DLQ topics: `{source}-dlq` — never publish to main topic from replay without HITL
- Flink: periodic 1-second watermarks, 2-minute out-of-orderness bound
- Consumer offsets: commit after successful processing only — never auto-commit

## Testing
`pytest tests/` for unit tests.
E2E tests in `tests/e2e/` require full Kafka + Flink + Iceberg stack running.
""",
        "README.md": """\
# data-pipeline

ETL pipeline ingesting 50M events/day from 8 sources. Kafka → Flink → Iceberg.

## Architecture

```
8 sources → Kafka topics → Avro validation → Flink windowing → Iceberg tables
                                ↓
                         Dead Letter Queue → S3 Parquet archive
```

## Local dev

```bash
docker compose up  # Kafka, Schema Registry, Flink, MinIO (S3-compatible)
python -m consumers.clickstream
```

## Key metrics

- 50M events/day throughput target
- p99 end-to-end latency: 4m12s (target: <5 min)
- On-call burden target: <2 pages/week
""",
        "DECISIONS.md": """\
# data-pipeline — Architectural Decisions

## Avro over Protobuf for schema serialization [2026-05-14]
Avro integrates natively with Confluent Schema Registry. Protobuf requires cross-team code generation.

## Flink over Spark Streaming for windowing [2026-05-22]
Flink true streaming (not micro-batch). Lower latency, better watermark handling.
Spark Streaming prototyped: 3x higher p99 latency, rejected.

## Iceberg over Delta Lake and Hudi for table format [2026-05-22]
Best Flink native connector. Partition evolution without rewrite. PyIceberg for Python consumers.

## Dead Letter Queue per source, not per error type [2026-05-30]
Per-source DLQs easier to operate — on-call knows which team owns each queue.

## FORWARD_TRANSITIVE schema compatibility policy [2026-05-28]
Writers must be backward compatible; readers handle forward compatible changes.
Breaks caught at Schema Registry registration time.

## PagerDuty over OpsGenie for alerting [2026-06-02]
Company standard, existing on-call rotation, Grafana integration already set up.
""",
    },
}


@app.get("/projects/{project_id}/files")
async def list_project_files(
    project_id: str, request: Request
) -> list[str]:
    """Return the list of editable markdown files for a project.

    Files that do not yet exist on disk are still listed so the user can
    create them from the dashboard. 403 is raised if the project is unknown.
    """
    project = await db_module.get_project(await _db(request), project_id)
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
    In demo mode, returns realistic fake content keyed by project name.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if filename not in _EDITABLE_FILES:
        raise HTTPException(status_code=403, detail="file not in allow-list")
    from fastapi.responses import JSONResponse
    env_demo = os.environ.get("MERIDIAN_DEMO", "").lower() in ("1", "true", "yes")
    cookie_demo = bool(request.cookies.get(_DEMO_CONTEXT_COOKIE))
    if env_demo or cookie_demo:
        proj_name = project.get("name", "")
        demo_files = _DEMO_FILE_CONTENT.get(proj_name, {})
        content = demo_files.get(filename, "")
        return JSONResponse(content={"filename": filename, "content": content}, headers={"Content-Type": "application/json; charset=utf-8"})
    path = _REPO_ROOT / filename
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    return JSONResponse(content={"filename": filename, "content": content}, headers={"Content-Type": "application/json; charset=utf-8"})


@app.put("/projects/{project_id}/files/{filename}")
async def put_project_file(
    project_id: str, filename: str, body: FileContent, request: Request
) -> dict[str, object]:
    """Write content to one editable markdown file.

    Creates the file if it does not exist. 403 if the filename is not in the
    allow-list. Returns ``{"filename": ..., "size": <bytes>}``.
    """
    project = await db_module.get_project(await _db(request), project_id)
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
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    goal = await db_module.get_goal(await _db(request), project_id)
    sprint_items = await db_module.get_sprint_items(
        await _db(request), project_id, status="pending"
    )
    all_tasks = await db_module.get_tasks(await _db(request), project_id, limit=20)
    pending_tasks = [
        t for t in all_tasks if t.get("status") in ("pending", "in_progress", "done")
    ][:10]
    sessions = await db_module.get_sessions(
        await _db(request), project_id, active_only=True
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
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    goal = await db_module.get_goal(await _db(request), project_id)
    sprint_items = await db_module.get_sprint_items(
        await _db(request), project_id, status="pending", show_blocked=False
    )
    all_tasks = await db_module.get_tasks(await _db(request), project_id, limit=20)
    pending_tasks = [t for t in all_tasks if t["status"] in ("pending", "in_progress")][:10]
    sessions = await db_module.get_sessions(await _db(request), project_id, active_only=True)
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


# Decisions routes → meridian/routes/decisions.py
# ---------------------------------------------------------------------------
# v2.4 — HITL (human-in-the-loop) queue
# ---------------------------------------------------------------------------


# HITL routes → meridian/routes/hitl.py
# Notes routes → meridian/routes/notes.py


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
    return await db_module.get_team_summary(await _db(request), project_id, days)


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
    db = await _db(request)
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
    token = await db_module.ensure_project_token(await _db(request), project_id)
    if token is None:
        raise HTTPException(status_code=404, detail="project not found")
    return {"project_id": project_id, "token": token}


# ---------------------------------------------------------------------------
# v3.0 — Executor runs
# ---------------------------------------------------------------------------


@app.get("/projects/{project_id}/search")
async def search_project_all(
    project_id: str,
    request: Request,
    q: str = "",
    limit: int = 10,
) -> dict[str, Any]:
    """Universal search across tasks, notes, decisions, and sprint items."""
    db = await _db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if not q.strip():
        return {"query": q, "tasks": [], "notes": [], "decisions": [], "sprint_items": [], "total": 0}
    return await db_module.search_all(db, project_id, q.strip(), limit=limit)


@app.get("/projects/{project_id}/runs")
async def get_project_runs(
    project_id: str,
    request: Request,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List executor_runs for a project, newest first."""
    db = await _db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    runs = await db_module.get_executor_runs(db, project_id, limit=limit)
    for run in runs:
        if run.get("started_at") and run.get("ended_at"):
            from datetime import datetime
            try:
                fmt = "%Y-%m-%d %H:%M:%S"
                start = datetime.strptime(run["started_at"], fmt)
                end = datetime.strptime(run["ended_at"], fmt)
                run["duration_s"] = int((end - start).total_seconds())
            except Exception:
                run["duration_s"] = None
        else:
            run["duration_s"] = None
    return runs


@app.get("/projects/{project_id}/runs/{run_id}")
async def get_project_run(
    project_id: str,
    run_id: str,
    request: Request,
) -> dict[str, Any]:
    """Return a single executor_run with full transcript."""
    db = await _db(request)
    run = await db_module.get_executor_run(db, run_id)
    if run is None or run.get("project_id") != project_id:
        raise HTTPException(status_code=404, detail="run not found")
    return run


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
    from .hosted import get_current_tenant, is_admin_db, check_admin_password

    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        return HTMLResponse(
            '<meta http-equiv="refresh" content="0;url=/auth/login">',
            status_code=302,
        )

    if not await is_admin_db(tenant.get("email", ""), request.app.state.db):
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
    prefs = _notification_prefs_from_raw(body)
    await db_module.update_tenant(
        request.app.state.db, tenant["id"], notification_prefs=json.dumps(prefs)
    )
    return {"status": "ok", "prefs": prefs}


@app.get("/settings/notifications")
async def get_notification_prefs(request: Request) -> dict[str, Any]:
    """v2.5 — return current notification preferences for the authenticated tenant."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from .hosted import get_current_tenant
    tenant = await get_current_tenant(request)
    prefs = _notification_prefs_from_raw(tenant.get("notification_prefs"))
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
    db = request.app.state.db
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
    db = request.app.state.db
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
    members = await db_module.list_workspace_members(request.app.state.db, tenant["id"])
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
    await db_module.delete_workspace_member(request.app.state.db, member_id, tenant["id"])


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
    data = await db_module.export_tenant_data(request.app.state.db, tenant["id"])
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
    await db_module.delete_tenant_records(request.app.state.db, tenant["id"])

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
    projects = await db_module.list_projects(await _db(request))
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
        request.app.state.db, tenant["id"],
        compute_overage_cap_usd=compute_cap,
        storage_overage_cap_usd=storage_cap,
    )
    return {"status": "ok", "compute_cap": compute_cap, "storage_cap": storage_cap}


# ---------------------------------------------------------------------------
# GitHub integration endpoints (hosted-tier only)
# ---------------------------------------------------------------------------

@app.post("/projects/{project_id}/github/connect")
async def github_connect(project_id: str, request: Request) -> dict[str, Any]:
    """Validate a GitHub PAT and store it encrypted on the tenant."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    import httpx as _httpx
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    body = await request.json()
    pat = (body.get("pat") or "").strip()
    repo = (body.get("repo") or "").strip()
    branch = (body.get("branch") or "main").strip()
    if not pat:
        raise HTTPException(status_code=422, detail="pat is required")
    if not repo or "/" not in repo:
        raise HTTPException(status_code=422, detail="repo must be owner/repo format")
    # Validate PAT against GitHub API
    try:
        async with _httpx.AsyncClient(timeout=10.0) as http:
            r = await http.get(
                "https://api.github.com/user",
                headers={"Authorization": f"token {pat}", "Accept": "application/vnd.github+json"},
            )
        if r.status_code == 401:
            raise HTTPException(status_code=422, detail="GitHub PAT is invalid or expired")
        if r.status_code != 200:
            raise HTTPException(status_code=422, detail=f"GitHub API error: {r.status_code}")
        gh_user = r.json().get("login", "")
    except _httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach GitHub: {exc}") from exc
    await db_module.update_tenant(
        request.app.state.db, tenant["id"],
        github_pat=db_module.encrypt_field(pat),
        github_repo=repo,
        github_branch=branch,
    )
    return {"connected": True, "repo": repo, "branch": branch, "github_user": gh_user}


@app.get("/projects/{project_id}/github/status")
async def github_status(project_id: str, request: Request) -> dict[str, Any]:
    """Return the tenant's current GitHub connection status."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    # Re-fetch from auth DB to get latest values
    fresh = await db_module.get_tenant_by_id(request.app.state.db, tenant["id"])
    if not fresh:
        raise HTTPException(status_code=404, detail="tenant not found")
    connected = bool(fresh.get("github_pat"))
    return {
        "connected": connected,
        "repo": fresh.get("github_repo") or "",
        "branch": fresh.get("github_branch") or "main",
        "last_verified": None,
    }


@app.delete("/projects/{project_id}/github/disconnect", status_code=200)
async def github_disconnect(project_id: str, request: Request) -> dict[str, Any]:
    """Clear the tenant's stored GitHub credentials."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    await db_module.update_tenant(
        request.app.state.db, tenant["id"],
        github_pat=None,
        github_repo=None,
        github_branch=None,
    )
    return {"disconnected": True}


@app.get("/mcp/quickstart", response_class=PlainTextResponse)
async def mcp_quickstart() -> str:
    """One-page MCP quick reference — the 5 tools you use 90% of the time.

    Returns plain text cheat sheet, suitable for pasting into a new chat session
    or displaying in the dashboard Settings tab.
    """
    tool_count = len(_MCP_TOOLS_LIST)
    return f"""\
# Meridian MCP — Quick Reference

The 5 tools you use 90% of the time:

| Tool | One-liner | Example |
|------|-----------|---------|
| start_session | Register session, get full project context | start_session(project_id="abc-123", session_name="feature-x") |
| log_task | Record completed work | log_task(session_id="sid", project_id="abc-123", description="Fixed OAuth redirect") |
| checkpoint | Snapshot: auto-capture + delta handoff + next /goal | checkpoint(session_id="sid", project_id="abc-123") |
| pin_decision | Add to live constitution | pin_decision(project_id="abc-123", title="Use psycopg3", body="asyncpg has DLL issues on Windows", category="TECHNICAL") |
| request_hitl | Surface blocking question to human | request_hitl(project_id="abc-123", question="Rate-limit per IP or per token?", urgency="blocking") |

## Session lifecycle

  list_projects()        ← first, if you don't know the project_id
  start_session()       ← always first
  log_task()            ← after any meaningful work
  pin_decision()        ← for architectural choices
  request_hitl()        ← when you need a human call
  checkpoint()          ← before ending / before context fills

## Auto-hooks (recommended)

Wire Claude Code / Codex to call these automatically:

  Mac/Linux:  curl -fsSL https://usemeridian.us/hooks.sh | bash
  Windows:    irm https://usemeridian.us/hooks.ps1 | iex

## Full tool reference

  GET /mcp/tools-doc     — complete markdown reference ({tool_count} tools)
  https://docs.usemeridian.us
"""


@app.get("/mcp/tools-doc", response_class=PlainTextResponse)
async def mcp_tools_doc() -> str:
    """Generate organized markdown MCP tool reference."""
    tool_map = {t["name"]: t for t in _MCP_TOOLS_LIST}

    def _render_tool(name: str, override_desc: str | None = None) -> list[str]:
        tool = tool_map.get(name)
        if not tool:
            return []
        out = [f"\n### `{name}`\n"]
        desc = override_desc or tool.get("description", "")
        out.append(f"{desc}\n")
        props = (tool.get("inputSchema") or {}).get("properties") or {}
        required = set((tool.get("inputSchema") or {}).get("required") or [])
        if props:
            out.append("\n| Parameter | Type | Required | Description |")
            out.append("|-----------|------|----------|-------------|")
            for k, v in props.items():
                req = "required" if k in required else "optional"
                desc_col = (v.get("description") or "").replace("|", "\\|")
                out.append(f"| `{k}` | {v.get('type', 'string')} | {req} | {desc_col} |")
        example = _TOOL_EXAMPLES.get(name)
        if example:
            out.append(f"\n**Example:**\n```\n{example}\n```")
        out.append("")
        out.append("---")
        out.append("")
        return out

    n = len(_MCP_TOOLS_LIST)
    lines: list[str] = [
        "# MCP Tool Reference\n",
        f"Meridian exposes **{n} tools** over MCP. They fall into two usage patterns:\n",
        "**Planner sessions** (claude.ai, planning work) — `start_session` · `pin_decision` · `update_decision` · `add_note` · `get_context_block` · `generate_handoff`\n",
        "**Executor sessions** (Claude Code, Cursor, automated workers) — `start_session` · `log_task` · `request_hitl` · `get_session_brief` · `generate_handoff`\n",
        "---\n",
        "## Quick Reference — 5 tools you use 90% of the time\n",
        "| Tool | One-liner | Example call |\n",
        "|------|-----------|-------------|\n",
        "| `start_session` | Register session, get full project context | `start_session(project_id=\"abc-123\", session_name=\"feature-x\", human_id=\"alice\")` |\n",
        "| `log_task` | Record completed work to the shared task log | `log_task(session_id=\"sid\", project_id=\"abc-123\", description=\"Wired OAuth redirect\")` |\n",
        "| `checkpoint` | Snapshot progress: auto-capture + delta handoff + next /goal | `checkpoint(session_id=\"sid\", project_id=\"abc-123\")` |\n",
        "| `pin_decision` | Add an architectural decision to the live constitution | `pin_decision(project_id=\"abc-123\", title=\"Use psycopg3\", body=\"asyncpg has DLL issues on Windows\", category=\"TECHNICAL\")` |\n",
        "| `request_hitl` | Surface a blocking question to the human queue | `request_hitl(project_id=\"abc-123\", question=\"Should we rate-limit per IP or per token?\", urgency=\"blocking\")` |\n",
        "\n> **Tip:** Use `checkpoint()` instead of `generate_handoff()` when ending a session — it also runs `auto_capture` and returns the next `/goal` string.\n",
        "\n---\n",
        "## Starting a session\n",
    ]
    lines += _render_tool("start_session",
        "Register a session and get the full project context (goal, sprint, recent tasks, decisions) in one call. "
        "**Use this instead of `register_session`.**")
    lines += _render_tool("get_session_brief",
        "Compact session orientation (<500 tokens). Returns sprint focus, pending items, recent tasks, blocking "
        "failures, and open HITL requests. Ideal for worker/automation sessions that don't need the full context.")
    lines += ["## Tasks\n"]
    lines += _render_tool("log_task",
        "Log what this session did, is doing, or failed at. Call frequently — this is the primary signal in the "
        "timeline and handoffs.\n\n"
        "Valid statuses: `pending` · `in_progress` · `done` · `failed` · `backlog` · `future` · `backburner`")
    lines += _render_tool("get_tasks")
    lines += _render_tool("search_tasks")
    lines += ["## Goal & sprint\n"]
    lines += _render_tool("get_goal")
    lines += _render_tool("set_goal")
    lines += ["## Executor config & file coordination\n"]
    lines += _render_tool("set_executor_config",
        "Store project-level executor defaults so worker sessions start with repo path, env file, test command, "
        "deploy command, shell, branch, and the injected credentials rule.")
    lines += _render_tool("claim_file",
        "Claim exclusive edit rights on a file path for this session. Locks auto-expire after 2 hours.")
    lines += _render_tool("release_file",
        "Release a file lock held by this session when you're done editing.")
    lines += _render_tool("idle_until_session_done",
        "Wait on another session before touching a shared file. The tool polls every 30 seconds until the watched session is done.")
    lines += ["## Decisions\n"]
    lines += _render_tool("pin_decision",
        "Record an authoritative decision that supersedes earlier statements. Pinned decisions appear in every "
        "session's context block.\n\n"
        "Categories: `STRATEGIC` · `COMPETITIVE` · `TECHNICAL` · `TACTICAL` · `BUSINESS` · `PRODUCT` · `ARCHITECTURAL`")
    lines += _render_tool("update_decision",
        "Patch a pinned decision. Pass `new_title` + `new_body` to atomically supersede (creates a new row, marks "
        "old as superseded). Otherwise patches in place.")
    lines += _render_tool("get_pinned_decisions")
    lines += ["## Human-in-the-loop (HITL)\n"]
    lines += _render_tool("request_hitl",
        "Surface a question to the human queue. `urgency='blocking'` pauses the session until answered — poll "
        "`get_hitl_request` to resume. `normal`/`high` land in the dashboard without blocking.")
    lines += _render_tool("get_hitl_request",
        "Poll a HITL request for the human's answer. Returns the row including `status` "
        "(`pending`/`answered`/`dismissed`) and `answer` text.")
    lines += ["## Handoff & context\n"]
    lines += _render_tool("generate_handoff",
        "Generate a context handoff document. `mode='full'` writes the complete L0/L1/L2 handoff. `mode='delta'` "
        "returns a compact session summary with completed items, pending items, and the next `/goal` string.")
    lines += _render_tool("get_context_block",
        "Return a compact plain-text context block (north star, sprint, pending sprint items, recent tasks, recent "
        "decisions, active sessions). Use `mode='full'` to paste into a fresh Claude Code session; `mode='chat'` "
        "for a shorter paste into claude.ai.")
    lines += ["## Notes\n"]
    lines += _render_tool("add_note",
        "Add a per-project wiki note. Use for setup instructions, gotchas, environment details, how-tos.")
    lines += _render_tool("get_notes", "List project notes (newest first). Filter by tag substring.")
    lines += _render_tool("delete_note")
    lines += ["## Projects\n"]
    lines += _render_tool("create_project")
    lines += ["## Legacy\n"]
    lines += _render_tool("register_session",
        "!!! note \"Deprecated\"\n    Use `start_session` instead — it registers the session **and** returns "
        "goal + context in one call.")
    # strip trailing ---
    while lines and lines[-1].strip() in ("---", ""):
        lines.pop()
    return "\n".join(part.rstrip("\n") for part in lines).rstrip() + "\n"

# /admin/health, /admin/git-status → meridian/routes/admin.py


# DELETE /tasks/{task_id} → meridian/routes/tasks.py

@app.post("/tasks/enqueue", response_model=Task, status_code=202)
async def enqueue_task(body: EnqueueTask, request: Request) -> dict[str, Any]:
    """Paid-tier: queue a Claude subprocess and return the pending task row.

    Responds with 202 Accepted so clients can distinguish this from a
    synchronous task creation. The worker runs in the background; poll
    ``GET /projects/{id}/tasks`` to see the result land.
    """
    _req_db = await _db(request)
    project = await db_module.get_project(_req_db, body.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    async with _req_db.execute(
        "SELECT id FROM sessions WHERE id = ?", (body.session_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return await enqueue_module.enqueue_claude_task(
        _req_db,
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


async def _load_executor_session_context(
    db: aiosqlite.Connection,
    project_id: str,
) -> tuple[dict[str, Any], str]:
    """Return normalized executor config plus the injected context block."""
    settings = await db_module.get_project_settings(db, project_id)
    raw_config = (settings or {}).get("executor_config") if settings else None
    return executor_config_for_output(raw_config), build_executor_config_block(raw_config)


async def _idle_until_session_done(
    db: aiosqlite.Connection,
    session_id: str,
    *,
    poll_seconds: int = 30,
) -> dict[str, Any]:
    """Poll every 30 seconds until the target session is closed/archived."""
    while True:
        async with db.execute(
            "SELECT id, status, project_id, name, human_id, last_seen "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return {"session_id": session_id, "done": True, "status": "missing"}
        session = row if isinstance(row, dict) else {k: row[k] for k in row.keys()}
        status_val = session.get("status") or "missing"
        if status_val in {"closed", "archived"}:
            return {
                "session_id": session_id,
                "done": True,
                "status": status_val,
                "project_id": session.get("project_id"),
                "name": session.get("name"),
                "human_id": session.get("human_id"),
                "last_seen": session.get("last_seen"),
            }
        await asyncio.sleep(max(1, poll_seconds))


async def _start_session_composite(
    db: aiosqlite.Connection,
    project_id: str,
    session_name: str,
    data_dir: str,
    human_id: str | None = None,
    client_type: str | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    """Register + goal + tasks + sessions + handoff-check in one shot.

    Replaces the four-call cold-start sequence (register_session, get_goal,
    get_tasks, check handoff file) with a single call that returns everything
    a new session needs before touching anything.
    """
    # v1.8.x — archive sessions silent for 7+ days so they don't crowd
    # the active list seen by new sessions.
    await db_module.archive_empty_sessions(db)
    await db_module.archive_stale_sessions(db, project_id)
    try:
        await db_module.expire_file_locks(db)
    except Exception:
        pass

    session = await db_module.register_session(
        db, project_id, session_name, human_id=human_id, client_type=client_type
    )
    try:
        await db_module.create_executor_run(db, session["id"], project_id)
    except Exception:
        pass
    released_stale_claims = await db_module.release_stale_task_claims(
        db,
        project_id,
        exclude_session_id=session["id"],
        max_age_hours=2,
    )
    if released_stale_claims:
        noun = "claim" if released_stale_claims == 1 else "claims"
        await db_module.log_task(
            db,
            session["id"],
            project_id,
            f"Auto-released {released_stale_claims} stale {noun} older than 2 hours from previous sessions.",
            "done",
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

    # v3.1 — executor sessions get their project-level config injected so they
    # can use repo_path, test_cmd, etc. without manual lookup.
    executor_config: dict[str, Any] | None = None
    executor_context: str | None = None
    if role == "executor":
        try:
            executor_config, executor_context = await _load_executor_session_context(
                db, project_id
            )
        except Exception:
            executor_config = executor_config_for_output({})
            executor_context = build_executor_config_block({})

    payload: dict[str, Any] = {
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
    if executor_config is not None:
        payload["executor_config"] = executor_config
        payload["executor_context"] = executor_context
        payload["role"] = "executor"
    return payload


@app.post("/projects/{project_id}/start-session")
async def start_session_endpoint(
    project_id: str, body: StartSessionRequest, request: Request
) -> dict[str, Any]:
    """v0.4.4 — one call to start a coordinated session.

    Registers the caller, fetches goal + ambient tasks, fetches the last 10
    tasks, lists active sessions, and reports whether a handoff file already
    exists on disk. Replaces 4 separate MCP calls at session cold-start.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await _start_session_composite(
        await _db(request),
        project_id,
        body.session_name,
        _data_dir(request),
        human_id=body.human_id,
        client_type=body.client,
        role=body.role,
    )


# ---------------------------------------------------------------------------
# Admin — shutdown
# ---------------------------------------------------------------------------


# /admin/shutdown + /admin/restart → meridian/routes/admin.py


# /admin/snapshot → meridian/routes/admin.py


# ---------------------------------------------------------------------------
# Hooks - Claude Code / Codex session lifecycle endpoints
# ---------------------------------------------------------------------------


async def _resolve_hook_db(request: Request) -> Any:
    """Resolve the DB for hook routes.

    If the request carries an explicit Bearer token, always use that tenant and
    fail fast on invalid tokens instead of silently falling back to the shared
    auth/local DB. If no Bearer token is present, preserve the normal _db()
    behavior so browser-session and self-hosted flows keep working unchanged.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return await _db(request)

    raw_token = auth_header[len("Bearer "):].strip()
    if not raw_token:
        raise HTTPException(status_code=401, detail="Bearer token required")

    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    auth_db = request.app.state.db
    tenant = await db_module.get_tenant_from_token_hash(auth_db, token_hash)
    if tenant is None:
        raise HTTPException(status_code=401, detail="invalid API token")

    conn = await _open_tenant_db_by_id(request, tenant["id"])
    request.state._db_conn = conn
    return conn


@app.post("/hooks/session-start")
async def hooks_session_start(body: dict[str, Any], request: Request) -> dict[str, Any]:
    """Claude Code / Codex SessionStart hook.

    Accepts {project_id, session_name?}. Calls start_session() and returns
    {hookSpecificOutput: {additionalContext: "..."}} so Claude Code injects
    the project context into the agent's initial context window automatically.

    Hosted callers can authenticate with Authorization: Bearer sk_meridian_...
    to route directly to their tenant DB. Local/browser-session behavior is
    unchanged when no Bearer token is supplied.
    """
    project_id = (body.get("project_id") or "").strip()
    if not project_id:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={"error": "project_id required"})
    session_name = (body.get("session_name") or "hook-session").strip()
    db = await _resolve_hook_db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"error": "project not found"})
    result = await _start_session_composite(
        db, project_id, session_name, _data_dir(request),
        human_id=body.get("human_id"),
        client_type="hook",
    )
    goal = result.get("goal") or {}
    sprint_items = await db_module.get_sprint_items(db, project_id, status="pending")
    recent = result.get("recent_tasks") or []
    lines = [f"PROJECT: {project['name']} ({project_id[:8]})"]
    if goal.get("north_star"):
        lines.append(f"NORTH STAR: {goal['north_star'][:300]}")
    if goal.get("sprint"):
        lines.append(f"SPRINT: {goal['sprint'][:300]}")
    if sprint_items:
        lines.append(f"\nPENDING SPRINT ITEMS ({len(sprint_items)}):")
        for it in sprint_items[:8]:
            lines.append(f"- {it.get('id', '')[:8]} {it.get('title', '')[:120]}")
    if recent:
        lines.append("\nRECENT TASKS:")
        for t in recent[:5]:
            lines.append(f"- [{t.get('status','?').upper()}] {str(t.get('description',''))[:120]}")
    lines.append(f"\nSESSION ID: {result.get('session_id', '')}")
    additional_context = "\n".join(lines)
    return {"hookSpecificOutput": {"additionalContext": additional_context}}


@app.post("/hooks/stop")
async def hooks_stop(body: dict[str, Any], request: Request) -> dict[str, Any]:
    """Claude Code / Codex Stop hook.

    Accepts {project_id, session_id?}. Fires auto_capture + delta handoff
    and returns immediately. Fire-and-forget — the agent does not wait for
    this to complete before exiting.

    Hosted callers can authenticate with Authorization: Bearer sk_meridian_...
    to route directly to their tenant DB. Local/browser-session behavior is
    unchanged when no Bearer token is supplied.
    """
    project_id = (body.get("project_id") or "").strip()
    session_id = (body.get("session_id") or "").strip() or None
    if not project_id:
        return {"ok": False, "error": "project_id required"}
    db = await _resolve_hook_db(request)
    if session_id:
        try:
            await db_module.auto_capture_session(db, project_id, session_id)
        except Exception:  # noqa: BLE001
            pass
    try:
        from . import handoff as handoff_module_local
        await asyncio.wait_for(
            handoff_module_local.generate_handoff(
                db, project_id, _data_dir(request), mode="delta", session_id=session_id
            ),
            timeout=20.0,
        )
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True}


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
    plan = (body.get("plan") or "standard").strip()
    source = (body.get("source") or "landing").strip()
    note_parts = []
    if body.get("note"):
        note_parts.append(body["note"].strip())
    note_parts.append(f"plan:{plan} source:{source}")
    note = " ".join(note_parts) if note_parts else None
    db = await _db(request)
    try:
        entry = await db_module.add_waitlist_entry(db, email, note)
    except Exception as exc:
        if "UNIQUE" in str(exc) or "unique" in str(exc):
            raise HTTPException(status_code=409, detail="email already on waitlist")
        raise
    # Fire-and-forget confirmation email — never block the response
    if _hosted_mode():
        try:
            from .hosted import send_waitlist_confirmation_email  # noqa: PLC0415
            asyncio.create_task(send_waitlist_confirmation_email(email))
        except Exception:
            pass
    return entry


@app.get("/waitlist")
async def list_waitlist(request: Request) -> list[dict[str, Any]]:
    """GET all waitlist entries, newest first. Admin use only."""
    db = request.app.state.db
    return await db_module.get_waitlist(db)


@app.get("/admin/waitlist", response_class=HTMLResponse)
async def admin_waitlist_page(request: Request) -> HTMLResponse:
    """Admin waitlist management page — shows signups, tenant stats, approve/delete buttons."""
    from .hosted import get_current_tenant, is_admin_db  # noqa: PLC0415

    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        return HTMLResponse("<h1>403</h1><p>Not authenticated.</p>", status_code=403)
    if not await is_admin_db(tenant.get("email", ""), request.app.state.db):
        return HTMLResponse("<h1>403</h1><p>Admin only.</p>", status_code=403)

    db = request.app.state.db
    entries = await db_module.get_waitlist(db)

    async def _count(sql: str) -> int:
        async with db.execute(sql) as cur:
            row = await cur.fetchone()
        return (row[0] if row else 0) or 0

    total_tenants = await _count("SELECT COUNT(*) FROM tenants")
    free_tenants = await _count("SELECT COUNT(*) FROM tenants WHERE plan='free'")
    paid_tenants = await _count("SELECT COUNT(*) FROM tenants WHERE plan NOT IN ('free','') AND plan IS NOT NULL")

    rows_html = "".join(
        f"""<tr>
          <td style="padding:6px 10px;border-bottom:1px solid #2a2d35">{html_module.escape(e.get("email",""))}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #2a2d35;color:#8b8fa8;font-size:11px">{html_module.escape((e.get("created_at") or "")[:16])}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #2a2d35;color:#8b8fa8;font-size:11px">{html_module.escape(e.get("note") or "")}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #2a2d35">
            <button onclick="delWL('{html_module.escape(e.get('id',''))}',this)" style="background:#2a0f0f;border:1px solid #5a1a1a;color:#e05252;border-radius:3px;padding:2px 8px;font-size:10px;cursor:pointer">Delete</button>
          </td>
        </tr>"""
        for e in entries
    ) or "<tr><td colspan='4' style='padding:16px;text-align:center;color:#8b8fa8'>No waitlist entries.</td></tr>"

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Admin — Waitlist — Meridian</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d0d0f;color:#e8eaf0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:32px 24px}}
h1{{font-size:1.4rem;margin-bottom:8px}}nav a{{color:#6c8fff;text-decoration:none;font-size:13px;margin-right:16px}}
.stats{{display:flex;gap:16px;margin:20px 0}}
.stat{{background:#16181c;border:1px solid #2a2d35;border-radius:8px;padding:12px 18px;min-width:120px}}
.stat .n{{font-size:1.6rem;font-weight:700;color:#6c8fff}}.stat .l{{font-size:11px;color:#8b8fa8;margin-top:2px}}
table{{width:100%;border-collapse:collapse;background:#16181c;border:1px solid #2a2d35;border-radius:8px;overflow:hidden;margin-top:16px}}
th{{padding:8px 10px;text-align:left;background:#1e2029;font-size:11px;color:#8b8fa8;border-bottom:1px solid #2a2d35}}
tr:hover td{{background:#1a1c23}}
</style>
</head>
<body>
<nav><a href="/dashboard">← Dashboard</a> <a href="/admin/health">Health</a></nav>
<h1 style="margin-top:16px">Waitlist Management</h1>
<p style="color:#8b8fa8;font-size:13px;margin-top:4px">{len(entries)} total signup{"s" if len(entries)!=1 else ""}</p>
<div class="stats">
  <div class="stat"><div class="n">{len(entries)}</div><div class="l">Waitlist</div></div>
  <div class="stat"><div class="n">{total_tenants}</div><div class="l">Total Tenants</div></div>
  <div class="stat"><div class="n">{free_tenants}</div><div class="l">Free Plan</div></div>
  <div class="stat"><div class="n">{paid_tenants}</div><div class="l">Paid Plan</div></div>
</div>
<table>
<thead><tr><th>Email</th><th>Signed Up</th><th>Note</th><th>Action</th></tr></thead>
<tbody id="wl-body">{rows_html}</tbody>
</table>
<script>
async function delWL(id, btn) {{
  if (!confirm('Delete this waitlist entry?')) return;
  const r = await fetch('/admin/waitlist/' + id, {{method:'DELETE'}});
  if (r.ok) {{ btn.closest('tr').remove(); }} else {{ alert('Failed: ' + r.status); }}
}}
</script>
</body></html>"""
    return HTMLResponse(html)


@app.delete("/admin/waitlist/{entry_id}")
async def admin_delete_waitlist_entry(entry_id: str, request: Request) -> dict[str, Any]:
    """Delete a waitlist entry by id. Admin only."""
    from .hosted import get_current_tenant, is_admin_db  # noqa: PLC0415

    try:
        tenant = await get_current_tenant(request)
    except HTTPException:
        raise HTTPException(status_code=403, detail="not authenticated")
    if not await is_admin_db(tenant.get("email", ""), request.app.state.db):
        raise HTTPException(status_code=403, detail="admin only")
    db = request.app.state.db
    await db.execute("DELETE FROM waitlist WHERE id = ?", (entry_id,))
    await db.commit()
    return {"deleted": True, "id": entry_id}


@app.get("/waitlist-pending")
async def waitlist_pending(request: Request) -> HTMLResponse:
    """Landing page for non-admin users who sign in during pre-launch."""
    message = (request.query_params.get("message") or "").strip()
    badge = "Early access is full" if message else "✓ You're on the list"
    heading = "You're on the waitlist" if message else "Thanks for signing up!"
    body = (
        message
        if message
        else "Meridian is in early access. We'll email you when your account is ready."
    )
    html = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>You're on the waitlist — Meridian</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d0f;color:#e8eaf0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#16181c;border:1px solid #2a2d35;border-radius:12px;padding:44px 40px;
  max-width:480px;width:100%;margin:20px;text-align:center}
.logo{font-size:1.3rem;font-weight:700;color:#e8eaf0;margin-bottom:24px}
.logo span{color:#6c8fff}
h1{font-size:1.5rem;font-weight:700;margin-bottom:12px}
p{color:#8b8fa8;font-size:.9rem;line-height:1.6;margin-bottom:16px}
.badge{display:inline-block;background:#1e2029;border:1px solid #2a2d35;border-radius:20px;
  padding:6px 16px;font-size:.8rem;color:#6c8fff;margin-bottom:24px}
a{color:#6c8fff;text-decoration:none}
a:hover{text-decoration:underline}
</style>
</head>
<body>
<div class="card">
  <div class="logo">⬡ <span>Meridian</span></div>
  <div class="badge">__BADGE__</div>
  <h1>__HEADING__</h1>
  <p>__BODY__</p>
  <p>In the meantime, explore the live demo or read the docs.</p>
  <div style="display:flex;gap:10px;justify-content:center;margin-top:20px;flex-wrap:wrap">
    <a href="/" style="display:inline-block;background:#1a1c23;border:1px solid #2a2d35;border-radius:8px;padding:9px 18px;color:#e8eaf0;font-size:.85rem;text-decoration:none">← Back to home</a>
    <a href="/demo" style="display:inline-block;background:#7c3aed;border:none;border-radius:8px;padding:9px 18px;color:#fff;font-size:.85rem;text-decoration:none">→ Try the live demo</a>
    <a href="https://docs.usemeridian.us" target="_blank" style="display:inline-block;background:#1a1c23;border:1px solid #2a2d35;border-radius:8px;padding:9px 18px;color:#e8eaf0;font-size:.85rem;text-decoration:none">Read the docs</a>
  </div>
  <p style="margin-top:24px;font-size:.78rem"><a href="/auth/logout">sign out</a></p>
</div>
</body>
</html>"""
    html = (
        html.replace("__BADGE__", badge)
        .replace("__HEADING__", heading)
        .replace("__BODY__", body)
    )
    return HTMLResponse(html)


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

    db = request.app.state.db

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
    db = request.app.state.db
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
    safe = {k: v for k, v in tenant.items() if k not in ("neon_db_url", "github_pat")}
    safe["github_connected"] = bool(tenant.get("github_pat"))
    return safe


# ---------------------------------------------------------------------------
# v2.0 — Remote MCP endpoint (HTTP JSON-RPC 2.0 transport)
# ---------------------------------------------------------------------------

_MCP_PROTOCOL_VERSION = "2024-11-05"
_MCP_SERVER_INFO = {"name": "meridian", "version": _VERSION}


def _jsonrpc_ok(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_err(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# GitHub MCP tools — injected per-tenant when github_pat is set
# ---------------------------------------------------------------------------

_GITHUB_TOOL_NAMES = frozenset({"read_file", "list_files", "search_code", "git_log", "get_commit"})


def _github_tools_for_tenant(tenant: dict) -> list[dict[str, Any]]:
    """Return the 5 GitHub tool defs if the tenant has a GitHub PAT set."""
    if not db_module.decrypt_field(tenant.get("github_pat")):
        return []
    return [
        {
            "name": "read_file",
            "description": "Read a file from the connected GitHub repository. Returns decoded UTF-8 content.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to repo root (e.g. src/main.py)"},
                    "ref": {"type": "string", "description": "Branch, tag, or commit SHA (default: configured branch)"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "list_files",
            "description": "List all files in the connected GitHub repository (recursive tree).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Subdirectory to list (default: repo root)"},
                },
            },
        },
        {
            "name": "search_code",
            "description": "Search code in the connected GitHub repository using GitHub code search.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query string (GitHub code search syntax)"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "git_log",
            "description": "Return recent commits from the connected GitHub repository.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Number of commits to return (default: 10, max: 50)"},
                },
            },
        },
        {
            "name": "get_commit",
            "description": "Return details for a specific commit from the connected GitHub repository.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sha": {"type": "string", "description": "Full or short commit SHA"},
                },
                "required": ["sha"],
            },
        },
    ]


async def _dispatch_github_tool(name: str, args: dict[str, Any], tenant: dict) -> Any:
    """Dispatch a GitHub MCP tool call using the tenant's stored PAT."""
    import httpx as _httpx
    import base64 as _b64
    pat = db_module.decrypt_field(tenant.get("github_pat"))
    repo = tenant.get("github_repo") or ""
    branch = tenant.get("github_branch") or "main"
    if not pat or not repo:
        return {"error": "GitHub not connected — use POST /projects/{id}/github/connect"}
    gh_headers = {"Authorization": f"token {pat}", "Accept": "application/vnd.github+json"}
    async with _httpx.AsyncClient(timeout=15.0) as http:
        if name == "read_file":
            path = args.get("path", "")
            ref = args.get("ref") or branch
            r = await http.get(
                f"https://api.github.com/repos/{repo}/contents/{path}",
                headers=gh_headers,
                params={"ref": ref},
            )
            if r.status_code == 404:
                return {"error": f"File not found: {path}"}
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                return {"entries": [{"name": e["name"], "type": e["type"], "path": e["path"]} for e in data]}
            content_b64 = data.get("content", "")
            content = _b64.b64decode(content_b64).decode("utf-8", errors="replace")
            return {"path": data["path"], "sha": data["sha"], "size": data["size"], "content": content}

        if name == "list_files":
            path = args.get("path") or ""
            r = await http.get(
                f"https://api.github.com/repos/{repo}/git/trees/HEAD",
                headers=gh_headers,
                params={"recursive": "1"},
            )
            r.raise_for_status()
            tree = r.json().get("tree", [])
            files = [e["path"] for e in tree if e.get("type") == "blob"]
            if path:
                files = [f for f in files if f.startswith(path)]
            return {"repo": repo, "count": len(files), "files": files}

        if name == "search_code":
            query = args.get("query", "")
            r = await http.get(
                "https://api.github.com/search/code",
                headers=gh_headers,
                params={"q": f"{query} repo:{repo}"},
            )
            r.raise_for_status()
            items = r.json().get("items", [])
            return {
                "total_count": r.json().get("total_count", 0),
                "items": [{"path": i["path"], "sha": i["sha"], "url": i.get("html_url", "")} for i in items[:20]],
            }

        if name == "git_log":
            limit = min(int(args.get("limit") or 10), 50)
            r = await http.get(
                f"https://api.github.com/repos/{repo}/commits",
                headers=gh_headers,
                params={"per_page": str(limit)},
            )
            r.raise_for_status()
            commits = r.json()
            return {
                "commits": [
                    {
                        "sha": c["sha"][:12],
                        "message": c["commit"]["message"].split("\n")[0],
                        "author": c["commit"]["author"]["name"],
                        "date": c["commit"]["author"]["date"],
                    }
                    for c in commits
                ]
            }

        if name == "get_commit":
            sha = args.get("sha", "")
            r = await http.get(
                f"https://api.github.com/repos/{repo}/commits/{sha}",
                headers=gh_headers,
            )
            if r.status_code == 404:
                return {"error": f"Commit not found: {sha}"}
            r.raise_for_status()
            c = r.json()
            files = [{"filename": f["filename"], "status": f["status"], "additions": f.get("additions", 0), "deletions": f.get("deletions", 0)} for f in c.get("files", [])[:50]]
            return {
                "sha": c["sha"],
                "message": c["commit"]["message"],
                "author": c["commit"]["author"]["name"],
                "date": c["commit"]["author"]["date"],
                "files_changed": len(c.get("files", [])),
                "files": files,
            }

    return {"error": f"Unknown GitHub tool: {name}"}


async def _handle_mcp_request(
    body: dict[str, Any], db: Any, data_dir: str, tenant: dict[str, Any] | None = None
) -> dict[str, Any]:
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
        tools = list(_MCP_TOOLS_LIST)
        if tenant:
            tools = tools + _github_tools_for_tenant(tenant)
        return _jsonrpc_ok(req_id, {"tools": tools})

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            if name in _GITHUB_TOOL_NAMES and tenant:
                result = await _dispatch_github_tool(name, args, tenant)
            else:
                result = await _dispatch_mcp_tool(name, args, db, data_dir, tenant=tenant)
            return _jsonrpc_ok(req_id, {"content": [{"type": "text", "text": json.dumps(result)}]})
        except Exception as exc:
            return _jsonrpc_err(req_id, -32603, str(exc))

    return _jsonrpc_err(req_id, -32601, f"method not found: {method}")


async def _dispatch_mcp_tool(
    name: str,
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None = None,
) -> Any:
    """Route a tools/call to the appropriate db_module function."""
    if name == "create_project":
        return await db_module.create_project(db, args["name"])
    if name == "register_session":
        return await db_module.register_session(
            db, args["project_id"], args["session_name"],
            args.get("human_id"),
            agent_framework=args.get("agent_framework", "claude_code"),
            client_type=args.get("client"),
        )
    if name == "start_session":
        return await _start_session_composite(
            db,
            args["project_id"],
            args["session_name"],
            data_dir,
            human_id=args.get("human_id"),
            client_type=args.get("client"),
            role=args.get("role"),
        )
    if name == "list_projects":
        return await db_module.list_project_summaries(db)
    if name == "get_project_by_name":
        project = await db_module.get_project_by_name(db, args["name"])
        if project is None:
            raise ValueError(f"no project found matching '{args['name']}'")
        return {
            "id": project["id"],
            "name": project["name"],
            "sprint": project.get("sprint"),
        }
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
    if name == "search_tasks":
        return await db_module.search_tasks(
            db, args["project_id"], args["query"], args.get("limit", 5)
        )
    if name == "generate_handoff":
        from . import handoff as handoff_module_local
        session_id = args.get("session_id")
        if not isinstance(session_id, str):
            session_id = None
        mode = handoff_module_local.resolve_handoff_mode(
            args.get("mode"),
            session_id,
        )
        try:
            path, content = await asyncio.wait_for(
                handoff_module_local.generate_handoff(
                    db,
                    args["project_id"],
                    data_dir,
                    mode=mode,
                    session_id=session_id,
                ),
                timeout=90.0,
            )
        except asyncio.TimeoutError:
            path, content = await handoff_module_local._generate_handoff_l0(
                db, args["project_id"], data_dir
            )
            mode = "full"
        return {"file_path": path, "content": content, "mode": mode}
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
    if name == "delete_decision":
        deleted = await db_module.delete_pinned_decision(db, args["decision_id"])
        if not deleted:
            raise ValueError("decision not found")
        return {"deleted": True, "decision_id": args["decision_id"]}
    if name == "checkpoint":
        session_id = args["session_id"]
        project_id = args["project_id"]
        await db_module.auto_capture_session(db, project_id, session_id)
        from . import handoff as handoff_module_local
        try:
            _, content = await asyncio.wait_for(
                handoff_module_local.generate_handoff(
                    db, project_id, data_dir, mode="delta", session_id=session_id
                ),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            content = "delta handoff timed out"
        pending_items = await db_module.get_sprint_items(db, project_id, status="pending")
        ids_str = ", ".join(it["id"][:8] for it in pending_items[:8])
        next_goal = (
            f'/goal Complete sprint items: {", ".join(it["id"] for it in pending_items[:8])}. '
            f"Done when complete_sprint_item()\'d, tests pass, generate_handoff called."
        ) if pending_items else "/goal Continue work — all sprint items done."
        # 04f03ee4 — include start_session one-liner so next session can resume immediately
        start_fresh = f'start_session(project_id="{project_id}", session_name="describe-what-youre-doing")'
        # fa595ad8 — store snapshot for Recent Sessions dashboard panel (non-fatal)
        try:
            import json as _json
            async with db.execute(
                "SELECT name FROM sessions WHERE id = ?", (session_id,)
            ) as _sc:
                _sr = await _sc.fetchone()
            _session_name = (_sr["name"] if _sr else None) or session_id[:8]
            async with db.execute(
                "SELECT COUNT(*) AS n FROM task_log "
                "WHERE session_id = ? AND status = 'done'",
                (session_id,),
            ) as _tc:
                _tr = await _tc.fetchone()
            _items_done = int(_tr["n"]) if _tr else 0
            _summary_line = (content or "").split("\n")[0][:140]
            await db_module.add_project_note(
                db, project_id,
                title=f"checkpoint:{session_id[:8]}",
                body=_json.dumps({
                    "session_id": session_id,
                    "session_name": _session_name,
                    "items_done": _items_done,
                    "summary_line": _summary_line,
                    "next_goal": next_goal,
                    "start_fresh": start_fresh,
                }),
                tags="checkpoint",
            )
        except Exception:
            pass  # non-fatal — checkpoint still returns normally
        return {
            "summary": content,
            "pending_count": len(pending_items),
            "pending_ids": ids_str,
            "next_goal": next_goal,
            "start_fresh": start_fresh,
        }
    if name == "request_hitl":
        result = await db_module.request_hitl(
            db, args["project_id"], args["question"],
            session_id=args.get("session_id"),
            context=args.get("context"),
            urgency=args.get("urgency", "normal"),
            assigned_to=args.get("assigned_to"),
        )
        # Notify via configured notify_url — best-effort, non-blocking
        _hitl_urgency = args.get("urgency", "normal").upper()
        _hitl_q = args["question"][:200]
        _hitl_base = os.environ.get("MERIDIAN_BASE_URL", "https://usemeridian.us").rstrip("/")
        await _maybe_notify(
            db, args["project_id"],
            f"Action needed ({_hitl_urgency})",
            f"{_hitl_q}\n\nAnswer at: {_hitl_base}/dashboard",
            event="hitl",
            tenant=tenant,
            pref_key="hitl",
        )
        return result
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
    if name == "list_hitl_requests":
        status_filter = args.get("status", "pending")
        if status_filter == "all":
            status_filter = None
        return await db_module.list_hitl_requests(
            db, args["project_id"],
            status=status_filter,
            limit=args.get("limit", 50),
        )
    if name == "answer_hitl":
        result = await db_module.answer_hitl_request(
            db, args["request_id"], args["answer"],
            answered_by=args.get("answered_by"),
        )
        if result is None:
            raise ValueError("hitl request not found")
        return result
    if name == "dismiss_hitl":
        result = await db_module.dismiss_hitl_request(db, args["request_id"])
        if result is None:
            raise ValueError("hitl request not found")
        return result
    if name == "list_sessions":
        active_only = args.get("status", "active") != "all"
        return await db_module.get_sessions(db, args["project_id"], active_only=active_only)
    if name == "add_sprint_note":
        return await db_module.add_session_note(
            db, args["session_id"], args["title"], args["body"]
        )
    if name == "get_sprint_notes":
        return await db_module.get_session_notes(db, args["session_id"])
    if name == "add_sprint_item":
        return await db_module.add_sprint_item(
            db, args["project_id"], args["version"], args["title"],
            group=args.get("group"),
            human_id=args.get("human_id"),
            depends_on=args.get("depends_on"),
            failure_mode=args.get("failure_mode"),
            milestone_type=args.get("milestone_type", "task"),
        )
    if name == "get_sprint_items":
        return await db_module.get_sprint_items(
            db, args["project_id"],
            status=args.get("status"),
        )
    if name == "complete_sprint_item":
        item = await db_module.complete_sprint_item(
            db, args["project_id"], args["item_id"],
            task_id=args.get("task_id"),
        )
        if item is None:
            raise ValueError("sprint item not found")
        # Notify only when the sprint is fully complete.
        active_statuses = {"pending", "todo", "in_progress"}
        remaining_items = await db_module.get_sprint_items(db, args["project_id"])
        if not any((it.get("status") or "") in active_statuses for it in remaining_items):
            await _maybe_notify(
                db, args["project_id"],
                "Sprint done ✓",
                "All sprint items are complete.",
                event="sprint_done",
                tenant=tenant,
                pref_key="sprint",
            )
        return item
    if name == "get_run_transcript":
        run = await db_module.get_executor_run_by_session(db, args.get("session_id", ""))
        if run is None:
            return {"error": "no run found for session"}
        return {
            "run_id": run["id"],
            "session_id": run["session_id"],
            "started_at": run["started_at"],
            "ended_at": run.get("ended_at"),
            "status": run["status"],
            "task_count": run["task_count"],
            "transcript": run["transcript"],
        }
    if name == "set_executor_config":
        cfg_fields = {
            k: args[k]
            for k in ("repo_path", "env_file", "test_cmd", "test_min",
                      "deploy_cmd", "shell_type", "branch")
            if k in args
        }
        return await db_module.set_executor_config(db, args["project_id"], cfg_fields)
    if name == "claim_file":
        return await db_module.claim_file(db, args["file_path"], args["session_id"])
    if name == "release_file":
        released = await db_module.release_file(db, args["file_path"], args["session_id"])
        return {"released": released, "file_path": args["file_path"]}
    if name == "idle_until_session_done":
        return await _idle_until_session_done(db, args["watching_session_id"])
    if name == "search_all":
        return await db_module.search_all(
            db, args["project_id"], args["query"],
            limit=args.get("limit", 10),
        )
    if name == "get_session_brief":
        # v2.5 — single-call orientation, <500 tokens, XML output.
        project_id = args["project_id"]
        role = args.get("role", "worker")
        session_id_for_notes = args.get("session_id")
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
        # v2.6 — include session scratch-pad notes at top of brief
        notes_xml = ""
        if session_id_for_notes:
            try:
                session_notes = await db_module.get_session_notes(db, session_id_for_notes)
                if session_notes:
                    notes_xml = "<session_notes>\n" + "\n".join(
                        f'  <note title="{n.get("title","")}">{(n.get("body") or "")[:120]}</note>'
                        for n in session_notes
                    ) + "\n</session_notes>\n"
            except Exception:
                pass
        brief = (
            f'<session_brief project_id="{project_id}" role="{role}">\n'
            f'{notes_xml}'
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

# ---------------------------------------------------------------------------
# 9768d806 — MCP SSE transport (for dnakov/claude-mcp Chrome extension)
# ---------------------------------------------------------------------------
# Protocol: GET /mcp/sse opens an SSE stream, receives "endpoint" event with
# the POST URL. Client POSTs JSON-RPC to POST /mcp/sse?session_id=<uuid> and
# reads the JSON response directly from the HTTP response body.

_SSE_SESSIONS: dict[str, dict[str, Any]] = {}  # session_id → {db, queue}

# ── OAuth 2.0 for claude.ai custom connector ──────────────────────────────
import secrets as _sec, hashlib as _hs, base64 as _b64, time as _tm, json as _json
from urllib.parse import urlencode as _ue
from fastapi.responses import RedirectResponse as _RR

_oa_clients: dict = {}
_oa_codes: dict = {}

# Tokens persisted to disk in local mode for backwards compatibility.
_OA_TOKEN_FILE = DEFAULT_DATA_DIR / "oauth_tokens.json"

def _oauth_token_hash(token: str) -> str:
    return _hs.sha256(token.encode()).hexdigest()


def _normalize_oa_tokens(tokens: dict[str, Any]) -> dict[str, dict[str, Any]]:
    now = int(_tm.time())
    normalized: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in (tokens or {}).items():
        if not isinstance(raw_value, dict):
            continue
        try:
            exp = int(raw_value.get("exp", 0))
        except (TypeError, ValueError):
            continue
        if exp <= now:
            continue
        token_hash = (
            raw_key
            if isinstance(raw_key, str)
            and len(raw_key) == 64
            and all(c in "0123456789abcdef" for c in raw_key.lower())
            else _oauth_token_hash(str(raw_key))
        )
        normalized[token_hash] = {
            "tenant_id": raw_value.get("tenant_id"),
            "client_id": raw_value.get("client_id"),
            "exp": exp,
        }
    return normalized


def _load_oa_tokens_file() -> dict[str, dict[str, Any]]:
    try:
        if _OA_TOKEN_FILE.exists():
            data = _json.loads(_OA_TOKEN_FILE.read_text())
            return _normalize_oa_tokens(data)
    except Exception:
        pass
    return {}

def _save_oa_tokens(tokens: dict) -> None:
    if _hosted_mode():
        return
    try:
        _OA_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _OA_TOKEN_FILE.write_text(_json.dumps(tokens))
    except Exception:
        pass

async def _ensure_oauth_token_table(db: Any) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS oauth_tokens (
            token_hash TEXT PRIMARY KEY,
            tenant_id TEXT,
            client_id TEXT,
            exp BIGINT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS oauth_clients (
            client_id TEXT PRIMARY KEY,
            client_secret TEXT NOT NULL,
            redirect_uris TEXT NOT NULL DEFAULT '[]',
            client_name TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.commit()


async def _upsert_oauth_token(
    db: Any,
    token_hash: str,
    *,
    tenant_id: str | None,
    client_id: str,
    exp: int,
) -> None:
    await db.execute(
        "INSERT INTO oauth_tokens (token_hash, tenant_id, client_id, exp) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(token_hash) DO UPDATE SET "
        "tenant_id = excluded.tenant_id, "
        "client_id = excluded.client_id, "
        "exp = excluded.exp",
        (token_hash, tenant_id, client_id, exp),
    )
    await db.commit()


async def _get_oauth_token_from_db(
    db: Any,
    token_hash: str,
) -> dict[str, Any] | None:
    async with db.execute(
        "SELECT token_hash, tenant_id, client_id, exp "
        "FROM oauth_tokens WHERE token_hash = ? AND exp > ?",
        (token_hash, int(_tm.time())),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "tenant_id": row["tenant_id"],
        "client_id": row["client_id"],
        "exp": int(row["exp"]),
    }


async def _load_oauth_tokens_from_db(db: Any) -> dict[str, dict[str, Any]]:
    async with db.execute(
        "SELECT token_hash, tenant_id, client_id, exp "
        "FROM oauth_tokens WHERE exp > ?",
        (int(_tm.time()),),
    ) as cur:
        rows = await cur.fetchall()
    return {
        row["token_hash"]: {
            "tenant_id": row["tenant_id"],
            "client_id": row["client_id"],
            "exp": int(row["exp"]),
        }
        for row in rows
    }


async def _hydrate_oauth_cache(auth_db: Any) -> None:
    global _oa_tokens, _oa_clients

    await _ensure_oauth_token_table(auth_db)
    _oa_tokens = await _load_oauth_tokens_from_db(auth_db)
    # Load persisted OAuth client registrations (DCR)
    try:
        import json as _json
        async with auth_db.execute("SELECT client_id, client_secret, redirect_uris FROM oauth_clients") as cur:
            rows = await cur.fetchall()
        for row in rows:
            _oa_clients[row["client_id"]] = {
                "secret": row["client_secret"],
                "redirect_uris": _json.loads(row["redirect_uris"] or "[]")
            }
        if rows:
            print(f"[oauth] loaded {len(rows)} persisted client registrations")
    except Exception:
        pass

    if _hosted_mode():
        return

    legacy_tokens = _load_oa_tokens_file()
    if not legacy_tokens:
        return

    _oa_tokens.update(legacy_tokens)
    for token_hash, token_data in legacy_tokens.items():
        await _upsert_oauth_token(
            auth_db,
            token_hash,
            tenant_id=token_data.get("tenant_id"),
            client_id=str(token_data.get("client_id") or ""),
            exp=int(token_data.get("exp", 0)),
        )
    _save_oa_tokens(_oa_tokens)


_oa_tokens: dict[str, dict[str, Any]] = {}


@app.get("/.well-known/oauth-authorization-server")
async def _oauth_meta(request: Request):
    b = str(request.base_url).rstrip("/")
    return JSONResponse({"issuer": b,
        "client_name": "Meridian", "logo_uri": "https://usemeridian.us/static/logo.svg",
        "authorization_endpoint": f"{b}/oauth/authorize",
        "token_endpoint": f"{b}/oauth/token",
        "registration_endpoint": f"{b}/oauth/register",
        "scopes_supported": ["mcp"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"]})


@app.post("/oauth/register")
async def _oauth_reg(request: Request):
    d = await request.json()
    cid, cs = _sec.token_urlsafe(16), _sec.token_urlsafe(32)
    redirect_uris = d.get("redirect_uris", [])
    client_name = d.get("client_name", "")
    _oa_clients[cid] = {"secret": cs, "redirect_uris": redirect_uris}
    # Persist to DB so registrations survive restarts
    try:
        import json as _json
        auth_db = request.app.state.db
        await auth_db.execute(
            "INSERT OR REPLACE INTO oauth_clients (client_id, client_secret, redirect_uris, client_name) VALUES (?, ?, ?, ?)",
            (cid, cs, _json.dumps(redirect_uris), client_name)
        )
        await auth_db.commit()
    except Exception:
        pass  # in-memory fallback still works
    return JSONResponse({"client_id": cid, "client_secret": cs,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code"],
        "token_endpoint_auth_method": "client_secret_post"}, status_code=201)


@app.get("/oauth/authorize")
async def _oauth_auth(request: Request):
    p = dict(request.query_params)
    # ── Session cookie guard (hosted mode only) ─────────────────────────────
    # Prevents unauthenticated callers from obtaining MCP access tokens.
    # Local / self-hosted installs skip this guard (_hosted_mode() is False).
    if _hosted_mode():
        authed = False
        from .hosted import _SESSION_COOKIE, _read_session_cookie
        auth_db = request.app.state.db
        cookie_val = request.cookies.get(_SESSION_COOKIE, "")
        if cookie_val:
            sid = _read_session_cookie(cookie_val)
            if sid and await db_module.get_user_session(auth_db, sid):
                authed = True
        if not authed:
            # Also accept a bearer token (API-key flow)
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer "):
                tok_hash = hashlib.sha256(auth_header[7:].encode()).hexdigest()
                if await db_module.get_tenant_from_token_hash(auth_db, tok_hash):
                    authed = True
        if not authed:
            from urllib.parse import quote as _q
            orig_qs = str(request.url.query)
            next_path = f"/oauth/authorize?{orig_qs}" if orig_qs else "/oauth/authorize"
            return _RR(f"/auth/login?next={_q(next_path)}")
    # ── Auto-approve ────────────────────────────────────────────────────────
    # In hosted mode, capture the tenant_id from the session so MCP requests
    # can be routed to the correct per-tenant project DB.
    _tenant_id: str | None = None
    if _hosted_mode():
        from .hosted import _SESSION_COOKIE, _read_session_cookie
        _cookie = request.cookies.get(_SESSION_COOKIE, "")
        _sid = _read_session_cookie(_cookie) if _cookie else None
        if _sid:
            _sess = await db_module.get_user_session(request.app.state.db, _sid)
            if _sess:
                _tenant_id = _sess.get("tenant_id")

    code = _sec.token_urlsafe(32)
    _oa_codes[code] = {"client_id": p.get("client_id", ""),
        "redirect_uri": p.get("redirect_uri", ""),
        "challenge": p.get("code_challenge"),
        "tenant_id": _tenant_id,
        "exp": _tm.time() + 600}
    qs = _ue({"code": code, "state": p.get("state", "")})
    return _RR(f"{p.get('redirect_uri', '')}?{qs}")


@app.post("/oauth/token")
async def _oauth_token(request: Request):
    ct = request.headers.get("content-type", "")
    d = dict(await request.json() if "json" in ct else await request.form())
    if d.get("grant_type") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    code = d.get("code", "")
    if code not in _oa_codes:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    cd = _oa_codes.pop(code)
    if _tm.time() > cd["exp"]:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    v = d.get("code_verifier")
    if cd.get("challenge") and v:
        ch = _b64.urlsafe_b64encode(_hs.sha256(v.encode()).digest()).decode().rstrip("=")
        if ch != cd["challenge"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
    tok = _sec.token_urlsafe(32)
    tok_hash = _oauth_token_hash(tok)
    tok_data = {
        "client_id": cd["client_id"],
        "exp": int(_tm.time() + 86400 * 90),
        "tenant_id": cd.get("tenant_id"),  # propagate for per-tenant DB routing
    }
    _oa_tokens[tok_hash] = tok_data
    await _upsert_oauth_token(
        request.app.state.db,
        tok_hash,
        tenant_id=tok_data.get("tenant_id"),
        client_id=tok_data["client_id"],
        exp=tok_data["exp"],
    )
    _save_oa_tokens(_oa_tokens)
    return JSONResponse({"access_token": tok, "token_type": "bearer", "expires_in": 86400 * 90})


@app.get("/mcp")
async def _mcp_get(request: Request):
    accept = request.headers.get("accept", "")
    if "text/event-stream" in accept:
        return _RR("/mcp/sse")
    return JSONResponse({"name": "meridian", "version": "1.0", "transport": "http+sse"})

# ── End OAuth ──────────────────────────────────────────────────────────────


_SSE_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Cache-Control": "no-cache, no-store",
    "X-Accel-Buffering": "no",
}


@app.options("/mcp/sse")
async def mcp_sse_options(request: Request) -> Response:
    """CORS preflight for chrome-extension:// origin."""
    return Response(status_code=204, headers=_SSE_CORS_HEADERS)


@app.get("/mcp/sse")
async def mcp_sse_get(request: Request) -> StreamingResponse:
    """MCP SSE transport GET — opens event stream for dnakov/claude-mcp.

    Sends ``event: endpoint`` with POST URL, then heartbeats every 15 s.
    No strict auth required: uses the same _db() resolver (Bearer / cookie /
    local fallback) so both local and hosted-tier clients work.
    """
    import uuid as _uuid
    import anyio as _anyio

    db = await _db(request)
    data_dir = _data_dir(request)

    # Reuse session_id on reconnect if client sends one and it's valid
    requested_sid = request.query_params.get("session_id")
    if requested_sid and requested_sid in _SSE_SESSIONS:
        session_id = requested_sid
        _SSE_SESSIONS[session_id]["db"] = db
    else:
        session_id = str(_uuid.uuid4())
        _SSE_SESSIONS[session_id] = {"db": db, "data_dir": data_dir}

    endpoint_path = f"/mcp/sse?session_id={session_id}"

    async def _stream():
        try:
            yield f"event: endpoint\ndata: {endpoint_path}\n\n"
            # Heartbeat loop — exit when client disconnects
            while True:
                if await request.is_disconnected():
                    break
                yield ": heartbeat\n\n"
                await _anyio.sleep(15)
        finally:
            _SSE_SESSIONS.pop(session_id, None)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers=_SSE_CORS_HEADERS,
    )


@app.post("/mcp/sse")
async def mcp_sse_post(request: Request) -> Any:
    """MCP SSE transport POST — JSON-RPC handler for dnakov/claude-mcp.

    Client POSTs a JSON-RPC 2.0 message; response is returned as JSON in the
    HTTP response body (extension reads it directly, not via the SSE stream).
    """
    from fastapi.responses import JSONResponse as _JSONResp

    session_id = request.query_params.get("session_id", "")
    sess = _SSE_SESSIONS.get(session_id)
    db = sess["db"] if sess else await _db(request)
    data_dir = sess["data_dir"] if sess else _data_dir(request)

    try:
        body = await request.json()
    except Exception:
        return _JSONResp(_jsonrpc_err(None, -32700, "parse error"), status_code=400, headers=_SSE_CORS_HEADERS)

    if isinstance(body, list):
        results = [await _handle_mcp_request(item, db, data_dir) for item in body]
        return _JSONResp(results, headers=_SSE_CORS_HEADERS)

    result = await _handle_mcp_request(body, db, data_dir)
    return _JSONResp(result, headers=_SSE_CORS_HEADERS)


@app.post("/mcp")
async def remote_mcp(request: Request) -> Any:
    """Remote MCP endpoint — JSON-RPC 2.0 over HTTP.

    Accepts OAuth bearer tokens and Meridian API keys over the same endpoint.
    Rate-limited to 100 requests/minute per IP.
    Accepts a single JSON-RPC 2.0 message or a batch (list).
    """
    from fastapi.responses import JSONResponse

    if _limiter is not None:
        try:
            await _limiter._check_request_limit(request, None, False)
        except Exception:
            pass  # rate limiting is best-effort; don't block on errors

    # Check local OAuth tokens first (claude.ai connector via tunnel or hosted OAuth)
    _auth = request.headers.get("authorization", "")
    _bearer = _auth.removeprefix("Bearer ").strip()
    _bearer_hash = _oauth_token_hash(_bearer) if _bearer else ""
    _td = _oa_tokens.get(_bearer_hash) if _bearer_hash else None
    if _td is None and _bearer_hash:
        _td = await _get_oauth_token_from_db(request.app.state.db, _bearer_hash)
        if _td is not None:
            _oa_tokens[_bearer_hash] = _td
    if _td is not None:
        if _tm.time() > _td.get("exp", 0):
            _oa_tokens.pop(_bearer_hash, None)
            return JSONResponse({"error": "token_expired"}, status_code=401)
        try:
            _body = await request.json()
        except Exception:
            return JSONResponse(_jsonrpc_err(None, -32700, "parse error"), status_code=400)
        # In hosted mode, route to the tenant's project DB (not the shared auth DB).
        # tenant_id is stored in _oa_tokens when the OAuth flow ran in hosted mode.
        _mdb = request.app.state.db
        _oa_tenant_id = _td.get("tenant_id")
        if _oa_tenant_id and _hosted_mode():
            try:
                from ._deps import _open_tenant_db_by_id
                _mdb = await _open_tenant_db_by_id(request, _oa_tenant_id)
            except Exception:
                pass  # fall back to auth DB — better than 500
        _mdd = request.app.state.data_dir
        _oa_tenant = None
        if _oa_tenant_id and _hosted_mode():
            _oa_tenant = await db_module.get_tenant_by_id(request.app.state.db, _oa_tenant_id)
        if isinstance(_body, list):
            return JSONResponse([await _handle_mcp_request(i, _mdb, _mdd, tenant=_oa_tenant) for i in _body])
        return JSONResponse(await _handle_mcp_request(_body, _mdb, _mdd, tenant=_oa_tenant))

    tenant = None
    if _bearer_hash:
        tenant = await db_module.get_tenant_from_token_hash(request.app.state.db, _bearer_hash)
    if tenant is None:
        from .hosted import get_tenant_from_bearer

        # Bearer auth required (hosted tenant path)
        tenant = await get_tenant_from_bearer(request)  # raises 401 if invalid

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_jsonrpc_err(None, -32700, "parse error"), status_code=400)

    db = await _db(request)
    data_dir = _data_dir(request)

    if isinstance(body, list):
        results = [await _handle_mcp_request(item, db, data_dir, tenant=tenant) for item in body]
        return JSONResponse(results)

    result = await _handle_mcp_request(body, db, data_dir, tenant=tenant)
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
            # v1.9.x — read meridian.toml connection profiles (same logic as lifespan).
            # Without this the MCP server always falls back to local SQLite even when
            # the toml says use Postgres.
            _db_override = os.environ.get("MERIDIAN_DB", DEFAULT_DB_PATH)
            if _db_override != ":memory:":
                _toml_url, _toml_conn_name = toml_config_module.get_toml_db_url()
                if _toml_url:
                    os.environ["MERIDIAN_DB_URL"] = _toml_url
                elif _toml_conn_name is not None:
                    os.environ.pop("MERIDIAN_DB_URL", None)
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
                name="set_executor_config",
                description=(
                    "Store per-project executor defaults (repo_path, test_cmd, "
                    "deploy_cmd, etc.) so executor sessions auto-load them via "
                    "start_session(role='executor'). Set once; all executors "
                    "inherit automatically."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "repo_path": {"type": "string", "description": "Absolute path to the repo root."},
                        "env_file": {"type": "string", "description": "Path to .env file for the executor."},
                        "test_cmd": {"type": "string", "description": "Command to run the test suite."},
                        "test_min": {"type": "integer", "description": "Minimum passing test count."},
                        "deploy_cmd": {"type": "string", "description": "Command to deploy (e.g. git push)."},
                        "shell_type": {"type": "string", "description": "Shell to use: bash, powershell, cmd."},
                        "branch": {"type": "string", "description": "Default working branch."},
                    },
                    "required": ["project_id"],
                },
            ),
            Tool(
                name="claim_file",
                description=(
                    "Claim exclusive edit rights on a file path for this session. "
                    "Returns {claimed: true} on success or {claimed: false, holder_session_id} "
                    "when another session holds the lock. Locks auto-expire after 2 hours. "
                    "Always release_file() when done editing."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "file_path": {"type": "string", "description": "Repo-relative or absolute file path."},
                    },
                    "required": ["session_id", "file_path"],
                },
            ),
            Tool(
                name="release_file",
                description=(
                    "Release a file lock held by this session. "
                    "Silently succeeds if the lock was already released or expired."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "file_path": {"type": "string"},
                    },
                    "required": ["session_id", "file_path"],
                },
            ),
            Tool(
                name="idle_until_session_done",
                description=(
                    "Check whether a specific session has finished. "
                    "Use when you need to wait for another session to complete before editing "
                    "a shared file. Returns {done: true/false, status, suggested_wait_seconds}. "
                    "Poll with the suggested delay until done=true."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "watching_session_id": {"type": "string", "description": "The session ID to watch."},
                    },
                    "required": ["watching_session_id"],
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
                name="search_tasks",
                description=(
                    "Search past tasks by keyword or phrase. Uses trigram "
                    "similarity on Postgres, LIKE on SQLite. Returns top "
                    "matches with a similarity score so you can find related "
                    "work done by any session."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 5},
                    },
                    "required": ["project_id", "query"],
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
                    "filling up or before ending a session. mode='full' "
                    "writes the complete L0/L1/L2 handoff. mode='delta' "
                    "returns a compact session update with completed items, "
                    "remaining pending items, and the next /goal string. "
                    "mode='starter' returns a ≤20-line paste-after-/compact "
                    "block: project_id, start_session command, last 5 done, "
                    "top 3 pending IDs, and a /goal string. "
                    "mode='planner' gives strategic context for claude.ai."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "mode": {
                            "type": "string",
                            "enum": ["full", "delta", "planner", "starter"],
                        },
                        "session_id": {
                            "type": "string",
                            "description": (
                                "Optional session id for auto-delta on repeat "
                                "calls in the same chat."
                            ),
                        },
                    },
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
                name="list_hitl_requests",
                description=(
                    "v2.4 — list HITL requests for a project without needing "
                    "UUIDs. Returns pending queue by default; pass status='all' "
                    "to see answered/dismissed items too. Use before answer_hitl "
                    "or dismiss_hitl to find request IDs."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "status": {
                            "type": "string",
                            "description": "Filter: 'pending' (default), 'answered', 'dismissed', or 'all'.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results, default 50.",
                        },
                    },
                    "required": ["project_id"],
                },
            ),
            Tool(
                name="answer_hitl",
                description=(
                    "v2.4 — answer a pending HITL request so the waiting "
                    "session can resume. Use list_hitl_requests to find "
                    "request IDs."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "request_id": {"type": "string"},
                        "answer": {"type": "string"},
                        "answered_by": {
                            "type": "string",
                            "description": "Optional human_id of the answerer.",
                        },
                    },
                    "required": ["request_id", "answer"],
                },
            ),
            Tool(
                name="dismiss_hitl",
                description=(
                    "v2.4 — dismiss a HITL request (won't-answer / no longer "
                    "relevant). Stays in audit trail. Use list_hitl_requests "
                    "to find request IDs."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"request_id": {"type": "string"}},
                    "required": ["request_id"],
                },
            ),
            Tool(
                name="list_sessions",
                description=(
                    "v2.4 — list active sessions for a project. Useful to see "
                    "what's currently running before filing new sprint items. "
                    "Pass status='all' to include closed sessions."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "status": {
                            "type": "string",
                            "description": "Filter: 'active' (default) or 'all'.",
                        },
                    },
                    "required": ["project_id"],
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
                        "milestone_type": {
                            "type": "string",
                            "enum": ["task", "milestone"],
                            "description": "'milestone' renders as a timeline marker. Default: 'task'.",
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
                    "file is. If project_id is unknown, call list_projects() "
                    "first. Call this INSTEAD of register_session + "
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
                        "client": {
                            "type": "string",
                            "enum": ["claude-code", "claude-desktop", "cursor", "other"],
                            "description": "Client app — used for presence indicators.",
                        },
                        "role": {
                            "type": "string",
                            "enum": ["executor"],
                            "description": "Pass 'executor' to inject executor_config and credentials guidance.",
                        },
                    },
                    "required": ["project_id", "session_name"],
                },
            ),
            Tool(
                name="list_projects",
                description=(
                    "Call first when project_id is unknown. Returns the "
                    "current tenant's projects as [{id, name, sprint, "
                    "created_at}] newest first."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="get_project_by_name",
                description=(
                    "Look up a project by name (case-insensitive substring "
                    "match). Returns the first hit with id, name, and sprint. "
                    "Use this when you know the project name but not the UUID."
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
                    client_type=arguments.get("client"),
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
            elif name == "set_executor_config":
                cfg_fields = {
                    k: arguments[k]
                    for k in ("repo_path", "env_file", "test_cmd", "test_min",
                              "deploy_cmd", "shell_type", "branch")
                    if k in arguments
                }
                try:
                    result = await db_module.set_executor_config(
                        db, arguments["project_id"], cfg_fields
                    )
                except ValueError as exc:
                    result = {"error": str(exc)}
            elif name == "claim_file":
                try:
                    result = await db_module.claim_file(
                        db,
                        arguments["file_path"],
                        arguments["session_id"],
                    )
                except ValueError as exc:
                    result = {"error": str(exc)}
            elif name == "release_file":
                released = await db_module.release_file(
                    db,
                    arguments["file_path"],
                    arguments["session_id"],
                )
                result = {"released": released, "file_path": arguments["file_path"]}
            elif name == "idle_until_session_done":
                result = await _idle_until_session_done(
                    db,
                    arguments["watching_session_id"],
                )
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
                session_id = arguments.get("session_id")
                if not isinstance(session_id, str):
                    session_id = None
                mode = handoff_module.resolve_handoff_mode(
                    arguments.get("mode"),
                    session_id,
                )
                try:
                    path, content = await asyncio.wait_for(
                        handoff_module.generate_handoff(
                            db,
                            arguments["project_id"],
                            state["data_dir"],
                            mode=mode,
                            session_id=session_id,
                        ),
                        timeout=90.0,
                    )
                except asyncio.TimeoutError:
                    path, content = await handoff_module._generate_handoff_l0(
                        db, arguments["project_id"], state["data_dir"]
                    )
                    mode = "full"
                result = {"path": path, "content": content, "mode": mode}
            elif name == "get_context_block":
                # v2.3 — reuse the dispatch impl so HTTP and stdio share one path.
                result = await _dispatch_mcp_tool(
                    "get_context_block", arguments, db, state["data_dir"]
                )
            elif name in (
                "pin_decision", "update_decision", "get_pinned_decisions",
                "request_hitl", "get_hitl_request",
                "list_hitl_requests", "answer_hitl", "dismiss_hitl",
                "list_sessions",
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
                result = await _claim_task_result(
                    db,
                    arguments["project_id"],
                    arguments["task_id"],
                    arguments["session_id"],
                )
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
                    client_type=arguments.get("client"),
                    role=arguments.get("role"),
                )
            elif name == "list_projects":
                result = await db_module.list_project_summaries(db)
            elif name == "get_project_by_name":
                name_arg = arguments["name"]
                project = await db_module.get_project_by_name(db, name_arg)
                if project is None:
                    result = {
                        "error": f"no project found matching '{name_arg}'"
                    }
                else:
                    result = {
                        "id": project["id"],
                        "name": project["name"],
                        "sprint": project.get("sprint"),
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
                    milestone_type=arguments.get("milestone_type", "task"),
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
            elif name == "get_run_transcript":
                session_id_arg = arguments.get("session_id", "")
                run = await db_module.get_executor_run_by_session(db, session_id_arg)
                if run is None:
                    result = {"error": "no run found for session"}
                else:
                    result = {
                        "run_id": run["id"],
                        "session_id": run["session_id"],
                        "started_at": run["started_at"],
                        "ended_at": run.get("ended_at"),
                        "status": run["status"],
                        "task_count": run["task_count"],
                        "transcript": run["transcript"],
                    }
            else:
                result = {"error": f"unknown tool: {name}"}
        except Exception as exc:  # noqa: BLE001 — surface to MCP client
            result = {"error": f"{type(exc).__name__}: {exc}"}

        # Implicit last_seen bump: any tool call that carries a session_id
        # keeps the session alive without requiring explicit heartbeats.
        _session_id = arguments.get("session_id")
        if _session_id and name != "heartbeat":
            try:
                await db_module.update_session_seen(db, _session_id)
            except Exception:
                pass

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
