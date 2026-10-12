"""a9794fb9 -- DOCX region claims for every meridian-docs mutating tool.

273df573 (247b17d8) routed the mutating meridian-docs tools through the
tunnel-layer region-claim gate, tun._DOCS_WRITE_TOOLS. Ten more tools re-save
or merge the .docx and were missing from that map, so a second session could
write them over another session's claim. This file pins those ten additions
and adds a drift guard: every meridian-docs @mcp.tool() wrapper that takes a
.docx path must either be gated or be classified below as read-only (or as a
draft-only writer that never touches the document path).

The drift guard reads server.py with ast instead of importing it, so it does
not depend on the meridian-docs runtime packages.
"""
from __future__ import annotations

import ast
import os

import pytest

from meridian import db as db_module
from meridian.routes import tunnel as tun

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_DOCS_SERVER = os.path.join(
    _REPO, "extensions", "meridian-docs", "meridian_docs", "server.py"
)

# The ten mutators this item adds to _DOCS_WRITE_TOOLS, with the same
# (path_arg, anchor_arg) shape the map uses.
NEW_MUTATORS = {
    "insert_media_part": ("docx_path", "anchor_para_id"),
    "remove_package_part": ("docx_path", None),
    "remove_cross_reference": ("docx_path", "anchor_para_id"),
    "flag_for_review": ("docx_path", None),
    "insert_table": ("docx_path", "anchor_para_id"),
    "remove_table": ("docx_path", None),
    "insert_column": ("docx_path", None),
    "split_cell": ("docx_path", None),
    "transpose_table": ("docx_path", None),
    "apply_and_merge_batch_transform": ("canonical_path", None),
}

# Wrappers that take a .docx-style path but never write that document.
READ_ONLY_DOCS_TOOLS = frozenset({
    "search_document",
    "read_document_snapshot",
    "locate_anchor",
    "locate_anchors",
    "find_caption_paragraph",
    "get_document_review",
    "audit_document",
    "check_render_capability",
    "render_with_receipt",
    "list_render_receipts",
    "check_release_render_gate",
    "find_image_paragraph",
    "extract_paragraph_images",
    "audit_equation_style",
    "audit_caption_style",
    "audit_table_style",
    "audit_heading_style",
    "audit_manuscript_structure",
    "audit_reference_consistency",
    "audit_equation_contract",
    "audit_equation_integrity",
    "scan_citation_keys",
    "get_section_content",
    "find_references_to",
    "scan_stale_notes",
    "plan_batch_transform",
    "build_prose_edit_packet",
})

# Draft-only writers: document_path is opened read-only and only
# draft_output_path is written, so the canonical document needs no claim.
DRAFT_ONLY_DOCS_TOOLS = frozenset({
    "apply_batch_transform",
    "apply_prose_edit_packets",
    "repair_equation_batch",
})

_DOC_PATH_PARAMS = frozenset({"docx_path", "canonical_path", "document_path"})


def _docs_server_tools() -> dict:
    """Map each @mcp.tool() function in meridian-docs server.py to its params."""
    with open(_DOCS_SERVER, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    tools = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_tool = any(
            isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "tool"
            for d in node.decorator_list
        )
        if is_tool:
            tools[node.name] = [a.arg for a in node.args.args] + [
                a.arg for a in node.args.kwonlyargs
            ]
    return tools


# ---------------------------------------------------------------------------
# Map registration and target resolution.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool_name", sorted(NEW_MUTATORS))
def test_new_mutator_is_registered_with_expected_arguments(tool_name):
    assert tun._DOCS_WRITE_TOOLS[tool_name] == NEW_MUTATORS[tool_name]


def test_docs_write_target_resolves_new_mutators():
    assert tun._docs_write_target(
        "insert_table",
        {"docx_path": "t.docx", "anchor_para_id": "P9", "rows": 2, "cols": 2},
    ) == ("t.docx", "P9")
    assert tun._docs_write_target(
        "remove_table", {"docx_path": "t.docx", "table_index": 0},
    ) == ("t.docx", None)
    assert tun._docs_write_target(
        "apply_and_merge_batch_transform",
        {"canonical_path": "c.docx", "draft_output_path": "d.docx", "operations": []},
    ) == ("c.docx", None)
    # Connector-prefixed name resolves the same way.
    assert tun._docs_write_target(
        "docs__split_cell",
        {"docx_path": "t.docx", "table_index": 0, "row_index": 0, "col_index": 0, "cols": 2},
    ) == ("t.docx", None)


def test_new_mutator_without_path_argument_fails_open():
    assert tun._docs_write_target("insert_table", {"anchor_para_id": "P9"}) is None
    assert tun._docs_write_target(
        "apply_and_merge_batch_transform", {"draft_output_path": "d.docx"},
    ) is None


def test_every_map_entry_names_a_real_wrapper_parameter():
    tools = _docs_server_tools()
    for name, (path_arg, anchor_arg) in tun._DOCS_WRITE_TOOLS.items():
        assert name in tools, (
            f"{name} is in _DOCS_WRITE_TOOLS but is not a meridian-docs @mcp.tool()"
        )
        assert path_arg in tools[name], f"{name} has no parameter {path_arg!r}"
        if anchor_arg is not None:
            assert anchor_arg in tools[name], f"{name} has no parameter {anchor_arg!r}"


def test_every_map_entry_declares_session_id_on_its_wrapper():
    tools = _docs_server_tools()
    missing = sorted(n for n in tun._DOCS_WRITE_TOOLS if "session_id" not in tools.get(n, []))
    assert missing == [], (
        f"{missing} do not declare session_id, so a compliant client cannot tell "
        "the tunnel guard who it is and the owner's own writes would be refused"
    )


# ---------------------------------------------------------------------------
# Drift guard: every docx-taking wrapper is gated or explicitly classified.
# ---------------------------------------------------------------------------

def test_every_docx_taking_tool_is_gated_or_classified():
    tools = _docs_server_tools()
    doc_tools = {n for n, params in tools.items() if _DOC_PATH_PARAMS & set(params)}
    gated = set(tun._DOCS_WRITE_TOOLS)
    classified = READ_ONLY_DOCS_TOOLS | DRAFT_ONLY_DOCS_TOOLS

    unclassified = sorted(doc_tools - gated - classified)
    assert unclassified == [], (
        f"New .docx-taking meridian-docs tools are neither gated in "
        f"_DOCS_WRITE_TOOLS nor classified read-only here: {unclassified}. "
        "A writer must be added to the map; a reader must be added to "
        "READ_ONLY_DOCS_TOOLS."
    )
    stale = sorted(classified - doc_tools)
    assert stale == [], f"Classified tools no longer exist with a document path: {stale}"
    assert not (READ_ONLY_DOCS_TOOLS & gated), "a read-only tool must not be gated"
    assert not (DRAFT_ONLY_DOCS_TOOLS & gated), "a draft-only writer must not be gated"


# ---------------------------------------------------------------------------
# Behaviour through the real relay: a whole-file lock held by another session
# blocks each new mutator before anything is relayed to the tunnel.
# ---------------------------------------------------------------------------

async def _mk_session(db, name: str) -> str:
    proj = await db_module.create_project(db, name=f"proj-{name}")
    sess = await db_module.register_session(db, project_id=proj["id"], name=name)
    return sess["id"]


@pytest.mark.parametrize("tool_name", sorted(NEW_MUTATORS))
async def test_whole_file_lock_blocks_new_mutator(db, monkeypatch, tool_name):
    owner = await _mk_session(db, f"a9794-owner-{tool_name}")
    doc = f"a9794-locked-{tool_name}.docx"
    await db_module.claim_file(db, doc, owner, mode="write")

    tenant = f"tenant-a9794-block-{tool_name}"
    monkeypatch.setitem(tun._tunnel_tool_routes, tenant, {f"docs__{tool_name}": "docs"})
    monkeypatch.setitem(tun._tunnel_docs_sockets, tenant, object())

    async def _fake_jsonrpc(*a, **k):
        raise AssertionError("must not relay when whole-file-locked")

    monkeypatch.setattr(tun, "_tunnel_jsonrpc", _fake_jsonrpc)

    path_arg = NEW_MUTATORS[tool_name][0]
    with pytest.raises(RuntimeError, match="whole-file write lock"):
        await tun.call_tunnel_tool(
            tenant, f"docs__{tool_name}", {path_arg: doc},
            db=db, session_id="other-session",
        )


async def test_lock_owner_is_relayed_for_new_mutator(db, monkeypatch):
    """The owner of the whole-file claim is not blocked by its own lock."""
    owner = await _mk_session(db, "a9794-owner-passes")
    doc = "a9794-owner-passes.docx"
    await db_module.claim_file(db, doc, owner, mode="write")

    tenant = "tenant-a9794-owner"
    monkeypatch.setitem(tun._tunnel_tool_routes, tenant, {"docs__insert_table": "docs"})
    monkeypatch.setitem(tun._tunnel_docs_sockets, tenant, object())

    called = {"relayed": False}

    async def _fake_jsonrpc(*a, **k):
        called["relayed"] = True
        return {"result": {"content": [{"type": "text", "text": "{}"}]}}

    monkeypatch.setattr(tun, "_tunnel_jsonrpc", _fake_jsonrpc)

    await tun.call_tunnel_tool(
        tenant, "docs__insert_table",
        {"docx_path": doc, "anchor_para_id": "P1", "rows": 1, "cols": 1},
        db=db, session_id=owner,
    )
    assert called["relayed"] is True
