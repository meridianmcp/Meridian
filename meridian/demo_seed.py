"""Demo data seeding helpers — used during lifespan startup."""
from __future__ import annotations

from pathlib import Path

from . import db as db_module

_REPO_ROOT = Path(__file__).parent.parent


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
    for title, body, tags in [
        ("Release checklist", "Run the load test, verify rate-limit headers, and keep the v1 deprecation notice ready.", "release,qa"),
        ("API docs", "OpenAPI docs are at /docs; keep examples in sync with the latest auth keys and pagination changes.", "docs,api"),
    ]:
        await db_module.add_project_note(db, api["id"], title, body, tags)
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
    for title, body, tags in [
        ("Incident drill", "If DLQ growth spikes, check schema compatibility first, then consumer lag.", "ops,dlq"),
        ("Pipeline checklist", "Keep Flink windows at 5 minutes and watch PII masking before the next pilot.", "etl,quality"),
    ]:
        await db_module.add_project_note(db, pipe["id"], title, body, tags)
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
