"""One-shot workstation sync for hosted citation markers and local attachments."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import re
import sys
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from .tunnel_config import get_zotero_collection_keys
from .zotero_client import (
    fetch_zotero_item_details,
    list_zotero_item_attachments,
    resolve_citation_ref,
)


_MAX_ITEMS = 500
_ITEM_KEY_RE = re.compile(r"^[A-Za-z0-9]{8}$")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_hosted_mcp_call() -> Callable[..., dict[str, Any]]:
    """Load the existing authenticated tools/call client from plugin-base."""
    try:
        module = importlib.import_module("meridian_plugin_base.ingest_client")
        return module.call_mcp_tool
    except ModuleNotFoundError as exc:
        if exc.name not in {"meridian_plugin_base", "meridian_plugin_base.ingest_client"}:
            raise

    package_root = _repo_root() / "packages" / "meridian-plugin-base"
    if package_root.is_dir() and str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))
        importlib.invalidate_caches()
    try:
        module = importlib.import_module("meridian_plugin_base.ingest_client")
    except ImportError as exc:
        raise RuntimeError(
            "hosted MCP client is unavailable; install meridian-plugin-base or run from a source checkout"
        ) from exc
    return module.call_mcp_tool


def _call_hosted_tool(
    tool_name: str,
    params: dict[str, Any],
    *,
    base_url: str | None = None,
    call_tool: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    caller = call_tool or _load_hosted_mcp_call()
    # The public client reads credentials from MERIDIAN_API_KEY/BEARER_TOKEN
    # and places them only in the Authorization header.
    return caller(tool_name, params, base_url=base_url)


def _load_outputs_registry() -> Any | None:
    """Load the Outputs package's public local artifact registry API."""
    try:
        return importlib.import_module("meridian_outputs.artifact_registry")
    except ModuleNotFoundError as exc:
        if exc.name not in {"meridian_outputs", "meridian_outputs.artifact_registry"}:
            return None

    extension_root = _repo_root() / "extensions" / "meridian-outputs"
    if not extension_root.is_dir():
        return None
    if str(extension_root) not in sys.path:
        sys.path.insert(0, str(extension_root))
        importlib.invalidate_caches()
    try:
        return importlib.import_module("meridian_outputs.artifact_registry")
    except ImportError:
        return None


def _safe_attachment_path(
    attachment: dict[str, Any], zotero_data_dir: str | Path | None
) -> Path | None:
    """Resolve a Zotero path locally without allowing storage traversal."""
    raw = attachment.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip()
    if raw.lower().startswith("storage:"):
        if zotero_data_dir is None:
            return None
        item_key = attachment.get("zotero_key")
        if not isinstance(item_key, str) or _ITEM_KEY_RE.fullmatch(item_key) is None:
            return None
        relative = raw[len("storage:"):].replace("\\", "/")
        parts = Path(relative).parts
        if not relative or Path(relative).is_absolute() or any(part in {".", ".."} for part in parts):
            return None
        storage_root = (Path(zotero_data_dir).expanduser() / "storage").resolve()
        candidate = (storage_root / item_key / relative).resolve()
        if storage_root not in candidate.parents:
            return None
        return candidate if candidate.is_file() else None

    path = Path(raw).expanduser()
    if not path.is_absolute() and not PureWindowsPath(raw).is_absolute():
        return None
    return path if path.is_file() else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _register_attachment(
    attachment: dict[str, Any],
    path: Path,
    outputs_dir: str | Path,
    registry: Any,
) -> bool:
    key = attachment.get("zotero_key")
    parent_key = attachment.get("parent_key")
    if (
        not isinstance(key, str)
        or _ITEM_KEY_RE.fullmatch(key) is None
        or not isinstance(parent_key, str)
        or _ITEM_KEY_RE.fullmatch(parent_key) is None
    ):
        return False
    source_locator = f"zotero:user/0/item/{parent_key}/attachment/{key}"
    digest = _sha256_file(path)
    metadata = {
        "source": "zotero",
        "parent_item_key": parent_key,
        "attachment_key": key,
        "parent_version": attachment.get("parent_version"),
        "attachment_version": attachment.get("version"),
        "filename": attachment.get("filename"),
        "content_type": attachment.get("content_type"),
        "sha256": digest,
    }
    try:
        record = registry.register_artifact(
            str(outputs_dir),
            "document",
            canonical_path=str(path),
            expected_sha256=digest,
            generator="zotero-sync",
            source_locator=source_locator,
            metadata=metadata,
        )
        artifact_id = record.get("artifact_id") if isinstance(record, dict) else None
        if not isinstance(artifact_id, str) or not artifact_id:
            return False
        registry.bind_source_edge(
            str(outputs_dir), artifact_id, source_locator,
            relation="zotero_attachment",
            metadata={"parent_item_key": parent_key, "attachment_key": key},
        )
        verification = registry.verify_artifact_hash(str(outputs_dir), artifact_id, path=str(path))
        return isinstance(verification, dict) and verification.get("verified") is True
    except Exception:  # noqa: BLE001 — an unavailable local ledger stays unverified
        return False


def _is_unsafe_direct_item_ref(ref: str) -> bool:
    match = re.match(r"^\s*zotero\s*:", ref, re.IGNORECASE)
    if match is None:
        return False
    key = ref[match.end():].strip()
    return _ITEM_KEY_RE.fullmatch(key) is None


async def sync_zotero_citations(
    project_id: str,
    *,
    max_items: int = 100,
    dry_run: bool = False,
    server: str | None = None,
    zotero_data_dir: str | Path | None = None,
    outputs_dir: str | Path | None = None,
    call_tool: Callable[..., dict[str, Any]] | None = None,
    resolver: Callable[..., Any] | None = None,
    item_lookup: Callable[..., Any] | None = None,
    attachment_lookup: Callable[..., Any] | None = None,
    registry: Any | None = None,
) -> dict[str, Any]:
    """Resolve and apply one bounded batch of hosted citation markers."""
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id is required")
    if not isinstance(max_items, int) or isinstance(max_items, bool) or not 1 <= max_items <= _MAX_ITEMS:
        raise ValueError(f"max_items must be between 1 and {_MAX_ITEMS}")

    remote_call = lambda name, arguments: _call_hosted_tool(  # noqa: E731
        name, arguments, base_url=server, call_tool=call_tool,
    )
    pending_response = remote_call(
        "get_pending_zotero_citations",
        {"project_id": project_id, "max_items": max_items},
    )
    markers = pending_response.get("markers") if isinstance(pending_response, dict) else None
    if not isinstance(markers, list):
        raise RuntimeError("hosted response did not include a citation marker list")

    selected = get_zotero_collection_keys()
    resolve = resolver or resolve_citation_ref
    lookup_item = item_lookup or fetch_zotero_item_details
    list_attachments = attachment_lookup or list_zotero_item_attachments
    registry_api = registry if registry is not None else _load_outputs_registry()
    resolutions: list[dict[str, Any]] = []
    unresolved = 0
    attachments_seen = 0
    attachments_registered = 0
    attachments_unverified = 0

    for marker in markers[:max_items]:
        if not isinstance(marker, dict):
            unresolved += 1
            continue
        element_id = marker.get("id") or marker.get("element_id")
        ref = marker.get("ref")
        if (
            not isinstance(element_id, str)
            or not element_id.strip()
            or len(element_id) > 200
            or not isinstance(ref, str)
            or not ref.strip()
            or len(ref) > 500
            or _is_unsafe_direct_item_ref(ref)
        ):
            unresolved += 1
            continue
        ref = ref.strip()
        item = await resolve(ref)
        if not isinstance(item, dict):
            unresolved += 1
            continue
        zotero_key = item.get("zotero_key")
        if not isinstance(zotero_key, str) or _ITEM_KEY_RE.fullmatch(zotero_key) is None:
            unresolved += 1
            continue

        details = await lookup_item(zotero_key)
        collection_keys = (
            details.get("collection_keys", [])
            if isinstance(details, dict) and isinstance(details.get("collection_keys"), list)
            else []
        )
        collection_keys = [
            key.strip().upper()
            for key in collection_keys
            if isinstance(key, str) and _ITEM_KEY_RE.fullmatch(key.strip())
        ]
        if selected and not set(selected).intersection(collection_keys):
            # Direct Zotero-key refs bypass the collection list endpoint; the
            # metadata check keeps those refs inside the user's local scope too.
            unresolved += 1
            continue

        item_version = details.get("version") if isinstance(details, dict) else None
        title = item.get("title")
        if not isinstance(title, str) and isinstance(details, dict):
            title = details.get("title")
        doi = item.get("doi")
        if not isinstance(doi, str) and isinstance(details, dict):
            doi = details.get("doi")

        resolutions.append({
            "element_id": element_id.strip(),
            "ref": ref,
            "zotero_key": zotero_key,
            "doi": doi if isinstance(doi, str) else None,
            "title": title if isinstance(title, str) else None,
            "version": item_version,
            "collection_keys": collection_keys,
        })

        parent_key = zotero_key
        if isinstance(details, dict) and isinstance(details.get("parent_key"), str):
            parent_key = details["parent_key"]
        child_records = await list_attachments(parent_key)
        if isinstance(details, dict) and details.get("item_type") == "attachment":
            child_records = [details]
            parent_key = details.get("parent_key") or zotero_key
        if not isinstance(child_records, list):
            child_records = []
        for child in child_records:
            if not isinstance(child, dict):
                continue
            attachments_seen += 1
            attachment = {
                **child,
                "parent_key": child.get("parent_key") or parent_key,
                "parent_version": item_version,
            }
            path = _safe_attachment_path(attachment, zotero_data_dir)
            if dry_run or outputs_dir is None or registry_api is None or path is None:
                attachments_unverified += 1
                continue
            if _register_attachment(attachment, path, outputs_dir, registry_api):
                attachments_registered += 1
            else:
                attachments_unverified += 1

    write_summary: dict[str, Any] = {
        "applied": 0,
        "cross_doc_linked": 0,
        "stale": 0,
        "already_resolved": 0,
        "rejected": 0,
    }
    if resolutions and not dry_run:
        write_summary = remote_call(
            "apply_zotero_citation_edges",
            {
                "project_id": project_id,
                "selected_collection_keys": selected,
                "resolutions": resolutions,
            },
        )

    return {
        "project_id": project_id,
        "candidate_count": len(markers),
        "processed": min(len(markers), max_items),
        "resolved_locally": len(resolutions),
        "unresolved": unresolved,
        "remaining_candidates": pending_response.get("has_more", False) is True,
        "dry_run": bool(dry_run),
        "attachments_seen": attachments_seen,
        "attachments_registered": attachments_registered,
        "attachments_unverified": attachments_unverified,
        **write_summary,
    }


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="meridian zotero")
    subparsers = parser.add_subparsers(dest="action", required=True)
    sync_parser = subparsers.add_parser("sync", help="run one local Zotero sync pass")
    sync_parser.add_argument("--project-id", required=True)
    sync_parser.add_argument("--max-items", type=int, default=100)
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument("--server", default=None, help="Meridian base URL (defaults to MERIDIAN_URL)")
    sync_parser.add_argument("--outputs-dir", default=os.environ.get("MERIDIAN_OUTPUTS_DIR"))
    sync_parser.add_argument("--zotero-data-dir", default=os.environ.get("ZOTERO_DATA_DIR"))
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(sync_zotero_citations(
            args.project_id,
            max_items=args.max_items,
            dry_run=args.dry_run,
            server=args.server,
            outputs_dir=args.outputs_dir,
            zotero_data_dir=args.zotero_data_dir,
        ))
    except Exception as exc:  # noqa: BLE001 — CLI reports a concise actionable error
        print(f"zotero sync failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0
