"""
Idempotent demo DB seeder for usemeridian.us/demo.
Run: pixi run python scripts/seed_demo.py [--db-url URL]

Safe to re-run -- clears all rows for the demo projects first, then reseeds
with expanded content: 2 projects, 8+ sessions each, 30+ tasks each, 3+ HITLs,
5+ pinned decisions, 10+ sprint items.
"""
import asyncio, selectors, os, sys, uuid
import psycopg
from datetime import datetime, timedelta, timezone
from pathlib import Path
from psycopg.rows import dict_row

asyncio.set_event_loop(asyncio.SelectorEventLoop(selectors.SelectSelector()))
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load .env
_env = Path(__file__).parent.parent / ".env"
if _env.exists():
    for _l in _env.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in _l and not _l.startswith("#"):
            _k, _, _v = _l.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"'))

DEMO_DB_URL = os.environ.get("MERIDIAN_DEMO_DB_URL", "")
if not DEMO_DB_URL:
    print("ERROR: Set MERIDIAN_DEMO_DB_URL in .env or pass --db-url")
    sys.exit(1)

NOW = datetime.now(timezone.utc)

def ts(days_ago=0, hours_ago=0, mins_ago=0):
    return (NOW - timedelta(days=days_ago, hours=hours_ago, minutes=mins_ago)).strftime("%Y-%m-%d %H:%M:%S")

def datestamp(days_ago=0):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d")

def uid():
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Project 1: backend-api-v2
# ---------------------------------------------------------------------------

P1_ID = "25946a49-2dc1-4c9f-bff7-e18827b391c7"  # stable ID (existing demo project)

P1_NORTH_STAR = (
    "Build a fast, observable REST API that the team can extend without stepping on each other. "
    "Developer experience first -- local setup under 2 minutes, CI under 4 minutes, zero DX debt. "
    "Every endpoint typed, every error surfaced, every decision logged."
)

P1_SPRINT = (
    "v1.2 -- rate limiting + API key auth\n"
    "Alice: rate limiting Redis pool fix complete, load test passing at 1k req/min.\n"
    "Bob: POST /v1/api-keys done. SDK generation starting.\n"
    "Worker 1: OpenAPI spec at /docs, all 14 endpoints annotated.\n"
    "Worker 2: TypeScript SDK generation in progress from OpenAPI spec."
)

def make_p1_sessions():
    return {
        "alice_planning": uid(),
        "alice_review":   uid(),
        "alice_standup":  uid(),
        "bob_rate":       uid(),
        "bob_apikeys":    uid(),
        "bob_review":     uid(),
        "worker1_openapi":uid(),
        "worker2_sdk":    uid(),
        "worker1_tests":  uid(),
    }

def p1_sessions(sids):
    return [
        (sids["alice_planning"], P1_ID, "alice", "alice/sprint-planning",       "claude_code", "claude-desktop", ts(14),   ts(14,0,5)),
        (sids["alice_review"],   P1_ID, "alice", "alice/architecture-review",   "claude_code", "claude-desktop", ts(7),    ts(7,0,10)),
        (sids["alice_standup"],  P1_ID, "alice", "alice/daily-standup",         "claude_code", "claude-desktop", ts(1),    ts(0,23)),
        (sids["bob_rate"],       P1_ID, "bob",   "bob/rate-limiting-impl",      "claude_code", "claude-desktop", ts(3),    ts(2)),
        (sids["bob_apikeys"],    P1_ID, "bob",   "bob/api-keys-endpoint",       "claude_code", "claude-desktop", ts(1,4),  ts(0,6)),
        (sids["bob_review"],     P1_ID, "bob",   "bob/code-review-sprint",      "claude_code", "claude-desktop", ts(0,8),  ts(0,4)),
        (sids["worker1_openapi"],P1_ID, "alice", "claude/openapi-generation",   "claude_code", "claude-code",    ts(0,6),  ts(0,3)),
        (sids["worker2_sdk"],    P1_ID, "bob",   "claude/typescript-sdk",       "claude_code", "claude-code",    ts(0,2),  ts(0,0,30)),
        (sids["worker1_tests"],  P1_ID, "alice", "claude/integration-tests",    "claude_code", "claude-code",    ts(0,10), ts(0,7)),
    ]

def p1_tasks(sids):
    s = sids
    return [
        # Alice planning (14 days ago)
        (uid(), s["alice_planning"], P1_ID, "Mapped v1.2 scope: rate limiting + API key auth + OpenAPI. Three parallel workstreams assigned.", "done", ts(14)),
        (uid(), s["alice_planning"], P1_ID, "DECISION: Redis token bucket -- survives deploys, consistent across instances. In-memory rejected.", "done", ts(14)),
        (uid(), s["alice_planning"], P1_ID, "Created sprint items for v1.2. Bob: rate limiting. Workers: API keys + OpenAPI.", "done", ts(14)),
        # Alice architecture review (7 days ago)
        (uid(), s["alice_review"],   P1_ID, "Architecture review: API versioning via URL prefix (/v1/) wins over Accept header.", "done", ts(7)),
        (uid(), s["alice_review"],   P1_ID, "Decided on cursor-based pagination -- offset breaks on concurrent inserts.", "done", ts(7)),
        (uid(), s["alice_review"],   P1_ID, "Reviewed Bob's rate limiter PR. Redis connection pool was leaking on exception path. Fixed.", "done", ts(7)),
        # Bob rate limiting (3-2 days ago)
        (uid(), s["bob_rate"],       P1_ID, "Investigated Redis connection pool exhaustion in staging -- leaking on exception path. Fixed.", "done", ts(3)),
        (uid(), s["bob_rate"],       P1_ID, "Wrote integration tests for token bucket algorithm. 12/12 passing including boundary cases.", "done", ts(3)),
        (uid(), s["bob_rate"],       P1_ID, "Rate limiting middleware shipped to staging. 1000 req/min load test holding steady.", "done", ts(2,8)),
        (uid(), s["bob_rate"],       P1_ID, "Found off-by-one in window reset logic -- double-counting at boundary. Fixed + test added.", "done", ts(2,4)),
        (uid(), s["bob_rate"],       P1_ID, "Filed HITL: should test environment use real Redis or fakeredis?", "done", ts(2)),
        # Bob API keys (1 day ago)
        (uid(), s["bob_apikeys"],    P1_ID, "Scaffolded POST /v1/api-keys. Schema: id, key_hash, name, created_at, last_used_at, revoked_at.", "done", ts(1,6)),
        (uid(), s["bob_apikeys"],    P1_ID, "Implemented key generation -- 32-byte random base64url prefixed mk_live_/mk_test_. Hash stored.", "done", ts(1,5)),
        (uid(), s["bob_apikeys"],    P1_ID, "Added GET /v1/api-keys (list) and DELETE /v1/api-keys/{id} (revoke).", "done", ts(1,4)),
        (uid(), s["bob_apikeys"],    P1_ID, "Tests: create key, authenticate, revoke, reject revoked. All passing.", "done", ts(1,3)),
        # Bob code review (today)
        (uid(), s["bob_review"],     P1_ID, "Reviewed worker 1 OpenAPI PR -- all 14 endpoints annotated correctly.", "done", ts(0,8)),
        (uid(), s["bob_review"],     P1_ID, "HITL: Should we add rate limiting headers (X-RateLimit-Remaining) to all endpoints now?", "done", ts(0,6)),
        # Worker 1 OpenAPI (today, earlier)
        (uid(), s["worker1_openapi"],P1_ID, "Integrated FastAPI OpenAPI spec at /openapi.json. Redoc UI at /docs.", "done", ts(0,10)),
        (uid(), s["worker1_openapi"],P1_ID, "Added response schema annotations to all 14 endpoints.", "done", ts(0,8)),
        (uid(), s["worker1_openapi"],P1_ID, "OpenAPI spec validates against 3.1. Exported to docs/openapi.json.", "done", ts(0,7)),
        (uid(), s["worker1_openapi"],P1_ID, "Merged api_keys endpoints into spec -- Bob had added them in parallel.", "done", ts(0,6)),
        # Worker 1 integration tests
        (uid(), s["worker1_tests"],  P1_ID, "Wrote integration test suite: 47 tests covering all v1.2 endpoints.", "done", ts(0,10)),
        (uid(), s["worker1_tests"],  P1_ID, "Rate limiter integration tests: window reset, burst handling, Redis failover.", "done", ts(0,9)),
        (uid(), s["worker1_tests"],  P1_ID, "API key integration tests: create, list, authenticate, revoke, reject-revoked.", "done", ts(0,8)),
        (uid(), s["worker1_tests"],  P1_ID, "All 47 integration tests passing against staging.", "done", ts(0,7)),
        # Alice standup (yesterday)
        (uid(), s["alice_standup"],  P1_ID, "Standup: all v1.2 items on track. SDK generation starting today.", "done", ts(1,0,5)),
        (uid(), s["alice_standup"],  P1_ID, "Unblocked Bob's API keys PR -- resolved merge conflict with worker 1's OpenAPI changes.", "done", ts(1,0,15)),
        # Worker 2 SDK (now, in progress)
        (uid(), s["worker2_sdk"],    P1_ID, "Started TypeScript SDK from OpenAPI spec using openapi-typescript-codegen.", "done", ts(0,2)),
        (uid(), s["worker2_sdk"],    P1_ID, "Generated client stubs -- 14 API methods typed. Adding custom auth wrapper.", "done", ts(0,1)),
        (uid(), s["worker2_sdk"],    P1_ID, "Generating TypeScript SDK -- writing tests against staging before publishing to npm.", "in_progress", ts(0,0,30)),
        # Backburner
        (uid(), s["alice_planning"], P1_ID, "Evaluate GraphQL as alternative to REST for v2.0 -- benchmarks, schema design, client tooling.", "backburner", ts(14)),
        (uid(), s["bob_rate"],       P1_ID, "Investigate gRPC transcoding for high-volume internal routes (v2.0 consideration).", "backburner", ts(3)),
    ]

def p1_sprint_items():
    return [
        (uid(), P1_ID, "v1.0", "POST /v1/auth/login -- JWT issuance",          "done",    ts(30)),
        (uid(), P1_ID, "v1.0", "GET /v1/users/{id} -- profile endpoint",        "done",    ts(29)),
        (uid(), P1_ID, "v1.0", "Postgres connection pooling (asyncpg)",          "done",    ts(28)),
        (uid(), P1_ID, "v1.0", "Docker compose for local dev",                  "done",    ts(27)),
        (uid(), P1_ID, "v1.0", "CI pipeline -- tests + lint on PR",             "done",    ts(26)),
        (uid(), P1_ID, "v1.1", "Cursor-based pagination on list endpoints",      "done",    ts(14)),
        (uid(), P1_ID, "v1.1", "X-Request-ID header for distributed tracing",   "done",    ts(13)),
        (uid(), P1_ID, "v1.1", "Structured JSON logging via structlog",          "done",    ts(12)),
        (uid(), P1_ID, "v1.1", "GET /health endpoint",                          "done",    ts(11)),
        (uid(), P1_ID, "v1.2", "Redis token bucket rate limiting middleware",    "done",    ts(3)),
        (uid(), P1_ID, "v1.2", "POST /v1/api-keys -- issue API keys",           "done",    ts(1,6)),
        (uid(), P1_ID, "v1.2", "DELETE /v1/api-keys/{id} -- revoke keys",       "done",    ts(1,4)),
        (uid(), P1_ID, "v1.2", "OpenAPI spec generation + Redoc UI at /docs",   "done",    ts(0,7)),
        (uid(), P1_ID, "v1.2", "Integration test suite (47 tests)",             "done",    ts(0,7)),
        (uid(), P1_ID, "v1.2", "Python client SDK from OpenAPI spec",           "pending", ts(0,2)),
        (uid(), P1_ID, "v1.2", "TypeScript client SDK from OpenAPI spec",       "pending", ts(0,2)),
        (uid(), P1_ID, "v1.2", "Load test rate limiter at 10k req/min",         "pending", ts(0,1)),
        (uid(), P1_ID, "v1.3", "Outbound webhooks -- POST on entity change",    "pending", ts(0,0)),
        (uid(), P1_ID, "v1.3", "Event streaming via SSE for live updates",      "pending", ts(0,0)),
        (uid(), P1_ID, "v1.4", "Multi-tenant isolation (schema-per-tenant)",    "pending", ts(0,0)),
    ]

def p1_decisions():
    return [
        ("Postgres over MySQL for primary store",
         "Better JSON operators, pgvector for future embedding search, existing team familiarity. MySQL eliminated on day 1.",
         "TECHNICAL", ts(30)),
        ("API versioning via URL prefix not Accept header",
         "URL versioning (/v1/) is visible in logs, easy to route in nginx, simpler for SDK consumers. Accept header versioning prototyped and rejected.",
         "TECHNICAL", ts(28)),
        ("Redis token bucket for rate limiting",
         "Survives deploys, works consistently across multiple instances. In-memory rejected -- state lost on restart, breaks horizontal scaling.",
         "TECHNICAL", ts(7)),
        ("API keys prefixed mk_live_ / mk_test_",
         "Stripe-style prefixes identifiable in logs and support tickets. Raw key shown once on creation, only hash stored. 32-byte base64url = 256 bits entropy.",
         "TECHNICAL", ts(5)),
        ("Cursor-based pagination over offset",
         "Offset pagination breaks when rows inserted during traversal. Cursor (created_at + id) is stable under concurrent writes. Default 20 rows, max 100.",
         "TECHNICAL", ts(14)),
        ("openapi-typescript-codegen for SDK generation",
         "Evaluated 4 libraries. openapi-typescript-codegen won: smallest bundle, best TypeScript inference, active maintenance.",
         "TECHNICAL", ts(0,2)),
    ]

def p1_hitl(sids):
    h1 = uid()
    h2 = uid()
    h3 = uid()
    return [
        (h1, P1_ID, sids["bob_rate"], "Should test environment use real Redis or fakeredis?",
         "Rate limiter tests use fakeredis (faster) but miss connection pool issues. Real Redis adds 8s to CI.",
         "answered", ts(2), "Use fakeredis for unit tests, real Redis for nightly integration suite. -- Alice"),
        (h2, P1_ID, sids["bob_review"], "Should we add X-RateLimit-Remaining headers to all endpoints?",
         "Standard practice per RFC 6585. Clients can back off proactively. Adds ~50 bytes per response.",
         "answered", ts(0,6), "Yes -- add X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset. Standard, low cost."),
        (h3, P1_ID, sids["worker2_sdk"], "TypeScript SDK: publish to npm under @meridian-api/client or meridian-api-client?",
         "Scoped packages (@meridian-api/client) look more professional but require npm org setup. Unscoped is simpler.",
         "pending", ts(0,0,30), None),
    ]


# ---------------------------------------------------------------------------
# Project 2: data-pipeline
# ---------------------------------------------------------------------------

P2_ID = uid()  # new project, fresh ID each seed (idempotent: clear-then-insert)

P2_NORTH_STAR = (
    "Build a reliable, observable ETL pipeline that ingests 50M events/day from 8 data sources "
    "without data loss, with end-to-end latency under 5 minutes. "
    "Quality gates at every stage -- schema validation, dedup, lineage tracking. "
    "On-call burden under 2 pages/week."
)

P2_SPRINT = (
    "v0.3 -- Kafka consumer reliability + dead letter queue\n"
    "Maya: schema registry integrated, Avro validation at ingestion. All 8 sources registered.\n"
    "Jordan: DLQ implemented, alerting on dead letter spikes wired to PagerDuty.\n"
    "Worker 1: Flink windowing operator complete, 5-minute tumbling windows tested.\n"
    "Worker 2: Iceberg table compaction job running nightly, query latency -40%%."
)

def make_p2_sessions():
    return {
        "maya_sources":   uid(),
        "maya_schema":    uid(),
        "maya_infra":     uid(),
        "jordan_dlq":     uid(),
        "jordan_alerts":  uid(),
        "jordan_oncall":  uid(),
        "worker1_flink":  uid(),
        "worker2_iceberg":uid(),
        "worker1_tests":  uid(),
    }

def p2_sessions(sids):
    return [
        (sids["maya_sources"],   P2_ID, "maya",   "maya/source-ingestion-setup",  "claude_code", "claude-desktop", ts(21),   ts(21,0,30)),
        (sids["maya_schema"],    P2_ID, "maya",   "maya/schema-registry-avro",    "claude_code", "claude-desktop", ts(7),    ts(6,22)),
        (sids["maya_infra"],     P2_ID, "maya",   "maya/kafka-consumer-infra",    "claude_code", "claude-desktop", ts(2),    ts(2,0,45)),
        (sids["jordan_dlq"],     P2_ID, "jordan", "jordan/dead-letter-queue",     "claude_code", "claude-desktop", ts(5),    ts(4)),
        (sids["jordan_alerts"],  P2_ID, "jordan", "jordan/alerting-pagerduty",    "claude_code", "claude-desktop", ts(1,6),  ts(0,22)),
        (sids["jordan_oncall"],  P2_ID, "jordan", "jordan/oncall-runbooks",       "claude_code", "claude-desktop", ts(0,8),  ts(0,5)),
        (sids["worker1_flink"],  P2_ID, "maya",   "claude/flink-windowing",       "claude_code", "claude-code",    ts(3),    ts(2,18)),
        (sids["worker2_iceberg"],P2_ID, "jordan", "claude/iceberg-compaction",    "claude_code", "claude-code",    ts(1),    ts(0,14)),
        (sids["worker1_tests"],  P2_ID, "maya",   "claude/pipeline-e2e-tests",    "claude_code", "claude-code",    ts(0,6),  ts(0,3)),
    ]

def p2_tasks(sids):
    s = sids
    return [
        # Maya source ingestion (21 days ago)
        (uid(), s["maya_sources"],  P2_ID, "Connected all 8 data sources to Kafka topics. Clickstream, payments, inventory, logistics, CRM, IoT sensors x3.", "done", ts(21)),
        (uid(), s["maya_sources"],  P2_ID, "Set up Kafka cluster on k8s. 3 broker nodes, replication factor 3, retention 7 days.", "done", ts(21)),
        (uid(), s["maya_sources"],  P2_ID, "DECISION: Avro over Protobuf for schema serialization. Schema registry compatibility.", "done", ts(21)),
        (uid(), s["maya_sources"],  P2_ID, "Discovered clickstream source sends malformed JSON on session expiry. Wrote sanitization layer.", "done", ts(20)),
        # Maya schema registry (7 days ago)
        (uid(), s["maya_schema"],   P2_ID, "Deployed Confluent Schema Registry. All 8 sources registered with Avro schemas.", "done", ts(7)),
        (uid(), s["maya_schema"],   P2_ID, "Schema evolution policy set: FORWARD_TRANSITIVE. Breaks caught at registration time.", "done", ts(7)),
        (uid(), s["maya_schema"],   P2_ID, "Added schema validation at consumer ingestion. Malformed events routed to DLQ automatically.", "done", ts(6,22)),
        (uid(), s["maya_schema"],   P2_ID, "Discovered payments source ships nullable amount field. Filed HITL: drop or default-0?", "done", ts(6,20)),
        # Jordan DLQ (5-4 days ago)
        (uid(), s["jordan_dlq"],    P2_ID, "Implemented Dead Letter Queue topic per source. Poison pill events routed with error metadata.", "done", ts(5)),
        (uid(), s["jordan_dlq"],    P2_ID, "DLQ consumer: logs error, emits metric, archives to S3 as Parquet for later analysis.", "done", ts(5)),
        (uid(), s["jordan_dlq"],    P2_ID, "Replay tooling: scripts/dlq_replay.py reads S3 archive, re-publishes to source topic.", "done", ts(4)),
        (uid(), s["jordan_dlq"],    P2_ID, "Stress test: injected 10k bad events. All landed in DLQ, none silently dropped. Zero main pipeline impact.", "done", ts(4)),
        # Maya Kafka infra (2 days ago)
        (uid(), s["maya_infra"],    P2_ID, "Kafka consumer group lag monitoring wired to Grafana dashboard.", "done", ts(2)),
        (uid(), s["maya_infra"],    P2_ID, "Consumer auto-scaling: HPA triggers above 50k lag. Tested under 3x load.", "done", ts(2)),
        (uid(), s["maya_infra"],    P2_ID, "Fixed consumer offset commit race on rebalance. Was causing duplicate processing at partition handoff.", "done", ts(2,0,45)),
        # Jordan alerts (yesterday)
        (uid(), s["jordan_alerts"], P2_ID, "PagerDuty alerts wired: DLQ spike (>100/min), consumer lag (>100k), broker disk (>80%).", "done", ts(1,8)),
        (uid(), s["jordan_alerts"], P2_ID, "Alert runbook written for each alert type. On-call rotation documented.", "done", ts(1,6)),
        (uid(), s["jordan_alerts"], P2_ID, "Silence rules: exclude IoT sensor source during 02:00-04:00 maintenance window.", "done", ts(1,4)),
        # Jordan on-call (today)
        (uid(), s["jordan_oncall"], P2_ID, "Updated on-call runbooks for DLQ, Kafka broker failure, schema registry outage.", "done", ts(0,8)),
        (uid(), s["jordan_oncall"], P2_ID, "Filed HITL: should on-call have write access to schema registry in production?", "done", ts(0,6)),
        # Worker 1 Flink (3-2 days ago)
        (uid(), s["worker1_flink"], P2_ID, "Implemented 5-minute tumbling window operator in Flink. Clickstream event counts per user.", "done", ts(3)),
        (uid(), s["worker1_flink"], P2_ID, "Late-arrival handling: 10-minute allowed lateness, side output for beyond-lateness events.", "done", ts(3)),
        (uid(), s["worker1_flink"], P2_ID, "Flink job running in production. Processing 580k events/min at p99 latency 4m12s.", "done", ts(2,18)),
        (uid(), s["worker1_flink"], P2_ID, "Watermark strategy tuned: periodic 1-second watermarks with 2-minute out-of-orderness bound.", "done", ts(2,16)),
        # Worker 1 tests (today)
        (uid(), s["worker1_tests"], P2_ID, "E2E test suite: inject events, verify Iceberg table row counts, check DLQ metrics.", "done", ts(0,6)),
        (uid(), s["worker1_tests"], P2_ID, "34 E2E tests passing. Coverage: all 8 sources, DLQ routing, schema validation, Flink windowing.", "done", ts(0,4)),
        (uid(), s["worker1_tests"], P2_ID, "Running load test: sustained 50M events/day simulation for 1 hour. Monitoring lag.", "in_progress", ts(0,3)),
        # Worker 2 Iceberg (yesterday + today)
        (uid(), s["worker2_iceberg"],P2_ID, "Iceberg table compaction job running nightly. Small file problem resolved.", "done", ts(1)),
        (uid(), s["worker2_iceberg"],P2_ID, "Partition pruning for time-range queries. Query latency reduced 40%% on 30-day lookbacks.", "done", ts(0,16)),
        (uid(), s["worker2_iceberg"],P2_ID, "Snapshot expiry policy: 30 snapshots retained, older purged. Storage costs projected -60%%.", "done", ts(0,14)),
        # Backburner
        (uid(), s["maya_sources"],  P2_ID, "Evaluate Debezium CDC for Postgres change capture instead of polling.", "backburner", ts(20)),
        (uid(), s["jordan_dlq"],    P2_ID, "Build self-service DLQ replay UI in internal dashboard. Currently CLI only.", "backburner", ts(4)),
    ]

def p2_sprint_items():
    return [
        (uid(), P2_ID, "v0.1", "Kafka cluster provisioning (3-broker, k8s)",        "done",    ts(30)),
        (uid(), P2_ID, "v0.1", "Connect all 8 data sources",                         "done",    ts(28)),
        (uid(), P2_ID, "v0.1", "Base consumer framework (Faust)",                    "done",    ts(26)),
        (uid(), P2_ID, "v0.2", "Avro schema registry + schema validation",           "done",    ts(14)),
        (uid(), P2_ID, "v0.2", "Iceberg table sink for processed events",             "done",    ts(12)),
        (uid(), P2_ID, "v0.2", "Flink 5-minute tumbling window operator",            "done",    ts(4)),
        (uid(), P2_ID, "v0.2", "Grafana dashboard: lag, throughput, error rate",     "done",    ts(7)),
        (uid(), P2_ID, "v0.3", "Dead Letter Queue per source topic",                 "done",    ts(5)),
        (uid(), P2_ID, "v0.3", "DLQ replay tooling (scripts/dlq_replay.py)",        "done",    ts(4)),
        (uid(), P2_ID, "v0.3", "Consumer auto-scaling (HPA at 50k lag)",             "done",    ts(2)),
        (uid(), P2_ID, "v0.3", "PagerDuty alerts: DLQ spike, lag, disk",            "done",    ts(1,8)),
        (uid(), P2_ID, "v0.3", "Iceberg table compaction nightly job",               "done",    ts(1)),
        (uid(), P2_ID, "v0.3", "E2E test suite (34 tests)",                         "done",    ts(0,4)),
        (uid(), P2_ID, "v0.3", "Load test: 50M events/day sustained simulation",     "pending", ts(0,3)),
        (uid(), P2_ID, "v0.3", "On-call runbook -- all alert types documented",      "done",    ts(0,8)),
        (uid(), P2_ID, "v0.4", "Schema evolution: backward compatibility CI gate",   "pending", ts(0,0)),
        (uid(), P2_ID, "v0.4", "Data lineage tracking (OpenLineage)",                "pending", ts(0,0)),
        (uid(), P2_ID, "v0.4", "Cost dashboard: Kafka + compute + storage by source","pending", ts(0,0)),
        (uid(), P2_ID, "v0.4", "DLQ self-service replay UI (internal dashboard)",    "pending", ts(0,0)),
        (uid(), P2_ID, "v0.5", "Debezium CDC for Postgres change data capture",      "pending", ts(0,0)),
    ]

def p2_decisions():
    return [
        ("Avro over Protobuf for schema serialization",
         "Avro integrates natively with Confluent Schema Registry. Protobuf requires code generation and cross-team coordination. Avro won on tooling maturity.",
         "TECHNICAL", ts(21)),
        ("Flink over Spark Streaming for windowing",
         "Flink true streaming (not micro-batch). Lower latency, better watermark handling. Spark Streaming evaluated -- 3x higher latency at p99.",
         "TECHNICAL", ts(14)),
        ("Iceberg over Delta Lake and Hudi for table format",
         "Iceberg has best Flink native connector. Partition evolution without rewrite. PyIceberg for Python consumers. Evaluated all three.",
         "TECHNICAL", ts(14)),
        ("Dead Letter Queue per source, not per error type",
         "Per-source DLQs easier to operate -- on-call knows which team owns each queue. Per-error-type DLQs create too many topics.",
         "TECHNICAL", ts(5)),
        ("FORWARD_TRANSITIVE schema compatibility policy",
         "Writers must be backward compatible, readers must handle forward compatible changes. Stricter than FULL but simpler to enforce at registration time.",
         "TECHNICAL", ts(7)),
        ("PagerDuty over OpsGenie for alerting",
         "Company standard -- existing on-call rotation. Integration with Grafana already set up. OpsGenie not evaluated (not on approved vendor list).",
         "PROCESS", ts(2)),
    ]

def p2_hitl(sids):
    h1 = uid()
    h2 = uid()
    h3 = uid()
    return [
        (h1, P2_ID, sids["maya_schema"], "Payments source ships nullable amount field -- drop event or default to 0?",
         "Payment events sometimes have amount: null on refund reversals. Schema requires non-null amount. Options: (1) drop event to DLQ, (2) default 0, (3) add nullable variant to schema.",
         "answered", ts(6,18), "Default 0 for refund reversals -- confirmed with payments team. Add to schema docs. -- Maya"),
        (h2, P2_ID, sids["jordan_oncall"], "Should on-call engineers have write access to schema registry in production?",
         "Current: read-only. Write access needed to hot-fix a bad schema blocking ingestion. Risk: accidental incompatible change breaks consumers.",
         "answered", ts(0,4), "Yes -- but require HITL in Meridian before any production schema write. Audit trail required. -- Jordan"),
        (h3, P2_ID, sids["worker1_tests"], "Load test is hitting 48M events/day max -- short of 50M target. Scale up Kafka brokers or tune consumer parallelism?",
         "CPU headroom exists on brokers. Consumer group at 32 partitions. Network is the bottleneck at current setup.",
         "pending", ts(0,3), None),
    ]


# ---------------------------------------------------------------------------
# Seed runner
# ---------------------------------------------------------------------------

async def clear_project(conn, project_id):
    for tbl in (
        "task_log", "sprint_items", "decisions_pinned", "project_notes",
        "hitl_requests", "sessions_archived", "goal_states", "sessions",
    ):
        try:
            await conn.execute(f"DELETE FROM {tbl} WHERE project_id = %s", (project_id,))
        except Exception:
            pass
    try:
        await conn.execute("UPDATE projects SET decisions = NULL WHERE id = %s", (project_id,))
    except Exception:
        pass
    print(f"  [OK] cleared project {project_id[:8]}")


async def seed():
    conn = await psycopg.AsyncConnection.connect(DEMO_DB_URL, row_factory=dict_row, autocommit=True)
    print("Connected to demo DB.")

    # ---- Project 1: backend-api-v2 ----
    print("\nSeeding backend-api-v2...")
    await clear_project(conn, P1_ID)
    await conn.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (%s,%s,%s) ON CONFLICT (id) DO NOTHING",
        (P1_ID, "backend-api-v2", ts(30))
    )

    await conn.execute(
        "INSERT INTO goal_states (id, project_id, version, content, goal_north_star, goal_sprint, ns_updated_at, content_updated_at, sprint_updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
        (uid(), P1_ID, 1, P1_SPRINT, P1_NORTH_STAR, P1_SPRINT, ts(30), ts(0,1), ts(0,0,30))
    )

    p1_sids = make_p1_sessions()
    for s in p1_sessions(p1_sids):
        await conn.execute(
            "INSERT INTO sessions (id, project_id, human_id, name, agent_framework, client_type, created_at, last_seen) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING", s
        )
    print(f"  [OK] {len(p1_sessions(p1_sids))} sessions")

    tasks_p1 = p1_tasks(p1_sids)
    for t in tasks_p1:
        await conn.execute(
            "INSERT INTO task_log (id, session_id, project_id, description, status, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING", t
        )
    print(f"  [OK] {len(tasks_p1)} tasks")

    sprints_p1 = p1_sprint_items()
    for s in sprints_p1:
        await conn.execute(
            "INSERT INTO sprint_items (id, project_id, version, title, status, added_at) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING", s
        )
    print(f"  [OK] {len(sprints_p1)} sprint items")

    decisions_p1 = p1_decisions()
    for d in decisions_p1:
        await conn.execute(
            "INSERT INTO decisions_pinned (id, project_id, title, body, category, status, created_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,'active',%s,%s) ON CONFLICT (id) DO NOTHING",
            (uid(), P1_ID, d[0], d[1], d[2], d[3], d[3])
        )
    print(f"  [OK] {len(decisions_p1)} decisions")

    hitl_p1 = p1_hitl(p1_sids)
    for h in hitl_p1:
        hid, proj_id, sess_id, question, context, status, created, answer = h
        await conn.execute(
            "INSERT INTO hitl_requests (id, project_id, session_id, question, context, status, answer, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
            (hid, proj_id, sess_id, question, context, status, answer, created)
        )
    print(f"  [OK] {len(hitl_p1)} HITL requests")

    # ---- Project 2: data-pipeline ----
    print("\nSeeding data-pipeline...")
    # Clear any old data-pipeline project (by name)
    cur = await conn.execute("SELECT id FROM projects WHERE name = 'data-pipeline'")
    old_ids = await cur.fetchall()
    for row in old_ids:
        await clear_project(conn, row["id"])
        await conn.execute("DELETE FROM projects WHERE id = %s", (row["id"],))

    await conn.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (%s,%s,%s) ON CONFLICT (id) DO NOTHING",
        (P2_ID, "data-pipeline", ts(30))
    )

    await conn.execute(
        "INSERT INTO goal_states (id, project_id, version, content, goal_north_star, goal_sprint, ns_updated_at, content_updated_at, sprint_updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
        (uid(), P2_ID, 1, P2_SPRINT, P2_NORTH_STAR, P2_SPRINT, ts(30), ts(0,1), ts(0,0,30))
    )

    p2_sids = make_p2_sessions()
    for s in p2_sessions(p2_sids):
        await conn.execute(
            "INSERT INTO sessions (id, project_id, human_id, name, agent_framework, client_type, created_at, last_seen) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING", s
        )
    print(f"  [OK] {len(p2_sessions(p2_sids))} sessions")

    tasks_p2 = p2_tasks(p2_sids)
    for t in tasks_p2:
        await conn.execute(
            "INSERT INTO task_log (id, session_id, project_id, description, status, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING", t
        )
    print(f"  [OK] {len(tasks_p2)} tasks")

    sprints_p2 = p2_sprint_items()
    for s in sprints_p2:
        await conn.execute(
            "INSERT INTO sprint_items (id, project_id, version, title, status, added_at) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING", s
        )
    print(f"  [OK] {len(sprints_p2)} sprint items")

    decisions_p2 = p2_decisions()
    for d in decisions_p2:
        await conn.execute(
            "INSERT INTO decisions_pinned (id, project_id, title, body, category, status, created_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,'active',%s,%s) ON CONFLICT (id) DO NOTHING",
            (uid(), P2_ID, d[0], d[1], d[2], d[3], d[3])
        )
    print(f"  [OK] {len(decisions_p2)} decisions")

    hitl_p2 = p2_hitl(p2_sids)
    for h in hitl_p2:
        hid, proj_id, sess_id, question, context, status, created, answer = h
        await conn.execute(
            "INSERT INTO hitl_requests (id, project_id, session_id, question, context, status, answer, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
            (hid, proj_id, sess_id, question, context, status, answer, created)
        )
    print(f"  [OK] {len(hitl_p2)} HITL requests")

    await conn.close()

    total_tasks = len(tasks_p1) + len(tasks_p2)
    total_sessions = len(p1_sessions(p1_sids)) + len(p2_sessions(p2_sids))
    total_sprints = len(sprints_p1) + len(sprints_p2)
    total_decisions = len(decisions_p1) + len(decisions_p2)
    total_hitl = len(hitl_p1) + len(hitl_p2)

    print(f"""
Demo DB seeded successfully.
  Projects:     2 (backend-api-v2, data-pipeline)
  Sessions:     {total_sessions} ({len(p1_sessions(p1_sids))} + {len(p2_sessions(p2_sids))})
  Tasks:        {total_tasks} ({len(tasks_p1)} + {len(tasks_p2)})
  Sprint items: {total_sprints} ({len(sprints_p1)} + {len(sprints_p2)})
  Decisions:    {total_decisions} ({len(decisions_p1)} + {len(decisions_p2)})
  HITL:         {total_hitl} ({len(hitl_p1)} + {len(hitl_p2)})
""")

asyncio.get_event_loop().run_until_complete(seed())
