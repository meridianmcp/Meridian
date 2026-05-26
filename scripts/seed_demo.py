"""
Idempotent demo DB seeder for usemeridian.us/demo.
Run: pixi run python scripts/seed_demo.py

Safe to re-run — clears all rows for the demo project first, then reseeds
with the expanded content (in_progress + backburner tasks, append-only
decisions log, ROADMAP/DECISIONS/DEVLOG notes, deduped sessions).
"""
import asyncio, selectors, os, sys, uuid, httpx, psycopg
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
    _key = os.environ.get("MERIDIAN_STANDARD_KEY", "")
    if not _key:
        print("ERROR: Set MERIDIAN_DEMO_DB_URL or MERIDIAN_STANDARD_KEY in .env")
        sys.exit(1)
    async def _fetch():
        async with httpx.AsyncClient() as _c:
            _r = await _c.get(
                "https://console.neon.tech/api/v2/projects/blue-smoke-62506461/connection_uri",
                headers={"Authorization": f"Bearer {_key}"},
                params={"database_name": "neondb", "role_name": "neondb_owner", "pooled": "false"},
                timeout=30,
            )
            return _r.json().get("uri", "")
    DEMO_DB_URL = asyncio.get_event_loop().run_until_complete(_fetch())
    print(f"Got demo URL via API: {DEMO_DB_URL[:55]}...")

NOW = datetime.now(timezone.utc)
def ts(days_ago=0, hours_ago=0):
    return (NOW - timedelta(days=days_ago, hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
def datestamp(days_ago=0):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d")
def uid():
    return str(uuid.uuid4())

PROJ_ID      = "25946a49-2dc1-4c9f-bff7-e18827b391c7"
SID_ALICE    = uid()
SID_BOB      = uid()
SID_WORKER1  = uid()
SID_WORKER2  = uid()

NORTH_STAR = (
    "Build a fast, observable REST API that the team can extend without stepping on each other. "
    "Developer experience first — local setup under 2 minutes, CI under 4 minutes, zero DX debt. "
    "Every endpoint typed, every error surfaced, every decision logged. Ship fast without breaking things."
)

VERSION_GOAL = (
    "v1.2.0 — Ship rate limiting (Redis token bucket), API key issuance endpoint, and OpenAPI "
    "spec auto-generation by end of week. Three parallel workstreams: Bob owns rate limiting, "
    "worker sessions own API key endpoint + OpenAPI. All behind feature flag, tests required. "
    "Target: staging deploy Friday, production Monday pending load test."
)

SESSION_FOCUS = (
    "v1.2 — rate limiting + API key auth\n"
    "Worker 1: POST /v1/api-keys endpoint complete. Starting SDK generation.\n"
    "Worker 2: OpenAPI spec merged with api_keys schema. Redoc UI live at /docs.\n"
    "Bob: Rate limiting at 0.25CU after Redis pool fix. Load test passing at 1k req/min."
)

# Append-only decisions log entries — formatted same as set_decision() helper.
# Newest first since UI shows most recent at top.
DECISIONS_LOG_ENTRIES = [
    (3, "TECHNICAL: Cursor-based pagination over offset. Offset pagination breaks when rows inserted "
        "during traversal. Cursor (created_at + id) is stable. Default 20 rows, max 100."),
    (5, "TECHNICAL: API keys prefixed mk_live_ / mk_test_ — Stripe-style. Raw key shown once on creation, "
        "only hash stored. 32-byte base64url = 256 bits entropy."),
    (8, "TECHNICAL: Redis token bucket for rate limiting over in-memory. In-memory rejected — state lost "
        "on restart, breaks horizontal scaling. Bob prototyped both, Redis won."),
    (12, "TECHNICAL: API versioning via URL prefix (/v1/) not Accept header. Easier to route in nginx, "
         "visible in logs, simpler for SDK consumers. Decided in architecture session with Alice."),
    (14, "TECHNICAL: Chose Postgres over MySQL — better JSON operators, pgvector available for future "
         "embedding search, existing team familiarity. MySQL eliminated day 1."),
]


async def _clear_project(conn):
    """Delete all rows for the demo project across all known tables.

    Order respects FK constraints (children before parents). The project row
    itself is preserved if present so existing IDs/references stay valid.
    """
    for tbl in (
        "task_log", "sprint_items", "decisions_pinned", "project_notes",
        "hitl_requests", "sessions_archived", "goal_states", "sessions",
    ):
        try:
            await conn.execute(f"DELETE FROM {tbl} WHERE project_id = %s", (PROJ_ID,))
        except psycopg.errors.UndefinedTable:
            pass
        except Exception as exc:  # noqa: BLE001
            print(f"  [skip] {tbl}: {exc}")
    # Reset decisions log so we don't append on top of stale entries.
    try:
        await conn.execute("UPDATE projects SET decisions = NULL WHERE id = %s", (PROJ_ID,))
    except Exception as exc:  # noqa: BLE001
        print(f"  [skip] projects.decisions reset: {exc}")
    print("  [OK] cleared demo project")


async def seed():
    conn = await psycopg.AsyncConnection.connect(DEMO_DB_URL, row_factory=dict_row, autocommit=True)
    print("Seeding demo DB...")

    await _clear_project(conn)

    # PROJECT
    await conn.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (%s,%s,%s) ON CONFLICT (id) DO NOTHING",
        (PROJ_ID, "backend-api-v2", ts(14))
    )
    print("  [OK] project")

    # GOAL STATE — expanded north star + version goal + session focus
    goal_content = (
        f"north_star: {NORTH_STAR}\n\n"
        f"version_goal: {VERSION_GOAL}\n\n"
        f"sprint: {SESSION_FOCUS}"
    )
    await conn.execute("""
        INSERT INTO goal_states
            (id, project_id, version, content, goal_north_star, goal_sprint,
             ns_updated_at, content_updated_at, sprint_updated_at, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (id) DO NOTHING
    """, (
        uid(), PROJ_ID, 1, goal_content, NORTH_STAR, SESSION_FOCUS,
        ts(14), ts(0, 2), ts(0, 1), ts(0, 1),
    ))
    print("  [OK] goal state")

    # APPEND-ONLY DECISIONS LOG — projects.decisions column
    # Format mirrors db.set_decision(): "[YYYY-MM-DD] <text>\n" newest-first.
    decisions_blob = ""
    for days_ago, text in DECISIONS_LOG_ENTRIES:
        decisions_blob += f"[{datestamp(days_ago)}] {text}\n"
    await conn.execute(
        "UPDATE projects SET decisions = %s WHERE id = %s",
        (decisions_blob.rstrip() + "\n", PROJ_ID),
    )
    print(f"  [OK] decisions log ({len(DECISIONS_LOG_ENTRIES)} entries)")

    # SESSIONS — alice + bob humans, two claude workers parented to alice/bob
    # (id, project_id, human_id, name, agent_framework, client_type, created_at, last_seen)
    sessions = [
        (SID_ALICE,   PROJ_ID, "alice", "Alice Chen",         "claude_code", "claude-desktop", ts(2),   ts(0,8)),
        (SID_BOB,     PROJ_ID, "bob",   "Bob Okafor",         "claude_code", "claude-desktop", ts(1,4), ts(0,4)),
        (SID_WORKER1, PROJ_ID, "alice", "Claude (worker 1)",  "claude_code", "claude-code",    ts(0,6), ts(0,3)),
        (SID_WORKER2, PROJ_ID, "bob",   "Claude (worker 2)",  "claude_code", "claude-code",    ts(0,2), ts(0,1)),
    ]
    for s in sessions:
        await conn.execute("""
            INSERT INTO sessions (id, project_id, human_id, name, agent_framework, client_type, created_at, last_seen)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING
        """, s)
    print("  [OK] sessions")

    # TASK LOG — one task in_progress, one backburner, rest done/pending
    tasks = [
        (uid(), SID_ALICE,   PROJ_ID, "Mapped out v1.2 scope: rate limiting + API key auth + OpenAPI generation. Three parallel workstreams.", "done",        ts(2)),
        (uid(), SID_ALICE,   PROJ_ID, "DECISION: Redis token bucket for rate limiting -- survives deploys, consistent across instances.",     "done",        ts(2)),
        (uid(), SID_ALICE,   PROJ_ID, "Created sprint items for v1.2. Bob takes rate limiting, worker sessions take API key endpoint.",       "done",        ts(2)),
        (uid(), SID_BOB,     PROJ_ID, "Investigated Redis connection pool exhaustion in staging -- leaking on exception path. Fixed.",       "done",        ts(1,6)),
        (uid(), SID_BOB,     PROJ_ID, "Wrote integration tests for token bucket algorithm. 12/12 passing including boundary cases.",         "done",        ts(1,5)),
        (uid(), SID_BOB,     PROJ_ID, "Rate limiting middleware shipped to staging. 1000 req/min load test holding steady.",                 "done",        ts(1,4)),
        (uid(), SID_BOB,     PROJ_ID, "Found off-by-one in window reset logic -- double-counting at boundary. Fixed + test added.",          "done",        ts(1,3)),
        (uid(), SID_BOB,     PROJ_ID, "Raised HITL: Redis vs in-memory for test environment.",                                                "done",        ts(1,2)),
        (uid(), SID_WORKER1, PROJ_ID, "Scaffolded POST /v1/api-keys. Schema: id, key_hash, name, created_at, last_used_at, revoked_at.",     "done",        ts(0,8)),
        (uid(), SID_WORKER1, PROJ_ID, "Implemented key generation -- 32-byte random base64url prefixed mk_live_/mk_test_. Hash stored.",     "done",        ts(0,7)),
        (uid(), SID_WORKER1, PROJ_ID, "Added GET /v1/api-keys (list) and DELETE /v1/api-keys/{id} (revoke). Metadata only, no hash.",        "done",        ts(0,6)),
        (uid(), SID_WORKER1, PROJ_ID, "Tests: create key, authenticate, revoke, reject revoked. All passing.",                              "done",        ts(0,5)),
        (uid(), SID_WORKER2, PROJ_ID, "Integrated FastAPI OpenAPI spec at /openapi.json. Redoc UI at /docs.",                                "done",        ts(0,4)),
        (uid(), SID_WORKER2, PROJ_ID, "Added response schema annotations to all 14 endpoints.",                                              "done",        ts(0,3)),
        (uid(), SID_WORKER2, PROJ_ID, "OpenAPI spec validates against 3.1. Exported to docs/openapi.json.",                                  "done",        ts(0,2)),
        (uid(), SID_WORKER2, PROJ_ID, "Merged api_keys endpoints into spec -- worker 1 had added them while I was working.",                "done",        ts(0,1)),
        # Task 5 — in_progress TypeScript SDK task (was pending)
        (uid(), SID_WORKER2, PROJ_ID, "Generating TypeScript SDK from OpenAPI spec -- openapi-typescript-codegen, custom client wrapper.",   "in_progress", ts(0,0)),
        # Task 5 — backburner GraphQL evaluation task
        (uid(), SID_ALICE,   PROJ_ID, "Evaluate GraphQL as alternative to REST for v2.0 -- benchmarks, schema design, client tooling.",      "backburner",  ts(0,0)),
    ]
    for t in tasks:
        await conn.execute("""
            INSERT INTO task_log (id, session_id, project_id, description, status, created_at)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING
        """, t)
    print(f"  [OK] task log ({len(tasks)} tasks: done/in_progress/backburner)")

    # SPRINT ITEMS
    sprints = [
        (uid(), PROJ_ID, "v1.0", "POST /v1/auth/login -- JWT issuance",          "done",    ts(14)),
        (uid(), PROJ_ID, "v1.0", "GET /v1/users/{id} -- profile endpoint",        "done",    ts(13)),
        (uid(), PROJ_ID, "v1.0", "Postgres connection pooling (asyncpg)",          "done",    ts(12)),
        (uid(), PROJ_ID, "v1.0", "Docker compose for local dev",                  "done",    ts(11)),
        (uid(), PROJ_ID, "v1.0", "CI pipeline -- tests + lint on PR",             "done",    ts(10)),
        (uid(), PROJ_ID, "v1.1", "Cursor-based pagination on list endpoints",      "done",    ts(7)),
        (uid(), PROJ_ID, "v1.1", "X-Request-ID header for distributed tracing",   "done",    ts(6)),
        (uid(), PROJ_ID, "v1.1", "Structured JSON logging via structlog",          "done",    ts(5)),
        (uid(), PROJ_ID, "v1.1", "GET /health endpoint",                          "done",    ts(4)),
        (uid(), PROJ_ID, "v1.2", "Redis token bucket rate limiting middleware",    "done",    ts(1)),
        (uid(), PROJ_ID, "v1.2", "POST /v1/api-keys -- issue API keys",           "done",    ts(0,6)),
        (uid(), PROJ_ID, "v1.2", "DELETE /v1/api-keys/{id} -- revoke keys",       "done",    ts(0,5)),
        (uid(), PROJ_ID, "v1.2", "OpenAPI spec generation + Redoc UI at /docs",   "done",    ts(0,2)),
        (uid(), PROJ_ID, "v1.2", "Python client SDK from OpenAPI spec",           "pending", ts(0,1)),
        (uid(), PROJ_ID, "v1.2", "TypeScript client SDK from OpenAPI spec",       "pending", ts(0,1)),
        (uid(), PROJ_ID, "v1.2", "Load test rate limiter at 10k req/min",         "pending", ts(0,0)),
    ]
    for s in sprints:
        await conn.execute("""
            INSERT INTO sprint_items (id, project_id, version, title, status, added_at)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING
        """, s)
    print(f"  [OK] sprint items ({len(sprints)})")

    # DECISIONS PINNED — same 5 entries; clear-and-seed prevents duplicates
    decisions = [
        ("Postgres over MySQL for primary store",
         "Better JSON support, existing team familiarity, pgvector for future embedding search. MySQL eliminated.", "technical"),
        ("API versioning via URL prefix not Accept header",
         "URL versioning (/v1/) visible in logs, easy to route, simpler for SDK consumers.", "technical"),
        ("Redis token bucket for rate limiting",
         "Survives deploys, works across instances. In-memory rejected -- state lost on restart.", "technical"),
        ("API keys prefixed mk_live_ / mk_test_",
         "Stripe-style prefixes identifiable in logs. Raw key shown once, hash stored. 256 bits entropy.", "technical"),
        ("Cursor-based pagination over offset",
         "Offset breaks on concurrent inserts. Cursor (created_at + id) stable. Default 20 rows, max 100.", "technical"),
    ]
    for d in decisions:
        await conn.execute("""
            INSERT INTO decisions_pinned (id, project_id, title, body, category, status, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,'active',%s,%s) ON CONFLICT (id) DO NOTHING
        """, (uid(), PROJ_ID, d[0], d[1], d[2], ts(7), ts(1)))
    print(f"  [OK] decisions pinned ({len(decisions)})")

    # PROJECT NOTES — wiki entries + ROADMAP/DECISIONS/DEVLOG mirrors
    notes = [
        ("Local dev setup",
         "1. cp .env.example .env\n2. docker compose up -d\n3. make migrate\n4. make dev\nAPI at localhost:8000, docs at /docs",
         "setup", ts(5)),
        ("Redis in tests",
         "Use fakeredis for unit tests. Set TEST_USE_FAKE_REDIS=true in .env.test. Real Redis for integration only.",
         "gotcha", ts(5)),
        ("Deploying to staging",
         "Push staging branch triggers CI. Migrations run via alembic upgrade head. Check Fly logs on failure.",
         "howto", ts(5)),
        ("ROADMAP",
         "v1.0 SHIPPED: Auth (JWT), user profiles, Postgres pooling, Docker compose, CI pipeline\n"
         "v1.1 SHIPPED: Cursor pagination, request tracing, structured logging, health check\n"
         "v1.2 IN PROGRESS: Rate limiting, API keys, OpenAPI spec\n"
         "v1.3 PLANNED: Webhooks (outbound), event streaming via SSE\n"
         "v1.4 PLANNED: Multi-tenant isolation, per-tenant rate limits\n"
         "v2.0 FUTURE: GraphQL layer (under evaluation), SDK generation (Python + TypeScript)",
         "roadmap", ts(1)),
        ("DECISIONS",
         f"{datestamp(14)}: Postgres over MySQL — better JSON support, pgvector available\n"
         f"{datestamp(12)}: API versioning via URL prefix (/v1/) not Accept header\n"
         f"{datestamp(8)}:  Redis token bucket for rate limiting, not in-memory\n"
         f"{datestamp(5)}:  API keys prefixed mk_live_/mk_test_ — Stripe-style, 256-bit entropy\n"
         f"{datestamp(3)}:  Cursor-based pagination — offset breaks on concurrent inserts\n"
         f"{datestamp(2)}:  OpenAPI spec via FastAPI auto-generation, not hand-written",
         "decisions", ts(1)),
        ("DEVLOG",
         f"{datestamp(1)}: Redis connection pool exhaustion in staging — leaking connections on exception "
         "path. Fixed by ensuring pool.release() called in finally block. Bob found it via load test.\n"
         f"{datestamp(3)}: Off-by-one in rate limit window reset — requests at exact boundary "
         "double-counted. Fixed + regression test added.\n"
         f"{datestamp(8)}: Decided against GraphQL for v1.x — REST is simpler for current team size, "
         "GraphQL deferred to v2.0 evaluation.\n"
         f"{datestamp(12)}: Rejected Accept header versioning after Alice prototyped it — too hard to "
         "debug in logs, too complex for SDK consumers.",
         "devlog", ts(0, 6)),
    ]
    for n in notes:
        await conn.execute("""
            INSERT INTO project_notes (id, project_id, title, body, tags, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING
        """, (uid(), PROJ_ID, n[0], n[1], n[2], n[3], n[3]))
    print(f"  [OK] project notes ({len(notes)})")

    # HITL
    await conn.execute("""
        INSERT INTO hitl_requests (id, project_id, session_id, question, context, status, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING
    """, (
        uid(), PROJ_ID, SID_BOB,
        "Should test environment use real Redis or fakeredis?",
        "Rate limiting tests use fakeredis (faster) but miss connection pool issues. Real Redis adds 8s to CI. "
        "Bob: keep fakeredis for unit tests, add redis-integration suite nightly.",
        "pending", ts(1,2)
    ))
    print("  [OK] HITL request")

    await conn.close()
    print("\nDemo DB seeded successfully.")
    print(f"  Project: backend-api-v2 ({PROJ_ID})")
    print(f"  Sessions: {len(sessions)} | Tasks: {len(tasks)} | Sprint items: {len(sprints)}")
    print(f"  Decisions pinned: {len(decisions)} | Decisions log: {len(DECISIONS_LOG_ENTRIES)} | Notes: {len(notes)} | HITL: 1")

asyncio.get_event_loop().run_until_complete(seed())
