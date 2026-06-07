"""G5.19 / G5.20 — workspace-member roles and GitHub access caps.

The tenant *owner* (the email tied to the tenant row itself) implicitly has
every permission. Invitees, listed in ``workspace_members``, carry an
explicit role and a github_access cap.

Roles
-----
``owner``   tenant owner; alias of the implicit case above when an invitee
            is co-promoted to full access. Can do anything.
``admin``   tenant-management permissions short of billing/deletion.
``member``  read + write project data; no team/billing/settings changes.
``viewer``  read-only.

GitHub access
-------------
Repo-touching MCP tools dispatch with the *owner's* stored github_pat
(item G5.21 — per-member OAuth — is intentionally backlog). The
``github_access`` column caps what an invitee can do with that token:

``none``    no GitHub tools at all.
``read``    list_files / read_file / search_code / get_commit / git_log.
``write``   adds create_branch / commit / push.

Defaults: viewer → ``none``, member → ``read``, admin/owner → ``write``.
"""
from __future__ import annotations

from typing import Final

ROLE_OWNER: Final[str] = "owner"
ROLE_ADMIN: Final[str] = "admin"
ROLE_MEMBER: Final[str] = "member"
ROLE_VIEWER: Final[str] = "viewer"

VALID_ROLES: Final[frozenset[str]] = frozenset(
    {ROLE_OWNER, ROLE_ADMIN, ROLE_MEMBER, ROLE_VIEWER}
)

# Permission names. Keep short, action-shaped.
PERM_READ = "read"            # GET routes
PERM_WRITE = "write"          # any project-data mutation
PERM_HITL_ANSWER = "hitl"     # respond to / dismiss HITL requests
PERM_INVITE = "invite"        # add or remove workspace members
PERM_SETTINGS = "settings"    # change tenant-level settings
PERM_BILLING = "billing"      # open Stripe portal, change plan
PERM_DELETE_TENANT = "delete" # /account/delete

ROLE_PERMS: Final[dict[str, frozenset[str]]] = {
    ROLE_OWNER: frozenset({
        PERM_READ, PERM_WRITE, PERM_HITL_ANSWER,
        PERM_INVITE, PERM_SETTINGS, PERM_BILLING, PERM_DELETE_TENANT,
    }),
    ROLE_ADMIN: frozenset({
        PERM_READ, PERM_WRITE, PERM_HITL_ANSWER,
        PERM_INVITE, PERM_SETTINGS,
    }),
    ROLE_MEMBER: frozenset({PERM_READ, PERM_WRITE}),
    ROLE_VIEWER: frozenset({PERM_READ}),
}

GITHUB_ACCESS_NONE = "none"
GITHUB_ACCESS_READ = "read"
GITHUB_ACCESS_WRITE = "write"

VALID_GITHUB_ACCESS: Final[frozenset[str]] = frozenset(
    {GITHUB_ACCESS_NONE, GITHUB_ACCESS_READ, GITHUB_ACCESS_WRITE}
)

# Per-role default for the github_access column on new invites. Operators
# can override at invite time; the column on workspace_members is the
# source of truth at dispatch.
DEFAULT_GITHUB_ACCESS_FOR_ROLE: Final[dict[str, str]] = {
    ROLE_OWNER: GITHUB_ACCESS_WRITE,
    ROLE_ADMIN: GITHUB_ACCESS_WRITE,
    ROLE_MEMBER: GITHUB_ACCESS_READ,
    ROLE_VIEWER: GITHUB_ACCESS_NONE,
}

# Read-only GitHub tools; everything else counts as write.
GITHUB_READ_ONLY_TOOLS: Final[frozenset[str]] = frozenset({
    "list_files", "read_file", "search_code", "get_commit", "git_log",
})


def has_perm(role: str | None, perm: str) -> bool:
    """Return True when ``role`` grants ``perm``. Unknown roles → False."""
    if not role:
        return False
    return perm in ROLE_PERMS.get(role, frozenset())


def can_github(access: str | None, tool_name: str) -> bool:
    """Return True when ``access`` permits dispatching the named GitHub
    tool. ``write`` allows everything, ``read`` covers ``GITHUB_READ_ONLY_TOOLS``,
    ``none`` (or unknown) blocks everything."""
    if access == GITHUB_ACCESS_WRITE:
        return True
    if access == GITHUB_ACCESS_READ:
        return tool_name in GITHUB_READ_ONLY_TOOLS
    return False


def default_github_access_for_role(role: str | None) -> str:
    if not role:
        return GITHUB_ACCESS_NONE
    return DEFAULT_GITHUB_ACCESS_FOR_ROLE.get(role, GITHUB_ACCESS_NONE)
