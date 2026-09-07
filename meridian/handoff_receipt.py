"""Durable, structural receipt for handoff/goal-token provenance verification
(1b7eb437, follow-up to 833649f1).

The gap this closes: ``verify_handoff_token`` / ``accept_handoff`` are the
canonical way a receiving session independently confirms a pasted ``/goal``
block's ``<goal_token>`` was minted by a real ``generate_handoff`` call
(AGENTS.md's "Handoff delivery & trust" section) -- but calling them is
entirely on the honor system today. Commit ``42309187`` ("harden(833649f1)")
already documented this precisely: *"claim_sprint_item has no structural
linkage to any verification receipt -- calling it after a failed
verification still succeeds unconditionally today. This is documentation/
banner hardening only, not a new server-side gate."* Nothing writes a durable
row proving a verification call actually happened, and nothing at
``claim_sprint_item`` time checks for one.

This module mirrors :mod:`meridian.code_intel_receipt`'s architecture
deliberately -- same shape, same opt-in contract, same
``action_audit_log``-reuse pattern -- with one **explicit, documented scope
reduction** (see "Deliberate scope reduction" below): unlike code-intel
prospecting, this pass ships the receipt-writing side AND the claim-time
check as **warn-only**. It never blocks a claim, regardless of the
declared ``availability_policy``.

**Structural, not self-report.** The receipt is written by the SERVER's own
tool-dispatch code (see the two call sites in ``meridian/mcp/handler.py``:
the ``verify_handoff_token`` and ``accept_handoff`` branches of
``_handle_mcp_request``'s ``tools/call`` handling -- the ONE place every
native tool call passes through) -- never by the calling agent declaring
"yes, I verified". A caller that never routes a verification call through
this connection (or whose verification genuinely failed) simply never gets a
receipt written for it.

**Opt-in via the project's capability manifest**, not a global switch: this
whole module is a no-op (``applicable=False``, zero behavior change, and --
see below -- zero extra I/O) unless the project has declared a capability
with id :data:`HANDOFF_PROVENANCE_CAPABILITY_ID`
(``"handoff_provenance_verification"``) via ``set_capability_manifest`` --
"old projects are not broken by this feature existing" (AGENTS.md's
capability-manifest contract, 649e095f).

**Deliberate scope reduction (read before extending this module):**

1. **Warn-only in this pass, for EVERY declared ``availability_policy``**
   (``required`` included). :func:`verify_handoff_provenance` never returns
   ``ok=False`` today. A fail-closed ``required`` path, plus an
   ``override_handoff_provenance_receipt`` override flag mirroring
   ``override_code_intel_receipt``, are explicitly **DEFERRED** -- see the
   item's own completion evidence / ``pin_decision`` for the rationale:
   unlike code-intel prospecting (which only had to add a *check* against an
   already-shipped, already-tested receipt-writing flow), this item had to
   build the write side from scratch too, and a hard fail-closed gate on the
   single most heavily-used claim path in the codebase is not something to
   ship untested in the same pass that first introduces the write path.
2. **A server-side receipt can prove "a genuine verify_handoff_token /
   accept_handoff call succeeded for this project" -- it cannot prove that
   call is the one that produced THIS claim.** Binding a receipt to the
   claiming session requires the CALLER to volunteer a ``session_id`` on the
   verification call (neither ``verify_handoff_token`` nor ``accept_handoff``
   accepted one before this item; both now accept it as an optional,
   attribution-only field -- see their schemas in ``mcp_tools.py``). A
   non-compliant client that never passes ``session_id`` to the verification
   call, or that never calls verification at all because it came in through
   the TRUSTED ``pending_goal``/``load_handoff`` channel (which correctly has
   nothing to verify), will simply never accrue an attributable receipt. This
   is a real, structural limit -- not a bug to "fix" by making the gate
   stricter, since doing so would punish exactly the honest executors using
   the trusted channel as intended. See :func:`verify_handoff_provenance`'s
   own docstring for how the fallback (no ``session_id`` supplied) is
   handled.
3. **No per-claim capability-manifest cost for a project that never opted
   in.** Unlike :mod:`meridian.code_intel_receipt` (which has a natural,
   cheap early-exit per item -- ``_item_declares_resources``/
   ``prospect_bypass`` -- before ever calling
   ``check_capability_availability``), ``claim_sprint_item`` has no
   equivalent per-item filter for handoff provenance: every claim is a
   candidate. ``check_capability_availability`` itself unconditionally
   builds a live-inventory snapshot (a real, potentially tunnel-network-bound
   call for a tenant with an active tunnel) even when the requested
   capability id isn't declared at all. To avoid adding that cost to the
   single hottest claim path in the codebase for the overwhelming majority
   of projects that never opt in, :func:`verify_handoff_provenance` reads
   the project's capability manifest itself FIRST (one cheap DB read,
   already `no-op-shaped` for a project with none) and returns immediately
   when the capability isn't declared, before ever touching
   ``check_capability_availability``/live-inventory building. A project that
   HAS opted in pays one extra (already-cheap) manifest read plus the same
   live-inventory cost every other capability check already pays.
4. **No tool-unavailability branch, unlike ``code_intel_receipt.py``'s
   ``CODE_INTEL_UNAVAILABLE``.** ``verify_handoff_token``/``accept_handoff``
   are both NATIVE (non-tunnel) tools, sourced from
   ``build_tool_manifest``'s always-in-process builtin-tools set — so
   ``evaluate_capability_availability`` will classify this capability's
   ``required_tools`` as ``available`` essentially always, unlike code-intel
   tools (frequently tunnel-forwarded/third-party, genuinely can be down).
   Copying that branch here would add real code with no realistic path to
   ever executing it. This module deliberately does NOT branch on the
   resolved tool-availability status at all (see
   :func:`verify_handoff_provenance`) — only on "was a matching receipt
   found" — rather than dead-code a check that would almost never fire.
"""
from __future__ import annotations

import json
from typing import Any

from . import db as db_module

#: event_type recorded in action_audit_log for a genuine handoff-provenance
#: verification receipt.
RECEIPT_EVENT_TYPE = "handoff_provenance_receipt"

#: Well-known capability id a project's manifest opts in with (see
#: set_capability_manifest / meridian.capability_manifest). Absent from a
#: project's manifest -> this whole module is a no-op for that project, with
#: zero extra I/O (see module docstring, point 3).
HANDOFF_PROVENANCE_CAPABILITY_ID = "handoff_provenance_verification"

#: Bare (unprefixed) MCP tool names whose SUCCESSFUL result this module will
#: record a receipt for. Both are native (non-tunnel) tools.
HANDOFF_PROVENANCE_RECEIPT_TOOLS = frozenset({"verify_handoff_token", "accept_handoff"})

#: Bounded lookback window when scanning for a matching receipt -- generous
#: enough to cover a session's real handoff-acceptance step without an
#: unbounded table scan.
_RECEIPT_LOOKBACK_LIMIT = 25


async def record_handoff_provenance_receipt(
    db: Any,
    *,
    tenant_id: "str | None",
    project_id: "str | None",
    session_id: "str | None",
    tool_name: str,
    outcome: str,
    reason: "str | None" = None,
) -> "dict[str, Any] | None":
    """Write ONE durable handoff-provenance receipt to ``action_audit_log``.

    Called ONLY on a genuinely successful result (``verify_handoff_token``'s
    ``valid=True``, or ``accept_handoff``'s ``accepted=True``) -- see the two
    call sites in ``meridian/mcp/handler.py``. Never called for a failed
    check: a failed check is already visible via the caller's own return
    value, and writing a receipt for it would misrepresent what happened as
    a success.

    ``session_id`` is the attribution key :func:`verify_handoff_provenance`
    matches against later (mirrors ``record_prospect_receipt``'s own
    ``actor=session_id`` convention exactly). A caller that omits it still
    gets a receipt written -- just one that can only ever satisfy the
    claim-time gate's project-wide fallback lookup (see
    :func:`verify_handoff_provenance`), never a session-attributed match.

    Best-effort and fully guarded, mirroring
    :func:`meridian.code_intel_receipt.record_prospect_receipt`'s contract
    EXACTLY: a receipt-write failure must NEVER break the underlying
    ``verify_handoff_token``/``accept_handoff`` call that already computed
    its real result. Returns the stored row, or ``None`` when nothing could
    be written (no ``project_id`` to attribute it to, or an unexpected DB
    error).
    """
    if not project_id:
        return None
    try:
        detail = json.dumps({
            "tool": tool_name,
            "outcome": outcome,
            "reason": reason,
        })
        return await db_module.record_action_audit_event(
            db, RECEIPT_EVENT_TYPE,
            tenant_id=tenant_id, project_id=project_id,
            actor=session_id or None, detail=detail,
        )
    except Exception:  # noqa: BLE001 -- logging must never break the caller's tool call
        return None


async def find_recent_handoff_provenance_receipt(
    db: Any,
    *,
    project_id: str,
    tenant_id: "str | None" = None,
    session_id: "str | None" = None,
) -> "dict[str, Any] | None":
    """Return a matching handoff-provenance receipt for *project_id*, or
    ``None``.

    When *session_id* is given, prefers a receipt whose own ``actor`` field
    (set at write time from the verifying call's ``session_id``) matches it
    exactly -- real evidence THIS session verified a handoff. When none of
    the recent receipts match that session (including when *session_id* is
    omitted entirely, e.g. a caller of ``claim_sprint_item`` that never
    passed one), falls back to the newest receipt for the project regardless
    of actor -- a weaker, project-wide "the provenance pathway has been used
    recently" signal, deliberately not treated as a mismatch (this module
    never blocks in this pass -- see module docstring -- so there is no risk
    in this fallback being lenient; it only affects an informational
    warning's wording, never a pass/fail outcome).
    """
    try:
        rows = await db_module.get_action_audit_log(
            db, project_id=project_id, tenant_id=tenant_id,
            event_type=RECEIPT_EVENT_TYPE, limit=_RECEIPT_LOOKBACK_LIMIT,
        )
    except Exception:  # noqa: BLE001 -- an unverifiable check must never wedge a claim
        return None
    if not rows:
        return None
    if session_id:
        for row in rows:
            if row.get("actor") == session_id:
                return row
    return rows[0]


async def verify_handoff_provenance(
    db: Any,
    tenant: "dict[str, Any] | None",
    project_id: str,
    *,
    session_id: "str | None" = None,
    live_inventory: "dict[str, Any] | None" = None,
) -> "dict[str, Any]":
    """The claim-time handoff-provenance receipt check.

    Never raises for an expected condition -- always returns a structured
    ``{"applicable", "ok", "code", "message", ...}`` dict, same contract
    style as :func:`meridian.code_intel_receipt.verify_code_intel_prospecting`
    / :func:`meridian.sprint_evidence_guard.verify_strict_completion_evidence`.
    Only a genuinely unexpected error degrades this check to "not
    applicable" (fail-open on infrastructure trouble -- a structural defect
    here must never block a claim).

    **WARN-ONLY IN THIS PASS** (see module docstring, "Deliberate scope
    reduction" #1): ``ok`` is always ``True``. ``degraded``/``warning`` are
    set instead of a hard block whenever the capability is declared but no
    matching receipt was found -- there is no fail-closed branch and no
    ``code`` value that means "blocked" yet.

    Returns keys:
      ``applicable`` -- ``False`` means "this gate does not apply" (the
        project's manifest never declared
        :data:`HANDOFF_PROVENANCE_CAPABILITY_ID`) -- zero behavior change
        (and, per module docstring point 3, zero live-inventory-build cost)
        from before this module existed.
      ``ok`` -- always ``True`` in this pass.
      ``degraded`` / ``warning`` -- set when the capability is declared but
        no matching receipt was found.
      ``capability`` -- the ``evaluate_capability_availability`` verdict, for
        callers that want to surface it.
      ``receipt`` -- the matched receipt row, when found.
    """
    base: "dict[str, Any]" = {
        "applicable": False, "ok": True, "code": None, "message": None,
        "capability": None, "receipt": None, "degraded": False, "warning": None,
    }
    if not project_id:
        return base

    try:
        # Module docstring point 3 — cheap early-exit BEFORE ever calling
        # check_capability_availability (which unconditionally builds a
        # live-inventory snapshot, a real tunnel-network-bound call for a
        # tenant with an active tunnel) so a project that never opted in
        # pays nothing beyond this one already-cheap manifest read.
        manifest = await db_module.get_project_capability_manifest(db, project_id)
        declared = [
            c for c in (manifest.get("capabilities") or [])
            if c.get("id") == HANDOFF_PROVENANCE_CAPABILITY_ID
        ]
        if not declared:
            return base

        from .mcp.handlers.project_tools import check_capability_availability  # noqa: PLC0415

        availability = await check_capability_availability(
            db, project_id, tenant,
            capability_id=HANDOFF_PROVENANCE_CAPABILITY_ID,
            live_inventory=live_inventory,
        )
    except Exception:  # noqa: BLE001 -- infra trouble must never block a claim
        return base
    if not availability:
        # Declared moments ago but the manifest changed out from under us
        # between the two reads (or evaluate_manifest_availability filtered
        # it for an unrelated reason) -- treat as not-applicable, same
        # "don't overclaim" posture as the rest of this module.
        return base

    cap_result = availability[0]
    policy = cap_result.get("availability_policy") or "required"

    receipt = await find_recent_handoff_provenance_receipt(
        db, project_id=project_id,
        tenant_id=(tenant or {}).get("id") if tenant else None,
        session_id=session_id,
    )
    if receipt is not None:
        return {**base, "applicable": True, "ok": True, "capability": cap_result, "receipt": receipt}

    return {
        **base, "applicable": True, "ok": True, "degraded": True,
        "capability": cap_result,
        "warning": (
            f"capability {HANDOFF_PROVENANCE_CAPABILITY_ID!r} is declared "
            f"(policy={policy!r}) but no verify_handoff_token/accept_handoff "
            "provenance receipt was found for this claim. This may mean the "
            "goal/handoff behind this claim was never independently "
            "verified (e.g. a pasted /goal block whose token was never "
            "checked), OR it may simply mean this claim came in through the "
            "TRUSTED pending_goal/load_handoff channel, which correctly has "
            "nothing to verify -- a non-compliant or trusted-channel client "
            "can never be forced to produce a receipt (see "
            "meridian.handoff_receipt's module docstring). Informational "
            "only in this pass: this never blocks the claim. If this claim "
            "did come from a pasted /goal, consider calling "
            "verify_handoff_token or accept_handoff (with session_id set to "
            "this claiming session) before claiming, so future claims by "
            "this session resolve a receipt here."
        ),
    }
