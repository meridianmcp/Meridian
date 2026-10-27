"""The eight local-only Zotero bridge write tools (meridian/mcp/handlers/zotero_bridge_tools.py).

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
from meridian import db as db_module
from meridian import member_policy
from meridian import zotero_bridge_transport as transport_mod
from meridian.mcp import handler as mh
from meridian.mcp.handlers import zotero_bridge_tools as handlers
from meridian.mcp_tools import _MCP_TOOLS_LIST
from meridian.zotero_bridge_client import (
    ALREADY_ATTACHED,
    ALREADY_PRESENT,
    BatchRolledBackError,
    CollectionChain,
    GrantError,
    ZoteroBridgeClient,
    ZoteroBridgeError,
)
from zotero_bridge_fakes import URL, FakeLibrary, LibraryReader
from zotero_bridge_fakes import make as make_library_client

ALL_TOOLS = (
    "zotero_create_collection",
    "zotero_ensure_collection_chain",
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
    "zotero_ensure_collection_chain": {"project_name": "Project A", "subproject_name": "Methods"},
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

    def ensure_collection(self, name, parent_key=None):
        return self._record("ensure_collection", name, parent_key)

    def ensure_collection_chain(self, project_name, subproject_name=None):
        return self._record("ensure_collection_chain", project_name, subproject_name)

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
        {"name": "Reading list", "parent_key": "ABCD1234", "reuse_existing": False},
        ("create_collection", ("Reading list", "ABCD1234")),
        "NEWCOLL01",
        {"collection_key": "NEWCOLL01", "status": "created"},
    ),
    (
        "zotero_create_collection",
        {"name": "Reading list", "parent_key": "ABCD1234"},
        ("ensure_collection", ("Reading list", "ABCD1234")),
        ("already_exists", "ABCD1234"),
        {"collection_key": "ABCD1234", "status": "already_exists"},
    ),
    (
        "zotero_create_collection",
        {"name": "Top level", "reuse_existing": None},
        ("ensure_collection", ("Top level", None)),
        ("created", "NEWCOLL04"),
        {"collection_key": "NEWCOLL04", "status": "created"},
    ),
    (
        "zotero_ensure_collection_chain",
        {"project_name": "Project A", "subproject_name": "Methods"},
        ("ensure_collection_chain", ("Project A", "Methods")),
        CollectionChain("PROJ0001", "already_exists", "METH0001", "created"),
        {
            "project_key": "PROJ0001",
            "project_status": "already_exists",
            "subproject_key": "METH0001",
            "subproject_status": "created",
        },
    ),
    (
        "zotero_ensure_collection_chain",
        {"project_name": "Project B"},
        ("ensure_collection_chain", ("Project B", None)),
        CollectionChain("PROJ0002", "created", None, None),
        {
            "project_key": "PROJ0002",
            "project_status": "created",
            "subproject_key": None,
            "subproject_status": None,
        },
    ),
    (
        "zotero_create_collection",
        {"name": "Top level", "reuse_existing": False},
        ("create_collection", ("Top level", None)),
        "NEWCOLL02",
        {"collection_key": "NEWCOLL02", "status": "created"},
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
        ("created", "WEB00001"),
        {"item_key": "WEB00001", "status": "created"},
    ),
    (
        "zotero_create_webpage",
        {"title": "Pricing", "url": "https://example.com/pricing", "collection_keys": ["ABCD1234"]},
        ("create_webpage", ("Pricing", "https://example.com/pricing", ["ABCD1234"])),
        ("already_present", "WEB00001"),
        {"item_key": "WEB00001", "status": "already_present"},
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
    fake.returns["ensure_collection"] = ("created", "NEWCOLL01")

    payload = await mh._dispatch_mcp_tool(
        "zotero_create_collection", {"name": "Reading list"}, db, str(tmp_path)
    )

    assert payload == {"ok": True, "error": None, "collection_key": "NEWCOLL01", "status": "created"}
    assert fake.calls == [("ensure_collection", ("Reading list", None))]


@pytest.mark.asyncio
async def test_reuse_existing_true_routes_to_ensure_and_reports_already_exists(fake):
    fake.returns["ensure_collection"] = ("already_exists", "ABCD1234")

    payload = await handlers.handle_zotero_create_collection(
        {"name": "Reading list", "parent_key": "PARENT01", "reuse_existing": True}
    )

    assert payload == {"ok": True, "error": None, "collection_key": "ABCD1234", "status": "already_exists"}
    assert fake.calls == [("ensure_collection", ("Reading list", "PARENT01"))]


@pytest.mark.asyncio
async def test_reuse_existing_true_reports_created_when_nothing_matched(fake):
    fake.returns["ensure_collection"] = ("created", "NEWCOLL03")

    payload = await handlers.handle_zotero_create_collection({"name": "Reading list", "reuse_existing": True})

    assert payload == {"ok": True, "error": None, "collection_key": "NEWCOLL03", "status": "created"}
    assert fake.calls == [("ensure_collection", ("Reading list", None))]


@pytest.mark.asyncio
async def test_reuse_existing_that_is_not_a_boolean_is_refused_before_any_client_call(fake):
    payload = await handlers.handle_zotero_create_collection({"name": "Reading list", "reuse_existing": "yes"})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_input"
    assert "reuse_existing must be true or false" in payload["error"]["message"]
    assert fake.calls == []


@pytest.mark.asyncio
async def test_an_ambiguous_collection_comes_back_with_its_code_and_nothing_is_created(fake):
    fake.error = ZoteroBridgeError("ambiguous_collection", "More than one collection has that exact name.")

    payload = await handlers.handle_zotero_create_collection({"name": "Methods", "reuse_existing": True})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "ambiguous_collection"


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
    "zotero_ensure_collection_chain": ["project_name"],
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


@pytest.mark.asyncio
async def test_zotero_batch_refuses_an_unlisted_route_through_the_real_client_before_any_send(monkeypatch):
    sent: list[tuple[str, str]] = []

    def transport(method, path, headers, body):
        sent.append((method, path))
        return 200, {}

    real = ZoteroBridgeClient(transport=transport, reader=lambda path: (200, []), sleep=lambda seconds: None)
    monkeypatch.setattr(transport_mod, "get_client", lambda: real)

    payload = await handlers.handle_zotero_batch(
        {"operations": [{"method": "POST", "path": "/v1/grants", "body": {"app_name": "x"}}]}
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_batchable"
    assert sent == []


# ---------------------------------------------------------------------------
# Reruns through the MCP handlers: the real client over the in-memory library from zotero_bridge_fakes.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_rerun_through_the_mcp_handlers_finds_everything_and_writes_nothing(monkeypatch):
    library = FakeLibrary()
    client, _, _ = make_library_client(library)
    monkeypatch.setattr(transport_mod, "get_client", lambda: client)

    first_collection = await handlers.handle_zotero_create_collection({"name": "Reading list"})
    first_page = await handlers.handle_zotero_create_webpage(
        {"title": "Pricing", "url": URL, "collection_keys": [first_collection["collection_key"]]}
    )
    assert (first_collection["status"], first_page["status"]) == ("created", "created")
    assert library.created == ["collection", "item"]

    second_collection = await handlers.handle_zotero_create_collection({"name": "Reading list"})
    second_page = await handlers.handle_zotero_create_webpage(
        {"title": "Pricing", "url": URL, "collection_keys": [second_collection["collection_key"]]}
    )

    assert second_collection == {
        "ok": True,
        "error": None,
        "collection_key": first_collection["collection_key"],
        "status": "already_exists",
    }
    assert second_page == {
        "ok": True,
        "error": None,
        "item_key": first_page["item_key"],
        "status": "already_present",
    }
    assert library.created == ["collection", "item"]


@pytest.mark.asyncio
async def test_an_ambiguous_same_name_comes_back_as_ambiguous_collection_with_no_write(monkeypatch):
    library = FakeLibrary()
    library.seed_collection("Methods")
    library.seed_collection("Methods")
    client, bridge, _ = make_library_client(library)
    monkeypatch.setattr(transport_mod, "get_client", lambda: client)

    payload = await handlers.handle_zotero_create_collection({"name": "Methods"})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "ambiguous_collection"
    assert library.created == [] and bridge.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "arguments"),
    [
        (handlers.handle_zotero_create_collection, {"name": "Reading list"}),
        (handlers.handle_zotero_ensure_collection_chain, {"project_name": "Project A", "subproject_name": "Methods"}),
    ],
)
async def test_the_default_duplicate_check_stops_with_reader_failed_when_the_local_api_is_off(
    monkeypatch, handler, arguments
):
    library = FakeLibrary()
    client, bridge, _ = make_library_client(library, reader=LibraryReader(library, fail_status=500))
    monkeypatch.setattr(transport_mod, "get_client", lambda: client)

    payload = await handler(dict(arguments))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "reader_failed"
    assert library.created == [] and bridge.calls == []


@pytest.mark.asyncio
async def test_create_collection_with_reuse_existing_false_does_not_read_so_a_failing_reader_does_not_stop_it(monkeypatch):
    library = FakeLibrary()
    reader = LibraryReader(library, fail_status=500)
    client, _, _ = make_library_client(library, reader=reader)
    monkeypatch.setattr(transport_mod, "get_client", lambda: client)

    payload = await handlers.handle_zotero_create_collection({"name": "Reading list", "reuse_existing": False})

    assert (payload["ok"], payload["status"]) == (True, "created")
    assert reader.paths == [] and library.count("collection") == 1


@pytest.mark.asyncio
async def test_the_chain_tool_second_call_creates_nothing_and_reports_already_exists_at_each_level(monkeypatch):
    library = FakeLibrary()
    client, bridge, _ = make_library_client(library)
    monkeypatch.setattr(transport_mod, "get_client", lambda: client)
    arguments = {"project_name": "Project A", "subproject_name": "Methods"}

    first = await handlers.handle_zotero_ensure_collection_chain(dict(arguments))

    assert first["ok"] is True
    assert (first["project_status"], first["subproject_status"]) == ("created", "created")
    assert library.created == ["collection", "collection"]
    grants_before = [call for call in bridge.calls if call[1] == "/v1/grants"]

    second = await handlers.handle_zotero_ensure_collection_chain(dict(arguments))

    assert second == {
        "ok": True,
        "error": None,
        "project_key": first["project_key"],
        "project_status": "already_exists",
        "subproject_key": first["subproject_key"],
        "subproject_status": "already_exists",
    }
    assert library.created == ["collection", "collection"]
    assert [call for call in bridge.calls if call[1] == "/v1/grants"] == grants_before


@pytest.mark.asyncio
async def test_the_mcp_dispatcher_reaches_the_chain_tool(db, tmp_path, fake):
    fake.returns["ensure_collection_chain"] = CollectionChain("PROJ0001", "created", None, None)

    payload = await mh._dispatch_mcp_tool(
        "zotero_ensure_collection_chain", {"project_name": "Project A"}, db, str(tmp_path)
    )

    assert payload == {
        "ok": True,
        "error": None,
        "project_key": "PROJ0001",
        "project_status": "created",
        "subproject_key": None,
        "subproject_status": None,
    }
    assert fake.calls == [("ensure_collection_chain", ("Project A", None))]


@pytest.mark.asyncio
async def test_hosted_refusal_holds_through_the_dispatcher_for_the_chain_tool(db, tmp_path, monkeypatch, fake):
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")

    payload = await mh._dispatch_mcp_tool(
        "zotero_ensure_collection_chain", {"project_name": "Project A"}, db, str(tmp_path)
    )

    assert payload["error"]["code"] == "hosted_refused"
    assert fake.built == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("project_name", ["Out of scope project", "No such project"])
async def test_hosted_refusal_holds_for_a_scoped_caller_whether_or_not_the_name_is_a_meridian_project(
    db, tmp_path, monkeypatch, fake, project_name
):
    # A hosted member with no projects in scope, who holds the project-write capability the chain tool is checked
    # against. The chain tool's project_name is a Zotero collection name, so an existing Meridian project name must not
    # change the answer: both names come back as hosted_refused, not as a scope error or a no-match error.
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    await db_module.create_project(db, "Out of scope project")
    member = member_policy.ScopedProjectIds(
        (),
        policy=member_policy.EffectivePolicy(
            caps=frozenset({member_policy.WRITE_PROJECT}), role=member_policy.ROLE_MEMBER,
        ),
    )

    payload = await mh._dispatch_mcp_tool(
        "zotero_ensure_collection_chain", {"project_name": project_name}, db, str(tmp_path), scoped_project_ids=member,
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "hosted_refused"
    assert fake.built == 0


@pytest.mark.asyncio
async def test_the_chain_tool_is_refused_on_hosted_before_any_read_or_write(monkeypatch):
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    reads: list[str] = []
    sent: list[tuple[str, str]] = []

    def transport(method, path, headers, body):
        sent.append((method, path))
        return 200, {}

    def reader(path):
        reads.append(path)
        return 200, []

    real = ZoteroBridgeClient(transport=transport, reader=reader, sleep=lambda seconds: None)
    monkeypatch.setattr(transport_mod, "get_client", lambda: real)

    payload = await handlers.handle_zotero_ensure_collection_chain(
        {"project_name": "Project A", "subproject_name": "Methods"}
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "hosted_refused"
    assert reads == [] and sent == []


def test_reuse_existing_defaults_to_true_in_the_schema_and_the_description():
    by_name = {tool["name"]: tool for tool in _MCP_TOOLS_LIST}
    tool = by_name["zotero_create_collection"]

    assert "Defaults to true" in tool["inputSchema"]["properties"]["reuse_existing"]["description"]
    assert "By default (`reuse_existing` true)" in tool["description"]
    assert "defaults to false" not in tool["description"].lower()
    assert "reader_failed and nothing is written" in tool["description"]


def test_batch_description_says_creates_are_not_deduplicated_and_documented_not_refused():
    description = next(t for t in _MCP_TOOLS_LIST if t["name"] == "zotero_batch")["description"]

    assert "POST /v1/items" in description and "POST /v1/collections" in description
    assert "not deduplicated inside a batch" in description
    assert "a rerun of the same batch writes them again" in description
    assert "documented here, not refused" in description
    assert "zotero_create_collection with `reuse_existing` left at its default of true" in description
