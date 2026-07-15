"""Stdlib-only HTTP client for the hosted Meridian /mcp endpoint.

This module is the CANONICAL home for the urllib-based JSON-RPC wrapper that
plugins use to call back to the hosted Meridian server. It was previously
duplicated (and would have been duplicated again in every new plugin) inside:
  - extensions/meridian-docs/meridian_docs/local_ingest.py (_call_mcp_tool,
    call_hosted_ingest, call_hosted_ingest_structure)

When meridian-plugin-base is published to PyPI, future plugins (meridian-research,
meridian-figma, ...) declare `meridian-plugin-base>=0.1` and import from here
instead of vendoring their own copy.

Pure stdlib — no httpx, no requests, no third-party deps.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

__all__ = [
    "MeridianClientError",
    "call_mcp_tool",
    "call_hosted_ingest",
    "call_hosted_ingest_structure",
    "resolve_base_url",
    "resolve_token",
]

_DEFAULT_BASE_URL = "https://usemeridian.us"


class MeridianClientError(Exception):
    """Raised on network, HTTP, or server-side errors calling the hosted /mcp endpoint."""


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def resolve_base_url() -> str:
    """Resolve the Meridian server URL from environment (MERIDIAN_URL env var)."""
    return (os.environ.get("MERIDIAN_URL") or _DEFAULT_BASE_URL).rstrip("/")


def resolve_token() -> str:
    """Resolve the API token from environment: MERIDIAN_API_KEY > BEARER_TOKEN."""
    token = os.environ.get("MERIDIAN_API_KEY") or os.environ.get("BEARER_TOKEN") or ""
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


# ---------------------------------------------------------------------------
# Core JSON-RPC caller
# ---------------------------------------------------------------------------

def call_mcp_tool(
    tool_name: str,
    params: dict[str, Any],
    base_url: str | None = None,
    token: str | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    """POST a ``tools/call`` JSON-RPC call to the hosted Meridian /mcp endpoint.

    Uses stdlib ``urllib.request`` only (no httpx/requests dependency).

    ``base_url`` defaults to ``MERIDIAN_URL`` env var (falls back to
    ``https://usemeridian.us``). ``token`` defaults to ``MERIDIAN_API_KEY`` or
    ``BEARER_TOKEN`` env vars.

    Returns the unwrapped result dict (MCP content envelope stripped), or raises
    :class:`MeridianClientError` describing the network/HTTP/server-side failure.

    NOTE on User-Agent: Python's default "Python-urllib/3.x" UA is blocked by
    the Cloudflare WAF (error 1010 / browser_signature_banned). Every Meridian
    client that hits usemeridian.us sets a non-Python UA. This function follows
    that convention.
    """
    url = (base_url or resolve_base_url()) + "/mcp"
    tok = token if token is not None else resolve_token()

    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": params,
        },
    }).encode()

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "meridian-plugin/1.0",
    }
    if tok:
        headers["Authorization"] = f"Bearer {tok}"

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            body = str(exc)
        raise MeridianClientError(
            f"hosted {tool_name} returned HTTP {exc.code}: {body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise MeridianClientError(
            f"could not reach Meridian at {url}: {exc.reason}"
        ) from exc

    # The /mcp endpoint may return Streamable HTTP (SSE) or plain JSON.
    result_data: Any = None
    if "data:" in raw:
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk:
                    try:
                        result_data = json.loads(chunk)
                    except json.JSONDecodeError:
                        pass
    else:
        try:
            result_data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MeridianClientError(
                f"unexpected response from hosted Meridian: {raw[:500]}"
            ) from exc

    if result_data is None:
        raise MeridianClientError(
            f"no parseable result in hosted Meridian response: {raw[:500]}"
        )

    if isinstance(result_data, dict) and "error" in result_data:
        err = result_data["error"]
        raise MeridianClientError(f"hosted {tool_name} error: {err}")

    rpc_result: Any = (
        result_data.get("result", result_data)
        if isinstance(result_data, dict)
        else result_data
    )

    # Unwrap MCP tool-result envelope: {content: [{type: "text", text: "<json>"}]}
    if isinstance(rpc_result, dict) and "content" in rpc_result:
        content_items = rpc_result["content"]
        if isinstance(content_items, list) and content_items:
            first = content_items[0]
            if isinstance(first, dict) and first.get("type") == "text":
                text_val = first.get("text", "")
                try:
                    rpc_result = json.loads(text_val)
                except json.JSONDecodeError:
                    rpc_result = {"text": text_val}

    if isinstance(rpc_result, dict) and rpc_result.get("error"):
        raise MeridianClientError(
            f"hosted {tool_name} returned error: {rpc_result['error']}"
        )

    return rpc_result if isinstance(rpc_result, dict) else {"result": rpc_result}


# ---------------------------------------------------------------------------
# Meridian-specific tool wrappers
# ---------------------------------------------------------------------------

def call_hosted_ingest(
    project_id: str,
    content: str,
    title: str | None = None,
    source: str | None = None,
    tags: str | None = None,
    base_url: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """POST an ``ingest_document`` call to the hosted Meridian /mcp endpoint.

    Populates the FLAT note store (searchable via search_all / search_synthesis).
    To also populate the structural doc-store, call :func:`call_hosted_ingest_structure`
    with the same ``source`` value after this call.
    """
    p: dict[str, Any] = {"project_id": project_id, "content": content}
    if title is not None:
        p["title"] = title
    if source is not None:
        p["source"] = source
    if tags is not None:
        p["tags"] = tags
    return call_mcp_tool("ingest_document", p, base_url=base_url, token=token)


def call_hosted_ingest_structure(
    project_id: str,
    source: str,
    blocks: list[dict[str, Any]],
    title: str | None = None,
    doc_type: str = "docx",
    base_url: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """POST an ``ingest_document_structure`` call to hosted Meridian.

    Forwards the raw ``blocks`` list from :func:`meridian_plugin_base.ooxml.document_content_tree`
    to the hosted server to populate the structural doc-store
    (doc_documents / doc_elements / doc_figures / doc_tables rows).

    ``source`` MUST match the source used in :func:`call_hosted_ingest` for the
    same file so that ``find_similar_figure`` can resolve the correct document_id.
    """
    p: dict[str, Any] = {
        "project_id": project_id,
        "source": source,
        "blocks": blocks,
        "doc_type": doc_type,
    }
    if title is not None:
        p["title"] = title
    return call_mcp_tool("ingest_document_structure", p, base_url=base_url, token=token)
