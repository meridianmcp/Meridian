"""The seven local-only Zotero bridge write tools (meridian/mcp/handlers/zotero_bridge_tools.py).

Every test uses a fake client, so nothing reaches Zotero or the bridge. The fake is installed through
meridian.zotero_bridge_transport.get_client, which the handlers reach at call time. One test runs the real
ZoteroBridgeClient over fake transport and reader callables, to check the attachment body the handler sends.
The grant key and the token are never printed by any test.
"""

from __future__ import annotations

import base64
import json
import secrets
from datetime import datetime, timedelta, timezone

import pytest

import meridian.server  # noqa: F401 - import first to avoid the handler/server import cycle
from meridian import zotero_bridge_transport as transport_mod
from meridian.mcp import handler as mh
from meridian.mcp.handlers import zotero_bridge_tools as handlers
from meridian.mcp_tools import _MCP_TOOLS_LIST
from meridian.zotero_bridge_client import (
    ALREADY_ATTACHED,
    ALREADY_PRESENT,
    BatchRolledBackError,
    GrantError,
    ZoteroBridgeClient,
    ZoteroBridgeError,
)

ALL_TOOLS = (
    "zotero_create_collection",
    "zotero_move_collection",
    "zotero_move_item",
    "zotero_create_webpage",
    "zotero_attach_pdf",
    "zotero_add_child_note",
    "zotero_batch",
)

OPERATION = {"method": "PATCH", "path": "/v1/items/WXYZ5678", "body": {"add_collections": ["ABCD1234"]}}

VALID_ARGS = {
    "zotero_create_collection": {"name": "Reading list"},
    "zotero_move_collection": {"key": "ABCD1234", "parent_key": None},
    "zotero_move_item": {"item_key": "WXYZ5678", "add_collections": ["ABCD1234"]},
    "zotero_create_webpage": {"title": "Pricing", "url": "https://example.com/pricing"},
    "zotero_attach_pdf": {"item_key": "WXYZ5678", "path": "/papers/a.pdf"},
    "zotero_add_child_note": {
        "parent_key": "WXYZ5678",
        "html": "<p>Summary</p>",
        "dedup_marker": "meridian-summary-v1",
    },
    "zotero_batch": {"operations": [OPERATION]},
}


class FakeClient:
    """Records each call as (method name, positional arguments). Returns ``returns[method]``, or raises ``error``."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self.returns: dict[str, object] = {}
        self.error: BaseException | None = None

    def _record(self, method: str, *args):
        self.calls.append((method, args))
        if self.error is not None:
            raise self.error
        return self.returns.get(method)

    def create_collection(self, name, parent_key=None):
        return self._record("create_collection", name, parent_key)

    def move_collection(self, key, parent_key):
        return self._record("move_collection", key, parent_key)

    def move_item(self, item_key, add_collections=(), remove_collections=()):
        return self._record("move_item", item_key, list(add_collections), list(remove_collections))

    def create_webpage(self, title, url, parent_collection_keys=()):
        return self._record("create_webpage", title, url, list(parent_collection_keys))

    def attach_pdf(self, item_key, path, title=None):
        return self._record("attach_pdf", item_key, path, title)

    def add_child_note(self, parent_key, html, dedup_marker):
        return self._record("add_child_note", parent_key, html, dedup_marker)

    def batch(self, operations):
        return self._record("batch", operations)


@pytest.fixture(autouse=True)
def _local_mode_and_no_shared_client(monkeypatch):
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    transport_mod.reset_client_for_tests()
    yield
    transport_mod.reset_client_for_tests()


@pytest.fixture
def fake(monkeypatch):
    """Installs a FakeClient as the process-wide client. ``fake.built`` counts how often one was asked for."""
    client = FakeClient()
    client.built = 0

    def factory():
        client.built += 1
        return client

    monkeypatch.setattr(transport_mod, "get_client", factory)
    return client


# ---------------------------------------------------------------------------
# Each tool calls the client with the right arguments
# ---------------------------------------------------------------------------

CASES = [
    (
        "zotero_create_collection",
        {"name": "Reading list", "parent_key": "ABCD1234"},
        ("create_collection", ("Reading list", "ABCD1234")),
        "NEWCOLL01",
        {"collection_key": "NEWCOLL01"},
    ),
    (
        "zotero_create_collection",
        {"name": "Top level"},
        ("create_collection", ("Top level", None)),
        "NEWCOLL02",
        {"collection_key": "NEWCOLL02"},
    ),
    (
        "zotero_move_collection",
        {"key": "ABCD1234", "parent_key": None},
        ("move_collection", ("ABCD1234", None)),
        None,
        {"collection_key": "ABCD1234", "parent_key": None},
    ),
    (
        "zotero_move_collection",
        {"key": "ABCD1234"},
        ("move_collection", ("ABCD1234", None)),
        None,
        {"collection_key": "ABCD1234", "parent_key": None},
    ),
    (
        "zotero_move_collection",
        {"key": "ABCD1234", "parent_key": "PARENT01"},
        ("move_collection", ("ABCD1234", "PARENT01")),
        None,
        {"collection_key": "ABCD1234", "parent_key": "PARENT01"},
    ),
    (
        "zotero_move_item",
        {"item_key": "WXYZ5678", "add_collections": ["ABCD1234"]},
        ("move_item", ("WXYZ5678", ["ABCD1234"], [])),
        None,
        {"item_key": "WXYZ5678"},
    ),
    (
        "zotero_move_item",
        {"item_key": "WXYZ5678", "remove_collections": ["EFGH5678"]},
        ("move_item", ("WXYZ5678", [], ["EFGH5678"])),
        None,
        {"item_key": "WXYZ5678"},
    ),
    (
        "zotero_create_webpage",
        {"title": "Pricing", "url": "https://example.com/pricing", "collection_keys": ["ABCD1234"]},
        ("create_webpage", ("Pricing", "https://example.com/pricing", ["ABCD1234"])),
        "WEB00001",
        {"item_key": "WEB00001"},
    ),
    (
        "zotero_attach_pdf",
        {"item_key": "WXYZ5678", "path": "/papers/a.pdf", "title": "Full text"},
        ("attach_pdf", ("WXYZ5678", "/papers/a.pdf", "Full text")),
        "ATT00001",
        {"status": "attached", "attachment_key": "ATT00001"},
    ),
    (
        "zotero_attach_pdf",
        {"item_key": "WXYZ5678", "path": "/papers/a.pdf"},
        ("attach_pdf", ("WXYZ5678", "/papers/a.pdf", None)),
        ALREADY_ATTACHED,
        {"status": "already_attached", "attachment_key": None},
    ),
    (
        "zotero_add_child_note",
        {"parent_key": "WXYZ5678", "html": "<p>Summary</p>", "dedup_marker": "meridian-summary-v1"},
        ("add_child_note", ("WXYZ5678", "<p>Summary</p>", "meridian-summary-v1")),
        "NOTE0001",
        {"status": "created", "note_key": "NOTE0001"},
    ),
    (
        "zotero_add_child_note",
        {"parent_key": "WXYZ5678", "html": "<p>Summary</p>", "dedup_marker": "meridian-summary-v1"},
        ("add_child_note", ("WXYZ5678", "<p>Summary</p>", "meridian-summary-v1")),
        ALREADY_PRESENT,
        {"status": "already_present", "note_key": None},
    ),
    (
        "zotero_batch",
        {"operations": [OPERATION]},
        ("batch", ([OPERATION],)),
        [{"status": 200}],
        {"results": [{"status": 200}]},
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "args", "expected_call", "returned", "extra"), CASES)
async def test_each_tool_calls_the_client_with_the_right_arguments(
    tool, args, expected_call, returned, extra, fake
):
    fake.returns[expected_call[0]] = returned

    payload = await handlers.ZOTERO_BRIDGE_HANDLERS[tool](args)

    assert fake.calls == [expected_call]
    assert payload == {"ok": True, "error": None, **extra}


@pytest.mark.asyncio
async def test_the_mcp_dispatcher_reaches_each_zotero_tool(db, tmp_path, fake):
    fake.returns["create_collection"] = "NEWCOLL01"

    payload = await mh._dispatch_mcp_tool(
        "zotero_create_collection", {"name": "Reading list"}, db, str(tmp_path)
    )

    assert payload == {"ok": True, "error": None, "collection_key": "NEWCOLL01"}
    assert fake.calls == [("create_collection", ("Reading list", None))]


# ---------------------------------------------------------------------------
# Hosted refusal, the same technique as the runpod tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ALL_TOOLS)
async def test_hosted_refuses_every_zotero_tool_before_any_client_is_built(tool, monkeypatch, fake):
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")

    payload = await handlers.ZOTERO_BRIDGE_HANDLERS[tool](dict(VALID_ARGS[tool]))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "hosted_refused"
    assert "not available on hosted Meridian" in payload["error"]["message"]
    assert fake.built == 0
    assert fake.calls == []


@pytest.mark.asyncio
async def test_hosted_refusal_also_holds_through_the_dispatcher(db, tmp_path, monkeypatch, fake):
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")

    payload = await mh._dispatch_mcp_tool("zotero_batch", {"operations": [OPERATION]}, db, str(tmp_path))

    assert payload["error"]["code"] == "hosted_refused"
    assert fake.built == 0


# ---------------------------------------------------------------------------
# Errors come back structured, with the bridge's code and no traceback or secret
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_bridge_refusal_comes_back_with_its_code_and_message(fake):
    fake.error = GrantError("grant_denied", "The desktop answered the grant request: denied.", status=403)

    payload = await handlers.handle_zotero_create_collection({"name": "X"})

    assert payload == {
        "ok": False,
        "error": {
            "code": "grant_denied",
            "message": "The desktop answered the grant request: denied.",
            "status": 403,
        },
    }


@pytest.mark.asyncio
async def test_a_rolled_back_batch_reports_the_failed_index_and_the_inner_code(fake):
    fake.error = BatchRolledBackError(
        "The batch was rolled back.", status=409, failed_index=1, inner_code="invalid_input"
    )

    payload = await handlers.handle_zotero_batch({"operations": [OPERATION, OPERATION]})

    assert payload["error"] == {
        "code": "batch_rolled_back",
        "message": "The batch was rolled back.",
        "status": 409,
        "failed_index": 1,
        "inner_code": "invalid_input",
    }


@pytest.mark.asyncio
async def test_a_client_validation_error_is_returned_as_invalid_input(fake):
    fake.error = ZoteroBridgeError("invalid_input", "name must be a non-empty string.")

    payload = await handlers.handle_zotero_create_collection({"name": "  "})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_input"


@pytest.mark.asyncio
async def test_an_unexpected_exception_returns_its_type_name_and_never_its_text(fake):
    secret = secrets.token_hex(16)
    fake.error = RuntimeError(f"boom {secret}")

    payload = await handlers.handle_zotero_create_collection({"name": "X"})

    assert payload["error"]["code"] == "unexpected_error"
    assert "RuntimeError" in payload["error"]["message"]
    assert secret not in json.dumps(payload)


@pytest.mark.asyncio
async def test_a_collection_list_that_is_not_a_list_is_refused_before_any_client_call(fake):
    payload = await handlers.handle_zotero_move_item({"item_key": "WXYZ5678", "add_collections": "ABCD1234"})

    assert payload["error"]["code"] == "invalid_input"
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Attachments through the real client: the duplicate check and the import body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_pdf_through_the_real_client_writes_nothing_when_a_pdf_is_already_attached(monkeypatch, tmp_path):
    writes: list[tuple[str, str]] = []

    def transport(method, path, headers, body):
        writes.append((method, path))
        return 201, {"key": "ATT00001"}

    def reader(path):
        return 200, [{"data": {"itemType": "attachment", "contentType": "application/pdf", "filename": "a.pdf"}}]

    real = ZoteroBridgeClient(transport=transport, reader=reader, sleep=lambda seconds: None)
    monkeypatch.setattr(transport_mod, "get_client", lambda: real)
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    payload = await handlers.handle_zotero_attach_pdf({"item_key": "WXYZ5678", "path": str(pdf)})

    assert payload == {"ok": True, "error": None, "status": "already_attached", "attachment_key": None}
    assert writes == []


@pytest.mark.asyncio
async def test_attach_pdf_imports_a_small_file_as_base64_under_a_grant(monkeypatch, tmp_path):
    grant_id = "0" * 32
    expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    sent: list[tuple[str, str, object]] = []
    pdf_bytes = b"%PDF-1.4\nsmall file\n"

    def transport(method, path, headers, body):
        sent.append((method, path, body))
        if path == "/v1/grants":
            return 200, {"grant_id": grant_id}
        if path == f"/v1/grants/{grant_id}":
            return 200, {"status": "granted", "write_key": "K" * 64, "expires_at": expires}
        return 201, {"key": "ATT00001"}

    real = ZoteroBridgeClient(
        transport=transport, reader=lambda path: (200, []), sleep=lambda seconds: None
    )
    monkeypatch.setattr(transport_mod, "get_client", lambda: real)
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(pdf_bytes)

    payload = await handlers.handle_zotero_attach_pdf({"item_key": "WXYZ5678", "path": str(pdf)})

    assert payload == {"ok": True, "error": None, "status": "attached", "attachment_key": "ATT00001"}
    method, path, body = sent[-1]
    assert (method, path) == ("POST", "/v1/items/WXYZ5678/attachments")
    assert body["mode"] == "imported_file"
    assert base64.b64decode(body["data_base64"]) == pdf_bytes


# ---------------------------------------------------------------------------
# Registration: descriptions say what is written, the grant prompt, and local only
# ---------------------------------------------------------------------------

REQUIRED = {
    "zotero_create_collection": ["name"],
    "zotero_move_collection": ["key"],
    "zotero_move_item": ["item_key"],
    "zotero_create_webpage": ["title", "url"],
    "zotero_attach_pdf": ["item_key", "path"],
    "zotero_add_child_note": ["parent_key", "html", "dedup_marker"],
    "zotero_batch": ["operations"],
}


def test_every_zotero_tool_is_listed_with_its_required_arguments():
    by_name = {tool["name"]: tool for tool in _MCP_TOOLS_LIST}
    for name, required in REQUIRED.items():
        schema = by_name[name]["inputSchema"]
        assert schema["type"] == "object", name
        assert schema["required"] == required, name


@pytest.mark.parametrize("name", ALL_TOOLS)
def test_every_zotero_description_says_local_only_grant_prompt_and_what_it_writes(name):
    by_name = {tool["name"]: tool for tool in _MCP_TOOLS_LIST}
    description = by_name[name]["description"]
    assert "Local only: refused on hosted Meridian." in description
    assert "grant prompt" in description
    assert "person must approve" in description
    assert "Zotero Bridge plugin" in description
    assert "Writes to the Zotero" in description


def test_the_path_argument_of_attach_pdf_is_documented_as_absolute():
    by_name = {tool["name"]: tool for tool in _MCP_TOOLS_LIST}
    assert "absolute" in by_name["zotero_attach_pdf"]["description"]
    path_description = by_name["zotero_attach_pdf"]["inputSchema"]["properties"]["path"]["description"]
    assert "absolute" in path_description.lower()


def test_batch_schema_bounds_the_operation_count_to_the_client_limit():
    schema = next(t for t in _MCP_TOOLS_LIST if t["name"] == "zotero_batch")["inputSchema"]
    operations = schema["properties"]["operations"]
    assert operations["minItems"] == 1
    assert operations["maxItems"] == 200
    assert operations["items"]["required"] == ["method", "path"]
