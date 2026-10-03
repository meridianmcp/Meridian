"""275a8631 — artifact_pointer_check=strict and require_exact_*_output_pointer
are enforced at completion (they used to be advisory), and the MCP schema text
now matches what the code does.

Before this item, ``policy.artifact_pointer_check="strict"`` and
``require_exact_figure/table_output_pointer`` only changed what a HANDOFF said
(``pointers.evaluate_artifact_pointer_policy`` -> ``ready=False``). Nothing
stopped ``complete_sprint_item`` marking such an item done with no output
pointer, while ``mcp_tools._ARTIFACT_POLICY_SCHEMA`` told callers "strict = block
completion". Coverage (each behavior test fails against the pre-fix code):

  * strict item without an exact output pointer -> completion refused; with a
    valid pointer (planned_output OR a stored sprint_item_pointer) -> completes.
  * a bare .docx / directory is not an exact pointer.
  * require_exact_figure/table flags enforce independently of the check level,
    only for an item of that kind, and 'off' switches everything off.
  * OPT-IN: undeclared / default 'warn' / non-figure-table items are unchanged.
  * the only override is human-bound (require_human HITL + audit).
  * the HTTP completion route answers 409 instead of a 500.
  * the schema text no longer claims behavior it lacks.
"""
from __future__ import annotations

import json

import pytest

from meridian import db as db_module
from meridian import gate_override
from meridian import server as srv
from meridian.db import sprint_items as si_mod
from meridian.mcp_tools import _ARTIFACT_POLICY_SCHEMA, _MCP_TOOLS_LIST

_STRICT = {"artifact_pointer_check": "strict"}
_YES = "Yes — approve this override"


def _planned(uri="outputs/figures/error_rate.png", *, target_kind="planned_new"):
    return {
        "source_type": "code",
        "targets": [{
            "uri": uri,
            "selector": {"type": "range", "start_line": 1, "end_line": 1},
            "target_kind": target_kind,
        }],
        "label": "the output",
    }


async def _claimed(db, pid, title, **kw):
    item = await db_module.add_sprint_item(db, pid, "v1", title, **kw)
    await db_module.claim_sprint_item(db, pid, item["id"], actor="exec")
    return item


async def _project(db, name):
    return (await db_module.create_project(db, name))["id"]


async def _audit(db, pid):
    return await db_module.get_action_audit_log(
        db, project_id=pid, event_type=gate_override.ARTIFACT_POINTER_OVERRIDE_EVENT_TYPE,
    )


async def _status(db, item_id):
    return (await db_module.get_sprint_item(db, item_id))["status"]


# ---------------------------------------------------------------------------
# strict: refused without an exact pointer, completes with one
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_strict_figure_item_without_a_pointer_is_refused(db):
    pid = await _project(db, "strict-refused")
    item = await _claimed(
        db, pid, "Plot the error-rate curve",
        artifact_kind="figure", artifact_policy=_STRICT,
    )
    with pytest.raises(si_mod.SprintItemArtifactPointerRequired) as exc:
        await db_module.complete_sprint_item(db, pid, item["id"], actor="exec")
    verdict = exc.value.verdict
    assert verdict["code"] == "ARTIFACT_POINTER_REQUIRED"
    assert verdict["triggers"] == ["artifact_pointer_check=strict"]
    assert verdict["warning_code"] == "missing_pointer"
    assert "override_artifact_pointer" in str(exc.value)
    assert await _status(db, item["id"]) == "in_progress"


@pytest.mark.asyncio
async def test_planned_new_output_remains_discoverable_but_cannot_complete(db):
    pid = await _project(db, "strict-planned")
    item = await _claimed(
        db, pid, "Plot the error-rate curve",
        artifact_kind="figure", artifact_policy=_STRICT, planned_output=_planned(),
    )
    from meridian.pointers import evaluate_artifact_pointer_policy

    planning_verdict = evaluate_artifact_pointer_policy(item)
    assert planning_verdict["ready"] is True
    assert planning_verdict["warning_code"] is None
    with pytest.raises(si_mod.SprintItemArtifactPointerRequired) as exc:
        await db_module.complete_sprint_item(db, pid, item["id"], actor="exec")
    assert exc.value.verdict["warning_code"] == "missing_pointer"
    assert "planned_new" in str(exc.value)
    assert await _status(db, item["id"]) == "in_progress"


@pytest.mark.asyncio
async def test_strict_figure_item_with_existing_planned_output_completes(
    db, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "outputs" / "figures" / "error_rate.png"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"figure")
    pid = await _project(db, "strict-existing")
    item = await _claimed(
        db, pid, "Plot the error-rate curve",
        artifact_kind="figure", artifact_policy=_STRICT,
        planned_output=_planned(target_kind="existing"),
    )
    done = await db_module.complete_sprint_item(db, pid, item["id"], actor="exec")
    assert done["status"] == "done"
    assert "artifact_pointer_override" not in done


@pytest.mark.asyncio
async def test_a_stored_existing_sprint_item_pointer_also_satisfies_strict(
    db, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "outputs" / "figures" / "error_rate.png"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"figure")
    pid = await _project(db, "strict-stored")
    item = await _claimed(
        db, pid, "Plot the error-rate curve",
        artifact_kind="figure", artifact_policy=_STRICT,
    )
    with pytest.raises(si_mod.SprintItemArtifactPointerRequired):
        await db_module.complete_sprint_item(db, pid, item["id"], actor="exec")
    await db_module.add_sprint_item_pointer(
        db, pid, item["id"], "code",
        [{"uri": "outputs/figures/error_rate.png", "target_kind": "existing",
          "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    )
    assert (await db_module.complete_sprint_item(
        db, pid, item["id"], actor="exec",
    ))["status"] == "done"


@pytest.mark.asyncio
async def test_stored_planned_new_pointer_does_not_satisfy_exact_figure_flag(db):
    pid = await _project(db, "required-figure-planned-new")
    item = await _claimed(
        db, pid, "Plot the planned curve", artifact_kind="figure",
        artifact_policy={"require_exact_figure_output_pointer": True},
    )
    await db_module.add_sprint_item_pointer(
        db, pid, item["id"], "code", _planned()["targets"],
    )
    with pytest.raises(si_mod.SprintItemArtifactPointerRequired) as exc:
        await db_module.complete_sprint_item(db, pid, item["id"], actor="exec")
    assert exc.value.verdict["triggers"] == ["require_exact_figure_output_pointer"]
    assert await _status(db, item["id"]) == "in_progress"


@pytest.mark.parametrize("uri,code", [
    ("paper/draft.docx", "insufficient_pointer_bare_docx"),
    ("outputs/figures", "insufficient_pointer_directory"),
])
@pytest.mark.asyncio
async def test_a_bare_docx_or_directory_is_not_an_exact_pointer(
    db, uri, code, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    weak_path = tmp_path / uri
    if uri.endswith(".docx"):
        weak_path.parent.mkdir(parents=True)
        weak_path.write_bytes(b"docx")
    else:
        weak_path.mkdir(parents=True)
    pid = await _project(db, f"strict-weak-{code}")
    item = await _claimed(
        db, pid, "Plot the error-rate curve",
        artifact_kind="figure", artifact_policy=_STRICT,
        planned_output=_planned(uri, target_kind="existing"),
    )
    with pytest.raises(si_mod.SprintItemArtifactPointerRequired) as exc:
        await db_module.complete_sprint_item(db, pid, item["id"], actor="exec")
    assert exc.value.verdict["warning_code"] == code
    assert await _status(db, item["id"]) == "in_progress"


# ---------------------------------------------------------------------------
# require_exact_*: enforced independently of the check level, per kind
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_require_exact_table_flag_is_enforced_even_under_warn(
    db, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "outputs" / "tables" / "ablation.csv"
    output.parent.mkdir(parents=True)
    output.write_text("metric,value\nthroughput,1\n", encoding="utf-8")
    pid = await _project(db, "req-table")
    policy = {"artifact_pointer_check": "warn",
              "require_exact_table_output_pointer": True}
    item = await _claimed(
        db, pid, "Tabulate the ablation results", artifact_kind="table",
        artifact_policy=policy,
    )
    with pytest.raises(si_mod.SprintItemArtifactPointerRequired) as exc:
        await db_module.complete_sprint_item(db, pid, item["id"], actor="exec")
    assert exc.value.verdict["triggers"] == ["require_exact_table_output_pointer"]

    ok = await _claimed(
        db, pid, "Summarise throughput per worker count", artifact_kind="table",
        artifact_policy=policy,
        planned_output=_planned("outputs/tables/ablation.csv", target_kind="existing"),
    )
    assert (await db_module.complete_sprint_item(
        db, pid, ok["id"], actor="exec"))["status"] == "done"


@pytest.mark.asyncio
async def test_a_figure_pointer_does_not_satisfy_the_table_requirement(db):
    pid = await _project(db, "req-table-wrong-kind")
    item = await _claimed(
        db, pid, "Tabulate the ablation results", artifact_kind="table",
        artifact_policy={"require_exact_table_output_pointer": True},
        planned_output=_planned("outputs/figures/ablation.png"),
    )
    with pytest.raises(si_mod.SprintItemArtifactPointerRequired):
        await db_module.complete_sprint_item(db, pid, item["id"], actor="exec")


@pytest.mark.asyncio
async def test_a_require_flag_only_applies_to_an_item_of_that_kind(db):
    pid = await _project(db, "req-figure-on-table")
    item = await _claimed(
        db, pid, "Tabulate the ablation results", artifact_kind="table",
        artifact_policy={"require_exact_figure_output_pointer": True},
    )
    assert (await db_module.complete_sprint_item(
        db, pid, item["id"], actor="exec"))["status"] == "done"


@pytest.mark.asyncio
async def test_require_exact_figure_flag_is_enforced(db):
    pid = await _project(db, "req-figure")
    item = await _claimed(
        db, pid, "Plot the curve", artifact_kind="figure",
        artifact_policy={"require_exact_figure_output_pointer": True},
    )
    with pytest.raises(si_mod.SprintItemArtifactPointerRequired) as exc:
        await db_module.complete_sprint_item(db, pid, item["id"], actor="exec")
    assert exc.value.verdict["triggers"] == ["require_exact_figure_output_pointer"]


# ---------------------------------------------------------------------------
# OPT-IN: nothing changes for items that did not declare enforcement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_undeclared_warn_off_and_non_sensitive_items_are_never_blocked(db):
    pid = await _project(db, "optin")
    cases = {
        "undeclared figure item": dict(artifact_kind="figure"),
        "default warn policy": dict(artifact_kind="figure",
                                    artifact_policy={"artifact_pointer_check": "warn"}),
        "off disables strict-like flags": dict(
            artifact_kind="figure",
            artifact_policy={"artifact_pointer_check": "off",
                             "require_exact_figure_output_pointer": True}),
        "strict but document_only": dict(artifact_kind="document_only",
                                         artifact_policy=_STRICT),
        "strict but no signal at all": dict(artifact_policy=_STRICT),
    }
    for title, kw in cases.items():
        item = await _claimed(db, pid, f"opt-in case: {title}", **kw)
        done = await db_module.complete_sprint_item(db, pid, item["id"], actor="exec")
        assert done["status"] == "done", title
    assert await _audit(db, pid) == []


@pytest.mark.asyncio
async def test_gate_costs_nothing_for_items_that_did_not_opt_in(db, monkeypatch):
    """No pointer lookup at all unless the item opted in."""
    pid = await _project(db, "no-lookup")
    item = await _claimed(db, pid, "plain item")

    async def _boom(*_a, **_k):
        raise AssertionError("pointer rows must not be loaded for a non-opted-in item")

    monkeypatch.setattr(si_mod, "get_sprint_item_pointers", _boom)
    assert (await db_module.complete_sprint_item(
        db, pid, item["id"], actor="exec"))["status"] == "done"


# ---------------------------------------------------------------------------
# MCP surface + the human-bound override
# ---------------------------------------------------------------------------

async def _mcp_strict_item(db, name):
    pid = await _project(db, name)
    item = await _claimed(
        db, pid, "Plot the error-rate curve",
        artifact_kind="figure", artifact_policy=_STRICT,
    )
    return pid, item


def _complete_args(pid, item, **extra):
    return {"project_id": pid, "item_id": item["id"], "actor": "exec",
            "notes": "done", **extra}


@pytest.mark.asyncio
async def test_mcp_refuses_then_completes_once_the_pointer_exists(
    db, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "outputs" / "figures" / "error_rate.png"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"figure")
    pid, item = await _mcp_strict_item(db, "mcp-strict")
    res = await srv._dispatch_mcp_tool("complete_sprint_item", _complete_args(pid, item), db, "/tmp")
    assert res["error"] == "ARTIFACT_POINTER_REQUIRED"
    assert res["artifact_pointer"]["warning_code"] == "missing_pointer"
    assert res["artifact_pointer"]["classification"] == "figure"
    assert await _status(db, item["id"]) == "in_progress"

    await srv._dispatch_mcp_tool(
        "update_sprint_item",
        {"project_id": pid, "item_id": item["id"],
         "planned_output": _planned(target_kind="existing"), "force": True},
        db, "/tmp",
    )
    done = await srv._dispatch_mcp_tool("complete_sprint_item", _complete_args(pid, item), db, "/tmp")
    assert done.get("error") is None and done["status"] == "done"


@pytest.mark.asyncio
async def test_override_needs_a_reason(db):
    pid, item = await _mcp_strict_item(db, "mcp-ov-reason")
    res = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        _complete_args(pid, item, override_artifact_pointer=True), db, "/tmp",
    )
    assert res["error"] == "OVERRIDE_REASON_REQUIRED"
    assert await _status(db, item["id"]) == "in_progress"


@pytest.mark.asyncio
async def test_override_is_human_bound_single_use_and_audited(db):
    pid, item = await _mcp_strict_item(db, "mcp-ov-flow")
    # The audited configuration: aggressive auto-answer must not matter.
    await db_module.update_project_settings(db, pid, hitl_auto_answer=2)
    args = _complete_args(
        pid, item, override_artifact_pointer=True,
        override_reason="figure is produced by the nightly job, pointer not yet known",
    )

    first = await srv._dispatch_mcp_tool("complete_sprint_item", dict(args), db, "/tmp")
    assert first["error"] == "HUMAN_APPROVAL_REQUIRED"
    hitl = await db_module.get_hitl_request(db, first["hitl_id"])
    assert hitl["kind"] == gate_override.GATE_OVERRIDE_HITL_KIND
    assert hitl["status"] == "pending" and hitl["answered_by"] is None
    payload = json.loads(hitl["payload"])
    assert payload["require_human"] is True
    assert payload["gate"] == gate_override.GATE_ARTIFACT_POINTER
    assert payload["subject_id"] == item["id"]
    assert await _status(db, item["id"]) == "in_progress"

    # A retry does not spam the human queue; an unanswered approval is refused.
    again = await srv._dispatch_mcp_tool("complete_sprint_item", dict(args), db, "/tmp")
    assert again["hitl_id"] == first["hitl_id"]
    unanswered = await srv._dispatch_mcp_tool(
        "complete_sprint_item", {**args, "override_hitl_id": first["hitl_id"]}, db, "/tmp",
    )
    assert unanswered["error"] == "HITL_NOT_ANSWERED"
    assert await _status(db, item["id"]) == "in_progress"

    # A human says Yes: completes, records the override, writes the audit row.
    await db_module.answer_hitl_request(db, first["hitl_id"], _YES, answered_by="adam")
    done = await srv._dispatch_mcp_tool(
        "complete_sprint_item", {**args, "override_hitl_id": first["hitl_id"]}, db, "/tmp",
    )
    assert done.get("error") is None and done["status"] == "done"
    assert done["artifact_pointer_override"]["hitl_id"] == first["hitl_id"]
    rows = await _audit(db, pid)
    assert len(rows) == 1
    detail = json.loads(rows[0]["detail"])
    assert detail["subject_id"] == item["id"] and detail["hitl_id"] == first["hitl_id"]
    assert "nightly job" in detail["reason"]

    # The approval is bound to THAT item and is spent: it cannot complete another.
    other = await _claimed(
        db, pid, "Plot another curve", artifact_kind="figure", artifact_policy=_STRICT,
    )
    replay = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {**_complete_args(pid, other, override_artifact_pointer=True,
                          override_reason="same reason"),
         "override_hitl_id": first["hitl_id"]},
        db, "/tmp",
    )
    assert replay["error"] in ("HITL_INVALID", "HITL_ALREADY_USED")
    assert await _status(db, other["id"]) == "in_progress"


@pytest.mark.asyncio
async def test_a_no_answer_or_auto_answer_never_authorizes_the_override(db):
    pid, item = await _mcp_strict_item(db, "mcp-ov-notapproved")
    args = _complete_args(
        pid, item, override_artifact_pointer=True, override_reason="please",
    )
    first = await srv._dispatch_mcp_tool("complete_sprint_item", dict(args), db, "/tmp")
    await db_module.answer_hitl_request(
        db, first["hitl_id"], "No — do not approve", answered_by="adam",
    )
    no = await srv._dispatch_mcp_tool(
        "complete_sprint_item", {**args, "override_hitl_id": first["hitl_id"]}, db, "/tmp",
    )
    assert no["error"] == "HITL_NOT_APPROVED"
    second = await srv._dispatch_mcp_tool("complete_sprint_item", dict(args), db, "/tmp")
    await db_module.answer_hitl_request(db, second["hitl_id"], _YES, answered_by="auto")
    auto = await srv._dispatch_mcp_tool(
        "complete_sprint_item", {**args, "override_hitl_id": second["hitl_id"]}, db, "/tmp",
    )
    assert auto["error"] == "HITL_NOT_APPROVED"
    assert await _status(db, item["id"]) == "in_progress"
    assert await _audit(db, pid) == []


@pytest.mark.asyncio
async def test_filing_the_approval_request_survives_an_unregistered_session_id(db):
    """hitl_requests.session_id is a foreign key; a caller passing an arbitrary
    session label must get the approval request, not a crash."""
    pid, item = await _mcp_strict_item(db, "mcp-ov-badsession")
    res = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        _complete_args(pid, item, override_artifact_pointer=True,
                       override_reason="please", session_id="not-a-registered-session"),
        db, "/tmp",
    )
    assert res["error"] == "HUMAN_APPROVAL_REQUIRED"
    hitl = await db_module.get_hitl_request(db, res["hitl_id"])
    assert hitl["session_id"] is None and hitl["status"] == "pending"


@pytest.mark.asyncio
async def test_override_flags_are_inert_when_the_gate_does_not_block(db):
    pid = await _project(db, "mcp-ov-inert")
    item = await _claimed(db, pid, "plain item")
    done = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        _complete_args(pid, item, override_artifact_pointer=True, override_reason="just in case"),
        db, "/tmp",
    )
    assert done["status"] == "done" and "artifact_pointer_override" not in done
    assert await _audit(db, pid) == []
    assert await db_module.list_hitl_requests(db, pid, status=None) == []


# ---------------------------------------------------------------------------
# HTTP route: a clean 409, not a 500
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_http_complete_route_returns_409_for_a_strict_item_without_a_pointer(
    client, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "outputs" / "figures" / "error_rate.png"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"figure")
    db = client.app.state.db
    pid = await _project(db, "http-strict")
    item = await _claimed(
        db, pid, "Plot the error-rate curve", artifact_kind="figure", artifact_policy=_STRICT,
    )
    r = client.post(f"/projects/{pid}/sprint-items/{item['id']}/complete", json={})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error"] == "ARTIFACT_POINTER_REQUIRED"
    assert await _status(db, item["id"]) == "in_progress"

    await db_module.patch_sprint_item(
        db, pid, item["id"], planned_output=_planned(target_kind="existing")
    )
    ok = client.post(f"/projects/{pid}/sprint-items/{item['id']}/complete", json={})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "done"


# ---------------------------------------------------------------------------
# schema text matches reality (mcp_tools.py:282 claimed blocking that did not exist)
# ---------------------------------------------------------------------------

def test_policy_schema_text_describes_the_enforcement_that_now_exists():
    props = _ARTIFACT_POLICY_SCHEMA["properties"]
    check = props["artifact_pointer_check"]["description"]
    assert "complete_sprint_item refuses" in check and "ARTIFACT_POINTER_REQUIRED" in check
    assert "non-executable" in check, "the handoff half of strict must still be described"
    assert "planned_new" in check and "never satisfies completion" in check
    for flag in ("require_exact_figure_output_pointer", "require_exact_table_output_pointer"):
        text = props[flag]["description"]
        assert "complete_sprint_item refuses" in text and "ARTIFACT_POINTER_REQUIRED" in text
        assert "planned_new" in text and "planning-only" in text
    # allow_document_only_override is consulted by nothing: the text must say so
    # instead of promising a bypass.
    doc_only = props["allow_document_only_override"]["description"]
    assert "NOT consulted" in doc_only and "bypass" not in doc_only.lower()
    assert "human-approved override" in _ARTIFACT_POLICY_SCHEMA["description"]


def test_complete_sprint_item_schema_exposes_the_human_bound_override():
    tool = next(t for t in _MCP_TOOLS_LIST if t["name"] == "complete_sprint_item")
    props = tool["inputSchema"]["properties"]
    assert "override_artifact_pointer" in props and "override_hitl_id" in props
    assert "ARTIFACT_POINTER_REQUIRED" in tool["description"]
    assert "HUMAN-approved" in tool["description"]
