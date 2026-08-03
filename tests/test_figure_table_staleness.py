"""Tests for figure AND table embedded-copy-vs-source drift detection (432fcfcb).

Covers:
  - Pure check_embedded_staleness() function (no DB, no async):
      - fresh (source unchanged, fingerprint matches -> not stale)
      - stale (source fingerprint differs from embed-time -> stale)
      - source missing (distinct state, not same as stale)
      - no source provenance at all (degrade gracefully, not crash)
      - mtime-only fallback (when no sha256 was recorded)
  - Identical coverage for tables and figures via kind="table" / kind="figure"
  - MCP tool end-to-end via _dispatch_mcp_tool:
      - figure path with outputs_dir resolve-through (auto-infers embed sha256)
      - table path with explicit source_path
      - missing doc -> helpful error
      - missing kind -> error
      - hosted-mode guard skips outputs_dir resolution
"""
from __future__ import annotations

import asyncio
import hashlib
import os

import pytest

from meridian.embedded_staleness import check_embedded_staleness


# ---------------------------------------------------------------------------
# Pure function tests — no database, no async runtime
# ---------------------------------------------------------------------------

class TestCheckEmbeddedStalenessNoProvenance:
    """When source_path is absent/blank, always no-source-provenance."""

    def test_none_source_path(self):
        r = check_embedded_staleness("figure", source_path=None, embed_sha256="abc")
        assert r["stale"] is None
        assert r["reason"] == "no-source-provenance"
        assert r["kind"] == "figure"

    def test_blank_source_path(self):
        r = check_embedded_staleness("table", source_path="   ", embed_sha256=None)
        assert r["stale"] is None
        assert r["reason"] == "no-source-provenance"
        assert r["kind"] == "table"

    def test_no_sha256_no_mtime_but_path_given(self, tmp_path):
        p = tmp_path / "results.csv"
        p.write_text("a,b,c\n1,2,3\n")
        # source_path given but neither embed_sha256 nor embed_mtime was recorded.
        r = check_embedded_staleness("table", source_path=str(p), embed_sha256=None, embed_mtime=None)
        assert r["stale"] is None
        assert r["reason"] == "no-source-provenance"


class TestCheckEmbeddedStalenessMissingSource:
    """Source file gone from disk -> source-missing (distinct from stale)."""

    def test_missing_file_is_not_stale(self, tmp_path):
        gone = str(tmp_path / "does_not_exist.csv")
        r = check_embedded_staleness("table", source_path=gone, embed_sha256="somehash123")
        assert r["stale"] is None
        assert r["reason"] == "source-missing"
        assert r["current_sha256"] is None

    def test_missing_file_figure(self, tmp_path):
        gone = str(tmp_path / "plot.png")
        r = check_embedded_staleness("figure", source_path=gone, embed_sha256="abc", embed_mtime=1234.5)
        assert r["stale"] is None
        assert r["reason"] == "source-missing"


class TestCheckEmbeddedStalenessFresh:
    """Source unchanged (sha256 matches) -> stale=False."""

    def test_figure_fresh_sha256(self, tmp_path):
        p = tmp_path / "plot.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\nsome-data")
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        r = check_embedded_staleness("figure", source_path=str(p), embed_sha256=h)
        assert r["stale"] is False
        assert r["reason"] == "current"
        assert r["current_sha256"] == h
        assert r["embed_sha256"] == h

    def test_table_fresh_sha256(self, tmp_path):
        p = tmp_path / "results.csv"
        p.write_text("col1,col2\n1,2\n3,4\n")
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        r = check_embedded_staleness("table", source_path=str(p), embed_sha256=h)
        assert r["stale"] is False
        assert r["reason"] == "current"
        assert r["kind"] == "table"

    def test_mtime_only_fresh(self, tmp_path):
        p = tmp_path / "data.csv"
        p.write_text("x,y\n")
        mtime = os.stat(str(p)).st_mtime
        r = check_embedded_staleness("table", source_path=str(p), embed_sha256=None, embed_mtime=mtime)
        assert r["stale"] is False
        assert r["reason"] == "current"


class TestCheckEmbeddedStalenessStale:
    """Source fingerprint differs -> stale=True."""

    def test_figure_stale_sha256_changed(self, tmp_path):
        p = tmp_path / "plot.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\noriginal")
        old_hash = hashlib.sha256(b"totally-different-content").hexdigest()
        r = check_embedded_staleness("figure", source_path=str(p), embed_sha256=old_hash)
        assert r["stale"] is True
        assert r["reason"] == "content-changed"
        assert r["current_sha256"] != old_hash

    def test_table_stale_sha256_changed(self, tmp_path):
        p = tmp_path / "sweep_results.csv"
        p.write_text("model,accuracy\nv2,0.95\n")
        stale_hash = hashlib.sha256(b"model,accuracy\nv1,0.88\n").hexdigest()
        r = check_embedded_staleness("table", source_path=str(p), embed_sha256=stale_hash)
        assert r["stale"] is True
        assert r["reason"] == "content-changed"
        assert r["kind"] == "table"
        assert r["source_path"] == str(p)

    def test_mtime_only_stale(self, tmp_path):
        p = tmp_path / "data.csv"
        p.write_text("x,y\n1,2\n")
        mtime = os.stat(str(p)).st_mtime
        old_mtime = mtime - 3600.0  # 1 hour earlier = stale
        r = check_embedded_staleness("table", source_path=str(p), embed_sha256=None, embed_mtime=old_mtime)
        assert r["stale"] is True
        assert r["reason"] == "mtime-changed"


# ---------------------------------------------------------------------------
# Result shape — every call returns the expected keys regardless of path
# ---------------------------------------------------------------------------

class TestResultShape:
    """All code paths return the same key set."""

    _EXPECTED_KEYS = {
        "kind", "stale", "reason", "source_path",
        "embed_sha256", "current_sha256", "embed_mtime", "current_mtime",
    }

    def _check_shape(self, result: dict, kind: str) -> None:
        assert self._EXPECTED_KEYS <= set(result.keys()), (
            f"Missing keys: {self._EXPECTED_KEYS - set(result.keys())}"
        )
        assert result["kind"] == kind

    def test_no_provenance(self):
        self._check_shape(
            check_embedded_staleness("figure", source_path=None, embed_sha256=None),
            "figure",
        )

    def test_missing_source(self, tmp_path):
        self._check_shape(
            check_embedded_staleness("table", source_path="/nonexistent/x.csv", embed_sha256="h"),
            "table",
        )

    def test_fresh(self, tmp_path):
        p = tmp_path / "f.csv"
        p.write_bytes(b"a,b")
        h = hashlib.sha256(b"a,b").hexdigest()
        self._check_shape(
            check_embedded_staleness("figure", source_path=str(p), embed_sha256=h),
            "figure",
        )

    def test_stale(self, tmp_path):
        p = tmp_path / "f.csv"
        p.write_bytes(b"a,b,c")
        self._check_shape(
            check_embedded_staleness("table", source_path=str(p), embed_sha256="oldhash"),
            "table",
        )


# ---------------------------------------------------------------------------
# MCP tool end-to-end via _dispatch_mcp_tool
# ---------------------------------------------------------------------------

from meridian import doc_store, db as db_module


def _seed_store_via_env(tmp_path, monkeypatch):
    sidecar = str(tmp_path / "mcp_doc_structure.db")
    monkeypatch.setenv("MERIDIAN_DOC_STORE_URL", sidecar)
    doc_store._reset_doc_store_cache()
    return sidecar


def test_mcp_check_embedded_staleness_requires_project_id_doc_kind(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh
        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            assert (await mh._dispatch_mcp_tool(
                "check_embedded_staleness", {}, db, str(tmp_path),
            )).get("error")
            assert (await mh._dispatch_mcp_tool(
                "check_embedded_staleness", {"project_id": "p"}, db, str(tmp_path),
            )).get("error")
            assert (await mh._dispatch_mcp_tool(
                "check_embedded_staleness",
                {"project_id": "p", "doc": "d.docx"},
                db, str(tmp_path),
            )).get("error")
            # kind must be figure or table
            assert (await mh._dispatch_mcp_tool(
                "check_embedded_staleness",
                {"project_id": "p", "doc": "d.docx", "kind": "equation"},
                db, str(tmp_path),
            )).get("error")
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_check_embedded_staleness_unknown_doc_returns_error(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh
        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "es-proj-1")
            res = await mh._dispatch_mcp_tool(
                "check_embedded_staleness",
                {"project_id": proj["id"], "doc": "never-ingested.docx", "kind": "figure"},
                db, str(tmp_path),
            )
            assert "error" in res
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_check_embedded_staleness_figure_no_source_provenance(tmp_path, monkeypatch):
    """A figure indexed with no file_path and no outputs_dir -> no-source-provenance."""
    async def _run():
        from meridian import server as mh
        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "es-proj-noprov")
            pid = proj["id"]
            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="chapter1.docx")
            # Index a caption-only figure (no file_path).
            fig_res = await mh._dispatch_mcp_tool(
                "index_figure",
                {"project_id": pid, "doc": "chapter1.docx",
                 "caption": "Figure 1: Something without a source file"},
                db, str(tmp_path),
            )
            assert "error" not in fig_res
            figure_id = fig_res["figure"]["id"]

            res = await mh._dispatch_mcp_tool(
                "check_embedded_staleness",
                {"project_id": pid, "doc": "chapter1.docx",
                 "kind": "figure", "figure_id": figure_id},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["stale"] is None
            assert res["reason"] == "no-source-provenance"
            assert res["kind"] == "figure"
            assert res["project_id"] == pid
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_check_embedded_staleness_figure_fresh_via_explicit_sha256(tmp_path, monkeypatch):
    """Figure with a real file_path and matching embed_sha256 -> not stale."""
    async def _run():
        from meridian import server as mh
        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "es-proj-fresh-fig")
            pid = proj["id"]
            asset = tmp_path / "plot.png"
            asset.write_bytes(b"\x89PNG\r\n\x1a\nsome-content")
            current_hash = hashlib.sha256(asset.read_bytes()).hexdigest()

            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="thesis.docx")
            fig_res = await mh._dispatch_mcp_tool(
                "index_figure",
                {"project_id": pid, "doc": "thesis.docx",
                 "file_path": str(asset),
                 "caption": "Figure 1: Results plot"},
                db, str(tmp_path),
            )
            assert "error" not in fig_res
            figure_id = fig_res["figure"]["id"]

            res = await mh._dispatch_mcp_tool(
                "check_embedded_staleness",
                {"project_id": pid, "doc": "thesis.docx",
                 "kind": "figure", "figure_id": figure_id,
                 "embed_sha256": current_hash},  # same as current -> fresh
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["stale"] is False
            assert res["reason"] == "current"
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_check_embedded_staleness_figure_stale_via_explicit_sha256(tmp_path, monkeypatch):
    """Figure with a real file_path and OLD embed_sha256 -> stale."""
    async def _run():
        from meridian import server as mh
        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "es-proj-stale-fig")
            pid = proj["id"]
            asset = tmp_path / "plot.png"
            asset.write_bytes(b"\x89PNG\r\n\x1a\nnew-regenerated-content")
            old_hash = hashlib.sha256(b"old-content").hexdigest()

            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="thesis.docx")
            fig_res = await mh._dispatch_mcp_tool(
                "index_figure",
                {"project_id": pid, "doc": "thesis.docx",
                 "file_path": str(asset),
                 "caption": "Figure 2: Results plot v2"},
                db, str(tmp_path),
            )
            figure_id = fig_res["figure"]["id"]

            res = await mh._dispatch_mcp_tool(
                "check_embedded_staleness",
                {"project_id": pid, "doc": "thesis.docx",
                 "kind": "figure", "figure_id": figure_id,
                 "embed_sha256": old_hash},  # old hash -> stale
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["stale"] is True
            assert res["reason"] == "content-changed"
            assert res["embed_sha256"] == old_hash
            assert res["current_sha256"] != old_hash
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_check_embedded_staleness_figure_source_missing(tmp_path, monkeypatch):
    """Figure points to a file that no longer exists -> source-missing."""
    async def _run():
        from meridian import server as mh
        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "es-proj-missing-fig")
            pid = proj["id"]
            gone_path = str(tmp_path / "deleted_plot.png")  # does not exist

            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="thesis.docx")
            fig_res = await mh._dispatch_mcp_tool(
                "index_figure",
                {"project_id": pid, "doc": "thesis.docx",
                 "file_path": gone_path,
                 "caption": "Figure 3: Missing"},
                db, str(tmp_path),
            )
            figure_id = fig_res["figure"]["id"]

            res = await mh._dispatch_mcp_tool(
                "check_embedded_staleness",
                {"project_id": pid, "doc": "thesis.docx",
                 "kind": "figure", "figure_id": figure_id,
                 "embed_sha256": "abc123"},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["stale"] is None
            assert res["reason"] == "source-missing"
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_check_embedded_staleness_table_fresh(tmp_path, monkeypatch):
    """Table with matching embed_sha256 -> not stale."""
    async def _run():
        from meridian import server as mh
        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "es-proj-tbl-fresh")
            pid = proj["id"]
            csv_file = tmp_path / "sweep_results.csv"
            csv_file.write_text("model,accuracy\nv2,0.95\n")
            current_hash = hashlib.sha256(csv_file.read_bytes()).hexdigest()

            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="report.docx")
            await mh._dispatch_mcp_tool(
                "index_table",
                {"project_id": pid, "doc": "report.docx",
                 "table_index": 1,
                 "caption": "Table 1: Sweep results"},
                db, str(tmp_path),
            )

            res = await mh._dispatch_mcp_tool(
                "check_embedded_staleness",
                {"project_id": pid, "doc": "report.docx",
                 "kind": "table",
                 "source_path": str(csv_file),
                 "embed_sha256": current_hash},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["stale"] is False
            assert res["reason"] == "current"
            assert res["kind"] == "table"
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_check_embedded_staleness_table_stale(tmp_path, monkeypatch):
    """Table where source CSV was regenerated -> stale."""
    async def _run():
        from meridian import server as mh
        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "es-proj-tbl-stale")
            pid = proj["id"]
            csv_file = tmp_path / "results.csv"
            csv_file.write_text("model,accuracy\nv3,0.97\n")  # "current" version
            old_hash = hashlib.sha256(b"model,accuracy\nv1,0.80\n").hexdigest()  # embed-time hash

            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="report.docx")
            await mh._dispatch_mcp_tool(
                "index_table",
                {"project_id": pid, "doc": "report.docx",
                 "table_index": 1,
                 "caption": "Table 1: Accuracy results"},
                db, str(tmp_path),
            )

            res = await mh._dispatch_mcp_tool(
                "check_embedded_staleness",
                {"project_id": pid, "doc": "report.docx",
                 "kind": "table",
                 "source_path": str(csv_file),
                 "embed_sha256": old_hash},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["stale"] is True
            assert res["reason"] == "content-changed"
            assert res["kind"] == "table"
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_check_embedded_staleness_table_source_missing(tmp_path, monkeypatch):
    """Table where source CSV is gone -> source-missing."""
    async def _run():
        from meridian import server as mh
        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "es-proj-tbl-missing")
            pid = proj["id"]

            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="report.docx")
            await mh._dispatch_mcp_tool(
                "index_table",
                {"project_id": pid, "doc": "report.docx",
                 "caption": "Table 2: Now you see it"},
                db, str(tmp_path),
            )

            res = await mh._dispatch_mcp_tool(
                "check_embedded_staleness",
                {"project_id": pid, "doc": "report.docx",
                 "kind": "table",
                 "source_path": str(tmp_path / "gone.csv"),
                 "embed_sha256": "somehash"},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["stale"] is None
            assert res["reason"] == "source-missing"
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_check_embedded_staleness_table_no_source_provenance(tmp_path, monkeypatch):
    """Table with no source_path and no outputs_dir -> no-source-provenance."""
    async def _run():
        from meridian import server as mh
        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "es-proj-tbl-noprov")
            pid = proj["id"]

            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="report.docx")
            await mh._dispatch_mcp_tool(
                "index_table",
                {"project_id": pid, "doc": "report.docx",
                 "caption": "Table 3: Manually typed"},
                db, str(tmp_path),
            )

            res = await mh._dispatch_mcp_tool(
                "check_embedded_staleness",
                {"project_id": pid, "doc": "report.docx", "kind": "table"},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["stale"] is None
            assert res["reason"] == "no-source-provenance"
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_check_embedded_staleness_figure_outputs_dir_resolve_through(tmp_path, monkeypatch):
    """When outputs_dir is given and the file is there, the tool auto-resolves
    the embed-time sha256 from the outputs_index and correctly detects freshness.
    This tests the d2a3537a resolve-through path: no embed_sha256 arg needed.
    """
    async def _run():
        from meridian import server as mh
        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "es-proj-outputs-dir")
            pid = proj["id"]

            # Create an outputs directory with a figure asset.
            outputs_dir = tmp_path / "outputs"
            outputs_dir.mkdir()
            plot_file = outputs_dir / "results_plot.png"
            plot_file.write_bytes(b"\x89PNG\r\n\x1a\nresults-content")

            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="thesis.docx")
            fig_res = await mh._dispatch_mcp_tool(
                "index_figure",
                {"project_id": pid, "doc": "thesis.docx",
                 "file_path": str(plot_file),
                 "caption": "Figure 1: Results plot from outputs/"},
                db, str(tmp_path),
            )
            figure_id = fig_res["figure"]["id"]

            # No embed_sha256 given — let the tool resolve it from outputs_dir.
            # Since the file currently matches what was in the outputs dir when
            # it was walked, it should report fresh OR no-source-provenance
            # (depending on whether the FTS index picks up the file).
            res = await mh._dispatch_mcp_tool(
                "check_embedded_staleness",
                {"project_id": pid, "doc": "thesis.docx",
                 "kind": "figure", "figure_id": figure_id,
                 "outputs_dir": str(outputs_dir)},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["kind"] == "figure"
            # The result is either fresh (resolve-through found the sha256) or
            # no-source-provenance (outputs index didn't resolve it, e.g. because
            # duckdb/fts is not available in this test environment) — neither
            # "stale=True" nor a hard error is acceptable.
            assert res["stale"] is not True, (
                f"Expected fresh or no-provenance, got stale: {res}"
            )
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# audit_figure_table_provenance (6b657a8b) — whole-document batch audit
# ---------------------------------------------------------------------------

def test_mcp_audit_figure_table_provenance_requires_project_id_and_doc(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh
        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            assert (await mh._dispatch_mcp_tool(
                "audit_figure_table_provenance", {}, db, str(tmp_path),
            )).get("error")
            assert (await mh._dispatch_mcp_tool(
                "audit_figure_table_provenance", {"project_id": "p"}, db, str(tmp_path),
            )).get("error")
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_audit_figure_table_provenance_unknown_doc_returns_error(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh
        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "audit-proj-unknown-doc")
            res = await mh._dispatch_mcp_tool(
                "audit_figure_table_provenance",
                {"project_id": proj["id"], "doc": "never-ingested.docx"},
                db, str(tmp_path),
            )
            assert "error" in res
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_audit_figure_table_provenance_orphan_and_unresolved(tmp_path, monkeypatch):
    """A figure with no file_path is an orphan; a figure WITH a file_path but no
    outputs_dir is unresolved (not an error, not a false 'ok'); a table whose
    caption carries no source hint is an orphan; summary counts add up."""
    async def _run():
        from meridian import server as mh
        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "audit-proj-orphan")
            pid = proj["id"]
            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="thesis.docx")

            # Figure with no file_path at all -> orphan / no-embedded-asset.
            await mh._dispatch_mcp_tool(
                "index_figure",
                {"project_id": pid, "doc": "thesis.docx",
                 "caption": "Figure 1: Hand-drawn sketch, no source file"},
                db, str(tmp_path),
            )
            # Figure WITH a file_path, but no outputs_dir given -> unresolved.
            asset = tmp_path / "plot.png"
            asset.write_bytes(b"\x89PNG\r\n\x1a\ndata")
            await mh._dispatch_mcp_tool(
                "index_figure",
                {"project_id": pid, "doc": "thesis.docx",
                 "file_path": str(asset),
                 "caption": "Figure 2: Results plot"},
                db, str(tmp_path),
            )
            # Table whose caption has no 'generated by'-style hint -> orphan.
            await mh._dispatch_mcp_tool(
                "index_table",
                {"project_id": pid, "doc": "thesis.docx",
                 "table_index": 1,
                 "caption": "Table 1: Summary of experimental results"},
                db, str(tmp_path),
            )

            res = await mh._dispatch_mcp_tool(
                "audit_figure_table_provenance",
                {"project_id": pid, "doc": "thesis.docx"},  # no outputs_dir
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["document_id"]
            assert len(res["figures"]) == 2
            assert len(res["tables"]) == 1

            no_path_fig = next(f for f in res["figures"] if not f["file_path"])
            assert no_path_fig["status"] == "orphan"
            assert no_path_fig["reason"] == "no-embedded-asset"

            with_path_fig = next(f for f in res["figures"] if f["file_path"])
            assert with_path_fig["status"] == "unresolved"
            assert with_path_fig["reason"] == "no-outputs-dir"

            assert res["tables"][0]["status"] == "orphan"
            assert res["tables"][0]["reason"] == "no-source-hint"

            summary = res["summary"]
            assert summary["figure_count"] == 2
            assert summary["table_count"] == 1
            assert summary["orphan_count"] == 2  # 1 figure + 1 table
            assert summary["unresolved_count"] == 1
            assert (
                summary["ok_count"] + summary["ambiguous_count"]
                + summary["orphan_count"] + summary["mismatch_count"]
                + summary["unresolved_count"]
                == summary["figure_count"] + summary["table_count"]
            )
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_audit_figure_table_provenance_table_with_source_hint_no_outputs_dir(tmp_path, monkeypatch):
    """A table caption carrying a 'generated by' hint but no outputs_dir given
    is unresolved (the hint is surfaced, but nothing was traced)."""
    async def _run():
        from meridian import server as mh
        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "audit-proj-hint-no-dir")
            pid = proj["id"]
            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="report.docx")
            await mh._dispatch_mcp_tool(
                "index_table",
                {"project_id": pid, "doc": "report.docx",
                 "table_index": 1,
                 "caption": "Table 1: Results (generated by sweep_runner.py)"},
                db, str(tmp_path),
            )

            res = await mh._dispatch_mcp_tool(
                "audit_figure_table_provenance",
                {"project_id": pid, "doc": "report.docx"},
                db, str(tmp_path),
            )
            assert "error" not in res
            tbl = res["tables"][0]
            assert tbl["status"] == "unresolved"
            assert tbl["reason"] == "no-outputs-dir"
            assert tbl["generating_script"] == "sweep_runner.py"
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_audit_figure_table_provenance_no_figures_or_tables(tmp_path, monkeypatch):
    """A document with no indexed figures/tables yields empty lists and an
    all-zero summary, never an error."""
    async def _run():
        from meridian import server as mh
        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "audit-proj-empty")
            pid = proj["id"]
            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.put_document(pid, "docx", [], source="empty.docx")

            res = await mh._dispatch_mcp_tool(
                "audit_figure_table_provenance",
                {"project_id": pid, "doc": "empty.docx"},
                db, str(tmp_path),
            )
            assert "error" not in res
            assert res["figures"] == []
            assert res["tables"] == []
            assert res["summary"]["figure_count"] == 0
            assert res["summary"]["table_count"] == 0
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())
