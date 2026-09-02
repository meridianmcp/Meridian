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

from . import inspector

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


def main() -> None:
    """Console entry point (``uvx --from <path> meridian-file-inspection-mcp``)."""
    mcp.run()


if __name__ == "__main__":
    main()
