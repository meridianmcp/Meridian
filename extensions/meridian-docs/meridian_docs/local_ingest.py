"""Local-file extract-then-ingest bridge for hosted Meridian (fdbd4296).

Problem: the hosted Meridian ``ingest_document`` MCP tool runs on Fly.io and
has NO access to the caller's local filesystem.  When a caller passes a
``file_path`` it gets back:

    "ingest_document reads the file from the Meridian server's own filesystem,
     so on hosted Meridian it cannot open a path on your machine..."

The manual workaround -- read the file with a local tool, then hand-copy the
text into a separate ``ingest_document(content=...)`` call -- is error-prone
and may be lossy when an LLM condenses the text before forwarding it.

Fix (fdbd4296): :func:`ingest_local_document` reads a local file programmatically,
extracts its full plain text, and forwards the complete extracted text to the
hosted ``ingest_document`` tool via an HTTP JSON-RPC call.  This populates the
FLAT note store (searchable via search_all / search_synthesis) only.

Limitation (db42acce): the flat note store and the STRUCTURAL doc-store
(headings tree, doc_figures, doc_tables, doc_equations) are SEPARATE.
:func:`ingest_local_document` can only populate the flat note store because
structural elements (heading level, image position, table shape) are not
recoverable from a plain text string — they require parsing the actual .docx
binary (its real XML tree).  To also populate the structural doc-store (so
find_similar_figure / index_figure / index_table / index_equation work on a
locally-stored .docx), use :func:`ingest_local_document_structure`, which:
  1. Parses the real .docx binary locally via ``docparse.docs_intel.document_content_tree``.
  2. Forwards the resulting structural blocks to the hosted
     ``ingest_document_structure`` MCP tool, which stores them into the
     doc-structure store (doc_documents / doc_elements / doc_figures rows)
     keyed on the SAME source as the flat note — so find_similar_figure's
     get_document() lookup resolves the correct document_id.

Text extraction support for :func:`ingest_local_document` (stdlib only):
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
    "call_hosted_ingest_structure",
    "ingest_local_document_structure",
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


def _call_mcp_tool(
    tool_name: str,
    params: dict[str, Any],
    base_url: str | None = None,
    token: str | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    """POST a ``tools/call`` JSON-RPC call to the hosted Meridian /mcp endpoint.

    Uses stdlib ``urllib.request`` only (no httpx/requests dependency).

    Returns the unwrapped result dict, or raises :class:`DocExtractionError`
    describing the server-side failure.
    """
    url = (base_url or _resolve_base_url()) + "/mcp"
    tok = token if token is not None else _resolve_token()

    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": params,
            },
        }
    ).encode()

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        # Cloudflare WAF (error 1010 / browser_signature_banned) blocks requests
        # carrying Python's default "Python-urllib/3.x" User-Agent.  Every other
        # Meridian client that hits usemeridian.us (meridian_connect.py,
        # smoke_test_signup.py, test_live.py) explicitly sets a non-Python UA for
        # exactly this reason.  This constant mirrors that pattern so that the
        # ingest_local_document path is not blocked by the WAF.
        "User-Agent": "meridian-local-ingest/1.0",
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
        raise DocExtractionError(
            f"hosted {tool_name} returned HTTP {exc.code}: {body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise DocExtractionError(
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
            raise DocExtractionError(
                f"unexpected response from hosted Meridian: {raw[:500]}"
            ) from exc

    if result_data is None:
        raise DocExtractionError(
            f"no parseable result in hosted Meridian response: {raw[:500]}"
        )

    if isinstance(result_data, dict) and "error" in result_data:
        err = result_data["error"]
        raise DocExtractionError(f"hosted {tool_name} error: {err}")

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

    if isinstance(rpc_result, dict) and rpc_result.get("error"):
        raise DocExtractionError(
            f"hosted {tool_name} returned error: {rpc_result['error']}"
        )

    return rpc_result if isinstance(rpc_result, dict) else {"result": rpc_result}


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

    NOTE (db42acce): this populates the FLAT note store only (searchable via
    search_all / search_synthesis).  To also populate the structural doc-store
    (headings tree, doc_figures, doc_tables) so find_similar_figure returns a
    real document_id, call :func:`call_hosted_ingest_structure` separately with
    the same ``source`` value.
    """
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
    return _call_mcp_tool("ingest_document", params, base_url=base_url, token=token)


def call_hosted_ingest_structure(
    project_id: str,
    source: str,
    blocks: list[dict[str, Any]],
    title: str | None = None,
    doc_type: str = "docx",
    base_url: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """POST an ``ingest_document_structure`` JSON-RPC call to hosted Meridian.

    db42acce — forwards the raw ``blocks`` list from
    ``docparse.docs_intel.document_content_tree`` to the hosted server, which
    converts them to structural elements (via ``elements_from_docx_content_tree``)
    and stores them in the doc-structure store (doc_documents / doc_elements /
    doc_figures / doc_tables rows) keyed on ``source``.

    ``source`` MUST match the source used for the corresponding
    ``ingest_document(content=...)`` call (default: the local file path) so that
    ``get_document(project_id, source)`` resolves the same ``document_id`` for
    both the flat note and the structural rows — which is the key that makes
    ``find_similar_figure`` return a real (non-null) document_id.

    Returns ``{document_id, source, doc_type, element_count}``, or raises
    :class:`DocExtractionError` on network/server errors.
    """
    params: dict[str, Any] = {
        "project_id": project_id,
        "source": source,
        "blocks": blocks,
        "doc_type": doc_type,
    }
    if title is not None:
        params["title"] = title
    return _call_mcp_tool(
        "ingest_document_structure", params, base_url=base_url, token=token
    )


# ---------------------------------------------------------------------------
# Combined extract-then-ingest (flat note only — fdbd4296)
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

    fdbd4296 — this is the single-call replacement for the two-step manual
    workaround (local-read tool + separate ``ingest_document(content=...)``
    call).

    Steps:
      1. ``path`` is opened locally and its text extracted programmatically
         (``.docx`` via OOXML parsing, plain text/source files as UTF-8).
      2. The extracted text is forwarded to the hosted Meridian
         ``ingest_document`` tool via an HTTP call with ``content=<text>``.
      3. The hosted tool's response (the created note id/slug/title/source) is
         returned.

    SCOPE LIMITATION (db42acce): this function populates the FLAT note store
    ONLY (searchable via search_all / search_synthesis). The structural doc-store
    (headings tree, doc_figures, doc_tables, doc_equations) is NOT populated by
    this function, because structural elements (heading level, image position,
    table shape) are not recoverable from a plain text string — they require
    parsing the actual .docx binary.

    For the FULL two-step ingest (flat note + structural rows) call BOTH:
      1. ``ingest_local_document(path, ...)`` — populates the flat note store.
      2. ``ingest_local_document_structure(path, ...)`` — populates the
         structural doc-store.  Use the SAME ``source`` value for both (the
         default is ``path`` in both cases).

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


# ---------------------------------------------------------------------------
# Structural ingest — headings/figures/tables (db42acce)
# ---------------------------------------------------------------------------

def ingest_local_document_structure(
    path: str,
    project_id: str,
    title: str | None = None,
    source: str | None = None,
    base_url: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Parse a local .docx's structural content and persist it to hosted Meridian.

    db42acce — this is the structural complement to :func:`ingest_local_document`.
    Where that function can only forward plain text (populating the flat note
    store), this function parses the REAL .docx binary locally (where the file
    actually lives) to extract its structural tree (headings, figures, tables)
    and forwards the structural rows to the hosted server's doc-structure store.

    Steps:
      1. The .docx at ``path`` is parsed via
         ``docparse.docs_intel.document_content_tree`` — the same stdlib-only
         OOXML parser used by the 7a98286b structural linter.  This extracts
         headings (level, text, para_id), tables (rows/cells), and figure
         caption paragraphs (SEQ-field captions) in true document order.
      2. The ``blocks`` list from the parse result is forwarded to the hosted
         ``ingest_document_structure`` MCP tool via an HTTP call.  The hosted
         server converts the blocks to structured elements (headings/figures/
         tables with parent relationships) via ``elements_from_docx_content_tree``
         and stores them in doc_documents / doc_elements rows.
      3. The result (document_id, source, element_count) is returned.

    The ``source`` used MUST match the source used in any prior
    :func:`ingest_local_document` call for the same file (default: ``path`` for
    both), so that ``find_similar_figure`` / ``index_figure`` / ``index_table``
    can look up the same ``document_id`` via ``get_document(project_id, source)``.

    Args:
      path:        Absolute or relative path to a local .docx file.
      project_id:  Meridian project UUID to ingest into.
      title:       Document title (optional; stored for display in doc-store).
      source:      Source key (defaults to ``path``).  Must match the source
                   used for :func:`ingest_local_document`.
      base_url:    Override the Meridian server URL.
      token:       Override the API token.

    Returns:
      ``{document_id, source, doc_type, element_count, local_path}``.

    Raises:
      FileNotFoundError:         if ``path`` does not exist.
      DocExtractionError:        if the file is not a valid .docx, or on
                                 network/server errors.
      UnsupportedDocumentError:  if ``docparse`` is not installed (raised by the
                                 import; add ``docparse`` to your environment).
    """
    if not path or not str(path).strip():
        raise DocExtractionError("path must be a non-empty string")
    if not os.path.exists(path):
        raise FileNotFoundError(f"no such file: {path}")
    if not os.path.isfile(path):
        raise DocExtractionError(f"not a file: {path}")

    ext = os.path.splitext(path)[1].lower()
    if ext != ".docx":
        raise UnsupportedDocumentError(
            f"ingest_local_document_structure only supports .docx files "
            f"(structural parsing requires OOXML binary, got '{ext}'). "
            "For .tex LaTeX documents, use ingest_document with file_path on a "
            "self-hosted Meridian instance."
        )

    try:
        from meridian_docs._vendored_content_tree import document_content_tree  # noqa: PLC0415
    except ImportError as exc:
        raise DocExtractionError(
            "docparse is not installed in this environment — install it with "
            "'pip install docparse' or 'pip install -e packages/docparse' "
            "(from the Meridian repo root)"
        ) from exc

    try:
        tree = document_content_tree(path)
    except Exception as exc:  # noqa: BLE001
        raise DocExtractionError(
            f"could not parse .docx structural content: {exc}"
        ) from exc

    blocks = tree.get("blocks") or []
    resolved_source = source if source is not None else path
    result = call_hosted_ingest_structure(
        project_id=project_id,
        source=resolved_source,
        blocks=blocks,
        title=title,
        doc_type="docx",
        base_url=base_url,
        token=token,
    )
    result["local_path"] = path
    result["blocks_forwarded"] = len(blocks)
    return result
