"""Coverage for the stdio MCP transport's generate_handoff argument parity.

Root cause (see the uncommitted diff to meridian/mcp/stdio_handler.py): the
stdio transport's ``generate_handoff`` Tool schema and dispatch branch only
ever read/forwarded ``mode`` and ``session_id``, silently dropping
``force_include_ids``, ``version``, ``strict_evidence``, and
``strict_pointer_evidence`` even though the canonical HTTP MCP dispatch
(meridian/mcp/handler.py, via ``_dispatch_mcp_tool``) already supported all
four. This file proves the fix: the Tool schema now advertises the new
properties (plus ``"goal"`` in the ``mode`` enum), and the ``call_tool``
dispatch branch actually threads them through to
``handoff_module.generate_handoff`` — mirroring the existing HTTP-transport
coverage in tests/test_b8f89491_handoff_version_scope.py and the stdio-vs-
handler parity test in tests/test_cov_handler.py.
"""
from __future__ import annotations

import json

import pytest

from meridian import db as db_module
import meridian.server  # noqa: F401 — load the server before handler to avoid its import cycle


def _build_stdio_server(monkeypatch, db):
    """Build the stdio MCP server with its lazy DB pinned to ``db``.

    Same pattern as tests/test_cov_handler.py's ``_stdio_server`` and
    tests/test_add_sprint_item_notes.py: monkeypatch ``db_module.init_db`` so
    the server's internal ``_ensure_db`` returns the already-seeded
    in-memory connection instead of opening a fresh on-disk file.
    """
    import meridian.server as server_module

    async def _return_db(*_a, **_k):
        return db

    monkeypatch.setattr(db_module, "init_db", _return_db)
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)
    server, _run_stdio = server_module.build_mcp_server()
    return server


async def _call_generate_handoff(server, arguments):
    import mcp.types as mcp_types

    call_handler = server.request_handlers[mcp_types.CallToolRequest]
    called = await call_handler(
        mcp_types.CallToolRequest(
            params=mcp_types.CallToolRequestParams(
                name="generate_handoff",
                arguments=arguments,
            )
        )
    )
    return json.loads(called.root.content[0].text)


# ---------------------------------------------------------------------------
# 1. Tool schema exposes the new properties + "goal" mode.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stdio_generate_handoff_schema_exposes_new_args(db, monkeypatch):
    import mcp.types as mcp_types

    server = _build_stdio_server(monkeypatch, db)
    list_handler = server.request_handlers[mcp_types.ListToolsRequest]
    listed = await list_handler(mcp_types.ListToolsRequest())
    tool = next(t for t in listed.root.tools if t.name == "generate_handoff")
    props = tool.inputSchema["properties"]

    assert "force_include_ids" in props
    assert props["force_include_ids"]["type"] == "array"
    assert "version" in props
    assert props["version"]["type"] == "string"
    assert "strict_evidence" in props
    assert props["strict_evidence"]["type"] == "boolean"
    assert "strict_pointer_evidence" in props
    assert props["strict_pointer_evidence"]["type"] == "boolean"
    assert "goal" in props["mode"]["enum"]


# ---------------------------------------------------------------------------
# 2. force_include_ids actually threads through the stdio dispatch.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stdio_generate_handoff_force_include_ids_reincludes_deferred(db, monkeypatch):
    project = await db_module.create_project(db, "stdio-force-include")
    future = "2099-01-01T00:00:00"
    deferred = await db_module.add_sprint_item(
        db, project["id"], "v1", "Stdio deferred task", deferred_until=future
    )

    server = _build_stdio_server(monkeypatch, db)

    # Baseline: without force_include_ids, the deferred item stays hidden —
    # proves the assertion below is actually exercising the override, not a
    # handoff that always shows everything.
    baseline = await _call_generate_handoff(
        server, {"project_id": project["id"]}
    )
    assert "Stdio deferred task" not in baseline["content"]

    result = await _call_generate_handoff(
        server,
        {
            "project_id": project["id"],
            "force_include_ids": [deferred["id"]],
        },
    )
    assert "Stdio deferred task" in result["content"]

    # deferred_until is NOT cleared by the one-call override.
    refetched = await db_module.get_sprint_item(db, deferred["id"])
    assert refetched["deferred_until"] == future


# ---------------------------------------------------------------------------
# 3. version scoping works through the stdio path too.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stdio_generate_handoff_version_scopes_to_requested_version(db, monkeypatch):
    project = await db_module.create_project(db, "stdio-version-scope")
    await db_module.add_sprint_item(
        db, project["id"], "v1", "wrong stdio scope unique item", force=True,
    )
    target = await db_module.add_sprint_item(
        db, project["id"], "v2", "correct stdio scope unique item", force=True,
    )

    server = _build_stdio_server(monkeypatch, db)
    result = await _call_generate_handoff(
        server,
        {
            "project_id": project["id"],
            "mode": "full",
            "version": "v2",
        },
    )

    assert target["id"][:8] in result["content"]
    assert "correct stdio scope unique item" in result["content"]
    assert "wrong stdio scope" not in result["content"]


# ---------------------------------------------------------------------------
# Bonus: strict_evidence / HandoffEvidenceRequired mirrored on stdio too —
# the diff added the same try/except HandoffEvidenceRequired branch to
# stdio_handler.py's call_tool as routes/handoff.py got, so it gets the same
# structured-refusal coverage (a raised exception is not appropriate over
# stdio's call_tool contract, which always returns a JSON payload — so it
# must show up as a structured error dict instead).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stdio_generate_handoff_strict_evidence_returns_structured_error(
    db, monkeypatch
):
    project = await db_module.create_project(db, "stdio-strict-evidence")
    await db_module.add_sprint_item(db, project["id"], "v1", "pending item")

    async def _boom(*_a, **_k):
        raise RuntimeError("wave_gate_configs table missing")

    monkeypatch.setattr(db_module, "get_wave_gate_configs", _boom)

    server = _build_stdio_server(monkeypatch, db)
    result = await _call_generate_handoff(
        server,
        {
            "project_id": project["id"],
            "mode": "full",
            "strict_evidence": True,
        },
    )

    assert result["error"] == "HANDOFF_EVIDENCE_BLOCKED"
    assert result["project_id"] == project["id"]
    assert any(
        e["capability"] == "wave_gate_exclusion" for e in result["evidence_errors"]
    )
    # A refused call must not also carry the normal success shape.
    assert "content" not in result
    assert "path" not in result


# ---------------------------------------------------------------------------
# 5. Backward compatibility — omitting the new args changes nothing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stdio_generate_handoff_omitting_new_args_is_backward_compatible(
    db, monkeypatch
):
    """The exact same failed-capability state that trips strict_evidence above
    must degrade gracefully (not refuse) when strict_evidence is simply never
    passed — today's behavior, unchanged."""
    project = await db_module.create_project(db, "stdio-backward-compat")
    await db_module.add_sprint_item(db, project["id"], "v1", "compat item")

    async def _boom(*_a, **_k):
        raise RuntimeError("wave_gate_configs table missing")

    monkeypatch.setattr(db_module, "get_wave_gate_configs", _boom)

    server = _build_stdio_server(monkeypatch, db)
    result = await _call_generate_handoff(
        server, {"project_id": project["id"], "mode": "full"}
    )

    assert "error" not in result
    assert "compat item" in result["content"]
    assert result["mode"] == "full"
