"""4e2bce48 — a legacy single-colon ``file:<path>:<symbol>`` resource must lock,
release, transfer and reset under the REAL file ``<path>`` on every path.

claim_sprint_item's gate (meridian.mcp.handler._sprint_item_resource_claim_gate)
used the raw suffix, so it locked the literal path "<path>:<symbol>" -- a key
nothing else checks -- and the real file stayed unprotected. The db-layer mirror
_claim_batch_resource had already been fixed (6b3b2c0e). The release paths had
the same raw-suffix bug, so fixing only the claim side would have leaked the
real-file lock for its whole TTL; they now share one helper.
"""
from __future__ import annotations

import meridian.server  # noqa: F401 — import first to avoid the handler/server import cycle
from meridian import db as db_module
from meridian.db import sprint_items as sprint_items_module
from meridian.mcp.handler import (
    _check_file_only_resources_warning,
    _code_notes_for_item_resources,
    _prospect_code_context,
    _sprint_item_resource_claim_gate,
)
from meridian.mcp_tools import _MCP_TOOLS_LIST
from meridian.sprint_evidence_guard import _declared_evidence_paths

SHORTHAND = "file:pkg/mod.py:helper"
REAL = "pkg/mod.py"
BOGUS = "pkg/mod.py:helper"


async def _item(db, name: str, resources: list[str]):
    project = await db_module.create_project(db, name)
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", name, touches_resources=resources, prospect_bypass=True,
    )
    return project["id"], item


async def _holder(db, path: str):
    return ((await db_module.get_file_claims(db, path)).get("file_lock") or {}).get("session_id")


# ---------------------------------------------------------------------------
# Claim side
# ---------------------------------------------------------------------------

async def test_gate_locks_the_real_file_not_the_literal_suffix(db):
    pid, item = await _item(db, "4e2bce48-gate", [SHORTHAND])
    sess = await db_module.register_session(db, pid, "w1")

    gate = await _sprint_item_resource_claim_gate(db, pid, item["id"], sess["id"])

    assert gate["ok"] is True
    entry = gate["lock_scope"][0]
    assert entry["file_path"] == REAL
    assert entry["claim_granularity"] == "file"
    assert entry["resolved_from_legacy_shorthand"] is True
    assert await _holder(db, REAL) == sess["id"]
    assert await _holder(db, BOGUS) is None


async def test_two_shorthand_symbols_on_one_file_conflict_like_the_scheduler_says(db):
    project = await db_module.create_project(db, "4e2bce48-conflict")
    pid = project["id"]
    a = await db_module.add_sprint_item(
        db, pid, "v1", "a", touches_resources=["file:pkg/mod.py:funcA"], prospect_bypass=True,
    )
    b = await db_module.add_sprint_item(
        db, pid, "v1", "b", touches_resources=["file:pkg/mod.py:funcB"], prospect_bypass=True,
    )
    s1 = await db_module.register_session(db, pid, "w1")
    s2 = await db_module.register_session(db, pid, "w2")

    assert (await _sprint_item_resource_claim_gate(db, pid, a["id"], s1["id"]))["ok"] is True
    blocked = await _sprint_item_resource_claim_gate(db, pid, b["id"], s2["id"])

    assert blocked["ok"] is False
    assert blocked["error"] == "RESOURCE_LOCKED"
    conflict = blocked["conflicts"][0]
    assert conflict["file_path"] == REAL
    assert conflict["resolved_from_legacy_shorthand"] is True
    # Claim-time enforcement now agrees with the scheduler's conflict model.
    assert db_module._two_resources_conflict("file:pkg/mod.py:funcA", "file:pkg/mod.py:funcB") is True


async def test_windows_drive_letter_path_is_not_mistaken_for_shorthand(db):
    pid, item = await _item(db, "4e2bce48-drive", ["file:C:/repo/x.py"])
    sess = await db_module.register_session(db, pid, "w1")

    entry = (await _sprint_item_resource_claim_gate(db, pid, item["id"], sess["id"]))["lock_scope"][0]

    assert entry["file_path"] == "C:/repo/x.py"
    assert "resolved_from_legacy_shorthand" not in entry


async def test_plain_file_resource_is_unchanged(db):
    pid, item = await _item(db, "4e2bce48-plain", ["file:pkg/plain.py"])
    sess = await db_module.register_session(db, pid, "w1")

    entry = (await _sprint_item_resource_claim_gate(db, pid, item["id"], sess["id"]))["lock_scope"][0]

    assert entry["file_path"] == "pkg/plain.py"
    assert "resolved_from_legacy_shorthand" not in entry
    assert await _holder(db, "pkg/plain.py") == sess["id"]


# ---------------------------------------------------------------------------
# Release side -- must mirror the claim side or the real-file lock leaks
# ---------------------------------------------------------------------------

async def test_release_claim_releases_the_real_file(db):
    pid, item = await _item(db, "4e2bce48-release", [SHORTHAND])
    owner = await db_module.register_session(db, pid, "owner")
    await db_module.claim_sprint_item(db, pid, item["id"], actor=owner["id"])
    assert (await _sprint_item_resource_claim_gate(db, pid, item["id"], owner["id"]))["ok"] is True
    assert await _holder(db, REAL) == owner["id"]

    result = await db_module.release_sprint_item_claim(db, pid, item["id"], owner["id"], reason="test")

    assert result["released_resources"] == [SHORTHAND]
    assert await _holder(db, REAL) is None


async def test_release_also_clears_a_lock_taken_before_the_fix(db):
    pid, item = await _item(db, "4e2bce48-prefix-lock", [SHORTHAND])
    owner = await db_module.register_session(db, pid, "owner")
    await db_module.claim_sprint_item(db, pid, item["id"], actor=owner["id"])
    # Exactly what the gate did before 4e2bce48.
    await db_module.claim_file(db, BOGUS, owner["id"])

    result = await db_module.release_sprint_item_claim(db, pid, item["id"], owner["id"], reason="test")

    assert result["released_resources"] == [SHORTHAND]
    assert await _holder(db, BOGUS) is None


async def test_stale_reset_releases_the_real_file(db):
    pid, item = await _item(db, "4e2bce48-stale", [SHORTHAND])
    owner = await db_module.register_session(db, pid, "owner")
    await db_module.claim_sprint_item(db, pid, item["id"], actor=owner["id"])
    assert (await _sprint_item_resource_claim_gate(db, pid, item["id"], owner["id"]))["ok"] is True
    assert await _holder(db, REAL) == owner["id"]

    reset = await sprint_items_module._reset_stale_claim(
        db, pid, item["id"], {"classification": "stale", "reasons": ["test"], "signals": {}},
    )

    assert reset is not None
    assert reset["released_resources"] == [SHORTHAND]
    assert await _holder(db, REAL) is None


async def test_transfer_moves_the_real_file_lock(db):
    pid, item = await _item(db, "4e2bce48-transfer", [SHORTHAND])
    from_sess = await db_module.register_session(db, pid, "from")
    to_sess = await db_module.register_session(db, pid, "to")
    await db_module.claim_sprint_item(db, pid, item["id"], actor=from_sess["id"])
    assert (await _sprint_item_resource_claim_gate(db, pid, item["id"], from_sess["id"]))["ok"] is True

    await db_module.transfer_sprint_item_claim(
        db, pid, item["id"], from_sess["id"], to_sess["id"],
        to_session_id=to_sess["id"], reason="handing off",
    )

    assert await _holder(db, REAL) == to_sess["id"]
    assert await _holder(db, BOGUS) is None


async def test_release_helper_reports_nothing_released_when_nothing_was_held(db):
    project = await db_module.create_project(db, "4e2bce48-helper-noop")
    sess = await db_module.register_session(db, project["id"], "w1")
    assert await sprint_items_module._release_file_resource(db, SHORTHAND, sess["id"]) is False


async def test_mcp_release_file_by_the_declared_string_releases_the_real_lock(db):
    """Executors commonly release by the string they declared. Claim time now
    locks the real file, so release_file must fall back to it or the lock leaks."""
    from meridian import server as srv

    pid, item = await _item(db, "4e2bce48-mcp-release", [SHORTHAND])
    sess = await db_module.register_session(db, pid, "w1")
    assert (await _sprint_item_resource_claim_gate(db, pid, item["id"], sess["id"]))["ok"] is True

    res = await srv._dispatch_mcp_tool(
        "release_file", {"file_path": BOGUS, "session_id": sess["id"]}, db, "/tmp",
    )

    assert res["released"] is True
    assert res["released_file_path"] == REAL
    assert res["resolved_from_legacy_shorthand"] is True
    assert await _holder(db, REAL) is None


async def test_mcp_release_file_accepts_the_full_resource_id_too(db):
    from meridian import server as srv

    pid, item = await _item(db, "4e2bce48-mcp-release-rid", [SHORTHAND])
    sess = await db_module.register_session(db, pid, "w1")
    assert (await _sprint_item_resource_claim_gate(db, pid, item["id"], sess["id"]))["ok"] is True

    res = await srv._dispatch_mcp_tool(
        "release_file", {"file_path": SHORTHAND, "session_id": sess["id"]}, db, "/tmp",
    )

    assert res["released"] is True
    assert await _holder(db, REAL) is None


async def test_mcp_release_file_plain_path_response_is_unchanged(db):
    from meridian import server as srv

    project = await db_module.create_project(db, "4e2bce48-mcp-release-plain")
    sess = await db_module.register_session(db, project["id"], "w1")

    res = await srv._dispatch_mcp_tool(
        "release_file", {"file_path": "pkg/never_locked.py", "session_id": sess["id"]}, db, "/tmp",
    )

    assert res == {"released": False, "file_path": "pkg/never_locked.py"}


async def test_reclaim_does_not_append_a_spurious_file_resource(db):
    pid, item = await _item(db, "4e2bce48-amend", [SHORTHAND])
    sess = await db_module.register_session(db, pid, "w1")
    await db_module.claim_sprint_item(db, pid, item["id"], actor=sess["id"])
    assert (await _sprint_item_resource_claim_gate(db, pid, item["id"], sess["id"]))["ok"] is True

    # Re-claiming the real file for the same item must not grow its declaration.
    await db_module.claim_file(db, REAL, sess["id"], item_id=item["id"])

    fresh = await db_module.get_sprint_item(db, item["id"])
    assert db_module.parse_touches_resources(fresh["touches_resources"]) == [SHORTHAND]
    assert not fresh.get("resources_amended")


async def test_whole_file_claim_over_a_symbol_declaration_still_amends(db):
    """A whole-file lock IS broader than a single symbol, so that amendment is kept."""
    pid, item = await _item(db, "4e2bce48-amend-symbol", ["symbol:pkg/mod.py::helper"])
    sess = await db_module.register_session(db, pid, "w1")
    await db_module.claim_sprint_item(db, pid, item["id"], actor=sess["id"])

    await db_module.claim_file(db, REAL, sess["id"], item_id=item["id"])

    fresh = await db_module.get_sprint_item(db, item["id"])
    assert f"file:{REAL}" in db_module.parse_touches_resources(fresh["touches_resources"])
    assert fresh.get("resources_amended")


def test_drive_letter_path_with_a_symbol_suffix_resolves_to_the_real_file():
    assert db_module._resource_file_of("file:C:/repo/x.py:helper") == "C:/repo/x.py"
    assert db_module._resource_file_of("file:C:\\repo\\x.py:helper") == "C:\\repo\\x.py"
    assert db_module._resource_file_of("file:C:/repo/x.py") == "C:/repo/x.py"
    assert db_module._resource_file_of("file:x.py:helper") == "x.py"


async def test_gate_locks_the_real_file_for_a_drive_letter_shorthand(db):
    pid, item = await _item(db, "4e2bce48-drive-symbol", ["file:C:/repo/x.py:helper"])
    sess = await db_module.register_session(db, pid, "w1")

    entry = (await _sprint_item_resource_claim_gate(db, pid, item["id"], sess["id"]))["lock_scope"][0]

    assert entry["file_path"] == "C:/repo/x.py"
    assert entry["resolved_from_legacy_shorthand"] is True


# ---------------------------------------------------------------------------
# Same literal-path mistake in the non-lock consumers
# ---------------------------------------------------------------------------

async def test_code_notes_surface_for_a_shorthand_declaration(db):
    pid, item = await _item(db, "4e2bce48-notes", [SHORTHAND])
    await db_module.add_project_note(
        db, pid, "careful in mod.py", "read this before editing", kind="code", file_path=REAL,
    )

    notes = await _code_notes_for_item_resources(db, pid, item)

    assert [entry["file_path"] for entry in notes] == [REAL]
    assert notes[0]["notes"][0]["title"] == "careful in mod.py"


def test_strict_evidence_paths_resolve_the_real_file():
    assert _declared_evidence_paths({"touches_resources": [SHORTHAND]}) == [REAL]


def test_prospect_context_names_the_real_file():
    ctx = _prospect_code_context({"touches_resources": [SHORTHAND], "title": "x"})
    assert ctx["files"] == [REAL]


def test_prospect_context_resolves_an_inferred_shorthand_too():
    ctx = _prospect_code_context({"touches_resources": [f"inferred:{SHORTHAND}"], "title": "x"})
    assert ctx["files"] == [REAL]


async def test_code_notes_surface_for_an_inferred_shorthand(db):
    pid, item = await _item(db, "4e2bce48-notes-inferred", [f"inferred:{SHORTHAND}"])
    await db_module.add_project_note(
        db, pid, "inferred note", "read this", kind="code", file_path=REAL,
    )

    notes = await _code_notes_for_item_resources(db, pid, item)

    assert [entry["file_path"] for entry in notes] == [REAL]


# ---------------------------------------------------------------------------
# Guidance must stop advertising the shorthand as the co-batching form
# ---------------------------------------------------------------------------

def test_symbol_scope_hint_recommends_the_canonical_symbol_form():
    hint = _check_file_only_resources_warning(
        ["file:pkg/mod.py"], "fix parse_config_value handling", "",
    )
    assert "symbol:pkg/mod.py::parse_config_value" in hint
    assert "file:pkg/mod.py:parse_config_value" not in hint


def test_tool_schemas_recommend_the_canonical_symbol_form():
    for tool_name in ("add_sprint_item", "update_sprint_item"):
        tool = next(t for t in _MCP_TOOLS_LIST if t["name"] == tool_name)
        description = tool["inputSchema"]["properties"]["touches_resources"]["description"]
        assert "symbol:path.py::" in description, tool_name
        assert "append ':symbol_name' to a file id" not in description, tool_name
