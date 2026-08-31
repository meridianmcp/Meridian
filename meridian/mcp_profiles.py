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
# orient -> plan -> research -> record -> execute -> verify/hand off.  It is
# deliberately broad enough to be useful as a real product, but stays below
# the 70-tool review target.  It excludes GitHub dynamic tools, tunnel
# plugins, admin/configuration controls, raw logs, and local-path
# document/code tools from the first directory review.
OPENAI_PUBLIC_TOOL_NAMES: FrozenSet[str] = frozenset(
    {
        "create_project",
        "get_project_by_name",
        "start_session",
        "get_goal",
        "set_goal",
        "set_north_star",
        "get_session_brief",
        "get_planning_brief",
        "get_sprint_progress",
        "get_sprint_items",
        "get_context_block",
        "refresh_context",
        "load_handoff",
        "verify_handoff_token",
        "accept_handoff",
        "generate_handoff",
        "log_task",
        "get_tasks",
        "search_tasks",
        "search_all",
        "checkpoint",
        "add_note",
        "get_notes",
        "read_note",
        "pin_decision",
        "update_decision",
        "get_pinned_decisions",
        "validate_assumption",
        "capture_research_finding",
        "add_workspace_proposal",
        "get_workspace_proposals",
        "advance_proposal_status",
        "promote_proposal",
        "preview_proposal_promotion",
        "set_sprint",
        "add_sprint_item",
        "update_sprint_item",
        "claim_sprint_item",
        "complete_sprint_item",
        "add_sprint_item_pointer",
        "get_sprint_item_pointers",
        "resolve_sprint_item_pointers",
        "add_sprint_note",
        "get_sprint_notes",
        "get_parallelizable_groups",
        "assign_sprint_waves",
        "complete_wave_gate",
        "start_wave_run",
        "finalize_wave_run",
        "resume_wave",
        "analyze_sprint",
        "request_hitl",
        "get_hitl_request",
        "list_hitl_requests",
        "paper_search",
        "social_search",
        "github_search",
        "search_synthesis",
        "get_capability_manifest",
        "get_effective_capability_profile",
        "add_workspace_note",
        "get_workspace_notes",
        "pin_workspace_decision",
        "get_workspace_decisions",
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
