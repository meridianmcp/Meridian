"""meridian-outputs: standalone local MCP server for outputs indexing."""
from __future__ import annotations

# 7518bfcd -- fingerprint-based staleness invalidation. Re-exported at the
# package level (not wired into server.py's tool surface yet -- that
# registration is left to the item that actually exposes MCP tools for it)
# so other in-progress modules in this package can `from meridian_outputs
# import tag_output` etc. without reaching into the submodule directly.
from .fingerprint import (
    ScriptTaggedFingerprint,
    StalenessResult,
    check_staleness,
    find_stale_by_script,
    script_content_hash,
    tag_output,
)

__version__ = "0.1.0"

__all__ = [
    "ScriptTaggedFingerprint",
    "StalenessResult",
    "check_staleness",
    "find_stale_by_script",
    "script_content_hash",
    "tag_output",
]
