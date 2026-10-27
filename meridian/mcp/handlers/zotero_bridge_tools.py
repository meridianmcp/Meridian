"""Per-tool handlers for the Zotero bridge write tools: zotero_create_collection, zotero_ensure_collection_chain,
zotero_move_collection, zotero_move_item, zotero_create_webpage, zotero_attach_pdf, zotero_add_child_note and
zotero_batch. Local only: hosted Meridian refuses all eight.

Each handler is a thin call into the process-wide ZoteroBridgeClient (meridian/zotero_bridge_transport.py). The client
validates the input, runs the duplicate checks and the grant flow, and raises ZoteroBridgeError carrying the bridge's
error code. A failure comes back as an ``error`` object with that code and the client's scrubbed message. No traceback,
token or grant key is returned.

HOSTED REFUSAL. The check runs before any client is built and before any path is read, the same as the runpod tools. A
hosted server does not sit beside the caller's Zotero library, and it must not write to one on a caller's request.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from meridian import zotero_bridge_transport as bridge
from meridian._deps import _hosted_mode as _deps_hosted_mode
from meridian.zotero_bridge_client import (
    ALREADY_ATTACHED,
    ALREADY_PRESENT,
    CREATED,
    BatchRolledBackError,
    ZoteroBridgeClient,
    ZoteroBridgeError,
)

_HOSTED_ERROR_CODE = "hosted_refused"
_HOSTED_MESSAGE = (
    "Zotero bridge writes are not available on hosted Meridian: the Zotero library and its bridge run on the caller's "
    "own machine, which a hosted server cannot reach. Run Meridian self-hosted."
)
_UNEXPECTED_CODE = "unexpected_error"


def _hosted_mode() -> bool:
    """Same answer as handler.py's ``_hosted_mode``, copied from runpod_tools.py: the handler's binding when that module
    is loaded (so a test patch on the handler name still applies), else the ``_deps`` one. Both read MERIDIAN_HOSTED."""
    handler_mod = sys.modules.get("meridian.mcp.handler")
    patched = getattr(handler_mod, "_hosted_mode", None)
    return bool((patched or _deps_hosted_mode)())


def _refused() -> dict[str, Any]:
    return {"ok": False, "error": {"code": _HOSTED_ERROR_CODE, "message": _HOSTED_MESSAGE, "status": None}}


def _error_payload(error: ZoteroBridgeError) -> dict[str, Any]:
    body: dict[str, Any] = {"code": error.code, "message": error.message, "status": error.status}
    if isinstance(error, BatchRolledBackError):
        body["failed_index"] = error.failed_index
        body["inner_code"] = error.inner_code
    return {"ok": False, "error": body}


def _list_arg(args: dict[str, Any], key: str) -> list[Any]:
    """An optional list argument. Omitted or null is an empty list; anything else that is not a list is refused."""
    value = args.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ZoteroBridgeError("invalid_input", f"{key} must be a list of Zotero keys.")
    return value


def _flag_arg(args: dict[str, Any], key: str, default: bool = False) -> bool:
    """An optional true/false argument. Omitted or null takes ``default``.
    Anything else that is not a boolean is refused."""
    value = args.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ZoteroBridgeError("invalid_input", f"{key} must be true or false.")
    return value


async def _run(action: Callable[[ZoteroBridgeClient], dict[str, Any]]) -> dict[str, Any]:
    """Runs one client call in a worker thread (a grant wait can take minutes) and maps the outcome to a payload."""
    if _hosted_mode():
        return _refused()
    try:
        value = await asyncio.to_thread(bridge.call, action)
    except ZoteroBridgeError as error:
        return _error_payload(error)
    except Exception as error:  # noqa: BLE001 - the type name only; the text is not echoed, in case it holds a secret
        message = f"The call failed with {type(error).__name__}. Nothing more is shown."
        return {"ok": False, "error": {"code": _UNEXPECTED_CODE, "message": message, "status": None}}
    return {"ok": True, "error": None, **value}


async def handle_zotero_create_collection(args: dict[str, Any]) -> dict[str, Any]:
    """MCP tool: zotero_create_collection. Reuses a same-named collection under the same parent unless reuse_existing is
    false, which always creates one. Reuse is the default, so a rerun does not add a second collection."""

    def action(client: ZoteroBridgeClient) -> dict[str, Any]:
        if _flag_arg(args, "reuse_existing", default=True):
            status, key = client.ensure_collection(args.get("name"), args.get("parent_key"))
            return {"collection_key": key, "status": status}
        key = client.create_collection(args.get("name"), args.get("parent_key"))
        return {"collection_key": key, "status": CREATED}

    return await _run(action)


async def handle_zotero_ensure_collection_chain(args: dict[str, Any]) -> dict[str, Any]:
    """MCP tool: zotero_ensure_collection_chain. Maps a project to a top-level collection, and an optional subproject
    to a collection under it. Each level that already exists is reused and not written again."""

    def action(client: ZoteroBridgeClient) -> dict[str, Any]:
        chain = client.ensure_collection_chain(args.get("project_name"), args.get("subproject_name"))
        return {
            "project_key": chain.project_key,
            "project_status": chain.project_status,
            "subproject_key": chain.subproject_key,
            "subproject_status": chain.subproject_status,
        }

    return await _run(action)


async def handle_zotero_move_collection(args: dict[str, Any]) -> dict[str, Any]:
    """MCP tool: zotero_move_collection. Moves a collection under a parent, or to the top level when it is null."""

    def action(client: ZoteroBridgeClient) -> dict[str, Any]:
        client.move_collection(args.get("key"), args.get("parent_key"))
        return {"collection_key": args.get("key"), "parent_key": args.get("parent_key")}

    return await _run(action)


async def handle_zotero_move_item(args: dict[str, Any]) -> dict[str, Any]:
    """MCP tool: zotero_move_item. Adds an item to collections and removes it from others."""

    def action(client: ZoteroBridgeClient) -> dict[str, Any]:
        client.move_item(
            args.get("item_key"), _list_arg(args, "add_collections"), _list_arg(args, "remove_collections")
        )
        return {"item_key": args.get("item_key")}

    return await _run(action)


async def handle_zotero_create_webpage(args: dict[str, Any]) -> dict[str, Any]:
    """MCP tool: zotero_create_webpage. Creates a webpage item, optionally in collections."""

    def action(client: ZoteroBridgeClient) -> dict[str, Any]:
        status, key = client.create_webpage(args.get("title"), args.get("url"), _list_arg(args, "collection_keys"))
        return {"item_key": key, "status": status}

    return await _run(action)


async def handle_zotero_attach_pdf(args: dict[str, Any]) -> dict[str, Any]:
    """MCP tool: zotero_attach_pdf. Attaches a PDF file from this machine to an item, unless it already has one."""

    def action(client: ZoteroBridgeClient) -> dict[str, Any]:
        result = client.attach_pdf(args.get("item_key"), args.get("path"), args.get("title"))
        if result == ALREADY_ATTACHED:
            return {"status": "already_attached", "attachment_key": None}
        return {"status": "attached", "attachment_key": result}

    return await _run(action)


async def handle_zotero_add_child_note(args: dict[str, Any]) -> dict[str, Any]:
    """MCP tool: zotero_add_child_note. Adds a child note once, keyed by its dedup marker."""

    def action(client: ZoteroBridgeClient) -> dict[str, Any]:
        result = client.add_child_note(args.get("parent_key"), args.get("html"), args.get("dedup_marker"))
        if result == ALREADY_PRESENT:
            return {"status": "already_present", "note_key": None}
        return {"status": "created", "note_key": result}

    return await _run(action)


async def handle_zotero_batch(args: dict[str, Any]) -> dict[str, Any]:
    """MCP tool: zotero_batch. Runs 1 to 200 bridge operations in one transaction."""

    def action(client: ZoteroBridgeClient) -> dict[str, Any]:
        return {"results": client.batch(args.get("operations"))}

    return await _run(action)


ZOTERO_BRIDGE_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
    "zotero_create_collection": handle_zotero_create_collection,
    "zotero_ensure_collection_chain": handle_zotero_ensure_collection_chain,
    "zotero_move_collection": handle_zotero_move_collection,
    "zotero_move_item": handle_zotero_move_item,
    "zotero_create_webpage": handle_zotero_create_webpage,
    "zotero_attach_pdf": handle_zotero_attach_pdf,
    "zotero_add_child_note": handle_zotero_add_child_note,
    "zotero_batch": handle_zotero_batch,
}
