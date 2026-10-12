"""Regression tests for sprint item 5c78017e (slice 1): the fan-out ledger.

Covers the storage contract (one ai_log_events row per agent result, the
idempotency key, correlation by run_id), the digest-only rules (quote cap,
hash of the full quote, raw-page fields refused), per-entry best-effort
results, the secret gate, the read-back timeline, and the MCP wiring on the
HTTP dispatch path and the stdio transport.
"""
from __future__ import annotations

import hashlib
import json

import pytest
import pytest_asyncio

import meridian.server  # noqa: F401 - imported before handler to avoid an import cycle
from meridian import db as db_module
from meridian import fanout_ledger as fl
from meridian.mcp import handler as mh
from meridian.mcp_tools import (
    _MCP_TOOLS_LIST,
    _PERSISTENCE_NOTICE,
    _READ_ONLY_TOOLS,
    _TOOL_CATEGORY,
    _TOOL_ROLE_RELEVANCE,
    _TOOL_WORKFLOW_TIER,
)

DATA_DIR = "/tmp/meridian-test-fanout"
RUN = "run-5c78017e-a"


@pytest_asyncio.fixture
async def db():
    conn = await db_module.init_db(":memory:")
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def project(db):
    return await db_module.create_project(db, "fanout-ledger-test")


def _entry(agent_key: str, verdict: str = "supports", **extra) -> dict:
    base = {
        "agent_key": agent_key,
        "verdict": verdict,
        "url": f"https://example.org/{agent_key}",
        "quote": f"quote from {agent_key}",
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# Storage contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_one_row_per_agent_with_contract_fields(db, project):
    result = await fl.record_fanout_results(
        db, project["id"], RUN, [_entry("alpha"), _entry("beta", "refutes")],
    )
    assert result["recorded"] == 2
    assert result["duplicate"] == 0
    assert [r["status"] for r in result["results"]] == ["recorded", "recorded"]

    from meridian.db.ai_log import list_events
    rows = await list_events(db, project["id"], event_type=fl.EVENT_TYPE, limit=50)
    assert len(rows) == 2
    by_actor = {r["actor_id"]: r for r in rows}
    assert set(by_actor) == {"alpha", "beta"}
    alpha = by_actor["alpha"]
    assert alpha["event_type"] == "fanout.agent_result"
    assert alpha["actor_kind"] == "model"
    assert alpha["correlation_id"] == RUN
    assert alpha["payload_schema"] == "agent_result@1"
    assert alpha["idempotency_key"] == f"fanout:{RUN}:alpha"
    assert alpha["payload"]["verdict"] == "supports"
    assert alpha["payload"]["url"] == "https://example.org/alpha"
    assert by_actor["beta"]["payload"]["verdict"] == "refutes"


@pytest.mark.asyncio
async def test_retry_returns_duplicate_and_writes_no_second_row(db, project):
    await fl.record_fanout_results(db, project["id"], RUN, [_entry("alpha")])
    again = await fl.record_fanout_results(
        db, project["id"], RUN, [_entry("alpha", "refutes"), _entry("gamma")],
    )
    assert [r["status"] for r in again["results"]] == ["duplicate", "recorded"]
    assert again["duplicate"] == 1
    assert again["recorded"] == 1

    from meridian.db.ai_log import list_events
    rows = await list_events(db, project["id"], event_type=fl.EVENT_TYPE, limit=50)
    alpha_rows = [r for r in rows if r["actor_id"] == "alpha"]
    assert len(alpha_rows) == 1
    # First write wins: the retried verdict does not overwrite the stored one.
    assert alpha_rows[0]["payload"]["verdict"] == "supports"


@pytest.mark.asyncio
async def test_same_agent_key_in_different_runs_is_not_a_duplicate(db, project):
    a = await fl.record_fanout_results(db, project["id"], "run-one", [_entry("alpha")])
    b = await fl.record_fanout_results(db, project["id"], "run-two", [_entry("alpha")])
    assert a["recorded"] == 1
    assert b["recorded"] == 1


# ---------------------------------------------------------------------------
# Digest-only rules
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_quote_is_capped_and_hash_covers_the_full_quote(db, project):
    full = "x" * (fl.QUOTE_MAX_CHARS + 120)
    result = await fl.record_fanout_results(
        db, project["id"], RUN, [_entry("alpha", quote=full)],
    )
    assert result["recorded"] == 1
    got = await fl.get_fanout_run(db, project["id"], RUN)
    agent = got["agents"][0]
    assert len(agent["quote"]) == fl.QUOTE_MAX_CHARS
    assert agent["quote_truncated"] is True
    expected = "sha256:" + hashlib.sha256(full.encode("utf-8")).hexdigest()
    assert agent["quote_hash"] == expected


@pytest.mark.asyncio
async def test_raw_page_fields_are_refused_and_other_entries_still_recorded(db, project):
    entries = [
        _entry("alpha"),
        {"agent_key": "beta", "verdict": "supports", "body": "<html>raw page</html>"},
        _entry("gamma"),
    ]
    result = await fl.record_fanout_results(db, project["id"], RUN, entries)
    assert [r["status"] for r in result["results"]] == ["recorded", "rejected", "recorded"]
    assert "body" in result["results"][1]["error"]
    assert result["rejected"] == 1
    assert result["recorded"] == 2
    # The rejected raw page never reaches storage.
    got = await fl.get_fanout_run(db, project["id"], RUN)
    assert {a["agent_key"] for a in got["agents"]} == {"alpha", "gamma"}


@pytest.mark.asyncio
async def test_bad_verdict_and_bad_source_hash_are_rejected_per_entry(db, project):
    entries = [
        _entry("alpha", verdict="probably"),
        _entry("beta", source_hash="not-a-hash"),
        _entry("gamma", source_hash="ab" * 32),
    ]
    result = await fl.record_fanout_results(db, project["id"], RUN, entries)
    assert [r["status"] for r in result["results"]] == ["rejected", "rejected", "recorded"]
    got = await fl.get_fanout_run(db, project["id"], RUN)
    assert got["agents"][0]["source_hash"] == "sha256:" + "ab" * 32


@pytest.mark.asyncio
async def test_secret_shaped_quote_is_refused_and_never_stored(db, project):
    # Fake, all-zero AWS-key shape: matches the redaction pattern, is not a real key.
    fake_key = "AKIA" + "0" * 16
    entries = [_entry("alpha", quote=f"token {fake_key} in page"), _entry("beta")]
    result = await fl.record_fanout_results(db, project["id"], RUN, entries)
    assert result["results"][0]["status"] == "rejected"
    assert fake_key not in json.dumps(result)
    assert result["results"][1]["status"] == "recorded"
    got = await fl.get_fanout_run(db, project["id"], RUN)
    assert [a["agent_key"] for a in got["agents"]] == ["beta"]


# ---------------------------------------------------------------------------
# Whole-call validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_whole_call_problems_raise_before_any_write(db, project):
    with pytest.raises(fl.FanoutValidationError):
        await fl.record_fanout_results(db, project["id"], "bad run id!", [_entry("a")])
    with pytest.raises(fl.FanoutValidationError):
        await fl.record_fanout_results(db, project["id"], RUN, [])
    with pytest.raises(fl.FanoutValidationError):
        await fl.record_fanout_results(
            db, project["id"], RUN,
            [_entry(f"a{i}") for i in range(fl.MAX_ENTRIES_PER_CALL + 1)],
        )
    with pytest.raises(fl.FanoutValidationError):
        await fl.record_fanout_results(db, "", RUN, [_entry("a")])
    got = await fl.get_fanout_run(db, project["id"], RUN)
    assert got["count"] == 0


# ---------------------------------------------------------------------------
# Read-back
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_fanout_run_tallies_verdicts_and_isolates_runs(db, project):
    await fl.record_fanout_results(db, project["id"], RUN, [
        _entry("alpha", "supports"), _entry("beta", "refutes"), _entry("gamma", "supports"),
    ])
    await fl.record_fanout_results(db, project["id"], "run-other", [_entry("delta")])
    got = await fl.get_fanout_run(db, project["id"], RUN)
    assert got["count"] == 3
    assert got["verdict_counts"] == {"supports": 2, "refutes": 1}
    assert {a["agent_key"] for a in got["agents"]} == {"alpha", "beta", "gamma"}
    assert got["truncated"] is False
    empty = await fl.get_fanout_run(db, project["id"], "no-such-run")
    assert empty["count"] == 0
    assert empty["agents"] == []


@pytest.mark.asyncio
async def test_get_fanout_run_truncates_to_limit_keeping_newest(db, project):
    await fl.record_fanout_results(db, project["id"], RUN, [
        _entry("alpha"), _entry("beta"), _entry("gamma"),
    ])
    got = await fl.get_fanout_run(db, project["id"], RUN, limit=2)
    assert got["limit"] == 2
    assert got["count"] == 2
    assert got["truncated"] is True


# ---------------------------------------------------------------------------
# MCP wiring: HTTP dispatch path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_dispatch_roundtrip(db, project):
    rec = await mh._handle_session_tools(
        "record_fanout_results",
        {"project_id": project["id"], "run_id": RUN, "entries": [_entry("alpha")]},
        db, DATA_DIR, None, None,
    )
    assert rec["recorded"] == 1
    got = await mh._handle_session_tools(
        "get_fanout_run", {"project_id": project["id"], "run_id": RUN}, db, DATA_DIR, None, None,
    )
    assert got["count"] == 1
    assert got["agents"][0]["agent_key"] == "alpha"


@pytest.mark.asyncio
async def test_mcp_missing_project_id_and_bad_input_return_error_dicts(db):
    missing = await mh._handle_session_tools(
        "record_fanout_results", {"run_id": RUN, "entries": [_entry("a")]}, db, DATA_DIR, None, None,
    )
    assert "error" in missing and "project_id" in missing["error"]
    bad = await mh._handle_session_tools(
        "get_fanout_run", {"project_id": "p", "run_id": "no spaces"}, db, DATA_DIR, None, None,
    )
    assert "error" in bad


# ---------------------------------------------------------------------------
# MCP wiring: schema, tier and category maps
# ---------------------------------------------------------------------------

def _schema(name: str) -> dict:
    return next(t for t in _MCP_TOOLS_LIST if t["name"] == name)


@pytest.mark.parametrize("name", ["record_fanout_results", "get_fanout_run"])
def test_tool_is_registered_in_every_map(name):
    assert _TOOL_CATEGORY.get(name) == "research"
    assert _TOOL_ROLE_RELEVANCE.get(name) == "both"
    assert _TOOL_WORKFLOW_TIER.get(name) == "common-support"
    assert _schema(name)["description"].startswith("[SUPPORT] ")


def test_read_tool_is_read_only_and_write_tool_discloses_persistence():
    assert "get_fanout_run" in _READ_ONLY_TOOLS
    assert "record_fanout_results" not in _READ_ONLY_TOOLS
    assert _PERSISTENCE_NOTICE not in _schema("get_fanout_run")["description"]
    assert _PERSISTENCE_NOTICE in _schema("record_fanout_results")["description"]


def test_entries_schema_bounds_batch_and_verdict_vocabulary():
    entries = _schema("record_fanout_results")["inputSchema"]["properties"]["entries"]
    assert entries["maxItems"] == fl.MAX_ENTRIES_PER_CALL
    verdict = entries["items"]["properties"]["verdict"]
    assert set(verdict["enum"]) == set(fl.VERDICTS)
    assert set(entries["items"]["required"]) == {"agent_key", "verdict"}


# ---------------------------------------------------------------------------
# MCP wiring: stdio transport advertises and routes both tools
# ---------------------------------------------------------------------------

def _build_stdio_server(monkeypatch, db):
    import meridian.server as server_module

    async def _return_db(*_a, **_k):
        return db

    monkeypatch.setattr(db_module, "init_db", _return_db)
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)
    server, _run_stdio = server_module.build_mcp_server()
    return server


@pytest.mark.asyncio
async def test_stdio_advertises_and_routes_fanout_tools(db, project, monkeypatch):
    import mcp.types as mcp_types

    server = _build_stdio_server(monkeypatch, db)
    listed = await server.request_handlers[mcp_types.ListToolsRequest](
        mcp_types.ListToolsRequest(),
    )
    names = {t.name for t in listed.root.tools}
    assert {"record_fanout_results", "get_fanout_run"} <= names

    call = server.request_handlers[mcp_types.CallToolRequest]
    called = await call(mcp_types.CallToolRequest(
        params=mcp_types.CallToolRequestParams(
            name="record_fanout_results",
            arguments={"project_id": project["id"], "run_id": RUN, "entries": [_entry("alpha")]},
        ),
    ))
    result = json.loads(called.root.content[0].text)
    assert result["recorded"] == 1
