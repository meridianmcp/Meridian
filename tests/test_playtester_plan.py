"""Playtester plan (board item 08564061).

A playtester has Pro entitlements, no Stripe relationship, and usage that is
still bounded. This file pins:

* the helper layer in ``meridian/plans.py`` (alias, validation, optional end
  date, unknown values fail closed, every plan documented in one place);
* that every plan-dependent decision gives a playtester the same answer as a
  Pro tenant (one parametrized table of probes, plus end-to-end checks through
  the real routes and jobs where a probe would just restate the expression);
* that a playtester is never billed, dunned, churned, trial-expired or sent an
  overage invoice, while its usage ceilings still warn and alert (nothing is
  ever throttled for it);
* that a playtester is only ever on a Neon project of its own (the grant is
  refused on a shared one, provisioning creates a project for it, the pool
  allocator never places anyone beside it) and that the usage jobs therefore
  judge every other tenant exactly as before, shared pool or not;
* that a database drop asks the Neon account that owns the pool;
* the operator action that grants and revokes the plan, and that no boot-time
  backfill undoes it;
* that ``is_internal`` (staff) keeps exactly the meaning it had.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest

from dashboard_src import dashboard_source
from meridian import db as db_module
from meridian import plans

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
PAST = "2000-01-01 00:00:00"
FUTURE = "2099-01-01 00:00:00"


def _t(plan: Any, **extra: Any) -> dict[str, Any]:
    return {"id": "t-1", "email": "t@example.com", "plan": plan, **extra}


# ---------------------------------------------------------------------------
# plans.py
# ---------------------------------------------------------------------------


def test_alias_maps_playtester_to_pro_and_nothing_else():
    assert plans.effective_entitlement_plan("playtester") == "pro"
    for p in ("free", "trial", "standard", "pro", "admin"):
        assert plans.effective_entitlement_plan(p) == p
    # Exact match only: unknown, differently-cased and empty values are returned
    # untouched so they keep hitting today's fail-closed fallbacks.
    for p in ("Playtester", "PLAYTESTER", " playtester", "playtest", "bogus", "", None):
        assert plans.effective_entitlement_plan(p) == p


def test_catalog_is_consistent_with_every_limit_table():
    import meridian.server as srv
    from meridian import _deps, hosted

    known = plans.KNOWN_PLANS
    assert set(hosted.PLAN_LIMITS) <= known
    assert set(hosted._PLAN_STORAGE_LIMIT_GB) <= known
    assert set(_deps._TENANT_RL_PER_MINUTE) <= known
    assert set(srv._WORKSPACE_MEMBER_LIMITS) <= known
    assert plans.PURCHASABLE_PLANS <= known
    assert "playtester" not in plans.PURCHASABLE_PLANS
    assert plans.UNBILLED_PLANS <= known
    # Every alias points at a canonical plan that has its own rows.
    for alias, target in plans.ENTITLEMENT_ALIASES.items():
        assert alias in known and target in known
        assert target in hosted.PLAN_LIMITS and target in _deps._TENANT_RL_PER_MINUTE
    # The explicit PLAN_LIMITS row for a playtester is Pro's, not a retyped copy.
    assert hosted.PLAN_LIMITS["playtester"] == hosted.PLAN_LIMITS["pro"]


def test_every_known_plan_is_documented_in_the_plans_module():
    doc = plans.__doc__ or ""
    for p in plans.KNOWN_PLANS:
        assert f"``{p}``" in doc, f"plan {p!r} is not documented in meridian/plans.py"


def test_validate_plan_accepts_known_and_rejects_everything_else():
    for p in plans.KNOWN_PLANS:
        assert plans.validate_plan(p) == p
    for bad in ("bogus", "Playtester", "playtest", "pro ", "", None, 3):
        with pytest.raises(ValueError):
            plans.validate_plan(bad)


@pytest.mark.parametrize(
    "raw, expired",
    [
        (None, False),
        ("", False),
        ("   ", False),
        ("2026-10-07 00:00:00", False),
        ("2026-10-06 11:59:59", True),
        ("2026-10-06 12:00:00", True),
        ("2026-10-07T00:00:00Z", False),
        ("2026-10-05T00:00:00+00:00", True),
        ("not-a-date", True),  # unparseable counts as expired: fail closed
    ],
)
def test_playtester_optional_end_date(raw, expired):
    tenant = _t("playtester", inactivity_expires_at=raw)
    assert plans.playtester_access_expired(tenant, NOW) is expired
    assert plans.tenant_entitlement_plan(tenant, now=NOW) == ("free" if expired else "pro")


def test_end_date_is_ignored_for_every_other_plan():
    for p in ("free", "trial", "standard", "pro", "admin"):
        tenant = _t(p, inactivity_expires_at=PAST)
        assert plans.playtester_access_expired(tenant, NOW) is False
        assert plans.tenant_entitlement_plan(tenant, now=NOW) == p


def test_only_trial_window_plans_and_playtester_can_expire():
    assert {p for p in plans.KNOWN_PLANS if plans.plan_has_end_date(p)} == {
        "free", "trial", "playtester",
    }
    for odd in (None, "", "solo", "Playtester", 3):
        assert plans.plan_has_end_date(odd) is False
    assert plans.END_DATED_PLANS <= plans.KNOWN_PLANS


def test_is_internal_is_not_a_plan():
    # Staff keep their own flag; it neither maps to a plan nor changes one.
    tenant = _t("free", is_internal=1)
    assert plans.tenant_entitlement_plan(tenant) == "free"
    assert plans.is_unbilled_plan("free") is False


async def test_update_tenant_accepts_playtester_and_rejects_unknown_values(db):
    t = await db_module.upsert_tenant(db, "pt-write@example.com")
    t = await db_module.update_tenant(db, t["id"], plan="playtester")
    assert t["plan"] == "playtester"
    for bad in ("playtest", "Playtester", "", "bogus"):
        with pytest.raises(ValueError):
            await db_module.update_tenant(db, t["id"], plan=bad)
    assert (await db_module.get_tenant_by_id(db, t["id"]))["plan"] == "playtester"


# ---------------------------------------------------------------------------
# Every plan-dependent decision: playtester == pro
# ---------------------------------------------------------------------------


def _probe_tunnel_gate(plan):
    from meridian.routes import tunnel as tn
    return tn._is_tunnel_allowed(_t(plan))


def _probe_rate_limit_budget(plan):
    from meridian import _deps
    table = _deps._TENANT_RL_PER_MINUTE
    return table.get(_deps._tenant_rl_plan(_t(plan)), table["free"])


def _probe_limits_table(plan):
    # The helper run_overage_check and GET /settings/usage both resolve through.
    from meridian import hosted
    return hosted.plan_limits_for(_t(plan))


def _probe_direct_limits_row(plan):
    from meridian import hosted
    return hosted.PLAN_LIMITS.get(plan, hosted.PLAN_LIMITS["free"])


def _probe_storage_ceiling(plan):
    # The helper run_storage_overage_check resolves through.
    from meridian import hosted
    return hosted.storage_limit_gb_for(_t(plan))


def _probe_member_limit(plan):
    # The helper POST /workspace/invite resolves through.
    import meridian.server as srv
    return srv._workspace_member_limit(_t(plan))


def _probe_pool_tier(plan):
    # The tier provision_neon_db allocates from.
    from meridian import hosted
    return hosted.pool_tier_for(_t(plan))


def _probe_neon_api_key(plan):
    from meridian import hosted
    return hosted._neon_api_key_for_tier(plan)


def _probe_neon_org(plan):
    from meridian import hosted
    return hosted._neon_org_id_for_tier(plan)


def _probe_doc_store_backend(plan):
    from meridian.doc_store import resolve_doc_store_target
    return resolve_doc_store_target(plan, True, "/data", "postgresql://u:p@h/db")[1]


PROBES: dict[str, Callable[[str], Any]] = {
    "tunnel_gate": _probe_tunnel_gate,
    "rate_limit_budget": _probe_rate_limit_budget,
    "limits_table": _probe_limits_table,
    "direct_limits_row": _probe_direct_limits_row,
    "storage_ceiling": _probe_storage_ceiling,
    "member_limit": _probe_member_limit,
    "pool_tier": _probe_pool_tier,
    "neon_api_key": _probe_neon_api_key,
    "neon_org": _probe_neon_org,
    "doc_store_backend": _probe_doc_store_backend,
}


@pytest.mark.parametrize("probe", sorted(PROBES))
def test_playtester_decision_equals_pro(probe, monkeypatch):
    monkeypatch.setenv("NEON_API_KEY", "key-standard")
    monkeypatch.setenv("NEON_API_KEY_PRO", "key-pro")
    monkeypatch.setenv("NEON_ORG_ID", "org-standard")
    monkeypatch.setenv("NEON_ORG_ID_PRO", "org-pro")
    fn = PROBES[probe]
    assert fn("playtester") == fn("pro")
    # Not vacuous: the probe really distinguishes Pro from the lowest tier.
    assert fn("pro") != fn("free"), f"probe {probe!r} cannot tell pro from free"


# Probes whose tables fall back to the LOWEST tier for an unknown plan. (The
# storage/member/pool/Neon-account lookups default to standard/25 for a missing
# plan, which this lane leaves exactly as it was.)
FAIL_CLOSED_PROBES = [
    "tunnel_gate", "rate_limit_budget", "limits_table", "direct_limits_row",
    "doc_store_backend",
]
# These lower-case a plan before looking it up (they always have), so a
# differently-cased spelling is treated like the lower-case plan.
LOWERCASING_PROBES = {"rate_limit_budget", "doc_store_backend"}


@pytest.mark.parametrize("probe", FAIL_CLOSED_PROBES)
def test_unknown_plan_values_fail_closed(probe):
    """A value no gate recognises gets the lowest tier -- including near-misses
    of 'playtester' (the alias is an exact match)."""
    fn = PROBES[probe]
    for bogus in ("bogus", "playtest", "playtesters", "Playtester", "PLAYTESTER"):
        if probe in LOWERCASING_PROBES and bogus.lower() == "playtester":
            continue
        assert fn(bogus) == fn("free"), (probe, bogus)
    if probe != "limits_table":  # the usage jobs default a missing plan to 'standard'
        assert fn(None) == fn("free")
        assert fn("") == fn("free")


# ---------------------------------------------------------------------------
# Rate limiter, end to end (60 s plan cache included)
# ---------------------------------------------------------------------------


async def test_rate_limiter_meters_playtester_like_pro_and_lapsed_like_free(db, monkeypatch):
    import meridian.server as srv
    from meridian._deps import _reset_tenant_rate_limit

    def _fake_req(token):
        return types.SimpleNamespace(
            headers={"authorization": f"Bearer {token}"},
            url=types.SimpleNamespace(path="/mcp"),
            app=types.SimpleNamespace(state=types.SimpleNamespace(db=db)),
        )

    current: dict[str, Any] = {}

    async def _fake_tenant(_auth_db, _token_hash):
        return dict(current)

    monkeypatch.setattr(srv, "_hosted_mode", lambda: True)
    monkeypatch.setattr(db_module, "get_tenant_from_token_hash", _fake_tenant)
    monkeypatch.setitem(srv._TENANT_RL_PER_MINUTE, "free", 3)

    async def _blocked_after(extra: dict[str, Any], calls: int) -> int | None:
        current.clear()
        current.update({"id": f"tenant-{extra['plan']}-{id(extra)}", **extra})
        _reset_tenant_rate_limit()
        for n in range(calls):
            res = await srv._tenant_rate_limit_decision(_fake_req("tok"))
            if getattr(res, "status_code", None) == 429:
                return n
        return None

    assert await _blocked_after({"plan": "free"}, 10) == 3
    assert await _blocked_after({"plan": "pro"}, 10) is None
    assert await _blocked_after({"plan": "playtester"}, 10) is None
    # Past its end date a playtester drops to the free budget...
    assert await _blocked_after({"plan": "playtester", "inactivity_expires_at": PAST}, 10) == 3
    # ...an unparseable end date fails closed too...
    assert await _blocked_after({"plan": "playtester", "inactivity_expires_at": "nope"}, 10) == 3
    # ...and a near-miss spelling is simply an unknown plan.
    assert await _blocked_after({"plan": "playtest"}, 10) == 3
    # is_internal is not a plan: it grants the limiter nothing.
    assert await _blocked_after({"plan": "free", "is_internal": 1}, 10) == 3


# ---------------------------------------------------------------------------
# Hosted client helpers (real routes)
# ---------------------------------------------------------------------------


def _hosted_client(monkeypatch, tmp_path):
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    from fastapi.testclient import TestClient
    import meridian.server as server_module

    server_module = importlib.reload(server_module)
    return TestClient(server_module.app)


def _seed(client, email: str, plan: str, *, is_internal: bool = False, **fields: Any) -> dict[str, Any]:
    """Create a tenant on ``plan`` and log the client in as it."""
    from meridian import hosted as hosted_module

    db = client.app.state.db

    async def _go():
        t = await db_module.upsert_tenant(db, email)
        await db_module.update_tenant(db, t["id"], plan=plan, **fields)
        if is_internal:
            await db.execute("UPDATE tenants SET is_internal = 1 WHERE id = ?", (t["id"],))
            await db.commit()
        s = await db_module.create_user_session(db, t["id"], "2099-01-01T00:00:00+00:00")
        return await db_module.get_tenant_by_id(db, t["id"]), s

    tenant, session = asyncio.run(_go())
    client.cookies.set(
        hosted_module._SESSION_COOKIE, hosted_module._make_session_cookie(session["id"])
    )
    return tenant


def test_project_cap_follows_the_entitlement(monkeypatch, tmp_path):
    from meridian import _deps

    client = _hosted_client(monkeypatch, tmp_path)
    cases = [
        ("pro", {}, 201),
        ("playtester", {}, 201),
        ("free", {}, 403),
        ("playtester", {"inactivity_expires_at": PAST}, 403),
    ]
    with client:
        for i, (plan, extra, second_status) in enumerate(cases):
            tenant = _seed(client, f"cap-{i}@example.com", plan, **extra)
            # Each tenant gets its own project database (the production
            # _tenant_db_cache seam), so one tenant's projects never count
            # against another's cap.
            project_db = asyncio.run(db_module.init_db(":memory:"))
            _deps._tenant_db_cache[tenant["id"]] = project_db
            try:
                r1 = client.post("/projects", json={"name": f"first-{i}"})
                assert r1.status_code == 201, (plan, r1.text)
                r2 = client.post("/projects", json={"name": f"second-{i}"})
                assert r2.status_code == second_status, (plan, extra, r2.text)
                if second_status == 403:
                    assert "Free tier" in r2.json()["detail"]
            finally:
                _deps._tenant_db_cache.pop(tenant["id"], None)
                asyncio.run(project_db.close())


def test_me_reports_playtester_without_a_trial_clock(monkeypatch, tmp_path):
    client = _hosted_client(monkeypatch, tmp_path)
    with client:
        _seed(client, "me-pt@example.com", "playtester")
        me = client.get("/me").json()
        assert me["plan"] == "playtester"
        assert me["entitlement_plan"] == "pro"
        assert me["is_playtester"] is True
        assert me["expired"] is False and me["days_remaining"] is None
        assert me["has_stripe_customer"] is False and me["is_internal"] is False

        _seed(client, "me-pt-future@example.com", "playtester", inactivity_expires_at=FUTURE)
        me = client.get("/me").json()
        assert me["expired"] is False and me["days_remaining"] > 0
        assert me["entitlement_plan"] == "pro"

        _seed(client, "me-pt-past@example.com", "playtester", inactivity_expires_at=PAST)
        me = client.get("/me").json()
        assert me["expired"] is True and me["entitlement_plan"] == "free"

        _seed(client, "me-pt-junk@example.com", "playtester", inactivity_expires_at="garbage")
        me = client.get("/me").json()
        assert me["expired"] is True and me["entitlement_plan"] == "free"

        _seed(client, "me-pro@example.com", "pro")
        me = client.get("/me").json()
        assert me["entitlement_plan"] == "pro" and me["is_playtester"] is False


def test_is_internal_behaviour_is_unchanged_on_me(monkeypatch, tmp_path):
    """Regression guard: reads only fields /me already had before the playtester
    plan existed, so it passes on the code this lane started from."""
    client = _hosted_client(monkeypatch, tmp_path)
    with client:
        # Staff on a pro plan with a stale end date: never shown as expired.
        _seed(client, "staff@example.com", "pro", is_internal=True, inactivity_expires_at=PAST)
        me = client.get("/me").json()
        assert me["is_internal"] is True
        assert me["expired"] is False and me["days_remaining"] is None
        assert me["plan"] == "pro"
        # Staff on a trial-window plan with a lapsed date: still not expired.
        _seed(client, "staff-free@example.com", "free", is_internal=True,
              inactivity_expires_at=PAST)
        me = client.get("/me").json()
        assert me["plan"] == "free" and me["is_internal"] is True
        assert me["expired"] is False and me["days_remaining"] is None
        assert me["has_stripe_customer"] is False


def test_me_entitlement_for_staff_is_the_stored_plan(monkeypatch, tmp_path):
    """is_internal is a separate flag, not a plan: it never changes the
    entitlement /me reports."""
    client = _hosted_client(monkeypatch, tmp_path)
    with client:
        _seed(client, "staff-ent@example.com", "free", is_internal=True)
        me = client.get("/me").json()
        assert me["entitlement_plan"] == "free" and me["is_playtester"] is False
        assert me["is_internal"] is True
        _seed(client, "staff-ent-pro@example.com", "pro", is_internal=True,
              inactivity_expires_at=PAST)
        me = client.get("/me").json()
        assert me["entitlement_plan"] == "pro" and me["is_playtester"] is False


@pytest.mark.parametrize("plan", ["standard", "pro", "admin"])
def test_me_never_reports_a_paying_plan_as_expired(monkeypatch, tmp_path, plan):
    """A stale inactivity_expires_at (left by a trial or a playtester period)
    must not turn a paying tenant's dashboard into 'Pro expired'."""
    client = _hosted_client(monkeypatch, tmp_path)
    with client:
        _seed(client, f"paid-{plan}@example.com", plan, inactivity_expires_at=PAST)
        me = client.get("/me").json()
        assert me["plan"] == plan
        assert me["expired"] is False and me["days_remaining"] is None
        # The trial window itself is unchanged: a lapsed free tenant is expired.
        _seed(client, f"lapsed-free-{plan}@example.com", "free", inactivity_expires_at=PAST)
        assert client.get("/me").json()["expired"] is True


def test_converted_playtester_is_not_expired_after_paying(monkeypatch, tmp_path):
    """The documented conversion path: a playtester with an end date checks out
    for Pro. The webhook flips the plan but leaves the end date in the column;
    /me must read the tenant as a normal Pro one."""
    import meridian.hosted as hosted_module

    client = _hosted_client(monkeypatch, tmp_path)
    with client:
        monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)

        async def _cap(_db):
            return None

        async def _provision(tenant_id, db_):
            return await db_module.get_tenant_by_id(db_, tenant_id)

        async def _welcome(*_a, **_k):
            return None

        monkeypatch.setattr(hosted_module, "check_capacity", _cap)
        monkeypatch.setattr(hosted_module, "provision_neon_db", _provision)
        monkeypatch.setattr(hosted_module, "send_welcome_email", _welcome)

        _seed(client, "convert@example.com", "playtester", inactivity_expires_at=PAST)
        assert client.get("/me").json()["expired"] is True  # lapsed playtester, as before
        r = client.post(
            "/webhooks/stripe",
            json={
                "type": "checkout.session.completed",
                "data": {"object": {
                    "customer_email": "convert@example.com",
                    "customer": "cus_convert",
                    "metadata": {"plan": "pro"},
                }},
            },
        )
        assert r.status_code == 200, r.text
        me = client.get("/me").json()
        assert me["plan"] == "pro" and me["entitlement_plan"] == "pro"
        assert me["is_playtester"] is False and me["has_stripe_customer"] is True
        assert me["expired"] is False and me["days_remaining"] is None


def test_settings_usage_gives_playtester_pro_ceilings_and_no_budget(monkeypatch, tmp_path):
    client = _hosted_client(monkeypatch, tmp_path)
    with client:
        _seed(client, "use-pro@example.com", "pro")
        pro = client.get("/settings/usage").json()
        assert client.patch("/settings/usage", json={"compute_cap": 5, "storage_cap": 5}).status_code == 200

        _seed(client, "use-pt@example.com", "playtester")
        pt = client.get("/settings/usage").json()
        for key in ("unlimited",):
            assert pt[key] == pro[key]
        for section in ("compute", "storage"):
            for key in ("limit", "grace", "limit_gb", "rate", "unlimited"):
                assert pt[section].get(key) == pro[section].get(key), (section, key)
        assert pt["compute"]["limit"] == 200.0 and pt["storage"]["limit_gb"] == 10.0
        assert pro["overage_billing"] is True and pt["overage_billing"] is False
        assert pt["plan"] == "playtester"
        # No overage budget can be set on an unbilled plan.
        r = client.patch("/settings/usage", json={"compute_cap": 5, "storage_cap": 5})
        assert r.status_code == 400

        _seed(client, "use-pt-past@example.com", "playtester", inactivity_expires_at=PAST)
        lapsed = client.get("/settings/usage").json()
        assert lapsed["compute"]["limit"] == 10.0  # the free ceiling


def test_tunnel_plugins_card_gate_reports_the_entitlement_plan(monkeypatch, tmp_path):
    client = _hosted_client(monkeypatch, tmp_path)
    with client:
        for email, plan, extra, expect in (
            ("tp-pro@example.com", "pro", {}, "pro"),
            ("tp-pt@example.com", "playtester", {}, "pro"),
            ("tp-pt-past@example.com", "playtester", {"inactivity_expires_at": PAST}, "free"),
            ("tp-free@example.com", "free", {}, "free"),
        ):
            _seed(client, email, plan, **extra)
            body = client.get("/tunnel/plugins").json()
            assert body["plan"] == expect, (plan, extra)


def test_workspace_member_limit_follows_the_entitlement(monkeypatch, tmp_path):
    """The real limits table, with a member count between Standard's cap (25)
    and Pro's (50): the only place a playtester that fell through to the
    default (25) would be told it is full while Pro is not."""
    client = _hosted_client(monkeypatch, tmp_path)
    with client:
        async def _thirty_members(_db, _tenant_id):
            return 30

        monkeypatch.setattr(db_module, "count_workspace_members", _thirty_members)
        for i, (plan, extra, status) in enumerate((
            ("standard", {}, 402),
            ("pro", {}, 201),
            ("playtester", {}, 201),
            # past its end date a playtester has the Free entitlement
            ("playtester", {"inactivity_expires_at": PAST}, 402),
        )):
            _seed(client, f"ws-{i}@example.com", plan, **extra)
            r = client.post("/workspace/invite", json={"email": f"invitee-{i}@example.com"})
            assert r.status_code == status, (plan, extra, r.text)
            if status == 402:
                assert "(25)" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Checkout / webhook: a playtester is granted, never bought
# ---------------------------------------------------------------------------


def test_playtester_cannot_be_reached_through_checkout_or_stripe_metadata(monkeypatch, tmp_path):
    import meridian.hosted as hosted_module

    client = _hosted_client(monkeypatch, tmp_path)
    with client:
        monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
        r = client.get("/checkout", params={"plan": "playtester"}, follow_redirects=False)
        assert r.status_code == 400

        async def _cap(_db):
            return None

        async def _provision(tenant_id, db_):
            return await db_module.get_tenant_by_id(db_, tenant_id)

        async def _welcome(*_a, **_k):
            return None

        monkeypatch.setattr(hosted_module, "check_capacity", _cap)
        monkeypatch.setattr(hosted_module, "provision_neon_db", _provision)
        monkeypatch.setattr(hosted_module, "send_welcome_email", _welcome)
        for meta in ({"plan": "playtester"}, {"plan": ["pro"]}, {"plan": "Playtester"}):
            r = client.post(
                "/webhooks/stripe",
                json={
                    "type": "checkout.session.completed",
                    "data": {"object": {
                        "customer_email": "buyer@example.com",
                        "customer": "cus_buyer",
                        "metadata": meta,
                    }},
                },
            )
            assert r.status_code == 200, r.text
            tenant = asyncio.run(
                db_module.get_tenant_by_stripe_customer(client.app.state.db, "cus_buyer")
            )
            assert tenant["plan"] == "standard", meta


# ---------------------------------------------------------------------------
# Provisioning: pool tier and Neon account
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "plan, tier, key, retention",
    [
        ("pro", "pro", "key-pro", 604800),
        ("free", "free", "key-standard", 86400),
    ],
)
async def test_provisioning_still_allocates_an_ordinary_tenant_from_the_pool_of_its_tier(
    db, monkeypatch, plan, tier, key, retention
):
    """Everyone but a playtester claims a slot in a shared pool of their tier."""
    from meridian import hosted

    monkeypatch.setenv("NEON_API_KEY", "key-standard")
    monkeypatch.setenv("NEON_API_KEY_PRO", "key-pro")
    seen: dict[str, Any] = {}

    async def _capacity(_db):
        return {}

    async def _claim(_db, *, tier, max_customers):
        seen["tier"] = tier
        return {"id": "pool-row", "neon_project_id": "np-shared"}

    async def _create_db(api_key, _project_id, _db_name):
        seen["key"] = api_key
        return "postgres://u:p@h/db"

    async def _pitr(api_key, _project_id, seconds):
        seen["pitr"] = (api_key, seconds)

    monkeypatch.setattr(hosted, "check_capacity", _capacity)
    monkeypatch.setattr(db_module, "claim_pool_project_slot", _claim)
    monkeypatch.setattr(hosted, "_create_customer_database", _create_db)
    monkeypatch.setattr(hosted, "_set_neon_pitr", _pitr)

    t = await db_module.upsert_tenant(db, f"prov-{plan}@example.com")
    await db_module.update_tenant(db, t["id"], plan=plan)
    updated = await hosted.provision_neon_db(t["id"], db)
    assert updated["neon_project_id"] == "np-shared"
    assert seen["tier"] == tier
    assert seen["key"] == key
    assert seen["pitr"] == (key, retention)


@pytest.mark.parametrize(
    "ends, tier, key, retention",
    [
        (None, "pro", "key-pro", 604800),         # a live playtester: Pro's account, retention
        (PAST, "free", "key-standard", 86400),    # a lapsed one: Free's
    ],
)
async def test_provisioning_gives_a_playtester_a_neon_project_of_its_own(
    db, monkeypatch, ends, tier, key, retention
):
    """Neon reports usage per project, so a playtester never takes a slot in a
    shared pool: a new project is created for it, in the pool tier its
    entitlement maps to (the CHECK constraint only knows free/standard/pro),
    registered as full so nobody else is placed in it."""
    from meridian import hosted

    monkeypatch.setenv("NEON_API_KEY", "key-standard")
    monkeypatch.setenv("NEON_API_KEY_PRO", "key-pro")
    seen: dict[str, Any] = {}
    real_claim = db_module.claim_pool_project_slot

    async def _capacity(_db):
        return {}

    async def _no_claim(*_a, **_k):
        raise AssertionError("a playtester must not take a slot in a shared pool")

    async def _create_project(api_key, pool_tier):
        seen["project"] = (api_key, pool_tier)
        return "np-own", "postgres://u:p@h/first"

    async def _create_db(api_key, project_id, _db_name):
        seen["db"] = (api_key, project_id)
        return "postgres://u:p@h/db"

    async def _pitr(api_key, project_id, seconds):
        seen["pitr"] = (api_key, project_id, seconds)

    monkeypatch.setattr(hosted, "check_capacity", _capacity)
    monkeypatch.setattr(db_module, "claim_pool_project_slot", _no_claim)
    monkeypatch.setattr(hosted, "_create_neon_pool_project", _create_project)
    monkeypatch.setattr(hosted, "_create_customer_database", _create_db)
    monkeypatch.setattr(hosted, "_set_neon_pitr", _pitr)

    t = await db_module.upsert_tenant(db, "prov-own-pt@example.com")
    await db_module.update_tenant(db, t["id"], plan="playtester", inactivity_expires_at=ends)
    updated = await hosted.provision_neon_db(t["id"], db)

    assert updated["neon_project_id"] == "np-own"
    assert seen["project"] == (key, tier)
    assert seen["db"] == (key, "np-own")
    assert seen["pitr"] == (key, "np-own", retention)
    # The registry row is how the usage jobs find the project's Neon account; it
    # is full from the first moment, so no concurrent signup can be placed in it.
    async with db.execute(
        "SELECT id, tier, customer_count FROM neon_pool_projects WHERE neon_project_id = ?",
        ("np-own",),
    ) as cur:
        row = await cur.fetchone()
    assert row["tier"] == tier and row["customer_count"] == hosted._MAX_CUSTOMERS_PER_PROJECT
    assert updated["pool_project_id"] == row["id"]
    assert await real_claim(db, tier=tier, max_customers=hosted._MAX_CUSTOMERS_PER_PROJECT) is None


async def test_the_pool_allocator_never_places_a_tenant_beside_a_playtester(db):
    """A playtester that was alone on an ordinary project when it was granted the
    plan stays alone: the project is skipped however many slots it has left."""
    from meridian import hosted

    cap = hosted._MAX_CUSTOMERS_PER_PROJECT
    await db_module.register_pool_project(db, "np-solo", "pro")
    await db_module.register_pool_project(db, "np-open", "pro")
    await db.execute("UPDATE neon_pool_projects SET customer_count = 5 WHERE neon_project_id = 'np-solo'")
    await db.execute("UPDATE neon_pool_projects SET customer_count = 2 WHERE neon_project_id = 'np-open'")
    await db.commit()
    pt = await _provisioned(db, "alone-in-solo@example.com", "playtester", pool="np-solo")

    # np-solo is the fuller project with room, which is what the allocator prefers
    # unless it holds a playtester
    claimed = await db_module.claim_pool_project_slot(db, tier="pro", max_customers=cap)
    assert claimed["neon_project_id"] == "np-open" and claimed["customer_count"] == 3

    await db.execute("UPDATE neon_pool_projects SET customer_count = ? WHERE neon_project_id = 'np-open'", (cap,))
    await db.commit()
    assert await db_module.claim_pool_project_slot(db, tier="pro", max_customers=cap) is None

    # revoking the plan hands the project back to the allocator
    await db_module.update_tenant(db, pt["id"], plan="free")
    again = await db_module.claim_pool_project_slot(db, tier="pro", max_customers=cap)
    assert again["neon_project_id"] == "np-solo" and again["customer_count"] == 6


async def test_launch_cap_treats_playtester_like_pro(db, monkeypatch):
    """A paid/granted plan keeps its slot when the free launch cap is full; a
    free or unknown plan is waitlisted."""
    from meridian import hosted

    monkeypatch.setenv("MERIDIAN_LAUNCH_OPEN", "true")
    monkeypatch.setenv("MERIDIAN_FREE_LAUNCH_CAP", "0")

    async def _no_provision(*_a, **_k):
        raise AssertionError("must not provision when the free cap is full")

    monkeypatch.setattr(hosted, "provision_neon_db", _no_provision)
    full = "/waitlist-pending?message=Early%20access%20is%20full"

    async def _dest(plan_value: str, email: str) -> str:
        t = await db_module.upsert_tenant(db, email)
        if plan_value in plans.KNOWN_PLANS:
            t = await db_module.update_tenant(db, t["id"], plan=plan_value)
        else:  # an unknown value can only get here by a raw write
            await db.execute("UPDATE tenants SET plan = ? WHERE id = ?", (plan_value, t["id"]))
            await db.commit()
            t = await db_module.get_tenant_by_id(db, t["id"])
        return await hosted._post_login_redirect(t, db)

    assert await _dest("free", "cap-free@example.com") == full
    assert await _dest("pro", "cap-pro@example.com") == "/dashboard"
    assert await _dest("playtester", "cap-pt@example.com") == "/dashboard"
    assert await _dest("playtest", "cap-typo@example.com") == full


# ---------------------------------------------------------------------------
# Document structure store (ingest) and the pool migration script
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "plan, extra, backend",
    [
        ("pro", {}, "cloud_pg"),
        ("playtester", {}, "cloud_pg"),
        ("playtester", {"inactivity_expires_at": FUTURE}, "cloud_pg"),
        # The raw 'playtester' plan would still reach the Pro backend through the
        # alias inside resolve_doc_store_target; only the handler resolving the
        # entitlement first (lapsed -> free) keeps a lapsed playtester off it.
        ("playtester", {"inactivity_expires_at": PAST}, "local_sqlite"),
        ("free", {}, "local_sqlite"),
        ("standard", {}, "local_sqlite"),
    ],
)
async def test_ingest_doc_store_follows_the_entitlement(monkeypatch, plan, extra, backend):
    from meridian import doc_store, tenant_crypto
    from meridian.mcp import handler

    seen: dict[str, Any] = {}

    async def _open(**kwargs):
        seen["backend"] = doc_store.resolve_doc_store_target(**kwargs)[1]
        return "store"

    monkeypatch.setattr(doc_store, "open_doc_store_for", _open)
    monkeypatch.setattr(
        tenant_crypto, "decrypt_tenant_db_url", lambda _tid, _enc: "postgresql://u:p@h/db"
    )
    monkeypatch.setattr(handler, "_hosted_mode", lambda: True)
    monkeypatch.delenv("MERIDIAN_DOC_STORE_URL", raising=False)

    tenant = {"id": "t-ing", "plan": plan, "neon_db_url": "enc", **extra}
    assert await handler._resolve_ingest_doc_store(None, "/data", tenant) == "store"
    assert seen["backend"] == backend


def _run_pool_migration_script(monkeypatch, tmp_path, capsys, rows, *, pro_key: str):
    """Execute scripts/migrate_pool_provision.py for real, against a fake auth DB.

    The script runs at import time and loads a .env next to it, so a copy is run
    from tmp_path (no .env there); provisioning and the DB are stubbed."""
    import asyncio as _asyncio
    import runpy
    import shutil
    from pathlib import Path

    from meridian import hosted, pg_adapter

    src = Path(__file__).resolve().parent.parent / "scripts" / "migrate_pool_provision.py"
    script = tmp_path / "scripts" / src.name
    script.parent.mkdir()
    shutil.copy(src, script)

    # The script writes these two with setdefault; set them first so monkeypatch
    # owns (and removes) them and nothing leaks into the rest of the session.
    monkeypatch.setenv("NEON_API_KEY", "key-standard")
    monkeypatch.setenv("NEON_API_KEY_PRO", pro_key)
    monkeypatch.setenv("MERIDIAN_DB_URL", "postgresql://fake/auth")
    monkeypatch.delenv("MERIDIAN_PROJECT_ID", raising=False)  # HITL filing is skipped
    monkeypatch.setattr(sys, "path", list(sys.path))  # the script inserts its repo root
    monkeypatch.setattr(sys, "argv", [str(script)])

    provisioned: list[str] = []

    async def _provision(tenant_id, _db):
        provisioned.append(tenant_id)
        return {"pool_project_id": "pool-1", "neon_project_id": "np-1"}

    class _Cursor:
        async def fetchall(self):
            return rows

    class _Db:
        async def execute(self, _query, _params=()):
            return _Cursor()

        async def close(self):
            return None

    async def _open(_url):
        return _Db()

    monkeypatch.setattr(hosted, "provision_neon_db", _provision)
    monkeypatch.setattr(pg_adapter, "open_pg_connection", _open)
    try:
        runpy.run_path(str(script), run_name="__main__")
    finally:
        loop = _asyncio.get_event_loop()
        loop.close()
        _asyncio.set_event_loop(None)
    return provisioned, capsys.readouterr().out


def test_pool_migration_script_defers_a_playtester_like_a_pro_tenant(monkeypatch, tmp_path, capsys):
    rows = [
        {"id": f"id-{p}-0000", "email": f"{p}@example.com", "plan": p,
         "neon_project_id": None, "pool_project_id": None}
        for p in ("pro", "playtester", "free", "standard")
    ]
    provisioned, out = _run_pool_migration_script(monkeypatch, tmp_path, capsys, rows, pro_key="")
    # Without the Pro Neon key a playtester would be provisioned into the wrong
    # account (or fail at the pool allocation), exactly like a Pro tenant.
    assert provisioned == ["id-free-0000", "id-standard-0000"]
    assert out.count("DEFERRED") == 2


def test_pool_migration_script_provisions_everyone_when_the_pro_key_is_set(
    monkeypatch, tmp_path, capsys
):
    rows = [
        {"id": f"id-{p}-0000", "email": f"{p}@example.com", "plan": p,
         "neon_project_id": None, "pool_project_id": None}
        for p in ("pro", "playtester", "free")
    ]
    provisioned, out = _run_pool_migration_script(
        monkeypatch, tmp_path, capsys, rows, pro_key="key-pro"
    )
    assert provisioned == ["id-pro-0000", "id-playtester-0000", "id-free-0000"]
    assert "DEFERRED" not in out


# ---------------------------------------------------------------------------
# Tunnel client
# ---------------------------------------------------------------------------


def _run_tunnel_with_me(monkeypatch, tmp_path, me: dict[str, Any], capsys) -> tuple[int, str]:
    from unittest.mock import AsyncMock

    from meridian import tunnel_client as tc

    monkeypatch.setattr(tc, "_force_utf8_io", lambda: None)
    monkeypatch.setattr(tc, "_resolve_token", lambda t: "sk_tok")
    monkeypatch.setattr(tc, "_fetch_me", AsyncMock(return_value=me))
    # Past the plan gate the next thing that fails is the home-directory repo
    # scope guard, which is how these tests tell "gate passed" from "gate failed".
    monkeypatch.setattr(tc.Path, "cwd", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    rc = asyncio.run(tc.run_tunnel(token="sk_tok", base_url="https://x", repo_path=None))
    return rc, capsys.readouterr().err


@pytest.mark.parametrize(
    "me, passes_plan_gate",
    [
        ({"tenant_id": "t1", "plan": "pro"}, True),
        ({"tenant_id": "t1", "plan": "playtester"}, True),  # older server: alias map
        ({"tenant_id": "t1", "plan": "playtester", "entitlement_plan": "pro"}, True),
        ({"tenant_id": "t1", "plan": "playtester", "entitlement_plan": "free"}, False),
        ({"tenant_id": "t1", "plan": "free", "is_internal": True}, True),
        ({"tenant_id": "t1", "plan": "free"}, False),
        ({"tenant_id": "t1", "plan": "playtest"}, False),
    ],
)
def test_tunnel_client_plan_gate(monkeypatch, tmp_path, capsys, me, passes_plan_gate):
    rc, err = _run_tunnel_with_me(monkeypatch, tmp_path, me, capsys)
    assert rc == 1
    assert ("Pro feature" not in err) is passes_plan_gate, err
    if passes_plan_gate:
        assert "repo scope" in err


# ---------------------------------------------------------------------------
# Billing: never charged, dunned, churned or trial-expired; usage still bounded
# ---------------------------------------------------------------------------


class _Calls:
    """Everything the overage/dunning jobs could do to a tenant, recorded."""

    def __init__(self) -> None:
        self.meter: list[dict[str, Any]] = []
        self.storage_reports: list[tuple] = []
        self.cap_calls: list[tuple] = []  # every (neon_project_id, max_cu) sent to Neon
        self.emails: list[tuple[str, str, str]] = []
        self.owner_alerts: list[tuple[str, str]] = []
        self.cancelled: list[str] = []
        self.dropped: list[str] = []
        self.drop_dbs: list[Any] = []  # the control-plane db each database drop was given
        self.deleted: list[str] = []

    @property
    def throttles(self) -> list[tuple]:
        """Calls that clamp a pool to the throttle ceiling (0.25 CU)."""
        return [c for c in self.cap_calls if c[1] == 0.25]


@pytest.fixture
def calls(monkeypatch):
    from meridian import hosted

    rec = _Calls()

    async def _consumption(_project, _key, _from, _to):
        return {"periods": [{"consumption": [{"metrics": [
            {"metric_name": "compute_unit_seconds", "value": rec.cu_hours * 3600},
            {"metric_name": "root_branch_bytes_month", "value": rec.storage_gb * 1e9},
        ]}]}]}

    async def _throttle(project_id, _key, max_cu):
        rec.cap_calls.append((project_id, max_cu))

    async def _email(email, subject, html):
        rec.emails.append((email, subject, html))

    async def _owner(subject, html):
        rec.owner_alerts.append((subject, html))

    async def _report(customer_id, gb, _key):
        rec.storage_reports.append((customer_id, round(gb, 3)))

    async def _cancel(customer_id):
        rec.cancelled.append(customer_id)

    async def _drop(tenant, db=None):
        rec.dropped.append(tenant["id"])
        rec.drop_dbs.append(db)

    async def _delete(_db, tenant_id):
        rec.deleted.append(tenant_id)

    rec.cu_hours = 0.0
    rec.storage_gb = 0.0
    monkeypatch.setattr(hosted, "_fetch_neon_consumption", _consumption)
    monkeypatch.setattr(hosted, "_set_neon_max_cu", _throttle)
    monkeypatch.setattr(hosted, "_send_overage_email", _email)
    # raising=False: the regression guards that use this fixture also run
    # against code from before the playtester plan, which has no owner alert.
    monkeypatch.setattr(hosted, "_send_owner_alert", _owner, raising=False)
    monkeypatch.setattr(hosted, "report_stripe_overage", _report)
    monkeypatch.setattr(hosted, "cancel_stripe_subscription", _cancel)
    monkeypatch.setattr(hosted, "_drop_tenant_neon_database", _drop)
    monkeypatch.setattr(db_module, "delete_tenant_records", _delete)

    fake_stripe = types.ModuleType("stripe")
    fake_stripe.api_key = ""
    fake_stripe.billing = types.SimpleNamespace(
        MeterEvent=types.SimpleNamespace(create=lambda **kw: rec.meter.append(kw))
    )
    monkeypatch.setitem(sys.modules, "stripe", fake_stripe)

    class _FakeHttp:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, *_a, **_k):
            return types.SimpleNamespace(status_code=200, raise_for_status=lambda: None)

    monkeypatch.setattr("httpx.AsyncClient", _FakeHttp)
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_x")
    monkeypatch.setenv("NEON_API_KEY", "key-standard")
    monkeypatch.setenv("NEON_API_KEY_PRO", "key-pro")
    monkeypatch.delenv("MERIDIAN_ADMIN_EMAILS", raising=False)
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    return rec


async def _provisioned(
    db, email: str, plan: str, *, stripe: bool = True, pool: "str | None" = None, **fields: Any
) -> dict[str, Any]:
    """A tenant with a database. ``pool`` puts it in a named (shared) Neon pool
    project; by default each tenant gets a pool project of its own. Every tenant
    has an overage budget unless a field says otherwise."""
    t = await db_module.upsert_tenant(db, email)
    fields = {"compute_overage_cap_usd": 500.0, "storage_overage_cap_usd": 500.0, **fields}
    return await db_module.update_tenant(
        db, t["id"], plan=plan, neon_project_id=pool or f"np-{plan}-{email.split('@')[0]}",
        stripe_customer_id="cus_stray" if stripe else None, **fields,
    )


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


_POOL_KEY = {"free": "key-standard", "standard": "key-standard", "pro": "key-pro"}


class _FakeNeon:
    """Neon as the overage jobs see it: a project only answers to the API key of
    the account that owns it, which is the pool tier it was created under (a
    free or standard pool belongs to the standard account, a pro pool to the pro
    one). Anything else is refused the way the real API refuses it: an empty
    consumption answer."""

    def __init__(self) -> None:
        self.owner_key: dict[str, str] = {}
        self.fetches: list[tuple[str, str]] = []   # (project, key) of every consumption read
        self.cap_attempts: list[tuple[str, str, float]] = []  # (project, key, max_cu)

    def accepts(self, project: str, key: str) -> bool:
        return self.owner_key.get(project, key) == key


@pytest.fixture
def neon(calls, monkeypatch):
    from meridian import hosted

    fake = _FakeNeon()
    real_fetch = hosted._fetch_neon_consumption

    async def _consumption(project, key, from_dt, to_dt):
        fake.fetches.append((project, key))
        if not fake.accepts(project, key):
            return {}
        return await real_fetch(project, key, from_dt, to_dt)

    real_cap = hosted._set_neon_max_cu

    async def _cap(project, key, max_cu):
        fake.cap_attempts.append((project, key, max_cu))
        await real_cap(project, key, max_cu)

    monkeypatch.setattr(hosted, "_fetch_neon_consumption", _consumption)
    monkeypatch.setattr(hosted, "_set_neon_max_cu", _cap)
    return fake


async def _neon_pool(db, neon: _FakeNeon, project: str, tier: str) -> str:
    """Register ``project`` as a pool project of ``tier`` and make Neon answer
    only to that tier's account key."""
    await db_module.register_pool_project(db, project, tier)
    neon.owner_key[project] = _POOL_KEY[tier]
    return project


async def test_playtester_over_the_ceiling_is_alerted_never_billed_or_throttled(db, calls):
    from meridian import hosted

    calls.cu_hours, calls.storage_gb = 260.0, 20.0  # past Pro's 200 + 20 grace and 10 GB
    pt = await _provisioned(db, "bill-pt@example.com", "playtester")
    await hosted.run_overage_check(db)

    assert calls.meter == [] and calls.storage_reports == []
    assert calls.cap_calls == []  # nothing is ever clamped for a playtester
    assert sorted(s for _e, s, _h in calls.emails) == [
        "Meridian: compute limit reached",
        "Meridian: storage limit exceeded",
    ]
    for _e, _s, html in calls.emails:
        assert "Set an overage budget" not in html and "/GB-month" not in html
        assert "throttl" not in html.lower()
    assert len(calls.owner_alerts) == 2  # compute + storage
    row = await db_module.get_tenant_by_id(db, pt["id"])
    assert row["compute_throttled_at"] is None
    prefs = json.loads(row["notification_prefs"])
    assert prefs["playtester_compute_notice_month"] == _month()
    assert prefs["playtester_storage_notice_month"] == _month()
    assert prefs["storage"] is True  # the rest of the blob is kept

    # Re-checked every day, told once a month.
    await hosted.run_overage_check(db)
    assert len(calls.emails) == 2 and len(calls.owner_alerts) == 2
    assert calls.meter == [] and calls.storage_reports == [] and calls.cap_calls == []


async def test_control_pro_tenant_with_a_budget_is_still_billed(db, calls):
    """Same usage, same stray Stripe customer and budget: Pro is metered and
    not throttled, so the playtester test above is not passing vacuously."""
    from meridian import hosted

    calls.cu_hours, calls.storage_gb = 260.0, 20.0
    await _provisioned(db, "bill-pro@example.com", "pro")
    await hosted.run_overage_check(db)
    assert len(calls.meter) == 1 and calls.meter[0]["event_name"] == "compute_overage_cu_hours"
    assert len(calls.storage_reports) == 1
    assert calls.throttles == [] and calls.owner_alerts == []


async def test_a_pro_tenant_without_a_budget_is_still_throttled_as_before(db, calls):
    """The existing throttle for a tenant that has no overage budget is untouched."""
    from meridian import hosted

    calls.cu_hours = 260.0
    pro = await _provisioned(db, "throttle-pro@example.com", "pro", compute_overage_cap_usd=0.0)
    await hosted.run_overage_check(db)
    assert calls.cap_calls == [(pro["neon_project_id"], 0.25)]
    assert [s for _e, s, _h in calls.emails] == ["Meridian: compute limit reached — sessions throttled"]
    assert (await db_module.get_tenant_by_id(db, pro["id"]))["compute_throttled_at"]


async def test_playtester_warns_at_the_pro_threshold_without_budget_text(db, calls):
    from meridian import hosted

    calls.cu_hours = 210.0  # past Pro's 200 limit, inside its 20 grace hours
    await _provisioned(db, "warn-pt@example.com", "playtester")
    await _provisioned(db, "warn-pro@example.com", "pro")
    await hosted.run_overage_check(db)

    by_email = {e: (s, h) for e, s, h in calls.emails}
    pt_subject, pt_html = by_email["warn-pt@example.com"]
    pro_subject, pro_html = by_email["warn-pro@example.com"]
    assert pt_subject == pro_subject == "Meridian: compute approaching limit"
    assert "Set an overage budget" in pro_html
    assert "Set an overage budget" not in pt_html and "not billed" in pt_html
    assert "throttl" not in pt_html.lower() and "restricted" not in pt_html
    assert calls.cap_calls == [] and calls.meter == []

    # A playtester is warned once a month; the Pro control keeps its old daily behaviour.
    calls.emails.clear()
    await hosted.run_overage_check(db)
    assert [e for e, _s, _h in calls.emails] == ["warn-pro@example.com"]

    calls.emails.clear()
    calls.cu_hours = 150.0  # under the ceiling: silence
    await hosted.run_overage_check(db)
    assert calls.emails == []


async def test_playtester_is_judged_by_pro_limits_not_standard_ones(db, calls):
    """205 CU-hours is past Standard's grace (50 + 20) but only in Pro's grace
    window (200 + 20), so a playtester is warned and a Standard tenant (here
    with no Stripe customer, so nothing to meter) is throttled."""
    from meridian import hosted

    calls.cu_hours = 205.0
    await _provisioned(db, "judge-pt@example.com", "playtester")
    await _provisioned(db, "judge-std@example.com", "standard", stripe=False)
    await hosted.run_overage_check(db)
    assert [p for p, _ in calls.throttles] == ["np-standard-judge-std"]
    assert [e for e, s, _h in calls.emails if s.endswith("approaching limit")] == [
        "judge-pt@example.com"
    ]


async def test_lapsed_playtester_is_judged_by_free_limits(db, calls):
    from meridian import hosted

    # 14 CU-hours: far inside Pro's 200, but past Free's 10 and inside its 5 grace.
    calls.cu_hours = 14.0
    await _provisioned(db, "lapsed-pt@example.com", "playtester", inactivity_expires_at=PAST)
    await hosted.run_overage_check(db)
    assert [s for _e, s, _h in calls.emails] == ["Meridian: compute approaching limit"]
    assert calls.cap_calls == []


async def test_playtester_notices_are_once_per_calendar_month(db):
    from meridian import hosted

    t = await _provisioned(db, "month-pt@example.com", "playtester")
    jan = datetime(2026, 1, 31, 23, 0, tzinfo=timezone.utc)
    assert await hosted._claim_playtester_notice(db, t, "compute", jan) is True
    assert await hosted._claim_playtester_notice(db, t, "compute", jan) is False
    # the same month, another day
    assert await hosted._claim_playtester_notice(
        db, t, "compute", datetime(2026, 1, 2, tzinfo=timezone.utc)) is False
    # another kind of notice is its own
    assert await hosted._claim_playtester_notice(db, t, "storage", jan) is True
    # the next month re-arms it (a per-year flag would not), and so does the next year
    assert await hosted._claim_playtester_notice(
        db, t, "compute", datetime(2026, 2, 1, tzinfo=timezone.utc)) is True
    assert await hosted._claim_playtester_notice(
        db, t, "compute", datetime(2027, 2, 1, tzinfo=timezone.utc)) is True
    prefs = json.loads((await db_module.get_tenant_by_id(db, t["id"]))["notification_prefs"])
    assert prefs["playtester_compute_notice_month"] == "2027-02"
    assert prefs["playtester_storage_notice_month"] == "2026-01"
    assert prefs["sprint"] is True  # unrelated preferences survive every write


# --- Every other tenant is judged exactly as before, shared pool or not ------
#
# A playtester is only granted on a project of its own, so the usage jobs carry
# no rule about pools. These tests build the state the grant now refuses to
# create (a playtester sharing a project, as a raw write or an older release
# could have left it) to show that nothing a billed tenant gets depends on it.


async def _legacy_playtester(
    db, email: str, pool: str, *, stripe: bool = True, **fields: Any
) -> dict[str, Any]:
    """A playtester that shares ``pool``, written raw: ``update_tenant`` refuses it."""
    t = await _provisioned(db, email, "free", pool=pool, stripe=stripe, **fields)
    await db.execute("UPDATE tenants SET plan = 'playtester' WHERE id = ?", (t["id"],))
    await db.commit()
    return await db_module.get_tenant_by_id(db, t["id"])


async def test_free_tenant_sharing_a_pool_with_a_playtester_is_still_throttled(db, calls):
    from meridian import hosted

    calls.cu_hours = 20.0  # past Free's 10 + 5 grace, far from a playtester's 200
    await _legacy_playtester(db, "lg-pt@example.com", "np-legacy")
    free = await _provisioned(
        db, "lg-free@example.com", "free", pool="np-legacy", compute_overage_cap_usd=0.0
    )
    await hosted.run_overage_check(db)

    assert calls.cap_calls == [("np-legacy", 0.25)]
    assert [(e, s) for e, s, _h in calls.emails] == [
        ("lg-free@example.com", "Meridian: compute limit reached — sessions throttled")
    ]
    assert (await db_module.get_tenant_by_id(db, free["id"]))["compute_throttled_at"]
    assert calls.owner_alerts == []  # nobody is told a paying tenant "was not metered"


async def test_free_tenant_sharing_a_pool_with_a_playtester_is_still_warned(db, calls):
    from meridian import hosted

    calls.cu_hours = 12.0  # inside Free's grace window
    await _legacy_playtester(db, "lgw-pt@example.com", "np-legacy-w")
    await _provisioned(
        db, "lgw-free@example.com", "free", pool="np-legacy-w", compute_overage_cap_usd=0.0
    )
    await hosted.run_overage_check(db)

    assert [(e, s) for e, s, _h in calls.emails] == [
        ("lgw-free@example.com", "Meridian: compute approaching limit")
    ]
    assert calls.cap_calls == [] and calls.owner_alerts == []


async def test_usage_meter_of_a_tenant_sharing_a_pool_with_a_playtester_is_refreshed(db, calls):
    """A skipped tenant's usage columns were never written, so a Pro tenant's
    usage meter in the dashboard read 0."""
    from meridian import hosted

    calls.cu_hours, calls.storage_gb = 50.0, 2.0
    pt = await _legacy_playtester(db, "meter-pt@example.com", "np-meter")
    pro = await _provisioned(db, "meter-pro@example.com", "pro", pool="np-meter")
    await hosted.run_overage_check(db)

    for tenant in (pt, pro):
        row = await db_module.get_tenant_by_id(db, tenant["id"])
        assert row["compute_cu_hours_used"] == 50.0 and row["storage_gb_used"] == 2.0
    assert calls.emails == [] and calls.owner_alerts == [] and calls.meter == []


async def test_a_paying_tenant_is_metered_as_before_in_a_pool_that_holds_a_playtester(db, calls):
    from meridian import hosted

    calls.cu_hours = 260.0  # past Pro's 200 + 20 grace for both of them
    await _legacy_playtester(db, "mix-pt@example.com", "np-mix")
    await _provisioned(db, "mix-pro@example.com", "pro", pool="np-mix")  # has a budget
    await hosted.run_overage_check(db)

    assert [m["event_name"] for m in calls.meter] == ["compute_overage_cu_hours"]  # the payer only
    assert calls.cap_calls == []
    # the playtester gets its own notice, never a bill; nobody is told about a skipped payer
    assert [(e, s) for e, s, _h in calls.emails] == [
        ("mix-pt@example.com", "Meridian: compute limit reached")
    ]
    assert [s for s, _h in calls.owner_alerts] == [
        "[Meridian] Playtester over compute limit: mix-pt@example.com"
    ]


async def test_hourly_storage_job_reports_a_paying_tenant_in_a_pool_that_holds_a_playtester(
    db, calls, monkeypatch, caplog
):
    from meridian import hosted

    async def _gb(_project, _key):
        return 12.0  # over Pro's 10 GB

    monkeypatch.setattr(hosted, "get_neon_storage_gb", _gb)
    await _legacy_playtester(db, "stm-pt@example.com", "np-stm")
    # No overage budget at all: the hourly job reports overage without checking one.
    await _provisioned(db, "stm-pro@example.com", "pro", pool="np-stm", storage_overage_cap_usd=0.0)
    with caplog.at_level(logging.WARNING, logger="meridian.hosted"):
        await hosted.run_storage_overage_check(db)

    assert calls.storage_reports == [("cus_stray", 2.0)]  # the payer, as ever; never the playtester
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "stm-pt@example.com" in logged and "stm-pro@example.com" in logged
    assert "not attributable" not in logged


async def test_hourly_storage_job_bills_every_tenant_of_an_ordinary_shared_pool(db, calls, monkeypatch):
    from meridian import hosted

    async def _gb(_project, _key):
        return 12.0

    monkeypatch.setattr(hosted, "get_neon_storage_gb", _gb)
    await _provisioned(db, "st-mate-a@example.com", "pro", pool="np-st2", storage_overage_cap_usd=0.0)
    await _provisioned(db, "st-mate-b@example.com", "pro", pool="np-st2", storage_overage_cap_usd=0.0)
    await hosted.run_storage_overage_check(db)
    assert calls.storage_reports == [("cus_stray", 2.0), ("cus_stray", 2.0)]


async def test_daily_job_bills_pool_storage_to_a_paying_tenant_as_before(db, calls):
    from meridian import hosted

    calls.storage_gb = 20.0  # over Pro's 10 GB
    await _legacy_playtester(db, "dst-pt@example.com", "np-dst")
    await _provisioned(db, "dst-pro@example.com", "pro", pool="np-dst")
    await hosted.run_overage_check(db)

    assert calls.storage_reports == [("cus_stray", 10.0)]  # the payer's overage, as ever
    assert [(e, s) for e, s, _h in calls.emails] == [
        ("dst-pt@example.com", "Meridian: storage limit exceeded")
    ]
    assert [s for s, _h in calls.owner_alerts] == [
        "[Meridian] Playtester over storage limit: dst-pt@example.com"
    ]


async def test_playtester_in_a_dedicated_project_gets_the_normal_warnings_and_is_never_billed(
    db, calls, neon
):
    """The project provisioning creates for a playtester (registered full): its
    own Pro-threshold warning, then the ceiling notice, and never a meter event
    even with a Stripe customer and a budget on file, while a paying tenant
    elsewhere is metered as always."""
    from meridian import hosted

    await db_module.register_pool_project(
        db, "np-ded", "pro", customer_count=hosted._MAX_CUSTOMERS_PER_PROJECT
    )
    neon.owner_key["np-ded"] = "key-pro"
    await _provisioned(db, "ded-pt@example.com", "playtester", pool="np-ded")
    await _provisioned(db, "ded-pro@example.com", "pro")

    calls.cu_hours = 210.0  # past Pro's 200, inside its 20 grace hours
    await hosted.run_overage_check(db)
    assert {e: s for e, s, _h in calls.emails}["ded-pt@example.com"] == "Meridian: compute approaching limit"
    assert ("np-ded", "key-pro") in neon.fetches
    assert calls.meter == [] and calls.cap_calls == [] and calls.owner_alerts == []

    calls.emails.clear()
    calls.cu_hours = 260.0  # past the grace allowance
    await hosted.run_overage_check(db)
    assert {e: s for e, s, _h in calls.emails}["ded-pt@example.com"] == "Meridian: compute limit reached"
    assert [s for s, _h in calls.owner_alerts] == [
        "[Meridian] Playtester over compute limit: ded-pt@example.com"
    ]
    assert len(calls.meter) == 1 and calls.cap_calls == []  # the paying tenant's event, not the playtester's


# --- Which Neon account a usage job asks -------------------------------------


@pytest.mark.parametrize(
    "plan, pool_tier, polled_with",
    [
        ("playtester", "standard", "key-standard"),  # signed up as free/standard, flipped afterwards
        ("playtester", "free", "key-standard"),
        ("playtester", "pro", "key-pro"),
        # everyone else keeps asking the account their PLAN names, exactly as before
        # the playtester plan, even when their pool belongs to the other one
        ("pro", "standard", "key-pro"),
        ("standard", "pro", "key-standard"),
        ("free", "pro", "key-standard"),
    ],
)
async def test_overage_job_keys_follow_the_pool_for_a_playtester_and_the_plan_for_everyone_else(
    db, calls, neon, plan, pool_tier, polled_with
):
    from meridian import hosted

    pool = await _neon_pool(db, neon, f"np-key-{plan}-{pool_tier}", pool_tier)
    calls.cu_hours = 1.0
    await _provisioned(db, f"key-{plan}-{pool_tier}@example.com", plan, pool=pool)
    await hosted.run_overage_check(db)
    assert neon.fetches == [(pool, polled_with)]
    # usage is read, and persisted, only when the account asked owns the pool
    row = (await db_module.list_tenants_with_neon(db))[0]
    assert bool(row["compute_cu_hours_used"]) is (_POOL_KEY[pool_tier] == polled_with)


async def test_a_tenant_in_a_pool_of_another_tier_is_not_newly_measured_and_cannot_throttle_it(
    db, calls, neon
):
    """Reproduced by the verifier: A is on standard (a Stripe customer without a
    budget) and B on pro, both in one pro pool at 150 CU-hours. Following the pool
    registry for A would measure it past its own 50 + 20 grace and throttle the
    whole pool, B included. As before the playtester plan, A is simply not
    measured and nothing happens."""
    from meridian import hosted

    pool = await _neon_pool(db, neon, "np-pro-shared", "pro")
    calls.cu_hours = 150.0
    await _provisioned(db, "mm-a@example.com", "standard", pool=pool, compute_overage_cap_usd=0.0)
    await _provisioned(db, "mm-b@example.com", "pro", pool=pool, compute_overage_cap_usd=0.0)
    await hosted.run_overage_check(db)

    assert sorted(neon.fetches) == [(pool, "key-pro"), (pool, "key-standard")]
    assert calls.cap_calls == [] and neon.cap_attempts == []
    assert calls.emails == [] and calls.meter == []


async def test_playtester_flipped_from_a_standard_pool_is_still_measured_and_reported(
    db, calls, neon
):
    """The way a playtester is normally made: the person signs in (a free tenant,
    database in the standard account), then the plan is changed. The ceiling must
    still be measured: usage is read with the standard key, reported, never capped."""
    from meridian import hosted

    pool = await _neon_pool(db, neon, "np-flipped", "standard")
    calls.cu_hours = 260.0
    pt = await _provisioned(db, "flipped-pt@example.com", "playtester", pool=pool)
    await hosted.run_overage_check(db)

    assert neon.fetches == [(pool, "key-standard")]
    assert neon.cap_attempts == [] and calls.cap_calls == [] and calls.meter == []
    assert [s for _e, s, _h in calls.emails] == ["Meridian: compute limit reached"]
    assert [s for s, _h in calls.owner_alerts] == [
        "[Meridian] Playtester over compute limit: flipped-pt@example.com"
    ]
    assert (await db_module.get_tenant_by_id(db, pt["id"]))["compute_cu_hours_used"] == 260.0


async def test_database_outside_the_pool_registry_falls_back_to_the_plans_key(db, calls, neon):
    """A manually assigned database has no neon_pool_projects row: the plan's own
    account is the best remaining guess, as before."""
    from meridian import hosted

    calls.cu_hours = 1.0
    await _provisioned(db, "manual-pt@example.com", "playtester", pool="np-manual-pt")
    await _provisioned(db, "manual-std@example.com", "standard", pool="np-manual-std")
    await hosted.run_overage_check(db)
    assert sorted(neon.fetches) == [("np-manual-pt", "key-pro"), ("np-manual-std", "key-standard")]


async def test_storage_job_keys_follow_the_pool_for_a_playtester_and_the_plan_for_everyone_else(
    db, calls, neon, monkeypatch
):
    from meridian import hosted

    reads: list[tuple[str, str]] = []

    async def _gb(project, key):
        reads.append((project, key))
        return 0.0

    monkeypatch.setattr(hosted, "get_neon_storage_gb", _gb)
    for tier in ("standard", "pro"):
        await _neon_pool(db, neon, f"np-store-{tier}", tier)
        await _provisioned(db, f"store-{tier}-pt@example.com", "playtester", pool=f"np-store-{tier}")
    # an ordinary standard tenant in a pro pool is asked with its plan's key
    await _neon_pool(db, neon, "np-store-mismatch", "pro")
    await _provisioned(db, "store-mismatch@example.com", "standard", pool="np-store-mismatch")
    await hosted.run_storage_overage_check(db)
    assert sorted(reads) == [
        ("np-store-mismatch", "key-standard"),
        ("np-store-pro", "key-pro"),
        ("np-store-standard", "key-standard"),
    ]


async def test_storage_job_goes_on_when_one_pools_key_is_not_configured(
    db, calls, neon, monkeypatch, caplog
):
    """A missing key for one account used to abort the whole hourly pass at the
    first tenant of that pool, leaving everyone after it unmonitored."""
    from meridian import hosted

    reads: list[str] = []

    async def _gb(project, _key):
        reads.append(project)
        return 0.0

    monkeypatch.setattr(hosted, "get_neon_storage_gb", _gb)
    monkeypatch.delenv("NEON_API_KEY_PRO")
    await _neon_pool(db, neon, "np-nokey-pro", "pro")
    await _neon_pool(db, neon, "np-nokey-std", "standard")
    await _provisioned(db, "nokey-pt@example.com", "playtester", pool="np-nokey-pro")
    await _provisioned(db, "nokey-std@example.com", "standard", pool="np-nokey-std")
    with caplog.at_level(logging.WARNING, logger="meridian.hosted"):
        await hosted.run_storage_overage_check(db)
    assert reads == ["np-nokey-std"]
    assert any("nokey-pt@example.com" in r.getMessage() for r in caplog.records)


async def test_a_refused_consumption_read_is_logged_not_silent(db, calls, neon, caplog):
    """If Neon will not answer for a tenant's project, nothing can be enforced for
    it: the job says so (tenant, project, plan; never the key) instead of moving
    on, so a blind spot shows up in the logs."""
    from meridian import hosted

    pool = await _neon_pool(db, neon, "np-blind", "standard")
    neon.owner_key[pool] = "some-other-account-key"  # whatever key the job picks is refused
    calls.cu_hours = 900.0
    await _provisioned(db, "blind-pt@example.com", "playtester", pool=pool)
    with caplog.at_level(logging.WARNING, logger="meridian.hosted"):
        await hosted.run_overage_check(db)

    lines = [r.getMessage() for r in caplog.records if "blind-pt@example.com" in r.getMessage()]
    assert len(lines) == 1, "a refused consumption read must leave exactly one warning"
    line = lines[0]
    assert pool in line and "playtester" in line
    assert "key-standard" not in line and "key-pro" not in line
    assert calls.cap_calls == [] and calls.emails == [] and calls.meter == []


@pytest.mark.parametrize(
    "plan, project, registered, expected",
    [
        ("playtester", "np-a", {"np-a": "standard"}, "key-standard"),  # the pool beats the plan
        ("playtester", "np-a", {"np-a": "free"}, "key-standard"),
        ("playtester", "np-a", {"np-a": "pro"}, "key-pro"),
        ("pro", "np-a", {"np-a": "standard"}, "key-standard"),
        ("standard", "np-a", {"np-a": "pro"}, "key-pro"),
        ("playtester", "np-a", {}, "key-pro"),                          # not registered: the plan
        ("standard", "np-a", {"np-other": "pro"}, "key-standard"),
        (None, "np-a", {}, "key-standard"),                             # no plan: standard, as before
    ],
)
def test_neon_key_follows_the_pool_and_only_then_the_plan(monkeypatch, plan, project, registered, expected):
    from meridian import hosted

    monkeypatch.setenv("NEON_API_KEY", "key-standard")
    monkeypatch.setenv("NEON_API_KEY_PRO", "key-pro")
    tenant = {"plan": plan, "neon_project_id": project}
    assert hosted._neon_api_key_for_tenant(tenant, registered) == expected


@pytest.mark.parametrize(
    "plan, registered, expected",
    [
        ("playtester", {"np-a": "standard"}, "key-standard"),
        ("playtester", {"np-a": "pro"}, "key-pro"),
        ("playtester", {}, "key-pro"),
        ("pro", {"np-a": "standard"}, "key-pro"),       # an ordinary tenant ignores the registry
        ("standard", {"np-a": "pro"}, "key-standard"),
        ("free", {"np-a": "pro"}, "key-standard"),
        (None, {"np-a": "pro"}, "key-standard"),
    ],
)
def test_usage_jobs_ask_the_pool_only_for_an_unbilled_plan(monkeypatch, plan, registered, expected):
    from meridian import hosted

    monkeypatch.setenv("NEON_API_KEY", "key-standard")
    monkeypatch.setenv("NEON_API_KEY_PRO", "key-pro")
    tenant = {"plan": plan, "neon_project_id": "np-a"}
    assert hosted._neon_api_key_for_usage(tenant, registered) == expected


async def test_pool_registry_is_read_once_and_an_unreadable_one_degrades_to_the_plan(
    db, neon, caplog
):
    from meridian import hosted

    await _neon_pool(db, neon, "np-reg-pro", "pro")
    await _neon_pool(db, neon, "np-reg-free", "free")
    assert await hosted._pool_tiers_by_project(db) == {"np-reg-pro": "pro", "np-reg-free": "free"}

    class _Broken:
        def execute(self, *_a, **_k):
            raise RuntimeError("no such table")

    with caplog.at_level(logging.WARNING, logger="meridian.hosted"):
        assert await hosted._pool_tiers_by_project(_Broken()) == {}
    assert any("Pool registry unreadable" in r.getMessage() for r in caplog.records)


async def test_compute_and_storage_notices_do_not_erase_each_other(db, calls):
    """Both notices are de-duplicated through the same notification_prefs blob; a
    stale copy written back by the second one used to drop the first's flag, so
    the next day's run notified again."""
    from meridian import hosted

    calls.cu_hours, calls.storage_gb = 260.0, 20.0
    pt = await _provisioned(db, "both-pt@example.com", "playtester", pool="np-both")
    await hosted.run_overage_check(db)
    assert len(calls.owner_alerts) == 2 and len(calls.emails) == 2
    prefs = (await db_module.get_tenant_by_id(db, pt["id"]))["notification_prefs"]
    assert "playtester_compute_notice_month" in prefs and "playtester_storage_notice_month" in prefs

    await hosted.run_overage_check(db)
    assert len(calls.owner_alerts) == 2 and len(calls.emails) == 2


async def test_the_owner_alert_really_goes_to_the_admin_email(monkeypatch):
    """The fixtures above replace _send_owner_alert; this runs the real one."""
    from meridian import hosted

    sent: list[tuple[str, dict, dict]] = []

    class _Http:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, url, headers=None, json=None):
            sent.append((url, headers, json))

    monkeypatch.setattr("httpx.AsyncClient", _Http)
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")

    await hosted._alert_owner_playtester_limit("pt@example.com", "compute", "260 <b>CU</b>-hours")
    assert len(sent) == 1
    url, headers, body = sent[0]
    assert url == "https://api.resend.com/emails"
    assert headers["Authorization"] == "Bearer re_test_key"
    assert body["to"] == ["owner@example.com"]
    assert body["subject"] == "[Meridian] Playtester over compute limit: pt@example.com"
    assert "pt@example.com" in body["html"] and "&lt;b&gt;CU&lt;/b&gt;" in body["html"]

    # No key or no recipient: best-effort and silent, nothing is sent.
    monkeypatch.delenv("RESEND_API_KEY")
    await hosted._alert_owner_playtester_limit("pt@example.com", "compute", "x")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.delenv("ADMIN_EMAIL")
    await hosted._alert_owner_playtester_limit("pt@example.com", "compute", "x")
    assert len(sent) == 1


async def test_storage_job_detects_a_lapsed_playtester(db, calls, monkeypatch, caplog):
    """The hourly job must read the end date (it is not a SELECT *): 5 GB is
    inside a playtester's Pro ceiling but over the Free one a lapsed one drops to."""
    from meridian import hosted

    async def _gb(_project, _key):
        return 5.0

    monkeypatch.setattr(hosted, "get_neon_storage_gb", _gb)
    await _provisioned(db, "store-live-pt@example.com", "playtester", inactivity_expires_at=FUTURE)
    await _provisioned(db, "store-lapsed-pt@example.com", "playtester", inactivity_expires_at=PAST)
    with caplog.at_level(logging.WARNING, logger="meridian.hosted"):
        await hosted.run_storage_overage_check(db)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "store-lapsed-pt" in logged and "store-live-pt" not in logged
    assert calls.storage_reports == []  # and neither is ever reported to Stripe


async def test_storage_job_never_reports_a_playtester_to_stripe(db, calls, monkeypatch, caplog):
    from meridian import hosted

    async def _gb(_project, _key):
        return calls.storage_gb

    monkeypatch.setattr(hosted, "get_neon_storage_gb", _gb)
    await _provisioned(db, "store-pt@example.com", "playtester")
    await _provisioned(db, "store-pro@example.com", "pro")
    await _provisioned(db, "store-std@example.com", "standard")

    with caplog.at_level(logging.WARNING, logger="meridian.hosted"):
        calls.storage_gb = 5.0  # inside Pro's 10 GB, over Standard's 1 GB
        await hosted.run_storage_overage_check(db)
    assert [r.getMessage() for r in caplog.records if "store-pt" in r.getMessage()] == []
    assert any("store-std" in r.getMessage() for r in caplog.records)
    assert calls.storage_reports == [("cus_stray", 4.0)]  # standard only

    calls.storage_reports.clear()
    calls.storage_gb = 12.0  # over Pro's ceiling
    with caplog.at_level(logging.WARNING, logger="meridian.hosted"):
        await hosted.run_storage_overage_check(db)
    assert any("store-pt" in r.getMessage() for r in caplog.records)
    # pro and standard are metered; the playtester is only logged
    assert len(calls.storage_reports) == 2


async def test_dunning_never_touches_a_playtester(db, calls):
    from meridian import hosted

    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    pt = await _provisioned(db, "dun-pt@example.com", "playtester", payment_failed_at=old)
    pro = await _provisioned(db, "dun-pro@example.com", "pro", payment_failed_at=old)
    await hosted.run_dunning_cleanup(db)
    assert calls.deleted == [pro["id"]]  # day-15 hard delete reaches only the paying tenant
    assert pt["id"] not in calls.deleted and pt["id"] not in calls.dropped
    # the delete drops the database with the registry in hand (see the drop tests below)
    assert calls.dropped == [pro["id"]] and calls.drop_dbs == [db]


async def test_churn_cleanup_never_touches_a_playtester(db, calls, monkeypatch):
    from meridian import hosted

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(db_module, "decrement_pool_project_count", _noop)
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    await db.execute(
        "INSERT INTO tenants (id, email, plan, neon_project_id, pool_project_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("t-pt", "churn-pt@example.com", "playtester", "np-pt", "pool-1", old),
    )
    await db.execute(
        "INSERT INTO tenants (id, email, plan, neon_project_id, pool_project_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("t-free", "churn-free@example.com", "free", "np-free", "pool-1", old),
    )
    await db.commit()

    await hosted.run_churn_cleanup(db)
    assert calls.dropped == ["t-free"]  # the control is churned, the playtester is not
    assert calls.drop_dbs == [db]
    row = await db_module.get_tenant_by_id(db, "t-pt")
    assert row["neon_project_id"] == "np-pt"


def test_playtester_never_receives_trial_reminders():
    from meridian import hosted

    in_three_days = (NOW + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    free = {"plan": "free", "inactivity_expires_at": in_three_days}
    pt = {"plan": "playtester", "inactivity_expires_at": in_three_days}
    assert hosted.compute_trial_reminder(free, NOW) is not None  # control
    assert hosted.compute_trial_reminder(pt, NOW) is None
    assert "playtester" not in hosted._TRIAL_PLANS


def test_provisioning_pool_tier_of_a_lapsed_playtester_is_frees():
    from meridian import hosted

    assert hosted.pool_tier_for({"plan": "playtester"}) == "pro"
    assert hosted.pool_tier_for({"plan": "playtester", "inactivity_expires_at": FUTURE}) == "pro"
    assert hosted.pool_tier_for({"plan": "playtester", "inactivity_expires_at": PAST}) == "free"
    assert hosted.pool_tier_for({"plan": "pro"}) == "pro"
    assert hosted.pool_tier_for({"plan": None}) == "standard"


# ---------------------------------------------------------------------------
# is_internal keeps its meaning (staff): skipped by every lifecycle job
# ---------------------------------------------------------------------------


async def test_is_internal_tenants_are_still_skipped_by_every_job(db, calls, monkeypatch):
    from meridian import hosted

    async def _gb(_project, _key):
        return 99.0

    monkeypatch.setattr(hosted, "get_neon_storage_gb", _gb)
    calls.cu_hours, calls.storage_gb = 999.0, 99.0
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    staff = await _provisioned(db, "staff-job@example.com", "standard", payment_failed_at=old)
    await db.execute("UPDATE tenants SET is_internal = 1 WHERE id = ?", (staff["id"],))
    await db.commit()

    await hosted.run_overage_check(db)
    await hosted.run_storage_overage_check(db)
    await hosted.run_dunning_cleanup(db)
    await hosted.run_churn_cleanup(db)
    assert calls.meter == [] and calls.storage_reports == [] and calls.throttles == []
    assert calls.emails == [] and calls.owner_alerts == []
    assert calls.deleted == [] and calls.dropped == []


def test_is_internal_still_opens_the_tunnel_gate_regardless_of_plan():
    """Regression guard: mentions no plan value that did not exist before the
    playtester plan, so it passes on the code this lane started from."""
    from meridian.routes import tunnel as tn

    assert tn._is_tunnel_allowed({"plan": "free", "is_internal": True}) is True
    assert tn._is_tunnel_allowed({"plan": "free", "is_internal": False}) is False
    assert tn._is_tunnel_allowed({"plan": "standard", "is_internal": True}) is True
    assert tn._is_tunnel_allowed({"plan": "standard"}) is False
    assert tn._is_tunnel_allowed({"plan": "pro"}) is True
    assert tn._is_tunnel_allowed({"plan": "admin"}) is True
    assert tn._is_tunnel_allowed({"plan": "playtest", "is_internal": True}) is True
    assert tn._is_tunnel_allowed({"plan": "playtest"}) is False
    assert tn._is_tunnel_allowed({"is_internal": True}) is True
    assert tn._is_tunnel_allowed({}) is False


def test_tunnel_gate_for_a_playtester_is_pros_and_independent_of_is_internal():
    from meridian.routes import tunnel as tn

    assert tn._is_tunnel_allowed({"plan": "playtester", "is_internal": False}) is True
    assert tn._is_tunnel_allowed({"plan": "playtester"}) is True
    # A lapsed playtester has Free's entitlement: closed, unless it is also staff.
    lapsed = {"plan": "playtester", "inactivity_expires_at": PAST}
    assert tn._is_tunnel_allowed(lapsed) is False
    assert tn._is_tunnel_allowed({**lapsed, "is_internal": True}) is True


# ---------------------------------------------------------------------------
# Dashboard source (no node needed: raw module source is scanned)
# ---------------------------------------------------------------------------


def test_dashboard_labels_playtester_and_never_offers_it_billing():
    src = dashboard_source()
    assert "playtester: 'Playtester'" in src
    assert src.count("playtester: '#0891b2'") == 2  # sprint badge + sidebar badge colours
    # no billing/upgrade affordance for a playtester (badge + settings account card)
    assert src.count("plan === 'admin' || plan === 'playtester'") == 2
    assert "u.overage_billing === false" in src
    # feature gating keys off the server's entitlement verdict, not the label
    assert "me.entitlement_plan || me.plan" in src
    # the Tunnel Plugins card gate is unchanged: the server now sends the
    # entitlement plan in `plan`, so a playtester reads as 'pro' there
    assert "plan === 'pro' || plan === 'admin'" in src


def test_dashboard_says_when_a_playtesters_access_ends_and_has_no_budget_copy_to_throttle():
    src = dashboard_source()
    # the optional end date, in the account card (no purchase button next to it)
    assert "Playtester access ends" in src and "Playtester access ended" in src
    assert "plan === 'playtester' && !window.state.tenantIsInternal" in src
    # the usage card's fixed-limits note must stay true: nothing is throttled
    # for a playtester, the tenant is emailed when usage passes the limits
    assert "you are emailed when usage passes them" in src
    assert "restricted" not in src.split("These limits are fixed for this account")[1][:200]
    assert "compute is throttled once the grace allowance is used" not in src


def test_tracked_bundle_carries_the_playtester_dashboard_and_matches_its_manifest():
    """dashboard.bundle.js is tracked and is what the hosted image serves (the
    Dockerfile has no node step), so a source change that is not rebuilt into it
    never reaches a user. Rebuild with `node build.mjs` and commit both
    dashboard.bundle.js and asset-manifest.json."""
    import hashlib
    import json
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "meridian" / "static"
    raw = (static / "dashboard.bundle.js").read_bytes()
    bundle = raw.decode("utf-8")
    stale = "dashboard.bundle.js is stale: rebuild with node build.mjs"
    for marker in (
        'playtester: "Playtester"',                      # plan label
        'plan === "playtester" || !!me.is_internal',     # sidebar badge: no billing button
        'plan === "playtester" || !!window.state.tenantIsInternal',  # account card
        "Playtester access ends",                        # optional end date note
        "overage_billing === false",                     # no overage budget row
        "me.entitlement_plan || me.plan",                # tunnel indicator follows entitlement
    ):
        assert marker in bundle, f"{stale} ({marker!r} missing)"
    assert bundle.count('playtester: "#0891b2"') == 2, stale  # two badge colour maps
    manifest = json.loads((static / "asset-manifest.json").read_text(encoding="utf-8"))
    assert manifest["bundle_hash"] == hashlib.sha256(raw).hexdigest()[:12], stale


# ---------------------------------------------------------------------------
# Database drops: the Neon account is the pool's, not the plan's
# ---------------------------------------------------------------------------
#
# Account deletion, admin reset-provisioning, churn and dunning all drop a
# tenant's database through one best-effort helper. It used to pick the Neon key
# from the plan; a playtester is normally a free/standard tenant whose plan was
# flipped, so its database sits in a standard-account pool, Neon refused the Pro
# key, the helper swallowed that, and the caller still answered "deleted".


class _NeonConsole:
    """Neon's console API as a database drop sees it: a project answers only to the
    key of the account that owns it. ``log`` holds every (method, project, key,
    answered) it was asked."""

    def __init__(self) -> None:
        self.owner_key: dict[str, str] = {}
        self.log: list[tuple[str, str, str, bool]] = []
        self.deleted_databases: list[tuple[str, str]] = []

    def answers(self, project: str, key: str) -> bool:
        return self.owner_key.get(project, key) == key


@pytest.fixture
def neon_console(monkeypatch):
    console = _NeonConsole()

    def _who(url: str, headers: "dict | None") -> tuple[str, str]:
        key = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        return url.split("/projects/", 1)[1].split("/", 1)[0], key

    class _Http:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def get(self, url, headers=None, **_k):
            project, key = _who(url, headers)
            ok = console.answers(project, key)
            console.log.append(("GET", project, key, ok))
            body = {"branches": [{"id": "br-main", "default": True}]} if ok else {"message": "not found"}
            return types.SimpleNamespace(status_code=200 if ok else 404, json=lambda: body)

        async def delete(self, url, headers=None, **_k):
            project, key = _who(url, headers)
            ok = console.answers(project, key)
            console.log.append(("DELETE", project, key, ok))
            if ok:
                console.deleted_databases.append((project, url.rsplit("/", 1)[1]))
            return types.SimpleNamespace(status_code=200 if ok else 404, json=lambda: {})

    monkeypatch.setattr("httpx.AsyncClient", _Http)
    monkeypatch.setenv("NEON_API_KEY", "key-standard")
    monkeypatch.setenv("NEON_API_KEY_PRO", "key-pro")
    return console


@pytest.mark.parametrize(
    "plan, pool_tier, key",
    [
        ("playtester", "standard", "key-standard"),  # the verifier's repro: the Pro key was used and refused
        ("playtester", "free", "key-standard"),
        ("playtester", "pro", "key-pro"),
        ("pro", "standard", "key-standard"),         # same bug class for any plan changed after provisioning
        ("standard", "pro", "key-pro"),
        ("standard", "standard", "key-standard"),    # controls
        ("pro", "pro", "key-pro"),
    ],
)
async def test_database_drop_asks_the_account_that_owns_the_pool(db, neon_console, plan, pool_tier, key):
    from meridian import hosted

    await db_module.register_pool_project(db, "np-drop", pool_tier)
    neon_console.owner_key["np-drop"] = _POOL_KEY[pool_tier]
    tenant = {"id": "abcdef12-0000", "email": "who.am@example.com", "plan": plan,
              "neon_project_id": "np-drop"}
    await hosted._drop_tenant_neon_database(tenant, db)
    assert neon_console.log == [("GET", "np-drop", key, True), ("DELETE", "np-drop", key, True)]
    assert neon_console.deleted_databases == [("np-drop", "cust_who_am_abcdef12")]


async def test_database_drop_falls_back_to_the_plans_key_for_an_unregistered_pool(db, neon_console):
    """A manually assigned database has no registry row: the plan's account is the
    best guess, with or without the registry at hand."""
    from meridian import hosted

    tenant = {"id": "abcdef12-0000", "email": "who@example.com", "plan": "playtester",
              "neon_project_id": "np-manual"}
    await hosted._drop_tenant_neon_database(tenant, db)
    await hosted._drop_tenant_neon_database(tenant)
    assert [entry[2] for entry in neon_console.log] == ["key-pro"] * 4


async def test_a_drop_neon_refuses_leaves_a_warning_and_never_names_the_key(db, neon_console, caplog):
    from meridian import hosted

    neon_console.owner_key["np-refused"] = "some-other-account-key"
    tenant = {"id": "abcdef12-0000", "email": "who@example.com", "plan": "pro",
              "neon_project_id": "np-refused"}
    with caplog.at_level(logging.WARNING, logger="meridian.hosted"):
        await hosted._drop_tenant_neon_database(tenant, db)
    assert [entry[0] for entry in neon_console.log] == ["GET"]  # no DELETE was attempted
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "np-refused" in logged and "abcdef12-0000" in logged and "404" in logged
    assert "key-pro" not in logged and "key-standard" not in logged


def test_account_deletion_removes_a_playtesters_database_from_the_account_that_owns_it(
    monkeypatch, tmp_path, neon_console
):
    """Reproduced by the verifier through POST /account/delete: a playtester made
    the normal way (database in a standard-account pool) got {"deleted": true}
    while Neon refused every call and the database stayed."""
    client = _hosted_client(monkeypatch, tmp_path)
    with client:
        asyncio.run(db_module.register_pool_project(client.app.state.db, "np-del", "standard"))
        neon_console.owner_key["np-del"] = "key-standard"
        tenant = _seed(client, "del-pt@example.com", "playtester", neon_project_id="np-del")
        r = client.post("/account/delete", json={"confirmation": "DELETE"})
        assert r.status_code == 200 and r.json() == {"deleted": True}
        deadline = time.monotonic() + 5  # the drop runs as a background task
        while not neon_console.deleted_databases and time.monotonic() < deadline:
            time.sleep(0.05)
    assert neon_console.log[:2] == [
        ("GET", "np-del", "key-standard", True), ("DELETE", "np-del", "key-standard", True),
    ]
    assert neon_console.deleted_databases == [("np-del", f"cust_del-pt_{tenant['id'][:8]}")]


async def test_reset_provisioning_drops_the_playtesters_database_from_the_account_that_owns_it(
    db, neon_console
):
    from meridian import hosted

    await db_module.register_pool_project(db, "np-reset", "standard")
    neon_console.owner_key["np-reset"] = "key-standard"
    t = await _provisioned(db, "reset-pt@example.com", "playtester", pool="np-reset", stripe=False)
    result = await hosted.reset_tenant_provisioning(db, t["id"])
    assert result["had_neon_project"] is True and result["dropped_neon_project_id"] == "np-reset"
    assert neon_console.deleted_databases == [("np-reset", f"cust_reset-pt_{t['id'][:8]}")]
    assert [entry[2] for entry in neon_console.log] == ["key-standard", "key-standard"]


# ---------------------------------------------------------------------------
# Boot-time backfills never touch a playtester
# ---------------------------------------------------------------------------
#
# MERIDIAN_INTERNAL_EMAILS re-asserts is_internal=1 and MERIDIAN_ADMIN_EMAILS
# plan='admin' on EVERY boot. If either secret lists the account an operator just
# made a playtester, the next restart would silently undo it.


async def _make_tenant(db, email: str, plan: str = "free", **fields: Any) -> dict[str, Any]:
    t = await db_module.upsert_tenant(db, email)
    return await db_module.update_tenant(db, t["id"], plan=plan, **fields) if (plan != "free" or fields) else t


async def _flag(db, tenant_id: str, column: str) -> Any:
    return (await db_module.get_tenant_by_id(db, tenant_id))[column]


async def _seed_backfill_cases(db) -> dict[str, dict[str, Any]]:
    return {
        "pt": await _make_tenant(db, "pt-boot@example.com", "playtester"),
        "staff": await _make_tenant(db, "staff-boot@example.com", "pro"),
        "boss": await _make_tenant(db, "boss-boot@example.com", "free"),
    }


async def test_boot_backfills_leave_a_playtester_alone_and_still_do_their_job(db, monkeypatch):
    monkeypatch.setenv("MERIDIAN_INTERNAL_EMAILS", "pt-boot@example.com, STAFF-boot@example.com")
    monkeypatch.setenv("MERIDIAN_ADMIN_EMAILS", "pt-boot@example.com,boss-boot@example.com")
    cases = await _seed_backfill_cases(db)

    for _boot in range(2):  # every boot re-runs them
        await db_module._migrate_tenants_is_internal(db)
        await db_module._migrate_admin_plan(db)
        assert await _flag(db, cases["pt"]["id"], "is_internal") == 0
        assert await _flag(db, cases["pt"]["id"], "plan") == "playtester"
        # the behaviour for everyone else is exactly what it was
        assert await _flag(db, cases["staff"]["id"], "is_internal") == 1
        assert await _flag(db, cases["boss"]["id"], "plan") == "admin"


class _PgOverSqlite:
    """Just enough of PostgresConnection for the Postgres boot backfills. They use
    the same ``?`` SQL as the SQLite ones (the adapter only swaps the placeholder),
    so the statements run on the SQLite test db and their effect is checked there.
    The information_schema probe answers 'integer' (no legacy BOOLEAN column)."""

    def __init__(self, db) -> None:
        self.db = db

    async def executescript(self, _sql: str) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()):
        return _PgExec(self.db, sql, params)


class _PgExec:
    def __init__(self, db, sql: str, params: tuple) -> None:
        self.db, self.sql, self.params = db, sql, params

    async def _run(self) -> "_PgExec":
        if "information_schema" not in self.sql:
            await self.db.execute(self.sql, self.params)
            await self.db.commit()
        return self

    def __await__(self):
        return self._run().__await__()

    async def __aenter__(self) -> "_PgExec":
        return await self._run()

    async def __aexit__(self, *_a) -> bool:
        return False

    async def fetchone(self) -> dict[str, str]:
        return {"data_type": "integer"}


async def test_postgres_boot_backfills_leave_a_playtester_alone_and_still_do_their_job(db, monkeypatch):
    from meridian import pg_adapter

    monkeypatch.setenv("MERIDIAN_INTERNAL_EMAILS", "pt-boot@example.com,staff-boot@example.com")
    monkeypatch.setenv("MERIDIAN_ADMIN_EMAILS", "pt-boot@example.com,boss-boot@example.com")
    cases = await _seed_backfill_cases(db)
    conn = _PgOverSqlite(db)

    for _boot in range(2):
        await pg_adapter._migrate_pg_tenants_is_internal(conn)
        await pg_adapter._migrate_pg_admin_plan(conn)
        assert await _flag(db, cases["pt"]["id"], "is_internal") == 0
        assert await _flag(db, cases["pt"]["id"], "plan") == "playtester"
        assert await _flag(db, cases["staff"]["id"], "is_internal") == 1
        assert await _flag(db, cases["boss"]["id"], "plan") == "admin"


# ---------------------------------------------------------------------------
# Operator action: set / revoke a plan by email (tenant_plan_admin)
# ---------------------------------------------------------------------------


def test_every_plan_has_a_label_that_matches_the_dashboard_and_settable_plans_are_known():
    import re

    assert set(plans.PLAN_LABELS) == set(plans.KNOWN_PLANS)
    assert plans.OPERATOR_SETTABLE_PLANS <= plans.KNOWN_PLANS
    assert "admin" not in plans.OPERATOR_SETTABLE_PLANS and "playtester" in plans.OPERATOR_SETTABLE_PLANS
    assert plans.plan_label("playtester") == "Playtester" and plans.plan_label("bogus") == "bogus"
    assert plans.plan_label(None) == ""
    block = re.search(r"export const _PLAN_LABELS[^=]*=\s*\{(.*?)\};", dashboard_source(), re.S)
    assert block, "dashboard-utils.ts _PLAN_LABELS not found"
    client_labels = dict(re.findall(r"(\w+):\s*'([^']*)'", block.group(1)))
    for plan, label in plans.PLAN_LABELS.items():
        assert client_labels[plan] == label, plan


async def _audit(db, tenant_id: str) -> list[dict[str, Any]]:
    return await db_module.get_action_audit_log(db, tenant_id=tenant_id, event_type="tenant_plan_changed")


async def test_grant_previews_then_applies_audits_and_is_idempotent(db):
    from meridian import tenant_plan_admin as tpa

    t = await db_module.upsert_tenant(db, "grant@example.com")

    preview = await tpa.set_tenant_plan_by_email(
        db, "grant@example.com", "playtester", actor="owner@example.com")
    assert preview["applied"] is False and preview["changed"] is True
    assert preview["plan_before"] == "free" and preview["plan"] == "playtester"
    assert preview["plan_label"] == "Playtester"
    assert (await _flag(db, t["id"], "plan")) == "free"  # a preview writes nothing
    assert await _audit(db, t["id"]) == []

    done = await tpa.set_tenant_plan_by_email(
        db, "  Grant@Example.COM ", "playtester", apply=True, actor="owner@example.com")
    assert done["applied"] is True and done["changed"] is True and done["tenant_id"] == t["id"]
    row = await db_module.get_tenant_by_id(db, t["id"])
    assert row["plan"] == "playtester" and row["inactivity_expires_at"] is None
    audit = await _audit(db, t["id"])
    assert len(audit) == 1 and audit[0]["actor"] == "owner@example.com"
    detail = json.loads(audit[0]["detail"])
    assert detail["plan"] == ["free", "playtester"] and detail["via"] == "admin"
    assert detail["email"] == "grant@example.com"

    again = await tpa.set_tenant_plan_by_email(db, "grant@example.com", "playtester", apply=True)
    assert again["changed"] is False and again["applied"] is True
    assert len(await _audit(db, t["id"])) == 1  # a no-op leaves no audit row


async def test_end_date_is_set_kept_replaced_and_cleared(db):
    from meridian import tenant_plan_admin as tpa

    t = await db_module.upsert_tenant(db, "end@example.com")

    async def _set(**kw):
        return await tpa.set_tenant_plan_by_email(db, "end@example.com", "playtester", apply=True, **kw)

    r = await _set(expires_at="2099-06-30")
    assert r["expires_at"] == "2099-06-30 00:00:00"
    row = await db_module.get_tenant_by_id(db, t["id"])
    assert row["inactivity_expires_at"] == "2099-06-30 00:00:00"
    assert plans.playtester_access_expired(row) is False

    kept = await _set()  # no date given: a playtester keeps its own
    assert kept["changed"] is False and await _flag(db, t["id"], "inactivity_expires_at") == "2099-06-30 00:00:00"

    r = await _set(expires_at="2099-07-01T12:30:00")
    assert r["changed"] is True and await _flag(db, t["id"], "inactivity_expires_at") == "2099-07-01 12:30:00"

    r = await _set(expires_at=None)  # explicit null: no end date
    assert r["changed"] is True and await _flag(db, t["id"], "inactivity_expires_at") is None

    for bad, status in (("2000-01-01", 400), ("not a date", 400), ("", 400)):
        with pytest.raises(tpa.PlanChangeError) as err:
            await _set(expires_at=bad)
        assert err.value.status == status
    assert await _flag(db, t["id"], "inactivity_expires_at") is None  # refusals changed nothing


async def test_granting_never_inherits_a_trial_date_and_revoking_clears_the_end_date(db):
    from meridian import tenant_plan_admin as tpa

    t = await db_module.upsert_tenant(db, "trialdate@example.com")
    await db_module.update_tenant(db, t["id"], inactivity_expires_at=PAST)  # a lapsed free trial

    await tpa.set_tenant_plan_by_email(db, "trialdate@example.com", "playtester", apply=True)
    row = await db_module.get_tenant_by_id(db, t["id"])
    # the old trial date must not become a playtester end date that expires it at once
    assert row["inactivity_expires_at"] is None
    assert plans.playtester_access_expired(row) is False
    assert plans.tenant_entitlement_plan(row) == "pro"

    await tpa.set_tenant_plan_by_email(
        db, "trialdate@example.com", "playtester", expires_at="2099-01-01", apply=True)
    revoked = await tpa.set_tenant_plan_by_email(db, "trialdate@example.com", "free", apply=True)
    row = await db_module.get_tenant_by_id(db, t["id"])
    assert row["plan"] == "free" and revoked["plan_label"] == "Free Trial"
    # the playtester's end date is not left behind as the free trial's expiry
    assert row["inactivity_expires_at"] is None
    assert len(await _audit(db, t["id"])) == 3

    again = await tpa.set_tenant_plan_by_email(db, "trialdate@example.com", "free", apply=True)
    assert again["changed"] is False


async def test_a_plan_change_between_other_plans_leaves_the_trial_clock_alone(db):
    from meridian import tenant_plan_admin as tpa

    t = await db_module.upsert_tenant(db, "clock@example.com")
    await db_module.update_tenant(db, t["id"], inactivity_expires_at=FUTURE)
    r = await tpa.set_tenant_plan_by_email(db, "clock@example.com", "pro", apply=True)
    assert r["plan_label"] == "Pro" and r["expires_at"] == FUTURE
    assert await _flag(db, t["id"], "inactivity_expires_at") == FUTURE


@pytest.mark.parametrize(
    "email, plan, extra, status",
    [
        ("known@example.com", "bogus", {}, 400),
        ("known@example.com", "Playtester", {}, 400),          # exact spelling only
        ("known@example.com", "admin", {}, 400),               # never set from here
        ("known@example.com", "trial", {}, 400),
        ("known@example.com", None, {}, 400),
        ("known@example.com", "pro", {"expires_at": "2099-01-01"}, 400),  # only a playtester has an end date
        ("", "playtester", {}, 400),
        ("not-an-email", "playtester", {}, 400),
        ("nobody@example.com", "playtester", {}, 404),
        ("boss@example.com", "playtester", {}, 409),           # an operator account
        ("payer@example.com", "playtester", {}, 409),          # a live Stripe customer would keep paying
    ],
)
async def test_the_action_refuses_what_it_should_and_changes_nothing(db, email, plan, extra, status):
    from meridian import tenant_plan_admin as tpa

    known = await _make_tenant(db, "known@example.com")
    boss = await _make_tenant(db, "boss@example.com", "admin")
    payer = await _make_tenant(db, "payer@example.com", "pro", stripe_customer_id="cus_real")
    with pytest.raises(tpa.PlanChangeError) as err:
        await tpa.set_tenant_plan_by_email(db, email, plan, apply=True, **extra)
    assert err.value.status == status and err.value.message
    for t, plan_now in ((known, "free"), (boss, "admin"), (payer, "pro")):
        assert await _flag(db, t["id"], "plan") == plan_now
    assert [e for t in (known, boss, payer) for e in await _audit(db, t["id"])] == []


async def test_a_paying_customer_can_still_be_moved_to_free_but_a_playtester_has_none(db):
    """The Stripe refusal is for GRANTING the unbilled plan, not for any change."""
    from meridian import tenant_plan_admin as tpa

    t = await _make_tenant(db, "payer2@example.com", "pro", stripe_customer_id="cus_real")
    r = await tpa.set_tenant_plan_by_email(db, "payer2@example.com", "standard", apply=True)
    assert r["changed"] is True and await _flag(db, t["id"], "plan") == "standard"


async def test_clearing_the_staff_flag_is_explicit_and_only_ever_clears(db):
    from meridian import tenant_plan_admin as tpa

    t = await _make_tenant(db, "staffer@example.com", "pro")
    await db.execute("UPDATE tenants SET is_internal = 1 WHERE id = ?", (t["id"],))
    await db.commit()

    kept = await tpa.set_tenant_plan_by_email(db, "staffer@example.com", "playtester", apply=True)
    assert kept["is_internal"] is True and kept["is_internal_before"] is True
    assert any("is_internal" in w and "no ceiling" in w for w in kept["warnings"])
    assert await _flag(db, t["id"], "is_internal") == 1  # not touched unless asked

    cleared = await tpa.set_tenant_plan_by_email(
        db, "staffer@example.com", "playtester", clear_internal=True, apply=True)
    assert cleared["changed"] is True and cleared["is_internal"] is False
    assert not any("is_internal" in w for w in cleared["warnings"])
    assert await _flag(db, t["id"], "is_internal") == 0
    audits = [json.loads(a["detail"]) for a in await _audit(db, t["id"])]
    assert any(a["is_internal"] == [True, False] for a in audits)  # the audit says what changed

    noop = await tpa.set_tenant_plan_by_email(
        db, "staffer@example.com", "playtester", clear_internal=True, apply=True)
    assert noop["changed"] is False
    assert len(await _audit(db, t["id"])) == 2


@pytest.mark.parametrize(
    "mate_plan, mate_internal",
    [("free", False), ("standard", False), ("pro", False), ("pro", True)],
)
async def test_grant_is_refused_for_a_tenant_on_a_shared_project_and_changes_nothing(
    db, mate_plan, mate_internal
):
    """Whatever the neighbour is (a free trial, a payer, staff), Neon reports the
    project's usage as one figure, so the playtester plan needs a project of its
    own. The refusal also answers a preview, so nothing is promised first."""
    from meridian import tenant_plan_admin as tpa

    target = await _make_tenant(db, "sh-target@example.com", "free", neon_project_id="np-sh")
    mate = await _make_tenant(db, "sh-mate@example.com", mate_plan, neon_project_id="np-sh")
    if mate_internal:
        await db.execute("UPDATE tenants SET is_internal = 1 WHERE id = ?", (mate["id"],))
        await db.commit()

    for apply in (False, True):
        with pytest.raises(tpa.PlanChangeError) as err:
            await tpa.set_tenant_plan_by_email(
                db, "sh-target@example.com", "playtester", expires_at="2099-01-01", apply=apply)
        assert err.value.status == 409
        message = err.value.message
        assert "np-sh" in message and "1 other tenant" in message and "dedicated project" in message
        assert f"/admin/tenants/{target['id']}/reset-provisioning" in message
    row = await db_module.get_tenant_by_id(db, target["id"])
    assert row["plan"] == "free" and row["inactivity_expires_at"] is None
    assert await _audit(db, target["id"]) == []


async def test_grant_passes_for_a_tenant_alone_on_its_project_or_without_a_database(db):
    from meridian import tenant_plan_admin as tpa

    await _make_tenant(db, "al-target@example.com", "free", neon_project_id="np-al")
    await _make_tenant(db, "al-elsewhere@example.com", "pro", neon_project_id="np-al-other")
    alone = await tpa.set_tenant_plan_by_email(db, "al-target@example.com", "playtester", apply=True)
    assert alone["applied"] is True and alone["changed"] is True and alone["warnings"] == []
    assert "billed_pool_neighbours" not in alone
    again = await tpa.set_tenant_plan_by_email(db, "al-target@example.com", "playtester", apply=True)
    assert again["changed"] is False  # idempotent, and still allowed

    await _make_tenant(db, "al-nodb@example.com", "free")
    nodb = await tpa.set_tenant_plan_by_email(db, "al-nodb@example.com", "playtester")
    assert any("no database" in w and "of its own" in w for w in nodb["warnings"])


async def test_a_playtester_already_on_a_shared_project_is_refused_again_but_can_be_revoked(db):
    from meridian import tenant_plan_admin as tpa

    pt = await _legacy_playtester(db, "lg-sh-pt@example.com", "np-lg", stripe=False)
    await _make_tenant(db, "lg-sh-mate@example.com", "free", neon_project_id="np-lg")

    with pytest.raises(tpa.PlanChangeError) as err:  # even though the plan itself would not change
        await tpa.set_tenant_plan_by_email(
            db, "lg-sh-pt@example.com", "playtester", expires_at="2099-01-01", apply=True)
    assert err.value.status == 409
    assert await _flag(db, pt["id"], "inactivity_expires_at") is None

    revoked = await tpa.set_tenant_plan_by_email(db, "lg-sh-pt@example.com", "free", apply=True)
    assert revoked["changed"] is True and await _flag(db, pt["id"], "plan") == "free"
    assert len(await _audit(db, pt["id"])) == 1


async def test_the_refusal_is_cleared_by_resetting_provisioning_as_its_message_says(db, calls):
    from meridian import hosted
    from meridian import tenant_plan_admin as tpa

    target = await _make_tenant(db, "mv-target@example.com", "free", neon_project_id="np-mv")
    await _make_tenant(db, "mv-mate@example.com", "free", neon_project_id="np-mv")
    with pytest.raises(tpa.PlanChangeError):
        await tpa.set_tenant_plan_by_email(db, "mv-target@example.com", "playtester", apply=True)

    summary = await hosted.reset_tenant_provisioning(db, target["id"])
    assert summary["dropped_neon_project_id"] == "np-mv" and calls.dropped == [target["id"]]

    granted = await tpa.set_tenant_plan_by_email(db, "mv-target@example.com", "playtester", apply=True)
    assert granted["applied"] is True and await _flag(db, target["id"], "plan") == "playtester"
    assert any("no database" in w for w in granted["warnings"])


async def test_a_pool_mate_arriving_between_the_check_and_the_write_is_still_refused(db, monkeypatch):
    from meridian import tenant_plan_admin as tpa

    target = await _make_tenant(db, "race-target@example.com", "free", neon_project_id="np-race")
    answers = iter([0])  # the action's own check sees nobody ...

    async def _mates(_db, _tenant_id, _pool):
        return next(answers, 1)  # ... the write's check sees the newcomer

    monkeypatch.setattr(db_module, "count_pool_mates", _mates)
    with pytest.raises(tpa.PlanChangeError) as err:
        await tpa.set_tenant_plan_by_email(db, "race-target@example.com", "playtester", apply=True)
    assert err.value.status == 409
    assert await _flag(db, target["id"], "plan") == "free"
    assert await _audit(db, target["id"]) == []


async def test_update_tenant_refuses_the_playtester_plan_on_a_shared_project(db):
    """The write path itself, not only the admin action: any caller is refused."""
    a = await _make_tenant(db, "ut-a@example.com", "free", neon_project_id="np-ut")
    await _make_tenant(db, "ut-b@example.com", "free", neon_project_id="np-ut")
    assert await db_module.count_pool_mates(db, a["id"], "np-ut") == 1  # any plan counts, not oneself
    assert await db_module.count_pool_mates(db, a["id"], "np-ut-nobody") == 0
    assert await db_module.count_pool_mates(db, a["id"], None) == 0

    with pytest.raises(plans.SharedPoolError) as err:
        await db_module.update_tenant(db, a["id"], plan="playtester")
    assert isinstance(err.value, ValueError) and err.value.mates == 1 and err.value.pool == "np-ut"
    assert await _flag(db, a["id"], "plan") == "free"

    # the very write that moves the database onto a shared project is refused too
    c = await _make_tenant(db, "ut-c@example.com", "free")
    with pytest.raises(plans.SharedPoolError):
        await db_module.update_tenant(db, c["id"], plan="playtester", neon_project_id="np-ut")
    assert await _flag(db, c["id"], "plan") == "free" and await _flag(db, c["id"], "neon_project_id") is None

    # a project of its own is fine, and so is every other plan on the shared one
    ok = await db_module.update_tenant(db, c["id"], plan="playtester", neon_project_id="np-ut-own")
    assert ok["plan"] == "playtester"
    assert (await db_module.update_tenant(db, a["id"], plan="pro"))["plan"] == "pro"

    # the usage jobs write other columns of a playtester that shares a project
    legacy = await _legacy_playtester(db, "ut-legacy@example.com", "np-ut")
    await db_module.update_tenant(db, legacy["id"], compute_cu_hours_used=3.0)
    assert await _flag(db, legacy["id"], "compute_cu_hours_used") == 3.0


async def test_the_unknown_plan_guard_on_the_one_write_path_is_unchanged(db):
    t = await db_module.upsert_tenant(db, "guard@example.com")
    with pytest.raises(ValueError):
        await db_module.update_tenant(db, t["id"], plan="playtest")
    await db_module.update_tenant(db, t["id"], plan="playtester")
    assert await _flag(db, t["id"], "plan") == "playtester"


# --- the admin route ---------------------------------------------------------


def _as_admin(client, monkeypatch, email: str = "plan-admin@example.com") -> dict[str, Any]:
    monkeypatch.setenv("MERIDIAN_ADMIN_EMAILS", email)
    monkeypatch.delenv("MERIDIAN_ADMIN_PASSWORD", raising=False)
    return _seed(client, email, "free")


def test_admin_plan_route_is_not_served_when_self_hosted(client):
    r = client.post("/admin/tenants/plan", json={"email": "a@example.com", "plan": "free", "confirm": True})
    assert r.status_code == 404


def test_admin_plan_route_refuses_anonymous_and_non_admin_callers(monkeypatch, tmp_path):
    client = _hosted_client(monkeypatch, tmp_path)
    monkeypatch.delenv("MERIDIAN_ADMIN_EMAILS", raising=False)
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    body = {"email": "victim@example.com", "plan": "playtester", "confirm": True}
    with client:
        victim = _seed(client, "victim@example.com", "free")
        client.cookies.clear()
        assert client.post("/admin/tenants/plan", json=body).status_code == 401
        _seed(client, "plain-user@example.com", "free")
        assert client.post("/admin/tenants/plan", json=body).status_code == 403
        # an admin whose password cookie is missing is refused too
        monkeypatch.setenv("MERIDIAN_ADMIN_EMAILS", "plain-user@example.com")
        monkeypatch.setenv("MERIDIAN_ADMIN_PASSWORD", "hunter2-test")
        assert client.post("/admin/tenants/plan", json=body).status_code == 403
        row = asyncio.run(db_module.get_tenant_by_id(client.app.state.db, victim["id"]))
    assert row["plan"] == "free"


def test_admin_grants_and_revokes_the_playtester_plan_over_http(monkeypatch, tmp_path):
    client = _hosted_client(monkeypatch, tmp_path)
    with client:
        db = client.app.state.db
        target = _seed(client, "moved@example.com", "pro", is_internal=True)  # the staff stopgap
        _as_admin(client, monkeypatch)
        url = "/admin/tenants/plan"
        grant = {"email": "moved@example.com", "plan": "playtester", "is_internal": False}

        r = client.post(url, json=grant)  # no confirm: a preview
        assert r.status_code == 200
        body = r.json()
        assert body["applied"] is False and body["changed"] is True and body["plan_label"] == "Playtester"
        assert asyncio.run(db_module.get_tenant_by_id(db, target["id"]))["plan"] == "pro"

        r = client.post(url, json={**grant, "confirm": True})
        assert r.status_code == 200
        body = r.json()
        assert body["applied"] is True and body["plan_before"] == "pro" and body["plan"] == "playtester"
        assert body["is_internal_before"] is True and body["is_internal"] is False
        row = asyncio.run(db_module.get_tenant_by_id(db, target["id"]))
        assert row["plan"] == "playtester" and row["is_internal"] == 0
        assert row["inactivity_expires_at"] is None
        audit = asyncio.run(_audit(db, target["id"]))
        assert len(audit) == 1 and audit[0]["actor"] == "plan-admin@example.com"
        assert json.loads(audit[0]["detail"])["via"] == "admin_route"

        # idempotent, and the account now reads as a playtester everywhere
        again = client.post(url, json={**grant, "confirm": True}).json()
        assert again["changed"] is False and again["applied"] is True
        assert len(asyncio.run(_audit(db, target["id"]))) == 1

        # end date: given, kept when the key is absent, cleared by null
        r = client.post(url, json={"email": "moved@example.com", "plan": "playtester",
                                   "expires_at": "2099-12-31", "confirm": True})
        assert r.json()["expires_at"] == "2099-12-31 00:00:00"
        r = client.post(url, json={"email": "moved@example.com", "plan": "playtester", "confirm": True})
        assert r.json()["expires_at"] == "2099-12-31 00:00:00" and r.json()["changed"] is False
        r = client.post(url, json={"email": "moved@example.com", "plan": "playtester",
                                   "expires_at": None, "confirm": True})
        assert r.json()["expires_at"] is None and r.json()["changed"] is True

        r = client.post(url, json={"email": "moved@example.com", "plan": "free", "confirm": True})
        assert r.status_code == 200 and r.json()["plan_label"] == "Free Trial"
        assert asyncio.run(db_module.get_tenant_by_id(db, target["id"]))["plan"] == "free"


def test_admin_plan_route_validates_the_request(monkeypatch, tmp_path):
    client = _hosted_client(monkeypatch, tmp_path)
    with client:
        db = client.app.state.db
        _seed(client, "route-known@example.com", "free")
        payer = _seed(client, "route-payer@example.com", "pro", stripe_customer_id="cus_real")
        _as_admin(client, monkeypatch)
        url = "/admin/tenants/plan"

        def _post(**body):
            return client.post(url, json={"confirm": True, **body})

        assert _post(email="route-known@example.com", plan="bogus").status_code == 400
        assert _post(email="route-known@example.com", plan="admin").status_code == 400
        assert _post(email="nobody@example.com", plan="playtester").status_code == 404
        assert _post(email="route-payer@example.com", plan="playtester").status_code == 409
        # the staff flag can be cleared here, never set
        assert _post(email="route-known@example.com", plan="playtester", is_internal=True).status_code == 400
        assert _post(email="route-known@example.com", plan="playtester", is_internal="no").status_code == 400
        assert client.post(url, content=b"not json").status_code == 400
        assert client.post(url, json=["a list"]).status_code == 400
        assert asyncio.run(db_module.get_tenant_by_id(db, payer["id"]))["plan"] == "pro"
        known = asyncio.run(db_module.get_tenant_by_id(db, _seed_id(db, "route-known@example.com")))
        assert known["plan"] == "free" and known["is_internal"] == 0


def test_admin_plan_route_answers_409_for_a_tenant_on_a_shared_project(monkeypatch, tmp_path):
    client = _hosted_client(monkeypatch, tmp_path)
    with client:
        db = client.app.state.db
        target = _seed(client, "route-sh-target@example.com", "free", neon_project_id="np-route-sh")
        _seed(client, "route-sh-mate@example.com", "free", neon_project_id="np-route-sh")
        _as_admin(client, monkeypatch)
        url = "/admin/tenants/plan"
        body = {"email": "route-sh-target@example.com", "plan": "playtester"}

        for confirm in (False, True):  # the preview is refused as well
            r = client.post(url, json={**body, "confirm": confirm})
            assert r.status_code == 409
            detail = r.json()["detail"]
            assert "dedicated project" in detail and f"/admin/tenants/{target['id']}/reset-provisioning" in detail
        assert asyncio.run(db_module.get_tenant_by_id(db, target["id"]))["plan"] == "free"
        assert asyncio.run(_audit(db, target["id"])) == []

        # only the playtester plan is refused: any other change goes through
        r = client.post(url, json={**body, "plan": "pro", "confirm": True})
        assert r.status_code == 200 and r.json()["plan"] == "pro"


def _seed_id(db, email: str) -> str:
    async def _go():
        async with db.execute("SELECT id FROM tenants WHERE email = ?", (email,)) as cur:
            return (await cur.fetchone())["id"]

    return asyncio.run(_go())


def test_admin_waitlist_page_does_not_count_a_playtester_as_a_paid_plan(monkeypatch, tmp_path):
    import re

    client = _hosted_client(monkeypatch, tmp_path)
    with client:
        _seed(client, "count-pro@example.com", "pro")
        _seed(client, "count-std@example.com", "standard")
        _seed(client, "count-pt@example.com", "playtester")
        _as_admin(client, monkeypatch, "count-admin@example.com")
        page = client.get("/admin/waitlist")
        assert page.status_code == 200
        paid = re.search(r'<div class="n">(\d+)</div><div class="l">Paid Plan', page.text)
        total = re.search(r'<div class="n">(\d+)</div><div class="l">Total Tenants', page.text)
        assert paid and total
        assert (int(paid.group(1)), int(total.group(1))) == (2, 4)


# --- the command line --------------------------------------------------------


def _load_cli():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "set_tenant_plan.py"
    spec = importlib.util.spec_from_file_location("set_tenant_plan_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_cli_previews_by_default_and_applies_with_the_same_rules(db):
    cli = _load_cli()
    t = await db_module.upsert_tenant(db, "cli@example.com")

    def _args(*argv: str):
        return cli.build_parser().parse_args(["cli@example.com", *argv])

    preview = await cli.run(_args("playtester"), db)
    assert preview["applied"] is False and await _flag(db, t["id"], "plan") == "free"

    done = await cli.run(_args("playtester", "--expires", "2099-03-01", "--apply"), db)
    assert done["applied"] is True and done["plan_label"] == "Playtester"
    assert await _flag(db, t["id"], "inactivity_expires_at") == "2099-03-01 00:00:00"
    audit = (await _audit(db, t["id"]))[0]
    assert audit["actor"].startswith("cli:") and json.loads(audit["detail"])["via"] == "cli"

    kept = await cli.run(_args("playtester", "--apply"), db)  # no date flag: keeps it
    assert kept["changed"] is False
    cleared = await cli.run(_args("playtester", "--no-expiry", "--apply"), db)
    assert cleared["changed"] is True and await _flag(db, t["id"], "inactivity_expires_at") is None

    await db.execute("UPDATE tenants SET is_internal = 1 WHERE id = ?", (t["id"],))
    await db.commit()
    await cli.run(_args("playtester", "--clear-internal", "--apply"), db)
    assert await _flag(db, t["id"], "is_internal") == 0

    from meridian.tenant_plan_admin import PlanChangeError

    with pytest.raises(PlanChangeError):
        await cli.run(_args("admin", "--apply"), db)
    revoked = await cli.run(_args("free", "--apply"), db)
    assert revoked["plan"] == "free" and await _flag(db, t["id"], "plan") == "free"


async def test_cli_refuses_a_grant_on_a_shared_project_like_the_route(db):
    from meridian.tenant_plan_admin import PlanChangeError

    cli = _load_cli()
    target = await _make_tenant(db, "cli-sh@example.com", "free", neon_project_id="np-cli-sh")
    await _make_tenant(db, "cli-sh-mate@example.com", "free", neon_project_id="np-cli-sh")
    args = cli.build_parser().parse_args(["cli-sh@example.com", "playtester", "--apply"])
    with pytest.raises(PlanChangeError) as err:
        await cli.run(args, db)
    assert err.value.status == 409 and "dedicated project" in err.value.message
    assert await _flag(db, target["id"], "plan") == "free"


def test_cli_needs_a_database_and_rejects_conflicting_date_flags(monkeypatch, capsys):
    cli = _load_cli()
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)
    monkeypatch.delenv("MERIDIAN_AUTH_DB", raising=False)
    assert cli.main(["someone@example.com", "free"]) == 1
    assert "MERIDIAN_DB_URL" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["a@example.com", "playtester", "--expires", "2099-01-01", "--no-expiry"])
