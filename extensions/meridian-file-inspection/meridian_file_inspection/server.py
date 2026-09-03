"""Thin MCP stdio server exposing the bounded local file inspector as one
tool (item 2ffd763d).

Run with ``uvx --from <path> meridian-file-inspection-mcp`` (console entry
point) or ``python -m meridian_file_inspection.server``. Fully local: no
hosted call, no tunnel, no Serena dependency, no network access, no writes.
See :mod:`meridian_file_inspection.inspector` for the implementation and
``README.md`` for the full contract and security notes.
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import inspector, tabular

mcp = FastMCP("meridian-file-inspection")


@mcp.tool()
def inspect_file(
    path: str,
    format: str = "auto",
    max_bytes: int = inspector.DEFAULT_MAX_BYTES,
    max_depth: int = inspector.DEFAULT_MAX_DEPTH,
    max_items: int = inspector.DEFAULT_MAX_ITEMS,
    preview_chars: int = inspector.DEFAULT_PREVIEW_CHARS,
    timeout_seconds: float = inspector.DEFAULT_TIMEOUT_SECONDS,
    allowed_root: str | None = None,
    allow_symlinks: bool = False,
    selector: str | None = None,
) -> dict[str, Any]:
    """Inspect exactly ONE local XML or JSON file and return a bounded,
    deterministic structural summary -- never the full file content.

    Fully local and read-only: no directory walk, no shell, no network
    access, no writes, no database/cache persistence. XML is parsed under
    hardened settings that reject DTDs, entities, and external resolution
    outright (see ``meridian_file_inspection.xml_safe`` for the full threat
    model) -- this is the ONLY supported way to inspect untrusted XML
    through this tool. Secret-named files (``.env*``, ``*.key``, ``*secret*``,
    ``*credential*``, ``config.*``, etc.) are refused before ever being
    opened.

    Args:
      path:            Path to the single file to inspect.
      format:          ``"auto"`` (default, sniffed from magic bytes -- never
                        from extension alone), ``"xml"``, or ``"json"``.
      max_bytes:        Maximum source file size in bytes (default 10 MiB).
                        A larger file is refused with ``limit_exceeded``
                        before any content is read.
      max_depth:        Maximum nesting depth (default 100). XML: element
                        nesting. JSON: object/array nesting, bound-checked
                        BEFORE the file is fully parsed (defends against a
                        pathologically deep document crashing the parser).
      max_items:        Maximum element/node count (default 50,000).
      preview_chars:    Maximum characters in any text/content preview
                        (default 2000) -- the response never contains full
                        file content.
      timeout_seconds:  Soft wall-clock budget (default 5.0s) checked
                        periodically during the parse; a document that runs
                        over is reported ``partial``/``timeout``, never left
                        to run unbounded.
      allowed_root:     Optional directory the resolved ``path`` must fall
                        under (symlink/junction-resolved) -- a path escaping
                        it is refused as ``denied``/``outside_allowed_root``.
      allow_symlinks:   Set True to permit inspecting a symlink target
                        (default False -- a symlink path is refused).
      selector:         Optional bounded, safe dotted/bracket path into a
                        JSON document (e.g. ``"a.b.0.c"``) -- pure read-only
                        traversal, no expression language. Ignored for XML.

    Returns:
      A stable envelope: ``{schema_version, source_ref, format, mime,
      size_bytes, source_sha256, parser_id, parser_version, result_hash,
      state, shape, bounds, warnings, errors, provenance_ref}``.
      ``source_ref`` is a REDACTED portable reference (basename plus up to
      two parent directory names) -- never the raw machine-local absolute
      path. ``state`` is one of ``"complete"``/``"partial"``/``"failed"``; a
      ``"partial"`` result is still useful but must never be treated as
      complete. Every failure mode (unsupported format, oversized/malformed/
      DTD-bearing input, path-policy violation, timeout) is reported via a
      structured entry in ``errors``/``warnings`` using one of the stable
      codes (``unsupported``/``limit_exceeded``/``malformed``/``denied``/
      ``timeout``/``partial``) -- this tool never raises for a bad input
      file. ``provenance_ref`` is always ``None`` here: this tool never
      persists anything to shared Meridian state on its own; a caller that
      wants to bind an inspection to a run/artifact should pass this
      envelope's ``result_hash``/``source_sha256`` to ``meridian-outputs``
      (``record_provenance``/``bind_artifact_provenance``) itself.
    """
    return inspector.inspect_file(
        path,
        format=format,
        max_bytes=max_bytes,
        max_depth=max_depth,
        max_items=max_items,
        preview_chars=preview_chars,
        timeout_seconds=timeout_seconds,
        allowed_root=allowed_root,
        allow_symlinks=allow_symlinks,
        selector=selector,
    )


@mcp.tool()
def inspect_tabular_file(
    path: str,
    format: str = "auto",
    max_bytes: int = tabular.DEFAULT_MAX_BYTES,
    max_decompressed_bytes: int = tabular.DEFAULT_MAX_DECOMPRESSED_BYTES,
    max_depth: int = tabular.DEFAULT_MAX_DEPTH,
    max_items: int = tabular.DEFAULT_MAX_ITEMS,
    max_columns: int = tabular.DEFAULT_MAX_COLUMNS,
    max_sample_rows: int = tabular.DEFAULT_MAX_SAMPLE_ROWS,
    preview_chars: int = tabular.DEFAULT_PREVIEW_CHARS,
    timeout_seconds: float = tabular.DEFAULT_TIMEOUT_SECONDS,
    allowed_root: str | None = None,
    allow_symlinks: bool = False,
    allow_extension_network_install: bool = False,
) -> dict[str, Any]:
    """Inspect exactly ONE local CSV, JSON, or XLSX file's TABULAR shape
    (schema, a bounded row sample, and a row count) through DuckDB -- never
    the full file content, never a second index/database.

    This is the Wave 1 companion to ``inspect_file`` (which covers raw XML
    and generic JSON structure): a bare JSON array-of-objects or JSON-Lines
    file is treated here as ROWS with COLUMNS (via DuckDB's ``read_json``),
    a genuinely different and complementary view from ``inspect_file``'s
    generic key/structure summary of the same bytes.

    Fully local and read-only: no directory walk, no shell, no writes, no
    database/cache persistence. The only network access this tool can ever
    make is a ONE-TIME fetch of DuckDB's ``excel`` core extension for
    ``.xlsx`` files, and ONLY if that extension isn't already cached AND
    ``allow_extension_network_install=True`` is explicitly passed --
    otherwise an ``.xlsx`` request on a machine that has never cached the
    extension is refused with ``denied``/``xlsx_extension_unavailable``
    rather than silently reaching the network. Secret-named files are
    refused before ever being opened, exactly like ``inspect_file``.

    Args:
      path:             Path to the single file to inspect.
      format:           ``"auto"`` (default, sniffed from content -- CSV has
                        no magic-byte signature, so its sniff is a content-
                        shape heuristic: decodable text with a recognized
                        delimiter in the first line), ``"csv"``, ``"json"``,
                        or ``"xlsx"``.
      max_bytes:        Maximum source file size in bytes (default 10 MiB).
      max_decompressed_bytes: For XLSX only -- maximum TOTAL declared
                        uncompressed size across all ZIP members (summed
                        from the central directory, never actually
                        inflated) before refusing as a zip-bomb shape
                        (default 200 MiB).
      max_depth:        JSON-only nesting-depth prescan bound (default
                        100), applied before DuckDB's JSON reader ever runs.
                        Ignored for csv/xlsx.
      max_items:        JSON-only container-count prescan bound (default
                        50,000). Ignored for csv/xlsx.
      max_columns:      Maximum columns included in the returned schema
                        summary (default 500) -- bounds response size for a
                        pathologically wide file; does not limit what
                        DuckDB itself computes (timeout_seconds/memory do).
      max_sample_rows:  Maximum rows included in the returned sample
                        (default 100).
      preview_chars:    Maximum characters per sampled cell value (default
                        2000).
      timeout_seconds:  Soft wall-clock budget (default 5.0s) shared across
                        the schema/count/sample queries -- a query still
                        running past this is cancelled via DuckDB's
                        cross-thread ``interrupt()`` and reported as
                        ``timeout``, never left to run unbounded.
      allowed_root:     Optional directory the resolved ``path`` must fall
                        under -- a path escaping it is refused as
                        ``denied``/``outside_allowed_root``.
      allow_symlinks:   Set True to permit inspecting a symlink target
                        (default False).
      allow_extension_network_install: Set True to allow a one-time network
                        fetch of DuckDB's ``excel`` extension if it isn't
                        already cached locally (default False -- refuses
                        instead of making an implicit network call).

    Returns:
      The same stable envelope shape as ``inspect_file``:
      ``{schema_version, source_ref, format, mime, size_bytes,
      source_sha256, parser_id, parser_version, result_hash, state, shape,
      bounds, warnings, errors, provenance_ref}``. ``parser_id`` is one of
      ``"duckdb-csv"``/``"duckdb-json"``/``"duckdb-excel"``. ``shape``
      contains ``row_count`` (an ``{value, exact}`` pair -- ``exact`` is
      ``false`` only if the count query itself timed out/errored, NEVER
      presented as a false-exact value), ``column_count``, ``columns``
      (``[{name, type}, ...]``, possibly truncated), ``sample_rows``
      (possibly truncated), and truncation flags. ``state`` is one of
      ``"complete"``/``"partial"``/``"failed"`` -- a ``"partial"`` result
      (e.g. schema succeeded but the count or sample query hit the
      timeout) is still useful but must never be treated as complete.
    """
    return tabular.inspect_tabular_file(
        path,
        format=format,
        max_bytes=max_bytes,
        max_decompressed_bytes=max_decompressed_bytes,
        max_depth=max_depth,
        max_items=max_items,
        max_columns=max_columns,
        max_sample_rows=max_sample_rows,
        preview_chars=preview_chars,
        timeout_seconds=timeout_seconds,
        allowed_root=allowed_root,
        allow_symlinks=allow_symlinks,
        allow_extension_network_install=allow_extension_network_install,
    )


def main() -> None:
    """Console entry point (``uvx --from <path> meridian-file-inspection-mcp``)."""
    mcp.run()


if __name__ == "__main__":
    main()
