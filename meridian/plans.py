"""Tenant plan values: the one place that names them and says what each entitles.

``tenants.plan`` is a plain TEXT column (no CHECK constraint), so the set of
legal values lives here and nowhere else. Every code path that branches on a
plan value should reach it through the helpers below rather than comparing
strings, so a new plan is a change to this file plus the limit tables it points
at, not a hunt through the codebase.

What each plan entitles
-----------------------

====================  =====================================================
plan                  entitlements
====================  =====================================================
``free``              Trial tier. Single project, no tunnel, Bearer-token
                      request budget of the lowest tier, free pool tier,
                      PLAN_LIMITS["free"]. 30-day trial clock with reminder
                      emails.
``trial``             Legacy label of the trial tier. Same request and
                      compute budgets as ``free`` (it has no rows of its
                      own in the limit tables; the one-project cap applies
                      to ``free`` only); also receives trial reminders.
``standard``          Paid. No tunnel, mid request budget, standard pool
                      tier, PLAN_LIMITS["standard"]. Billed through Stripe.
``pro``               Paid. Tunnel, unlimited request budget, pro pool tier,
                      PLAN_LIMITS["pro"]. Billed through Stripe.
``admin``             Operator account. Tunnel, unlimited request budget,
                      manually assigned database (never auto-provisioned),
                      unlimited compute and storage.
``playtester``        Pro entitlements, no Stripe relationship. See below.
====================  =====================================================

The limit tables themselves stay next to the code that enforces them:
``hosted.PLAN_LIMITS`` (compute/storage), ``_deps._TENANT_RL_PER_MINUTE``
(Bearer-token requests per minute), ``server._WORKSPACE_MEMBER_LIMITS``
(team size), and the pool tiers in ``hosted.provision_neon_db``. They are keyed
by the *canonical* plans only; ``effective_entitlement_plan`` maps an alias onto
its canonical plan before any lookup.

``playtester``
--------------
A person invited to exercise the hosted product. Entitlements are exactly
those of ``pro`` (one alias, ``ENTITLEMENT_ALIASES``), but the account is never
charged, never dunned, never churned, never trial-expired and never receives
an overage invoice: there is no Stripe customer behind it. Usage is still
bounded by the Pro ceilings in ``hosted.PLAN_LIMITS``. Past the compute grace
allowance the daily overage job throttles Neon compute (the same path an
unbilled tenant already takes) instead of metering a charge, and the owner is
alerted by email; storage past the ceiling is logged and emailed (the existing
storage path has no write-refusal step).

An optional end date can be kept in ``tenants.inactivity_expires_at`` (NULL =
no end date). Once it passes, ``tenant_entitlement_plan`` reports ``free`` for
that tenant, so every gate that resolves its plan through that helper lapses
together; no data is deleted. A value that cannot be parsed counts as expired
(fail closed).

``is_internal`` is a separate flag (staff) and is not a plan: it keeps its own
meaning wherever it is checked.

Unknown plan values are never aliased and never accepted on write: every
lookup falls through to the lowest tier and ``validate_plan`` rejects them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

FREE = "free"
TRIAL = "trial"
STANDARD = "standard"
PRO = "pro"
ADMIN = "admin"
PLAYTESTER = "playtester"

# Every legal value of tenants.plan, with a one-line statement of what it is.
PLAN_CATALOG: dict[str, str] = {
    FREE: "trial tier, lowest budgets",
    TRIAL: "legacy label of the trial tier",
    STANDARD: "paid, Stripe-billed",
    PRO: "paid, Stripe-billed, tunnel and unlimited request budget",
    ADMIN: "operator account, unlimited, manually assigned database",
    PLAYTESTER: "pro entitlements, never billed, usage still bounded",
}

KNOWN_PLANS: frozenset[str] = frozenset(PLAN_CATALOG)

# The only plans a customer can reach by paying (Stripe checkout and its
# webhook). 'playtester' is deliberately absent: it is granted by an operator,
# never purchased.
PURCHASABLE_PLANS: frozenset[str] = frozenset({STANDARD, PRO})

# Plans with no Stripe relationship: never charged, dunned, churned or
# trial-expired, and never sent an overage invoice.
UNBILLED_PLANS: frozenset[str] = frozenset({PLAYTESTER})

# alias -> the canonical plan whose entitlements it shares.
ENTITLEMENT_ALIASES: dict[str, str] = {PLAYTESTER: PRO}


def effective_entitlement_plan(plan: Any) -> Any:
    """Map ``plan`` onto the canonical plan whose entitlements it shares.

    Exact match only: ``'playtester'`` becomes ``'pro'``; every other value,
    including unknown ones, ``None`` and differently-cased spellings, is
    returned unchanged so it keeps hitting the same fail-closed fallbacks it
    hits today. Callers apply their own default for a missing plan.
    """
    if isinstance(plan, str):
        return ENTITLEMENT_ALIASES.get(plan, plan)
    return plan


def is_known_plan(plan: Any) -> bool:
    return isinstance(plan, str) and plan in KNOWN_PLANS


def validate_plan(plan: Any) -> str:
    """Return ``plan`` if it is a legal ``tenants.plan`` value, else raise.

    Used at the single write choke point (``db.update_tenant``) so a typo
    cannot silently park a tenant on a plan no gate recognises.
    """
    if not is_known_plan(plan):
        raise ValueError(
            f"unknown plan {plan!r}; expected one of {sorted(KNOWN_PLANS)}"
        )
    return plan


def is_playtester(plan: Any) -> bool:
    return plan == PLAYTESTER


def is_unbilled_plan(plan: Any) -> bool:
    """True for a plan that must never be charged, dunned or churned."""
    return isinstance(plan, str) and plan in UNBILLED_PLANS


def _parse_end_date(raw: Any) -> "datetime | None":
    """Parse ``tenants.inactivity_expires_at`` (``YYYY-MM-DD HH:MM:SS`` or
    ISO-8601, naive = UTC). Returns None when it cannot be parsed."""
    s = str(raw).strip()
    for parser in (
        lambda v: datetime.strptime(v[:19], "%Y-%m-%d %H:%M:%S"),
        lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")),
    ):
        try:
            dt = parser(s)
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def playtester_access_expired(
    tenant: Mapping[str, Any], now: "datetime | None" = None
) -> bool:
    """True when a playtester's optional end date has passed.

    NULL/empty means no end date. An unparseable value counts as expired.
    Always False for a tenant that is not a playtester.
    """
    if tenant.get("plan") != PLAYTESTER:
        return False
    raw = tenant.get("inactivity_expires_at")
    if raw is None or str(raw).strip() == "":
        return False
    end = _parse_end_date(raw)
    if end is None:
        return True
    return end <= (now or datetime.now(timezone.utc))


def tenant_entitlement_plan(
    tenant: Mapping[str, Any],
    *,
    default: str = FREE,
    now: "datetime | None" = None,
) -> Any:
    """The plan whose entitlements apply to ``tenant`` right now.

    ``effective_entitlement_plan`` of the tenant's plan, except that a
    playtester past its optional end date is reported as ``free``. This is the
    function every per-tenant gate calls; use ``effective_entitlement_plan``
    directly only where no tenant row is at hand or where the plan decides
    *where something already lives* (the Neon API key of an existing database
    follows the plan it was provisioned under, not the lapsed one).
    """
    plan = tenant.get("plan") or default
    if plan == PLAYTESTER and playtester_access_expired(tenant, now):
        return FREE
    return effective_entitlement_plan(plan)
