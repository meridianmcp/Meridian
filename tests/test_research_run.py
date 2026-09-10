"""Tests for sprint item a5343387 — bounded ephemeral research runs.

Mirrors tests/test_external_job_register.py's structure: model-layer
validation tests (no DB), db-layer lifecycle tests (the `db` fixture), and
MCP-dispatch-level tests (server._dispatch_mcp_tool / the stdio transport).
"""
from __future__ import annotations

import json

import pytest

from meridian import db as db_module
from meridian import research_run as model
from meridian.db import research_runs as run_db
import meridian.mcp_tools as mcp_tools
import meridian.server as server


async def _session(db, prefix: str):
    project = await db_module.create_project(db, prefix)
    session = await db_module.register_session(db, project["id"], f"{prefix}-session")
    return project, session


# ---------------------------------------------------------------------------
# Model-layer validation (no DB)
# ---------------------------------------------------------------------------


def test_validate_run_mode_and_status_reject_unknown_values():
    with pytest.raises(model.ResearchRunError, match="mode must be one of"):
        model.validate_run_mode("read-write")
    assert model.validate_run_mode("READ_ONLY") == "read_only"
    with pytest.raises(model.ResearchRunError, match="status must be one of"):
        model.validate_run_status("done")
    assert model.validate_run_status("Active") == "active"


def test_validate_disposition_is_explicit_never_inferred():
    with pytest.raises(model.ResearchRunError, match="disposition is required"):
        model.validate_disposition(None)
    assert model.validate_disposition(None, required=False) is None
    with pytest.raises(model.ResearchRunError, match="disposition must be one of"):
        model.validate_disposition("archive")
    assert model.validate_disposition("promote") == "promote"


def test_validate_turn_budget_bounds():
    with pytest.raises(model.ResearchRunError, match="positive integer"):
        model.validate_turn_budget(0)
    with pytest.raises(model.ResearchRunError, match="exceeds the maximum"):
        model.validate_turn_budget(model.MAX_TURN_BUDGET + 1)
    with pytest.raises(model.ResearchRunError):
        model.validate_turn_budget("not-a-number")
    assert model.validate_turn_budget(25) == 25


def test_validate_ttl_seconds_defaults_and_bounds():
    assert model.validate_ttl_seconds(None) == model.DEFAULT_TTL_SECONDS
    with pytest.raises(model.ResearchRunError, match="ttl_seconds must be between"):
        model.validate_ttl_seconds(1)
    with pytest.raises(model.ResearchRunError, match="ttl_seconds must be between"):
        model.validate_ttl_seconds(model.MAX_TTL_SECONDS + 1)
    assert model.validate_ttl_seconds(120) == 120


def test_validate_repository_id_rejects_absolute_paths_and_secrets():
    """A5343387 — repository_id is a canonical STRING identity, never a path."""
    with pytest.raises(model.ResearchRunError, match="absolute path"):
        model.validate_repository_id("C:\\Users\\adam\\Meridian\\repository")
    with pytest.raises(model.ResearchRunError, match="absolute path"):
        model.validate_repository_id("/home/adam/repo")
    with pytest.raises(ValueError):
        model.validate_repository_id(
            "token sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        )
    assert (
        model.validate_repository_id("meridian-repo@worktree:wf_a1ea1dc6-002-1")
        == "meridian-repo@worktree:wf_a1ea1dc6-002-1"
    )


def test_validate_allowed_paths_rejects_absolute_paths_and_secret_shaped_values():
    """A5343387 — allowed_paths: project-RELATIVE only, no secrets."""
    with pytest.raises(model.ResearchRunError, match="absolute path"):
        model.validate_allowed_paths(["C:\\Users\\adam\\notes.txt"])
    with pytest.raises(model.ResearchRunError, match="absolute path"):
        model.validate_allowed_paths(["/etc/passwd"])
    with pytest.raises(model.ResearchRunError, match="escape the project root"):
        model.validate_allowed_paths(["../outside/the/repo.py"])
    with pytest.raises(ValueError):
        model.validate_allowed_paths(
            ["config/AKIAABCDEFGHIJKLMNOP.txt"]
        )
    assert model.validate_allowed_paths(["meridian/research_run.py", "tests/"]) == [
        "meridian/research_run.py", "tests/",
    ]
    assert model.validate_allowed_paths(None) == []


def test_validate_run_fields_requires_allowed_paths_for_isolated_write():
    with pytest.raises(model.ResearchRunError, match="isolated_write mode requires"):
        model.validate_run_fields(
            mode="isolated_write", repository_id="repo-id", allowed_paths=None, turn_budget=10,
        )
    fields = model.validate_run_fields(
        mode="isolated_write", repository_id="repo-id",
        allowed_paths=["tests/scratch/"], turn_budget=10,
    )
    assert fields["mode"] == "isolated_write"
    assert fields["allowed_paths"] == ["tests/scratch/"]


def test_is_write_path_allowed_checks_bounded_paths():
    allowed = ["tests/scratch", "docs/notes.md"]
    assert model.is_write_path_allowed("tests/scratch/probe.py", allowed) is True
    assert model.is_write_path_allowed("tests/scratch", allowed) is True
    assert model.is_write_path_allowed("docs/notes.md", allowed) is True
    assert model.is_write_path_allowed("tests/other/file.py", allowed) is False
    assert model.is_write_path_allowed("meridian/server.py", allowed) is False
    assert model.is_write_path_allowed("", allowed) is False


def test_validate_result_receipt_bounds_fields_and_rejects_unknown():
    receipt = model.validate_result_receipt({
        "files_touched": ["tests/test_x.py"],
        "commands_run": ["pytest tests/test_x.py -q"],
        "result_summary": "Confirmed the bug reproduces.",
        "artifact_references": ["finding-123"],
        "failure_reason": None,
    })
    assert receipt["files_touched"] == ["tests/test_x.py"]
    assert receipt["failure_reason"] is None

    with pytest.raises(model.ResearchRunError, match="unknown field"):
        model.validate_result_receipt({"bogus_field": "x"})

    with pytest.raises(model.ResearchRunError, match="absolute path"):
        model.validate_result_receipt({"files_touched": ["/etc/shadow"]})


def test_validate_result_receipt_rejects_over_16kb_deterministically():
    """a5343387 — documented choice: REJECT (raise), never silently truncate,
    once the encoded receipt exceeds the 16KB cap."""
    oversized_summary = "x" * (model.MAX_RESULT_SUMMARY_CHARS)
    # A single huge-but-under-per-field-cap summary, repeated across enough
    # commands_run entries, pushes the ENCODED receipt over the 16KB total
    # cap even though every individual field stays within its own bound.
    many_commands = [f"echo {'y' * 900}" for _ in range(30)]
    with pytest.raises(model.ResearchRunError, match="16000-byte cap|16_000-byte cap|exceeds the 16000"):
        model.validate_result_receipt({
            "result_summary": oversized_summary,
            "commands_run": many_commands,
        })


# ---------------------------------------------------------------------------
# DB-layer lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_complete_and_idempotent_complete(db):
    project, session = await _session(db, "research-run-lifecycle")
    run = await run_db.start_research_run(
        db, project["id"], session["id"],
        mode="read_only", repository_id="meridian-repo@main",
        turn_budget=15,
    )
    assert run["status"] == "active"
    assert run["mode"] == "read_only"
    assert run["allowed_paths"] == []
    assert run["expires_at"]
    assert run["result_receipt"] is None

    completed = await run_db.complete_research_run(
        db, project["id"], session["id"],
        run_id=run["id"],
        receipt={"result_summary": "Investigated the flaky test; root cause found."},
        disposition="keep",
    )
    assert completed["status"] == "completed"
    assert completed["disposition"] == "keep"
    assert completed["result_receipt"]["result_summary"].startswith("Investigated")
    assert completed["completed_at"]

    # Idempotent: a duplicate complete_research_run call on an
    # already-terminal run returns the EXISTING state, no error, and does
    # not require a valid session_id (defense against a stale/rotated caller).
    duplicate = await run_db.complete_research_run(
        db, project["id"], "not-a-real-session-id",
        run_id=run["id"],
        receipt={"result_summary": "a different summary that must be ignored"},
        disposition="discard",
    )
    assert duplicate["id"] == completed["id"]
    assert duplicate["disposition"] == "keep"
    assert duplicate["result_receipt"]["result_summary"] == completed["result_receipt"]["result_summary"]


@pytest.mark.asyncio
async def test_isolated_write_requires_worktree_attestation_and_allowed_paths(db):
    project, session = await _session(db, "research-run-isolated-write")
    with pytest.raises(ValueError, match="isolated_write mode requires is_isolated_worktree"):
        await run_db.start_research_run(
            db, project["id"], session["id"],
            mode="isolated_write", repository_id="meridian-repo@worktree:wf-1",
            allowed_paths=["tests/scratch/"], turn_budget=10,
            is_isolated_worktree=False,
        )
    with pytest.raises(ValueError, match="isolated_write mode requires a non-empty allowed_paths"):
        await run_db.start_research_run(
            db, project["id"], session["id"],
            mode="isolated_write", repository_id="meridian-repo@worktree:wf-1",
            allowed_paths=None, turn_budget=10,
            is_isolated_worktree=True,
        )
    run = await run_db.start_research_run(
        db, project["id"], session["id"],
        mode="isolated_write", repository_id="meridian-repo@worktree:wf-1",
        allowed_paths=["tests/scratch/"], turn_budget=10,
        is_isolated_worktree=True,
    )
    assert run["mode"] == "isolated_write"
    assert run["allowed_paths"] == ["tests/scratch/"]


@pytest.mark.asyncio
async def test_read_only_mode_requires_no_claim_and_no_allowed_paths(db):
    """Read-only runs need no claim_file and no allowed_paths at all."""
    project, session = await _session(db, "research-run-read-only")
    run = await run_db.start_research_run(
        db, project["id"], session["id"],
        mode="read_only", repository_id="meridian-repo@main", turn_budget=5,
    )
    assert run["mode"] == "read_only"
    assert run["allowed_paths"] == []
    # No claim_file call was made anywhere above, and none is required —
    # the run started successfully regardless.


@pytest.mark.asyncio
async def test_failed_and_abandoned_transitions(db):
    project, session = await _session(db, "research-run-transitions")
    run_a = await run_db.start_research_run(
        db, project["id"], session["id"],
        mode="read_only", repository_id="meridian-repo@main", turn_budget=5,
    )
    failed = await run_db.update_research_run(
        db, project["id"], session["id"], run_id=run_a["id"], status="failed",
    )
    assert failed["status"] == "failed"
    assert failed["completed_at"]

    run_b = await run_db.start_research_run(
        db, project["id"], session["id"],
        mode="read_only", repository_id="meridian-repo@main", turn_budget=5,
    )
    abandoned = await run_db.update_research_run(
        db, project["id"], session["id"], run_id=run_b["id"], status="abandoned",
    )
    assert abandoned["status"] == "abandoned"

    # A terminal run cannot be reopened or silently replaced.
    with pytest.raises(ValueError, match="cannot transition"):
        await run_db.update_research_run(
            db, project["id"], session["id"], run_id=run_a["id"], status="active",
        )


@pytest.mark.asyncio
async def test_expire_stale_runs_marks_old_active_runs_expired(db):
    project, session = await _session(db, "research-run-expiry")
    run = await run_db.start_research_run(
        db, project["id"], session["id"],
        mode="read_only", repository_id="meridian-repo@main", turn_budget=5,
    )
    # Force expiry into the past directly (no real 60s+ wait in a unit test).
    await db.execute(
        "UPDATE scratch_research_runs SET expires_at = '2000-01-01T00:00:00' WHERE id = ?",
        (run["id"],),
    )
    await db.commit()

    other_project, other_session = await _session(db, "research-run-expiry-other")
    still_fresh = await run_db.start_research_run(
        db, other_project["id"], other_session["id"],
        mode="read_only", repository_id="meridian-repo@main", turn_budget=5,
    )

    count = await run_db.expire_stale_runs(db, project["id"])
    assert count == 1
    expired = await run_db.get_research_run(db, project["id"], run_id=run["id"])
    assert expired["status"] == "expired"
    assert expired["completed_at"]

    # Scoped to project_id — the other project's fresh run is untouched.
    other_count = await run_db.expire_stale_runs(db, other_project["id"])
    assert other_count == 0
    still = await run_db.get_research_run(db, other_project["id"], run_id=still_fresh["id"])
    assert still["status"] == "active"

    # Idempotent: expiring again finds nothing left to expire.
    assert await run_db.expire_stale_runs(db, project["id"]) == 0


@pytest.mark.asyncio
async def test_list_research_runs_filters_by_status_and_include_terminal(db):
    project, session = await _session(db, "research-run-listing")
    active_run = await run_db.start_research_run(
        db, project["id"], session["id"],
        mode="read_only", repository_id="meridian-repo@main", turn_budget=5,
    )
    done_run = await run_db.start_research_run(
        db, project["id"], session["id"],
        mode="read_only", repository_id="meridian-repo@main", turn_budget=5,
    )
    await run_db.complete_research_run(
        db, project["id"], session["id"], run_id=done_run["id"],
        receipt={"result_summary": "done"}, disposition="discard",
    )

    default_listing = await run_db.list_research_runs(db, project["id"])
    assert [r["id"] for r in default_listing] == [active_run["id"]]

    all_listing = await run_db.list_research_runs(db, project["id"], include_terminal=True)
    assert {r["id"] for r in all_listing} == {active_run["id"], done_run["id"]}

    completed_only = await run_db.list_research_runs(
        db, project["id"], include_terminal=True, status="completed",
    )
    assert [r["id"] for r in completed_only] == [done_run["id"]]

    active_only = await run_db.list_research_runs(db, project["id"], status="active")
    assert [r["id"] for r in active_only] == [active_run["id"]]


@pytest.mark.asyncio
async def test_promote_research_run_creates_a_finding(db):
    project, session = await _session(db, "research-run-promote")
    run = await run_db.start_research_run(
        db, project["id"], session["id"],
        mode="read_only", repository_id="meridian-repo@main", turn_budget=5,
    )
    await run_db.complete_research_run(
        db, project["id"], session["id"], run_id=run["id"],
        receipt={
            "result_summary": "The regression is in the LIKE-pattern escaper.",
            "files_touched": ["meridian/db/__init__.py"],
        },
        disposition="promote",
    )
    result = await run_db.promote_research_run(db, project["id"], session["id"], run_id=run["id"])
    assert result["run_id"] == run["id"]
    finding_note = result["finding"]["note"]
    # NOTE (drift found, not introduced here): save_finding's own docstring
    # calls this a "kind='finding' note", but add_project_note's closed
    # kind vocabulary (wiki|insight|reference|code|document) does not
    # include "finding" -- the value is silently coerced to NULL at write
    # time. The note remains fully discoverable via its TAGS
    # ("finding,<source_type>"), which is the actual mechanism
    # get_notes(tag='finding') (and AGENTS.md's own documented convention)
    # relies on -- asserted below instead of a "kind" field that does not
    # survive the write.
    assert finding_note["note_kind"] is None
    assert "finding" in (finding_note["tags"] or "")
    assert "LIKE-pattern escaper" in finding_note["body"]

    # Actually created and discoverable — not just returned inline.
    findings = await db_module.get_project_notes(db, project["id"], tag="finding")
    assert any(n["id"] == finding_note["id"] for n in findings)


@pytest.mark.asyncio
async def test_promote_research_run_requires_promote_disposition(db):
    project, session = await _session(db, "research-run-promote-guard")
    run = await run_db.start_research_run(
        db, project["id"], session["id"],
        mode="read_only", repository_id="meridian-repo@main", turn_budget=5,
    )
    await run_db.complete_research_run(
        db, project["id"], session["id"], run_id=run["id"],
        receipt={"result_summary": "nothing interesting"}, disposition="discard",
    )
    with pytest.raises(ValueError, match="requires disposition='promote'"):
        await run_db.promote_research_run(db, project["id"], session["id"], run_id=run["id"])


@pytest.mark.asyncio
async def test_project_and_session_scope_are_enforced(db):
    project, session = await _session(db, "research-run-scope-a")
    other_project, other_session = await _session(db, "research-run-scope-b")
    run = await run_db.start_research_run(
        db, project["id"], session["id"],
        mode="read_only", repository_id="meridian-repo@main", turn_budget=5,
    )
    assert await run_db.get_research_run(db, other_project["id"], run_id=run["id"]) is None
    with pytest.raises(ValueError, match="does not belong"):
        await run_db.update_research_run(
            db, project["id"], other_session["id"], run_id=run["id"], status="failed",
        )


# ---------------------------------------------------------------------------
# MCP dispatch level
# ---------------------------------------------------------------------------


def test_mcp_tools_advertise_research_run_surface():
    names = {tool["name"] for tool in mcp_tools._MCP_TOOLS_LIST}
    assert {
        "start_research_run", "complete_research_run", "get_research_run",
        "list_research_runs", "promote_research_run",
    } <= names
    read_only = set(mcp_tools._READ_ONLY_TOOLS)
    assert {"get_research_run", "list_research_runs"} <= read_only


@pytest.mark.asyncio
async def test_mcp_start_complete_get_list_dispatch(db, tmp_path):
    project, session = await _session(db, "research-run-mcp")
    started = await server._dispatch_mcp_tool(
        "start_research_run",
        {
            "project_id": project["id"], "session_id": session["id"],
            "mode": "read_only", "repository_id": "meridian-repo@main",
            "turn_budget": 8,
        },
        db, str(tmp_path),
    )
    run_id = started["run"]["id"]
    assert started["run"]["status"] == "active"

    fetched = await server._dispatch_mcp_tool(
        "get_research_run", {"project_id": project["id"], "run_id": run_id}, db, str(tmp_path),
    )
    assert fetched["run"]["id"] == run_id

    completed = await server._dispatch_mcp_tool(
        "complete_research_run",
        {
            "project_id": project["id"], "session_id": session["id"], "run_id": run_id,
            "receipt": {"result_summary": "probe finished cleanly"},
            "disposition": "keep",
        },
        db, str(tmp_path),
    )
    assert completed["run"]["status"] == "completed"

    listed = await server._dispatch_mcp_tool(
        "list_research_runs",
        {"project_id": project["id"], "include_terminal": True}, db, str(tmp_path),
    )
    assert listed["count"] == 1
    assert listed["runs"][0]["id"] == run_id

    missing = await server._dispatch_mcp_tool(
        "get_research_run", {"project_id": project["id"], "run_id": "no-such-run"}, db, str(tmp_path),
    )
    assert "error" in missing


@pytest.mark.asyncio
async def test_stdio_transport_dispatches_research_run_tools(db, tmp_path, monkeypatch):
    """Claude Desktop's stdio entrypoint must execute the advertised tools."""
    import mcp.types as mcp_types
    import meridian.server as server_module

    project, session = await _session(db, "research-run-stdio")

    async def _return_db(*_args, **_kwargs):
        return db

    monkeypatch.setattr(db_module, "init_db", _return_db)
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    server_instance, _run_stdio = server_module.build_mcp_server()
    call_handler = server_instance.request_handlers[mcp_types.CallToolRequest]
    response = await call_handler(
        mcp_types.CallToolRequest(
            params=mcp_types.CallToolRequestParams(
                name="start_research_run",
                arguments={
                    "project_id": project["id"],
                    "session_id": session["id"],
                    "mode": "read_only",
                    "repository_id": "meridian-repo@main",
                    "turn_budget": 6,
                },
            )
        )
    )
    payload = json.loads(response.root.content[0].text)
    assert payload["run"]["mode"] == "read_only"


# ---------------------------------------------------------------------------
# handoff.py / server.py integration (include_research_runs / active_research_runs)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_continuation_manifest_omits_research_runs_by_default(db):
    """a5343387 — flag-omitted default must be byte-for-byte identical: no
    'research_run_receipts' key at all, not even an empty list."""
    from meridian import handoff as handoff_module

    project, session = await _session(db, "research-run-manifest-default")
    manifest = await handoff_module.build_continuation_manifest(
        db, project["id"], session_id=session["id"], record_revision=False,
    )
    assert "research_run_receipts" not in manifest


@pytest.mark.asyncio
async def test_build_continuation_manifest_includes_kept_and_promoted_runs_only(db):
    from meridian import handoff as handoff_module

    project, session = await _session(db, "research-run-manifest-included")

    kept = await run_db.start_research_run(
        db, project["id"], session["id"],
        mode="read_only", repository_id="meridian-repo@main", turn_budget=5,
    )
    await run_db.complete_research_run(
        db, project["id"], session["id"], run_id=kept["id"],
        receipt={"result_summary": "kept receipt"}, disposition="keep",
    )
    discarded = await run_db.start_research_run(
        db, project["id"], session["id"],
        mode="read_only", repository_id="meridian-repo@main", turn_budget=5,
    )
    await run_db.complete_research_run(
        db, project["id"], session["id"], run_id=discarded["id"],
        receipt={"result_summary": "discarded receipt"}, disposition="discard",
    )
    still_active = await run_db.start_research_run(
        db, project["id"], session["id"],
        mode="read_only", repository_id="meridian-repo@main", turn_budget=5,
    )

    manifest = await handoff_module.build_continuation_manifest(
        db, project["id"], session_id=session["id"], record_revision=False,
        include_research_runs=True,
    )
    receipts = manifest["research_run_receipts"]
    ids = {r["run_id"] for r in receipts}
    assert kept["id"] in ids
    assert discarded["id"] not in ids
    assert still_active["id"] not in ids
    kept_entry = next(r for r in receipts if r["run_id"] == kept["id"])
    assert kept_entry["result_summary"] == "kept receipt"
    assert kept_entry["disposition"] == "keep"


@pytest.mark.asyncio
async def test_build_continue_payload_reports_active_research_runs(db):
    project, session = await _session(db, "research-run-continue-payload")
    await run_db.start_research_run(
        db, project["id"], session["id"],
        mode="read_only", repository_id="meridian-repo@main", turn_budget=5,
    )
    payload = await server._build_continue_payload(db, project["id"], session)
    assert payload["active_research_runs"] == 1
