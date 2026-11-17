"""Citation marker round trip: marker, pending, apply, graph.

Offline and sqlite-only. The doc-structure store is a local SQLite sidecar in
``tmp_path`` (via ``MERIDIAN_DOC_STORE_URL``), the workstation's Zotero lookup
is a fake resolver, and socket creation is blocked so nothing can reach Zotero
or any other host. Every hosted step goes through the real MCP dispatch.
"""
from __future__ import annotations

import asyncio
import socket
from typing import Any

import pytest

from meridian import db as db_module
from meridian import doc_store


_ZOTERO_LIBRARY: dict[str, dict[str, Any]] = {
    "smith2021": {
        "zotero_key": "SMITH221",
        "doi": "10.1000/smith2021",
        "title": "A paper that resolves",
        "version": 7,
        "collection_keys": ["COLL0001"],
    },
}


class _FakeZoteroResolver:
    """Stands in for ``zotero_client.resolve_citation_ref`` over a fixed library."""

    def __init__(self, library: dict[str, dict[str, Any]]) -> None:
        self._library = library
        self.calls: list[str] = []

    async def __call__(self, ref: str) -> dict[str, Any] | None:
        self.calls.append(ref)
        item = self._library.get(ref)
        return dict(item) if item is not None else None


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _blocked(*_args, **_kwargs):
        raise AssertionError("citation round trip must not open a network connection")

    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)


@pytest.fixture
def sqlite_sidecar(tmp_path, monkeypatch):
    sidecar = str(tmp_path / "doc_structure.db")
    monkeypatch.setenv("MERIDIAN_DOC_STORE_URL", sidecar)
    doc_store._reset_doc_store_cache()
    yield sidecar
    doc_store._reset_doc_store_cache()


def _manuscript_elements() -> list[dict[str, Any]]:
    """A short LaTeX outline with one resolvable and one unresolvable citation."""
    return [
        {"ordinal": 0, "level": 2, "kind": "section", "text": "Background",
         "ref": None, "parent_ordinal": None},
        {"ordinal": 1, "level": None, "kind": "citation",
         "text": r"\cite{smith2021}", "ref": "smith2021", "parent_ordinal": 0},
        {"ordinal": 2, "level": None, "kind": "citation",
         "text": r"\cite{ghost2019}", "ref": "ghost2019", "parent_ordinal": 0},
    ]


async def _call(name: str, args: dict[str, Any], db: Any, data_dir: str) -> Any:
    # Import through meridian.server, as the other MCP dispatch tests do: importing
    # meridian.mcp.handler first trips a circular import.
    from meridian import server as mcp_server  # noqa: PLC0415

    return await mcp_server._dispatch_mcp_tool(name, args, db, data_dir)


async def _store(tmp_path, sidecar: str) -> doc_store.DocStructureStore:
    return await doc_store.open_doc_store_for(
        plan=None, hosted=False, data_dir=str(tmp_path),
        tenant_pg_url=None, override_url=sidecar,
    )


async def _seeded_project(db: Any, tmp_path, sidecar: str) -> str:
    project = await db_module.create_project(db, "citation-roundtrip")
    pid = project["id"]
    store = await _store(tmp_path, sidecar)
    await store.put_document(pid, "latex", _manuscript_elements(), source="paper.tex")
    return pid


async def _resolve_and_apply(
    db: Any, data_dir: str, pid: str, resolver: _FakeZoteroResolver,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Workstation side: read pending markers, resolve them, apply the results."""
    pending = await _call("get_pending_zotero_citations", {"project_id": pid}, db, data_dir)
    assert "error" not in pending
    resolutions: list[dict[str, Any]] = []
    for marker in pending["markers"]:
        item = await resolver(marker["ref"])
        if item is None:
            continue
        resolutions.append({
            "element_id": marker["id"],
            "ref": marker["ref"],
            "zotero_key": item["zotero_key"],
            "doi": item["doi"],
            "title": item["title"],
            "version": item["version"],
            "collection_keys": item["collection_keys"],
        })
    summary = await _call(
        "apply_zotero_citation_edges",
        {"project_id": pid, "selected_collection_keys": [], "resolutions": resolutions},
        db,
        data_dir,
    )
    return pending, resolutions, summary


async def _graph_by_ref(db: Any, data_dir: str, pid: str) -> dict[str, dict[str, Any]]:
    result = await _call("get_citation_edges", {"project_id": pid}, db, data_dir)
    assert "error" not in result
    return {marker["ref"]: marker for marker in result["markers"]}


def _zotero_edges(marker: dict[str, Any]) -> list[dict[str, Any]]:
    return [edge for edge in marker["edges"] if edge["target_kind"] == "zotero_item"]


def test_resolved_marker_appears_in_citation_edges_after_apply(tmp_path, sqlite_sidecar):
    resolver = _FakeZoteroResolver(_ZOTERO_LIBRARY)

    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            pid = await _seeded_project(db, tmp_path, sqlite_sidecar)
            pending, _resolutions, summary = await _resolve_and_apply(
                db, str(tmp_path), pid, resolver,
            )
            assert {marker["ref"] for marker in pending["markers"]} == {"smith2021", "ghost2019"}
            assert summary["applied"] == 1
            assert summary["stale"] == 0
            assert summary["rejected"] == 0

            graph = await _graph_by_ref(db, str(tmp_path), pid)
            marker = graph["smith2021"]
            edges = _zotero_edges(marker)
            assert len(edges) == 1
            assert edges[0]["source_element_id"] == marker["element_id"]
            assert edges[0]["target_ref"] == "10.1000/smith2021"
        finally:
            await doc_store.close_all_doc_stores()
            await db.close()

    asyncio.run(_run())


def test_unresolved_marker_stays_in_pending_after_apply(tmp_path, sqlite_sidecar):
    resolver = _FakeZoteroResolver(_ZOTERO_LIBRARY)

    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            pid = await _seeded_project(db, tmp_path, sqlite_sidecar)
            _pending, _resolutions, summary = await _resolve_and_apply(
                db, str(tmp_path), pid, resolver,
            )
            assert summary["applied"] == 1

            pending_after = await _call(
                "get_pending_zotero_citations", {"project_id": pid}, db, str(tmp_path),
            )
            assert "error" not in pending_after
            assert [marker["ref"] for marker in pending_after["markers"]] == ["ghost2019"]

            graph = await _graph_by_ref(db, str(tmp_path), pid)
            assert _zotero_edges(graph["ghost2019"]) == []
        finally:
            await doc_store.close_all_doc_stores()
            await db.close()

    asyncio.run(_run())


def test_second_apply_of_same_resolutions_writes_no_new_edge(tmp_path, sqlite_sidecar):
    resolver = _FakeZoteroResolver(_ZOTERO_LIBRARY)

    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            pid = await _seeded_project(db, tmp_path, sqlite_sidecar)
            _pending, resolutions, first = await _resolve_and_apply(
                db, str(tmp_path), pid, resolver,
            )
            assert first["applied"] == 1

            store = await _store(tmp_path, sqlite_sidecar)
            edges_before = await store.get_edges(pid)
            assert len(edges_before) == 1

            replay = await _call(
                "apply_zotero_citation_edges",
                {"project_id": pid, "selected_collection_keys": [], "resolutions": resolutions},
                db,
                str(tmp_path),
            )
            assert "error" not in replay
            assert replay["applied"] == 0
            assert replay["already_resolved"] == 1

            edges_after = await store.get_edges(pid)
            assert [edge["id"] for edge in edges_after] == [edge["id"] for edge in edges_before]
        finally:
            await doc_store.close_all_doc_stores()
            await db.close()

    asyncio.run(_run())


def test_round_trip_runs_on_sqlite_sidecar_with_fake_resolver_only(tmp_path, sqlite_sidecar):
    resolver = _FakeZoteroResolver(_ZOTERO_LIBRARY)

    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            pid = await _seeded_project(db, tmp_path, sqlite_sidecar)
            _pending, _resolutions, summary = await _resolve_and_apply(
                db, str(tmp_path), pid, resolver,
            )
            assert summary["applied"] == 1
            # The only resolver the workstation side consulted is the fake.
            assert sorted(resolver.calls) == ["ghost2019", "smith2021"]
            # The doc-structure store lives in the local SQLite sidecar file.
            assert (tmp_path / "doc_structure.db").is_file()
        finally:
            await doc_store.close_all_doc_stores()
            await db.close()

    asyncio.run(_run())
