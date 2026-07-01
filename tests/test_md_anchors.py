"""Tests for the XML-anchor markdown auto-update feature (v3.3).

Covers: anchor parsing, atomic/guarded writes, the HITL kind/payload migration,
the single answer-chokepoint that applies an approved section replacement, the
category content-guard, and the pathspec-scoped checkpoint git commit.

Isolation invariants enforced here:
* every write targets ``MERIDIAN_MD_ROOT`` (a tmp dir), never the real repo docs;
* git is always mocked — no real ``git`` process is ever spawned.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian import md_anchors
from meridian import git_md
from meridian import db as db_module
from meridian import server as server_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wrap(name: str, body: str = "") -> str:
    return (
        f"{md_anchors.start_marker(name)}\n{body}"
        f"{'' if body.endswith(chr(10)) or not body else chr(10)}"
        f"{md_anchors.end_marker(name)}\n"
    )


def _write_anchor_file(path: Path, name: str, body: str) -> None:
    path.write_text(f"# Doc\n\nintro\n\n{_wrap(name, body)}", encoding="utf-8")


# ---------------------------------------------------------------------------
# Pure anchor parsing
# ---------------------------------------------------------------------------

def test_find_anchor_present_absent():
    text = f"a\n{md_anchors.start_marker('x')}\nbody\n{md_anchors.end_marker('x')}\n"
    span = md_anchors.find_anchor(text, "x")
    assert span is not None
    assert text[span[0]:span[1]] == "body\n"
    assert md_anchors.find_anchor(text, "nope") is None


def test_find_anchor_duplicate_raises():
    text = (
        f"{md_anchors.start_marker('x')}\n1\n{md_anchors.end_marker('x')}\n"
        f"{md_anchors.start_marker('x')}\n2\n{md_anchors.end_marker('x')}\n"
    )
    with pytest.raises(md_anchors.AnchorAmbiguous):
        md_anchors.find_anchor(text, "x")


def test_find_anchor_malformed_raises():
    text = f"{md_anchors.start_marker('x')}\nbody\n"  # START, no END
    with pytest.raises(md_anchors.AnchorError):
        md_anchors.find_anchor(text, "x")


def test_append_to_anchor_inserts_before_end_and_is_idempotent():
    text = f"{md_anchors.start_marker('log')}\n{md_anchors.end_marker('log')}\n"
    once = md_anchors.append_to_anchor(text, "log", "- line one")
    assert "- line one" in once
    assert once.index("- line one") < once.index(md_anchors.end_marker("log"))
    # Re-appending the identical line is a no-op.
    twice = md_anchors.append_to_anchor(once, "log", "- line one")
    assert twice == once
    # A different line appends below the first.
    three = md_anchors.append_to_anchor(once, "log", "- line two")
    assert three.index("- line one") < three.index("- line two") < three.index(md_anchors.end_marker("log"))


def test_replace_anchor_replaces_and_missing_raises():
    text = f"pre\n{md_anchors.start_marker('b')}\nold\n{md_anchors.end_marker('b')}\npost\n"
    out = md_anchors.replace_anchor(text, "b", "brand new")
    assert "brand new" in out and "old" not in out
    assert "pre" in out and "post" in out
    assert out.count(md_anchors.end_marker("b")) == 1
    with pytest.raises(md_anchors.AnchorMissing):
        md_anchors.replace_anchor("no anchor here", "b", "x")


# ---------------------------------------------------------------------------
# Policy: content guard + replace-target registry
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cat,ok", [
    ("TECHNICAL", True), ("ARCHITECTURAL", True), ("PRODUCT", True),
    ("technical", True), ("STRATEGIC", False), ("COMPETITIVE", False),
    ("BUSINESS", False), ("", False), (None, False),
])
def test_is_committable_category(cat, ok):
    assert md_anchors.is_committable_category(cat) is ok


def test_assert_replace_target():
    md_anchors.assert_replace_target("CLAUDE.md", "claude-body")   # ok
    md_anchors.assert_replace_target("AGENTS.md", "agents-body")   # ok
    with pytest.raises(ValueError):  # append-only anchor
        md_anchors.assert_replace_target("DECISIONS.md", "decisions-log")
    with pytest.raises(ValueError):  # README never writable
        md_anchors.assert_replace_target("README.md", "anything")
    with pytest.raises(ValueError):  # unknown anchor
        md_anchors.assert_replace_target("CLAUDE.md", "nope")


# ---------------------------------------------------------------------------
# Guarded / atomic apply ops
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apply_append_creates_anchor_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    md_anchors.drain_touched()
    path = await md_anchors.apply_append("DEVLOG.md", "devlog", "- first entry")
    assert path is not None and path.exists()
    text = path.read_text(encoding="utf-8")
    assert md_anchors.start_marker("devlog") in text
    assert "- first entry" in text
    assert path.resolve() in md_anchors.drain_touched()
    # idempotent: same line again writes nothing
    again = await md_anchors.apply_append("DEVLOG.md", "devlog", "- first entry")
    assert again is None


@pytest.mark.asyncio
async def test_apply_append_skips_noncommittable_category(tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    res = await md_anchors.apply_append(
        "DECISIONS.md", "decisions-log", "- secret strategy", category="STRATEGIC"
    )
    assert res is None
    assert not (tmp_path / "DECISIONS.md").exists()


@pytest.mark.asyncio
async def test_apply_append_rejects_replace_anchor(tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    with pytest.raises(ValueError):
        await md_anchors.apply_append("CLAUDE.md", "claude-body", "- nope")


@pytest.mark.asyncio
async def test_apply_replace_replaces_and_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    _write_anchor_file(tmp_path / "AGENTS.md", "agents-body", "old body\n")
    path = await md_anchors.apply_replace("AGENTS.md", "agents-body", "new body")
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "new body" in text and "old body" not in text
    assert "intro" in text  # human prose outside the anchor preserved
    # no-op when unchanged
    assert await md_anchors.apply_replace("AGENTS.md", "agents-body", "new body") is None
    # missing file -> AnchorMissing
    with pytest.raises(md_anchors.AnchorMissing):
        await md_anchors.apply_replace("CLAUDE.md", "claude-body", "x")


@pytest.mark.asyncio
async def test_hosted_mode_skips_all_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")
    _write_anchor_file(tmp_path / "AGENTS.md", "agents-body", "old\n")
    assert await md_anchors.apply_append("DEVLOG.md", "devlog", "- x") is None
    assert await md_anchors.apply_replace("AGENTS.md", "agents-body", "new") is None
    assert not (tmp_path / "DEVLOG.md").exists()
    assert "old" in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")


def test_atomic_write_leaves_no_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    md_anchors.drain_touched()
    p = tmp_path / "X.md"
    assert md_anchors._atomic_write(p, "hello") is True
    assert p.read_text(encoding="utf-8") == "hello"
    assert not (tmp_path / "X.md.tmp").exists()
    md_anchors.drain_touched()


def test_build_diff_nonempty(tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    _write_anchor_file(tmp_path / "AGENTS.md", "agents-body", "line a\n")
    diff = md_anchors.build_diff("AGENTS.md", "agents-body", "line b")
    assert "-line a" in diff and "+line b" in diff


# ---------------------------------------------------------------------------
# DB migration: hitl_requests.kind + payload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hitl_kind_payload_roundtrip(anydb):
    db = anydb
    p = await db_module.create_project(db, "kp")
    payload = json.dumps({"file": "AGENTS.md", "anchor": "agents-body"})
    h = await db_module.request_hitl(
        db, p["id"], "Approve?", kind="md_section_update", payload=payload
    )
    got = await db_module.get_hitl_request(db, h["id"])
    assert got["kind"] == "md_section_update"
    assert got["payload"] == payload
    # default kind stays 'question' for normal requests
    h2 = await db_module.request_hitl(db, p["id"], "normal?")
    got2 = await db_module.get_hitl_request(db, h2["id"])
    assert got2["kind"] == "question"
    assert got2["payload"] is None


# ---------------------------------------------------------------------------
# HITL answer chokepoint applies the section replacement (once, both paths)
# ---------------------------------------------------------------------------

async def _make_md_hitl(db, tmp_path, *, file="AGENTS.md", anchor="agents-body",
                        content="NEW BODY", base_hash="__current__"):
    p = await db_module.create_project(db, "md-choke")
    if base_hash == "__current__":
        base_hash = md_anchors.anchor_content_hash(file, anchor)
    payload = json.dumps({
        "file": file, "anchor": anchor, "content": content,
        "base_hash": base_hash, "diff": "irrelevant",
    })
    h = await db_module.request_hitl(
        db, p["id"], f"Approve {file}?", kind="md_section_update", payload=payload
    )
    return h


@pytest.mark.asyncio
async def test_answer_applies_md_update(db, tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    _write_anchor_file(tmp_path / "AGENTS.md", "agents-body", "old body\n")
    h = await _make_md_hitl(db, tmp_path)
    res = await server_module._answer_hitl_and_apply(db, h["id"], "approved")
    assert res["applied"] is True
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "NEW BODY" in text and "old body" not in text


@pytest.mark.asyncio
async def test_reject_does_not_write(db, tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    _write_anchor_file(tmp_path / "AGENTS.md", "agents-body", "keep me\n")
    h = await _make_md_hitl(db, tmp_path)
    # dismiss path -> approved=False
    row = await db_module.dismiss_hitl_request(db, h["id"])
    extra = await server_module._on_hitl_answered(db, row, approved=False)
    assert extra["applied"] is False
    assert "keep me" in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_stale_base_hash_refused(db, tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    _write_anchor_file(tmp_path / "AGENTS.md", "agents-body", "original\n")
    h = await _make_md_hitl(db, tmp_path, base_hash="deadbeefdeadbeef")
    res = await server_module._answer_hitl_and_apply(db, h["id"], "approved")
    assert res["applied"] is False
    assert "changed since proposal" in res["apply_error"]
    assert "original" in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_legacy_question_hitl_unaffected(db, tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    p = await db_module.create_project(db, "legacy")
    h = await db_module.request_hitl(db, p["id"], "normal question?")
    res = await server_module._answer_hitl_and_apply(db, h["id"], "yes")
    assert res["status"] == "answered" and res["answer"] == "yes"
    assert "applied" not in res  # no md side-effect for plain questions


# ---------------------------------------------------------------------------
# update_md_section MCP tool (via the shared dispatcher)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_md_section_creates_hitl_then_applies(db, tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    _write_anchor_file(tmp_path / "AGENTS.md", "agents-body", "before\n")
    p = await db_module.create_project(db, "tool")
    created = await server_module._dispatch_mcp_tool(
        "update_md_section",
        {"project_id": p["id"], "file": "AGENTS.md", "anchor": "agents-body",
         "content": "AFTER CONTENT"},
        db, str(tmp_path),
    )
    assert created["kind"] == "md_section_update"
    assert created["status"] == "pending"
    payload = json.loads(created["payload"])
    assert payload["file"] == "AGENTS.md" and payload["anchor"] == "agents-body"
    assert "AFTER CONTENT" in payload["content"]
    assert payload["diff"]  # diff preview present
    # File not touched until approved.
    assert "before" in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    # Approve through the same dispatcher (answer_hitl).
    applied = await server_module._dispatch_mcp_tool(
        "answer_hitl", {"request_id": created["id"], "answer": "approved"},
        db, str(tmp_path),
    )
    assert applied["applied"] is True
    assert "AFTER CONTENT" in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_update_md_section_force_skips_hitl(db, tmp_path, monkeypatch):
    """v1.1 — force=true applies the section replacement directly (no HITL),
    while the default path still files a HITL request."""
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    _write_anchor_file(tmp_path / "AGENTS.md", "agents-body", "before\n")
    p = await db_module.create_project(db, "force-tool")
    result = await server_module._dispatch_mcp_tool(
        "update_md_section",
        {"project_id": p["id"], "file": "AGENTS.md", "anchor": "agents-body",
         "content": "FORCED CONTENT", "force": True},
        db, str(tmp_path),
    )
    # Applied directly — no HITL kind/status in the response.
    assert result["applied"] is True
    assert result["forced"] is True
    assert "kind" not in result
    # File written immediately, without an approval step.
    assert "FORCED CONTENT" in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    # No HITL request was created.
    assert await db_module.list_hitl_requests(db, p["id"], status="pending") == []


@pytest.mark.asyncio
async def test_update_md_section_rejects_append_anchor(db, tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    p = await db_module.create_project(db, "tool2")
    with pytest.raises(ValueError):
        await server_module._dispatch_mcp_tool(
            "update_md_section",
            {"project_id": p["id"], "file": "DECISIONS.md",
             "anchor": "decisions-log", "content": "x"},
            db, str(tmp_path),
        )


def test_update_md_section_tool_registered():
    from meridian.mcp_tools import _MCP_TOOLS_LIST
    tool = next((t for t in _MCP_TOOLS_LIST if t["name"] == "update_md_section"), None)
    assert tool is not None
    props = tool["inputSchema"]["properties"]
    assert {"project_id", "file", "anchor", "content"} <= set(props)


# ---------------------------------------------------------------------------
# Content guard end-to-end via pin_decision / add_note
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pin_decision_appends_committable_only(db, tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    p = await db_module.create_project(db, "guard")
    await server_module._dispatch_mcp_tool(
        "pin_decision",
        {"project_id": p["id"], "title": "Use psycopg3", "body": "stable on win",
         "category": "TECHNICAL"},
        db, str(tmp_path),
    )
    decisions = tmp_path / "DECISIONS.md"
    assert decisions.exists()
    assert "Use psycopg3" in decisions.read_text(encoding="utf-8")

    # A STRATEGIC decision is persisted in the DB but never written to the doc.
    await server_module._dispatch_mcp_tool(
        "pin_decision",
        {"project_id": p["id"], "title": "Raise prices 3x", "body": "secret",
         "category": "STRATEGIC"},
        db, str(tmp_path),
    )
    assert "Raise prices" not in decisions.read_text(encoding="utf-8")
    pinned = await db_module.get_pinned_decisions(db, p["id"])
    assert any(d["title"] == "Raise prices 3x" for d in pinned)


@pytest.mark.asyncio
async def test_add_note_roadmap_requires_committable_category(db, tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    p = await db_module.create_project(db, "notes")
    roadmap = tmp_path / "ROADMAP.md"
    # roadmap tag but no category -> fail-closed, nothing written
    await server_module._dispatch_mcp_tool(
        "add_note",
        {"project_id": p["id"], "title": "Idea A", "body": "b", "tags": "roadmap"},
        db, str(tmp_path),
    )
    assert not roadmap.exists()
    # roadmap tag + committable category -> appended
    await server_module._dispatch_mcp_tool(
        "add_note",
        {"project_id": p["id"], "title": "Idea B", "body": "b", "tags": "roadmap",
         "category": "PRODUCT"},
        db, str(tmp_path),
    )
    assert roadmap.exists() and "Idea B" in roadmap.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Checkpoint git commit — pathspec-scoped, git fully mocked
# ---------------------------------------------------------------------------

class _GitRecorder:
    """Async stand-in for git_md._git that records argv and returns canned codes."""

    def __init__(self, diff_rc: int = 1):
        self.calls: list[list[str]] = []
        self.diff_rc = diff_rc

    async def __call__(self, *argv, cwd, timeout: float = 10.0):
        self.calls.append(list(argv))
        head = argv[0] if argv else ""
        if head == "rev-parse":
            return (0, "true\n", "")
        if head == "diff":
            return (self.diff_rc, "", "")
        return (0, "", "")


@pytest.mark.asyncio
async def test_commit_uses_pathspec_never_add_all(tmp_path, monkeypatch):
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    rec = _GitRecorder(diff_rc=1)  # 1 => something staged
    monkeypatch.setattr(git_md, "_git", rec)
    target = tmp_path / "DEVLOG.md"
    target.write_text("x", encoding="utf-8")
    out = await git_md.commit_touched_md([target], "docs: test", cwd=tmp_path)
    assert out["committed"] is True
    flat = [tok for call in rec.calls for tok in call]
    assert "-A" not in flat and "." not in flat       # never blanket-stage
    assert ["add", "--", str(target)] in rec.calls    # explicit pathspec add
    assert any(c[0] == "commit" and "--" in c for c in rec.calls)


@pytest.mark.asyncio
async def test_commit_skips_when_nothing_staged(tmp_path, monkeypatch):
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    rec = _GitRecorder(diff_rc=0)  # 0 => nothing staged
    monkeypatch.setattr(git_md, "_git", rec)
    target = tmp_path / "DEVLOG.md"
    target.write_text("x", encoding="utf-8")
    out = await git_md.commit_touched_md([target], "docs: test", cwd=tmp_path)
    assert out["committed"] is False and out["reason"] == "nothing-staged"
    assert not any(c[0] == "commit" for c in rec.calls)


@pytest.mark.asyncio
async def test_commit_hosted_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")
    rec = _GitRecorder()
    monkeypatch.setattr(git_md, "_git", rec)
    out = await git_md.commit_touched_md([tmp_path / "DEVLOG.md"], "m", cwd=tmp_path)
    assert out["committed"] is False and out["reason"] == "hosted"
    assert rec.calls == []  # git never invoked


@pytest.mark.asyncio
async def test_finalize_session_appends_devlog_and_commits(db, tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    rec = _GitRecorder(diff_rc=1)
    monkeypatch.setattr(git_md, "_git", rec)
    md_anchors.drain_touched()
    p = await db_module.create_project(db, "fin")
    s = await db_module.register_session(db, p["id"], "sess-final")
    # two done tasks so the session has something to summarize
    await db_module.log_task(db, s["id"], p["id"], "Implemented anchors", status="done")
    await db_module.log_task(db, s["id"], p["id"], "Wired checkpoint commit", status="done")
    await server_module._finalize_session_md(db, p["id"], s["id"])
    devlog = (tmp_path / "DEVLOG.md").read_text(encoding="utf-8")
    assert "sess-final" in devlog
    assert any(c[0] == "commit" for c in rec.calls)  # touched DEVLOG was committed


# ---------------------------------------------------------------------------
# Dashboard wiring regression (cheap, via the static asset)
# ---------------------------------------------------------------------------

def test_dashboard_js_has_md_section_update_ui(client):
    js = client.get("/static/dashboard.ts").text
    assert "md_section_update" in js
    assert "hitl-approve-btn" in js
    assert "renderDiff" in js
