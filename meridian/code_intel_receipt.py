"""Durable, structural receipt for code-intel prospecting (a8c0f3b7).

The gap this closes: ``meridian/mcp/handler.py::_prospect_code_context`` (the
``code_context``/``hint`` field on a freshly-claimed sprint item) and
``routes/tunnel.py``'s ``_CODE_INTEL_FIRST_GUIDANCE`` are both PROSE nudges --
"prospect before editing", "call search_graph first". A live executor
transcript still used broad Read/grep/``git show``/PowerShell ``Get-Content``,
or spawned a sub-agent that never touched code-intel at all, and there was no
way to tell after the fact whether real semantic prospecting happened before
the edit. The existing ``.claude/hooks/code_intel_guard.sh`` PreToolUse hook
(see ``tests/test_code_intel_guard.py``) blocks raw Grep/Glob at the CLI layer
for ONE surface (Claude Code), but a Read tool call, a shell ``git show`` /
``Get-Content``, or a sub-agent that calls Serena/codebase-memory-mcp through
a connection Meridian never sees are all structurally invisible to it.

This module does NOT try to block those paths -- that would overclaim (see
the module docstring warning below). Instead it builds an audit RECEIPT: a
durable, machine-checkable row proving "a genuine code-intel prospecting call
was made for this project" -- and a verification gate
(:func:`verify_code_intel_prospecting`) that ``complete_sprint_item`` consults
before marking an item done. Reuses the existing, already-migrated
``action_audit_log`` table (5dfe34b2/cd495afa -- same append-only audit
pattern as ``sprint_evidence_guard``'s strict-evidence overrides and the
manual-issue-screening toggle log) rather than inventing a new table: no new
migration, no SQLite/Postgres parity work needed.

**Harden, do not overclaim.** This is explicitly NOT a hard requirement for
every project or every sprint item:

* A sprint item that never declared ``touches_resources`` was never a real
  prospecting candidate in the first place (mirrors ``claim_sprint_item``'s
  own UNPROSPECTED gate scope guard, :func:`meridian.db.sprint_items.
  _item_declares_resources`) -- not gated here either.
* A human-set ``prospect_bypass`` on the item is honoured, same as the claim
  gate.
* **Opt-in via the project's capability manifest**, not a global switch: the
  gate is a no-op (``applicable=False``, zero behavior change) unless the
  project has declared a capability with id
  :data:`CODE_INTEL_CAPABILITY_ID` (``"code_intel_prospecting"``) via
  ``set_capability_manifest`` -- "old projects are not broken by this feature
  existing" (AGENTS.md's capability-manifest contract, 649e095f).
* When that capability IS declared, its ``availability_policy`` (``required``
  / ``optional`` / ``degraded_ok``) governs what happens when code-intel
  itself is unavailable (fail closed only for ``required``) or when it WAS
  available but no receipt was recorded (fail closed for ``required``, warn
  and degrade for ``optional``/``degraded_ok``) -- reusing
  :mod:`meridian.capability_availability` / :func:`meridian.mcp.handlers.
  project_tools.check_capability_availability` rather than re-implementing
  the required/optional/degraded_ok posture from scratch.

**Structural, not self-report.** The receipt is written by the SERVER's own
tool-dispatch code (see the two call sites in ``meridian/mcp/handler.py``:
the native ``prospect_symbol`` branch of ``_handle_code_index_tools``, and
the tunnel-forward chokepoint inside ``_handle_mcp_request``'s
``tools/call`` handling -- the ONE place every tool call over a Meridian MCP
connection passes through, tunneled or native) -- never by the calling agent
declaring "yes, I searched". A bare Read/grep/``git show``/``Get-Content``
call, or a sub-agent that never routes a code-intel call through this
connection, simply never reaches either receipt-writing call site, so no row
is ever written for that work -- :func:`verify_code_intel_prospecting` then
correctly reports no receipt, exactly the "cannot silently evade" property
the item asks for.

**Known, documented limitation** (do not overclaim this either): a
tunnel-forwarded code-intel tool call (``codebase__search_graph``,
``extractor__find_symbol``, ...) is a THIRD-PARTY tool schema that does not
carry Meridian's own project UUID, so :func:`resolve_receipt_project_id`
falls back to the self-hosted default-project convention
(``toml_config.get_default_project_id()`` / ``MERIDIAN_PROJECT_ID`` --
AGENTS.md's own "Auto-scoping to a single project" feature) to attribute the
receipt. A hosted, multi-project tenant with no default project configured
and a caller that never passes a UUID-shaped ``project_id`` on the call gets
no receipt attribution at all for THAT call (``resolve_receipt_project_id``
returns ``None`` and the write is skipped) -- a real, acknowledged gap, not a
silent false-positive: the completion-time gate then correctly reports "no
receipt found" rather than fabricating one.
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import db as db_module

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

#: event_type recorded in action_audit_log for a genuine prospecting receipt.
RECEIPT_EVENT_TYPE = "code_intel_prospect_receipt"

#: event_type recorded in action_audit_log for an audited override of a
#: blocked (missing-receipt / unavailable) completion.
OVERRIDE_EVENT_TYPE = "code_intel_receipt_override"

#: Well-known capability id a project's manifest opts in with (see
#: set_capability_manifest / meridian.capability_manifest). Absent from a
#: project's manifest -> this whole module is a no-op for that project.
CODE_INTEL_CAPABILITY_ID = "code_intel_prospecting"

#: Bare (unprefixed) tool names that count as a genuine code-intel
#: prospecting call. ``prospect_symbol`` is the promoted single entry point
#: (agent_defaults.py v12 -- "call prospect_symbol FIRST"); the rest mirror
#: the codebase-memory-mcp / Serena tools code_intel_guard.sh's stderr
#: already names as the correct alternative to grep/glob, plus the local
#: BM25 fallback (search_code_semantic).
CODE_INTEL_RECEIPT_TOOLS = frozenset({
    "prospect_symbol",
    "search_graph", "query_graph", "trace_path", "get_architecture",
    "search_code", "get_code_snippet",
    "find_symbol", "find_declaration", "find_implementations",
    "find_referencing_symbols", "get_symbols_overview",
    "search_code_semantic",
})

# Keys, in priority order, that plausibly carry the symbol/query text across
# the different (Meridian-native + third-party) code-intel tool schemas.
_QUERY_HINT_KEYS = ("symbol", "query", "name_path", "symbol_name", "name")


def bare_tool_name(name: str) -> str:
    """Strip a tunnel slot prefix (``codebase__search_graph`` -> ``search_graph``)."""
    return name.split("__", 1)[1] if isinstance(name, str) and "__" in name else (name or "")


def is_code_intel_receipt_tool(name: str) -> bool:
    """True when *name* (prefixed or bare) is a recognized prospecting call."""
    return bare_tool_name(name) in CODE_INTEL_RECEIPT_TOOLS


def extract_query_hint(args: "dict[str, Any] | None") -> str:
    """Best-effort short text describing what a prospecting call searched for."""
    if not isinstance(args, dict):
        return ""
    for key in _QUERY_HINT_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:200]
    return ""


def resolve_receipt_project_id(args: "dict[str, Any] | None") -> "str | None":
    """Best-effort resolution of the MERIDIAN project id a receipt belongs to.

    Code-intel graph tools identify a project by a LOCAL REPO-PATH SLUG (see
    ``routes/tunnel.py``'s ``_CODE_INTEL_PROJECT_TOOLS`` error enrichment) --
    a different identifier from Meridian's own project UUID, so a tool call's
    own ``project_id``/``project`` argument cannot be trusted as Meridian's
    id. Prefers the self-hosted default-project convention
    (``toml_config.get_default_project_id()`` -- the same resolution
    ``start_session`` already falls back to, AGENTS.md's "Auto-scoping to a
    single project"), since that genuinely IS Meridian's id; falls back to a
    UUID-shaped ``project_id`` passed directly on the call (covers a caller
    that happens to pass the real Meridian id) only when no default is
    configured. Returns ``None`` when neither resolves -- callers must treat
    that as "cannot attribute this receipt", not silently guess.
    """
    from . import toml_config as _toml_config  # noqa: PLC0415

    default_pid = _toml_config.get_default_project_id()
    if default_pid:
        return default_pid
    if isinstance(args, dict):
        pid = str(args.get("project_id") or "").strip()
        if _UUID_RE.match(pid):
            return pid
    return None


async def record_prospect_receipt(
    db: Any,
    *,
    tenant_id: "str | None",
    project_id: "str | None",
    session_id: "str | None",
    tool_name: str,
    query: "str | None" = None,
) -> "dict[str, Any] | None":
    """Write ONE durable prospecting receipt to ``action_audit_log``.

    Best-effort and fully guarded: a receipt-write failure must NEVER break
    the underlying tool call that already succeeded. Returns the stored row,
    or ``None`` when nothing could be written (no ``project_id`` to attribute
    it to, or an unexpected DB error).
    """
    if not project_id:
        return None
    try:
        detail = json.dumps({"tool": tool_name, "query": (query or "")[:200]})
        return await db_module.record_action_audit_event(
            db, RECEIPT_EVENT_TYPE,
            tenant_id=tenant_id, project_id=project_id,
            actor=session_id or None, detail=detail,
        )
    except Exception:  # noqa: BLE001 -- logging must never break the caller's tool call
        return None


async def find_recent_prospect_receipt(
    db: Any,
    *,
    project_id: str,
    tenant_id: "str | None" = None,
    since: "str | None" = None,
) -> "dict[str, Any] | None":
    """Return the newest prospecting receipt for *project_id*, or ``None``.

    ``since`` (inclusive lower bound on ``created_at``, same TEXT-comparable
    ``YYYY-MM-DD HH:MM:SS`` form the rest of this codebase's timestamps use)
    scopes the search to receipts recorded no earlier than the item's own
    ``claimed_at`` -- a receipt from a stale, earlier pass at the item does
    not count as evidence for the CURRENT claim, mirroring
    ``sprint_evidence_guard``'s ``EVIDENCE_STALE`` freshness check.
    """
    try:
        rows = await db_module.get_action_audit_log(
            db, project_id=project_id, tenant_id=tenant_id,
            event_type=RECEIPT_EVENT_TYPE, since=since, limit=1,
        )
    except Exception:  # noqa: BLE001 -- an unverifiable check must never wedge completion
        return None
    return rows[0] if rows else None


def _claimed_at_since(item: "dict[str, Any]") -> "str | None":
    """``item['claimed_at']`` normalized to the DB's comparable timestamp form."""
    try:
        from .db.sprint_items import _parse_deferral_ts  # noqa: PLC0415

        dt = _parse_deferral_ts(item.get("claimed_at"))
    except Exception:  # noqa: BLE001
        dt = None
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt is not None else None


async def verify_code_intel_prospecting(
    db: Any,
    tenant: "dict[str, Any] | None",
    project_id: str,
    item: "dict[str, Any]",
    *,
    session_id: "str | None" = None,
    live_inventory: "dict[str, Any] | None" = None,
) -> "dict[str, Any]":
    """The completion-time prospecting-receipt gate.

    Never raises for an expected condition -- every rejection comes back as
    a structured ``{"applicable", "ok", "code", "message", ...}`` dict, same
    contract style as ``sprint_evidence_guard.verify_strict_completion_
    evidence``. Only a genuinely unexpected error inside the availability
    lookup degrades that check to "not applicable" (fail-open on
    infrastructure trouble -- a structural defect here must never
    permanently wedge the board).

    Returns keys:
      ``applicable`` -- False means "this gate does not apply" (no declared
        touches_resources, ``prospect_bypass`` set, or the project's manifest
        never declared the :data:`CODE_INTEL_CAPABILITY_ID` capability) --
        zero behavior change from before this module existed.
      ``ok`` -- False means BLOCKED (fail-closed, ``required`` policy only).
      ``code`` -- ``CODE_INTEL_UNAVAILABLE`` | ``CODE_INTEL_RECEIPT_MISSING``
        | ``None``.
      ``degraded`` / ``warning`` -- set when proceeding on a documented
        degrade (``optional``/``degraded_ok`` policy) rather than a hard
        block.
      ``capability`` -- the ``evaluate_capability_availability`` verdict, for
        callers that want to surface it.
      ``receipt`` -- the matched receipt row, when found.
    """
    base: "dict[str, Any]" = {
        "applicable": False, "ok": True, "code": None, "message": None,
        "capability": None, "receipt": None, "degraded": False, "warning": None,
    }
    if not isinstance(item, dict):
        return base
    if not db_module._item_declares_resources(item) or bool(item.get("prospect_bypass")):
        return base

    try:
        from .mcp.handlers.project_tools import check_capability_availability  # noqa: PLC0415
        from . import capability_availability as _capability_availability  # noqa: PLC0415

        availability = await check_capability_availability(
            db, project_id, tenant,
            capability_id=CODE_INTEL_CAPABILITY_ID,
            live_inventory=live_inventory,
        )
    except Exception:  # noqa: BLE001 -- infra trouble must never wedge completion
        return base
    if not availability:
        # Project never opted in: no capability manifest entry declared.
        return base

    cap_result = availability[0]
    policy = cap_result.get("availability_policy") or "required"
    status = cap_result.get("status")
    unresolved = status in (
        _capability_availability.STATUS_MISSING, _capability_availability.STATUS_UNKNOWN,
    )

    if unresolved:
        if policy == "required":
            return {
                **base, "applicable": True, "ok": False,
                "code": "CODE_INTEL_UNAVAILABLE", "capability": cap_result,
                "message": (
                    "capability 'code_intel_prospecting' is declared REQUIRED for "
                    "this project but no required tool (and no working fallback) "
                    "is available right now -- failing closed rather than "
                    "silently skipping the prospecting-receipt requirement."
                ),
            }
        return {
            **base, "applicable": True, "ok": True, "degraded": True,
            "capability": cap_result,
            "warning": (
                f"code-intel is unavailable (policy={policy}) -- proceeding "
                "without a prospecting receipt; documented degrade, not a "
                "silent bypass."
            ),
        }

    # Code-intel IS usable (available, or degraded via a working fallback) --
    # the executor genuinely had the means to prospect. A durable receipt is
    # required to prove it actually happened for the CURRENT claim.
    receipt = await find_recent_prospect_receipt(
        db, project_id=project_id, tenant_id=(tenant or {}).get("id") if tenant else None,
        since=_claimed_at_since(item),
    )
    if receipt is not None:
        return {**base, "applicable": True, "ok": True, "capability": cap_result, "receipt": receipt}

    if policy == "required":
        return {
            **base, "applicable": True, "ok": False,
            "code": "CODE_INTEL_RECEIPT_MISSING", "capability": cap_result,
            "message": (
                "code-intel is available for this project but no durable "
                "search_graph/find_symbol/prospect_symbol receipt was recorded "
                "since this item was claimed. Run a code-intel prospecting call "
                "(prospect_symbol, search_graph, find_symbol, ...) before "
                "completing, or pass override_code_intel_receipt=true with a "
                "non-empty override_reason to explicitly acknowledge and "
                "complete anyway (audited)."
            ),
        }
    return {
        **base, "applicable": True, "ok": True, "degraded": True,
        "capability": cap_result,
        "warning": (
            f"code-intel was available but no prospecting receipt was found "
            f"(policy={policy}) -- proceeding, skip noted."
        ),
    }


async def record_prospect_receipt_override(
    db: Any,
    project_id: str,
    item_id: str,
    *,
    actor: "str | None",
    reason: "str | None",
    check: "dict[str, Any]",
    tenant_id: "str | None" = None,
) -> "dict[str, Any]":
    """Audit-log an explicit override of a blocked prospecting-receipt gate.

    ``reason`` is REQUIRED and non-empty -- mirrors ``sprint_evidence_guard.
    record_strict_evidence_override`` exactly: an override with no stated
    reason is refused outright (``ValueError``), never silently accepted.
    """
    _reason = (reason or "").strip()
    if not _reason:
        raise ValueError(
            "override_reason is required and must be non-empty to override a "
            "blocked code-intel prospecting-receipt gate -- an override with "
            "no stated reason is not auditable and is refused."
        )
    detail = json.dumps({
        "item_id": item_id,
        "reason": _reason,
        "code": check.get("code"),
        "capability": check.get("capability"),
    })
    return await db_module.record_action_audit_event(
        db, OVERRIDE_EVENT_TYPE,
        tenant_id=tenant_id, project_id=project_id,
        actor=actor, detail=detail,
    )
