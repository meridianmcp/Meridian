"""File-editing routes (AGENTS.md / CLAUDE.md) — extracted from server.py.

Owns the demo markdown fixtures (`_DEMO_FILE_CONTENT`) shown in demo mode. The
allow-list (`_EDITABLE_FILES`) and repo root (`_REPO_ROOT`) remain canonical in
server.py and are lazily imported to avoid a circular import.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .._deps import _db, _DEMO_CONTEXT_COOKIE
from .. import db as db_module
from ..models import FileContent

router = APIRouter()


_DEMO_FILE_CONTENT: dict[str, dict[str, str]] = {
    "backend-api-v2": {
        "AGENTS.md": """\
# backend-api-v2 — Agent Session Instructions

## Start a session

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
# backend-api-v2 — Session Instructions

## Project ID
`PROJECT_ID=<project-id>`

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

## Start a session

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
# data-pipeline — Session Instructions

## Project ID
`PROJECT_ID=<project-id>`

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


@router.get("/projects/{project_id}/files")
async def list_project_files(
    project_id: str, request: Request
) -> list[str]:
    """Return the list of editable markdown files for a project.

    Files that do not yet exist on disk are still listed so the user can
    create them from the dashboard. 403 is raised if the project is unknown.
    """
    from meridian.server import _EDITABLE_FILES  # noqa: PLC0415
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return _EDITABLE_FILES


@router.get("/projects/{project_id}/files/{filename}")
async def get_project_file(
    project_id: str, filename: str, request: Request
) -> dict[str, str]:
    """Read one editable markdown file and return its content.

    Returns ``{"filename": ..., "content": ...}`` with an empty string when
    the file does not yet exist. 403 if the filename is not in the allow-list.
    In demo mode, returns realistic fake content keyed by project name.
    """
    from meridian.server import _EDITABLE_FILES, _REPO_ROOT  # noqa: PLC0415
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if filename not in _EDITABLE_FILES:
        raise HTTPException(status_code=403, detail="file not in allow-list")
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


@router.put("/projects/{project_id}/files/{filename}")
async def put_project_file(
    project_id: str, filename: str, body: FileContent, request: Request
) -> dict[str, object]:
    """Write content to one editable markdown file.

    Creates the file if it does not exist. 403 if the filename is not in the
    allow-list. Returns ``{"filename": ..., "size": <bytes>}``.
    """
    from meridian.server import _EDITABLE_FILES, _REPO_ROOT  # noqa: PLC0415
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if filename not in _EDITABLE_FILES:
        raise HTTPException(status_code=403, detail="file not in allow-list")
    path = _REPO_ROOT / filename
    path.write_text(body.content, encoding="utf-8")
    return {"filename": filename, "size": len(body.content.encode("utf-8"))}
