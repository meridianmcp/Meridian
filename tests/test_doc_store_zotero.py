"""Coverage for the cross-document Zotero resolve pass + MCP tools (fefb596a).

CI-SAFE: no network. Every test injects a STUB resolver into
``DocStructureStore.resolve_zotero_edges`` (or, for the MCP tools,
monkeypatches ``meridian.zotero_client.resolve_citation_ref``) so nothing here
ever talks to a real Zotero instance. Uses a local SQLite doc-store sidecar in
``tmp_path`` exactly like tests/test_doc_store.py.
"""
from __future__ import annotations

import asyncio

import pytest

from meridian import doc_store
from meridian import db as db_module


async def _open_store(tmp_path) -> doc_store.DocStructureStore:
    conn = await db_module.init_db(str(tmp_path / "doc_structure.db"))
    store = doc_store.DocStructureStore(conn)
    await store.ensure_schema()
    return store


def _citation_doc_elements():
    """Two sections, three citation markers, two bib entries (matching test_doc_store)."""
    return [
        {"ordinal": 0, "level": 2, "kind": "section", "text": "Introduction",
         "ref": None, "parent_ordinal": None},
        {"ordinal": 1, "level": None, "kind": "citation",
         "text": r"\cite{knuth1984}", "ref": "knuth1984", "parent_ordinal": 0},
        {"ordinal": 2, "level": None, "kind": "citation",
         "text": r"\citep{missing_key}", "ref": "missing_key", "parent_ordinal": 0},
        {"ordinal": 3, "level": 2, "kind": "section", "text": "Results",
         "ref": None, "parent_ordinal": None},
        {"ordinal": 4, "level": None, "kind": "citation",
         "text": r"\citet{lamport1994}", "ref": "lamport1994", "parent_ordinal": 3},
        {"ordinal": 5, "level": None, "kind": "bibliography",
         "text": "Knuth", "ref": "knuth1984", "parent_ordinal": None},
        {"ordinal": 6, "level": None, "kind": "bibliography",
         "text": "Lamport", "ref": "lamport1994", "parent_ordinal": None},
    ]


def _stub_resolver(mapping):
    """Build an async resolver returning a fixed item dict for known refs, else None."""
    async def _resolver(ref, **_kwargs):
        return mapping.get(ref)
    return _resolver


# ---------------------------------------------------------------------------
# resolve_zotero_edges — happy path, idempotency, counts
# ---------------------------------------------------------------------------

def test_resolve_zotero_edges_creates_zotero_item_edges(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document(
                "proj-1", "latex", _citation_doc_elements(), source="paper.tex",
            )
            resolver = _stub_resolver({
                "knuth1984": {"zotero_key": "ZK1", "doi": "10.1/knuth", "title": "The TeXbook"},
                "lamport1994": {"zotero_key": "ZK2", "doi": "10.2/lamport", "title": "LaTeX"},
                # missing_key resolves to None (unresolved).
            })
            summary = await store.resolve_zotero_edges("proj-1", resolver=resolver)

            assert summary == {"resolved": 2, "unresolved": 1, "cross_doc_linked": 0}

            # Two zotero_item edges materialised; the DOI is the target_ref.
            edges = await store.get_edges("proj-1", document_id=doc["id"])
            zotero_edges = [e for e in edges if e["target_kind"] == "zotero_item"]
            assert len(zotero_edges) == 2
            by_ref = {e["target_ref"] for e in zotero_edges}
            assert by_ref == {"10.1/knuth", "10.2/lamport"}
            for e in zotero_edges:
                assert e["edge_kind"] == "cites"
                assert e["target_element_id"] is None
                assert e["target_document_id"] is None
                assert e["resolved_at"] is not None
        finally:
            await store.close()

    asyncio.run(_run())


def test_unresolved_citation_gets_no_zotero_edge(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            await store.put_document(
                "proj-1", "latex", _citation_doc_elements(), source="paper.tex",
            )
            # Resolver returns None for everything.
            summary = await store.resolve_zotero_edges(
                "proj-1", resolver=_stub_resolver({}),
            )
            assert summary == {"resolved": 0, "unresolved": 3, "cross_doc_linked": 0}
            all_edges = await store.get_edges("proj-1")
            assert [e for e in all_edges if e["target_kind"] == "zotero_item"] == []
        finally:
            await store.close()

    asyncio.run(_run())


def test_resolve_zotero_edges_is_idempotent(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            await store.put_document(
                "proj-1", "latex", _citation_doc_elements(), source="paper.tex",
            )
            resolver = _stub_resolver({
                "knuth1984": {"zotero_key": "ZK1", "doi": "10.1/knuth", "title": "T"},
                "lamport1994": {"zotero_key": "ZK2", "doi": "10.2/lamport", "title": "L"},
            })
            first = await store.resolve_zotero_edges("proj-1", resolver=resolver)
            assert first["resolved"] == 2

            # Second run: already-linked markers are filtered out by the NOT-EXISTS
            # guard, so it resolves nothing new and creates no duplicate edges.
            second = await store.resolve_zotero_edges("proj-1", resolver=resolver)
            assert second == {"resolved": 0, "unresolved": 1, "cross_doc_linked": 0}

            zotero_edges = [
                e for e in await store.get_edges("proj-1")
                if e["target_kind"] == "zotero_item"
            ]
            assert len(zotero_edges) == 2  # not 4 — no duplicates
        finally:
            await store.close()

    asyncio.run(_run())


def test_resolve_zotero_edges_max_items_caps_pass(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            await store.put_document(
                "proj-1", "latex", _citation_doc_elements(), source="paper.tex",
            )
            # All three refs resolve, but max_items=1 attempts only the first.
            resolver = _stub_resolver({
                "knuth1984": {"zotero_key": "ZK1", "doi": "10.1/k", "title": "T"},
                "missing_key": {"zotero_key": "ZK3", "doi": "10.3/m", "title": "M"},
                "lamport1994": {"zotero_key": "ZK2", "doi": "10.2/l", "title": "L"},
            })
            summary = await store.resolve_zotero_edges(
                "proj-1", resolver=resolver, max_items=1,
            )
            # Only one marker attempted (and resolved) this pass.
            assert summary["resolved"] == 1
            assert summary["unresolved"] == 0
            zotero_edges = [
                e for e in await store.get_edges("proj-1")
                if e["target_kind"] == "zotero_item"
            ]
            assert len(zotero_edges) == 1

            # A second unbounded pass picks up the remaining two.
            summary2 = await store.resolve_zotero_edges("proj-1", resolver=resolver)
            assert summary2["resolved"] == 2
        finally:
            await store.close()

    asyncio.run(_run())


def test_resolve_zotero_edges_uses_zotero_key_ref_when_no_doi(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            await store.put_document(
                "proj-1", "latex", _citation_doc_elements(), source="paper.tex",
            )
            # Resolved item carries no DOI -> target_ref falls back to zotero:<key>.
            resolver = _stub_resolver({
                "knuth1984": {"zotero_key": "NODOIKEY", "doi": None, "title": "T"},
            })
            summary = await store.resolve_zotero_edges("proj-1", resolver=resolver)
            assert summary["resolved"] == 1
            assert summary["cross_doc_linked"] == 0
            zotero_edges = [
                e for e in await store.get_edges("proj-1")
                if e["target_kind"] == "zotero_item"
            ]
            assert len(zotero_edges) == 1
            assert zotero_edges[0]["target_ref"] == "zotero:NODOIKEY"
        finally:
            await store.close()

    asyncio.run(_run())


def test_resolve_zotero_edges_guards_a_raising_resolver(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            await store.put_document(
                "proj-1", "latex", _citation_doc_elements(), source="paper.tex",
            )

            async def _boom(ref, **_kwargs):
                raise RuntimeError("resolver exploded")

            # A resolver that always raises: every marker is counted unresolved,
            # the pass does not abort.
            summary = await store.resolve_zotero_edges("proj-1", resolver=_boom)
            assert summary == {"resolved": 0, "unresolved": 3, "cross_doc_linked": 0}
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# cross_doc_linked — target_document_id set when the DOI matches another doc
# ---------------------------------------------------------------------------

def test_cross_doc_linked_via_source_doi_substring(tmp_path):
    """When the resolved DOI appears in ANOTHER stored doc's source, the edge's
    target_document_id points at that document (the cross-document hop)."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            # The citing paper.
            citing = await store.put_document(
                "proj-1", "latex", _citation_doc_elements(), source="paper.tex",
            )
            # A SECOND ingested doc whose source URL carries the cited DOI. This is
            # what _find_document_for_doi matches (LOWER(source) LIKE %doi%).
            cited = await store.put_document(
                "proj-1", "pdf",
                [{"ordinal": 0, "level": 1, "kind": "heading", "text": "Cited paper",
                  "ref": None, "parent_ordinal": None}],
                source="https://doi.org/10.1/KNUTH",
                title="The TeXbook",
            )

            resolver = _stub_resolver({
                "knuth1984": {"zotero_key": "ZK1", "doi": "10.1/knuth", "title": "The TeXbook"},
            })
            summary = await store.resolve_zotero_edges("proj-1", resolver=resolver)
            assert summary["resolved"] == 1
            assert summary["cross_doc_linked"] == 1

            zotero_edges = [
                e for e in await store.get_edges("proj-1", document_id=citing["id"])
                if e["target_kind"] == "zotero_item"
            ]
            assert len(zotero_edges) == 1
            # The DOI matched the cited doc's source (case-insensitive substring).
            assert zotero_edges[0]["target_document_id"] == cited["id"]
            assert zotero_edges[0]["target_ref"] == "10.1/knuth"
        finally:
            await store.close()

    asyncio.run(_run())


def test_cross_doc_linked_via_title_fallback(tmp_path):
    """When the DOI is not in any source, _find_document_for_doi falls back to a
    case-insensitive title match against another stored doc."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            citing = await store.put_document(
                "proj-1", "latex", _citation_doc_elements(), source="paper.tex",
            )
            # Cited doc: source does NOT carry the DOI, but its title matches the
            # resolved Zotero item's title exactly (case-insensitive).
            cited = await store.put_document(
                "proj-1", "pdf",
                [{"ordinal": 0, "level": 1, "kind": "heading", "text": "H",
                  "ref": None, "parent_ordinal": None}],
                source="local/knuth.pdf",
                title="The TeXbook",
            )
            resolver = _stub_resolver({
                "knuth1984": {"zotero_key": "ZK1", "doi": "10.1/knuth", "title": "THE texbook"},
            })
            summary = await store.resolve_zotero_edges("proj-1", resolver=resolver)
            assert summary["cross_doc_linked"] == 1
            zotero_edges = [
                e for e in await store.get_edges("proj-1", document_id=citing["id"])
                if e["target_kind"] == "zotero_item"
            ]
            assert zotero_edges[0]["target_document_id"] == cited["id"]
        finally:
            await store.close()

    asyncio.run(_run())


def test_no_cross_doc_link_when_doi_matches_nothing(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            await store.put_document(
                "proj-1", "latex", _citation_doc_elements(), source="paper.tex",
            )
            resolver = _stub_resolver({
                "knuth1984": {"zotero_key": "ZK1", "doi": "10.9/unmatched", "title": "Nowhere"},
            })
            summary = await store.resolve_zotero_edges("proj-1", resolver=resolver)
            assert summary["resolved"] == 1
            assert summary["cross_doc_linked"] == 0
            zotero_edges = [
                e for e in await store.get_edges("proj-1")
                if e["target_kind"] == "zotero_item"
            ]
            assert zotero_edges[0]["target_document_id"] is None
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# get_citation_graph — markers carry both bibentry AND zotero_item edges
# ---------------------------------------------------------------------------

def test_citation_graph_merges_bibentry_and_zotero_edges(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            await store.put_document(
                "proj-1", "latex", _citation_doc_elements(), source="paper.tex",
            )
            resolver = _stub_resolver({
                "knuth1984": {"zotero_key": "ZK1", "doi": "10.1/knuth", "title": "T"},
            })
            await store.resolve_zotero_edges("proj-1", resolver=resolver)

            graph = await store.get_citation_graph("proj-1", source="paper.tex")
            markers = {m["ref"]: m for m in graph["markers"]}
            assert set(markers) == {"knuth1984", "missing_key", "lamport1994"}

            # knuth1984 has BOTH a bibentry edge (intra-doc, from put) and a
            # zotero_item edge (cross-doc, from resolve), bibentry ordered first.
            knuth_edges = markers["knuth1984"]["edges"]
            kinds = [e["target_kind"] for e in knuth_edges]
            assert kinds == ["bibentry", "zotero_item"]

            # lamport1994 has only the intra-doc bibentry edge (not resolved).
            lamport_kinds = [e["target_kind"] for e in markers["lamport1994"]["edges"]]
            assert lamport_kinds == ["bibentry"]

            # missing_key is dangling — no edges at all.
            assert markers["missing_key"]["edges"] == []
        finally:
            await store.close()

    asyncio.run(_run())


def test_citation_graph_unknown_source_is_empty(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            await store.put_document(
                "proj-1", "latex", _citation_doc_elements(), source="paper.tex",
            )
            graph = await store.get_citation_graph("proj-1", source="nope.tex")
            assert graph == {"markers": []}
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# MCP tools — get_citation_edges + resolve_citations
# ---------------------------------------------------------------------------

def _seed_store_via_env(tmp_path, monkeypatch):
    """Point the MCP tools' tier-resolved store at a known sidecar via the env
    override, returning the resolved sidecar path so the test can pre-seed it."""
    sidecar = str(tmp_path / "mcp_doc_structure.db")
    monkeypatch.setenv("MERIDIAN_DOC_STORE_URL", sidecar)
    doc_store._reset_doc_store_cache()
    return sidecar


def test_mcp_get_citation_edges_returns_markers_and_edges(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "cite-graph")
            pid = proj["id"]

            # Seed structure directly into the same sidecar the tool will resolve.
            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(
                pid, "latex", _citation_doc_elements(), source="paper.tex",
            )

            res = await mh._dispatch_mcp_tool(
                "get_citation_edges",
                {"project_id": pid, "source": "paper.tex"},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["project_id"] == pid
            refs = {m["ref"] for m in res["markers"]}
            assert refs == {"knuth1984", "missing_key", "lamport1994"}
            # Intra-doc bibentry edges are present on the resolvable markers.
            by_ref = {m["ref"]: m for m in res["markers"]}
            assert any(
                e["target_kind"] == "bibentry" for e in by_ref["knuth1984"]["edges"]
            )
            assert by_ref["missing_key"]["edges"] == []
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_get_citation_edges_requires_project_id(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            res = await mh._dispatch_mcp_tool(
                "get_citation_edges", {}, db, str(tmp_path),
            )
            assert res.get("error")
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_resolve_citations_invokes_resolver_and_returns_summary(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh
        from meridian import zotero_client as zc

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "resolve-cites")
            pid = proj["id"]

            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(
                pid, "latex", _citation_doc_elements(), source="paper.tex",
            )

            # Stub the module-level resolver the tool uses (resolve_zotero_edges
            # defaults to zotero_client.resolve_citation_ref). NO network.
            calls = []

            async def _fake_resolve(ref, **_kwargs):
                calls.append(ref)
                mapping = {
                    "knuth1984": {"zotero_key": "ZK1", "doi": "10.1/knuth", "title": "T"},
                    "lamport1994": {"zotero_key": "ZK2", "doi": "10.2/lamport", "title": "L"},
                }
                return mapping.get(ref)

            monkeypatch.setattr(zc, "resolve_citation_ref", _fake_resolve)
            # resolve_zotero_edges binds its `resolver` default (a keyword-only
            # arg) at def-time to zotero_client.resolve_citation_ref, so a plain
            # module-attr patch does NOT rebind it — the tool calls
            # resolve_zotero_edges WITHOUT a resolver arg. Patch the captured
            # default directly in __kwdefaults__ (monkeypatch restores it after).
            _fn = doc_store.DocStructureStore.resolve_zotero_edges
            _patched_kwdefaults = dict(_fn.__kwdefaults__)
            _patched_kwdefaults["resolver"] = _fake_resolve
            monkeypatch.setattr(_fn, "__kwdefaults__", _patched_kwdefaults)

            res = await mh._dispatch_mcp_tool(
                "resolve_citations", {"project_id": pid}, db, str(tmp_path),
            )
            assert "error" not in res
            assert res["project_id"] == pid
            assert res["resolved"] == 2
            assert res["unresolved"] == 1
            assert res["cross_doc_linked"] == 0
            # The stubbed resolver was actually invoked for each pending marker.
            assert set(calls) == {"knuth1984", "missing_key", "lamport1994"}

            # The zotero_item edges are now readable via get_citation_edges.
            edges_res = await mh._dispatch_mcp_tool(
                "get_citation_edges",
                {"project_id": pid, "source": "paper.tex"},
                db, str(tmp_path),
            )
            by_ref = {m["ref"]: m for m in edges_res["markers"]}
            assert any(
                e["target_kind"] == "zotero_item"
                for e in by_ref["knuth1984"]["edges"]
            )
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_resolve_citations_requires_project_id(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            res = await mh._dispatch_mcp_tool(
                "resolve_citations", {}, db, str(tmp_path),
            )
            assert res.get("error")
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_doi_bounded_in_prevents_prefix_mislink():
    """fefb596a — the cross-doc hop (_find_document_for_doi) must not let a DOI that
    is a *prefix* of a longer DOI in some source capture the wrong document.
    _doi_bounded_in accepts only a whole-DOI boundary (end-of-string or a
    non-DOI-continuation char)."""
    from meridian.doc_store import _doi_bounded_in
    # a prefix of a longer DOI present in the source -> NOT a bounded match
    assert _doi_bounded_in("https://doi.org/10.1/knuth-extended", "10.1/knuth") is False
    # exact DOI at the end of a source URL -> match
    assert _doi_bounded_in("https://doi.org/10.1/knuth", "10.1/knuth") is True
    # DOI followed by a clear delimiter (?, #) -> match
    assert _doi_bounded_in("file:10.1/knuth?ref=x", "10.1/knuth") is True
    assert _doi_bounded_in("10.1/knuth#sec2", "10.1/knuth") is True
    # bare whole DOI matches; absent / empty -> no match
    assert _doi_bounded_in("10.1/knuth", "10.1/knuth") is True
    assert _doi_bounded_in("something else entirely", "10.1/knuth") is False
    assert _doi_bounded_in("10.1/knuth", "") is False
