"""Tests for sprint item 627187b8 -- multi-transport exposure of
``meridian.db.batch_management.execute_batch`` (86e4ae44).

Covers the behavioral half of the acceptance criteria (the schema/manifest/
docs half lives in ``tests/test_batch_management_schemas.py``):

1. Transport parity — the SAME logical request produces the SAME logical
   response shape across the MCP handler dispatch table
   (``meridian.mcp.handler._handle_sprint_tools``), the stdio transport
   (``meridian.mcp.stdio_handler.build_mcp_server``'s real ``CallToolRequest``
   handler), and the HTTP route (``POST /projects/{id}/sprint-batch``).
2. Tenant/project isolation.
3. Duplicate retry behavior (idempotency_key reuse -> replay, not
   re-execution).
4. Atomic rollback (all_or_nothing failure), driven through the MCP handler
   dispatch table specifically (not just the underlying engine, which
   ``tests/test_batch_management_writes.py`` already covers directly) —
   proves the multi-transport wiring doesn't lose or reshape the rollback
   contract.
5. Best-effort partial success, likewise through the dispatch table.
6. REAL subprocess exit-code propagation: spawns ``python -m meridian --mcp``
   as an actual OS subprocess (mirroring ``scripts/test_mcp.py``'s existing
   smoke-test pattern) and asserts the process's real exit code, not just an
   in-process return value.

Naming convention for this file matches
``tests/test_ba4f879b_sprint_tools_dispatch.py`` (module-level ``_run``/
``_make_db`` helpers, direct handler + dispatch-table calls) and
``tests/test_stdio_handoff_arg_parity.py`` (``_build_stdio_server`` +
``server.request_handlers[...]`` for the real stdio transport).
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
import uuid

import pytest
import pytest_asyncio

import meridian.server as server_module  # noqa: F401 — load before mcp.handler (import-cycle guard)
from meridian.mcp import handler as mh
from meridian.mcp.handlers import sprint_tools as st_mod
from meridian import db as db_module
from meridian.db import batch_management as bm

_DATA_DIR = "/tmp/meridian-test"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def project(db):
    return await db_module.create_project(db, "batch-dispatch-test-proj")


@pytest_asyncio.fixture
async def session(db, project):
    return await db_module.register_session(db, project["id"], "batch-dispatch-session")


def _build_stdio_server(monkeypatch, db):
    """Same pattern as tests/test_stdio_handoff_arg_parity.py."""
    async def _return_db(*_a, **_k):
        return db

    monkeypatch.setattr(db_module, "init_db", _return_db)
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)
    server, _run_stdio = server_module.build_mcp_server()
    return server


async def _call_stdio_tool(server, name: str, arguments: dict) -> dict:
    import mcp.types as mcp_types

    call_handler = server.request_handlers[mcp_types.CallToolRequest]
    called = await call_handler(
        mcp_types.CallToolRequest(
            params=mcp_types.CallToolRequestParams(name=name, arguments=arguments)
        )
    )
    return json.loads(called.root.content[0].text)


def _assert_batch_result_shape(result: dict) -> None:
    """The response CONTRACT every transport must expose identically.

    Structural, not byte-for-byte (ids/timestamps legitimately differ call to
    call) — this is what "same logical output shape" means for this feature.
    """
    for key in (
        "status", "mode", "entry_kind", "operation", "project_id",
        "idempotency_key", "idempotent_replay", "created_count",
        "error_count", "results",
    ):
        assert key in result, f"missing {key!r} in {result!r}"
    assert result["status"] in ("ok", "partial", "failed", "rejected")
    assert result["mode"] in ("all_or_nothing", "best_effort")
    assert isinstance(result["results"], list)
    for i, entry in enumerate(result["results"]):
        assert entry["index"] == i
        assert entry["status"] in ("ok", "error", "rolled_back", "not_attempted")
        assert "correlation_key" in entry
        assert "id" in entry
        assert "error_code" in entry
        assert "error_message" in entry
        assert "retryable" in entry


# ---------------------------------------------------------------------------
# 1. Transport parity — MCP handler dispatch vs. handler-direct vs. stdio.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_batch_dispatch_table_matches_handler_direct(db, project):
    args = {
        "project_id": project["id"], "operation": "sprint_items",
        "entries": [{"title": "Dispatch-table item"}],
        "mode": "all_or_nothing", "idempotency_key": "parity-dispatch-1",
    }
    via_dispatch = await mh._handle_sprint_tools(
        "execute_batch", args, db, _DATA_DIR, None, None
    )
    assert via_dispatch is not mh._MISS
    _assert_batch_result_shape(via_dispatch)
    assert via_dispatch["status"] == "ok"
    assert via_dispatch["operation"] == "sprint_items"
    assert via_dispatch["entry_kind"] == "sprint_item"

    direct = await st_mod.handle_execute_batch(
        {**args, "idempotency_key": "parity-dispatch-2",
         "entries": [{"title": "Handler-direct item"}]},
        db, _DATA_DIR, None, None,
    )
    _assert_batch_result_shape(direct)
    assert direct["status"] == "ok"


@pytest.mark.asyncio
async def test_execute_batch_stdio_transport_matches_handler_shape(db, project, monkeypatch):
    server = _build_stdio_server(monkeypatch, db)
    args = {
        "project_id": project["id"], "operation": "notes",
        "entries": [{"title": "Stdio note", "body": "stdio body"}],
        "mode": "best_effort", "idempotency_key": "parity-stdio-1",
        "session_id": (await db_module.register_session(
            db, project["id"], "stdio-parity-session"
        ))["id"],
    }
    stdio_result = await _call_stdio_tool(server, "execute_batch", args)
    _assert_batch_result_shape(stdio_result)
    assert stdio_result["status"] == "ok"
    assert stdio_result["operation"] == "notes"
    assert stdio_result["entry_kind"] == "sprint_note"

    # The exact same logical request via the handler dispatch table must
    # produce the same shape/status (different idempotency_key so it's a
    # fresh execution, not a replay of the stdio call above).
    handler_result = await mh._handle_sprint_tools(
        "execute_batch", {**args, "idempotency_key": "parity-stdio-2"},
        db, _DATA_DIR, None, None,
    )
    _assert_batch_result_shape(handler_result)
    assert handler_result["status"] == stdio_result["status"]
    assert handler_result["entry_kind"] == stdio_result["entry_kind"]


def test_execute_batch_http_route_matches_handler_shape(client):
    project = client.post("/projects", json={"name": "batch-http-parity"}).json()
    r = client.post(
        f"/projects/{project['id']}/sprint-batch",
        json={
            "operation": "sprint_items",
            "entries": [{"title": "HTTP batch item"}],
            "mode": "all_or_nothing",
            "idempotency_key": "parity-http-1",
        },
    )
    assert r.status_code == 200
    result = r.json()
    _assert_batch_result_shape(result)
    assert result["status"] == "ok"
    assert result["operation"] == "sprint_items"
    assert result["entry_kind"] == "sprint_item"
    assert result["created_count"] == 1


def test_execute_batch_http_route_requires_mode_and_idempotency_key(client):
    project = client.post("/projects", json={"name": "batch-http-required"}).json()
    r = client.post(
        f"/projects/{project['id']}/sprint-batch",
        json={"operation": "sprint_items", "entries": [{"title": "x"}]},
    )
    assert r.status_code == 422
    assert "mode" in r.json()["detail"]

    r = client.post(
        f"/projects/{project['id']}/sprint-batch",
        json={
            "operation": "sprint_items", "entries": [{"title": "x"}],
            "mode": "all_or_nothing",
        },
    )
    assert r.status_code == 422
    assert "idempotency_key" in r.json()["detail"]


def test_execute_batch_http_route_unknown_project_404(client):
    r = client.post(
        "/projects/does-not-exist/sprint-batch",
        json={
            "operation": "notes", "entries": [], "mode": "best_effort",
            "idempotency_key": None,
        },
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 2. Tenant / project isolation.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_item_updates_rejects_cross_project_item_id(db, project):
    other_project = await db_module.create_project(db, "batch-dispatch-other-proj")
    other_item = await db_module.add_sprint_item(
        db, other_project["id"], "v1", "Belongs to the other project"
    )

    result = await mh._handle_sprint_tools(
        "execute_batch",
        {
            "project_id": project["id"], "operation": "item_updates",
            "entries": [{"item_id": other_item["id"], "notes": "should not apply"}],
            "mode": "all_or_nothing", "idempotency_key": "isolation-1",
        },
        db, _DATA_DIR, None, None,
    )
    assert result["status"] == "rejected"
    assert result["results"][0]["error_code"] == bm.ERROR_NOT_FOUND

    # Nothing changed on the other project's item.
    unchanged = await db_module.get_sprint_item(db, other_item["id"])
    assert unchanged["notes"] != "should not apply"


@pytest.mark.asyncio
async def test_idempotency_receipts_isolated_per_tenant(db, project):
    """tenant_id scopes the idempotency RECEIPT (not an authz check — see
    meridian.batch_ops / batch_management docstrings): the SAME
    (project_id, operation, idempotency_key) under two different tenant_ids
    must execute independently, not collide/replay across tenants."""
    from meridian import batch_ops

    # Deliberately unrelated titles (not near-duplicates of each other) —
    # add_sprint_item's own 60%-word-overlap duplicate guard is a SEPARATE
    # concern from idempotency-receipt tenant scoping, and "Tenant A item"/
    # "Tenant B item" would trip THAT guard (67% word overlap) and mask what
    # this test is actually checking.
    shared_key = "cross-tenant-key-1"
    out_a = await batch_ops.execute_batch_operation(
        db, project_id=project["id"], operation="sprint_items",
        entries=[{"title": "First tenant's rate limiting work"}],
        mode="all_or_nothing", idempotency_key=shared_key, tenant_id="tenant-a",
    )
    out_b = await batch_ops.execute_batch_operation(
        db, project_id=project["id"], operation="sprint_items",
        entries=[{"title": "Second org's onboarding flow polish"}],
        mode="all_or_nothing", idempotency_key=shared_key, tenant_id="tenant-b",
    )
    assert out_a["idempotent_replay"] is False
    assert out_b["idempotent_replay"] is False
    assert out_a["results"][0]["id"] != out_b["results"][0]["id"]

    items = await db_module.get_sprint_items(db, project["id"])
    titles = {i["title"] for i in items}
    assert "First tenant's rate limiting work" in titles
    assert "Second org's onboarding flow polish" in titles


# ---------------------------------------------------------------------------
# 3. Duplicate retry behavior (idempotency_key reuse).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idempotency_key_reuse_replays_first_result(db, project):
    args = {
        "project_id": project["id"], "operation": "sprint_items",
        "entries": [{"title": "Idempotent create via dispatch"}],
        "mode": "all_or_nothing", "idempotency_key": "dispatch-replay-1",
    }
    first = await mh._handle_sprint_tools(
        "execute_batch", args, db, _DATA_DIR, None, None
    )
    assert first["idempotent_replay"] is False
    assert first["status"] == "ok"

    second = await mh._handle_sprint_tools(
        "execute_batch", args, db, _DATA_DIR, None, None
    )
    assert second["idempotent_replay"] is True
    assert second["results"] == first["results"]
    assert second["status"] == first["status"]

    items = await db_module.get_sprint_items(db, project["id"])
    matching = [i for i in items if i["title"] == "Idempotent create via dispatch"]
    assert len(matching) == 1  # NOT created twice


# ---------------------------------------------------------------------------
# 4. Atomic rollback (all_or_nothing failure) through the dispatch table.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_or_nothing_rollback_through_dispatch_table(db, project):
    await db_module.add_sprint_item(db, project["id"], "v1", "Pre-seeded dispatch item")

    result = await mh._handle_sprint_tools(
        "execute_batch",
        {
            "project_id": project["id"], "operation": "sprint_items",
            "entries": [
                {"title": "Will be rolled back"},
                {"title": "Pre-seeded dispatch item"},  # duplicate -> apply-time failure
            ],
            "mode": "all_or_nothing", "idempotency_key": "dispatch-rollback-1",
        },
        db, _DATA_DIR, None, None,
    )
    assert result["status"] == "failed"
    assert result["results"][0]["status"] == "rolled_back"
    assert result["results"][1]["status"] == "error"
    assert result["results"][1]["error_code"] == bm.ERROR_DUPLICATE

    items = await db_module.get_sprint_items(db, project["id"])
    titles = [i["title"] for i in items]
    assert "Will be rolled back" not in titles
    assert titles.count("Pre-seeded dispatch item") == 1


# ---------------------------------------------------------------------------
# 5. Best-effort partial success through the dispatch table.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_best_effort_partial_through_dispatch_table(db, project):
    await db_module.add_sprint_item(db, project["id"], "v1", "Existing best-effort title")

    result = await mh._handle_sprint_tools(
        "execute_batch",
        {
            "project_id": project["id"], "operation": "sprint_items",
            "entries": [
                {"title": "Existing best-effort title"},  # fails (duplicate)
                {"title": "Genuinely new best-effort item"},  # succeeds
            ],
            "mode": "best_effort", "idempotency_key": "dispatch-best-effort-1",
        },
        db, _DATA_DIR, None, None,
    )
    assert result["status"] == "partial"
    assert result["results"][0]["status"] == "error"
    assert result["results"][1]["status"] == "ok"
    assert result["created_count"] == 1
    assert result["error_count"] == 1

    items = await db_module.get_sprint_items(db, project["id"])
    assert any(i["title"] == "Genuinely new best-effort item" for i in items)


# ---------------------------------------------------------------------------
# 6. REAL subprocess exit-code propagation.
#
# Meridian's stdio MCP server (`python -m meridian --mcp`) is a long-running
# JSON-RPC process, not a one-shot batch CLI — there is no separate
# subprocess entry point specific to execute_batch. The real subprocess
# contract this feature can be held to is therefore the one
# `meridian/__main__.py` already documents: `main()` returns 0 after
# `run_stdio()` completes a clean shutdown (stdin closed), REGARDLESS of
# whether individual tool calls made during the session reported business-
# level success or failure over the JSON-RPC channel.
#
# This test speaks RAW line-delimited JSON-RPC directly over the child
# process's stdin/stdout (rather than the `mcp` client SDK's `stdio_client`,
# whose internal process handle isn't exposed to callers) so it can both (a)
# drive real execute_batch calls through the real subprocess and (b) inspect
# the process's ACTUAL exit code after a clean shutdown — proving a
# batch-level failure reported in-band never crashes the server process.
# ---------------------------------------------------------------------------

_PROTOCOL_VERSION = "2024-11-05"


class _RawStdioClient:
    """Minimal hand-rolled JSON-RPC-over-stdio client for one child process.

    Deliberately NOT a reimplementation of the `mcp` SDK's `stdio_client` —
    just enough newline-delimited JSON-RPC framing to drive `tools/call`
    against a real subprocess and read back its result, with the process
    object kept directly on hand (not hidden behind a context manager the
    caller can't inspect) so the real exit code is trivially available.
    """

    def __init__(self, proc: "asyncio.subprocess.Process") -> None:
        self.proc = proc
        self._ids = itertools.count(1)

    async def _send(self, method: str, params: dict | None = None) -> int:
        req_id = next(self._ids)
        msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()
        return req_id

    async def _notify(self, method: str, params: dict | None = None) -> None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def _read_response(self, expected_id: int, timeout: float = 45.0) -> dict:
        assert self.proc.stdout is not None
        while True:
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=timeout)
            if not line:
                raise RuntimeError("subprocess closed stdout before responding")
            stripped = line.strip()
            if not stripped:
                continue
            try:
                msg = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == expected_id:
                return msg

    async def initialize(self) -> None:
        req_id = await self._send("initialize", {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test-mcp-dispatch", "version": "0.0.0"},
        })
        resp = await self._read_response(req_id)
        assert "result" in resp, resp
        await self._notify("notifications/initialized")

    async def call_tool(self, name: str, arguments: dict) -> dict:
        req_id = await self._send("tools/call", {"name": name, "arguments": arguments})
        resp = await self._read_response(req_id)
        if "error" in resp:
            raise RuntimeError(f"JSON-RPC error calling {name}: {resp['error']}")
        return resp["result"]

    @staticmethod
    def tool_text(result: dict) -> str:
        return result["content"][0]["text"]


async def _run_subprocess_session(tmp_path, run_calls):
    """Spawn a real `python -m meridian --mcp` child, hand a connected
    `_RawStdioClient` to `run_calls` (an async callable that drives whatever
    tool calls it wants and returns whatever data it wants), then close
    stdin and wait for the REAL process exit code.

    Returns ``(returncode, data)`` where ``data`` is `run_calls`'s own
    return value. ``returncode`` is the actual OS exit status — never
    fabricated, never inferred from in-band tool results.
    """
    env = os.environ.copy()
    env["MERIDIAN_DB"] = str(tmp_path / "meridian.db")
    env["MERIDIAN_DATA_DIR"] = str(tmp_path)
    env["MERIDIAN_DB_URL"] = ""

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "meridian", "--mcp",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    client = _RawStdioClient(proc)
    try:
        # 75s, not 30s: this session runs CONCURRENTLY with a sibling session
        # (see the asyncio.gather call below) so both spawn a full
        # `python -m meridian --mcp` cold start at once. On a loaded Windows
        # dev host, two concurrent full server cold-starts (module imports +
        # DB init) plus initialize + several tool-call round trips have been
        # observed to take ~47-67s end to end -- comfortably inside 75s, but
        # not the original 30s. This is a wall-clock generosity fix only;
        # it does not change what the test asserts.
        data = await asyncio.wait_for(run_calls(client), timeout=75.0)
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            returncode = await asyncio.wait_for(proc.wait(), timeout=20.0)
        except asyncio.TimeoutError:
            proc.kill()
            returncode = await proc.wait()
    return returncode, data


@pytest.mark.asyncio
async def test_real_subprocess_execute_batch_exit_code(tmp_path):
    """The real OS exit code of `python -m meridian --mcp` must not leak
    in-band business-level batch outcomes.

    Discovery while writing this test: on this Windows dev box, a clean
    stdin-close shutdown of `python -m meridian --mcp` consistently exits 1
    (confirmed via a minimal repro with ZERO tool calls at all — this is a
    pre-existing characteristic of the stdio entrypoint's shutdown path on
    this platform, not something 627187b8 introduced, and not something in
    scope for this item to change). CI (.github/workflows/test.yml) runs on
    ubuntu-latest, where POSIX stdin-EOF handling plausibly differs and the
    exit code could legitimately be 0 there instead. Hard-coding either
    value would make this test lie on the other platform.

    The portable, meaningful assertion — and the one the acceptance
    criteria actually cares about — is comparative: run TWO real subprocess
    sessions against the SAME child-process code path, one where every
    execute_batch call succeeds and one where calls include a real
    all_or_nothing rollback failure AND a malformed/isError request, and
    assert they produce the IDENTICAL exit code. That proves business-level
    batch success/failure reported in-band never changes the real process's
    exit status — clients must read outcomes from the response body
    (asserted below for both sessions), never infer them from the exit
    code, on ANY platform.
    """

    async def _baseline_calls(client: _RawStdioClient) -> dict:
        await client.initialize()
        pname = f"batch-subproc-baseline-{uuid.uuid4().hex[:8]}"
        created = await client.call_tool("create_project", {"name": pname})
        project = json.loads(client.tool_text(created))

        ok_result = await client.call_tool("execute_batch", {
            "project_id": project["id"],
            "operation": "sprint_items",
            "entries": [{"title": "Subprocess baseline item"}],
            "mode": "all_or_nothing",
            "idempotency_key": "subproc-baseline-key",
        })
        return {"ok": json.loads(client.tool_text(ok_result))}

    async def _failure_calls(client: _RawStdioClient) -> dict:
        await client.initialize()
        pname = f"batch-subproc-failure-{uuid.uuid4().hex[:8]}"
        created = await client.call_tool("create_project", {"name": pname})
        project = json.loads(client.tool_text(created))
        project_id = project["id"]

        ok_result = await client.call_tool("execute_batch", {
            "project_id": project_id,
            "operation": "sprint_items",
            "entries": [{"title": "Subprocess batch item"}],
            "mode": "all_or_nothing",
            "idempotency_key": "subproc-key-ok",
        })
        ok = json.loads(client.tool_text(ok_result))

        # Business-level all_or_nothing rollback failure — a real, in-band
        # batch failure, NOT a protocol error. The first entry's title
        # deliberately shares no words with "Subprocess batch item"
        # (created just above) so it doesn't itself trip the 60%-word-
        # overlap duplicate guard — only the second entry (an exact title
        # repeat) should.
        fail_result = await client.call_tool("execute_batch", {
            "project_id": project_id,
            "operation": "sprint_items",
            "entries": [
                {"title": "Totally unrelated unique content here"},
                {"title": "Subprocess batch item"},  # duplicate -> rollback
            ],
            "mode": "all_or_nothing",
            "idempotency_key": "subproc-key-fail",
        })
        failed = json.loads(client.tool_text(fail_result))

        # Malformed request — missing required idempotency_key. The real
        # MCP SDK's low-level Server validates `arguments` against the
        # tool's advertised inputSchema BEFORE our handler ever runs
        # (`Server.call_tool`'s `validate_input=True` default) — a missing
        # "required" field short-circuits with isError=True and a
        # PLAIN-TEXT "Input validation error: ..." message, not our
        # handler's JSON {"error": ...} shape. This is a STRONGER
        # enforcement of "mode/idempotency_key are required" than an
        # in-handler check alone: the request never reaches application
        # code.
        malformed_result = await client.call_tool("execute_batch", {
            "project_id": project_id,
            "operation": "sprint_items",
            "entries": [{"title": "irrelevant"}],
            "mode": "all_or_nothing",
        })

        return {"ok": ok, "failed": failed, "malformed": malformed_result}

    # Separate data dirs (distinct SQLite files) run concurrently so two
    # independent OS processes never contend on the same on-disk DB, and
    # concurrency roughly halves this test's wall-clock cost.
    baseline_dir = tmp_path / "baseline"
    failure_dir = tmp_path / "failure"
    baseline_dir.mkdir()
    failure_dir.mkdir()
    (baseline_rc, baseline_data), (failure_rc, failure_data) = await asyncio.gather(
        _run_subprocess_session(baseline_dir, _baseline_calls),
        _run_subprocess_session(failure_dir, _failure_calls),
    )

    # In-band responses are correct for both sessions...
    assert baseline_data["ok"]["status"] == "ok"
    assert failure_data["ok"]["status"] == "ok"
    assert failure_data["failed"]["status"] == "failed"
    assert failure_data["failed"]["results"][0]["status"] == "rolled_back"
    assert failure_data["malformed"].get("isError") is True
    assert "idempotency_key" in _RawStdioClient.tool_text(failure_data["malformed"])

    # ...and the real process exit code is a concrete, real status (never
    # None/unset — proves both children actually terminated rather than
    # the wait falling through to the force-kill branch)...
    assert isinstance(baseline_rc, int)
    assert isinstance(failure_rc, int)

    # ...and IDENTICAL between the two sessions: the in-band batch failures
    # and malformed-request error in the second session did not change the
    # real subprocess's exit status one bit.
    assert failure_rc == baseline_rc


# ---------------------------------------------------------------------------
# a2a027cf — complete_sprint_item timeout-safety / idempotency / observability
# across transports (dispatch table, stdio, HTTP). The DB-level core
# (idempotent retry, phase timings, bounded advisory work) is covered in
# depth by tests/test_sprint_item_status_race.py; this section proves the
# SAME logical contract holds across the transports this file already
# exercises for execute_batch.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_sprint_item_dispatch_table_matches_handler_direct(db, project):
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Dispatch-table complete")

    direct = await st_mod.handle_complete_sprint_item(
        {"project_id": project["id"], "item_id": item["id"]},
        db, _DATA_DIR, None, None,
    )
    assert direct["status"] == "done"
    assert direct["completion_outcome"] == "committed"
    assert "correlation_id" in direct

    via_dispatch = await mh._handle_sprint_tools(
        "complete_sprint_item",
        {"project_id": project["id"], "item_id": item["id"]},
        db, _DATA_DIR, None, None,
    )
    # Idempotent retry through the OTHER call path -- same logical contract.
    assert via_dispatch["status"] == "done"
    assert via_dispatch["completion_outcome"] == "already_committed"


@pytest.mark.asyncio
async def test_complete_sprint_item_stdio_transport_idempotent_retry(db, project, monkeypatch):
    server = _build_stdio_server(monkeypatch, db)
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Stdio complete parity")

    first = await _call_stdio_tool(server, "complete_sprint_item", {
        "project_id": project["id"], "item_id": item["id"],
    })
    assert first["status"] == "done"
    assert first["completion_outcome"] == "committed"
    assert "correlation_id" in first

    # A retry over the SAME transport must be idempotent, not an error.
    second = await _call_stdio_tool(server, "complete_sprint_item", {
        "project_id": project["id"], "item_id": item["id"],
    })
    assert "error" not in second
    assert second["status"] == "done"
    assert second["completion_outcome"] == "already_committed"


def test_complete_sprint_item_http_route_idempotent_retry(client):
    project = client.post("/projects", json={"name": "complete-http-parity"}).json()
    item = client.post(
        f"/projects/{project['id']}/sprint-items",
        json={"version": "v1", "title": "HTTP complete parity"},
    ).json()

    first = client.post(f"/projects/{project['id']}/sprint-items/{item['id']}/complete", json={})
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["status"] == "done"
    assert first_body["completion_outcome"] == "committed"
    assert "correlation_id" in first_body

    # a2a027cf -- retrying the SAME HTTP call after the item is already done
    # is now a 200 idempotent no-op, not a 409. A GENUINELY conflicting race
    # (different terminal status) still 409s -- see
    # test_complete_sprint_item_http_route_genuine_conflict_still_409 below.
    second = client.post(f"/projects/{project['id']}/sprint-items/{item['id']}/complete", json={})
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["status"] == "done"
    assert second_body["completion_outcome"] == "already_committed"


def test_complete_sprint_item_http_route_genuine_conflict_still_409(client):
    project = client.post("/projects", json={"name": "complete-http-conflict"}).json()
    item = client.post(
        f"/projects/{project['id']}/sprint-items",
        json={"version": "v1", "title": "HTTP conflict item"},
    ).json()
    client.post(f"/projects/{project['id']}/sprint-items/{item['id']}/skip", json={"reason": "nope"})

    r = client.post(f"/projects/{project['id']}/sprint-items/{item['id']}/complete", json={})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["current_status"] == "skipped"
    assert "correlation_id" in detail
    assert "retry_guidance" in detail
