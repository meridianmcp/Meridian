"""Bounded local MCP file router (item a4cb12bf, LOCAL-MCP-FILE-ROUTER).

Exposes ONE local inspection workflow -- ``inspect_local_file`` -- that
routes a single file to whichever existing sibling capability already
understands its format, and normalizes every answer into the SAME bounded
envelope contract documented in
``docs/meridian-storage-and-file-inspector-contract-2026-08-31.md``. This
module is a thin dispatcher: it never parses XML/JSON/CSV/XLSX/DOCX itself
-- doing so would be exactly the "second parser" this item's title
("...without tunnel or duplicate parsers") forbids.

Routing table
---------------------------------------------------------------------------
  - raw XML, generic JSON (bare object, or anything not shaped like rows)
    -> ``extensions/meridian-file-inspection``'s ``inspect_file``.
  - CSV/TSV, XLSX, and JSON shaped like rows (a top-level array)
    -> ``extensions/meridian-file-inspection``'s ``inspect_tabular_file``.
  - DOCX/DOTX/DOCM/DOTM
    -> ``extensions/meridian-docs``'s ``document_outline`` (metadata/shape
    operations) or ``read_document_snapshot`` (preview operation).

Why subprocess/MCP-client, never a direct cross-package import
---------------------------------------------------------------------------
``meridian_file_inspection`` and ``meridian_docs`` are independently
``uvx``-installable packages -- neither can import core ``meridian`` or a
sibling extension package, per the standalone-isolation constraint already
documented in both packages' own ``pyproject.toml``/docstrings (see
``meridian_file_inspection.inspector``'s module docstring). A direct
``import meridian_file_inspection`` from here would work by accident in
THIS repo's pixi env (which happens to have every sibling's dependencies
installed as a superset) but silently break the moment ``meridian-outputs``
is installed on its own via ``uvx --from <path> meridian-outputs-mcp`` on a
machine that never installed the file-inspection/docs siblings -- exactly
the isolation boundary this item's own discovery investigation verified
empirically (no direct-import path exists in production). Each sibling is
therefore spawned as its own short-lived MCP stdio server and called
through the real ``mcp`` client SDK (``mcp.client.stdio``/
``mcp.client.session`` -- already a direct, identically-pinned dependency
of every extension package here, so this adds ZERO new dependency surface).

Two launch strategies are tried, in order, for each sibling:

  1. ``sys.executable -m <sibling_package>.server`` with ``cwd`` set to the
     sibling's own package directory (so Python's own ``-m`` sys.path[0]
     rule finds the package with no install step at all) -- fast, and
     correct whenever the sibling's dependencies already happen to be
     present in the calling interpreter (true in this repo's own dev/CI
     pixi env, and true of any deployment that installs every extension
     into one shared environment).
  2. ``uvx --from <sibling_dir> <sibling-entry-point>`` -- the sibling's own
     documented standalone-install path, used ONLY as a fallback when (1)
     fails to even start a working MCP session. This is the correct path
     for a genuinely isolated deployment (the actual production shape the
     packages are designed for) where the calling interpreter does NOT
     have e.g. ``lxml``/``duckdb`` installed. Requires ``uvx`` on PATH and,
     on a cold cache, a one-time network fetch -- exactly the situation the
     ``unavailable`` state below exists to report explicitly rather than
     hang or crash.

The ``local_only``/``unavailable`` convention (new to this codebase)
---------------------------------------------------------------------------
No prior tool in this repo needed to distinguish "this capability only
ever runs locally, and right now it genuinely could not be reached" from
an ordinary parse failure. Every response from this module carries
``"local_only": True`` (this router never makes a network or tunnel call
of its own -- only a local subprocess spawn) and, when NEITHER launch
strategy above can even complete an MCP handshake with the target sibling
(missing sibling directory, no compatible Python/uvx on PATH, the spawned
process crashing before it can answer, or the whole attempt exceeding its
wall-clock budget), ``state`` is the new ``"unavailable"`` value (distinct
from ``"failed"``, which means the sibling ran and reported a real parse
error) and ``errors`` carries a stable ``"unavailable"`` code. A caller
that only understands the original four-state contract
(complete/partial/failed) can still safely treat ``"unavailable"`` as a
failure; a caller that wants to retry, degrade, or explain the difference
to a human gets the more precise signal.

The ``operation`` post-filter (metadata / shape / preview)
---------------------------------------------------------------------------
Neither sibling tool has a cheaper-than-full call for "just the identity
fields" today (a real, documented gap -- see this item's own discovery
notes) -- ``inspect_file``/``inspect_tabular_file`` always compute the full
bounded structural summary in one shot. Rather than block on adding that
to two already-shipped, tested tools, this router applies a deterministic
POST-FILTER to the same underlying response:

  - ``"metadata"``: strip every content-shaped field from ``shape``
    (previews, sample rows/paragraphs, key/column/heading listings) --
    only small scalar facts survive (counts, root kind, etc).
  - ``"shape"``: strip only the heaviest CONTENT previews (``preview``,
    ``sample_rows``, ``paragraphs``) but keep structural listings (keys,
    columns, headings).
  - ``"preview"``: the full underlying response, unfiltered.

The one case where ``operation`` genuinely changes the cost of the
underlying call, not just what this router keeps afterward, is DOCX:
``document_outline`` accepts its own ``page_size``, so ``"metadata"``/
``"shape"`` requests a small bounded page of headings instead of the full
outline, and only ``"preview"`` calls the heavier ``read_document_snapshot``
for actual paragraph text.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

from .outputs_local import is_secret_path

SCHEMA_VERSION = "1.0.0"

DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB -- same default as the siblings
DEFAULT_TIMEOUT_SECONDS = 5.0  # forwarded to the sibling's own parse budget
#: Extra wall-clock allowance on top of ``timeout_seconds`` for subprocess
#: cold start (interpreter/import overhead) + the MCP handshake itself --
#: NOT part of the sibling's own parse budget, which stays exactly
#: ``timeout_seconds`` as documented on ``inspect_file``/``inspect_tabular_file``.
SUBPROCESS_OVERHEAD_SECONDS = 20.0

STATE_COMPLETE = "complete"
STATE_PARTIAL = "partial"
STATE_FAILED = "failed"
#: New to this module (see module docstring): neither sibling process could
#: be reached at all -- distinct from STATE_FAILED, which means a sibling
#: process DID run and reported a real parse/policy error.
STATE_UNAVAILABLE = "unavailable"

#: Superset of the sibling packages' own ERROR_CODES (unsupported,
#: limit_exceeded, malformed, denied, timeout, partial) plus this router's
#: own "unavailable" code for a sibling process that could not be reached.
ERROR_CODES = frozenset({
    "unsupported", "limit_exceeded", "malformed", "denied", "timeout",
    "partial", "unavailable",
})

OPERATIONS = frozenset({"metadata", "shape", "preview"})
_DEFAULT_OPERATION = "shape"

FORMATS = frozenset({"auto", "xml", "json", "csv", "xlsx", "docx"})

ROUTES = frozenset({"generic", "tabular", "docs"})

_DOCX_EXTENSIONS = frozenset({".docx", ".dotx", ".docm", ".dotm"})
_TABULAR_TEXT_EXTENSIONS = frozenset({".csv", ".tsv"})

# ---------------------------------------------------------------------------
# Sibling package locations -- resolved relative to THIS file, overridable by
# callers (mainly tests) via the ``file_inspection_dir``/``docs_dir`` params.
# ---------------------------------------------------------------------------
_THIS_FILE = Path(__file__).resolve()
_EXTENSIONS_DIR = _THIS_FILE.parent.parent.parent  # .../extensions
DEFAULT_FILE_INSPECTION_DIR = _EXTENSIONS_DIR / "meridian-file-inspection"
DEFAULT_DOCS_DIR = _EXTENSIONS_DIR / "meridian-docs"

_SIBLING_SPEC = {
    "file_inspection": {
        "module": "meridian_file_inspection.server",
        "entry_point": "meridian-file-inspection-mcp",
    },
    "docs": {
        "module": "meridian_docs.server",
        "entry_point": "meridian-docs-mcp",
    },
}

#: Keys stripped from ``shape`` (recursively) per operation tier. See module
#: docstring's "operation post-filter" section.
_HEAVY_KEYS_BY_OPERATION: dict[str, frozenset[str]] = {
    "shape": frozenset({"preview", "sample_rows", "paragraphs"}),
    "metadata": frozenset({
        "preview", "sample_rows", "paragraphs", "keys", "columns",
        "headings", "sample_types", "selected",
    }),
}


# ---------------------------------------------------------------------------
# Small, self-contained helpers -- deliberately duplicated (not imported)
# from meridian_file_inspection.inspector, per the isolation constraint this
# module's own docstring explains. ``is_secret_path`` is the one exception:
# it is a normal SAME-PACKAGE import from meridian_outputs.outputs_local,
# not a cross-package one, so no isolation boundary is crossed reusing it.
# ---------------------------------------------------------------------------


def _err(code: str, reason: str, detail: str | None = None) -> dict[str, Any]:
    assert code in ERROR_CODES, f"unknown error code {code!r}"
    out: dict[str, Any] = {"code": code, "reason": reason}
    if detail:
        out["detail"] = detail
    return out


def _redact_source_ref(path: str) -> str:
    """Portable, redacted reference: basename plus up to two parent
    directory names -- never the raw machine-local absolute path. Mirrors
    ``meridian_file_inspection.inspector._redact_source_ref`` exactly."""
    norm = path.replace("\\", "/")
    parts = [p for p in norm.split("/") if p not in ("", ".", "..")]
    tail = parts[-3:] if len(parts) > 3 else parts
    return "/".join(tail) if tail else os.path.basename(path)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _strip_keys(value: Any, keys_to_strip: frozenset[str]) -> Any:
    """Recursively drop ``keys_to_strip`` from every dict level of
    ``value`` -- used to implement the metadata/shape operation tiers over
    whatever nested shape a sibling tool returned."""
    if isinstance(value, dict):
        return {
            k: _strip_keys(v, keys_to_strip)
            for k, v in value.items()
            if k not in keys_to_strip
        }
    if isinstance(value, list):
        return [_strip_keys(v, keys_to_strip) for v in value]
    return value


def _reduce_shape(shape: Any, operation: str) -> Any:
    if operation == "preview":
        return shape
    keys_to_strip = _HEAVY_KEYS_BY_OPERATION.get(operation)
    if keys_to_strip is None:
        return shape
    return _strip_keys(shape, keys_to_strip)


# ---------------------------------------------------------------------------
# Path-policy preflight -- applied uniformly, BEFORE any subprocess is ever
# spawned, regardless of which sibling the path will route to. Mirrors
# meridian_file_inspection.inspector._resolve_path_policy; duplicated for
# the same isolation reason (module docstring), except is_secret_path which
# is a legitimate same-package import (see above).
# ---------------------------------------------------------------------------


def _preflight_path_policy(
    path: str, *, allowed_root: str | None, allow_symlinks: bool,
) -> dict[str, Any] | None:
    if not path or not isinstance(path, str):
        return _err("denied", "invalid_path", "path must be a non-empty string")

    if is_secret_path(path):
        return _err("denied", "secret_path_excluded")

    if not allow_symlinks and os.path.islink(path):
        return _err("denied", "symlink_not_allowed")

    if allowed_root is not None:
        try:
            real_root = os.path.realpath(allowed_root)
            real_path = os.path.realpath(path)
        except OSError as exc:
            return _err("denied", "path_resolution_failed", str(exc))
        if os.path.commonpath([real_root, real_path]) != real_root:
            return _err("denied", "outside_allowed_root")

    if not os.path.exists(path):
        return _err("denied", "not_found")

    if os.path.isdir(path):
        return _err("denied", "is_a_directory")

    if not os.path.isfile(path):
        return _err("denied", "not_a_regular_file")

    if not os.access(path, os.R_OK):
        return _err("denied", "unreadable")

    return None


# ---------------------------------------------------------------------------
# Format classification / routing
# ---------------------------------------------------------------------------


def _classify_json_content(path: str) -> dict[str, Any]:
    """A bare top-level JSON ARRAY reads naturally as ROWS (tabular route);
    anything else (object, scalar, malformed) keeps the existing generic
    key/structure view -- mirrors, without importing,
    ``meridian_file_inspection.tabular``'s own module-docstring distinction
    between the two equally-valid readings of the same JSON bytes."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError as exc:
        return {"error": _err("denied", "unreadable", str(exc))}
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped[:1] == b"[":
        return {"route": "tabular", "format": "json"}
    return {"route": "generic", "format": "json"}


def _classify_by_content_sniff(path: str) -> dict[str, Any]:
    """``format="auto"`` with an unrecognized/absent extension: fall back to
    a magic-byte peek, mirroring (without importing) the siblings' own
    sniffers. Callers must already have passed ``_preflight_path_policy``
    before this ever opens the file."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError as exc:
        return {"error": _err("denied", "unreadable", str(exc))}
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    first = stripped[:1]
    if first == b"<":
        return {"route": "generic", "format": "xml"}
    if first == b"[":
        return {"route": "tabular", "format": "json"}
    if first == b"{":
        return {"route": "generic", "format": "json"}
    if stripped[:4] == b"PK\x03\x04":
        # XLSX and DOCX are both zip containers, indistinguishable by magic
        # bytes alone -- a real DOCX MUST be identified by its
        # .docx/.dotx/.docm/.dotm extension (checked before this fallback
        # ever runs). An extensionless zip is routed to the tabular xlsx
        # reader, which itself reports malformed if it isn't really xlsx.
        return {"route": "tabular", "format": "xlsx"}
    return {"route": "tabular", "format": "csv"}


def classify_path(path: str, format: str = "auto") -> dict[str, Any]:
    """Decide which sibling should handle ``path`` and which concrete
    format to request from it.

    Returns ``{"route": "generic"|"tabular"|"docs", "format": <str>}`` or
    ``{"error": {...}}`` (this module's own error-code vocabulary). Never
    raises. Assumes the caller already ran ``_preflight_path_policy`` --
    this function may itself peek at the first 4KiB of ``path`` to sniff an
    ambiguous/absent extension.
    """
    fmt = (format or "auto").lower()
    if fmt not in FORMATS:
        return {"error": _err("malformed", "invalid_format", f"unknown format {fmt!r}")}

    ext = os.path.splitext(path)[1].lower()

    if fmt == "docx" or (fmt == "auto" and ext in _DOCX_EXTENSIONS):
        return {"route": "docs", "format": "docx"}
    if fmt == "csv" or (fmt == "auto" and ext in _TABULAR_TEXT_EXTENSIONS):
        return {"route": "tabular", "format": "csv"}
    if fmt == "xlsx" or (fmt == "auto" and ext == ".xlsx"):
        return {"route": "tabular", "format": "xlsx"}
    if fmt == "xml" or (fmt == "auto" and ext == ".xml"):
        return {"route": "generic", "format": "xml"}
    if fmt == "json":
        return _classify_json_content(path)
    if fmt == "auto" and ext == ".json":
        return _classify_json_content(path)
    if fmt == "auto":
        return _classify_by_content_sniff(path)
    # Every non-"auto" fmt is handled by one of the exact-match branches
    # above -- reachable only if FORMATS and the branches above ever drift
    # apart. Fail closed rather than silently falling through.
    return {"error": _err("unsupported", "format_not_recognized", f"format={fmt!r}")}


# ---------------------------------------------------------------------------
# Subprocess/MCP-client sibling dispatch
# ---------------------------------------------------------------------------


def _candidate_launches(sibling: str, sibling_dir: Path) -> list[tuple[str, list[str], dict[str, str] | None]]:
    """Ordered ``(command, args, env)`` candidates for spawning ``sibling``
    (see module docstring's "Two launch strategies"). ``env`` is merged
    OVER the stdio client's own safe default environment, never replacing
    it -- PATH and friends are always still inherited."""
    spec = _SIBLING_SPEC[sibling]
    candidates: list[tuple[str, list[str], dict[str, str] | None]] = [
        (
            sys.executable,
            ["-m", spec["module"]],
            {"PYTHONPATH": str(sibling_dir)},
        ),
    ]
    uvx_path = shutil.which("uvx")
    if uvx_path:
        candidates.append((uvx_path, ["--from", str(sibling_dir), spec["entry_point"]], None))
    return candidates


def _extract_result_text(call_result: Any) -> str | None:
    for block in getattr(call_result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            return text
    return None


class SiblingToolError(RuntimeError):
    """The sibling MCP process was reached successfully and answered the
    call -- but the tool itself reported an error for THIS input (e.g.
    ``document_outline`` raising on a missing/malformed .docx, surfaced by
    FastMCP as an MCP-level tool error). This is a real, deterministic
    parse/policy failure, never a transport/availability problem: retrying
    via a different launch strategy (e.g. falling back from the fast
    ``sys.executable`` path to ``uvx``) would just reproduce the identical
    error, so ``_call_sibling_async`` re-raises this immediately instead of
    trying the next candidate, and callers map it to ``STATE_FAILED`` (never
    ``STATE_UNAVAILABLE``)."""


async def _try_one_launch(
    command: str,
    args: list[str],
    env: dict[str, str] | None,
    cwd: Path,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=command, args=args, env=env, cwd=str(cwd))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
    if result.isError:
        text = _extract_result_text(result) or "tool reported an error with no detail"
        raise SiblingToolError(f"sibling tool {tool_name!r} reported an error: {text[:500]}")
    if isinstance(result.structuredContent, dict):
        return result.structuredContent
    text = _extract_result_text(result)
    if text is None:
        raise RuntimeError(f"sibling tool {tool_name!r} returned no content")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"sibling tool {tool_name!r} returned a non-object result")
    return parsed


async def _call_sibling_async(
    sibling: str,
    sibling_dir: Path,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Try each launch candidate in order; return the first success.

    A :class:`SiblingToolError` (the process answered, the tool itself
    reported a real error) is re-raised IMMEDIATELY -- never retried across
    candidates, since a different launch strategy would just reproduce the
    same deterministic failure. Any other exception (spawn failure, bad
    handshake, timeout) is aggregated and only the last one re-raised if
    every candidate fails that way -- the caller maps that into the
    ``unavailable`` envelope state.
    """
    candidates = _candidate_launches(sibling, sibling_dir)
    last_exc: BaseException | None = None
    for command, args, env in candidates:
        try:
            return await asyncio.wait_for(
                _try_one_launch(command, args, env, sibling_dir, tool_name, arguments),
                timeout=timeout_seconds + SUBPROCESS_OVERHEAD_SECONDS,
            )
        except SiblingToolError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- aggregated, re-raised below
            last_exc = exc
            continue
    assert last_exc is not None
    raise last_exc


def _run_async(coro: Any) -> Any:
    """Run an async coroutine to completion from this (synchronous) MCP
    tool function, which may itself already be executing INSIDE a running
    event loop -- FastMCP dispatches a sync ``@mcp.tool()`` function by
    calling it directly on the server's own event-loop thread (verified
    against ``mcp.server.fastmcp.utilities.func_metadata
    .call_fn_with_arg_validation``), so a plain ``asyncio.run()`` here would
    raise "cannot be called from a running event loop".

    Spawns a dedicated worker thread with its OWN fresh event loop instead
    (via ``asyncio.run()`` there, which is safe precisely because that
    thread has no event loop of its own yet) and blocks on it -- a small,
    self-contained bridge, not a new dependency. On Windows this also means
    the loop actually spawning the sibling subprocess is a fresh
    ``ProactorEventLoop`` (``asyncio.run()``'s default there), which is
    required for asyncio subprocess pipe support -- independent of whatever
    loop policy the OUTER caller (this standalone MCP stdio server's own
    process) happens to be running.
    """
    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def call_sibling(
    sibling: str,
    sibling_dir: Path,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Synchronous entry point: spawn ``sibling``'s MCP server, call
    ``tool_name(**arguments)`` on it, and return its parsed JSON result.
    Raises on any failure (unreachable, crashed, timed out, non-JSON,
    reported an MCP-level error) -- the caller (``inspect_local_file``)
    catches this and maps it to the ``unavailable`` envelope state."""
    return _run_async(
        _call_sibling_async(
            sibling, sibling_dir, tool_name, arguments,
            timeout_seconds=timeout_seconds,
        )
    )


# ---------------------------------------------------------------------------
# Envelope construction
# ---------------------------------------------------------------------------


def _base_envelope(path: str, *, bounds: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source_ref": _redact_source_ref(path),
        "format": None,
        "mime": None,
        "size_bytes": None,
        "source_sha256": None,
        "parser_id": None,
        "parser_version": None,
        "result_hash": None,
        "state": STATE_FAILED,
        "shape": {},
        "bounds": bounds,
        "warnings": [],
        "errors": [],
        "provenance_ref": None,
        # New to this router (see module docstring) -- always present:
        # this capability never crosses the network/tunnel on its own.
        "local_only": True,
        "operation": None,
        "route": None,
    }


_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _mark_unavailable(envelope: dict[str, Any], sibling: str, exc: BaseException) -> dict[str, Any]:
    envelope["state"] = STATE_UNAVAILABLE
    envelope["errors"].append(
        _err(
            "unavailable",
            "sibling_process_unreachable",
            f"could not reach the {sibling!r} sibling MCP server: {exc}"[:500],
        )
    )
    return envelope


def _mark_failed_from_tool_error(envelope: dict[str, Any], exc: BaseException) -> dict[str, Any]:
    """The sibling process WAS reached and DID answer, but the tool call
    itself reported a real error for this input (``SiblingToolError`` --
    see that class's docstring). Distinct from ``_mark_unavailable``: this
    is a genuine parse/policy failure, not an availability problem."""
    envelope["state"] = STATE_FAILED
    envelope["errors"].append(_err("malformed", "sibling_tool_error", str(exc)[:500]))
    return envelope


def _adapt_generic_or_tabular_result(
    envelope: dict[str, Any], raw: dict[str, Any], operation: str,
) -> dict[str, Any]:
    """``inspect_file``/``inspect_tabular_file`` already return the exact
    canonical envelope shape -- copy every field through verbatim, then
    apply the operation post-filter to ``shape`` only."""
    for key in (
        "format", "mime", "size_bytes", "source_sha256", "parser_id",
        "parser_version", "result_hash", "state", "warnings", "errors",
    ):
        if key in raw:
            envelope[key] = raw[key]
    envelope["shape"] = _reduce_shape(raw.get("shape", {}), operation)
    return envelope


def _map_docs_error(reason: str | None) -> str:
    if reason == "stale_cursor" or reason == "invalid_cursor":
        return "malformed"
    if reason == "section_not_found":
        return "denied"
    return "malformed"


def _adapt_docx_result(
    envelope: dict[str, Any], raw: dict[str, Any], operation: str, *, used_snapshot: bool,
) -> dict[str, Any]:
    """Synthesize the missing canonical-envelope fields around
    ``document_outline``/``read_document_snapshot``'s bespoke response
    shapes (real gap this router closes -- see module docstring)."""
    envelope["format"] = "docx"
    envelope["mime"] = _DOCX_MIME
    envelope["parser_id"] = "meridian-docs:read_document_snapshot" if used_snapshot else "meridian-docs:document_outline"
    envelope["parser_version"] = "meridian-docs/1"

    if "error" in raw:
        envelope["state"] = STATE_FAILED
        envelope["errors"].append(
            _err(_map_docs_error(raw.get("reason")), raw.get("reason", "docs_error"), str(raw.get("error"))[:300])
        )
        return envelope

    envelope["state"] = STATE_COMPLETE
    shape = _reduce_shape(dict(raw), operation)
    fingerprint = raw.get("document_fingerprint") or raw.get("source_sha256")
    if fingerprint:
        envelope["source_sha256"] = fingerprint
    envelope["shape"] = shape
    envelope["result_hash"] = _sha256_bytes(_canonical_json(shape).encode("utf-8"))
    return envelope


def inspect_local_file(
    path: str,
    operation: str = _DEFAULT_OPERATION,
    format: str = "auto",
    allowed_root: str | None = None,
    allow_symlinks: bool = False,
    selector: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    preview_chars: int | None = None,
    max_sample_rows: int | None = None,
    file_inspection_dir: "str | Path | None" = None,
    docs_dir: "str | Path | None" = None,
) -> dict[str, Any]:
    """Bounded, tunnel-independent, single-file inspection router. See the
    module docstring for the full routing table, launch strategy, and the
    ``local_only``/``unavailable``/``operation`` contract.

    Args:
      path:              Path to the single file to inspect.
      operation:         ``"metadata"`` (identity/size/hash/state only),
                          ``"shape"`` (default -- structure without content
                          previews/samples), or ``"preview"`` (the full
                          underlying bounded response, content included).
      format:            ``"auto"`` (default), ``"xml"``, ``"json"``,
                          ``"csv"``, ``"xlsx"``, or ``"docx"``.
      allowed_root:       Optional directory ``path`` must resolve under.
      allow_symlinks:     Set True to permit inspecting a symlink target.
      selector:           JSON-only bounded dotted/bracket selector,
                          forwarded to ``inspect_file`` when routed there.
      max_bytes:          Maximum source file size in bytes.
      timeout_seconds:    Wall-clock budget forwarded to the sibling's OWN
                          parse-time bound (NOT the subprocess spawn
                          overhead, which gets its own fixed allowance).
      preview_chars:      Optional override forwarded to the tabular/
                          generic siblings' own ``preview_chars``.
      max_sample_rows:    Optional override forwarded to
                          ``inspect_tabular_file``'s ``max_sample_rows``.
      file_inspection_dir/docs_dir: Override the sibling package
                          directories this router spawns (mainly for
                          tests); default to the real siblings under
                          ``extensions/`` relative to this file.

    Returns:
      The canonical envelope: ``{schema_version, source_ref, format, mime,
      size_bytes, source_sha256, parser_id, parser_version, result_hash,
      state, shape, bounds, warnings, errors, provenance_ref, local_only,
      operation, route}``. ``state`` is one of ``"complete"``/
      ``"partial"``/``"failed"``/``"unavailable"`` -- never raises.
    """
    bounds: dict[str, Any] = {
        "max_bytes": max_bytes,
        "timeout_seconds": timeout_seconds,
    }
    if preview_chars is not None:
        bounds["preview_chars"] = preview_chars
    if max_sample_rows is not None:
        bounds["max_sample_rows"] = max_sample_rows

    envelope = _base_envelope(path, bounds=bounds)

    op = (operation or _DEFAULT_OPERATION).lower()
    if op not in OPERATIONS:
        envelope["errors"].append(
            _err("malformed", "invalid_operation", f"unknown operation {op!r}; expected one of {sorted(OPERATIONS)}")
        )
        return envelope
    envelope["operation"] = op

    policy_error = _preflight_path_policy(path, allowed_root=allowed_root, allow_symlinks=allow_symlinks)
    if policy_error is not None:
        envelope["errors"].append(policy_error)
        return envelope

    try:
        size_bytes = os.path.getsize(path)
    except OSError as exc:
        envelope["errors"].append(_err("denied", "unreadable", str(exc)))
        return envelope
    envelope["size_bytes"] = size_bytes
    if size_bytes > max_bytes:
        envelope["errors"].append(_err("limit_exceeded", "max_bytes_exceeded", f"{size_bytes} > {max_bytes}"))
        return envelope

    classification = classify_path(path, format=format)
    if "error" in classification:
        envelope["errors"].append(classification["error"])
        return envelope
    route = classification["route"]
    resolved_format = classification["format"]
    envelope["route"] = route

    file_inspection_dir = Path(file_inspection_dir) if file_inspection_dir else DEFAULT_FILE_INSPECTION_DIR
    docs_dir = Path(docs_dir) if docs_dir else DEFAULT_DOCS_DIR

    if route == "generic":
        arguments: dict[str, Any] = {
            "path": path, "format": resolved_format, "max_bytes": max_bytes,
            "timeout_seconds": timeout_seconds,
            "allowed_root": allowed_root, "allow_symlinks": allow_symlinks,
        }
        if selector:
            arguments["selector"] = selector
        if preview_chars is not None:
            arguments["preview_chars"] = preview_chars
        try:
            raw = call_sibling("file_inspection", file_inspection_dir, "inspect_file", arguments, timeout_seconds=timeout_seconds)
        except SiblingToolError as exc:
            return _mark_failed_from_tool_error(envelope, exc)
        except BaseException as exc:  # noqa: BLE001
            return _mark_unavailable(envelope, "meridian-file-inspection", exc)
        return _adapt_generic_or_tabular_result(envelope, raw, op)

    if route == "tabular":
        arguments = {
            "path": path, "format": resolved_format, "max_bytes": max_bytes,
            "timeout_seconds": timeout_seconds,
            "allowed_root": allowed_root, "allow_symlinks": allow_symlinks,
        }
        if preview_chars is not None:
            arguments["preview_chars"] = preview_chars
        if max_sample_rows is not None:
            arguments["max_sample_rows"] = max_sample_rows
        try:
            raw = call_sibling("file_inspection", file_inspection_dir, "inspect_tabular_file", arguments, timeout_seconds=timeout_seconds)
        except SiblingToolError as exc:
            return _mark_failed_from_tool_error(envelope, exc)
        except BaseException as exc:  # noqa: BLE001
            return _mark_unavailable(envelope, "meridian-file-inspection", exc)
        return _adapt_generic_or_tabular_result(envelope, raw, op)

    # route == "docs"
    used_snapshot = op == "preview"
    if used_snapshot:
        # Bounded even for the "preview" tier -- a real page, never the
        # unbounded full-document default read_document_snapshot falls back
        # to when page_size is omitted.
        arguments = {"docx_path": path, "page_size": 50}
        tool_name = "read_document_snapshot"
    else:
        # metadata/shape: a small bounded page is enough, and genuinely
        # cheaper than the full outline (unlike the generic/tabular siblings,
        # this sibling DOES support a real cheap call -- see module docstring).
        arguments = {"path": path, "page_size": 5 if op == "metadata" else 200}
        tool_name = "document_outline"
    try:
        raw = call_sibling("docs", docs_dir, tool_name, arguments, timeout_seconds=timeout_seconds)
    except SiblingToolError as exc:
        return _mark_failed_from_tool_error(envelope, exc)
    except BaseException as exc:  # noqa: BLE001
        return _mark_unavailable(envelope, "meridian-docs", exc)
    return _adapt_docx_result(envelope, raw, op, used_snapshot=used_snapshot)
