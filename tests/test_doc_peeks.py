"""Recently-viewed (not saved) document-peek log (79ee73e8).

get_document_structure is a stateless peek; this records recent peeks in a capped,
tenant-scoped in-memory ring so the Documents tab can surface them.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from meridian import db as db_module
from meridian import doc_peeks


def test_record_and_get_peeks_newest_first_dedup_capped():
    doc_peeks.clear()
    for i in range(12):
        doc_peeks.record_peek("t1", f"/f{i}.docx")
    peeks = doc_peeks.get_peeks("t1")
    assert len(peeks) == 10                      # capped at _MAX_PER_SCOPE
    assert peeks[0]["file_path"] == "/f11.docx"  # newest first

    # Re-viewing an existing path moves it to the front with no duplicate.
    doc_peeks.record_peek("t1", "/f5.docx")
    peeks = doc_peeks.get_peeks("t1")
    assert peeks[0]["file_path"] == "/f5.docx"
    assert [p["file_path"] for p in peeks].count("/f5.docx") == 1

    # Scope isolation — a peek in one tenant never appears in another.
    doc_peeks.record_peek("t2", "/other.docx")
    assert doc_peeks.get_peeks("t2")[0]["file_path"] == "/other.docx"
    assert all(p["file_path"] != "/other.docx" for p in doc_peeks.get_peeks("t1"))
    doc_peeks.clear()


def test_record_peek_ignores_empty_path_and_scopes_none_to_local():
    doc_peeks.clear()
    doc_peeks.record_peek("s", "")           # empty path is a no-op
    assert doc_peeks.get_peeks("s") == []
    doc_peeks.record_peek(None, "/x.docx")   # scope None == "local"
    assert doc_peeks.get_peeks("local")[0]["file_path"] == "/x.docx"
    assert doc_peeks.get_peeks(None)[0]["file_path"] == "/x.docx"
    doc_peeks.clear()


@pytest.mark.asyncio
async def test_get_document_structure_records_peek(db, tmp_path):
    from meridian import server as srv
    doc_peeks.clear()
    doc_xml = (
        '<?xml version="1.0"?><w:document '
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        '<w:r><w:t>Intro</w:t></w:r></w:p></w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", doc_xml)
    docx = tmp_path / "peekme.docx"
    docx.write_bytes(buf.getvalue())

    res = await srv._dispatch_mcp_tool(
        "get_document_structure", {"file_path": str(docx)}, db, str(tmp_path))
    assert "heading_count" in res  # self-hosted peek succeeds
    peeks = doc_peeks.get_peeks(None)  # tenant None -> "local" scope
    assert peeks and peeks[0]["file_path"] == str(docx) and peeks[0]["ok"] is True
    doc_peeks.clear()


def test_document_peeks_endpoint(client):
    doc_peeks.clear()
    doc_peeks.record_peek(None, "/some/thesis.docx")
    r = client.get("/document-peeks")
    assert r.status_code == 200
    assert any(p["file_path"] == "/some/thesis.docx" for p in r.json()["peeks"])
    doc_peeks.clear()
