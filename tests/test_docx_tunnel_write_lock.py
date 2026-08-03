"""73d233e4 — concurrent-write protection for the word/office (docx) tunnel path.

The `word` slot (docx-mcp) is a pure network relay: a tunneled .docx write has no
partial write (a .docx is a zip container — every mutating tool re-saves the whole
file), so two sessions editing different sections of the same document silently
overwrite each other (last-save-wins). These tests lock in the fix: before relaying
a MUTATING word tool, `call_tunnel_tool` consults the target document's live file
claims (via the existing claim_file / get_file_claims + evaluate_claim_guard
machinery) and REFUSES a write that conflicts with another live session's claim,
while leaving reads, unclaimed writes, and own-session writes untouched.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian.routes import tunnel as tun


# ---------------------------------------------------------------------------
# Pure target-detection helper — no db, no tunnel.
# ---------------------------------------------------------------------------

def test_word_write_target_detects_mutating_tools():
    # A known writer with a recognized document-arg key resolves the target.
    assert tun._word_write_target("add_paragraph", {"filename": "thesis.docx"}) == "thesis.docx"
    assert tun._word_write_target("create_document", {"file_path": "/docs/a.docx"}) == "/docs/a.docx"
    # Connector-prefixed name is accepted (prefix stripped before the lookup).
    assert tun._word_write_target("word__add_heading", {"path": "b.docx"}) == "b.docx"
    # search_and_replace is an observed mutating tool in the live Word contract.
    assert tun._word_write_target(
        "search_and_replace", {"file_path": "replacements.docx"}
    ) == "replacements.docx"


def test_word_write_target_none_for_reads_and_unknowns():
    # Read-only / non-mutating tools are not writers → no target.
    assert tun._word_write_target("get_text", {"filename": "thesis.docx"}) is None
    assert tun._word_write_target("document_outline", {"filename": "thesis.docx"}) is None
    # A writer with no identifiable document arg fails open (can't guard it).
    assert tun._word_write_target("add_paragraph", {"text": "hi"}) is None
    assert tun._word_write_target("add_paragraph", None) is None


# ---------------------------------------------------------------------------
# check_word_write_conflict against real file claims.
# ---------------------------------------------------------------------------

async def _mk_session(db, name: str) -> str:
    proj = await db_module.create_project(db, name=f"proj-{name}")
    sess = await db_module.register_session(db, project_id=proj["id"], name=name)
    return sess["id"]


async def test_conflict_when_other_session_holds_write_claim(db):
    """Another live session's write claim on the target doc blocks the relay."""
    owner = await _mk_session(db, "owner")
    doc = "C:/Users/13144/Documents/thesis.docx"
    claimed = await db_module.claim_file(db, doc, owner, mode="write")
    assert claimed["claimed"] is True

    # A DIFFERENT session (or an unknown/claim-less caller) tries to write.
    verdict = await tun.check_word_write_conflict(
        db, "tenant-1", "add_paragraph", {"filename": doc}, session_id="intruder"
    )
    assert verdict is not None
    assert verdict["blocked"] is True
    assert verdict["document"] == db_module._normalize_file_path(doc)
    assert verdict["holder"] == owner
    assert "last-save-wins" in verdict["message"]


async def test_no_conflict_for_own_session(db):
    """The session that owns the claim may keep writing its own document."""
    owner = await _mk_session(db, "owner")
    doc = "report.docx"
    await db_module.claim_file(db, doc, owner, mode="write")

    verdict = await tun.check_word_write_conflict(
        db, "tenant-1", "add_paragraph", {"filename": doc}, session_id=owner
    )
    assert verdict is None


async def test_no_conflict_when_unclaimed(db):
    """A write to a document nobody has claimed passes through (no false block)."""
    verdict = await tun.check_word_write_conflict(
        db, "tenant-1", "add_paragraph", {"filename": "fresh.docx"}, session_id="s1"
    )
    assert verdict is None


async def test_read_tool_never_blocked(db):
    """A read-only word tool is never blocked, even against a live write claim."""
    owner = await _mk_session(db, "owner")
    doc = "locked.docx"
    await db_module.claim_file(db, doc, owner, mode="write")

    verdict = await tun.check_word_write_conflict(
        db, "tenant-1", "get_text", {"filename": doc}, session_id="reader"
    )
    assert verdict is None


async def test_different_documents_do_not_contend(db):
    """A write claim on doc A does not block a write to doc B."""
    owner = await _mk_session(db, "owner")
    await db_module.claim_file(db, "a.docx", owner, mode="write")

    verdict = await tun.check_word_write_conflict(
        db, "tenant-1", "add_paragraph", {"filename": "b.docx"}, session_id="other"
    )
    assert verdict is None


async def test_fail_open_without_db(db):
    """No db handle → fail open (the guard never fabricates a block)."""
    verdict = await tun.check_word_write_conflict(
        None, "tenant-1", "add_paragraph", {"filename": "x.docx"}, session_id="s1"
    )
    assert verdict is None


# ---------------------------------------------------------------------------
# End-to-end: call_tunnel_tool raises on a conflicting word write, and only for
# the word slot / mutating tools.
# ---------------------------------------------------------------------------

async def test_call_tunnel_tool_raises_on_word_write_conflict(db, monkeypatch):
    """A conflicting word write surfaces as a RuntimeError (MCP error), and the
    relay is never reached."""
    owner = await _mk_session(db, "owner")
    doc = "shared.docx"
    await db_module.claim_file(db, doc, owner, mode="write")

    tenant = "tenant-77"
    # Route "word__add_paragraph" to the word slot and pretend a socket exists so
    # the relay path is reachable were the guard not to fire.
    monkeypatch.setitem(tun._tunnel_tool_routes, tenant, {"word__add_paragraph": "word"})
    monkeypatch.setitem(tun._tunnel_word_sockets, tenant, object())

    called = {"relayed": False}

    async def _fake_jsonrpc(*a, **k):
        called["relayed"] = True
        return {"result": {"content": []}}

    monkeypatch.setattr(tun, "_tunnel_jsonrpc", _fake_jsonrpc)

    with pytest.raises(RuntimeError, match="Concurrent-write conflict"):
        await tun.call_tunnel_tool(
            tenant, "word__add_paragraph", {"filename": doc},
            db=db, session_id="intruder",
        )
    # The guard fired BEFORE the network relay.
    assert called["relayed"] is False


async def test_call_tunnel_tool_relays_word_write_when_clear(db, monkeypatch):
    """An unclaimed / own-session word write is relayed normally."""
    tenant = "tenant-78"
    monkeypatch.setitem(tun._tunnel_tool_routes, tenant, {"word__add_paragraph": "word"})
    monkeypatch.setitem(tun._tunnel_word_sockets, tenant, object())

    async def _fake_jsonrpc(*a, **k):
        return {"result": {"content": [{"type": "text", "text": "ok"}]}}

    monkeypatch.setattr(tun, "_tunnel_jsonrpc", _fake_jsonrpc)

    result = await tun.call_tunnel_tool(
        tenant, "word__add_paragraph", {"filename": "brand-new.docx"},
        db=db, session_id="s1",
    )
    assert result == {"content": [{"type": "text", "text": "ok"}]}

async def test_search_and_replace_requires_positive_structured_count(monkeypatch):
    tenant = "tenant-search-replace"
    monkeypatch.setitem(
        tun._tunnel_tool_routes, tenant, {"word__search_and_replace": "word"}
    )
    monkeypatch.setitem(tun._tunnel_word_sockets, tenant, object())

    async def _fake_jsonrpc(*_args, **_kwargs):
        return {
            "result": {
                "structuredContent": {"result": "搜索替换完成: 替换了 2 处"},
                "content": [{"type": "text", "text": "搜索替换完成: 替换了 2 处"}],
            }
        }

    monkeypatch.setattr(tun, "_tunnel_jsonrpc", _fake_jsonrpc)
    result = await tun.call_tunnel_tool(
        tenant,
        "word__search_and_replace",
        {"search_text": "alpha", "replace_text": "omega"},
    )
    assert result["structuredContent"]["result"].endswith("替换了 2 处")


@pytest.mark.parametrize(
    "structured_result, expected",
    [
        ("搜索替换完成: 替换了 0 处", "positive replacement count"),
        ("replacement completed", "no structured replacement count"),
    ],
)
async def test_search_and_replace_rejects_ambiguous_result(
    monkeypatch, structured_result, expected
):
    tenant = "tenant-search-replace-invalid"
    monkeypatch.setitem(
        tun._tunnel_tool_routes, tenant, {"word__search_and_replace": "word"}
    )
    monkeypatch.setitem(tun._tunnel_word_sockets, tenant, object())

    async def _fake_jsonrpc(*_args, **_kwargs):
        return {"result": {"structuredContent": {"result": structured_result}}}

    monkeypatch.setattr(tun, "_tunnel_jsonrpc", _fake_jsonrpc)
    with pytest.raises(RuntimeError, match=expected):
        await tun.call_tunnel_tool(
            tenant,
            "word__search_and_replace",
            {"search_text": "missing", "replace_text": "omega"},
        )
