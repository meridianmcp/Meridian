"""Local-file extract-then-ingest bridge for hosted Meridian (fdbd4296).

Problem: the hosted Meridian ``ingest_document`` MCP tool runs on Fly.io and
has NO access to the caller's local filesystem.  When a caller passes a
``file_path`` it gets back:

    "ingest_document reads the file from the Meridian server's own filesystem,
     so on hosted Meridian it cannot open a path on your machine..."

The manual workaround -- read the file with a local tool, then hand-copy the
text into a separate ``ingest_document(content=...)`` call -- is error-prone
and may be lossy when an LLM condenses the text before forwarding it.

Fix: this module provides a single :func:`ingest_local_document` function that
(1) reads a local file programmatically, (2) extracts its full plain text via
the same stdlib-only logic Meridian uses server-side, and (3) forwards the
complete extracted text to the hosted ``ingest_document`` tool via an HTTP
JSON-RPC call.  The caller makes ONE tool call; the two-step manual workaround
goes away.

Text extraction support (stdlib only, no third-party deps):
  - ``.docx`` -- unzipped via ``zipfile``; paragraphs joined from ``<w:t>`` runs.
  - ``.txt`` / ``.md`` / ``.markdown`` / common source extensions -- read as UTF-8
    (errors replaced).
  - ``.pdf`` and everything else -- rejected with a clear ``UnsupportedDocumentError``
    so the caller is told to extract the text themselves and use
    ``ingest_document(content=...)`` directly.

The hosted Meridian URL and API token are read from the spawned process
environment -- the tunnel client inherits these from the parent process:
  - URL:   ``MERIDIAN_URL``  (default ``https://usemeridian.us``)
  - Token: ``MERIDIAN_API_KEY`` > ``BEARER_TOKEN``

The HTTP call to ``/mcp`` uses stdlib ``urllib.request`` only -- no ``httpx``
or ``requests`` (matching the stdlib-only constraint of the rest of this
package).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from typing import Any

__all__ = [
    "UnsupportedDocumentError",
    "DocExtractionError",
    "extract_text",
    "call_hosted_ingest",
    "ingest_local_document",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_BASE_URL = "https://usemeridian.us"

# Plain-text extensions decoded as UTF-8 (matching meridian/doc_ingest.py).
_TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".txt", ".md", ".markdown", ".mdx", ".rst", ".text",
        ".log", ".csv", ".tsv", ".json", ".yaml", ".yml",
        ".toml", ".ini", ".cfg", ".xml", ".html", ".htm",
        ".py", ".js", ".ts", ".tsx", ".jsx",
        ".java", ".c", ".h", ".cpp", ".cc",
        ".go", ".rs", ".rb", ".php", ".sh", ".sql",
    }
)

# OOXML WordprocessingML namespace.
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W_P = f"{{{_W_NS}}}p"
_W_T = f"{{{_W_NS}}}t"
_W_TAB = f"{{{_W_NS}}}tab"
_W_BR = f"{{{_W_NS}}}br"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class DocExtractionError(Exception):
    """Raised when a supported document can't be read or parsed."""


class UnsupportedDocumentError(DocExtractionError):
    """Raised for a file type this module cannot parse (e.g. .pdf).

    The message tells the caller to extract the text themselves and pass it as
    ``content`` to ``ingest_document`` directly.
    """


# ---------------------------------------------------------------------------
# Text extraction (stdlib only)
# ---------------------------------------------------------------------------

def _extract_docx_bytes(data: bytes) -> str:
    """Extract plain text from raw .docx bytes.

    Walks ``word/document.xml`` in document order, emitting each ``<w:t>`` run,
    rendering ``<w:tab>`` as a tab and ``<w:br>`` as a newline, and inserting a
    newline on every ``<w:p>`` paragraph boundary.
    """
    import io  # noqa: PLC0415 -- stdlib, local import mirrors meridian/doc_ingest.py

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            try:
                xml_bytes = zf.read("word/document.xml")
            except KeyError as exc:
                raise DocExtractionError(
                    "not a valid .docx: missing word/document.xml"
                ) from exc
    except zipfile.BadZipFile as exc:
        raise DocExtractionError("not a valid .docx: file is not a zip archive") from exc

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise DocExtractionError(f"could not parse word/document.xml: {exc}") from exc

    paragraphs: list[str] = []
    for para in root.iter(_W_P):
        parts: list[str] = []
        for node in para.iter():
            if node.tag == _W_T:
                parts.append(node.text or "")
            elif node.tag == _W_TAB:
                parts.append("\t")
            elif node.tag == _W_BR:
                parts.append("\n")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs)


def extract_text(path: str) -> str:
    """Extract plain text from a local file (stdlib only).

    Supported:
      - ``.docx`` -- unzipped and paragraph text joined via OOXML parsing.
      - ``.txt``, ``.md`` and other text/source extensions -- decoded as UTF-8
        (errors replaced).
      - Extensionless files -- treated optimistically as plain text.

    Raises:
      - :class:`FileNotFoundError` if ``path`` does not exist.
      - :class:`UnsupportedDocumentError` for ``.pdf`` and unsupported types,
        with a message telling the caller what to do instead.
      - :class:`DocExtractionError` if a supported file can't be read/parsed.
    """
    if not path or not str(path).strip():
        raise DocExtractionError("path must be a non-empty string")
    if not os.path.exists(path):
        raise FileNotFoundError(f"no such file: {path}")
    if not os.path.isfile(path):
        raise DocExtractionError(f"not a file: {path}")

    ext = os.path.splitext(path)[1].lower()

    if ext == ".docx":
        with open(path, "rb") as fh:
            return _extract_docx_bytes(fh.read())

    if ext in _TEXT_EXTENSIONS or ext == "":
        try:
            with open(path, "rb") as fh:
                return fh.read().decode("utf-8", errors="replace")
        except OSError as exc:
            raise DocExtractionError(f"could not read {path}: {exc}") from exc

    if ext == ".pdf":
        raise UnsupportedDocumentError(
            "PDF text cannot be extracted locally by this tool (no PDF library "
            "installed). Extract the text with your own tools and pass it as "
            "the 'content' argument to ingest_document instead."
        )

    raise UnsupportedDocumentError(
        f"unsupported file type '{ext}'. This tool extracts .txt/.md/.docx "
        f"locally. For '{ext}', extract the text yourself and pass it as the "
        "'content' argument to ingest_document."
    )


# ---------------------------------------------------------------------------
# HTTP call to hosted Meridian
# ---------------------------------------------------------------------------

def _resolve_base_url() -> str:
    return (os.environ.get("MERIDIAN_URL") or _DEFAULT_BASE_URL).rstrip("/")


def _resolve_token() -> str:
    """Resolve the API token from env: MERIDIAN_API_KEY > BEARER_TOKEN."""
    token = os.environ.get("MERIDIAN_API_KEY") or os.environ.get("BEARER_TOKEN") or ""
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def call_hosted_ingest(
    project_id: str,
    content: str,
    title: str | None = None,
    source: str | None = None,
    tags: str | None = None,
    base_url: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """POST an ``ingest_document`` JSON-RPC call to the hosted Meridian /mcp endpoint.

    Uses stdlib ``urllib.request`` only (no httpx/requests dependency).

    ``base_url`` defaults to ``MERIDIAN_URL`` env var (falls back to
    ``https://usemeridian.us``). ``token`` defaults to ``MERIDIAN_API_KEY`` or
    ``BEARER_TOKEN`` env vars.

    Returns the ``result`` dict from the JSON-RPC response, or raises a
    :class:`DocExtractionError` describing the server-side failure.
    """
    url = (base_url or _resolve_base_url()) + "/mcp"
    tok = token if token is not None else _resolve_token()

    params: dict[str, Any] = {
        "project_id": project_id,
        "content": content,
    }
    if title is not None:
        params["title"] = title
    if source is not None:
        params["source"] = source
    if tags is not None:
        params["tags"] = tags

    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "ingest_document",
                "arguments": params,
            },
        }
    ).encode()

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if tok:
        headers["Authorization"] = f"Bearer {tok}"

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            body = str(exc)
        raise DocExtractionError(
            f"hosted ingest_document returned HTTP {exc.code}: {body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise DocExtractionError(
            f"could not reach Meridian at {url}: {exc.reason}"
        ) from exc

    # The /mcp endpoint may return Streamable HTTP (SSE) or plain JSON.
    # For a tools/call response it typically returns JSON; handle both.
    result_data: Any = None
    if "data:" in raw:
        # SSE stream -- extract the last non-empty data line's JSON.
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
            raise DocExtractionError(
                f"unexpected response from hosted Meridian: {raw[:500]}"
            ) from exc

    if result_data is None:
        raise DocExtractionError(
            f"no parseable result in hosted Meridian response: {raw[:500]}"
        )

    # JSON-RPC error object.
    if isinstance(result_data, dict) and "error" in result_data:
        err = result_data["error"]
        raise DocExtractionError(f"hosted ingest_document error: {err}")

    # JSON-RPC success: the MCP result may be nested under result.content[0].text
    # (MCP SDK wraps tool results in a content array) or directly as result.
    rpc_result: Any = result_data.get("result", result_data) if isinstance(result_data, dict) else result_data

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

    # Propagate server-side ingest errors surfaced as {"error": "..."} in result.
    if isinstance(rpc_result, dict) and rpc_result.get("error"):
        raise DocExtractionError(
            f"hosted ingest_document returned error: {rpc_result['error']}"
        )

    return rpc_result if isinstance(rpc_result, dict) else {"result": rpc_result}


# ---------------------------------------------------------------------------
# Combined extract-then-ingest
# ---------------------------------------------------------------------------

def ingest_local_document(
    path: str,
    project_id: str,
    title: str | None = None,
    source: str | None = None,
    tags: str | None = None,
    base_url: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Read a local file, extract its full text, and ingest it into Meridian.

    This is the single-call replacement for the two-step manual workaround
    (local-read tool + separate ``ingest_document(content=...)`` call).

    Steps:
      1. ``path`` is opened locally and its text extracted programmatically
         (``.docx`` via OOXML parsing, plain text/source files as UTF-8).
      2. The extracted text is forwarded to the hosted Meridian
         ``ingest_document`` tool via an HTTP call with ``content=<text>``.
      3. The hosted tool's response (the created note id/slug/title/source) is
         returned.

    Args:
      path:        Absolute or relative path to a local file.
      project_id:  Meridian project UUID to ingest into.
      title:       Note title (defaults to the file's basename on the server).
      source:      Provenance label stored on the note (defaults to ``path``).
      tags:        Comma-separated tags.
      base_url:    Override the Meridian server URL (default: ``MERIDIAN_URL``
                   env var, or ``https://usemeridian.us``).
      token:       Override the API token (default: ``MERIDIAN_API_KEY`` or
                   ``BEARER_TOKEN`` env vars).

    Returns:
      The ingested note record from the hosted server, augmented with
      ``chars_extracted`` (int) and ``local_path`` (str).

    Raises:
      FileNotFoundError:         if ``path`` does not exist.
      UnsupportedDocumentError:  if the file type cannot be extracted locally.
      DocExtractionError:        on extraction or network/server errors.
    """
    text = extract_text(path)
    # Default source to the file path so the note's provenance is recorded.
    resolved_source = source if source is not None else path
    result = call_hosted_ingest(
        project_id=project_id,
        content=text,
        title=title,
        source=resolved_source,
        tags=tags,
        base_url=base_url,
        token=token,
    )
    # Augment the result with local metadata useful to the caller.
    result["chars_extracted"] = len(text)
    result["local_path"] = path
    return result
