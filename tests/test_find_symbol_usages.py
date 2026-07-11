"""Coverage for find_symbol_usages — symbol/equation cross-reference tracking (9605edb0).

The tool answers "where else does this defined symbol/equation appear?" so a
later mention can be checked to point back to its DEFINITION rather than
assuming the reader remembers it. Exercises:

* DocStructureStore.find_symbol_usages end to end on a local SQLite sidecar:
  - an equation defined once + reused (same normalized latex) in a later element,
  - a symbol reused textually in a later PROSE paragraph,
  - definition-vs-reuse classification (earliest ordinal = definition),
  - ordinal ordering (definition sorts first),
  - resolution from a doc_equations row id (uses its stored latex_normalized),
  - resolution from a raw symbol string (normalized with the store's own
    normalize_latex, so "E = m c^2" matches a stored "E=mc^2"),
  - a non-existent symbol / blank input returns an empty hit list cleanly.
* The find_symbol_usages MCP tool through the real _dispatch_mcp_tool path:
  round-trip, unknown-doc empty result, and required-argument validation.
"""
from __future__ import annotations

import asyncio

import pytest

from meridian import doc_store
from meridian import db as db_module


# ---------------------------------------------------------------------------
# DocStructureStore.find_symbol_usages — full round-trip on a local sidecar
# ---------------------------------------------------------------------------

async def _open_store(tmp_path) -> doc_store.DocStructureStore:
    conn = await db_module.init_db(str(tmp_path / "doc_structure.db"))
    store = doc_store.DocStructureStore(conn)
    await store.ensure_schema()
    return store


async def _seed_document(store) -> str:
    """A document with:
      ordinal 0 — a heading (never a symbol hit),
      ordinal 1 — the paragraph DEFINING E=mc^2 (prose contains the symbol),
      ordinal 2 — a prose paragraph REUSING the symbol E=mc^2 later,
      ordinal 3 — an unrelated paragraph (no symbol),
    plus two equations: the E=mc^2 definition (element 1, ordinal 1) and a
    later reuse of the SAME normalized latex (element 2, ordinal 2)."""
    doc = await store.put_document(
        "proj-sym", "docx",
        [
            {"ordinal": 0, "level": 1, "kind": "heading", "text": "Physics",
             "id": "el-h0"},
            {"ordinal": 1, "kind": "paragraph",
             "text": "We define the relation E=mc^2 here.", "id": "el-def"},
            {"ordinal": 2, "kind": "paragraph",
             "text": "As shown above, E = m c^2 governs the mass defect.",
             "id": "el-reuse"},
            {"ordinal": 3, "kind": "paragraph",
             "text": "This sentence mentions nothing relevant.",
             "id": "el-none"},
        ],
        source="chapter1.docx",
    )
    await store.put_equations(doc["id"], [
        {"latex": "E=mc^2", "element_id": "el-def", "ordinal": 1,
         "semantic_label": "mass-energy"},
        {"latex": "E = m c^2", "element_id": "el-reuse", "ordinal": 2},
    ])
    return doc["id"]


def test_symbol_string_resolves_and_classifies_definition_vs_reuse(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc_id = await _seed_document(store)
            res = await store.find_symbol_usages(doc_id, "E=mc^2")

            assert res["resolved_from"] == "symbol"
            assert res["target"] == "E=mc^2"
            hits = res["hits"]
            # Two equations (same normalized latex) + two prose paragraphs.
            assert len(hits) == 4

            # Ordered by ordinal; equation sorts before paragraph at a tie.
            assert [h["ordinal"] for h in hits] == [1, 1, 2, 2]
            assert [h["context"] for h in hits] == [
                "equation", "paragraph", "equation", "paragraph",
            ]

            # Exactly one definition, and it is the earliest hit.
            defs = [h for h in hits if h["is_definition"]]
            assert len(defs) == 1
            assert defs[0] is hits[0]
            assert hits[0]["is_reuse"] is False
            assert all(h["is_reuse"] for h in hits[1:])
            assert not any(h["is_definition"] for h in hits[1:])
        finally:
            await store.close()

    asyncio.run(_run())


def test_prose_reuse_matches_whitespace_insensitively(tmp_path):
    """A later PROSE paragraph spelling the symbol as "E = m c^2" still counts
    as a reuse of the "E=mc^2" definition (normalized substring test)."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc_id = await _seed_document(store)
            res = await store.find_symbol_usages(doc_id, "E=mc^2")
            prose = [h for h in res["hits"] if h["context"] == "paragraph"]
            element_ids = {h["element_id"] for h in prose}
            assert element_ids == {"el-def", "el-reuse"}
            # The unrelated paragraph never matches.
            assert "el-none" not in element_ids
            # The later prose paragraph is a reuse, not the definition.
            reuse = next(h for h in prose if h["element_id"] == "el-reuse")
            assert reuse["is_reuse"] is True
            assert reuse["is_definition"] is False
        finally:
            await store.close()

    asyncio.run(_run())


def test_resolves_from_equation_id_using_stored_normalized_latex(tmp_path):
    """Passing a doc_equations row id resolves the target to THAT row's stored
    latex_normalized (authoritative), not a re-normalization of the id."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc_id = await _seed_document(store)
            equations = await store.get_equations(doc_id)
            first = equations[0]
            assert first["latex_normalized"] == "E=mc^2"

            res = await store.find_symbol_usages(doc_id, first["id"])
            assert res["resolved_from"] == "equation_id"
            assert res["target"] == "E=mc^2"
            # Same target → same 4 hits, definition first.
            assert len(res["hits"]) == 4
            assert res["hits"][0]["is_definition"] is True
        finally:
            await store.close()

    asyncio.run(_run())


def test_nonexistent_symbol_returns_empty_hits_cleanly(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc_id = await _seed_document(store)
            res = await store.find_symbol_usages(doc_id, r"\nabla \times B")
            assert res["resolved_from"] == "symbol"
            assert res["hits"] == []
        finally:
            await store.close()

    asyncio.run(_run())


def test_blank_and_unknown_document_yield_empty(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc_id = await _seed_document(store)
            # Blank symbol → empty, no target, never raises.
            blank = await store.find_symbol_usages(doc_id, "   ")
            assert blank["target"] == ""
            assert blank["hits"] == []
            # Unknown document id → empty (no elements, no equations).
            unknown = await store.find_symbol_usages("no-such-doc", "E=mc^2")
            assert unknown["hits"] == []
        finally:
            await store.close()

    asyncio.run(_run())


def test_equation_id_with_blank_normalized_latex_yields_empty(tmp_path):
    """An equation id whose stored latex_normalized is blank resolves to an empty
    target (never falls back to normalizing the id string)."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document(
                "proj-sym", "docx",
                [{"ordinal": 0, "kind": "paragraph", "text": "prose"}],
                source="blank.docx",
            )
            # OMML with no flattenable text → blank latex_normalized.
            put = await store.put_equations(doc["id"], [{"omml_raw": "<m:oMath></m:oMath>"}])
            eq_id = put["inserted"][0]["id"]
            assert (put["inserted"][0]["latex_normalized"] or "") == ""

            res = await store.find_symbol_usages(doc["id"], eq_id)
            assert res["resolved_from"] == "equation_id"
            assert res["target"] == ""
            assert res["hits"] == []
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# MCP tool — find_symbol_usages through _dispatch_mcp_tool
# ---------------------------------------------------------------------------

def _seed_store_via_env(tmp_path, monkeypatch):
    sidecar = str(tmp_path / "mcp_doc_structure.db")
    monkeypatch.setenv("MERIDIAN_DOC_STORE_URL", sidecar)
    doc_store._reset_doc_store_cache()
    return sidecar


def test_mcp_find_symbol_usages_round_trip(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "sym-proj")
            pid = proj["id"]

            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            doc = await seed.put_document(
                pid, "docx",
                [
                    {"ordinal": 0, "kind": "paragraph",
                     "text": "Define E=mc^2.", "id": "p-def"},
                    {"ordinal": 1, "kind": "paragraph",
                     "text": "Later, E = m c^2 recurs.", "id": "p-reuse"},
                ],
                source="chapter1.docx",
            )
            await seed.put_equations(doc["id"], [
                {"latex": "E=mc^2", "element_id": "p-def", "ordinal": 0},
            ])

            res = await mh._dispatch_mcp_tool(
                "find_symbol_usages",
                {"project_id": pid, "doc": "chapter1.docx",
                 "symbol_or_equation_id": "E=mc^2"},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["document_id"] == doc["id"]
            assert res["target"] == "E=mc^2"
            hits = res["hits"]
            assert hits  # at least the equation + two prose paragraphs
            # Exactly one definition, and it is first by ordinal.
            assert hits[0]["is_definition"] is True
            assert sum(1 for h in hits if h["is_definition"]) == 1
            assert any(h["is_reuse"] for h in hits)
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_find_symbol_usages_unknown_doc_returns_empty(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "sym-proj-2")
            res = await mh._dispatch_mcp_tool(
                "find_symbol_usages",
                {"project_id": proj["id"], "doc": "never-ingested.docx",
                 "symbol_or_equation_id": "E=mc^2"},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["document_id"] is None
            assert res["hits"] == []
            assert res["target"] == ""
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_find_symbol_usages_requires_project_id_doc_and_symbol(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            assert (await mh._dispatch_mcp_tool(
                "find_symbol_usages", {}, db, str(tmp_path),
            )).get("error")
            assert (await mh._dispatch_mcp_tool(
                "find_symbol_usages", {"project_id": "p"}, db, str(tmp_path),
            )).get("error")
            assert (await mh._dispatch_mcp_tool(
                "find_symbol_usages",
                {"project_id": "p", "doc": "x.docx"}, db, str(tmp_path),
            )).get("error")
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_find_symbol_usages_registered_read_only():
    """Declared in the canonical tool list, marked read-only, and mirrored in
    stdio + title/example maps."""
    from meridian.mcp_tools import (
        _MCP_TOOLS_LIST, _READ_ONLY_TOOLS, _TITLE_OVERRIDES, _TOOL_EXAMPLES,
    )

    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    assert "find_symbol_usages" in by_name
    tool = by_name["find_symbol_usages"]
    assert tool["inputSchema"]["required"] == ["doc", "symbol_or_equation_id"]
    assert "project_name" in tool["inputSchema"]["properties"]
    assert "find_symbol_usages" in _READ_ONLY_TOOLS
    assert tool["annotations"]["readOnlyHint"] is True
    assert _TITLE_OVERRIDES["find_symbol_usages"] == "Find Symbol Usages"
    assert "find_symbol_usages" in _TOOL_EXAMPLES
