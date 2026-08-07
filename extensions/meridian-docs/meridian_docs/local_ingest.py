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

c39ae092 — LOCAL FALLBACK (avoids Cloudflare 403):
The hosted ``ingest_document_structure`` POST was confirmed hitting a live
Cloudflare 403 error 1010 (browser_signature_banned).  :func:`ingest_local_document_structure_sidecar`
provides an entirely local alternative: it stores headings/figures/tables into
the SAME sidecar SQLite DB that :func:`index_document` and
:func:`search_paragraphs` already use (via ``docs_intel.index_docx_structure``),
so zero network access is required.  The updated :func:`ingest_local_document_structure`
tries the local sidecar path first when ``index_db_path`` is supplied, and only
falls back to the hosted POST when it is not.

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
import ssl
import shutil
import subprocess
import tempfile
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
    "ingest_local_document_structure_sidecar",
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


def _is_tls_verification_error(exc: BaseException) -> bool:
    """Return whether urllib failed during certificate verification.

    Some Windows Python/uvx environments resolve a different Cloudflare edge
    certificate than the system curl/browser stack.  This is intentionally
    narrow: connection failures and HTTP errors must not silently switch
    transports, and no fallback disables certificate verification.
    """
    reason = getattr(exc, "reason", exc)
    message = str(reason).lower()
    return (
        "certificate_verify_failed" in message
        or "certificate verify failed" in message
        or "certificate has expired" in message
        or "certificate is not yet valid" in message
    )


def _call_mcp_tool_via_curl(
    url: str,
    payload: bytes,
    headers: dict[str, str],
    timeout: int,
) -> str:
    """Retry one HTTPS JSON-RPC request through the system curl client.

    The payload and headers are placed in short-lived temp files so bearer
    tokens never appear in the child-process command line.  curl performs its
    normal certificate validation; this is a transport-path fallback, not an
    insecure TLS workaround.
    """
    curl = shutil.which("curl") or shutil.which("curl.exe")
    if not curl:
        raise RuntimeError("curl is unavailable for the TLS transport fallback")

    with tempfile.TemporaryDirectory(prefix="meridian-ingest-") as temp_dir:
        payload_path = os.path.join(temp_dir, "payload.json")
        headers_path = os.path.join(temp_dir, "headers.txt")
        with open(payload_path, "wb") as payload_file:
            payload_file.write(payload)
        with open(headers_path, "w", encoding="utf-8", newline="\n") as headers_file:
            for name, value in headers.items():
                headers_file.write(f"{name}: {value}\n")

        command = [
            curl,
            "--silent",
            "--show-error",
            "--fail-with-body",
            "--max-time",
            str(timeout),
            "--request",
            "POST",
            "--header",
            f"@{headers_path}",
            "--data-binary",
            f"@{payload_path}",
            url,
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=timeout + 5,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"curl fallback timed out after {timeout}s") from exc

        raw = completed.stdout.decode("utf-8", errors="replace")
        if completed.returncode:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            if raw.strip():
                detail = f"{detail}: {raw[:500]}" if detail else raw[:500]
            raise RuntimeError(
                f"curl fallback exited {completed.returncode}"
                + (f": {detail}" if detail else "")
            )
        return raw


def _call_mcp_tool(
    tool_name: str,
    params: dict[str, Any],
    base_url: str | None = None,
    token: str | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    """POST a ``tools/call`` JSON-RPC call to the hosted Meridian /mcp endpoint.

    Uses stdlib ``urllib.request`` primarily.  If that process reports a
    certificate-verification failure, retries once through the system curl
    client with normal certificate validation (no TLS bypass).

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
        # Accept: prefer JSON but also allow SSE (Streamable HTTP).
        "Accept": "application/json, text/event-stream",
        # Accept-Language: some WAFs check for a consistent browser-like header set;
        # an absent Accept-Language combined with a non-browser UA can trigger
        # additional fingerprinting blocks beyond the User-Agent check alone.
        "Accept-Language": "en-US,en;q=0.9",
        # Cloudflare WAF (error 1010 / browser_signature_banned) blocks requests
        # carrying Python's default "Python-urllib/3.x" User-Agent.  Every other
        # Meridian client that hits usemeridian.us (meridian_connect.py,
        # smoke_test_signup.py, test_live.py) explicitly sets a non-Python UA for
        # exactly this reason.  This constant mirrors that pattern so that the
        # ingest_local_document path is not blocked by the WAF.
        #
        # NOTE (40d93549): if Cloudflare 1010 still recurs against a verifiably fresh
        # process after these header additions, the remaining blocker is most likely TLS
        # fingerprinting (JA3/JA3S) which cannot be spoofed by stdlib urllib (fixed
        # OpenSSL defaults).  The proper fix would require curl_cffi or a similar
        # client that can impersonate browser TLS — flag as a follow-up if needed.
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
    except (urllib.error.URLError, ssl.SSLError) as exc:
        if not _is_tls_verification_error(exc):
            raise DocExtractionError(
                f"could not reach Meridian at {url}: {exc.reason}"
            ) from exc
        try:
            raw = _call_mcp_tool_via_curl(url, payload, headers, timeout)
        except Exception as fallback_exc:  # noqa: BLE001
            raise DocExtractionError(
                f"urllib TLS verification failed for {url}; curl fallback also failed: "
                f"{fallback_exc}"
            ) from fallback_exc

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
# c39ae092 — local sidecar path added as primary fallback
# ---------------------------------------------------------------------------

def ingest_local_document_structure_sidecar(
    path: str,
    index_db_path: str,
    title: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """c39ae092 — parse a local .docx and store structural elements locally.

    PURE LOCAL PATH — no network call, no hosted POST.

    Stores headings/figures/tables into the sidecar SQLite DB at
    ``index_db_path`` (the same DB used by :func:`index_document` /
    :func:`search_paragraphs`) via ``docs_intel.index_docx_structure``.

    This is the correct solution when the hosted ``ingest_document_structure``
    POST is blocked (e.g. Cloudflare 403 1010 confirmed live 2026-07-15).

    Args:
      path:           Absolute or relative path to a local .docx file.
      index_db_path:  Path to the sidecar SQLite index (created if absent).
                      Typically the same DB used for ``index_document`` on the
                      same file.
      title:          Document title (optional; stored in sidecar meta).
      source:         Source key (defaults to ``path``).

    Returns:
      ``{index_db, source, heading_count, figure_count, table_count,
      local_path, blocks_parsed, complete, source_sha256}``.  ``complete``
      and ``source_sha256`` (e9b2cd2b) are passed through from
      :func:`docs_intel.index_docx_structure`'s freshness metadata.

    Raises:
      FileNotFoundError:        if ``path`` does not exist.
      DocExtractionError:       if the file is not a valid .docx or cannot
                                be parsed.
      UnsupportedDocumentError: for non-.docx inputs.
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
            f"ingest_local_document_structure_sidecar only supports .docx files "
            f"(structural parsing requires OOXML binary, got '{ext}'). "
        )

    from meridian_docs import docs_intel as _di  # noqa: PLC0415

    try:
        summary = _di.index_docx_structure(path, index_db_path)
    except Exception as exc:  # noqa: BLE001
        raise DocExtractionError(
            f"could not index .docx structural content: {exc}"
        ) from exc

    resolved_source = source if source is not None else path

    # Store title + source in sidecar meta so callers can look it up later.
    import sqlite3 as _sqlite3  # noqa: PLC0415
    try:
        conn = _sqlite3.connect(index_db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO docx_index_meta (key, value) VALUES (?, ?)",
                ("struct_source", resolved_source),
            )
            if title is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO docx_index_meta (key, value) VALUES (?, ?)",
                    ("struct_title", title),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass  # meta write failure is non-fatal

    return {
        "index_db": index_db_path,
        "source": resolved_source,
        "local_path": path,
        "heading_count": summary.get("heading_count", 0),
        "figure_count": summary.get("figure_count", 0),
        "table_count": summary.get("table_count", 0),
        # e9b2cd2b — pass through the completeness marker + SHA-256 source
        # fingerprint index_docx_structure just stamped, so a caller of this
        # wrapper has the same freshness signal without a second sidecar hit.
        "complete": summary.get("complete", True),
        "source_sha256": summary.get("source_sha256"),
    }


def ingest_local_document_structure(
    path: str,
    project_id: str,
    title: str | None = None,
    source: str | None = None,
    index_db_path: str | None = None,
    force_hosted: bool = False,
    base_url: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Parse a local .docx's structural content and persist it.

    db42acce / c39ae092 / f8c7ffdc — structural complement to
    :func:`ingest_local_document`.  Where that function can only forward plain
    text (populating the flat note store), this function parses the REAL .docx
    binary locally (where the file actually lives) to extract its structural
    tree (headings, figures, tables).

    TWO PATHS — hosted routing is OPT-IN (f8c7ffdc):

    1. LOCAL SIDECAR (DEFAULT, no network) — when ``index_db_path`` is
       supplied, structural elements are stored into the sidecar SQLite DB
       via ``docs_intel.index_docx_structure``.  This requires NO hosted call
       and is immune to Cloudflare 403 blocks.  ``project_id`` is still
       accepted for API compatibility but is not used in this path.

    2. HOSTED POST (explicit opt-in only) — when ``index_db_path`` is None AND
       ``force_hosted=True``, the blocks are forwarded to the hosted
       ``ingest_document_structure`` MCP tool via an HTTP call (db42acce
       behaviour).  This path is subject to Cloudflare 403 on blocked IPs and
       the 100 KB body cap.

       If ``index_db_path`` is None and ``force_hosted`` is False (the default),
       a :class:`DocExtractionError` is raised rather than silently hitting the
       hosted endpoint.  Pass ``force_hosted=True`` to explicitly opt in to the
       hosted path when you genuinely have no local index DB.

    The ``source`` MUST match the source used in any prior
    :func:`ingest_local_document` call for the same file (default: ``path`` for
    both), so that ``find_similar_figure`` / ``index_figure`` / ``index_table``
    can look up the same ``document_id`` / source key.

    Args:
      path:           Absolute or relative path to a local .docx file.
      project_id:     Meridian project UUID (used only on the hosted path).
      title:          Document title (optional).
      source:         Source key (defaults to ``path``).
      index_db_path:  Path to the local sidecar SQLite index.  When supplied,
                      the local-only path is taken (no network call).  Strongly
                      preferred over the hosted path.
      force_hosted:   Set to ``True`` to explicitly use the hosted POST path
                      when ``index_db_path`` is None.  Default ``False`` — the
                      hosted path is NEVER used unless explicitly opted in.
      base_url:       Override the Meridian server URL (hosted path only).
      token:          Override the API token (hosted path only).

    Returns:
      Local path:  ``{index_db, source, heading_count, figure_count,
                      table_count, local_path, complete, source_sha256}``.
      Hosted path: ``{document_id, source, doc_type, element_count,
                      local_path, blocks_forwarded}``.

    Raises:
      FileNotFoundError:         if ``path`` does not exist.
      DocExtractionError:        if the file is not a valid .docx, or on
                                 network/server errors (hosted path), or if
                                 ``index_db_path`` is None and ``force_hosted``
                                 is False (hosted routing not opted in).
      UnsupportedDocumentError:  for non-.docx inputs.
    """
    # c39ae092 / f8c7ffdc: local sidecar is the DEFAULT — take it whenever
    # index_db_path is supplied.
    if index_db_path is not None:
        return ingest_local_document_structure_sidecar(
            path=path,
            index_db_path=index_db_path,
            title=title,
            source=source,
        )

    # f8c7ffdc: hosted routing is NEVER the silent default.  The caller must
    # explicitly opt in by passing force_hosted=True, otherwise we raise rather
    # than silently hitting the hosted endpoint (which risks Cloudflare 1010,
    # the 100 KB body cap, and unintended hosted-API usage from a local session).
    if not force_hosted:
        raise DocExtractionError(
            "ingest_local_document_structure: hosted routing is opt-in only "
            "(f8c7ffdc).  Supply index_db_path to use the local sidecar path "
            "(recommended, no network call), or pass force_hosted=True to "
            "explicitly use the hosted POST path.  Silently defaulting to the "
            "hosted endpoint is intentionally blocked — it was causing "
            "Cloudflare 1010 blocks and unexpected 100 KB body-cap errors."
        )

    # Explicit hosted-POST path (db42acce) — force_hosted=True was set.
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
