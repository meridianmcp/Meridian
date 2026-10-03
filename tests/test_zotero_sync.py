"""Focused coverage for the workstation-local Zotero sync boundary."""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest

from meridian import db as db_module
from meridian import doc_store, mcp_tools, zotero_client, zotero_sync
from meridian.mcp.handlers import zotero_sync as hosted_zotero_sync


def _run(coro):
    return asyncio.run(coro)


class _Response:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


class _Client:
    def __init__(self, body):
        self.body = body
        self.requests = []

    async def get(self, url, params=None):
        self.requests.append((url, params or {}))
        return _Response(self.body)


def test_sync_tools_are_exposed_with_local_data_excluded_from_write_schema():
    tools = {tool["name"]: tool for tool in mcp_tools._MCP_TOOLS_LIST}
    assert "get_pending_zotero_citations" in tools
    assert "apply_zotero_citation_edges" in tools
    assert "get_pending_zotero_citations" in mcp_tools._READ_ONLY_TOOLS
    assert "apply_zotero_citation_edges" not in mcp_tools._READ_ONLY_TOOLS

    apply_properties = tools["apply_zotero_citation_edges"]["inputSchema"]["properties"]
    resolution_properties = apply_properties["resolutions"]["items"]["properties"]
    assert set(resolution_properties) == {
        "element_id", "ref", "zotero_key", "doi", "title", "version", "collection_keys",
    }


def test_source_checkout_clients_and_outputs_registry_load():
    assert callable(zotero_sync._load_hosted_mcp_call())
    registry = zotero_sync._load_outputs_registry()
    assert registry is not None
    assert all(callable(getattr(registry, name, None)) for name in (
        "register_artifact", "bind_source_edge", "verify_artifact_hash",
    ))


def test_attachment_path_resolution_rejects_missing_and_traversal(tmp_path):
    assert zotero_sync._safe_attachment_path({}, tmp_path) is None
    assert zotero_sync._safe_attachment_path({"path": "storage:paper.pdf"}, None) is None
    assert zotero_sync._safe_attachment_path({
        "path": "storage:paper.pdf", "zotero_key": "bad",
    }, tmp_path) is None
    assert zotero_sync._safe_attachment_path({
        "path": "storage:../escape.pdf", "zotero_key": "ATTACH01",
    }, tmp_path) is None
    assert zotero_sync._safe_attachment_path({
        "path": "relative/paper.pdf", "zotero_key": "ATTACH01",
    }, tmp_path) is None
    assert zotero_sync._safe_attachment_path({
        "path": "storage:missing.pdf", "zotero_key": "ATTACH01",
    }, tmp_path) is None

    linked = tmp_path / "linked.pdf"
    linked.write_bytes(b"linked attachment")
    assert zotero_sync._safe_attachment_path({"path": str(linked)}, tmp_path) == linked


def test_attachment_registration_fails_closed_on_bad_identity_or_registry_error(tmp_path):
    attachment_file = tmp_path / "paper.pdf"
    attachment_file.write_bytes(b"paper")
    attachment = {
        "zotero_key": "ATTACH01", "parent_key": "PARENT01", "version": 3,
    }
    assert not zotero_sync._register_attachment(
        {**attachment, "zotero_key": "bad"}, attachment_file, tmp_path, object(),
    )

    class _BrokenRegistry:
        def register_artifact(self, *_args, **_kwargs):
            raise OSError("local ledger unavailable")

    assert not zotero_sync._register_attachment(
        attachment, attachment_file, tmp_path, _BrokenRegistry(),
    )


def _raw_item(key, data, *, version=12):
    return {
        "key": key,
        "version": version,
        "library": {"type": "user", "id": 0},
        "data": data,
    }


def test_local_item_and_attachment_lookups_return_metadata_without_bytes():
    parent_client = _Client(_raw_item(
        "PARENT01",
        {
            "itemType": "journalArticle",
            "title": "Local paper",
            "DOI": "10.1000/example",
            "collections": ["coll0001", "INVALID"],
        },
    ))
    parent = _run(zotero_client.fetch_zotero_item_details("PARENT01", client=parent_client))
    assert parent["version"] == 12
    assert parent["collection_keys"] == ["COLL0001"]
    assert parent_client.requests[0][0].endswith("/users/0/items/PARENT01")

    child_client = _Client([_raw_item(
        "ATTACH01",
        {
            "itemType": "attachment",
            "parentItem": "PARENT01",
            "path": "storage:paper.pdf",
            "filename": "paper.pdf",
            "contentType": "application/pdf",
            "content": "must-not-be-returned",
        },
    )])
    attachments = _run(zotero_client.list_zotero_item_attachments(
        "PARENT01", client=child_client,
    ))
    assert attachments == [{
        "zotero_key": "ATTACH01",
        "parent_key": "PARENT01",
        "version": 12,
        "path": "storage:paper.pdf",
        "filename": "paper.pdf",
        "content_type": "application/pdf",
    }]
    assert "must-not-be-returned" not in json.dumps(attachments)

    invalid_client = _Client([])
    assert _run(zotero_client.list_zotero_item_attachments("bad", client=invalid_client)) == []
    assert invalid_client.requests == []


class _Registry:
    def __init__(self):
        self.registered = []
        self.bound = []
        self.verified = []

    def register_artifact(self, outputs_dir, kind, **kwargs):
        self.registered.append((outputs_dir, kind, kwargs))
        return {"artifact_id": "artifact-1"}

    def bind_source_edge(self, outputs_dir, artifact_id, source_locator, **kwargs):
        self.bound.append((outputs_dir, artifact_id, source_locator, kwargs))
        return {"ok": True}

    def verify_artifact_hash(self, outputs_dir, artifact_id, *, path):
        self.verified.append((outputs_dir, artifact_id, path))
        return {"verified": True}


def _sync_fakes():
    calls = []

    def call_tool(name, params, *, base_url=None):
        calls.append((name, params, base_url))
        if name == "get_pending_zotero_citations":
            return {
                "markers": [{"id": "marker-1", "ref": "doe2020"}],
                "has_more": False,
            }
        return {"applied": 1, "cross_doc_linked": 0, "stale": 0,
                "already_resolved": 0, "rejected": 0}

    async def resolver(_ref):
        return {
            "zotero_key": "ITEMKEY1",
            "doi": "10.1000/example",
            "title": "Local paper",
        }

    async def item_lookup(_key):
        return {
            "version": 12,
            "collection_keys": ["COLL0001"],
            "title": "Local paper",
        }

    async def attachments(_parent_key):
        return [{
            "zotero_key": "ATTACH01",
            "parent_key": "ITEMKEY1",
            "version": 3,
            "path": "storage:paper.pdf",
            "filename": "private-local-name.pdf",
            "content_type": "application/pdf",
        }]

    return calls, call_tool, resolver, item_lookup, attachments


def test_one_shot_sync_keeps_keys_paths_and_attachment_bytes_local(tmp_path, monkeypatch):
    monkeypatch.setattr(zotero_sync, "get_zotero_collection_keys", lambda: ["COLL0001"])
    calls, hosted_call, resolver, item_lookup, attachments = _sync_fakes()
    attachment_path = tmp_path / "zotero" / "storage" / "ATTACH01" / "paper.pdf"
    attachment_path.parent.mkdir(parents=True)
    attachment_path.write_bytes(b"private PDF payload")
    registry = _Registry()

    summary = _run(zotero_sync.sync_zotero_citations(
        "project-1",
        server="https://meridian.example",
        zotero_data_dir=tmp_path / "zotero",
        outputs_dir=tmp_path / "outputs",
        call_tool=hosted_call,
        resolver=resolver,
        item_lookup=item_lookup,
        attachment_lookup=attachments,
        registry=registry,
    ))

    assert [entry[0] for entry in calls] == [
        "get_pending_zotero_citations", "apply_zotero_citation_edges",
    ]
    assert calls[0][2] == "https://meridian.example"
    assert calls[1][1]["selected_collection_keys"] == ["COLL0001"]
    assert summary["applied"] == 1
    assert summary["attachments_registered"] == 1
    assert summary["attachments_unverified"] == 0

    remote_payload = json.dumps(calls[1][1])
    assert str(attachment_path) not in remote_payload
    assert "private-local-name.pdf" not in remote_payload
    assert "private PDF payload" not in remote_payload
    assert "storage:paper.pdf" not in remote_payload
    assert registry.registered[0][2]["expected_sha256"]
    assert registry.bound[0][2].endswith("/item/ITEMKEY1/attachment/ATTACH01")
    assert registry.verified[0][2] == str(attachment_path)


def test_dry_run_reads_locally_but_does_not_register_or_write(tmp_path, monkeypatch):
    monkeypatch.setattr(zotero_sync, "get_zotero_collection_keys", lambda: ["COLL0001"])
    calls, hosted_call, resolver, item_lookup, attachments = _sync_fakes()
    attachment_path = tmp_path / "zotero" / "storage" / "ATTACH01" / "paper.pdf"
    attachment_path.parent.mkdir(parents=True)
    attachment_path.write_bytes(b"private PDF payload")
    registry = _Registry()

    summary = _run(zotero_sync.sync_zotero_citations(
        "project-1",
        dry_run=True,
        zotero_data_dir=tmp_path / "zotero",
        outputs_dir=tmp_path / "outputs",
        call_tool=hosted_call,
        resolver=resolver,
        item_lookup=item_lookup,
        attachment_lookup=attachments,
        registry=registry,
    ))

    assert [entry[0] for entry in calls] == ["get_pending_zotero_citations"]
    assert summary["dry_run"] is True
    assert summary["resolved_locally"] == 1
    assert summary["attachments_unverified"] == 1
    assert registry.registered == []


def test_selected_collection_scope_applies_to_direct_item_refs(monkeypatch):
    monkeypatch.setattr(zotero_sync, "get_zotero_collection_keys", lambda: ["COLL0001"])
    calls = []

    def hosted_call(name, params, *, base_url=None):
        calls.append(name)
        return {"markers": [{"id": "marker-1", "ref": "zotero:ITEMKEY1"}], "has_more": False}

    async def resolver(_ref):
        return {"zotero_key": "ITEMKEY1", "title": "Out of scope"}

    async def item_lookup(_key):
        return {"version": 1, "collection_keys": ["OTHER001"]}

    summary = _run(zotero_sync.sync_zotero_citations(
        "project-1", call_tool=hosted_call, resolver=resolver, item_lookup=item_lookup,
    ))
    assert summary["unresolved"] == 1
    assert calls == ["get_pending_zotero_citations"]


def test_sync_skips_bad_markers_and_reports_missing_hosted_marker_list():
    def hosted_call(_name, _params, *, base_url=None):
        return {"markers": [
            None,
            {"id": "too-long" * 30, "ref": "doe2020"},
            {"id": "marker-3", "ref": "zotero:bad"},
        ], "has_more": False}

    summary = _run(zotero_sync.sync_zotero_citations("project-1", call_tool=hosted_call))
    assert summary["processed"] == 3
    assert summary["unresolved"] == 3

    with pytest.raises(RuntimeError, match="citation marker list"):
        _run(zotero_sync.sync_zotero_citations(
            "project-1", call_tool=lambda *_args, **_kwargs: {"error": "unavailable"},
        ))


def test_sync_rejects_invalid_project_and_batch_sizes():
    with pytest.raises(ValueError, match="project_id"):
        _run(zotero_sync.sync_zotero_citations(" "))
    for count in (0, 501, True):
        with pytest.raises(ValueError, match="max_items"):
            _run(zotero_sync.sync_zotero_citations("project-1", max_items=count))


def test_sync_handles_attachment_item_and_missing_attachment_list(monkeypatch, tmp_path):
    monkeypatch.setattr(zotero_sync, "get_zotero_collection_keys", lambda: [])
    attachment_path = tmp_path / "zotero" / "storage" / "ATTACH01" / "paper.pdf"
    attachment_path.parent.mkdir(parents=True)
    attachment_path.write_bytes(b"local bytes")
    calls = []
    markers = [
        {"id": "marker-1", "ref": "citekey"},
        {"id": "marker-2", "ref": "other"},
    ]

    def hosted_call(name, params, *, base_url=None):
        calls.append(name)
        if name == "get_pending_zotero_citations":
            return {"markers": markers, "has_more": True}
        return {"applied": 2}

    async def resolver(ref):
        if ref == "citekey":
            return {"zotero_key": "ATTACH01"}
        return {"zotero_key": "ITEMKEY1"}

    async def item_lookup(key):
        if key == "ATTACH01":
            return {
                "zotero_key": "ATTACH01", "item_type": "attachment",
                "parent_key": "PARENT01", "version": 9,
                "collection_keys": [], "title": "Attachment parent title",
                "doi": "10.1000/fallback", "path": "storage:paper.pdf",
                "filename": "local.pdf", "content_type": "application/pdf",
            }
        return {"version": 4, "collection_keys": [], "title": "Second item"}

    async def attachments(parent_key):
        return None if parent_key == "ITEMKEY1" else []

    summary = _run(zotero_sync.sync_zotero_citations(
        "project-1", max_items=2, zotero_data_dir=tmp_path / "zotero",
        outputs_dir=tmp_path / "outputs", call_tool=hosted_call,
        resolver=resolver, item_lookup=item_lookup, attachment_lookup=attachments,
        registry=_Registry(),
    ))
    assert summary["remaining_candidates"] is True
    assert summary["attachments_seen"] == 1
    assert summary["attachments_registered"] == 1
    assert calls == ["get_pending_zotero_citations", "apply_zotero_citation_edges"]


def test_authenticated_client_helper_uses_plugin_base_call(monkeypatch):
    observed = {}

    def authenticated_call(name, params, *, base_url=None):
        observed.update(name=name, params=params, base_url=base_url)
        return {"ok": True}

    monkeypatch.setattr(zotero_sync, "_load_hosted_mcp_call", lambda: authenticated_call)
    monkeypatch.setenv("MERIDIAN_API_KEY", "local-test-secret")
    result = zotero_sync._call_hosted_tool(
        "apply_zotero_citation_edges", {"project_id": "project-1"},
        base_url="https://meridian.example",
    )
    assert result == {"ok": True}
    assert observed["base_url"] == "https://meridian.example"
    assert observed["params"] == {"project_id": "project-1"}
    assert "local-test-secret" not in json.dumps(observed["params"])


def test_cli_prints_json_and_reports_sync_errors(monkeypatch, capsys):
    def success_run(coro):
        coro.close()
        return {"project_id": "project-1", "applied": 1}

    monkeypatch.setattr(zotero_sync.asyncio, "run", success_run)
    assert zotero_sync.cli_main(["sync", "--project-id", "project-1"]) == 0
    assert json.loads(capsys.readouterr().out) == {"project_id": "project-1", "applied": 1}

    def failed_run(coro):
        coro.close()
        raise RuntimeError("hosted service unavailable")

    monkeypatch.setattr(zotero_sync.asyncio, "run", failed_run)
    assert zotero_sync.cli_main(["sync", "--project-id", "project-1"]) == 1
    captured = capsys.readouterr()
    assert "zotero sync failed" in captured.err
    assert "hosted service unavailable" in captured.err


def test_doc_store_batch_is_project_bound_bounded_and_idempotent(tmp_path):
    async def scenario():
        connection = await db_module.init_db(str(tmp_path / "doc_structure.db"))
        store = doc_store.DocStructureStore(connection)
        await store.ensure_schema()
        try:
            await store.put_document(
                "project-1",
                "latex",
                [{
                    "ordinal": 0,
                    "level": None,
                    "kind": "citation",
                    "text": r"\cite{doe2020}",
                    "ref": "doe2020",
                    "parent_ordinal": None,
                }],
                source="paper.tex",
            )
            pending = await store.get_pending_zotero_citations("project-1", max_items=1)
            assert len(pending["markers"]) == 1
            assert pending["has_more"] is False
            marker = pending["markers"][0]
            result = {
                "element_id": marker["id"],
                "ref": marker["ref"],
                "zotero_key": "ITEMKEY1",
                "doi": "10.1000/example",
                "title": "Paper",
            }
            applied = await store.apply_resolved_zotero_edges("project-1", [result])
            assert applied["applied"] == 1
            assert applied["stale"] == 0

            replay = await store.apply_resolved_zotero_edges("project-1", [result])
            assert replay["already_resolved"] == 1

            wrong_project = await store.apply_resolved_zotero_edges("project-2", [result])
            assert wrong_project["applied"] == 0
            assert wrong_project["stale"] == 1
            assert len(await store.get_edges("project-1")) == 1
            assert await store.get_edges("project-2") == []
        finally:
            await connection.close()

    _run(scenario())


def test_hosted_write_rejects_local_paths_and_binds_project(monkeypatch):
    async def scenario():
        result = await hosted_zotero_sync.handle_apply_zotero_citation_edges(
            {
                "project_id": "project-1",
                "resolutions": [{
                    "element_id": "marker-1",
                    "ref": "doe2020",
                    "zotero_key": "ITEMKEY1",
                    "path": "C:/Users/me/secret.pdf",
                }],
            },
            None,
            "",
            None,
            None,
        )
        assert "marker and Zotero identity fields" in result["error"]

        class _Store:
            def __init__(self):
                self.applied = None

            async def apply_resolved_zotero_edges(self, project_id, resolutions, *, max_items):
                self.applied = (project_id, resolutions, max_items)
                return {"applied": len(resolutions), "cross_doc_linked": 0,
                        "stale": 0, "already_resolved": 0, "rejected": 0}

        store = _Store()

        async def resolve_store(*_args):
            return store

        # Import through the normal server entry point first; handler.py is
        # also imported by server.py at module completion.
        importlib.import_module("meridian.server")
        handler_module = importlib.import_module("meridian.mcp.handler")
        monkeypatch.setattr(handler_module, "_resolve_ingest_doc_store", resolve_store)
        top_level_path = await hosted_zotero_sync.handle_apply_zotero_citation_edges(
            {"project_id": "project-1", "path": "C:/private.pdf", "resolutions": []},
            object(), "data-dir", None, None,
        )
        assert "unexpected argument" in top_level_path["error"]

        bad_collection = await hosted_zotero_sync.handle_apply_zotero_citation_edges(
            {"project_id": "project-1", "selected_collection_keys": ["bad"], "resolutions": []},
            object(), "data-dir", None, None,
        )
        assert "collection keys" in bad_collection["error"]

        result = await hosted_zotero_sync.handle_apply_zotero_citation_edges(
            {
                "project_id": "project-1",
                "selected_collection_keys": ["COLL0001"],
                "resolutions": [{
                    "element_id": "marker-1",
                    "ref": "doe2020",
                    "zotero_key": "ITEMKEY1",
                    "doi": "10.1000/example",
                    "collection_keys": ["COLL0001"],
                }],
            },
            object(),
            "data-dir",
            None,
            None,
        )
        assert result["project_id"] == "project-1"
        assert store.applied[0] == "project-1"
        assert store.applied[2] == 500
        assert store.applied[1][0]["zotero_key"] == "ITEMKEY1"

        class _BrokenStore:
            async def apply_resolved_zotero_edges(self, *_args, **_kwargs):
                raise RuntimeError("doc store unavailable")

        async def resolve_broken(*_args):
            return _BrokenStore()

        monkeypatch.setattr(handler_module, "_resolve_ingest_doc_store", resolve_broken)
        failed = await hosted_zotero_sync.handle_apply_zotero_citation_edges(
            {"project_id": "project-1", "resolutions": []},
            object(), "data-dir", None, None,
        )
        assert "could not apply citation results" in failed["error"]

    _run(scenario())


def test_hosted_pending_handler_is_bounded_and_degrades_cleanly(monkeypatch):
    importlib.import_module("meridian.server")
    handler_module = importlib.import_module("meridian.mcp.handler")

    async def scenario():
        invalid_project = await hosted_zotero_sync.handle_get_pending_zotero_citations(
            {"project_id": " "}, None, "", None, None,
        )
        assert "project_id" in invalid_project["error"]
        invalid_limit = await hosted_zotero_sync.handle_get_pending_zotero_citations(
            {"project_id": "project-1", "max_items": True}, None, "", None, None,
        )
        assert "max_items" in invalid_limit["error"]

        class _Store:
            async def get_pending_zotero_citations(self, project_id, *, max_items):
                return {"markers": [{"id": "m1", "ref": "citekey"}], "has_more": True,
                        "max_items": max_items}

        async def resolve_store(*_args):
            return _Store()

        monkeypatch.setattr(handler_module, "_resolve_ingest_doc_store", resolve_store)
        result = await hosted_zotero_sync.handle_get_pending_zotero_citations(
            {"project_id": "project-1", "max_items": 2}, object(), "data-dir", None, None,
        )
        assert result["project_id"] == "project-1"
        assert result["max_items"] == 2

        async def no_store(*_args):
            return None

        monkeypatch.setattr(handler_module, "_resolve_ingest_doc_store", no_store)
        unavailable = await hosted_zotero_sync.handle_get_pending_zotero_citations(
            {"project_id": "project-1"}, object(), "data-dir", None, None,
        )
        assert "unavailable" in unavailable["error"]

        class _BrokenStore:
            async def get_pending_zotero_citations(self, *_args, **_kwargs):
                raise RuntimeError("read failed")

        async def broken_store(*_args):
            return _BrokenStore()

        monkeypatch.setattr(handler_module, "_resolve_ingest_doc_store", broken_store)
        failed = await hosted_zotero_sync.handle_get_pending_zotero_citations(
            {"project_id": "project-1"}, object(), "data-dir", None, None,
        )
        assert "could not read pending citations" in failed["error"]

    _run(scenario())
