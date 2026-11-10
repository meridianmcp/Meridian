"""Choose which code-search backend to use for a repository.

Pure selection logic. This module does no I/O, makes no MCP calls and imports
no search backend. The caller reports how large the indexed code is and which
backends are available, and gets back the tool name and a short reason.

Order of preference
-------------------
1. ``codebase-memory`` when the indexed code is at or above the size threshold
   AND a codebase-memory index exists. A prebuilt graph index pays for itself on
   large trees.
2. ``regex`` when the indexed code is below the size threshold. Below the
   threshold a plain regex search is fast enough and needs no index, so regex is
   the first choice even when an index, Serena or BM25 is available. Serena and
   BM25 are not consulted below the threshold.
3. ``serena`` at or above the threshold, when there is no codebase-memory index
   and Serena (LSP-backed symbol tools) is available.
4. ``bm25`` at or above the threshold, when there is no codebase-memory index,
   no Serena, and a BM25 lexical index is available.
5. ``regex`` as the last resort at or above the threshold, when none of the
   above applies. This includes the case where no index or backend exists at all.

Threshold
---------
``size_threshold_bytes`` defaults to 50 MiB (``50 * 1024 * 1024`` bytes). The
comparison is ``indexed_bytes >= size_threshold_bytes``, so a tree exactly at the
threshold counts as large. A threshold of 0 makes every tree large.

This module is not wired into the MCP server or the guard hooks yet. See the
``not_done`` list in the wave report for the remaining wiring.
"""
from __future__ import annotations

TOOL_CODEBASE_MEMORY = "codebase-memory"
TOOL_SERENA = "serena"
TOOL_BM25 = "bm25"
TOOL_REGEX = "regex"

DEFAULT_SIZE_THRESHOLD_BYTES = 50 * 1024 * 1024


def _require_non_negative_int(name: str, value: object) -> int:
    # bool is a subclass of int, so reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value}")
    return value


def choose_code_search_tool(
    indexed_bytes: int,
    has_codebase_index: bool,
    has_serena: bool,
    has_bm25: bool,
    size_threshold_bytes: int = DEFAULT_SIZE_THRESHOLD_BYTES,
) -> tuple[str, str]:
    """Return ``(tool, reason)`` for the best available code-search backend.

    ``tool`` is one of ``TOOL_CODEBASE_MEMORY``, ``TOOL_SERENA``, ``TOOL_BM25``
    or ``TOOL_REGEX``. ``reason`` is a one-sentence explanation for logs and
    receipts. The selection order is described in the module docstring.

    Raises ``TypeError`` for non-int sizes and ``ValueError`` for negative sizes.
    """
    size = _require_non_negative_int("indexed_bytes", indexed_bytes)
    threshold = _require_non_negative_int("size_threshold_bytes", size_threshold_bytes)

    if size < threshold:
        return (
            TOOL_REGEX,
            f"indexed code is {size} bytes, below the {threshold}-byte threshold; "
            "regex is the first choice for small trees",
        )

    if has_codebase_index:
        return (
            TOOL_CODEBASE_MEMORY,
            f"indexed code is {size} bytes, at or above the {threshold}-byte threshold, "
            "and a codebase-memory index exists",
        )

    if has_serena:
        return (
            TOOL_SERENA,
            f"indexed code is {size} bytes, at or above the {threshold}-byte threshold, "
            "with no codebase-memory index; Serena is available",
        )

    if has_bm25:
        return (
            TOOL_BM25,
            f"indexed code is {size} bytes, at or above the {threshold}-byte threshold, "
            "with no codebase-memory index and no Serena; BM25 is available",
        )

    # Reaching here means no codebase-memory index, no Serena and no BM25.
    return (
        TOOL_REGEX,
        f"indexed code is {size} bytes, at or above the {threshold}-byte threshold, "
        "but no index or search backend is available; regex is the only option",
    )
