"""Tests for sprint item d09c29fe — gate generate_handoff on DOCX integrity
and unresolved audit findings.

Composes three DOCX-integrity primitives that landed earlier this same
megasprint (meridian.docx_integrity_gate module docstring has the full
rationale):

* 93cd9798 — extensions/meridian-docs render_gate.check_render_capability
* 4efc63fd — extensions/meridian-docs docs_intel.audit_equation_style
* dccc2311 — the same module's write-verification manifest concept
* 6cdc5df3 — meridian.db.proposal_links proposal-to-evidence linkage

Covers:

1. meridian.docx_integrity_gate — pure/DB-backed gate building via injected
   checkers (the extension is not installed in this pixi env — see the
   module docstring — so every live-probe test injects a stub rather than
   requiring a real LibreOffice/Word install).
2. meridian.handoff.build_docx_integrity_gate_for_handoff — the guarded
   wrapper mirroring build_effective_capability_contract /
   build_proposal_evidence_for_handoff.
3. MCP tool surface — docx_integrity present in every generate_handoff mode.
4. HTTP surface — docx_integrity present in POST /projects/{id}/handoff and
   GET /projects/{id}/handoff/planner.
5. Acceptance scenarios: no DOCX findings -> unaffected; an unresolved
   REQUIRED finding -> gated (executable=false, matching the existing
   ready/executable pattern); proposal linkage appears correctly.
"""
from __future__ import annotations

import json as _json

import pytest

from meridian import db as db_module
from meridian import docx_integrity_gate as gate_module
from meridian import handoff as handoff_module


def _rendered(status="rendered", **extra):
    out = {"status": status}
    out.update(extra)
    return out


def _equation_ok(**extra):
    out = {
        "docx_path": None,
        "equation_count": 3,
        "findings": [],
        "finding_count": 0,
        "findings_by_type": {},
        "policy": {"equation_alignment": "center"},
    }
    out.update(extra)
    return out


def _equation_findings(n=2, **extra):
    findings = [{"type": "misaligned_equation", "para_id": f"p{i}"} for i in range(n)]
    out = {
        "docx_path": None,
        "equation_count": n,
        "findings": findings,
        "finding_count": n,
        "findings_by_type": {"misaligned_equation": n},
        "policy": {"equation_alignment": "center"},
    }
    out.update(extra)
    return out


def _snapshot(**extra):
    out = {
        "status": "read_only",
        "byte_size": 4096,
        "paragraph_count": 12,
        "heading_count": 2,
        "xml_parts": ["word/document.xml", "word/styles.xml"],
    }
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# _looks_like_local_docx_uri — pure helper.
# ---------------------------------------------------------------------------

def test_looks_like_local_docx_uri_accepts_relative_and_absolute():
    assert gate_module._looks_like_local_docx_uri("outputs/report.docx")
    assert gate_module._looks_like_local_docx_uri("C:\\work\\thesis.docx")
    assert gate_module._looks_like_local_docx_uri("Report.DOCX")  # case-insensitive


def test_looks_like_local_docx_uri_rejects_non_local_and_non_docx():
    assert not gate_module._looks_like_local_docx_uri("https://example.com/report.docx")
    assert not gate_module._looks_like_local_docx_uri("zotero:ABC123")
    assert not gate_module._looks_like_local_docx_uri("finding:xyz")
    assert not gate_module._looks_like_local_docx_uri("meridian/handoff.py")
    assert not gate_module._looks_like_local_docx_uri(None)
    assert not gate_module._looks_like_local_docx_uri("")


# ---------------------------------------------------------------------------
# build_docx_integrity_gate — no DOCX artifacts at all: unaffected.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_empty_project_is_available_false_and_executable(db):
    """No sprint items, no proposal evidence, no injected checkers -> a
    clean, non-blocking, empty gate. This is the base "unaffected" case."""
    project = await db_module.create_project(db, "docx-gate-empty")
    result = await gate_module.build_docx_integrity_gate(db, project["id"])

    assert result["schema_version"] == gate_module.GATE_SCHEMA_VERSION
    assert result["project_id"] == project["id"]
    assert result["checked_artifacts"] == []
    assert result["candidate_count"] == 0
    assert result["unresolved_required_count"] == 0
    assert result["executable"] is True
    assert result["executable_reasons"] == []
    assert "generated_at" in result


@pytest.mark.asyncio
async def test_gate_plain_sprint_items_with_no_docx_pointers_unaffected(db):
    """Ordinary (non-DOCX) sprint items must not trigger any candidate
    discovery or checker call — the gate stays a no-op."""
    project = await db_module.create_project(db, "docx-gate-plain-items")
    await db_module.add_sprint_item(db, project["id"], "v1", "Refactor the parser")
    await db_module.add_sprint_item(db, project["id"], "v1", "Add a test")

    calls = []

    def _spy_render(path):
        calls.append(path)
        return _rendered()

    result = await gate_module.build_docx_integrity_gate(
        db, project["id"], render_checker=_spy_render,
    )
    assert calls == []
    assert result["checked_artifacts"] == []
    assert result["executable"] is True


# ---------------------------------------------------------------------------
# build_docx_integrity_gate — candidate discovery from sprint_item_pointers.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_discovers_docx_pointer_and_probes_it(db):
    project = await db_module.create_project(db, "docx-gate-pointer-discovery")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Write the thesis chapter",
        artifact_policy={"artifact_pointer_check": "strict"},
    )
    await db_module.add_sprint_item_pointer(
        db, project["id"], item["id"], "docs",
        [{"uri": "outputs/thesis.docx", "selector": {"type": "text_quote", "exact": "conclusion"}}],
        label="thesis output",
    )

    render_calls = []
    equation_calls = []

    def _render(path):
        render_calls.append(path)
        return _rendered()

    def _audit(path):
        equation_calls.append(path)
        return _equation_ok(docx_path=path)

    result = await gate_module.build_docx_integrity_gate(
        db, project["id"],
        render_checker=_render, equation_auditor=_audit,
    )
    # The candidate is discovered, but the file does not exist on disk in
    # this test -> never live-probed (exists_on_disk gates the checkers).
    assert render_calls == []
    assert equation_calls == []
    assert result["candidate_count"] == 1
    artifact = result["checked_artifacts"][0]
    assert artifact["docx_path"] == "outputs/thesis.docx"
    assert artifact["item_id"] == item["id"]
    assert artifact["required"] is True
    assert artifact["exists_on_disk"] is False
    assert artifact["unresolved"] is False
    assert artifact["ready"] is True
    assert result["executable"] is True


@pytest.mark.asyncio
async def test_gate_probes_existing_docx_file_clean(db, tmp_path):
    """A real (empty-stub) file on disk, with no findings from the injected
    checkers, must leave the handoff unaffected — the acceptance criterion
    'a handoff with no DOCX findings is unaffected'."""
    project = await db_module.create_project(db, "docx-gate-clean-file")
    docx_path = tmp_path / "clean.docx"
    docx_path.write_bytes(b"not a real docx, just needs to exist")

    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Ship the clean report",
        artifact_policy={"artifact_pointer_check": "strict"},
    )
    await db_module.add_sprint_item_pointer(
        db, project["id"], item["id"], "docs",
        [{"uri": str(docx_path), "selector": {"type": "text_quote", "exact": "x"}}],
    )

    result = await gate_module.build_docx_integrity_gate(
        db, project["id"],
        render_checker=lambda p: _rendered(),
        equation_auditor=lambda p: _equation_ok(docx_path=p),
        snapshot_reader=lambda p: _snapshot(),
    )
    artifact = result["checked_artifacts"][0]
    assert artifact["exists_on_disk"] is True
    assert artifact["render_status"] == "rendered"
    assert artifact["equation_audit"]["finding_count"] == 0
    assert artifact["provenance_manifest"]["manifest_hash"]
    assert artifact["unresolved"] is False
    assert artifact["ready"] is True
    assert result["unresolved_required_count"] == 0
    assert result["executable"] is True


# ---------------------------------------------------------------------------
# Gating — an unresolved REQUIRED finding blocks; warn/off never does.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_blocks_on_required_equation_findings(db, tmp_path):
    project = await db_module.create_project(db, "docx-gate-blocks-strict")
    docx_path = tmp_path / "bad.docx"
    docx_path.write_bytes(b"stub")

    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Finalize the equations",
        artifact_policy={"artifact_pointer_check": "strict"},
    )
    await db_module.add_sprint_item_pointer(
        db, project["id"], item["id"], "docs",
        [{"uri": str(docx_path), "selector": {"type": "text_quote", "exact": "eq"}}],
    )

    result = await gate_module.build_docx_integrity_gate(
        db, project["id"],
        render_checker=lambda p: _rendered(),
        equation_auditor=lambda p: _equation_findings(2, docx_path=p),
    )
    artifact = result["checked_artifacts"][0]
    assert artifact["required"] is True
    assert artifact["unresolved"] is True
    assert "equation_style_findings:2" in artifact["unresolved_reasons"]
    assert artifact["ready"] is False

    assert result["unresolved_required_count"] == 1
    assert result["executable"] is False
    assert any("docx_integrity_unresolved" in r for r in result["executable_reasons"])


@pytest.mark.asyncio
async def test_gate_blocks_on_render_failure(db, tmp_path):
    project = await db_module.create_project(db, "docx-gate-blocks-render")
    docx_path = tmp_path / "corrupt.docx"
    docx_path.write_bytes(b"stub")

    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Ship the corrupted export",
        artifact_policy={"artifact_pointer_check": "strict"},
    )
    await db_module.add_sprint_item_pointer(
        db, project["id"], item["id"], "docs",
        [{"uri": str(docx_path), "selector": {"type": "text_quote", "exact": "x"}}],
    )

    result = await gate_module.build_docx_integrity_gate(
        db, project["id"],
        render_checker=lambda p: {"status": "failed", "reason": "corrupt zip"},
        equation_auditor=lambda p: _equation_ok(docx_path=p),
    )
    artifact = result["checked_artifacts"][0]
    assert artifact["unresolved"] is True
    assert any(r.startswith("render_failed:") for r in artifact["unresolved_reasons"])
    assert result["executable"] is False


@pytest.mark.asyncio
async def test_gate_warn_policy_surfaces_but_never_blocks(db, tmp_path):
    """Same findings as the blocking test above, but the item's policy is
    the default ('warn') — the finding must still be surfaced (visible in
    checked_artifacts) but must NEVER flip executable to False. Mirrors
    evaluate_artifact_pointer_policy's own warn/strict distinction exactly."""
    project = await db_module.create_project(db, "docx-gate-warn-not-blocking")
    docx_path = tmp_path / "warn.docx"
    docx_path.write_bytes(b"stub")

    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "A document with no strict policy",
    )
    await db_module.add_sprint_item_pointer(
        db, project["id"], item["id"], "docs",
        [{"uri": str(docx_path), "selector": {"type": "text_quote", "exact": "x"}}],
    )

    result = await gate_module.build_docx_integrity_gate(
        db, project["id"],
        render_checker=lambda p: _rendered(),
        equation_auditor=lambda p: _equation_findings(1, docx_path=p),
    )
    artifact = result["checked_artifacts"][0]
    assert artifact["required"] is False
    assert artifact["unresolved"] is True  # the finding is still visible
    assert artifact["ready"] is True       # but never blocking under "warn"
    assert result["unresolved_required_count"] == 0
    assert result["executable"] is True


@pytest.mark.asyncio
async def test_gate_unavailable_render_status_never_blocks(db, tmp_path):
    """render_gate's own 'unavailable-with-reason' means 'we could not
    check', never 'we confirmed a problem' — must never set unresolved."""
    project = await db_module.create_project(db, "docx-gate-unavailable-status")
    docx_path = tmp_path / "cant-check.docx"
    docx_path.write_bytes(b"stub")

    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Report with no render backend",
        artifact_policy={"artifact_pointer_check": "strict"},
    )
    await db_module.add_sprint_item_pointer(
        db, project["id"], item["id"], "docs",
        [{"uri": str(docx_path), "selector": {"type": "text_quote", "exact": "x"}}],
    )

    result = await gate_module.build_docx_integrity_gate(
        db, project["id"],
        render_checker=lambda p: {"status": "unavailable-with-reason", "reason": "no backend"},
        equation_auditor=lambda p: _equation_ok(docx_path=p),
    )
    artifact = result["checked_artifacts"][0]
    assert artifact["render_status"] == "unavailable-with-reason"
    assert artifact["unresolved"] is False
    assert artifact["ready"] is True
    assert result["executable"] is True


# ---------------------------------------------------------------------------
# Proposal-evidence tie-in (6cdc5df3).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_discovers_proposal_evidence_artifact(db, tmp_path):
    project = await db_module.create_project(db, "docx-gate-proposal-artifact")
    docx_path = tmp_path / "proposal-output.docx"
    docx_path.write_bytes(b"stub")

    await db_module.link_proposal_evidence(
        db, project["id"], "prop-docx-1", "artifact", str(docx_path),
        label="Promoted DOCX output",
    )
    proposal_evidence = await handoff_module.build_proposal_evidence_for_handoff(
        db, project["id"],
    )
    assert proposal_evidence and proposal_evidence[0]["artifacts"]

    result = await gate_module.build_docx_integrity_gate(
        db, project["id"],
        proposal_evidence=proposal_evidence,
        render_checker=lambda p: _rendered(),
        equation_auditor=lambda p: _equation_findings(3, docx_path=p),
    )
    assert result["candidate_count"] == 1
    artifact = result["checked_artifacts"][0]
    assert artifact["source"] == "proposal_evidence_artifact"
    assert artifact["proposal_id"] == "prop-docx-1"
    assert artifact["label"] == "Promoted DOCX output"
    # No linked sprint_item declares a strict policy -> not required, so the
    # finding is surfaced but does not block.
    assert artifact["required"] is False
    assert artifact["unresolved"] is True
    assert result["executable"] is True


@pytest.mark.asyncio
async def test_gate_proposal_artifact_inherits_strict_from_sibling_sprint_item(db, tmp_path):
    """A proposal's .docx artifact evidence with no policy of its own
    inherits 'required' from a sibling sprint_item evidence link under the
    SAME proposal that declares a strict artifact policy."""
    project = await db_module.create_project(db, "docx-gate-proposal-inherits-strict")
    docx_path = tmp_path / "strict-output.docx"
    docx_path.write_bytes(b"stub")

    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Strict proposal item",
        artifact_policy={"artifact_pointer_check": "strict"},
    )
    await db_module.link_proposal_evidence(
        db, project["id"], "prop-docx-2", "sprint_item", item["id"],
    )
    await db_module.link_proposal_evidence(
        db, project["id"], "prop-docx-2", "artifact", str(docx_path),
    )
    proposal_evidence = await handoff_module.build_proposal_evidence_for_handoff(
        db, project["id"],
    )

    result = await gate_module.build_docx_integrity_gate(
        db, project["id"],
        proposal_evidence=proposal_evidence,
        render_checker=lambda p: _rendered(),
        equation_auditor=lambda p: _equation_findings(1, docx_path=p),
    )
    artifact = result["checked_artifacts"][0]
    assert artifact["required"] is True
    assert artifact["ready"] is False
    assert result["executable"] is False


@pytest.mark.asyncio
async def test_gate_merges_item_and_proposal_candidates_for_same_path(db, tmp_path):
    """The SAME .docx path discovered via both a sprint-item pointer AND a
    proposal-evidence artifact link must be evaluated exactly once."""
    project = await db_module.create_project(db, "docx-gate-dedup")
    docx_path = tmp_path / "shared.docx"
    docx_path.write_bytes(b"stub")

    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Shared artifact item",
        artifact_policy={"artifact_pointer_check": "strict"},
    )
    await db_module.add_sprint_item_pointer(
        db, project["id"], item["id"], "docs",
        [{"uri": str(docx_path), "selector": {"type": "text_quote", "exact": "x"}}],
    )
    await db_module.link_proposal_evidence(
        db, project["id"], "prop-docx-3", "artifact", str(docx_path),
    )
    proposal_evidence = await handoff_module.build_proposal_evidence_for_handoff(
        db, project["id"],
    )

    calls = []

    def _render(p):
        calls.append(p)
        return _rendered()

    result = await gate_module.build_docx_integrity_gate(
        db, project["id"],
        proposal_evidence=proposal_evidence,
        render_checker=_render,
        equation_auditor=lambda p: _equation_ok(docx_path=p),
    )
    assert result["candidate_count"] == 1
    assert len(calls) == 1
    artifact = result["checked_artifacts"][0]
    assert artifact["item_id"] == item["id"]
    assert artifact["proposal_id"] == "prop-docx-3"


# ---------------------------------------------------------------------------
# Bounds — never unbounded (23e20656 lesson).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_caps_checked_artifacts(db, tmp_path):
    project = await db_module.create_project(db, "docx-gate-cap")
    for i in range(5):
        p = tmp_path / f"doc{i}.docx"
        p.write_bytes(b"stub")
        await db_module.link_proposal_evidence(
            db, project["id"], f"prop-cap-{i}", "artifact", str(p),
        )
    proposal_evidence = await handoff_module.build_proposal_evidence_for_handoff(
        db, project["id"], limit=10,
    )
    result = await gate_module.build_docx_integrity_gate(
        db, project["id"],
        proposal_evidence=proposal_evidence,
        render_checker=lambda p: _rendered(),
        max_checked_artifacts=2,
    )
    assert result["candidate_count"] == 5
    assert len(result["checked_artifacts"]) == 2
    assert result["skipped_candidates"] == 3


# ---------------------------------------------------------------------------
# Guarded — never raises.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_checker_exception_never_propagates(db, tmp_path):
    project = await db_module.create_project(db, "docx-gate-checker-raises")
    docx_path = tmp_path / "boom.docx"
    docx_path.write_bytes(b"stub")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Boom item",
        artifact_policy={"artifact_pointer_check": "strict"},
    )
    await db_module.add_sprint_item_pointer(
        db, project["id"], item["id"], "docs",
        [{"uri": str(docx_path), "selector": {"type": "text_quote", "exact": "x"}}],
    )

    def _boom(path):
        raise RuntimeError("simulated checker crash")

    result = await gate_module.build_docx_integrity_gate(
        db, project["id"], render_checker=_boom, equation_auditor=_boom,
    )
    artifact = result["checked_artifacts"][0]
    # A checker that raises is "could not confirm" -- never a fabricated
    # finding, never a block.
    assert artifact["render_status"] is None
    assert artifact["unresolved"] is False
    assert artifact["ready"] is True
    assert result["executable"] is True


# ---------------------------------------------------------------------------
# meridian.handoff.build_docx_integrity_gate_for_handoff — wrapper.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wrapper_never_raises_on_lookup_failure(db, monkeypatch):
    project = await db_module.create_project(db, "docx-gate-wrapper-guarded")

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(gate_module, "build_docx_integrity_gate", _boom)
    result = await handoff_module.build_docx_integrity_gate_for_handoff(db, project["id"])
    assert result is None


@pytest.mark.asyncio
async def test_wrapper_returns_gate_dict(db):
    project = await db_module.create_project(db, "docx-gate-wrapper-ok")
    result = await handoff_module.build_docx_integrity_gate_for_handoff(db, project["id"])
    assert result is not None
    assert result["schema_version"] == gate_module.GATE_SCHEMA_VERSION
    assert result["executable"] is True


# ---------------------------------------------------------------------------
# MCP tool surface — generate_handoff includes docx_integrity.
# ---------------------------------------------------------------------------

def _mcp_call(client, name, arguments):
    r = client.post("/mcp/sse", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert r.status_code == 200
    return r.json()


def _result(resp):
    assert resp.get("result") is not None, resp
    return _json.loads(resp["result"]["content"][0]["text"])


@pytest.mark.parametrize("mode", ["full", "goal"])
def test_mcp_generate_handoff_includes_docx_integrity_field(client, mode):
    pid = client.post("/projects", json={"name": f"mcp-docx-integrity-{mode}"}).json()["id"]
    result = _result(_mcp_call(client, "generate_handoff", {
        "project_id": pid, "mode": mode,
    }))
    assert "docx_integrity" in result
    gate = result["docx_integrity"]
    assert gate is not None
    assert gate["executable"] is True
    assert gate["checked_artifacts"] == []


# ---------------------------------------------------------------------------
# HTTP surface — docx_integrity present in both endpoints.
# ---------------------------------------------------------------------------

def test_http_handoff_endpoint_includes_docx_integrity_field(client):
    pid = client.post("/projects", json={"name": "http-docx-integrity"}).json()["id"]
    r = client.post(f"/projects/{pid}/handoff")
    assert r.status_code == 200
    body = r.json()
    assert "docx_integrity" in body
    assert body["docx_integrity"]["executable"] is True


def test_http_planner_handoff_includes_docx_integrity_field(client):
    pid = client.post("/projects", json={"name": "http-docx-integrity-planner"}).json()["id"]
    r = client.get(f"/projects/{pid}/handoff/planner")
    assert r.status_code == 200
    body = r.json()
    assert "docx_integrity" in body


# ---------------------------------------------------------------------------
# End-to-end acceptance scenario, via MCP tools only: a proposal's linked
# .docx artifact with an unresolved REQUIRED finding is surfaced AND blocks
# the handoff's readiness signal — the concrete d09c29fe acceptance case.
# ---------------------------------------------------------------------------

def test_mcp_end_to_end_docx_finding_gates_handoff(client, tmp_path, monkeypatch):
    pid = client.post("/projects", json={"name": "mcp-docx-e2e-gate"}).json()["id"]

    item_resp = _result(_mcp_call(client, "add_sprint_item", {
        "project_id": pid, "version": "v1", "title": "Ship the strict thesis chapter",
        "policy": {"artifact_pointer_check": "strict"},
    }))
    item_id = item_resp["id"]

    docx_path = tmp_path / "e2e.docx"
    docx_path.write_bytes(b"stub")
    _result(_mcp_call(client, "add_sprint_item_pointer", {
        "project_id": pid, "sprint_item_id": item_id, "source_type": "docs",
        "targets": [{"uri": str(docx_path), "selector": {"type": "text_quote", "exact": "x"}}],
    }))

    # Inject stub checkers at the default-resolution layer so this test
    # never needs a real LibreOffice/Word install (the extension is not
    # importable in this pixi env — see the module docstring).
    monkeypatch.setattr(
        gate_module, "default_render_checker", lambda: (lambda p: _rendered()),
    )
    monkeypatch.setattr(
        gate_module, "default_equation_auditor",
        lambda: (lambda p: _equation_findings(4, docx_path=p)),
    )

    result = _result(_mcp_call(client, "generate_handoff", {
        "project_id": pid, "mode": "full",
    }))
    gate = result["docx_integrity"]
    assert gate is not None
    assert gate["executable"] is False
    assert gate["unresolved_required_count"] == 1
    matching = [a for a in gate["checked_artifacts"] if a["item_id"] == item_id]
    assert len(matching) == 1
    assert matching[0]["ready"] is False
    assert matching[0]["equation_audit"]["finding_count"] == 4
