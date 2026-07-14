"""5fdf8858 — call_hosted_ingest_structure double-JSON-encoded ``blocks``,
producing a bloated, heavily-escaped JSON-in-JSON payload that triggered
Cloudflare's ``browser_signature_banned`` WAF block on every real call.

Root cause (confirmed via direct code read, see the item's own notes):
``call_hosted_ingest_structure`` did ``params["blocks"] = json.dumps(blocks)``
before handing ``params`` to ``_call_mcp_tool``, which itself does
``json.dumps({..., "params": params, ...})`` on the WHOLE payload — so
``blocks`` was JSON-encoded twice. Fix: pass ``blocks`` through as the real
list, matching how the working sibling ``call_hosted_ingest`` passes
``content`` unmodified.

Live-network caveat: this session has no MERIDIAN_API_KEY/BEARER_TOKEN in its
inherited environment, and per this repo's hard credentials rule will not read
.env/meridian.toml to obtain one — so the literal production, Cloudflare-fronted
usemeridian.us endpoint could not be exercised here. What IS exercised for
real: the exact bytes ``_call_mcp_tool`` places on the wire (via a real
``urllib.request.Request`` object, not a mocked-away function), and the real
server-side ``ingest_document_structure`` handler parsing them end-to-end. That
directly falsifies the specific double-encoding mechanism identified as the
root cause; only the WAF's own verdict on the fixed payload remains unverified
here.
"""
from __future__ import annotations

import io
import json
import os
import sys
import zipfile

import pytest

# Existing test_db42acce_structural_ingest.py establishes this exact sys.path
# pattern for importing the vendored extensions/meridian-docs package from the
# main test suite.
_EXT_PATH = os.path.join(os.path.dirname(__file__), "..", "extensions", "meridian-docs")
sys.path.insert(0, os.path.abspath(_EXT_PATH))
try:
    from meridian_docs import local_ingest
except ImportError:
    local_ingest = None

pytestmark = pytest.mark.skipif(
    local_ingest is None, reason="meridian_docs not importable in this environment"
)


_STRUCTURAL_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="0000D001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Experimental Setup</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Some body text describing the method in detail.</w:t></w:r></w:p>
  </w:body>
</w:document>
"""


def _zip_docx(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


class _FakeHTTPResponse:
    """Minimal context-manager stand-in for urllib.request.urlopen's return value."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


def _mock_success_response(document_id: str = "doc-fake-123", element_count: int = 3):
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "document_id": document_id,
                                "source": "irrelevant",
                                "doc_type": "docx",
                                "element_count": element_count,
                            }
                        ),
                    }
                ]
            },
        }
    ).encode()
    return _FakeHTTPResponse(body)


def _real_blocks():
    from docparse.docs_intel import document_content_tree

    data = _zip_docx(_STRUCTURAL_DOCUMENT_XML)
    tree = document_content_tree(data)
    blocks = tree.get("blocks") or []
    assert blocks, "test fixture must produce at least one body block"
    return blocks


def test_call_hosted_ingest_structure_sends_blocks_as_real_list_not_double_encoded(monkeypatch):
    """The exact regression: intercept the REAL urllib.request.Request object
    (not a mocked-away call_hosted_ingest_structure) and assert the wire
    payload's params.arguments.blocks is a JSON array, not a JSON-encoded
    string sitting inside the JSON array's slot."""
    blocks = _real_blocks()
    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout=60):
        captured["data"] = req.data
        return _mock_success_response(element_count=len(blocks))

    monkeypatch.setattr(local_ingest.urllib.request, "urlopen", fake_urlopen)

    result = local_ingest.call_hosted_ingest_structure(
        project_id="proj-1",
        source="/local/path/chapter.docx",
        blocks=blocks,
        base_url="http://fake-host.invalid",
        token="sk_meridian_fake",
    )
    assert result["document_id"] == "doc-fake-123"

    sent = json.loads(captured["data"])
    arguments = sent["params"]["arguments"]
    # THE BUG: this used to be `isinstance(arguments["blocks"], str)` — a
    # JSON-encoded string sitting inside the outer JSON, escaped a second time
    # by the outer json.dumps. It must now be the real list.
    assert isinstance(arguments["blocks"], list), (
        "blocks must be sent as a real JSON array, not a JSON-encoded string "
        "(the double-encoding bug this item fixes)"
    )
    assert arguments["blocks"] == blocks
    # No doubly-escaped quotes from a nested json.dumps should appear.
    assert '\\"kind\\"' not in captured["data"].decode(), (
        "payload must not contain doubly-escaped JSON — a signature of the "
        "double-encoding bug"
    )


def test_double_encoded_payload_was_meaningfully_larger_than_fixed_payload(monkeypatch):
    """Quantifies the item notes' claim ('roughly doubling payload size') by
    directly comparing the OLD buggy payload shape against the NEW one for the
    same blocks, proving the fix is not merely cosmetic."""
    blocks = _real_blocks()

    # Reconstruct the OLD (buggy) payload shape exactly as the pre-fix code
    # produced it: blocks explicitly json.dumps'd before the outer encode.
    old_style_arguments = {
        "project_id": "proj-1",
        "source": "/local/path/chapter.docx",
        "blocks": json.dumps(blocks),  # the bug
        "doc_type": "docx",
    }
    old_payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "ingest_document_structure", "arguments": old_style_arguments}}
    ).encode()

    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout=60):
        captured["data"] = req.data
        return _mock_success_response(element_count=len(blocks))

    monkeypatch.setattr(local_ingest.urllib.request, "urlopen", fake_urlopen)
    local_ingest.call_hosted_ingest_structure(
        project_id="proj-1",
        source="/local/path/chapter.docx",
        blocks=blocks,
        base_url="http://fake-host.invalid",
        token="sk_meridian_fake",
    )
    new_payload = captured["data"]

    assert len(old_payload) > len(new_payload), (
        "the double-encoded (buggy) payload must be larger than the fixed one"
    )
    # Escaping overhead scales with the number of quote/brace characters in the
    # blocks, which scales with document size -- for this small 2-block fixture
    # the overhead is real but modest (~9%); the item notes' "roughly doubling"
    # claim was observed on a full dissertation chapter with far more blocks.
    # Assert the measured overhead directly rather than a size-independent
    # multiplier, so this doesn't overclaim on a small fixture.
    overhead_pct = (len(old_payload) - len(new_payload)) / len(new_payload) * 100
    assert overhead_pct > 5, (
        f"expected measurable escaping overhead from double-encoding, got {overhead_pct:.1f}%"
    )


def test_call_hosted_ingest_content_sibling_unaffected_regression_guard(monkeypatch):
    """call_hosted_ingest (the working sibling) already passed content as a
    plain string — confirms this fix didn't touch or regress it."""
    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout=60):
        captured["data"] = req.data
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1,
             "result": {"content": [{"type": "text", "text": json.dumps({"id": "note-1"})}]}}
        ).encode()
        return _FakeHTTPResponse(body)

    monkeypatch.setattr(local_ingest.urllib.request, "urlopen", fake_urlopen)
    local_ingest.call_hosted_ingest(
        project_id="proj-1",
        content="plain text content",
        base_url="http://fake-host.invalid",
        token="sk_meridian_fake",
    )
    sent = json.loads(captured["data"])
    assert sent["params"]["arguments"]["content"] == "plain text content"
    assert isinstance(sent["params"]["arguments"]["content"], str)


def test_ingest_document_structure_server_side_accepts_fixed_client_payload_shape():
    """End-to-end (in-process, real server dispatch): the FIXED client sends
    blocks as a real list; confirm meridian.server's ingest_document_structure
    handler round-trips it correctly (the isinstance(_blocks_raw, list) branch,
    not just the pre-existing isinstance(..., str) branch the old buggy client
    happened to also work through)."""
    import asyncio

    async def _run():
        from meridian import server as mh
        from meridian import db as db_module
        from meridian import doc_store

        import tempfile
        tmp_path = tempfile.mkdtemp()
        os.environ["MERIDIAN_DOC_STORE_URL"] = os.path.join(tmp_path, "doc_structure.db")
        doc_store._reset_doc_store_cache()

        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "fix-verify-proj")
            pid = proj["id"]
            source = "/local/path/fixed_payload_test.docx"

            blocks = _real_blocks()

            # This is exactly what the FIXED call_hosted_ingest_structure now
            # sends as `arguments["blocks"]` — a real list, not a JSON string.
            res = await mh._dispatch_mcp_tool(
                "ingest_document_structure",
                {
                    "project_id": pid,
                    "source": source,
                    "blocks": blocks,
                    "doc_type": "docx",
                },
                db, tmp_path,
            )
            assert "error" not in res, f"ingest_document_structure failed on real-list blocks: {res}"
            assert res["document_id"] is not None
            assert res["element_count"] > 0
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())
