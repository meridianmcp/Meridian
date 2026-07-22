"""Coverage for 8ca89e8f — flag-to-section drift check (the unbuilt half of
workspace proposal 8d8bbe63): a durable link from a config flag=value state to
the docx section/paragraph/figure/table id whose numbers it produced, plus a
staleness check when the flag's current default no longer matches what was
recorded.

Exercises:

* **flag_registry pure functions** — ``dedupe_flag_links`` (latest-per-pair
  collapse, tie-breaking, empty input) and ``diff_flag_links`` (ok/drifted/
  removed statuses, file/line pinning + its fallback-when-moved behaviour,
  malformed/missing flag_name is skipped).
* **check_flag_drift** — the convenience wrapper that also runs a fresh
  ``get_flag_registry`` scan.
* **doc_store durable storage** — ``DocStructureStore.link_flag_state``
  (insert + JSON round-trip of value/default, including ``None``/bool/float)
  and ``get_flag_links`` (all filters, append-only history, ordering).
* **MCP tools** — ``link_flag_to_section`` and ``get_flag_drift`` end-to-end
  through ``_dispatch_mcp_tool``, including the reverse query ("flag X
  changed, which sections does it touch") and required-field validation.
* **tool registration** — both tools are present in ``_MCP_TOOLS_LIST`` with
  the expected read-only/category/role/tier metadata.
"""
from __future__ import annotations

import asyncio

import pytest

from meridian import doc_store
from meridian import db as db_module
from meridian import flag_registry as fr


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _open_store(tmp_path) -> doc_store.DocStructureStore:
    conn = await db_module.init_db(str(tmp_path / "doc_structure.db"))
    store = doc_store.DocStructureStore(conn)
    await store.ensure_schema()
    return store


def _seed_store_via_env(tmp_path, monkeypatch):
    sidecar = str(tmp_path / "mcp_doc_structure.db")
    monkeypatch.setenv("MERIDIAN_DOC_STORE_URL", sidecar)
    doc_store._reset_doc_store_cache()
    return sidecar


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# ---------------------------------------------------------------------------
# (a) flag_registry.dedupe_flag_links
# ---------------------------------------------------------------------------

def test_dedupe_keeps_latest_per_element_flag_pair():
    links = [
        {"element_id": "e1", "flag_name": "F", "recorded_default": 0, "created_at": "2026-01-01"},
        {"element_id": "e1", "flag_name": "F", "recorded_default": 1, "created_at": "2026-02-01"},
        {"element_id": "e2", "flag_name": "F", "recorded_default": 0, "created_at": "2026-01-15"},
    ]
    result = fr.dedupe_flag_links(links)
    by_key = {(r["element_id"], r["flag_name"]): r for r in result}
    assert len(result) == 2
    assert by_key[("e1", "F")]["recorded_default"] == 1
    assert by_key[("e2", "F")]["recorded_default"] == 0


def test_dedupe_different_flags_same_element_both_kept():
    links = [
        {"element_id": "e1", "flag_name": "A", "created_at": "2026-01-01"},
        {"element_id": "e1", "flag_name": "B", "created_at": "2026-01-01"},
    ]
    result = fr.dedupe_flag_links(links)
    assert len(result) == 2


def test_dedupe_empty_input_returns_empty():
    assert fr.dedupe_flag_links([]) == []
    assert fr.dedupe_flag_links(None) == []


def test_dedupe_missing_created_at_does_not_crash():
    links = [
        {"element_id": "e1", "flag_name": "F", "recorded_default": 0},
        {"element_id": "e1", "flag_name": "F", "recorded_default": 1, "created_at": "2026-01-01"},
    ]
    result = fr.dedupe_flag_links(links)
    assert len(result) == 1
    # The one WITH a created_at sorts after the missing one (treated as "").
    assert result[0]["recorded_default"] == 1


# ---------------------------------------------------------------------------
# (b) flag_registry.diff_flag_links
# ---------------------------------------------------------------------------

def test_diff_ok_when_default_unchanged():
    current = [{"flag_name": "F", "file": "a.py", "line": 1, "default": 0}]
    links = [{"flag_name": "F", "recorded_default": 0}]
    out = fr.diff_flag_links(links, current)
    assert len(out) == 1
    assert out[0]["status"] == "ok"
    assert out[0]["current_default"] == 0
    assert out[0]["current_call_sites"] == 1


def test_diff_drifted_when_default_changed():
    """The DT_ONLY_WIDTH scenario from the item notes: default flipped."""
    current = [{"flag_name": "DT_ONLY_WIDTH", "file": "gt.py", "line": 10, "default": 0}]
    links = [{"flag_name": "DT_ONLY_WIDTH", "recorded_default": 1}]
    out = fr.diff_flag_links(links, current)
    assert out[0]["status"] == "drifted"
    assert out[0]["current_default"] == 0


def test_diff_removed_when_flag_no_longer_scanned():
    current = [{"flag_name": "OTHER_FLAG", "file": "a.py", "line": 1, "default": 0}]
    links = [{"flag_name": "RUN_DT_ONLY", "recorded_default": True}]
    out = fr.diff_flag_links(links, current)
    assert out[0]["status"] == "removed"
    assert out[0]["current_default"] is None
    assert out[0]["current_call_sites"] == 0


def test_diff_pins_to_exact_call_site_avoiding_false_drift():
    """Two call sites for the same flag name with different defaults: pinning
    to the recorded file/line must not be confused by the OTHER site."""
    current = [
        {"flag_name": "F", "file": "a.py", "line": 10, "default": 0},
        {"flag_name": "F", "file": "b.py", "line": 20, "default": 999},
    ]
    links = [{"flag_name": "F", "recorded_default": 0, "source_file": "a.py", "source_line": 10}]
    out = fr.diff_flag_links(links, current)
    assert out[0]["status"] == "ok"
    assert out[0]["current_call_sites"] == 1


def test_diff_falls_back_when_pinned_site_moved():
    """The pinned file/line is gone (renumbered), but the flag still exists
    elsewhere under the same name with the SAME default — falls back to a
    name-only match rather than falsely reporting 'removed'."""
    current = [{"flag_name": "F", "file": "a.py", "line": 15, "default": 0}]
    links = [{"flag_name": "F", "recorded_default": 0, "source_file": "a.py", "source_line": 10}]
    out = fr.diff_flag_links(links, current)
    assert out[0]["status"] == "ok"


def test_diff_multiple_matches_no_pin_reports_list_of_defaults():
    current = [
        {"flag_name": "F", "file": "a.py", "line": 10, "default": 0},
        {"flag_name": "F", "file": "b.py", "line": 20, "default": 1},
    ]
    links = [{"flag_name": "F", "recorded_default": 0}]
    out = fr.diff_flag_links(links, current)
    assert out[0]["status"] == "ok"  # 0 is among the current defaults
    assert isinstance(out[0]["current_default"], list)
    assert out[0]["current_call_sites"] == 2


def test_diff_skips_link_with_missing_flag_name():
    links = [{"recorded_default": 0}, {"flag_name": "", "recorded_default": 1}]
    out = fr.diff_flag_links(links, [])
    assert out == []


def test_diff_preserves_input_link_fields():
    current = [{"flag_name": "F", "file": "a.py", "line": 1, "default": 0}]
    links = [{"flag_name": "F", "recorded_default": 0, "element_id": "e1", "id": "link-1"}]
    out = fr.diff_flag_links(links, current)
    assert out[0]["element_id"] == "e1"
    assert out[0]["id"] == "link-1"


def test_diff_empty_links_and_empty_current():
    assert fr.diff_flag_links([], []) == []
    assert fr.diff_flag_links(None, None) == []


# ---------------------------------------------------------------------------
# (c) flag_registry.check_flag_drift (integration with a real scan)
# ---------------------------------------------------------------------------

def test_check_flag_drift_runs_a_fresh_scan(tmp_path):
    _write(tmp_path / "gt.py", 'import os\nX = os.environ.get("DT_ONLY_WIDTH", 0)\n')
    links = [{
        "flag_name": "DT_ONLY_WIDTH", "recorded_default": 1,
        "source_file": str(tmp_path / "gt.py"), "source_line": 2,
    }]
    out = fr.check_flag_drift(links, str(tmp_path))
    assert len(out) == 1
    assert out[0]["status"] == "drifted"
    assert out[0]["current_default"] == 0


def test_check_flag_drift_ok_when_matching(tmp_path):
    _write(tmp_path / "gt.py", 'import os\nX = os.environ.get("DT_ONLY_WIDTH", 0)\n')
    links = [{
        "flag_name": "DT_ONLY_WIDTH", "recorded_default": 0,
        "source_file": str(tmp_path / "gt.py"), "source_line": 2,
    }]
    out = fr.check_flag_drift(links, str(tmp_path))
    assert out[0]["status"] == "ok"


# ---------------------------------------------------------------------------
# (d) doc_store.DocStructureStore.link_flag_state / get_flag_links
# ---------------------------------------------------------------------------

def test_link_flag_state_stores_and_roundtrips(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            link = await store.link_flag_state(
                "proj-1", doc["id"], "el-1", "DT_ONLY_WIDTH",
                value=1, default=0, source_file="gt.py", source_line=42,
            )
            assert link["flag_name"] == "DT_ONLY_WIDTH"
            assert link["recorded_value"] == 1
            assert link["recorded_default"] == 0
            assert link["source_file"] == "gt.py"
            assert link["source_line"] == 42
            assert link["element_id"] == "el-1"
            assert link["document_id"] == doc["id"]
            assert link["project_id"] == "proj-1"
            assert link["id"]
            assert link["created_at"]

            fetched = await store.get_flag_links("proj-1", element_id="el-1")
            assert len(fetched) == 1
            assert fetched[0]["id"] == link["id"]
        finally:
            await store.close()

    asyncio.run(_run())


def test_link_flag_state_roundtrips_none_bool_float_value():
    """JSON encoding must faithfully round-trip every JSON-scalar type,
    including None (a legitimate 'this flag has no default' claim)."""
    async def _run(tmp_path):
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            cases = [None, True, False, 0, 3.14, "text"]
            for i, val in enumerate(cases):
                link = await store.link_flag_state(
                    "proj-1", doc["id"], f"el-{i}", "F", value=val, default=val,
                )
                assert link["recorded_value"] == val
                assert link["recorded_default"] == val
        finally:
            await store.close()

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        asyncio.run(_run(Path(td)))


def test_link_flag_state_is_append_only_history(tmp_path):
    """Re-linking the same (element_id, flag_name) pair adds a NEW row rather
    than overwriting -- an append-only provenance trail."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", [], source="a.docx")
            first = await store.link_flag_state(
                "proj-1", doc["id"], "el-1", "F", value=0, default=0,
            )
            second = await store.link_flag_state(
                "proj-1", doc["id"], "el-1", "F", value=1, default=0,
            )
            assert first["id"] != second["id"]
            # seq strictly increases across sequential calls -- the tiebreaker
            # that makes "newest first" correct even when created_at ties
            # (see the doc_flag_links schema comment / dedupe_flag_links).
            assert second["seq"] > first["seq"]
            history = await store.get_flag_links("proj-1", element_id="el-1", flag_name="F")
            assert len(history) == 2
            # Newest first.
            assert history[0]["id"] == second["id"]
        finally:
            await store.close()

    asyncio.run(_run())


def test_dedupe_uses_seq_to_break_created_at_ties():
    """Two links with an IDENTICAL created_at (a realistic race on some
    platforms -- see the doc_flag_links schema comment) must still resolve
    to the correctly-latest one via the seq tiebreaker."""
    links = [
        {"element_id": "e1", "flag_name": "F", "recorded_default": 0,
         "created_at": "2026-01-01T00:00:00", "seq": 5},
        {"element_id": "e1", "flag_name": "F", "recorded_default": 1,
         "created_at": "2026-01-01T00:00:00", "seq": 6},
    ]
    result = fr.dedupe_flag_links(links)
    assert len(result) == 1
    assert result[0]["recorded_default"] == 1


def test_get_flag_links_filters(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc1 = await store.put_document("proj-1", "docx", [], source="a.docx")
            doc2 = await store.put_document("proj-1", "docx", [], source="b.docx")
            await store.link_flag_state("proj-1", doc1["id"], "el-1", "FLAG_A", value=1)
            await store.link_flag_state("proj-1", doc1["id"], "el-2", "FLAG_B", value=2)
            await store.link_flag_state("proj-1", doc2["id"], "el-3", "FLAG_A", value=3)

            by_doc = await store.get_flag_links("proj-1", document_id=doc1["id"])
            assert {r["element_id"] for r in by_doc} == {"el-1", "el-2"}

            by_flag = await store.get_flag_links("proj-1", flag_name="FLAG_A")
            assert {r["element_id"] for r in by_flag} == {"el-1", "el-3"}

            by_element = await store.get_flag_links("proj-1", element_id="el-2")
            assert len(by_element) == 1
            assert by_element[0]["flag_name"] == "FLAG_B"

            all_links = await store.get_flag_links("proj-1")
            assert len(all_links) == 3
        finally:
            await store.close()

    asyncio.run(_run())


def test_get_flag_links_unknown_project_returns_empty(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            result = await store.get_flag_links("no-such-project")
            assert result == []
        finally:
            await store.close()

    asyncio.run(_run())


def test_doc_flag_links_schema_idempotent(tmp_path):
    """ensure_schema is idempotent and doc_flag_links exists on a fresh DB."""
    async def _run():
        conn = await db_module.init_db(str(tmp_path / "migration_test.db"))
        store = doc_store.DocStructureStore(conn)
        await store.ensure_schema()
        await store.ensure_schema()  # must not raise
        assert await store._column_exists("doc_flag_links", "flag_name")
        assert await store._column_exists("doc_flag_links", "recorded_default")
        assert await store._column_exists("doc_flag_links", "seq")
        await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# (e) MCP tools: link_flag_to_section / get_flag_drift end-to-end
# ---------------------------------------------------------------------------

def test_mcp_link_flag_to_section_end_to_end(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "flag-link-proj")
            pid = proj["id"]

            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="chapter4.docx")

            res = await mh._dispatch_mcp_tool(
                "link_flag_to_section",
                {"project_id": pid, "doc": "chapter4.docx", "element_id": "el-1",
                 "flag_name": "DT_ONLY_WIDTH", "value": 1, "default": 0,
                 "source_file": "pipeline/gt.py", "source_line": 142},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["link"]["flag_name"] == "DT_ONLY_WIDTH"
            assert res["link"]["recorded_value"] == 1
            assert res["link"]["recorded_default"] == 0
            assert res["document_id"]
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_link_flag_to_section_requires_all_fields(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            assert (await mh._dispatch_mcp_tool(
                "link_flag_to_section", {}, db, str(tmp_path),
            )).get("error")
            assert (await mh._dispatch_mcp_tool(
                "link_flag_to_section", {"project_id": "p"}, db, str(tmp_path),
            )).get("error")
            assert (await mh._dispatch_mcp_tool(
                "link_flag_to_section",
                {"project_id": "p", "doc": "x.docx"}, db, str(tmp_path),
            )).get("error")
            assert (await mh._dispatch_mcp_tool(
                "link_flag_to_section",
                {"project_id": "p", "doc": "x.docx", "element_id": "el-1"},
                db, str(tmp_path),
            )).get("error")
            # Missing value entirely (key absent).
            assert (await mh._dispatch_mcp_tool(
                "link_flag_to_section",
                {"project_id": "p", "doc": "x.docx", "element_id": "el-1",
                 "flag_name": "F"},
                db, str(tmp_path),
            )).get("error")
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_link_flag_to_section_accepts_explicit_none_value(tmp_path, monkeypatch):
    """value=None (JSON null) is a legitimate payload -- must not be treated
    as 'missing'."""
    async def _run():
        from meridian import server as mh

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "flag-link-proj-2")
            pid = proj["id"]
            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="c.docx")
            res = await mh._dispatch_mcp_tool(
                "link_flag_to_section",
                {"project_id": pid, "doc": "c.docx", "element_id": "el-1",
                 "flag_name": "F", "value": None},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["link"]["recorded_value"] is None
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_link_flag_to_section_unknown_doc_returns_error(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "flag-link-proj-3")
            res = await mh._dispatch_mcp_tool(
                "link_flag_to_section",
                {"project_id": proj["id"], "doc": "never-ingested.docx",
                 "element_id": "el-1", "flag_name": "F", "value": 1},
                db, str(tmp_path),
            )
            assert "error" in res
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_get_flag_drift_end_to_end(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "flag-drift-proj")
            pid = proj["id"]
            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="chapter4.docx")

            src_dir = tmp_path / "src"
            _write(src_dir / "gt.py", 'import os\nX = os.environ.get("DT_ONLY_WIDTH", 0)\n')

            await mh._dispatch_mcp_tool(
                "link_flag_to_section",
                {"project_id": pid, "doc": "chapter4.docx", "element_id": "el-1",
                 "flag_name": "DT_ONLY_WIDTH", "value": 1, "default": 1,
                 "source_file": str(src_dir / "gt.py"), "source_line": 2},
                db, str(tmp_path),
            )

            drift = await mh._dispatch_mcp_tool(
                "get_flag_drift",
                {"project_id": pid, "doc": "chapter4.docx", "root_dir": str(src_dir)},
                db, str(tmp_path),
            )
            assert "error" not in drift
            assert drift["summary"]["drifted"] == 1
            assert drift["links"][0]["status"] == "drifted"
            assert drift["links"][0]["current_default"] == 0
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_get_flag_drift_reverse_query_across_documents(tmp_path, monkeypatch):
    """flag_name with no `doc` -- 'flag X changed, which sections does it
    touch' across the whole project."""
    async def _run():
        from meridian import server as mh

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "flag-reverse-proj")
            pid = proj["id"]
            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="ch1.docx")
            await seed.put_document(pid, "docx", [], source="ch2.docx")

            await mh._dispatch_mcp_tool(
                "link_flag_to_section",
                {"project_id": pid, "doc": "ch1.docx", "element_id": "el-1",
                 "flag_name": "RUN_DT_ONLY", "value": True, "default": False},
                db, str(tmp_path),
            )
            await mh._dispatch_mcp_tool(
                "link_flag_to_section",
                {"project_id": pid, "doc": "ch2.docx", "element_id": "el-2",
                 "flag_name": "RUN_DT_ONLY", "value": True, "default": False},
                db, str(tmp_path),
            )

            src_dir = tmp_path / "src2"
            _write(src_dir / "pipeline.py", "import os\n")  # flag no longer read

            drift = await mh._dispatch_mcp_tool(
                "get_flag_drift",
                {"project_id": pid, "flag_name": "RUN_DT_ONLY", "root_dir": str(src_dir)},
                db, str(tmp_path),
            )
            assert "error" not in drift
            assert len(drift["links"]) == 2
            assert drift["summary"]["removed"] == 2
            assert {l["element_id"] for l in drift["links"]} == {"el-1", "el-2"}
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_get_flag_drift_requires_project_id(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            res = await mh._dispatch_mcp_tool(
                "get_flag_drift", {}, db, str(tmp_path),
            )
            assert res.get("error")
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_get_flag_drift_no_links_returns_empty(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "flag-empty-proj")
            res = await mh._dispatch_mcp_tool(
                "get_flag_drift",
                {"project_id": proj["id"], "root_dir": str(tmp_path)},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["links"] == []
            assert res["summary"] == {"ok": 0, "drifted": 0, "removed": 0}
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_get_flag_drift_unknown_doc_returns_error(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "flag-drift-unknown-doc")
            res = await mh._dispatch_mcp_tool(
                "get_flag_drift",
                {"project_id": proj["id"], "doc": "never-ingested.docx"},
                db, str(tmp_path),
            )
            assert "error" in res
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# (f) tool registration
# ---------------------------------------------------------------------------

def test_tools_registered_in_mcp_tools_list():
    from meridian import mcp_tools as mt

    names = [t["name"] for t in mt._MCP_TOOLS_LIST]
    assert "link_flag_to_section" in names
    assert "get_flag_drift" in names


def test_get_flag_drift_is_read_only_link_is_not():
    from meridian import mcp_tools as mt

    assert "get_flag_drift" in mt._READ_ONLY_TOOLS
    assert "link_flag_to_section" not in mt._READ_ONLY_TOOLS
    assert "link_flag_to_section" not in mt._DESTRUCTIVE_TOOLS
    assert "get_flag_drift" not in mt._DESTRUCTIVE_TOOLS


def test_tools_have_valid_category_and_role():
    from meridian import mcp_tools as mt

    assert mt._TOOL_CATEGORY.get("link_flag_to_section") == "docx"
    assert mt._TOOL_CATEGORY.get("get_flag_drift") == "docx"
    assert mt._TOOL_ROLE_RELEVANCE.get("link_flag_to_section") == "executor"
    assert mt._TOOL_ROLE_RELEVANCE.get("get_flag_drift") == "both"


def test_tools_stamped_on_live_tool_entries():
    from meridian import mcp_tools as mt

    by_name = {t["name"]: t for t in mt._MCP_TOOLS_LIST}
    link_tool = by_name["link_flag_to_section"]
    drift_tool = by_name["get_flag_drift"]
    assert link_tool["category"] == "docx"
    assert link_tool["role_relevance"] == "executor"
    assert link_tool["annotations"]["readOnlyHint"] is False
    assert drift_tool["category"] == "docx"
    assert drift_tool["annotations"]["readOnlyHint"] is True
    required = set(link_tool["inputSchema"]["required"])
    assert required == {"doc", "element_id", "flag_name", "value"}
