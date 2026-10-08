"""Operator action: set a tenant's plan by account email (grant or revoke ``playtester``).

One implementation behind the ``POST /admin/tenants/plan`` admin route and
``scripts/set_tenant_plan.py``, so the route and the command line cannot
disagree. What each plan entitles is documented in ``plans.py``.

The action describes a desired state and is idempotent: repeating a call that
already holds changes nothing and writes no audit row. Without ``apply`` it only
reports what it would do (the admin route calls that "unconfirmed"), so the
owner sees the effect, including any warning, before anything is written. Every
change is audited in ``action_audit_log`` (event ``tenant_plan_changed``).

Guard rails, each a refusal rather than a silent fix:

* only ``plans.OPERATOR_SETTABLE_PLANS`` can be set; the ``admin`` plan never
  comes from here and an ``admin`` tenant is never changed here;
* ``playtester`` is refused for a tenant with a Stripe customer, because the plan
  means "never charged" and a live subscription would keep charging;
* ``playtester`` is refused for a tenant whose database is in a Neon pool project
  other tenants use (``plans.SharedPoolError``, answered 409 with what to do
  first): Neon reports usage per project, so a playtester is only ever on a
  project of its own. A tenant with no database yet, or alone in its project,
  passes; revoking is never refused. The preview reports the refusal too;
* an end date is only accepted for ``playtester`` and must lie in the future (a
  past one would be a revocation in disguise).

Two details that are easy to get wrong by hand: granting ``playtester`` never
inherits an old free-trial date as the end date (``inactivity_expires_at`` is
reset unless one is given), and moving a tenant off ``playtester`` clears its end
date so it is not read as a trial expiry. The plan is written before the staff
flag is cleared, so an interrupted run leaves the tenant exempt from the limit
jobs (the old state) and never a plan-less, unbilled Pro tenant that the churn
job would collect.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from . import db as db_module
from .plans import (
    OPERATOR_SETTABLE_PLANS,
    PLAYTESTER,
    SharedPoolError,
    plan_label,
)

AUDIT_EVENT = "tenant_plan_changed"


class _Unset:
    """Marker for an argument the caller did not give (None means 'clear')."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNSET"


UNSET: Any = _Unset()


class PlanChangeError(Exception):
    """A refused request; ``status`` is the HTTP status the admin route answers with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def normalise_end_date(raw: Any, *, now: "datetime | None" = None) -> str:
    """``raw`` as the column's ``YYYY-MM-DD HH:MM:SS`` (naive UTC); it must lie in the future."""
    s = str(raw).strip() if raw is not None else ""
    parsed = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise PlanChangeError(400, "expires_at must be YYYY-MM-DD or YYYY-MM-DD HH:MM:SS (UTC)")
    if parsed.replace(tzinfo=timezone.utc) <= (now or datetime.now(timezone.utc)):
        raise PlanChangeError(
            400,
            "expires_at is in the past: that would end the access at once; "
            "revoke the plan instead (plan=free)",
        )
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _row(tenant: Any) -> dict[str, Any]:
    return tenant if isinstance(tenant, dict) else {k: tenant[k] for k in tenant.keys()}


async def set_tenant_plan_by_email(
    db: Any,
    email: str,
    plan: str,
    *,
    expires_at: Any = UNSET,
    clear_internal: bool = False,
    apply: bool = False,
    actor: str = "",
    via: str = "admin",
) -> dict[str, Any]:
    """Set ``email``'s plan to ``plan``; see the module docstring.

    ``expires_at``: ``UNSET`` keeps a playtester's current end date (a tenant that
    is not a playtester yet gets none), ``None`` clears it, a date sets it.
    ``clear_internal`` also clears the staff flag (it can never be set here).
    Raises ``PlanChangeError``.
    """
    address = (email or "").strip().lower() if isinstance(email, str) else ""
    if "@" not in address:
        raise PlanChangeError(400, "email is required")
    if not isinstance(plan, str) or plan not in OPERATOR_SETTABLE_PLANS:
        raise PlanChangeError(
            400,
            f"plan must be one of {sorted(OPERATOR_SETTABLE_PLANS)} "
            "(the admin plan comes from MERIDIAN_ADMIN_EMAILS, not from here)",
        )
    given_end = expires_at is not UNSET and expires_at is not None
    if given_end and plan != PLAYTESTER:
        raise PlanChangeError(400, "only a playtester has an end date")
    new_end = normalise_end_date(expires_at) if given_end else None

    async with db.execute(
        "SELECT * FROM tenants WHERE LOWER(email) = ?", (address,)
    ) as cur:
        found = await cur.fetchall()
    if not found:
        raise PlanChangeError(404, f"no tenant with email {address!r}")
    if len(found) > 1:
        raise PlanChangeError(409, f"{len(found)} tenants share the email {address!r}")
    current = _row(found[0])
    before = current.get("plan")

    if before == "admin":
        raise PlanChangeError(409, "this is an operator (admin) account; its plan is not changed here")
    if plan == PLAYTESTER and current.get("stripe_customer_id"):
        raise PlanChangeError(
            409,
            "this tenant has a Stripe customer: cancel the subscription first, "
            "a playtester is never charged",
        )
    if plan == PLAYTESTER:
        # Checked for a preview and for an unchanged plan too, so the operator
        # hears about a shared project before anything is written.
        try:
            await db_module.require_dedicated_pool(db, current["id"], current.get("neon_project_id"))
        except SharedPoolError as exc:
            raise PlanChangeError(409, str(exc)) from exc

    stored_end = current.get("inactivity_expires_at") or None
    internal_before = bool(current.get("is_internal"))

    # The end date the tenant has afterwards.
    if plan == PLAYTESTER:
        if expires_at is UNSET:
            # Keep a playtester's own end date, but never inherit a trial date.
            end_after = stored_end if before == PLAYTESTER else None
        else:
            end_after = new_end
    elif before == PLAYTESTER:
        end_after = None  # it was the playtester's end date, not a trial clock
    else:
        end_after = stored_end  # another plan's own clock: untouched

    updates: dict[str, Any] = {}
    if plan != before:
        updates["plan"] = plan
    if end_after != stored_end:
        updates["inactivity_expires_at"] = end_after
    internal_after = internal_before and not clear_internal
    changed = bool(updates) or internal_after != internal_before

    warnings: list[str] = []
    if plan == PLAYTESTER:
        if internal_after:
            warnings.append(
                "the tenant is still is_internal (staff): every usage and lifecycle job skips "
                "it, so no ceiling applies; clear it with is_internal=false"
            )
        if not current.get("neon_project_id"):
            warnings.append(
                "the tenant has no database yet; it is created in a Neon project of its own "
                "when the first project is created"
            )

    result: dict[str, Any] = {
        "tenant_id": current["id"],
        "email": address,
        "plan_before": before,
        "plan": plan,
        "plan_label": plan_label(plan),
        "expires_at_before": stored_end,
        "expires_at": end_after,
        "is_internal_before": internal_before,
        "is_internal": internal_after,
        "changed": changed,
        "applied": False,
        "warnings": warnings,
    }
    if not apply:
        return result

    if updates:
        try:
            await db_module.update_tenant(db, current["id"], **updates)
        except SharedPoolError as exc:  # a pool mate arrived since the check above
            raise PlanChangeError(409, str(exc)) from exc
    if internal_after != internal_before:
        await db.execute("UPDATE tenants SET is_internal = 0 WHERE id = ?", (current["id"],))
        await db.commit()
    if changed:
        await db_module.record_action_audit_event(
            db,
            AUDIT_EVENT,
            tenant_id=current["id"],
            actor=actor or None,
            detail=json.dumps(
                {
                    "email": address,
                    "plan": [before, plan],
                    "expires_at": [stored_end, end_after],
                    "is_internal": [internal_before, internal_after],
                    "via": via,
                }
            ),
        )
    result["applied"] = True
    return result
