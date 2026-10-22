"""Authenticated hosted writes for workstation-local Zotero resolution."""

from __future__ import annotations

import re
from typing import Any


_ITEM_KEY_RE = re.compile(r"^[A-Za-z0-9]{8}$")
_MAX_BATCH = 500


def _clean_collection_keys(value: Any) -> list[str] | None:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 100:
        return None
    keys: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or _ITEM_KEY_RE.fullmatch(entry.strip()) is None:
            return None
        keys.append(entry.strip().upper())
    return list(dict.fromkeys(keys))


async def handle_get_pending_zotero_citations(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> dict[str, Any]:
    """Return a bounded page of unresolved markers for local Zotero lookup."""
    project_id = args.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        return {"error": "project_id is required"}
    max_items = args.get("max_items", 100)
    if (
        not isinstance(max_items, int)
        or isinstance(max_items, bool)
        or not 1 <= max_items <= _MAX_BATCH
    ):
        return {"error": f"max_items must be between 1 and {_MAX_BATCH}"}

    from meridian.mcp.handler import _resolve_ingest_doc_store  # noqa: PLC0415

    store = await _resolve_ingest_doc_store(db, data_dir, tenant)
    if store is None:
        return {"project_id": project_id, "markers": [], "error": "document-structure store unavailable"}
    try:
        result = await store.get_pending_zotero_citations(
            project_id, max_items=max_items,
        )
    except Exception as exc:  # noqa: BLE001 — one read must not break MCP
        return {"project_id": project_id, "markers": [], "error": f"could not read pending citations: {exc}"}
    return {"project_id": project_id, **result}


async def handle_apply_zotero_citation_edges(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> dict[str, Any]:
    """Validate local results and persist only project-bound citation edges."""
    project_id = args.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        return {"error": "project_id is required"}
    if set(args) - {"project_id", "project_name", "resolutions", "selected_collection_keys"}:
        return {"error": "unexpected argument; local paths and credentials are never accepted"}

    selected = _clean_collection_keys(args.get("selected_collection_keys"))
    if selected is None:
        return {"error": "selected_collection_keys must contain Zotero collection keys"}
    raw_resolutions = args.get("resolutions")
    if not isinstance(raw_resolutions, list) or len(raw_resolutions) > _MAX_BATCH:
        return {"error": f"resolutions must contain at most {_MAX_BATCH} items"}

    resolutions: list[dict[str, Any]] = []
    allowed_fields = {
        "element_id", "ref", "zotero_key", "doi", "title", "version", "collection_keys",
    }
    for item in raw_resolutions:
        if not isinstance(item, dict) or set(item) - allowed_fields:
            return {"error": "each resolution must contain only marker and Zotero identity fields"}
        element_id = item.get("element_id")
        ref = item.get("ref")
        zotero_key = item.get("zotero_key")
        if not isinstance(element_id, str) or not element_id.strip() or len(element_id) > 200:
            return {"error": "resolution element_id is invalid"}
        if not isinstance(ref, str) or not ref.strip() or len(ref) > 500:
            return {"error": "resolution ref is invalid"}
        if not isinstance(zotero_key, str) or _ITEM_KEY_RE.fullmatch(zotero_key) is None:
            return {"error": "resolution zotero_key is invalid"}
        doi = item.get("doi")
        if doi is not None and (
            not isinstance(doi, str)
            or len(doi) > 500
            or not all(character.isprintable() for character in doi)
        ):
            return {"error": "resolution doi is invalid"}
        title = item.get("title")
        if title is not None and (
            not isinstance(title, str)
            or len(title) > 1000
            or not all(character.isprintable() for character in title)
        ):
            return {"error": "resolution title is invalid"}
        version = item.get("version")
        if version is not None and (
            not isinstance(version, int) or isinstance(version, bool) or version < 0
        ):
            return {"error": "resolution version is invalid"}
        item_collections = _clean_collection_keys(item.get("collection_keys"))
        if item_collections is None:
            return {"error": "resolution collection_keys is invalid"}

        # Only the server needed fields are retained. File paths, attachment
        # metadata, API keys, and raw Zotero objects cannot enter DocStore.
        resolutions.append({
            "element_id": element_id.strip(),
            "ref": ref.strip(),
            "zotero_key": zotero_key,
            "doi": doi.strip() if isinstance(doi, str) and doi.strip() else None,
            "title": title.strip() if isinstance(title, str) and title.strip() else None,
            "version": version,
            "collection_keys": item_collections,
        })

    from meridian.mcp.handler import _resolve_ingest_doc_store  # noqa: PLC0415

    store = await _resolve_ingest_doc_store(db, data_dir, tenant)
    if store is None:
        return {"error": "document-structure store unavailable"}
    try:
        summary = await store.apply_resolved_zotero_edges(
            project_id, resolutions, max_items=_MAX_BATCH,
        )
    except Exception as exc:  # noqa: BLE001 — invalid/stale data must not crash MCP
        return {"error": f"could not apply citation results: {exc}"}
    return {"project_id": project_id, **summary}
