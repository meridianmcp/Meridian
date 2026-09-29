"""3f47cc6e — project resolution precedence.

Before this fix ``_dispatch_mcp_tool`` resolved ``project_name`` FIRST and let a
resolvable name silently override an explicit ``project_id``. Because rename and
merge free old names (a merge renames the source to ``[merged] <name>``), a
stale name could retarget a call that carried the right id. These tests pin:

* an id/name pair that resolves to DIFFERENT projects is rejected with a clear
  error (and never dispatched against either project);
* an unresolvable name next to a valid id keeps the id; a matching pair works;
* a lone ``project_name`` still works (and still errors when unresolvable);
* the scoped-token path keeps its opaque "outside your access scope" error
  rather than leaking a conflict message about an out-of-scope project;
* ``resolve_receipt_project_id`` prefers an explicit UUID id over the default;
* the ``get_project_by_name`` docs (db docstring + MCP tool descriptions) say
  what the code does (exact, then case-insensitive exact -- NOT substring);
* AGENTS.md "Option B" no longer claims an ``mcp-remote`` env block reaches the
  hosted server.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from meridian import code_intel_receipt as cir
from meridian import db as db_module
from meridian import server as _server  # noqa: F401 -- import order: server before mcp.handler
from meridian.mcp.handler import _dispatch_mcp_tool, _handle_mcp_request

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFLICT = "refer to different projects"


async def _items(db, **args):
    return await _dispatch_mcp_tool("get_sprint_items", args, db, "/tmp")


async def _two_projects(db, name_a="prec-a", name_b="prec-b"):
    a = await db_module.create_project(db, name_a)
    b = await db_module.create_project(db, name_b)
    await db_module.add_sprint_item(db, a["id"], "v1", f"item-of-{name_a}")
    await db_module.add_sprint_item(db, b["id"], "v1", f"item-of-{name_b}")
    return a, b


# ---------------------------------------------------------------------------
# handler resolver: id vs name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_id_and_mismatching_name_is_rejected(db):
    a, b = await _two_projects(db)
    with pytest.raises(ValueError, match=CONFLICT) as exc:
        await _items(db, project_id=a["id"], project_name="prec-b")
    # The error names both references so the caller can see which is stale.
    assert a["id"] in str(exc.value)
    assert b["id"] in str(exc.value)
    assert "prec-b" in str(exc.value)


@pytest.mark.asyncio
async def test_mismatch_is_rejected_for_a_write_tool_too(db):
    """A conflicting pair must not let a WRITE land on the name's project."""
    a, b = await _two_projects(db)
    with pytest.raises(ValueError, match=CONFLICT):
        await _dispatch_mcp_tool(
            "add_sprint_item",
            {
                "project_id": a["id"], "project_name": "prec-b",
                "version": "v1", "title": "must-not-be-written",
            },
            db, "/tmp",
        )
    for pid in (a["id"], b["id"]):
        titles = {it["title"] for it in await db_module.get_sprint_items(db, pid)}
        assert "must-not-be-written" not in titles


@pytest.mark.asyncio
async def test_id_and_matching_name_works(db):
    a, _b = await _two_projects(db)
    items = await _items(db, project_id=a["id"], project_name="prec-a")
    assert {it["title"] for it in items} == {"item-of-prec-a"}


@pytest.mark.asyncio
async def test_uppercase_uuid_matching_name_is_not_a_conflict(db):
    a, _b = await _two_projects(db)
    items = await _items(db, project_id=a["id"].upper(), project_name="prec-a")
    assert {it["title"] for it in items} == {"item-of-prec-a"}


@pytest.mark.asyncio
async def test_id_with_unresolvable_name_keeps_the_id(db):
    a, _b = await _two_projects(db)
    items = await _items(db, project_id=a["id"], project_name="no-such-project")
    assert {it["title"] for it in items} == {"item-of-prec-a"}


@pytest.mark.asyncio
async def test_lone_id_passes_through(db):
    a, _b = await _two_projects(db)
    items = await _items(db, project_id=a["id"])
    assert {it["title"] for it in items} == {"item-of-prec-a"}


@pytest.mark.asyncio
async def test_lone_project_name_still_resolves(db):
    _a, b = await _two_projects(db)
    items = await _items(db, project_name="prec-b")
    assert {it["title"] for it in items} == {"item-of-prec-b"}
    assert all(it["project_id"] == b["id"] for it in items)


@pytest.mark.asyncio
async def test_lone_project_name_case_insensitive_still_resolves(db):
    _a, _b = await _two_projects(db)
    items = await _items(db, project_name="PREC-B")
    assert {it["title"] for it in items} == {"item-of-prec-b"}


@pytest.mark.asyncio
async def test_lone_unresolvable_project_name_still_raises(db):
    with pytest.raises(ValueError, match="no project found matching name"):
        await _items(db, project_name="ghost-project")


@pytest.mark.asyncio
async def test_non_uuid_project_id_still_resolves_as_a_name(db):
    _a, _b = await _two_projects(db)
    items = await _items(db, project_id="prec-b")
    assert {it["title"] for it in items} == {"item-of-prec-b"}


@pytest.mark.asyncio
async def test_non_uuid_project_id_conflicting_with_project_name_is_rejected(db):
    await _two_projects(db)
    with pytest.raises(ValueError, match=CONFLICT):
        await _items(db, project_id="prec-a", project_name="prec-b")


@pytest.mark.asyncio
async def test_non_uuid_project_id_agreeing_with_project_name_works(db):
    await _two_projects(db)
    items = await _items(db, project_id="prec-a", project_name="PREC-A")
    assert {it["title"] for it in items} == {"item-of-prec-a"}


# ---------------------------------------------------------------------------
# stale names after rename / merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_name_freed_by_rename_and_reused_no_longer_retargets(db):
    """A is renamed away from "shared-name"; B then takes the freed name. A call
    still carrying A's id plus the now-stale "shared-name" used to silently run
    against B. It must now be rejected."""
    a = await db_module.create_project(db, "shared-name")
    await db_module.add_sprint_item(db, a["id"], "v1", "item-of-a")
    await db_module.rename_project(db, a["id"], "a-renamed")
    b = await db_module.create_project(db, "shared-name")
    await db_module.add_sprint_item(db, b["id"], "v1", "item-of-b")

    with pytest.raises(ValueError, match=CONFLICT):
        await _items(db, project_id=a["id"], project_name="shared-name")

    # ...and the id alone (or the id with its CURRENT name) still reaches A.
    assert {it["title"] for it in await _items(db, project_id=a["id"])} == {"item-of-a"}
    assert {
        it["title"]
        for it in await _items(db, project_id=a["id"], project_name="a-renamed")
    } == {"item-of-a"}


@pytest.mark.asyncio
async def test_name_freed_by_rename_and_not_reused_keeps_the_id(db):
    a = await db_module.create_project(db, "soon-renamed")
    await db_module.add_sprint_item(db, a["id"], "v1", "item-of-a")
    await db_module.rename_project(db, a["id"], "now-renamed")
    items = await _items(db, project_id=a["id"], project_name="soon-renamed")
    assert {it["title"] for it in items} == {"item-of-a"}


@pytest.mark.asyncio
async def test_stale_name_of_merged_project_does_not_retarget(db):
    """merge renames the source to "[merged] <name>", freeing its old name; a
    call that names the merge TARGET by id but the (now freed) source by name
    keeps the id and never runs against the archived source."""
    src = await db_module.create_project(db, "merge-src")
    tgt = await db_module.create_project(db, "merge-tgt")
    await db_module.add_sprint_item(db, tgt["id"], "v1", "item-of-target")
    await db_module.merge_project(db, src["id"], tgt["id"])

    items = await _items(db, project_id=tgt["id"], project_name="merge-src")
    assert all(it["project_id"] == tgt["id"] for it in items)
    assert "item-of-target" in {it["title"] for it in items}


# ---------------------------------------------------------------------------
# JSON-RPC surface + scope interaction
# ---------------------------------------------------------------------------


def _rpc(args, name="get_sprint_items"):
    return {
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }


@pytest.mark.asyncio
async def test_conflict_surfaces_as_a_jsonrpc_error(db):
    a, _b = await _two_projects(db)
    resp = await _handle_mcp_request(
        _rpc({"project_id": a["id"], "project_name": "prec-b"}),
        db=db, data_dir="/tmp", scoped_project_ids=None,
    )
    assert "error" in resp, resp
    assert resp["error"]["code"] == -32603
    assert CONFLICT in resp["error"]["message"]
    assert "access scope" not in resp["error"]["message"]


@pytest.mark.asyncio
async def test_matching_pair_over_jsonrpc_dispatches_normally(db):
    a, _b = await _two_projects(db)
    resp = await _handle_mcp_request(
        _rpc({"project_id": a["id"], "project_name": "prec-a"}),
        db=db, data_dir="/tmp", scoped_project_ids=None,
    )
    assert "error" not in resp, resp
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert {it["title"] for it in payload} == {"item-of-prec-a"}


@pytest.mark.asyncio
async def test_scoped_out_of_scope_name_keeps_the_opaque_scope_error(db):
    """In-scope id + a name that resolves to an OUT-of-scope project must still
    say "outside your access scope" -- a conflict message would confirm that
    the name exists in a project the caller may not see."""
    a, b = await _two_projects(db)
    with pytest.raises(ValueError, match="outside your access scope") as exc:
        await _dispatch_mcp_tool(
            "get_sprint_items",
            {"project_id": a["id"], "project_name": "prec-b"},
            db, "/tmp", scoped_project_ids=[a["id"]],
        )
    assert CONFLICT not in str(exc.value)
    assert b["id"] not in str(exc.value)


@pytest.mark.asyncio
async def test_scoped_two_in_scope_projects_conflict_is_rejected(db):
    a, b = await _two_projects(db)
    with pytest.raises(ValueError, match=CONFLICT):
        await _dispatch_mcp_tool(
            "get_sprint_items",
            {"project_id": a["id"], "project_name": "prec-b"},
            db, "/tmp", scoped_project_ids=[a["id"], b["id"]],
        )


@pytest.mark.asyncio
async def test_scoped_matching_pair_in_scope_works(db):
    a, _b = await _two_projects(db)
    items = await _dispatch_mcp_tool(
        "get_sprint_items",
        {"project_id": a["id"], "project_name": "prec-a"},
        db, "/tmp", scoped_project_ids=[a["id"]],
    )
    assert {it["title"] for it in items} == {"item-of-prec-a"}


# ---------------------------------------------------------------------------
# code_intel_receipt.resolve_receipt_project_id: explicit id beats default
# ---------------------------------------------------------------------------

EXPLICIT = "5787cc92-ba7d-4788-b17c-28ab7938b839"
DEFAULT = "11111111-2222-3333-4444-555555555555"


def test_receipt_explicit_uuid_id_beats_the_default_project(monkeypatch):
    monkeypatch.setattr(
        "meridian.toml_config.get_default_project_id", lambda: DEFAULT
    )
    assert cir.resolve_receipt_project_id({"project_id": EXPLICIT}) == EXPLICIT


def test_receipt_explicit_uuid_id_used_when_no_default(monkeypatch):
    monkeypatch.setattr("meridian.toml_config.get_default_project_id", lambda: None)
    assert cir.resolve_receipt_project_id({"project_id": EXPLICIT}) == EXPLICIT


def test_receipt_repo_slug_project_id_falls_back_to_the_default(monkeypatch):
    """Tunnel-forwarded code-intel tools carry a repo-path slug, which is NOT
    Meridian's id -- it must never be attributed; the default still applies."""
    monkeypatch.setattr(
        "meridian.toml_config.get_default_project_id", lambda: DEFAULT
    )
    slug = "C-Users-13144-Documents-Meridian-repository"
    assert cir.resolve_receipt_project_id({"project_id": slug}) == DEFAULT


def test_receipt_default_used_when_no_project_id_given(monkeypatch):
    monkeypatch.setattr(
        "meridian.toml_config.get_default_project_id", lambda: DEFAULT
    )
    assert cir.resolve_receipt_project_id({}) == DEFAULT
    assert cir.resolve_receipt_project_id(None) == DEFAULT


def test_receipt_unresolvable_stays_none(monkeypatch):
    monkeypatch.setattr("meridian.toml_config.get_default_project_id", lambda: None)
    assert cir.resolve_receipt_project_id({"project_id": "repo-slug"}) is None
    assert cir.resolve_receipt_project_id(None) is None


def test_receipt_explicit_id_beats_env_default(monkeypatch):
    """Same rule through the real toml_config reader (env var default)."""
    monkeypatch.setenv("MERIDIAN_PROJECT_ID", DEFAULT)
    assert cir.resolve_receipt_project_id({"project_id": EXPLICIT}) == EXPLICIT
    assert cir.resolve_receipt_project_id({"project_id": "slug"}) == DEFAULT


# ---------------------------------------------------------------------------
# get_project_by_name: behaviour and docs agree (exact, then CI exact)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_project_by_name_is_exact_then_case_insensitive_exact(db):
    p = await db_module.create_project(db, "Kensington Park")
    exact = await db_module.get_project_by_name(db, "Kensington Park")
    assert exact is not None and exact["id"] == p["id"]
    ci = await db_module.get_project_by_name(db, "kensington PARK")
    assert ci is not None and ci["id"] == p["id"]
    # Substring / partial matches deliberately do NOT resolve.
    assert await db_module.get_project_by_name(db, "Kensington") is None
    assert await db_module.get_project_by_name(db, "Park") is None


def test_get_project_by_name_docstring_matches_behaviour():
    doc = db_module.get_project_by_name.__doc__ or ""
    assert "fuzzy" not in doc.lower()
    assert "exact" in doc.lower()
    assert "not a substring" in doc.lower()


def test_get_project_by_name_tool_descriptions_do_not_promise_substring_match():
    from meridian.mcp_tools import _MCP_TOOLS_LIST

    tool = next(t for t in _MCP_TOOLS_LIST if t["name"] == "get_project_by_name")
    desc = tool["description"]
    assert "case-insensitive substring match" not in desc
    assert "not a substring search" in desc

    stdio_src = (REPO_ROOT / "meridian" / "mcp" / "stdio_handler.py").read_text(
        encoding="utf-8"
    )
    # The stdio Tool(name="get_project_by_name") declaration and its `name`
    # parameter text no longer advertise substring matching.
    start = stdio_src.index('name="get_project_by_name"')
    block = stdio_src[start:start + 1200]
    assert "case-insensitive substring" not in block
    assert "Full or partial" not in block
    assert "not a substring search" in block
    assert "no partial" in block


# ---------------------------------------------------------------------------
# AGENTS.md "Option B" is self-hosted only
# ---------------------------------------------------------------------------


def _option_b_section() -> str:
    text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8-sig")
    start = text.index("**Option B")
    end = text.index("The env var takes precedence over the toml value", start)
    return text[start:end]


def test_agents_md_option_b_does_not_put_project_id_in_a_hosted_env_block():
    section = _option_b_section()
    blocks = re.findall(r"```json\n(.*?)```", section, re.S)
    assert blocks, "Option B must keep a JSON example"
    for block in blocks:
        if "MERIDIAN_PROJECT_ID" in block:
            assert "mcp-remote" not in block, (
                "MERIDIAN_PROJECT_ID in an mcp-remote env block never reaches "
                "the hosted server"
            )
            assert "usemeridian.us" not in block
    assert any("MERIDIAN_PROJECT_ID" in b for b in blocks)


def test_agents_md_option_b_says_self_hosted_only_and_not_hosted():
    section = _option_b_section()
    lowered = section.lower()
    assert "self-hosted only" in lowered
    assert "does **not** work on the hosted tier" in lowered
    assert "explicitly" in lowered  # hosted callers pass project_id explicitly
