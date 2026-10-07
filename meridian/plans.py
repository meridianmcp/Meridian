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

Operators set a plan with ``tenant_plan_admin.set_tenant_plan_by_email`` (the
``POST /admin/tenants/plan`` admin route and ``scripts/set_tenant_plan.py`` are
thin wrappers over it); it accepts ``OPERATOR_SETTABLE_PLANS`` only. ``admin``
is never set that way: it comes from the MERIDIAN_ADMIN_EMAILS allowlist.

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
an overage invoice: there is no Stripe customer behind it. Its monthly cost is
bounded by the Pro ceilings in ``hosted.PLAN_LIMITS``: the tenant gets the
warning at the Pro threshold and, past the compute grace allowance or the
storage ceiling, the tenant and the owner are emailed (once a month per
notice). Nothing is ever throttled, metered or refused for a playtester: the
quota of its own Neon project is the hard stop behind the warnings.

Neon reports consumption per pool project (which normally holds up to eight
tenants' databases), so a playtester is only ever on a project of its own: that
is what makes the usage figure its own, so the ceilings above are judged without
any tenant's figure depending on another's, and no other tenant is judged
any differently because a playtester exists. It is kept that way by
construction, not by special cases in the usage jobs:

* the plan is refused (``SharedPoolError``, raised by ``db.update_tenant`` and
  so by the admin action, with a message saying what to do) for a tenant whose
  database is in a project other tenants use; the operator moves the tenant
  first, e.g. ``POST /admin/tenants/{id}/reset-provisioning`` (which drops its
  database) and then grants the plan;
* a playtester provisioned from scratch gets a new Neon project of its own,
  registered as full (``hosted.provision_neon_db``);
* the pool allocator never places a tenant in a project that holds a
  playtester (``db.claim_pool_project_slot``), so one that was alone in its
  project when granted stays alone.

Granting it: ``set_tenant_plan_by_email`` on a tenant that has signed in at
least once. A tenant row created with this plan before the person's first
sign-in has no database, because sign-in only provisions one for a free-tier
tenant (a plan that maps to Pro is assumed to be provisioned at checkout, which
a playtester never goes through); ``POST /projects`` creates it on first use,
in its own project. A tenant that already has a database keeps it where it was
provisioned (a free, standard or pro project) whatever the plan becomes, which
is why the grant is refused unless the tenant is alone in that project. The
usage jobs poll a playtester's project with the key of the Neon account that
project belongs to, not the plan's, so the Pro ceilings are still measured
there; every other plan is polled with its own plan's key, as before. A
database drop (account deletion, reset-provisioning, churn, dunning) always
uses the pool's key, so a playtester's data is really removed. The project's
autoscaling ceiling, quota and retention are those of the pool tier it was
created in. Revoking the plan (``free``, ``standard``, ``pro``) is never
refused.

An optional end date can be kept in ``tenants.inactivity_expires_at`` (NULL =
no end date). Once it passes, ``tenant_entitlement_plan`` reports ``free`` for
that tenant, so every gate that resolves its plan through that helper lapses
together; no data is deleted. A value that cannot be parsed counts as expired
(fail closed). Only a plan in ``END_DATED_PLANS`` (free, trial, playtester) can
expire: a playtester who goes on to pay keeps the stale date in the column, and
it is ignored.

``is_internal`` is a separate flag (staff) and is not a plan: it keeps its own
meaning wherever it is checked. The boot-time backfills that set ``is_internal``
from MERIDIAN_INTERNAL_EMAILS and ``plan='admin'`` from MERIDIAN_ADMIN_EMAILS
skip a playtester, so an operator's grant is not undone at the next restart. The
admin action can clear ``is_internal`` when it grants the plan (the move from
the staff stopgap) but never sets it.

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

# Plans an operator may assign with tenant_plan_admin.set_tenant_plan_by_email.
# Not 'admin' (operator accounts come from the MERIDIAN_ADMIN_EMAILS allowlist)
# and not 'trial' (a legacy label nothing assigns any more).
OPERATOR_SETTABLE_PLANS: frozenset[str] = frozenset({FREE, STANDARD, PRO, PLAYTESTER})

# The label a person sees for each plan. Mirrors _PLAN_LABELS in
# static/dashboard-utils.ts (a test compares the two), so the badge in the
# dashboard and an operator's confirmation read the same.
PLAN_LABELS: dict[str, str] = {
    FREE: "Free Trial",
    TRIAL: "Trial",
    STANDARD: "Standard",
    PRO: "Pro",
    ADMIN: "Admin",
    PLAYTESTER: "Playtester",
}

# Plans whose tenants.inactivity_expires_at is an end date the product acts on:
# the free-tier trial window and a playtester's optional end date. Every other
# plan never expires, so a date left behind by a trial or a playtester period
# must not read as "expired" once the tenant is on a paying plan.
END_DATED_PLANS: frozenset[str] = frozenset({FREE, TRIAL, PLAYTESTER})


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


class SharedPoolError(ValueError):
    """An unbilled plan (playtester) was requested for a tenant on a shared Neon project.

    ``mates`` is how many other tenants have their database in ``pool`` (the
    tenant's ``neon_project_id``). The message is written for the operator who
    hit it, so the admin action can pass it on unchanged.
    """

    def __init__(self, tenant_id: str, pool: str, mates: int) -> None:
        self.tenant_id = tenant_id
        self.pool = pool
        self.mates = mates
        super().__init__(
            f"refused: this tenant's database is in the Neon pool project {pool!r}, which "
            f"{mates} other tenant(s) also use. The playtester plan is only granted on a "
            "project of its own, because Neon reports usage per project and a playtester's "
            "usage could not be told apart from its pool mates'. Move the tenant to a "
            f"dedicated project first: reset its provisioning (POST /admin/tenants/{tenant_id}"
            "/reset-provisioning with confirm=true, which DROPS its database and logs it out), "
            "then grant the plan again; the tenant's next database is created in a dedicated "
            "project."
        )


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


def plan_label(plan: Any) -> str:
    """Display label of ``plan``; an unknown value is shown as stored."""
    if isinstance(plan, str):
        return PLAN_LABELS.get(plan, plan)
    return ""


def is_playtester(plan: Any) -> bool:
    return plan == PLAYTESTER


def is_unbilled_plan(plan: Any) -> bool:
    """True for a plan that must never be charged, dunned or churned."""
    return isinstance(plan, str) and plan in UNBILLED_PLANS


def plan_has_end_date(plan: Any) -> bool:
    """True when ``tenants.inactivity_expires_at`` can expire a tenant on ``plan``."""
    return isinstance(plan, str) and plan in END_DATED_PLANS


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
