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

# 19917525 -- first-class derived-artifact cache and fast variant rendering.
# Re-exported at the package level for the same reason fingerprint's own
# public API is above: other in-package/downstream callers can
# `from meridian_outputs import get_or_render_variant` etc. without reaching
# into the submodule directly. Wired into server.py's MCP tool surface too
# (unlike fingerprint's original wave-1 landing) -- see that module's own
# docstring for the full contract.
from .derived_cache import (
    DerivedArtifactEntry,
    DerivedCacheConvergenceState,
    compute_cache_key,
    evict_to_budget,
    get_cache_stats,
    get_cached_variant,
    get_convergence_state as get_derived_cache_convergence_state,
    get_or_render_variant,
    invalidate_stale_sources,
    invalidate_variant,
    put_cached_variant,
)

__version__ = "0.1.0"

__all__ = [
    "ScriptTaggedFingerprint",
    "StalenessResult",
    "check_staleness",
    "find_stale_by_script",
    "script_content_hash",
    "tag_output",
    "DerivedArtifactEntry",
    "DerivedCacheConvergenceState",
    "compute_cache_key",
    "evict_to_budget",
    "get_cache_stats",
    "get_cached_variant",
    "get_derived_cache_convergence_state",
    "get_or_render_variant",
    "invalidate_stale_sources",
    "invalidate_variant",
    "put_cached_variant",
]
