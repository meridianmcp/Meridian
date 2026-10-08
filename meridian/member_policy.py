"""Configurable policy for project-scoped workspace members.

A *project-scoped* member is someone invited to one or more specific projects
of another tenant's workspace (``workspace_members.project_id`` set, no
workspace-wide row). What they may do inside that scope is decided per PERSON
from five capabilities:

``write_project``          edit everything inside their projects and subprojects
``create_subproject``      create a subproject under a top-level project of theirs
``delete_subproject``      delete / archive / merge away / detach / move a subproject
``delete_project``         delete / archive / merge away / nest a top-level project
``write_workspace_level``  change workspace-level state and create top-level projects

The owner sets the workspace default; each person may carry an override. The
built-in defaults are ``BUILTIN_DEFAULTS``. Owners, workspace-wide members,
non-members, self-hosted, demo, stdio and header-less callers are never scoped,
so none of this runs for them (``resolve_member_scope`` returns ``None`` after
the one membership SELECT they already paid).

This module is the single place that knows how to
  * resolve the effective policy for a person (``compute_effective_policy``),
  * turn a member's explicit project grants into their full scope, grants plus
    the live one-level subprojects of those grants (``resolve_member_scope``,
    the ONLY caller of ``db.get_scoped_project_ids_for_member``),
  * carry scope and policy together so no call site can hold one without the
    other (``ScopedProjectIds``),
  * decide which capabilities an operation needs (``classify_target`` and the
    ``required_caps_for_*`` functions), shared by the HTTP and MCP gates so the
    two transports can never disagree.

It does no I/O of its own: database access goes through the ``meridian.db``
functions passed in or looked up at call time (so tests can patch them).
"""
from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from .roles import ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER, ROLE_VIEWER

_log = logging.getLogger(__name__)

POLICY_REFUSAL: Final[str] = "member policy"

# ---------------------------------------------------------------------------
# Capability catalog
# ---------------------------------------------------------------------------

WRITE_PROJECT: Final[str] = "write_project"
CREATE_SUBPROJECT: Final[str] = "create_subproject"
DELETE_SUBPROJECT: Final[str] = "delete_subproject"
DELETE_PROJECT: Final[str] = "delete_project"
WRITE_WORKSPACE_LEVEL: Final[str] = "write_workspace_level"


@dataclass(frozen=True)
class Capability:
    """One entry of the catalog.

    ``sentence`` is the owner-facing toggle copy, ``denial`` the member-facing
    refusal sentence (the owner's email is appended by ``MemberPolicyDenied``),
    ``covers`` a short list of what the capability governs for the settings
    card, ``meaning`` the precise definition.
    """

    id: str
    label: str
    sentence: str
    denial: str
    builtin_default: bool
    covers: tuple[str, ...]
    meaning: str


CAPABILITIES: Final[tuple[Capability, ...]] = (
    Capability(
        id=WRITE_PROJECT,
        label="Edit project content",
        sentence="Edit everything in their project and its subprojects",
        denial=(
            "Your access to this workspace is read-only. "
            "You do not have the Edit project content permission."
        ),
        builtin_default=True,
        covers=(
            "tasks, sessions, sprint items, notes and pinned decisions",
            "goal, north star, profile layers and capability manifests of the project",
            "renaming the project and deleting its own data",
            "GitHub write tools bound to the project",
        ),
        meaning=(
            "Write anything inside every project in the member's scope: their "
            "invited projects plus all existing and later-created subprojects of "
            "those projects. Excludes project lifecycle (create top-level, delete, "
            "archive, merge, re-parent) and workspace-level state. Switching it off "
            "makes the member read-only and forces the other four capabilities off."
        ),
    ),
    Capability(
        id=CREATE_SUBPROJECT,
        label="Create subprojects",
        sentence="Create new subprojects under their project",
        denial="You do not have the Create subprojects permission in this workspace.",
        builtin_default=True,
        covers=("creating a subproject under a top-level project in their scope",),
        meaning=(
            "Create a subproject under a top-level project inside the member's "
            "scope. Also the 'nest' half of re-parenting, which additionally needs "
            "Delete their project (moving a top-level project down) or Delete "
            "subprojects (moving a subproject between parents)."
        ),
    ),
    Capability(
        id=DELETE_SUBPROJECT,
        label="Delete subprojects",
        sentence="Delete subprojects (also covers detaching, archiving or merging one away)",
        denial=(
            "You cannot delete, detach, archive or merge away subprojects "
            "(permission: Delete subprojects)."
        ),
        builtin_default=False,
        covers=(
            "deleting a subproject",
            "archiving or restoring a subproject",
            "merging a subproject away",
            "detaching a subproject from its parent or moving it to another parent",
        ),
        meaning=(
            "Destroy or retire a subproject. Also required, together with Delete "
            "their project, to delete, archive or merge a project that still has "
            "subprojects (the children travel in the same request)."
        ),
    ),
    Capability(
        id=DELETE_PROJECT,
        label="Delete their project",
        sentence="Delete their project (a project with subprojects can only be deleted together with them)",
        denial="You cannot delete this project (permission: Delete their project).",
        builtin_default=False,
        covers=(
            "deleting a top-level project of theirs",
            "archiving or restoring it",
            "merging it away",
            "nesting it under another project",
        ),
        meaning=(
            "Destroy or retire a top-level project in the member's scope. A project "
            "that still has subprojects additionally needs Delete subprojects and "
            "the children in the same request."
        ),
    ),
    Capability(
        id=WRITE_WORKSPACE_LEVEL,
        label="Change workspace-level things",
        sentence="Change workspace-level things (notes, decisions, settings, profiles, new top-level projects)",
        denial=(
            "Workspace-level changes (notes, decisions, settings, profiles, new "
            "top-level projects) are managed by the owner. You do not have the "
            "Change workspace-level things permission."
        ),
        builtin_default=False,
        covers=(
            "workspace notes, decisions, proposals and sprint items",
            "workspace settings and blog posts",
            "workspace, user and default profile layers and tenant capability profiles",
            "creating a top-level project and detaching a subproject",
        ),
        meaning=(
            "Write state that belongs to no single project: workspace notes, "
            "decisions, proposals and sprint items, workspace settings, tenant-level "
            "profile layers and capability profiles, the manual-issue-screening "
            "toggle, GitHub connections, creating a top-level project and the "
            "detach leg of re-parenting. Reads stay open. Invites, billing, tokens "
            "and account routes are not governed by this capability."
        ),
    ),
)

CAPABILITY_IDS: Final[tuple[str, ...]] = tuple(c.id for c in CAPABILITIES)
CAPABILITY_BY_ID: Final[Mapping[str, Capability]] = MappingProxyType({c.id: c for c in CAPABILITIES})
BUILTIN_DEFAULTS: Final[Mapping[str, bool]] = MappingProxyType(
    {c.id: c.builtin_default for c in CAPABILITIES}
)

_CAP_ORDER: Final[Mapping[str, int]] = MappingProxyType({cid: i for i, cid in enumerate(CAPABILITY_IDS)})

SOURCE_BUILTIN: Final[str] = "builtin"
SOURCE_DEFAULT: Final[str] = "default"
SOURCE_OVERRIDE: Final[str] = "override"
SOURCE_ROLE_CLAMP: Final[str] = "role_clamp"
SOURCE_IMPLICATION: Final[str] = "implication"
SOURCES: Final[tuple[str, ...]] = (
    SOURCE_BUILTIN, SOURCE_DEFAULT, SOURCE_OVERRIDE, SOURCE_ROLE_CLAMP, SOURCE_IMPLICATION,
)

REASON_VIEWER_ROLE: Final[str] = "viewer_role"
REASON_NO_MEMBERSHIP: Final[str] = "no_accepted_membership"
REASON_WRITE_PROJECT_OFF: Final[str] = "write_project_off"
REASON_CORRUPT_DEFAULT: Final[str] = "corrupt_default_row"
REASON_CORRUPT_OVERRIDE: Final[str] = "corrupt_override_row"


def sort_capabilities(caps: Iterable[str]) -> list[str]:
    """Catalog order; an id the catalog does not know sorts last (and is denied)."""
    return sorted(set(caps), key=lambda c: (_CAP_ORDER.get(c, len(_CAP_ORDER)), c))


# ---------------------------------------------------------------------------
# Effective policy
# ---------------------------------------------------------------------------

_ROLE_RANK: Final[Mapping[str, int]] = MappingProxyType({
    ROLE_VIEWER: 0, ROLE_MEMBER: 1, ROLE_ADMIN: 2, ROLE_OWNER: 3,
})


def most_restrictive_role(roles: Iterable[Any]) -> "str | None":
    """The least privileged role across a person's accepted rows.

    ``roles`` may be the ``[(role, project_id), ...]`` that ``db.get_member_roles``
    returns or bare role strings. An unknown or empty role counts as ``viewer``.
    ``None`` when there are no rows at all.
    """
    best: "str | None" = None
    best_rank = 99
    for entry in roles:
        role = entry[0] if isinstance(entry, (tuple, list)) else entry
        name = role if isinstance(role, str) and role in _ROLE_RANK else ROLE_VIEWER
        rank = _ROLE_RANK[name]
        if rank < best_rank:
            best, best_rank = name, rank
    return best


@dataclass(frozen=True)
class EffectivePolicy:
    """What one scoped member may do: the capabilities that are on, where each
    value came from (``builtin|default|override|role_clamp|implication``), the
    role it was clamped by and, for a denied capability whose cause is not a
    plain setting, a machine-readable reason."""

    caps: frozenset[str]
    sources: Mapping[str, str] = field(default_factory=dict)
    role: "str | None" = None
    reasons: Mapping[str, str] = field(default_factory=dict)

    def allows(self, capability: str) -> bool:
        return capability in self.caps

    def source(self, capability: str) -> str:
        return self.sources.get(capability, SOURCE_BUILTIN)

    def as_dict(self) -> dict[str, bool]:
        """``{capability_id: bool}`` in catalog order, for every catalog id."""
        return {cid: cid in self.caps for cid in CAPABILITY_IDS}

    def readout(self) -> dict[str, dict[str, Any]]:
        """``{id: {allowed, source, reason}}`` for the settings readout."""
        return {
            cid: {
                "allowed": cid in self.caps,
                "source": self.source(cid),
                "reason": self.reasons.get(cid),
            }
            for cid in CAPABILITY_IDS
        }


def _parse_policy_row(raw: Any) -> "tuple[dict[str, Any] | None, bool]":
    """``(object, corrupt)`` for one stored policy row. ``None`` (no row) is not
    corrupt; anything that is not a JSON object is."""
    if raw is None:
        return None, False
    if isinstance(raw, Mapping):
        return dict(raw), False
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            return None, True
    if not isinstance(raw, str):
        return None, True
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        return None, True
    if not isinstance(obj, dict):
        return None, True
    return obj, False


def compute_effective_policy(
    default: Any = None,
    override: Any = None,
    roles: "Iterable[Any] | None" = None,
) -> EffectivePolicy:
    """Resolve the effective policy of one scoped member.

    ``default`` and ``override`` are the stored JSON of the workspace default row
    and the person's override row (text, or an already parsed dict; ``None`` for
    no row). ``roles`` is the person's accepted rows, see
    :func:`most_restrictive_role`.

    Order: built-in defaults, then the workspace default, then the person's
    override (``null``/absent inherits), then the role clamp, then the
    implication (``write_project`` off forces the other four off). Fail closed:
    an unparseable default or override row denies all five capabilities; a value
    that is not a boolean for a known key is ``False``; unknown keys are ignored.
    """
    caps: dict[str, bool] = dict(BUILTIN_DEFAULTS)
    sources: dict[str, str] = {cid: SOURCE_BUILTIN for cid in CAPABILITY_IDS}
    reasons: dict[str, str] = {}

    default_obj, default_corrupt = _parse_policy_row(default)
    override_obj, override_corrupt = _parse_policy_row(override)
    role = most_restrictive_role(roles if roles is not None else (ROLE_MEMBER,))

    if default_corrupt or override_corrupt:
        source = SOURCE_DEFAULT if default_corrupt else SOURCE_OVERRIDE
        reason = REASON_CORRUPT_DEFAULT if default_corrupt else REASON_CORRUPT_OVERRIDE
        return EffectivePolicy(
            caps=frozenset(),
            sources=MappingProxyType({cid: source for cid in CAPABILITY_IDS}),
            role=role,
            reasons=MappingProxyType({cid: reason for cid in CAPABILITY_IDS}),
        )

    if default_obj:
        for cid in CAPABILITY_IDS:
            if cid in default_obj:
                value = default_obj[cid]
                caps[cid] = value if isinstance(value, bool) else False
                sources[cid] = SOURCE_DEFAULT
    if override_obj:
        for cid in CAPABILITY_IDS:
            value = override_obj.get(cid)
            if value is None:
                continue  # absent or null: inherit
            caps[cid] = value if isinstance(value, bool) else False
            sources[cid] = SOURCE_OVERRIDE

    if role is None or role == ROLE_VIEWER:
        for cid in CAPABILITY_IDS:
            caps[cid] = False
            sources[cid] = SOURCE_ROLE_CLAMP
            reasons[cid] = REASON_NO_MEMBERSHIP if role is None else REASON_VIEWER_ROLE
    elif not caps[WRITE_PROJECT]:
        for cid in CAPABILITY_IDS:
            if cid == WRITE_PROJECT:
                continue
            caps[cid] = False
            sources[cid] = SOURCE_IMPLICATION
            reasons[cid] = REASON_WRITE_PROJECT_OFF

    return EffectivePolicy(
        caps=frozenset(cid for cid, on in caps.items() if on),
        sources=MappingProxyType(sources),
        role=role,
        reasons=MappingProxyType(reasons),
    )


# What a plain list of ids means where there is no stored policy to consult
# (self-hosted and test mode): the built-in defaults for a plain member.
BUILTIN_POLICY: Final[EffectivePolicy] = compute_effective_policy(None, None, (ROLE_MEMBER,))


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class MemberPolicyDenied(ValueError):
    """A scoped member lacks a capability the operation needs.

    ``str(exc)`` is the short form MCP returns as the JSON-RPC message
    (``member policy: capability <id> required``); :attr:`detail` and
    :meth:`body` are the HTTP 403 form. A ``ValueError`` so the MCP dispatcher
    already maps it to ``-32603``.
    """

    status_code = 403
    error = "capability_required"

    def __init__(
        self,
        capability: str,
        *,
        label: "str | None" = None,
        sentence: "str | None" = None,
        owner_email: "str | None" = None,
        target: "str | None" = None,
        workspace_tenant_id: "str | None" = None,
        reason: "str | None" = None,
    ) -> None:
        cap = CAPABILITY_BY_ID.get(capability)
        self.capability = capability
        self.label = label or (cap.label if cap else capability)
        self.sentence = sentence or (
            cap.denial if cap else f"You do not have the {self.label} permission."
        )
        self.owner_email = owner_email
        self.target = target
        self.workspace_tenant_id = workspace_tenant_id
        self.reason = reason
        super().__init__(f"{POLICY_REFUSAL}: capability {capability} required")

    @property
    def detail(self) -> str:
        who = self.owner_email or "the workspace owner"
        return (
            f"{POLICY_REFUSAL}: capability {self.capability} required - "
            f"{self.sentence} Ask {who} to enable it."
        )

    def body(self) -> dict[str, Any]:
        """The top-level JSON of the HTTP 403."""
        return {
            "detail": self.detail,
            "error": self.error,
            "capability": self.capability,
            "capability_label": self.label,
            "workspace_owner": self.owner_email,
        }


class ProjectScopeDenied(ValueError):
    """A project named by an operation is outside the member's scope. Same
    wording as the scope middleware (HTTP) and the MCP scope guard."""

    status_code = 403
    detail = "Project is outside your access scope."

    def __init__(self) -> None:
        super().__init__("project is outside your access scope")


class MemberPolicyUnavailable(RuntimeError):
    """Policy could not be evaluated (hosted caller holding a scope that carries
    no policy, or an unreadable policy table). Always a refusal, never a pass."""

    status_code = 503
    detail = "policy check unavailable"

    def __init__(self, message: str = "policy check unavailable") -> None:
        super().__init__(message)


def orphaning_message(name: str, count: int) -> str:
    """The 409 for deleting a project without the subprojects it would orphan."""
    noun = "subproject" if count == 1 else "subprojects"
    return (
        f"{POLICY_REFUSAL}: deleting {name} would orphan {count} {noun}; "
        "include them in the same request"
    )


# ---------------------------------------------------------------------------
# The scope carrier
# ---------------------------------------------------------------------------

class ScopedProjectIds(list):
    """A member's scope: the project ids they reach, with their policy attached.

    Behaves as the flat ``list[str]`` every scope consumer already takes (``in``,
    ``set()``, ``sorted()``, ``==``). The extra attributes:

    ``explicit``            the grants on ``workspace_members`` rows (tuple)
    ``parent_of``           ``{subproject_id: parent_id}`` for the inherited ones
    ``policy``              the :class:`EffectivePolicy`
    ``role``                the most restrictive role across the person's rows
    ``email`` / ``workspace_tenant_id`` / ``owner_email``  who and where

    ``list(scoped)``, slicing, ``sorted()`` and ``scoped + [...]`` return plain
    lists that carry none of this. That is safe: in hosted mode a plain list
    reaching policy code is refused (:func:`policy_of`), never treated as the
    defaults. ``copy.copy`` keeps the attributes.
    """

    def __init__(
        self,
        ids: "Iterable[str]" = (),
        *,
        explicit: "Iterable[str] | None" = None,
        parent_of: "Mapping[str, str] | None" = None,
        policy: "EffectivePolicy | None" = None,
        role: "str | None" = None,
        email: str = "",
        workspace_tenant_id: str = "",
        owner_email: "str | None" = None,
    ) -> None:
        super().__init__(ids)
        self.explicit: tuple[str, ...] = tuple(self if explicit is None else explicit)
        self.parent_of: dict[str, str] = dict(parent_of or {})
        self.policy: EffectivePolicy = policy if policy is not None else BUILTIN_POLICY
        self.role: "str | None" = role if role is not None else self.policy.role
        self.email = email
        self.workspace_tenant_id = workspace_tenant_id
        self.owner_email = owner_email


def _is_hosted() -> bool:
    from . import _deps  # lazy: _deps imports this module

    return _deps._hosted_mode()


def policy_of(scoped: "Sequence[str]") -> EffectivePolicy:
    """The effective policy of a non-``None`` scope.

    A real scope carries it. A plain list is a copy or an ad hoc list: in hosted
    mode that raises :class:`MemberPolicyUnavailable` (fail closed); self-hosted
    and test mode treat it as the built-in defaults.
    """
    if isinstance(scoped, ScopedProjectIds):
        return scoped.policy
    if _is_hosted():
        raise MemberPolicyUnavailable("policy check unavailable: scope carries no member policy")
    return BUILTIN_POLICY


def allows(scoped: "Sequence[str] | None", capability: str) -> bool:
    """Whether the caller holds ``capability``. ``None`` (not scoped) is always
    allowed: the policy only governs project-scoped members."""
    if scoped is None:
        return True
    return policy_of(scoped).allows(capability)


def denial(
    scoped: "Sequence[str] | None",
    capability: str,
    *,
    target: "str | None" = None,
    sentence: "str | None" = None,
) -> MemberPolicyDenied:
    """Build the refusal for a missing capability from the caller's scope."""
    carrier = scoped if isinstance(scoped, ScopedProjectIds) else None
    reason = None
    if carrier is not None:
        reason = carrier.policy.reasons.get(capability)
    return MemberPolicyDenied(
        capability,
        sentence=sentence,
        owner_email=carrier.owner_email if carrier is not None else None,
        workspace_tenant_id=carrier.workspace_tenant_id if carrier is not None else None,
        target=target,
        reason=reason,
    )


def require_capability(
    scoped: "Sequence[str] | None", capability: str, *, target: "str | None" = None,
) -> None:
    """Raise :class:`MemberPolicyDenied` unless the caller holds ``capability``.
    A no-op for ``None`` (owner, workspace-wide member, self-hosted, demo)."""
    if scoped is None:
        return
    if not policy_of(scoped).allows(capability):
        raise denial(scoped, capability, target=target)


def require_capabilities(
    scoped: "Sequence[str] | None", capabilities: Iterable[str], *, target: "str | None" = None,
) -> None:
    """:func:`require_capability` for every id, reporting the first missing one in
    catalog order so the refusal always names a single capability."""
    if scoped is None:
        return
    policy = policy_of(scoped)
    for capability in sort_capabilities(capabilities):
        if not policy.allows(capability):
            raise denial(scoped, capability, target=target)


# ---------------------------------------------------------------------------
# Resolving a member's scope and policy
# ---------------------------------------------------------------------------

async def _load_policy(auth_db: Any, workspace_tenant_id: str, email: str) -> EffectivePolicy:
    from . import db as db_module

    rows = await db_module.get_member_policy_rows(auth_db, workspace_tenant_id, email)
    roles = await db_module.get_member_roles(auth_db, workspace_tenant_id, email)
    policy = compute_effective_policy(rows.get("default"), rows.get("override"), roles)
    corrupt = {r for r in policy.reasons.values() if r.startswith("corrupt_")}
    if corrupt:
        _log.error(
            "member_policy: unparseable %s for tenant=%s; denying every capability to %s",
            "/".join(sorted(corrupt)), workspace_tenant_id, email,
        )
    return policy


async def resolve_effective_policy(
    auth_db: Any, workspace_tenant_id: str, email: str,
) -> EffectivePolicy:
    """The effective policy of ``email`` in a workspace, from the auth DB alone
    (no tenant-DB access, so it never wakes the owner's database). A read error
    propagates; callers that must not fail the request treat it as all-denied."""
    return await _load_policy(auth_db, workspace_tenant_id, (email or "").strip().lower())


async def _owner_email(auth_db: Any, workspace_tenant_id: str) -> "str | None":
    """The workspace owner's email for the refusal text. Cosmetic, so a failed
    lookup yields ``None`` instead of failing the request."""
    from . import db as db_module

    try:
        tenant = await db_module.get_tenant_by_id(auth_db, workspace_tenant_id)
    except Exception:  # noqa: BLE001
        _log.warning("member_policy: owner email lookup failed for tenant=%s", workspace_tenant_id)
        return None
    email = (tenant or {}).get("email")
    return email or None


async def _tenant_db(tenant_db: Any) -> Any:
    """``tenant_db`` is a connection, or a zero-argument callable returning one
    (sync or async). The callable form lets the request path open the owner's
    database only after the caller turned out to be project-scoped."""
    if hasattr(tenant_db, "execute") or not callable(tenant_db):
        return tenant_db
    opened = tenant_db()
    # Not inspect.isawaitable: an aiosqlite connection is itself awaitable (it
    # starts its thread), so a sync factory returning one must not be awaited.
    return await opened if inspect.iscoroutine(opened) else opened


async def resolve_member_scope(
    auth_db: Any, tenant_db: Any, workspace_tenant_id: str, email: str,
) -> "ScopedProjectIds | None":
    """The caller's scope and policy in a workspace, or ``None`` when not scoped.

    The only caller of ``db.get_scoped_project_ids_for_member`` (a CI test keeps
    it that way): ``None`` from it (owner, workspace-wide member, non-member)
    returns immediately with no further query. Otherwise the explicit grants are
    expanded by one level with ``db.get_subproject_map`` on the owner's tenant
    database, and the member's policy and roles are read from the auth database.
    An empty grant list stays a scoped caller with an empty scope.

    ``tenant_db`` may be the owner's connection or a callable that opens it (see
    :func:`_tenant_db`). Every error propagates: the callers map it to 503, a
    scope is never silently narrowed to nothing or widened to everything.
    """
    from . import db as db_module

    email = (email or "").strip().lower()
    explicit = await db_module.get_scoped_project_ids_for_member(auth_db, workspace_tenant_id, email)
    if explicit is None:
        return None
    explicit_ids = tuple(dict.fromkeys(explicit))
    children: dict[str, str] = {}
    if explicit_ids:
        children = await db_module.get_subproject_map(await _tenant_db(tenant_db), explicit_ids)
    granted = set(explicit_ids)
    ordered = list(explicit_ids) + sorted(c for c in children if c not in granted)
    policy = await _load_policy(auth_db, workspace_tenant_id, email)
    return ScopedProjectIds(
        ordered,
        explicit=explicit_ids,
        parent_of=children,
        policy=policy,
        role=policy.role,
        email=email,
        workspace_tenant_id=workspace_tenant_id,
        owner_email=await _owner_email(auth_db, workspace_tenant_id),
    )


def make_scope(
    ids: "Iterable[str]" = (),
    *,
    policy: "EffectivePolicy | Mapping[str, bool] | None" = None,
    role: str = ROLE_MEMBER,
    explicit: "Iterable[str] | None" = None,
    parent_of: "Mapping[str, str] | None" = None,
    email: str = "member@example.com",
    workspace_tenant_id: str = "workspace-tenant",
    owner_email: "str | None" = "owner@example.com",
) -> ScopedProjectIds:
    """Test helper: a real :class:`ScopedProjectIds` without the databases.

    ``policy`` is an :class:`EffectivePolicy`, or a ``{capability: bool}`` dict
    applied as the person's override on top of the built-in defaults through the
    real resolution (so ``{"write_project": False}`` also turns the rest off).
    ``None`` is the built-in defaults. ``role`` goes through the role clamp.
    """
    if isinstance(policy, EffectivePolicy):
        effective = policy
    else:
        effective = compute_effective_policy(None, dict(policy) if policy else None, (role,))
    unique = list(dict.fromkeys(ids))
    return ScopedProjectIds(
        unique,
        explicit=unique if explicit is None else explicit,
        parent_of=parent_of,
        policy=effective,
        role=effective.role,
        email=email,
        workspace_tenant_id=workspace_tenant_id,
        owner_email=owner_email,
    )


# ---------------------------------------------------------------------------
# Pure decision functions, shared by the HTTP and MCP gates
# ---------------------------------------------------------------------------
#
# A "row" is a project row or a ``db.get_project_family`` entry (with its ``id``
# added) carrying ``parent_project_id`` and, when known, ``parent_exists``,
# ``status`` and ``child_ids``. ``family`` is the ``{id: entry}`` map from
# ``get_project_family``. Every function returns the set of capabilities the
# operation needs, always including ``write_project``; the caller runs
# :func:`require_capabilities` on it after the scope check of every project the
# operation names.

PROJECT_TOP: Final[str] = "top"
PROJECT_SUBPROJECT: Final[str] = "subproject"
PROJECT_ORPHAN: Final[str] = "orphan"


def _g(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def classify_target(row: Any, family: "Mapping[str, Any] | None" = None) -> str:
    """``top`` (no parent), ``subproject`` (a parent that exists) or ``orphan``
    (a parent id with no row). Unknown parent existence counts as ``orphan``: the
    stricter reading, which needs both delete capabilities."""
    parent = _g(row, "parent_project_id")
    if not parent:
        return PROJECT_TOP
    exists = _g(row, "parent_exists")
    if exists is None and family:
        own = family.get(_g(row, "id"))
        exists = _g(own, "parent_exists")
    return PROJECT_SUBPROJECT if exists else PROJECT_ORPHAN


def _child_ids(row: Any, family: "Mapping[str, Any] | None") -> list[str]:
    """Subprojects of the row, from the row itself and from ``family`` (either may
    be the only one that knows)."""
    children = list(_g(row, "child_ids") or ())
    if family:
        children += [c for c in (_g(family.get(_g(row, "id")), "child_ids") or ()) if c not in children]
    return children


def _delete_caps(row: Any, family: "Mapping[str, Any] | None") -> set[str]:
    """What it takes to destroy or retire this project."""
    cls = classify_target(row, family)
    caps = {WRITE_PROJECT}
    if cls in (PROJECT_TOP, PROJECT_ORPHAN):
        caps.add(DELETE_PROJECT)
    if cls in (PROJECT_SUBPROJECT, PROJECT_ORPHAN):
        caps.add(DELETE_SUBPROJECT)
    if _child_ids(row, family):
        caps.update((DELETE_PROJECT, DELETE_SUBPROJECT))
    return caps


def _as_rows(rows: Any) -> list[Any]:
    if rows is None:
        return []
    if isinstance(rows, Mapping):
        return [rows]
    return list(rows)


def required_caps_for_delete(
    rows: Any, family: "Mapping[str, Any] | None" = None,
) -> frozenset[str]:
    """Hard delete of one project or a batch (the AND over every row): a top-level
    project needs ``delete_project``, a subproject ``delete_subproject``, an
    orphan both, and a project that still has subprojects both."""
    caps = {WRITE_PROJECT}
    for row in _as_rows(rows):
        caps |= _delete_caps(row, family)
    return frozenset(caps)


def children_left_behind(
    ids: Iterable[str], family: Mapping[str, Any],
) -> dict[str, list[str]]:
    """``{project_id: [child ids not in ids]}`` for each project in ``ids`` whose
    subprojects are not all part of the same request (the no-orphan rule: the
    caller answers 409)."""
    wanted = set(ids)
    out: dict[str, list[str]] = {}
    for pid in dict.fromkeys(ids):
        missing = [c for c in _child_ids(family.get(pid), None) if c not in wanted]
        if missing:
            out[pid] = missing
    return out


def required_caps_for_status(
    row: Any, new_status: Any, family: "Mapping[str, Any] | None" = None,
) -> frozenset[str]:
    """``PATCH /projects/{id}/organization``. Moving into or out of ``archived`` is
    a delete of the project's class (archive and restore are symmetric); park,
    unpark and priority are plain writes. ``None`` means the body carries no status.
    A status that is not a string, or a current status that is unknown, takes the
    most restrictive reading."""
    if new_status is None:
        return frozenset({WRITE_PROJECT})
    current = _g(row, "status")
    if not isinstance(new_status, str) or not isinstance(current, str):
        crossing = True
    else:
        crossing = (current.strip().lower() == "archived") != (new_status.strip().lower() == "archived")
    return frozenset(_delete_caps(row, family) if crossing else {WRITE_PROJECT})


def required_caps_for_reparent(
    row: Any, cur_parent_row: Any, new_parent_row: Any,
) -> frozenset[str]:
    """Nest, move or detach (``POST /projects/{id}/parent``, MCP ``set_parent_project``).

    ``new_parent_row`` ``None`` is a detach, and an empty, absent or unresolvable
    parent name must be passed as ``None`` too. Re-parenting is a grant edge, so:

    * nest a top-level project: ``create_subproject`` and ``delete_project``
    * move a subproject to another parent: ``create_subproject`` and ``delete_subproject``
    * detach a subproject: ``delete_subproject`` and ``write_workspace_level``
    * an orphan: ``create_subproject``, ``delete_project`` and ``delete_subproject``
      (plus ``write_workspace_level`` to detach it)
    * the same parent, or detaching what is already top-level: a plain write
    """
    parent_id = _g(row, "parent_project_id") or ""
    if parent_id and _g(row, "parent_exists") is None:
        exists = cur_parent_row is not None
        cls = PROJECT_SUBPROJECT if exists else PROJECT_ORPHAN
    else:
        cls = classify_target(row)
    caps = {WRITE_PROJECT}
    if new_parent_row is None:  # detach
        if cls == PROJECT_TOP:
            return frozenset(caps)
        caps.update((DELETE_SUBPROJECT, WRITE_WORKSPACE_LEVEL))
        if cls == PROJECT_ORPHAN:
            caps.update((CREATE_SUBPROJECT, DELETE_PROJECT))
        return frozenset(caps)
    if parent_id and _g(new_parent_row, "id") == parent_id:
        return frozenset(caps)  # no-op
    caps.add(CREATE_SUBPROJECT)
    if cls in (PROJECT_TOP, PROJECT_ORPHAN):
        caps.add(DELETE_PROJECT)
    if cls in (PROJECT_SUBPROJECT, PROJECT_ORPHAN):
        caps.add(DELETE_SUBPROJECT)
    return frozenset(caps)


def required_caps_for_merge(
    source_row: Any, target_row: Any = None, family: "Mapping[str, Any] | None" = None,
) -> frozenset[str]:
    """MCP ``merge_project``. Merging empties the source, so the source's class
    decides (whatever ``archive_source`` says); the target only needs
    ``write_project``, which every result includes. A source that still has
    subprojects is refused by the caller with a 409 for scoped callers."""
    return frozenset(_delete_caps(source_row, family))


def required_caps_for_create(parent_project_id: Any) -> frozenset[str]:
    """Create: any non-``None`` parent (an empty string included) is a subproject
    attempt needing ``create_subproject``; an absent or ``None`` parent is a
    top-level project needing ``write_workspace_level``."""
    if parent_project_id is not None:
        return frozenset({WRITE_PROJECT, CREATE_SUBPROJECT})
    return frozenset({WRITE_PROJECT, WRITE_WORKSPACE_LEVEL})


def _hitl_payload(row: Any) -> dict[str, Any]:
    raw = _g(row, "payload")
    if isinstance(raw, Mapping):
        return dict(raw)
    if not raw or not isinstance(raw, (str, bytes, bytearray)):
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def check_hitl_answer(
    scoped: "Sequence[str] | None", db: Any, hitl_row: Any, answer: "str | None" = None,
) -> None:
    """Refuse a HITL answer a scoped caller must not apply, BEFORE it is persisted.

    Answering applies side effects: ``manual_issue_screening_toggle`` flips a
    workspace setting, ``hook_project_select`` writes the chosen project's
    executor config and ``proposal_github_issue`` files a GitHub issue through a
    project. A scoped member may file such a request against their own project
    with a payload naming anything, so the answer is checked here, for both
    transports. The request's own project must be in scope (the scope refusal
    comes first), then ``write_project``; then ``write_workspace_level`` for the
    toggle and an in-scope project for every project the payload would act on.
    ``answer=None`` is a dismissal, which has no side effect and needs only the
    first two.

    ``hitl_row`` may be the row or its id (read with ``db``). A missing row gets
    the scope refusal, the same answer as a foreign id. No-op for ``scoped=None``.
    """
    if scoped is None:
        return
    if isinstance(hitl_row, str):
        from . import db as db_module

        hitl_row = await db_module.get_hitl_request(db, hitl_row)
    project_id = _g(hitl_row, "project_id")
    if hitl_row is None or not project_id or project_id not in scoped:
        raise ProjectScopeDenied()
    require_capability(scoped, WRITE_PROJECT)
    if answer is None:
        return
    kind = _g(hitl_row, "kind")
    payload = _hitl_payload(hitl_row)
    if kind == "manual_issue_screening_toggle":
        require_capability(scoped, WRITE_WORKSPACE_LEVEL, target=str(project_id))
    elif kind == "hook_project_select":
        projects = payload.get("projects")
        chosen = next(
            (p for p in (projects if isinstance(projects, list) else [])
             if isinstance(p, Mapping) and p.get("name") == answer),
            None,
        )
        if chosen is not None:
            chosen_id = chosen.get("id")
            if not chosen_id or chosen_id not in scoped:
                raise ProjectScopeDenied()
    elif kind == "proposal_github_issue":
        target = payload.get("project_id")
        if target and target not in scoped:
            raise ProjectScopeDenied()
