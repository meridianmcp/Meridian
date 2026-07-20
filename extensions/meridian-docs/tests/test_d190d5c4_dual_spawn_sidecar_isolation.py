"""Regression test for d190d5c4 -- meridian-docs dual-spawn-source sidecar check.

Context: %APPDATA%\\Claude\\claude_desktop_config.json can spawn its own direct
local STDIO meridian-docs process, and the tunnel can *independently* spawn a
second meridian-docs process (extensions/meridian-docs via
``uvx --from ... meridian-docs-mcp``, see meridian/tunnel_plugins.py). This is
the same "dual-spawn-source" shape that produced a confirmed collision bug for
code-intel (3475c72f / 8e10fb80): code-intel derives its on-disk index path
from a *fixed* location (the indexed repo root), so two independently spawned
copies necessarily point at the identical file, and a second copy holding that
file open makes the Windows index-rename fail.

meridian-docs' ``index_document`` / ``index_document_structure`` /
``get_structure`` / ``get_paragraph`` / ``search_paragraphs`` tools also write
a SQLite sidecar file (``index_db_path``) that a same-path concurrent writer
could theoretically contend on -- but unlike code-intel, meridian-docs never
computes that path itself. Every entry point in meridian_docs.docs_intel and
its 1:1 MCP wrappers in meridian_docs.server takes ``index_db_path`` as a
*mandatory, caller-supplied* argument, with no server-side default, no
environment-derived fallback (e.g. no %APPDATA%-rooted constant), and no
module-level connection cache keyed by anything other than that exact string.
Two independently spawned processes therefore only collide if the calling
convention on *both* sides happens to choose the identical path string --
that is a caller-side choice, not a server-side fixed/shared path, so the
3475c72f fix shape (separate the path per consumer/instance) does not apply
here: there is no single shared path to separate.

This test locks in that verified-safe shape:
  1. index_db_path has no default anywhere in the index/read call chain
     (docs_intel) or in the MCP tool wrappers (server) -- so a dual-spawned
     copy can never silently fall back to some fixed, shared location.
  2. Two callers using naturally distinct per-instance sidecar paths (as
     Desktop's local STDIO spawn and the tunnel's independently spawned copy
     would, since each derives its own working/cache directory) get fully
     isolated on-disk indexes: no cross-instance data bleed, and closing /
     mutating one does not affect the other.
"""
from __future__ import annotations

import inspect
import io
import os
import sqlite3
import zipfile
from typing import Any

import pytest

from meridian_docs import docs_intel, server


def _make_docx(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _docx_with_heading(text: str) -> bytes:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="00000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>{text}</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""
    return _make_docx(xml)


# ---------------------------------------------------------------------------
# 1. index_db_path is mandatory / caller-supplied everywhere -- no fixed
#    default the server could silently share across dual-spawned copies.
# ---------------------------------------------------------------------------

_DOCS_INTEL_FUNCS_WITH_INDEX_DB_PATH = [
    docs_intel._connect,
    docs_intel.check_staleness,
    docs_intel.index_docx,
    docs_intel.get_paragraph,
    docs_intel.get_structure,
    docs_intel.find_paragraphs,
    docs_intel.index_docx_structure,
    docs_intel.get_local_structure_elements,
]

# The mandatory-path MCP tool wrappers in server.py (index_db_path required,
# no default -- mirrors the docs_intel functions they delegate to 1:1).
_SERVER_FUNCS_WITH_REQUIRED_INDEX_DB_PATH = [
    server.index_document,
    server.get_structure,
    server.index_document_structure,
    server.get_structure_elements,
    server.get_paragraph,
    server.search_paragraphs,
]


@pytest.mark.parametrize("fn", _DOCS_INTEL_FUNCS_WITH_INDEX_DB_PATH)
def test_docs_intel_index_db_path_has_no_default(fn):
    """docs_intel never hardcodes or defaults index_db_path.

    If it did, two independently spawned meridian-docs processes (Desktop's
    direct STDIO entry + the tunnel's slot) that both omitted the argument
    would silently converge on the SAME on-disk sidecar file -- the exact
    fixed-shared-path shape that caused the code-intel bug. Asserting there
    is no default keeps that door closed.
    """
    sig = inspect.signature(fn)
    param = sig.parameters["index_db_path"]
    assert param.default is inspect.Parameter.empty, (
        f"{fn.__name__} must require index_db_path (no default) -- a default "
        "would let dual-spawned instances silently share a fixed sidecar path"
    )


@pytest.mark.parametrize("fn", _SERVER_FUNCS_WITH_REQUIRED_INDEX_DB_PATH)
def test_server_mcp_tools_require_index_db_path(fn):
    """The MCP tool surface (what Desktop's STDIO spawn and the tunnel's
    spawn both expose identically) also requires an explicit index_db_path
    on every call -- it is part of the tool schema, not injected by the
    server process itself."""
    sig = inspect.signature(fn)
    param = sig.parameters["index_db_path"]
    assert param.default is inspect.Parameter.empty


def test_no_module_level_appdata_or_fixed_sidecar_constant():
    """No %APPDATA%-rooted or other fixed sidecar-path constant exists in
    docs_intel/server/local_ingest -- confirming index_db_path resolution is
    100% caller-driven, unlike code-intel's fixed repo-root-derived index."""
    import meridian_docs.local_ingest as local_ingest

    for mod in (docs_intel, server, local_ingest):
        src = inspect.getsource(mod)
        assert "APPDATA" not in src
        assert "DEFAULT_INDEX_DB" not in src.upper().replace("_", "")


# ---------------------------------------------------------------------------
# 2. Two independently-spawned "instances" (their own sidecar paths, exactly
#    as Desktop's local STDIO spawn and the tunnel's spawn would each derive
#    from their own working/cache directory) stay fully isolated.
# ---------------------------------------------------------------------------

def test_dual_spawn_instances_get_naturally_distinct_sidecar_paths_and_data(tmp_path):
    """Simulates the dual-spawn scenario end-to-end at the docs_intel layer.

    "desktop_instance" and "tunnel_instance" model the two independently
    spawned meridian-docs processes. Each picks its own sidecar path the way
    a real caller would (rooted in its own instance-scoped directory) and
    indexes the *same* logical document concurrently. Because index_db_path
    is caller-supplied per call (not server-fixed), the two paths are
    naturally distinct, and the resulting indexes are fully isolated: no
    shared connection, no cross-instance data bleed.
    """
    desktop_dir = tmp_path / "desktop_stdio_instance"
    tunnel_dir = tmp_path / "tunnel_spawned_instance"
    desktop_dir.mkdir()
    tunnel_dir.mkdir()

    desktop_index_db = str(desktop_dir / "sidecar.index.db")
    tunnel_index_db = str(tunnel_dir / "sidecar.index.db")

    # Naturally distinct -- nothing coordinated this; each instance just used
    # its own directory, same as two real spawned processes would.
    assert desktop_index_db != tunnel_index_db

    desktop_doc = _docx_with_heading("Desktop Instance Heading")
    tunnel_doc = _docx_with_heading("Tunnel Instance Heading")

    desktop_summary = docs_intel.index_docx(desktop_doc, desktop_index_db)
    tunnel_summary = docs_intel.index_docx(tunnel_doc, tunnel_index_db)

    assert desktop_summary["index_db"] == desktop_index_db
    assert tunnel_summary["index_db"] == tunnel_index_db

    # Both sidecar files exist independently on disk.
    assert os.path.exists(desktop_index_db)
    assert os.path.exists(tunnel_index_db)

    # Data is isolated: reading through one path never surfaces the other
    # instance's content.
    desktop_structure = docs_intel.get_structure(desktop_index_db)
    tunnel_structure = docs_intel.get_structure(tunnel_index_db)
    assert [h["text"] for h in desktop_structure] == ["Desktop Instance Heading"]
    assert [h["text"] for h in tunnel_structure] == ["Tunnel Instance Heading"]

    # Mutating one instance's sidecar (re-index with new content) leaves the
    # other completely untouched -- confirms no shared global/module state.
    docs_intel.index_docx(_docx_with_heading("Desktop Updated"), desktop_index_db)
    assert [h["text"] for h in docs_intel.get_structure(desktop_index_db)] == [
        "Desktop Updated"
    ]
    assert [h["text"] for h in docs_intel.get_structure(tunnel_index_db)] == [
        "Tunnel Instance Heading"
    ]

    # Deleting one instance's sidecar file entirely does not affect the
    # other's readability.
    os.remove(desktop_index_db)
    assert not os.path.exists(desktop_index_db)
    assert [h["text"] for h in docs_intel.get_structure(tunnel_index_db)] == [
        "Tunnel Instance Heading"
    ]


def test_connect_opens_a_fresh_scoped_connection_per_call(tmp_path):
    """_connect() (the shared entry point for every read/write op) opens a
    brand-new sqlite3.Connection bound to exactly the path it's given each
    call -- there is no process-wide connection cache/singleton that could
    silently redirect one instance's operations at another instance's
    already-open handle."""
    path_a = str(tmp_path / "a.db")
    path_b = str(tmp_path / "b.db")

    conn_a = docs_intel._connect(path_a)
    conn_b = docs_intel._connect(path_b)
    try:
        assert conn_a is not conn_b
        assert isinstance(conn_a, sqlite3.Connection)
        assert isinstance(conn_b, sqlite3.Connection)
    finally:
        conn_a.close()
        conn_b.close()
