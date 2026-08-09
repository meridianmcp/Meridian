"""e0b88967 (R2-H) — IMPLEMENT: run the AI-log contract matrix and promote
only the local-first core.

Integration gate for the Round 2 AI-log implementation tranche: 9e83be4a
(ExecutionEvent contract, ``meridian.ai_log``/``meridian.db.ai_log``),
ea972129 (retention/redaction design), c5c3fc5f (real capture wiring,
``meridian.session_tools``), 79491e26 (deterministic run-timeline
reconstruction + corrective-handoff trace), and c0168425 (export/purge MCP
surface). Each of those items unit-tested its own narrow surface in its own
file (``tests/test_ai_log_contract.py``, ``tests/test_ai_log_capture.py``,
``tests/test_ai_log_retention.py``, ``tests/test_ai_log_timeline.py``,
``tests/test_ai_log_artifacts.py``) — none of that per-module coverage is
duplicated here. This file is the CROSS-CUTTING gate: does the whole pipeline
(capture -> durable storage -> timeline reconstruction -> corrective handoff
-> export -> purge) actually hold together end to end, across the specific
axes this item's own notes call out:

  1. local-only            — the full lifecycle with no external sink at all.
  2. Redis available/unavailable
  3. OTel/Langfuse available/unavailable
  4. redaction
  5. timeout/recovery
  6. resume/corrective handoff
  7. project isolation
  8. exact deployment readiness

"PROMOTE ONLY THE LOCAL-FIRST CORE": every AI-log capability this codebase
ships production-ready today (the ``ai_log_events``/artifact-store tables,
``meridian.session_tools``'s capture layer, export/purge/timeline) is
SQLite/Postgres + local filesystem only. Per ``meridian.ai_log`` /
``meridian.db.ai_log`` / ``meridian.session_tools`` / ``meridian.artifact_store``'s
own module docstrings, none of them has (or should ever grow) a hard
dependency on Redis or an OTel/Langfuse sink — those remain explicitly
optional, future, non-authoritative layers per the Round 1 design proposal
(e143949d). Section 2/3 below pin that as an executable regression, not just
prose.

GENUINE GAP FOUND + FIXED BY THIS ITEM: prospecting the "resume/corrective
handoff" axis (section 6) surfaced that the ``record_handoff_correction`` MCP
tool (``meridian/mcp/handler.py``'s ``_handle_task_tools`` dispatch — the
interface an executor session actually calls; this codebase is MCP-first,
see AGENTS.md) never recorded the ``handoff.correction_recorded``/
``handoff.correction_regenerated`` durable ai_log trace that
``meridian.routes.handoff.record_handoff_correction_endpoint`` (its REST
mirror) already did via its own ``_log_correction_event`` helper (79491e26).
A correction recorded through the primary MCP interface was therefore
invisible to :func:`meridian.handoff.build_run_timeline_for_handoff` even
though the REST-recorded equivalent was not — exactly the kind of hole an
integration gate exists to catch. Fixed by factoring the trace into a new
shared, best-effort ``meridian.handoff.log_handoff_correction_event`` and
wiring it into the MCP dispatch branch too (see that function's docstring
for the full rationale). Section 6 below is the regression coverage for the
fix; it deliberately does NOT touch ``meridian/routes/handoff.py`` (outside
this item's locked touches_resources) — that file's own, still-correct
``_log_correction_event`` is left as-is.
"""
from __future__ import annotations

import ast
import os

import pytest

import meridian.server  # noqa: F401 — load the server before handler to avoid its import cycle
from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian import process_registry
from meridian import redis_bridge
from meridian import session_tools
from meridian.db import ai_log as ai_log_module
from meridian.mcp import handler as mcp_handler

#: Same secret-shaped fixture string tests/test_ai_log_capture.py already
#: proves trips meridian.secret_redaction.check_for_secrets.
_SECRET = "sk-ant-" + "a" * 40

_LOCAL_FIRST_CORE_MODULES = (
    "meridian/ai_log.py",
    "meridian/db/ai_log.py",
    "meridian/session_tools.py",
    "meridian/artifact_store.py",
)
_EXTERNAL_SINK_ROOTS = ("redis", "langfuse", "opentelemetry")


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


async def _events(db, project_id: str, **filters):
    return await db_module.list_events(db, project_id, **filters)


async def _seed_handoff(db, name: str, tmp_path):
    """Project with a goal + one fresh generated handoff row. Mirrors the
    identically-named helper in tests/test_ai_log_timeline.py /
    tests/test_cov_handoff.py."""
    pid = await _project(db, name)
    await db_module.set_goal(db, pid, "ship it", sprint="s1")
    await handoff_module.generate_handoff(db, pid, str(tmp_path), skip_ai_summary=True)
    rows = await db_module.get_handoffs(db, pid, limit=1)
    return pid, rows[0]


def _mcp_call(client, name, arguments, headers=None):
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=headers or {},
    )


def _result(resp) -> dict:
    import json
    outer = resp.json()
    return json.loads(outer["result"]["content"][0]["text"])


def _setup_authed_project(client, project_name: str):
    import asyncio
    proj_r = client.post("/projects", json={"name": project_name})
    assert proj_r.status_code == 201
    pid = proj_r.json()["id"]

    async def _create_token():
        db = client.app.state.db
        tenant = await db_module.upsert_tenant(db, f"{project_name}@test.invalid")
        raw, _ = await db_module.create_api_token(db, tenant["id"])
        return raw

    token = asyncio.run(_create_token())
    return pid, {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Local-only: full capture -> timeline -> export -> purge pipeline, no
#    external sink involved anywhere.
# ---------------------------------------------------------------------------

@pytest.fixture
def broker(tmp_path):
    return process_registry.ProcessLeaseBroker(persist_path=tmp_path / "leases.json")


@pytest.mark.asyncio
async def test_local_only_full_lifecycle_capture_through_purge(db, broker):
    pid = await _project(db, "matrix-local-only-lifecycle")

    # Capture across every wired boundary this codebase has today: session,
    # tool, and agent/subprocess (via the real register_process wrapper).
    started = await session_tools.capture_session_started(
        db, project_id=pid, session_id="sess-local", role="executor",
    )
    completed = await session_tools.capture_tool_completed(
        db, project_id=pid, tool_name="get_sprint_items", ok=True,
        duration_ms=12.5, session_id="sess-local", correlation_id="corr-1",
    )
    lease = await process_registry.register_process(
        broker, "claude-code", 4242, executable="node",
        capture=lambda lease: session_tools.capture_process_registered(
            db, project_id=pid, run_id=lease.run_id, client=lease.client,
            executable=lease.executable,
        ),
    )
    assert started is not None and completed is not None

    # Storage: every captured event is durably readable back.
    rows = await _events(db, pid)
    assert len(rows) == 3
    event_types = {r["event_type"] for r in rows}
    assert event_types == {"session.started", "tool.completed", "agent.registered"}

    # Timeline: deterministic ascending occurred_at reconstruction sees all 3.
    timeline = await ai_log_module.build_run_timeline(db, pid)
    assert len(timeline) == 3
    assert [e["occurred_at"] for e in timeline] == sorted(e["occurred_at"] for e in timeline)

    # Export: receipted bundle covers everything captured so far.
    bundle = await db_module.export_events(db, pid)
    assert bundle["event_count"] == 3
    assert bundle["truncated"] is False
    assert bundle["export_hash"].startswith("sha256:")

    # Purge: a future cutoff sweeps every row; re-export proves it's gone.
    deleted = await db_module.purge_events_before(db, pid, "2999-01-01T00:00:00Z")
    assert deleted == 3
    empty_bundle = await db_module.export_events(db, pid)
    assert empty_bundle["event_count"] == 0
    assert await _events(db, pid) == []


# ---------------------------------------------------------------------------
# 2. Redis available/unavailable — the AI-log pipeline never depends on it
#    (architecturally, section 3 below), and the one subsystem that DOES
#    (meridian.redis_bridge, for session_messages push) degrades safely on
#    its own, so "Redis unavailable" is a pure no-op for the whole system.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_redis_bridge_state():
    redis_bridge.reset_redis_client_cache()
    yield
    redis_bridge.reset_redis_client_cache()


@pytest.mark.asyncio
async def test_redis_unavailable_never_affects_ai_log_pipeline(db, monkeypatch):
    """The documented self-hosted default (MERIDIAN_REDIS_URL unset) — Redis
    is genuinely unavailable — and the ai_log capture/store/timeline/export
    pipeline completes identically to the local-only baseline above."""
    monkeypatch.delenv("MERIDIAN_REDIS_URL", raising=False)
    assert await redis_bridge.get_redis_client() is None

    pid = await _project(db, "matrix-redis-unavailable")
    await session_tools.capture_session_started(db, project_id=pid, session_id="s1")
    rows = await _events(db, pid)
    assert len(rows) == 1
    timeline = await ai_log_module.build_run_timeline(db, pid)
    assert len(timeline) == 1
    bundle = await db_module.export_events(db, pid)
    assert bundle["event_count"] == 1


@pytest.mark.asyncio
async def test_redis_broken_degrades_the_unrelated_redis_subsystem_safely_too(db, monkeypatch):
    """Simulate Redis being reachable-but-broken (a constructed client whose
    ``.publish()`` call fails — the realistic "connection dropped mid-call"
    shape; ``get_redis_client()`` itself is documented to never raise, see
    its own docstring, so faulting IT would test an impossible contract
    violation rather than a real outage) for the ONE real subsystem that
    legitimately depends on it (session_messages push augmentation,
    meridian.redis_bridge) — proves "Redis down" is a pure availability/perf
    question for THAT subsystem, never a correctness break, and completely
    orthogonal to ai_log (which never calls redis_bridge at all — see
    section 3)."""
    class _BrokenClient:
        async def publish(self, *args, **kwargs):
            raise ConnectionError("simulated Redis outage mid-publish")

    async def _fake_get_client():
        return _BrokenClient()

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    published = await redis_bridge.publish_session_message("some-session", {"x": 1})
    assert published is False  # never raises, degrades to "no push" safely

    # ai_log correctness is untouched by the same simulated outage.
    pid = await _project(db, "matrix-redis-broken-ai-log-unaffected")
    await session_tools.capture_session_started(db, project_id=pid, session_id="s1")
    assert len(await _events(db, pid)) == 1


# ---------------------------------------------------------------------------
# 3. OTel/Langfuse available/unavailable + "promote only the local-first
#    core" — optional telemetry env vars a real OTel/Langfuse integration
#    would read are completely inert to this codebase (nothing reads them),
#    and the local-first modules never hard-import an external sink.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_otel_langfuse_env_vars_present_do_not_alter_capture_behavior(db, monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    pid_baseline = await _project(db, "matrix-otel-baseline")
    await session_tools.capture_tool_completed(
        db, project_id=pid_baseline, tool_name="get_sprint_items", ok=True,
        duration_ms=1.0, session_id="s1",
    )
    baseline = await _events(db, pid_baseline)

    # Plausible env vars a real OTel/Langfuse exporter WOULD read.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid:4318")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "meridian-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "lf-secret-test-should-be-inert")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "lf-public-test")
    monkeypatch.setenv("LANGFUSE_HOST", "https://cloud.langfuse.invalid")
    pid_with_vars = await _project(db, "matrix-otel-vars-present")
    await session_tools.capture_tool_completed(
        db, project_id=pid_with_vars, tool_name="get_sprint_items", ok=True,
        duration_ms=1.0, session_id="s1",
    )
    with_vars = await _events(db, pid_with_vars)

    # Structurally identical outcome (ids/timestamps/project_id legitimately
    # differ per-row; the content that matters does not).
    assert len(baseline) == len(with_vars) == 1
    for key in ("event_type", "actor_kind", "payload", "payload_schema"):
        assert baseline[0][key] == with_vars[0][key]


def test_local_first_core_modules_have_no_hard_import_of_external_sinks():
    """Executable pin for the "promote only the local-first core" contract:
    none of the modules that make up today's production-ready AI-log surface
    imports redis/langfuse/opentelemetry ANYWHERE (module scope or deferred
    inside a function) — matches each module's own docstring claim, checked
    via a real AST walk rather than trusting the prose to stay true."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel_path in _LOCAL_FIRST_CORE_MODULES:
        full_path = os.path.join(repo_root, *rel_path.split("/"))
        with open(full_path, "r", encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source, filename=rel_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for imported in names:
                root = imported.split(".", 1)[0].lower()
                assert root not in _EXTERNAL_SINK_ROOTS, (
                    f"{rel_path} imports {imported!r} — the local-first core "
                    "must never hard-depend on an external telemetry sink"
                )


# ---------------------------------------------------------------------------
# 4. Redaction — the fail-closed write-path gate applies uniformly across
#    EVERY capture boundary helper, not just the generic capture_event path
#    tests/test_ai_log_capture.py already covers.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["session_started", "tool_completed", "process_registered"])
async def test_redaction_blocks_every_capture_boundary_helper(db, broker, boundary):
    pid = await _project(db, f"matrix-redact-{boundary}")
    if boundary == "session_started":
        result = await session_tools.capture_session_started(
            db, project_id=pid, session_id="s1", client=_SECRET,
        )
    elif boundary == "tool_completed":
        result = await session_tools.capture_tool_completed(
            db, project_id=pid, tool_name="x", ok=False, duration_ms=1.0,
            error_type=_SECRET,
        )
    else:
        lease = broker.register("claude-code", 1)
        result = await session_tools.capture_process_registered(
            db, project_id=pid, run_id=lease.run_id, client="claude-code",
            cwd=_SECRET,
        )
    assert result is None
    assert await _events(db, pid) == []


@pytest.mark.asyncio
async def test_redaction_is_a_hard_storage_layer_rejection_not_a_silent_mask(db):
    """Direct db.ai_log.append_event call (bypassing session_tools'
    never-raising wrapper entirely) — the gate lives at the storage layer:
    a secret-shaped payload raises ValueError and inserts nothing, it does
    not silently mask/redact the string in place."""
    pid = await _project(db, "matrix-redact-storage-layer")
    with pytest.raises(ValueError):
        await db_module.append_event(
            db, pid, "tool.invoked", "tool", payload={"arg": _SECRET},
        )
    assert await _events(db, pid) == []


# ---------------------------------------------------------------------------
# 5. Timeout/recovery — a simulated ai_log storage failure at the REAL
#    production dispatch chokepoints (the actual /mcp HTTP surface, and the
#    MCP record_handoff_correction dispatch) never breaks the boundary
#    operation itself.
# ---------------------------------------------------------------------------

def test_real_mcp_dispatch_survives_simulated_ai_log_storage_timeout(client, monkeypatch):
    """End to end through the real /mcp HTTP surface (not just a direct
    session_tools.capture_event unit call, see tests/test_ai_log_capture.py
    section 1) — start_session must still succeed even when the underlying
    ai_log write times out / raises."""
    async def _boom(*args, **kwargs):
        raise TimeoutError("simulated ai_log storage timeout")

    monkeypatch.setattr(db_module, "append_event", _boom)

    pid, headers = _setup_authed_project(client, "matrix-timeout-real-dispatch")
    r = _mcp_call(client, "start_session", {
        "project_id": pid, "session_name": "exec-sess", "role": "executor",
    }, headers)
    assert r.status_code == 200
    assert _result(r)["session_id"]  # the real boundary result is intact

    import asyncio
    rows = asyncio.run(_events(client.app.state.db, pid))
    assert rows == []  # the simulated write failure left no partial row


@pytest.mark.asyncio
async def test_corrective_handoff_mcp_dispatch_survives_simulated_ai_log_timeout(
    db, tmp_path, monkeypatch,
):
    """The NEW log_handoff_correction_event wiring (section 6) is itself
    best-effort — a simulated ai_log write failure must not prevent the
    correction dispatch from returning its normal success result."""
    pid, h = await _seed_handoff(db, "matrix-timeout-corrective-handoff", tmp_path)

    async def _boom(*args, **kwargs):
        raise TimeoutError("simulated ai_log storage timeout")

    monkeypatch.setattr(ai_log_module.AiLogStore, "append", _boom)

    result = await mcp_handler._handle_task_tools(
        "record_handoff_correction",
        {
            "project_id": pid, "source_handoff_id": h["id"],
            "blocker_classification": "other",
        },
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert result["regenerated"] is False
    assert result["correction"]["source_handoff_id"] == h["id"]


# ---------------------------------------------------------------------------
# 6. Resume/corrective handoff — regression coverage for the gap this item
#    found: the record_handoff_correction MCP tool now logs the SAME durable
#    trace the REST mirror already did, and a resuming session can see it.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_record_handoff_correction_now_logs_durable_event(db, tmp_path):
    pid, h = await _seed_handoff(db, "matrix-corrective-mcp-parity", tmp_path)

    result = await mcp_handler._handle_task_tools(
        "record_handoff_correction",
        {
            "project_id": pid, "source_handoff_id": h["id"],
            "blocker_classification": "pointer_unresolved", "session_id": "sess-mcp-1",
        },
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert result["regenerated"] is False

    timeline = await ai_log_module.build_run_timeline(db, pid)
    assert len(timeline) == 1
    event = timeline[0]
    assert event["event_type"] == "handoff.correction_recorded"
    assert event["session_id"] == "sess-mcp-1"
    assert event["correlation_id"] == h["id"]
    assert event["source"] == "mcp"
    assert event["payload"]["correction_id"] == result["correction"]["id"]
    assert event["payload"]["blocker_classification"] == "pointer_unresolved"


@pytest.mark.asyncio
async def test_mcp_record_handoff_correction_regenerate_logs_both_events(db, tmp_path):
    pid, h = await _seed_handoff(db, "matrix-corrective-mcp-regenerate", tmp_path)

    result = await mcp_handler._handle_task_tools(
        "record_handoff_correction",
        {
            "project_id": pid, "source_handoff_id": h["id"],
            "blocker_classification": "other", "regenerate": True,
        },
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert result.get("new_handoff_id")

    timeline = await ai_log_module.build_run_timeline(db, pid)
    event_types = [e["event_type"] for e in timeline]
    assert event_types == ["handoff.correction_recorded", "handoff.correction_regenerated"]
    assert {e["correlation_id"] for e in timeline} == {h["id"]}
    assert timeline[1]["payload"]["new_handoff_id"] == result["new_handoff_id"]


@pytest.mark.asyncio
async def test_resuming_session_sees_mcp_recorded_correction_via_delta_handoff(db, tmp_path):
    """The full resume loop: an executor records a corrective handoff via
    the MCP tool (the interface it actually has), then a (possibly
    different) resuming session asks for generate_handoff(mode='delta') and
    must see that correction on its run_timeline — proving the fix actually
    closes the resumability gap, not just that a row got written somewhere."""
    pid, h = await _seed_handoff(db, "matrix-corrective-resume-loop", tmp_path)

    await mcp_handler._handle_task_tools(
        "record_handoff_correction",
        {
            "project_id": pid, "source_handoff_id": h["id"],
            "blocker_classification": "scope_stale", "session_id": "sess-blocked",
        },
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )

    resumed = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": pid, "mode": "delta"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert resumed["run_timeline"] is not None
    assert resumed["run_timeline"]["event_count"] == 1
    assert resumed["run_timeline"]["events"][0]["event_type"] == "handoff.correction_recorded"
    assert "<run_timeline>" in resumed["content"]


# ---------------------------------------------------------------------------
# 7. Project isolation — across the FULL pipeline in one continuous run, not
#    just one function at a time (each function's own isolation is already
#    covered per-module; this proves it holds end to end together).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_project_isolation_across_full_capture_export_purge_pipeline(db, tmp_path):
    pid_a, h_a = await _seed_handoff(db, "matrix-isolation-a", tmp_path)
    pid_b, _h_b = await _seed_handoff(db, "matrix-isolation-b", tmp_path)

    for pid, sid in ((pid_a, "s-a"), (pid_b, "s-b")):
        await session_tools.capture_session_started(db, project_id=pid, session_id=sid)

    await mcp_handler._handle_task_tools(
        "record_handoff_correction",
        {
            "project_id": pid_a, "source_handoff_id": h_a["id"],
            "blocker_classification": "other",
        },
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )

    # Backdate + purge project A ONLY.
    for row in await _events(db, pid_a):
        await db.execute(
            "UPDATE ai_log_events SET recorded_at = '2020-01-01 00:00:00' WHERE id = ?",
            (row["id"],),
        )
    await db.commit()
    purge_result = await mcp_handler._handle_task_tools(
        "purge_ai_log", {"project_id": pid_a, "cutoff": "2025-01-01T00:00:00Z"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert purge_result["events_deleted"] == 2  # session.started + correction

    # Project A is empty everywhere; project B never saw or lost anything.
    assert await _events(db, pid_a) == []
    assert await ai_log_module.build_run_timeline(db, pid_a) == []
    export_a = await db_module.export_events(db, pid_a)
    assert export_a["event_count"] == 0

    events_b = await _events(db, pid_b)
    assert len(events_b) == 1
    assert events_b[0]["session_id"] == "s-b"
    timeline_b = await ai_log_module.build_run_timeline(db, pid_b)
    assert len(timeline_b) == 1
    export_b = await db_module.export_events(db, pid_b)
    assert export_b["event_count"] == 1
    assert export_b["events"][0]["session_id"] == "s-b"


# ---------------------------------------------------------------------------
# 8. Exact deployment readiness — the export/purge MCP surface is genuinely
#    registered and dispatchable, with correct safety annotations, through
#    the real tools/list + tools/call surface (not just internal dict
#    membership).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ai_log_mcp_tools_registered_with_correct_annotations():
    resp = await mcp_handler._handle_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        db=None, data_dir="/tmp", tenant=None,
    )
    tools_by_name = {t["name"]: t for t in resp["result"]["tools"]}
    for name in ("export_ai_log", "export_ai_log_artifacts", "purge_ai_log"):
        assert name in tools_by_name, f"{name} missing from tools/list"

    for name in ("export_ai_log", "export_ai_log_artifacts"):
        ann = tools_by_name[name]["annotations"]
        assert ann["readOnlyHint"] is True
        assert ann["destructiveHint"] is False

    purge_ann = tools_by_name["purge_ai_log"]["annotations"]
    assert purge_ann["readOnlyHint"] is False
    assert purge_ann["destructiveHint"] is True


@pytest.mark.asyncio
async def test_export_then_purge_backup_workflow_end_to_end_via_real_mcp_dispatch(db, tmp_path):
    """The workflow purge_ai_log's own tool description recommends ("call
    export_ai_log / export_ai_log_artifacts first if the data matters"),
    driven entirely through real MCP dispatch calls."""
    pid = await _project(db, "matrix-export-then-purge")
    data_dir = str(tmp_path)
    old = await db_module.append_event(db, pid, "session.started", "session")
    await db.execute(
        "UPDATE ai_log_events SET recorded_at = '2020-01-01 00:00:00' WHERE id = ?",
        (old["id"],),
    )
    await db.commit()

    snapshot = await mcp_handler._handle_task_tools(
        "export_ai_log", {"project_id": pid}, db, data_dir,
        tenant=None, _mcp_tenant_id=None,
    )
    assert snapshot["event_count"] == 1
    assert snapshot["events"][0]["id"] == old["id"]

    purged = await mcp_handler._handle_task_tools(
        "purge_ai_log", {"project_id": pid, "cutoff": "2025-01-01T00:00:00Z"},
        db, data_dir, tenant=None, _mcp_tenant_id=None,
    )
    assert purged["events_deleted"] == 1

    # What was purged is exactly what the pre-purge export snapshot captured.
    assert await db_module.get_event(db, old["id"]) is None
    post_purge_export = await mcp_handler._handle_task_tools(
        "export_ai_log", {"project_id": pid}, db, data_dir,
        tenant=None, _mcp_tenant_id=None,
    )
    assert post_purge_export["event_count"] == 0
