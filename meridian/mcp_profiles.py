"""Curated MCP tool profiles for directory submissions and constrained clients.

The default Meridian endpoint intentionally keeps its complete native surface
and optional tenant/tunnel tools.  A public directory listing needs a smaller,
stable contract: enough of the long-horizon workflow to be useful, without
exposing maintenance, account, tunnel, raw-log, or repository-write controls.

This module is deliberately pure.  It only defines profile names and ordered
allowlists; transport/authentication and per-tenant authorization remain in the
existing MCP handler.  An empty or unknown profile is never interpreted as
"allow everything".
"""
from __future__ import annotations

from typing import FrozenSet


OPENAI_PUBLIC_PROFILE = "openai"

# Keep the first public profile to the product's durable long-horizon workflow:
# orient -> plan -> record -> execute -> verify/hand off.  It intentionally
# excludes GitHub dynamic tools, tunnel plugins, admin/configuration controls,
# raw logs, and local-path document/code tools from the first directory review.
OPENAI_PUBLIC_TOOL_NAMES: FrozenSet[str] = frozenset(
    {
        "start_session",
        "get_planning_brief",
        "get_sprint_items",
        "get_sprint_progress",
        "get_context_block",
        "refresh_context",
        "log_task",
        "add_note",
        "get_notes",
        "read_note",
        "search_all",
        "search_tasks",
        "pin_decision",
        "get_pinned_decisions",
        "add_sprint_item",
        "update_sprint_item",
        "claim_sprint_item",
        "complete_sprint_item",
        "add_sprint_item_pointer",
        "get_sprint_item_pointers",
        "resolve_sprint_item_pointers",
        "checkpoint",
        "generate_handoff",
        "request_hitl",
        "get_hitl_request",
        "list_hitl_requests",
        "add_workspace_proposal",
        "get_workspace_proposals",
        "advance_proposal_status",
        "paper_search",
    }
)

_PROFILES: dict[str, FrozenSet[str]] = {
    OPENAI_PUBLIC_PROFILE: OPENAI_PUBLIC_TOOL_NAMES,
}


def get_tool_allowlist(profile: str | None) -> FrozenSet[str] | None:
    """Return the immutable allowlist for *profile*, or ``None`` by default.

    ``None`` means the ordinary full Meridian endpoint and preserves backwards
    compatibility.  A named profile must be known; callers should reject an
    unknown profile rather than accidentally serving the full surface.
    """
    if profile is None:
        return None
    try:
        return _PROFILES[profile]
    except KeyError as exc:
        raise ValueError(f"unknown MCP tool profile: {profile!r}") from exc

