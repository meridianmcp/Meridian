"""fdbd4296 — tunnel-routed local-ingest wrapper.

Tests for ``extensions/meridian-docs/meridian_docs/local_ingest.py`` and
the ``ingest_local_document`` MCP tool wired into ``server.py``.

Scope:
  (a) Text extraction — real file I/O exercised for .txt, .md, and .docx;
      UnsupportedDocumentError raised for .pdf and unknown types; FileNotFoundError
      for a missing path. No mocking of local file reads — that is the logic
      being verified here.
  (b) HTTP forwarding — ``call_hosted_ingest`` is tested with a fake HTTP server
      (``http.server`` in a thread) so the urllib.request call, the JSON-RPC body
      construction, Bearer header, and SSE/JSON response parsing are all exercised
      end-to-end up to the network boundary. The hosted Meridian backend itself is
      NOT contacted; only the HTTP mechanics are tested.
  (c) Combined path — ``ingest_local_document`` is tested with a mock of
      ``call_hosted_ingest`` only (real extraction, mocked forwarding boundary),
      confirming the two steps are wired together and the result is augmented with
      ``chars_extracted`` and ``local_path``.
  (d) Server registration — ``ingest_local_document`` appears in the list of tools
      registered on the FastMCP server in ``server.py``.
  (e) Token resolution — env var priority: MERIDIAN_API_KEY > BEARER_TOKEN, and
      the ``Bearer `` prefix is stripped when present.
"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch, MagicMock

import pytest

# The local_ingest module lives under extensions/meridian-docs, which is NOT
# on the default PYTHONPATH for the main test suite.  Add it dynamically so
# the import works without modifying pyproject.toml / conftest.py.
_MERIDIAN_DOCS_SRC = str(
    Path(__file__).parent.parent / "extensions" / "meridian-docs"
)
if _MERIDIAN_DOCS_SRC not in sys.path:
    sys.path.insert(0, _MERIDIAN_DOCS_SRC)

from meridian_docs import local_ingest  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _synthetic_docx(text_per_para: list[str]) -> bytes:
    """Build a minimal in-memory .docx with the given paragraph texts."""
    W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    paras_xml = "".join(
        f'<w:p xmlns:w="{W_NS}"><w:r><w:t>{t}</w:t></w:r></w:p>'
        for t in text_per_para
    )
    doc_xml = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:document xmlns:w="{W_NS}"><w:body>{paras_xml}</w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", doc_xml)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# (a) Text extraction — real file I/O
# ---------------------------------------------------------------------------

class TestExtractText:
    def test_txt_file(self, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_text("Hello, world!", encoding="utf-8")
        result = local_ingest.extract_text(str(f))
        assert result == "Hello, world!"

    def test_md_file(self, tmp_path):
        f = tmp_path / "notes.md"
        content = "# Title\n\nSome body text."
        f.write_text(content, encoding="utf-8")
        result = local_ingest.extract_text(str(f))
        # Normalise CRLF so the test passes on Windows and POSIX identically.
        # extract_text reads bytes and decodes; Windows write_text may produce CRLF.
        assert result.replace("\r\n", "\n") == content

    def test_py_source_file(self, tmp_path):
        f = tmp_path / "module.py"
        src = "def foo():\n    return 42\n"
        f.write_text(src, encoding="utf-8")
        result = local_ingest.extract_text(str(f))
        assert result.replace("\r\n", "\n") == src

    def test_docx_file(self, tmp_path):
        """Real .docx extraction via zipfile + ElementTree — no mocking."""
        docx_bytes = _synthetic_docx(["First paragraph.", "Second paragraph."])
        f = tmp_path / "doc.docx"
        f.write_bytes(docx_bytes)
        result = local_ingest.extract_text(str(f))
        # Paragraphs are joined with newlines.
        assert "First paragraph." in result
        assert "Second paragraph." in result

    def test_docx_file_multi_run(self, tmp_path):
        """Multiple <w:t> runs within one <w:p> are concatenated."""
        W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        doc_xml = (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<w:document xmlns:w="{W_NS}"><w:body>'
            f'<w:p><w:r><w:t>Hello </w:t></w:r><w:r><w:t>World</w:t></w:r></w:p>'
            f'</w:body></w:document>'
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("word/document.xml", doc_xml)
        f = tmp_path / "multi_run.docx"
        f.write_bytes(buf.getvalue())
        result = local_ingest.extract_text(str(f))
        assert "Hello World" in result

    def test_missing_file_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="no such file"):
            local_ingest.extract_text(str(tmp_path / "nonexistent.txt"))

    def test_pdf_raises_unsupported(self, tmp_path):
        f = tmp_path / "report.pdf"
        f.write_bytes(b"%PDF-1.4 stub")
        with pytest.raises(local_ingest.UnsupportedDocumentError, match="PDF"):
            local_ingest.extract_text(str(f))

    def test_unknown_extension_raises_unsupported(self, tmp_path):
        f = tmp_path / "archive.zip"
        f.write_bytes(b"PK fake")
        with pytest.raises(local_ingest.UnsupportedDocumentError, match=r"unsupported"):
            local_ingest.extract_text(str(f))

    def test_empty_path_raises_doc_extraction_error(self):
        with pytest.raises(local_ingest.DocExtractionError, match="non-empty"):
            local_ingest.extract_text("")

    def test_directory_path_raises_doc_extraction_error(self, tmp_path):
        with pytest.raises(local_ingest.DocExtractionError, match="not a file"):
            local_ingest.extract_text(str(tmp_path))

    def test_invalid_docx_raises_doc_extraction_error(self, tmp_path):
        f = tmp_path / "corrupt.docx"
        f.write_bytes(b"not a zip")
        with pytest.raises(local_ingest.DocExtractionError, match=r"not a valid .docx"):
            local_ingest.extract_text(str(f))

    def test_docx_missing_document_xml(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("other/file.xml", "<root/>")
        f = tmp_path / "bad.docx"
        f.write_bytes(buf.getvalue())
        with pytest.raises(local_ingest.DocExtractionError, match="missing word/document.xml"):
            local_ingest.extract_text(str(f))


# ---------------------------------------------------------------------------
# (b) HTTP forwarding — fake HTTP server exercises call_hosted_ingest
# ---------------------------------------------------------------------------

class _FakeIngestHandler(BaseHTTPRequestHandler):
    """Captures the request body/headers and returns a configurable JSON response."""

    # Populated by the test before starting the server.
    response_body: bytes = b'{}'
    response_status: int = 200
    received: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _FakeIngestHandler.received.append({
            "path": self.path,
            "body": json.loads(body) if body else {},
            "auth": self.headers.get("Authorization", ""),
            "content_type": self.headers.get("Content-Type", ""),
            "user_agent": self.headers.get("User-Agent", ""),
        })
        self.send_response(_FakeIngestHandler.response_status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(_FakeIngestHandler.response_body)

    def log_message(self, *args):  # noqa: N802
        pass  # suppress server log noise in test output


def _start_fake_server() -> tuple[HTTPServer, str]:
    """Start a fake HTTP server on an OS-assigned port, return (server, base_url)."""
    _FakeIngestHandler.received = []
    server = HTTPServer(("127.0.0.1", 0), _FakeIngestHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


class TestCallHostedIngest:
    def test_json_rpc_body_sent(self, tmp_path):
        """call_hosted_ingest POSTs the correct JSON-RPC body with ingest_document."""
        ingest_result = {"id": "note-1", "slug": "my-doc", "title": "Test"}
        _FakeIngestHandler.response_body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "result": ingest_result,
        }).encode()
        _FakeIngestHandler.response_status = 200

        server, base_url = _start_fake_server()
        try:
            result = local_ingest.call_hosted_ingest(
                project_id="proj-123",
                content="Hello content",
                title="My Doc",
                tags="test",
                base_url=base_url,
                token="sk-test-token",
            )
        finally:
            server.shutdown()

        assert len(_FakeIngestHandler.received) == 1
        req = _FakeIngestHandler.received[0]
        assert req["path"] == "/mcp"
        assert req["auth"] == "Bearer sk-test-token"
        assert req["body"]["method"] == "tools/call"
        assert req["body"]["params"]["name"] == "ingest_document"
        args = req["body"]["params"]["arguments"]
        assert args["project_id"] == "proj-123"
        assert args["content"] == "Hello content"
        assert args["title"] == "My Doc"
        assert args["tags"] == "test"
        # source not passed → not in args
        assert "source" not in args

    def test_result_returned(self, tmp_path):
        """The result dict from the JSON-RPC response is returned."""
        ingest_result = {"id": "note-abc", "slug": "doc-slug", "title": "Doc"}
        _FakeIngestHandler.response_body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "result": ingest_result,
        }).encode()
        _FakeIngestHandler.response_status = 200
        server, base_url = _start_fake_server()
        try:
            result = local_ingest.call_hosted_ingest(
                project_id="p1", content="text", base_url=base_url, token="t"
            )
        finally:
            server.shutdown()
        assert result["id"] == "note-abc"
        assert result["slug"] == "doc-slug"

    def test_mcp_envelope_unwrapped(self):
        """MCP SDK wraps tool results in {content: [{type:'text', text:'<json>'}]}.
        call_hosted_ingest must unwrap it to return the inner dict."""
        inner = {"id": "note-xyz", "slug": "unwrapped"}
        mcp_result = {"content": [{"type": "text", "text": json.dumps(inner)}]}
        _FakeIngestHandler.response_body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "result": mcp_result,
        }).encode()
        _FakeIngestHandler.response_status = 200
        server, base_url = _start_fake_server()
        try:
            result = local_ingest.call_hosted_ingest(
                project_id="p1", content="text", base_url=base_url, token="t"
            )
        finally:
            server.shutdown()
        assert result["id"] == "note-xyz"
        assert result["slug"] == "unwrapped"

    def test_sse_response_parsed(self):
        """SSE responses (data: <json>\\n\\n) are parsed correctly."""
        inner = {"id": "sse-note", "slug": "sse-slug"}
        sse_body = f"data: {json.dumps({'jsonrpc':'2.0','id':1,'result': inner})}\n\n"
        _FakeIngestHandler.response_body = sse_body.encode()
        _FakeIngestHandler.response_status = 200
        server, base_url = _start_fake_server()
        try:
            result = local_ingest.call_hosted_ingest(
                project_id="p1", content="text", base_url=base_url, token="t"
            )
        finally:
            server.shutdown()
        assert result["id"] == "sse-note"

    def test_http_error_raises_doc_extraction_error(self):
        """A 4xx/5xx HTTP response raises DocExtractionError."""
        _FakeIngestHandler.response_body = b'{"error": "unauthorized"}'
        _FakeIngestHandler.response_status = 401
        server, base_url = _start_fake_server()
        try:
            with pytest.raises(local_ingest.DocExtractionError, match="HTTP 401"):
                local_ingest.call_hosted_ingest(
                    project_id="p1", content="text", base_url=base_url, token="bad"
                )
        finally:
            server.shutdown()

    def test_server_side_error_in_result_raises(self):
        """If the server returns {result: {error: '...'}} raises DocExtractionError."""
        _FakeIngestHandler.response_body = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "result": {"error": "project not found"},
        }).encode()
        _FakeIngestHandler.response_status = 200
        server, base_url = _start_fake_server()
        try:
            with pytest.raises(local_ingest.DocExtractionError, match="project not found"):
                local_ingest.call_hosted_ingest(
                    project_id="bad-id", content="text", base_url=base_url, token="t"
                )
        finally:
            server.shutdown()

    def test_unreachable_server_raises_doc_extraction_error(self):
        """A URL that refuses connection raises DocExtractionError (not URLError)."""
        with pytest.raises(local_ingest.DocExtractionError, match="could not reach"):
            local_ingest.call_hosted_ingest(
                project_id="p1", content="text",
                base_url="http://127.0.0.1:1",  # port 1 always refused
                token="t",
            )

    def test_source_included_when_given(self):
        """When source is passed it appears in the JSON-RPC arguments."""
        _FakeIngestHandler.response_body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "result": {"id": "n1"},
        }).encode()
        _FakeIngestHandler.response_status = 200
        server, base_url = _start_fake_server()
        try:
            local_ingest.call_hosted_ingest(
                project_id="p1", content="text", source="/path/to/file.docx",
                base_url=base_url, token="t",
            )
        finally:
            server.shutdown()
        args = _FakeIngestHandler.received[0]["body"]["params"]["arguments"]
        assert args["source"] == "/path/to/file.docx"


# ---------------------------------------------------------------------------
# (b2) e40bc575 — User-Agent header must NOT be Python's default urllib string
#
# Cloudflare WAF (error 1010 / browser_signature_banned) blocks requests that
# arrive with Python's default "Python-urllib/3.x" User-Agent.  The fix adds
# "meridian-local-ingest/1.0" to _call_mcp_tool's headers so every call from
# ingest_local_document / call_hosted_ingest / call_hosted_ingest_structure
# passes the WAF.  Tests here use a real fake HTTP server (not a monkeypatched
# urlopen) so the request's actual User-Agent header is captured and asserted.
# ---------------------------------------------------------------------------

class TestUserAgentHeader:
    """e40bc575 — _call_mcp_tool must send a non-Python User-Agent to pass Cloudflare WAF."""

    def test_call_hosted_ingest_sends_non_python_user_agent(self):
        """call_hosted_ingest must NOT send Python's default urllib User-Agent.

        Cloudflare's browser_signature_banned (error 1010) blocks requests carrying
        "Python-urllib/..." — confirmed in scripts/smoke_test_signup.py comment.
        The fixed _call_mcp_tool sets User-Agent: meridian-local-ingest/1.0.
        """
        _FakeIngestHandler.response_body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "result": {"id": "note-ua-test"},
        }).encode()
        _FakeIngestHandler.response_status = 200
        server, base_url = _start_fake_server()
        try:
            local_ingest.call_hosted_ingest(
                project_id="p1", content="test content",
                base_url=base_url, token="t",
            )
        finally:
            server.shutdown()

        assert len(_FakeIngestHandler.received) == 1
        ua = _FakeIngestHandler.received[0]["user_agent"]
        assert ua, "User-Agent header must be set (was empty)"
        assert not ua.lower().startswith("python-urllib"), (
            f"User-Agent must NOT be Python's default urllib string "
            f"(Cloudflare WAF blocks it); got: {ua!r}"
        )
        assert "meridian" in ua.lower(), (
            f"User-Agent should identify as a Meridian client; got: {ua!r}"
        )

    def test_call_hosted_ingest_structure_sends_non_python_user_agent(self):
        """call_hosted_ingest_structure (the ingest_document_structure sibling)
        also routes through _call_mcp_tool and must send the same non-Python UA."""
        _FakeIngestHandler.response_body = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "document_id": "doc-ua-test",
                        "source": "/path/to/test.docx",
                        "doc_type": "docx",
                        "element_count": 1,
                    }),
                }],
            },
        }).encode()
        _FakeIngestHandler.response_status = 200
        server, base_url = _start_fake_server()
        try:
            local_ingest.call_hosted_ingest_structure(
                project_id="p1",
                source="/path/to/test.docx",
                blocks=[{"kind": "heading", "level": 1, "text": "Title"}],
                base_url=base_url,
                token="t",
            )
        finally:
            server.shutdown()

        assert len(_FakeIngestHandler.received) == 1
        ua = _FakeIngestHandler.received[0]["user_agent"]
        assert ua, "User-Agent header must be set (was empty)"
        assert not ua.lower().startswith("python-urllib"), (
            f"User-Agent must NOT be Python's default urllib string; got: {ua!r}"
        )

    def test_user_agent_value_is_meridian_local_ingest(self):
        """Exact string check: the User-Agent must be 'meridian-local-ingest/1.0'
        so Cloudflare does not WAF-block it (error 1010 / browser_signature_banned)."""
        _FakeIngestHandler.response_body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "result": {"id": "note-ua-exact"},
        }).encode()
        _FakeIngestHandler.response_status = 200
        server, base_url = _start_fake_server()
        try:
            local_ingest.call_hosted_ingest(
                project_id="p1", content="content", base_url=base_url, token="t"
            )
        finally:
            server.shutdown()

        ua = _FakeIngestHandler.received[0]["user_agent"]
        assert ua == "meridian-local-ingest/1.0", (
            f"Expected User-Agent 'meridian-local-ingest/1.0', got {ua!r}"
        )


# ---------------------------------------------------------------------------
# (c) Combined path — ingest_local_document
# ---------------------------------------------------------------------------

class TestIngestLocalDocument:
    def test_txt_extraction_and_forwarding(self, tmp_path, monkeypatch):
        """Real file extraction wired to a mocked forwarding call."""
        f = tmp_path / "report.txt"
        f.write_text("Line one.\nLine two.", encoding="utf-8")

        captured: dict[str, Any] = {}

        def fake_hosted_ingest(project_id, content, title, source, tags,
                               base_url, token):
            captured.update(
                project_id=project_id,
                content=content,
                title=title,
                source=source,
                tags=tags,
            )
            return {"id": "note-1", "slug": "report-txt", "title": "report.txt"}

        monkeypatch.setattr(local_ingest, "call_hosted_ingest", fake_hosted_ingest)

        result = local_ingest.ingest_local_document(
            path=str(f),
            project_id="proj-abc",
            title="My Report",
            tags="reports",
        )

        # Extraction was real — the content is the actual file text.
        # Normalise CRLF for cross-platform compatibility (Windows write_text → CRLF).
        assert captured["content"].replace("\r\n", "\n") == "Line one.\nLine two."
        assert captured["project_id"] == "proj-abc"
        assert captured["title"] == "My Report"
        assert captured["tags"] == "reports"
        # source defaults to path when not given.
        assert captured["source"] == str(f)

        # Result augmented with local metadata.
        # chars_extracted reflects the raw bytes decoded — may include CRLF on Windows.
        assert result["chars_extracted"] >= len("Line one.\nLine two.")
        assert result["local_path"] == str(f)
        assert result["id"] == "note-1"

    def test_docx_extraction_and_forwarding(self, tmp_path, monkeypatch):
        """Real .docx extraction (zipfile + ET) wired to mocked forwarding."""
        docx_bytes = _synthetic_docx(["Title paragraph.", "Body text."])
        f = tmp_path / "thesis.docx"
        f.write_bytes(docx_bytes)

        forwarded_content: list[str] = []

        def fake_hosted_ingest(project_id, content, title, source, tags,
                               base_url, token):
            forwarded_content.append(content)
            return {"id": "note-2", "slug": "thesis", "title": "thesis.docx"}

        monkeypatch.setattr(local_ingest, "call_hosted_ingest", fake_hosted_ingest)

        result = local_ingest.ingest_local_document(
            path=str(f),
            project_id="proj-xyz",
        )

        # The forwarded content is the real extracted text, not a summary.
        assert "Title paragraph." in forwarded_content[0]
        assert "Body text." in forwarded_content[0]
        assert result["chars_extracted"] > 0
        assert result["local_path"] == str(f)

    def test_source_defaults_to_path_when_not_given(self, tmp_path, monkeypatch):
        f = tmp_path / "notes.md"
        f.write_text("Some notes.", encoding="utf-8")
        captured: dict = {}

        def fake_hosted_ingest(project_id, content, title, source, tags,
                               base_url, token):
            captured["source"] = source
            return {"id": "n1"}

        monkeypatch.setattr(local_ingest, "call_hosted_ingest", fake_hosted_ingest)
        local_ingest.ingest_local_document(path=str(f), project_id="p1")
        assert captured["source"] == str(f)

    def test_explicit_source_overrides_path(self, tmp_path, monkeypatch):
        f = tmp_path / "notes.md"
        f.write_text("content", encoding="utf-8")
        captured: dict = {}

        def fake_hosted_ingest(project_id, content, title, source, tags,
                               base_url, token):
            captured["source"] = source
            return {"id": "n1"}

        monkeypatch.setattr(local_ingest, "call_hosted_ingest", fake_hosted_ingest)
        local_ingest.ingest_local_document(
            path=str(f), project_id="p1", source="https://example.com/notes"
        )
        assert captured["source"] == "https://example.com/notes"

    def test_missing_file_propagates_file_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(local_ingest, "call_hosted_ingest",
                            lambda *a, **kw: {"id": "n1"})
        with pytest.raises(FileNotFoundError):
            local_ingest.ingest_local_document(
                path=str(tmp_path / "ghost.txt"), project_id="p1"
            )

    def test_pdf_raises_unsupported(self, tmp_path, monkeypatch):
        f = tmp_path / "paper.pdf"
        f.write_bytes(b"%PDF-1.4 stub")
        monkeypatch.setattr(local_ingest, "call_hosted_ingest",
                            lambda *a, **kw: {"id": "n1"})
        with pytest.raises(local_ingest.UnsupportedDocumentError):
            local_ingest.ingest_local_document(path=str(f), project_id="p1")


# ---------------------------------------------------------------------------
# (d) Server registration
# ---------------------------------------------------------------------------

def test_ingest_local_document_registered_as_mcp_tool():
    """The ingest_local_document tool must appear in the FastMCP server's tool list."""
    from meridian_docs import server as docs_server
    # FastMCP stores registered tools in ._tool_manager._tools (mcp >= 1.0).
    # Tolerate different internal attrs across mcp SDK versions.
    mcp = docs_server.mcp
    registered_names: set[str] = set()
    for attr in ("_tool_manager", "_tools", "_registry"):
        mgr = getattr(mcp, attr, None)
        if mgr is None:
            continue
        for sub in ("_tools", "tools", "_tool_map"):
            tools = getattr(mgr, sub, None)
            if isinstance(tools, dict):
                registered_names.update(tools.keys())
            elif isinstance(tools, (list, set)):
                for t in tools:
                    n = getattr(t, "name", None) or (t if isinstance(t, str) else None)
                    if n:
                        registered_names.add(n)
    # Fallback: try the public list_tools() / get_tools() API if available.
    if not registered_names:
        for method_name in ("list_tools", "get_tools"):
            method = getattr(mcp, method_name, None)
            if callable(method):
                import asyncio
                tools = asyncio.run(method())
                if isinstance(tools, dict):
                    registered_names.update(tools.keys())
                elif hasattr(tools, "__iter__"):
                    for t in tools:
                        n = getattr(t, "name", None)
                        if n:
                            registered_names.add(n)
                break
    assert "ingest_local_document" in registered_names, (
        f"ingest_local_document not found in FastMCP registered tools; "
        f"found: {sorted(registered_names)}"
    )


# ---------------------------------------------------------------------------
# (e) Token resolution from environment
# ---------------------------------------------------------------------------

class TestTokenResolution:
    def test_meridian_api_key_takes_priority(self, monkeypatch):
        monkeypatch.setenv("MERIDIAN_API_KEY", "sk-primary")
        monkeypatch.setenv("BEARER_TOKEN", "sk-secondary")
        assert local_ingest._resolve_token() == "sk-primary"

    def test_bearer_token_fallback(self, monkeypatch):
        monkeypatch.delenv("MERIDIAN_API_KEY", raising=False)
        monkeypatch.setenv("BEARER_TOKEN", "sk-fallback")
        assert local_ingest._resolve_token() == "sk-fallback"

    def test_bearer_prefix_stripped(self, monkeypatch):
        monkeypatch.setenv("MERIDIAN_API_KEY", "Bearer sk-with-prefix")
        monkeypatch.delenv("BEARER_TOKEN", raising=False)
        assert local_ingest._resolve_token() == "sk-with-prefix"

    def test_empty_when_no_env(self, monkeypatch):
        monkeypatch.delenv("MERIDIAN_API_KEY", raising=False)
        monkeypatch.delenv("BEARER_TOKEN", raising=False)
        assert local_ingest._resolve_token() == ""

    def test_meridian_url_from_env(self, monkeypatch):
        monkeypatch.setenv("MERIDIAN_URL", "https://my.meridian.local")
        assert local_ingest._resolve_base_url() == "https://my.meridian.local"

    def test_meridian_url_default(self, monkeypatch):
        monkeypatch.delenv("MERIDIAN_URL", raising=False)
        assert local_ingest._resolve_base_url() == "https://usemeridian.us"

    def test_meridian_url_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv("MERIDIAN_URL", "https://my.meridian.local/")
        assert local_ingest._resolve_base_url() == "https://my.meridian.local"
