"""Playtester plan (board item 08564061).

A playtester has Pro entitlements, no Stripe relationship, and usage that is
still bounded. This file pins:

* the helper layer in ``meridian/plans.py`` (alias, validation, optional end
  date, unknown values fail closed, every plan documented in one place);
* that every plan-dependent decision gives a playtester the same answer as a
  Pro tenant (one parametrized table of probes, plus end-to-end checks through
  the real routes and jobs where a probe would just restate the expression);
* that a playtester is never billed, dunned, churned, trial-expired or sent an
  overage invoice, while its usage ceilings still warn, throttle and alert;
* that ``is_internal`` (staff) keeps exactly the meaning it had.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import sys
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
        ("playtester", "pro", "key-pro", 604800),
        ("free", "free", "key-standard", 86400),
    ],
)
async def test_provisioning_allocates_playtester_from_the_pro_pool(
    db, monkeypatch, plan, tier, key, retention
):
    """The pool-tier CHECK constraint only knows free/standard/pro, so an
    un-mapped 'playtester' would fail at register_pool_project; it must land in
    Pro's pool, on Pro's Neon account, with Pro's retention."""
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
        self.deleted: list[str] = []

    @property
    def throttles(self) -> list[tuple]:
        """Calls that clamp a pool to the throttle ceiling (0.25 CU)."""
        return [c for c in self.cap_calls if c[1] == 0.25]

    @property
    def restores(self) -> list[tuple]:
        """Calls that lift a pool back to its normal ceiling."""
        return [c for c in self.cap_calls if c[1] != 0.25]


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

    async def _drop(tenant):
        rec.dropped.append(tenant["id"])

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
    project; by default each tenant gets a pool project of its own."""
    t = await db_module.upsert_tenant(db, email)
    return await db_module.update_tenant(
        db, t["id"], plan=plan, neon_project_id=pool or f"np-{plan}-{email.split('@')[0]}",
        stripe_customer_id="cus_stray" if stripe else None,
        compute_overage_cap_usd=500.0, storage_overage_cap_usd=500.0, **fields,
    )


_POOL_KEY = {"free": "key-standard", "standard": "key-standard", "pro": "key-pro"}


class _FakeNeon:
    """Neon as the overage jobs see it: a project only answers to the API key of
    the account that owns it, which is the pool tier it was created under (a
    free or standard pool belongs to the standard account, a pro pool to the pro
    one). Anything else is refused the way the real API refuses it: an empty
    consumption answer, a PATCH that changes nothing."""

    def __init__(self) -> None:
        self.owner_key: dict[str, str] = {}
        self.fetches: list[tuple[str, str]] = []   # (project, key) of every consumption read
        self.cap_attempts: list[tuple[str, str, float]] = []  # (project, key, max_cu)
        self.refused_caps: list[tuple[str, str, float]] = []

    def accepts(self, project: str, key: str) -> bool:
        return self.owner_key.get(project, key) == key


@pytest.fixture
def neon(calls, monkeypatch):
    from meridian import hosted

    fake = _FakeNeon()
    real_fetch, real_cap = hosted._fetch_neon_consumption, hosted._set_neon_max_cu

    async def _consumption(project, key, from_dt, to_dt):
        fake.fetches.append((project, key))
        if not fake.accepts(project, key):
            return {}
        return await real_fetch(project, key, from_dt, to_dt)

    async def _cap(project, key, max_cu):
        fake.cap_attempts.append((project, key, max_cu))
        if not fake.accepts(project, key):
            fake.refused_caps.append((project, key, max_cu))
            return
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


async def test_playtester_over_the_ceiling_is_throttled_and_alerted_never_billed(db, calls):
    from meridian import hosted

    calls.cu_hours, calls.storage_gb = 260.0, 20.0  # past Pro's 200 + 20 grace and 10 GB
    pt = await _provisioned(db, "bill-pt@example.com", "playtester")
    await hosted.run_overage_check(db)

    assert calls.meter == [] and calls.storage_reports == []
    assert calls.throttles == [(pt["neon_project_id"], 0.25)]
    subjects = [s for _e, s, _h in calls.emails]
    assert "Meridian: compute limit reached — sessions throttled" in subjects
    assert "Meridian: storage limit exceeded" in subjects
    for _e, _s, html in calls.emails:
        assert "Set an overage budget" not in html and "/GB-month" not in html
    assert len(calls.owner_alerts) == 2  # compute + storage
    row = await db_module.get_tenant_by_id(db, pt["id"])
    assert row["compute_throttled_at"]
    assert "playtester_storage_alert_month" in row["notification_prefs"]

    # A second pass neither re-throttles nor re-alerts the owner this month.
    await hosted.run_overage_check(db)
    assert len(calls.throttles) == 1 and len(calls.owner_alerts) == 2
    assert calls.meter == [] and calls.storage_reports == []


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
    assert calls.throttles == [] and calls.meter == []

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
    assert calls.throttles == []


async def test_compute_throttle_is_lifted_at_the_monthly_reset(db, calls):
    """The throttle email promises 'until next month'. The DB flag used to clear
    on the reset while Neon stayed at 0.25 CU for good."""
    from meridian import hosted

    calls.cu_hours = 260.0
    pt = await _provisioned(db, "reset-pt@example.com", "playtester")
    await hosted.run_overage_check(db)
    assert calls.throttles == [(pt["neon_project_id"], 0.25)] and calls.restores == []
    assert (await db_module.get_tenant_by_id(db, pt["id"]))["compute_throttled_at"]

    # Later in the same month: still throttled, nothing more is sent to Neon.
    await hosted.run_overage_check(db)
    assert len(calls.cap_calls) == 1

    # The month rolls over and usage starts from zero again.
    await db_module.update_tenant(db, pt["id"], overage_reset_at="2000-01-15T00:00:00+00:00")
    calls.cu_hours = 1.0
    await hosted.run_overage_check(db)
    assert (await db_module.get_tenant_by_id(db, pt["id"]))["compute_throttled_at"] is None
    # A playtester lives in a pro-tier pool, so that is the ceiling it goes back to.
    assert calls.restores == [(pt["neon_project_id"], 4.0)]

    # Nothing is throttled or restored again afterwards.
    await hosted.run_overage_check(db)
    assert len(calls.throttles) == 1 and len(calls.restores) == 1


async def test_unthrottled_tenants_are_not_restored_at_the_monthly_reset(db, calls):
    from meridian import hosted

    calls.cu_hours = 1.0
    await _provisioned(db, "norestore-pt@example.com", "playtester",
                       overage_reset_at="2000-01-15T00:00:00+00:00")
    await hosted.run_overage_check(db)
    assert calls.cap_calls == []


@pytest.mark.parametrize(
    "plan, pool_tier, expected",
    [
        ("pro", None, 4.0),         # no pool row: the plan's own tier
        ("standard", None, 2.0),
        ("pro", "standard", 2.0),   # upgraded after provisioning: the pool it lives in wins
        ("standard", "pro", 4.0),
    ],
)
async def test_throttle_is_lifted_to_the_pools_own_ceiling(db, calls, plan, pool_tier, expected):
    from meridian import hosted

    pool = f"np-restore-{plan}-{pool_tier}"
    if pool_tier:
        await db_module.register_pool_project(db, pool, pool_tier)
    calls.cu_hours = 1.0
    await _provisioned(
        db, f"restore-{plan}-{pool_tier}@example.com", plan, pool=pool,
        compute_throttled_at="2026-01-01T00:00:00+00:00",
        overage_reset_at="2000-01-15T00:00:00+00:00",
    )
    await hosted.run_overage_check(db)
    assert calls.restores == [(pool, expected)]
    assert hosted._pool_max_cu("pro") == 4.0 and hosted._pool_max_cu("standard") == 2.0


async def test_playtester_never_throttles_a_pool_that_paying_customers_share(db, calls):
    """Consumption and the cap are per Neon pool project (up to 8 tenants), so a
    throttle for the playtester would land on the paying Pro tenant next to it,
    who has a budget precisely so that it is billed instead."""
    from meridian import hosted

    calls.cu_hours = 260.0
    pt = await _provisioned(db, "shared-pt@example.com", "playtester", pool="np-shared")
    await _provisioned(db, "shared-pro@example.com", "pro", pool="np-shared")
    await hosted.run_overage_check(db)

    assert calls.cap_calls == []  # the pool is left alone
    assert len(calls.meter) == 1  # the paying tenant is metered, as always
    assert [s for e, s, _h in calls.emails if e == "shared-pt@example.com"] == []
    assert [s for s, _h in calls.owner_alerts] == [
        "[Meridian] Playtester over compute limit: shared-pt@example.com"
    ]
    assert "NOT throttled" in calls.owner_alerts[0][1]
    row = await db_module.get_tenant_by_id(db, pt["id"])
    assert row["compute_throttled_at"] is None
    assert "playtester_compute_alert_month" in row["notification_prefs"]

    # Re-checked every day, but the owner hears about it once a month.
    await hosted.run_overage_check(db)
    assert len(calls.owner_alerts) == 1 and calls.cap_calls == []


@pytest.mark.parametrize("mate_plan, mate_internal", [("playtester", False), ("pro", True)])
async def test_playtester_is_throttled_when_no_paying_customer_shares_its_pool(
    db, calls, mate_plan, mate_internal
):
    """Other playtesters and staff are not customers anyone is billing."""
    from meridian import hosted

    calls.cu_hours = 260.0
    await _provisioned(db, "alone-pt@example.com", "playtester", pool="np-alone")
    mate = await _provisioned(db, "mate@example.com", mate_plan, pool="np-alone")
    if mate_internal:
        await db.execute("UPDATE tenants SET is_internal = 1 WHERE id = ?", (mate["id"],))
        await db.commit()
    await hosted.run_overage_check(db)
    assert calls.throttles and {p for p, _ in calls.throttles} == {"np-alone"}
    assert calls.meter == []


async def test_a_paying_customer_in_another_pool_does_not_stop_the_throttle(db, calls):
    """Only a paying customer in the playtester's OWN pool project can be hurt by
    the throttle. Without the pool match, any paying customer anywhere would
    switch the throttle off for every playtester."""
    from meridian import hosted

    calls.cu_hours = 260.0
    await _provisioned(db, "far-pro@example.com", "pro", pool="np-far")
    await _provisioned(db, "own-pt@example.com", "playtester", pool="np-own")
    await hosted.run_overage_check(db)

    assert calls.throttles == [("np-own", 0.25)]  # the playtester's pool, and only it
    assert [e for e, s, _h in calls.emails if "throttled" in s] == ["own-pt@example.com"]
    assert len(calls.meter) == 1  # the paying customer is billed, as always
    assert [s for s, _h in calls.owner_alerts] == [
        "[Meridian] Playtester over compute limit: own-pt@example.com"
    ]
    assert "NOT throttled" not in calls.owner_alerts[0][1]


def test_billed_neighbour_check_is_scoped_to_the_pool_and_to_who_is_billed():
    from meridian import hosted

    me = {"id": "pt", "neon_project_id": "np-1", "plan": "playtester"}

    def other(**kw):
        return {"id": "o", "neon_project_id": "np-1", "plan": "pro", **kw}

    assert hosted._pool_has_billed_neighbours([me, other()], me) is True
    assert hosted._pool_has_billed_neighbours([me, other(plan="standard")], me) is True
    # a paying customer somewhere else is not a neighbour
    assert hosted._pool_has_billed_neighbours([me, other(neon_project_id="np-2")], me) is False
    # staff and other playtesters are not customers anyone is billing
    assert hosted._pool_has_billed_neighbours([me, other(is_internal=1)], me) is False
    assert hosted._pool_has_billed_neighbours([me, other(plan="playtester")], me) is False
    # the tenant is not its own neighbour
    assert hosted._pool_has_billed_neighbours([{**me, "plan": "pro"}], {**me, "plan": "pro"}) is False


@pytest.mark.parametrize(
    "plan, pool_tier",
    [
        ("playtester", "standard"),  # signed up as free/standard, flipped afterwards
        ("playtester", "free"),
        ("playtester", "pro"),
        ("pro", "standard"),         # same mismatch for an ordinary upgrade
        ("standard", "pro"),         # and for a downgrade
        ("free", "pro"),
    ],
)
async def test_overage_job_polls_the_account_that_owns_the_database(
    db, calls, neon, plan, pool_tier
):
    """The Neon key follows the pool project the database lives in, not the plan
    the tenant is on today. Polling a standard-account project with the Pro key
    is refused by Neon, and the job used to move on without a trace."""
    from meridian import hosted

    pool = await _neon_pool(db, neon, f"np-key-{plan}-{pool_tier}", pool_tier)
    calls.cu_hours = 1.0
    await _provisioned(db, f"key-{plan}-{pool_tier}@example.com", plan, pool=pool)
    await hosted.run_overage_check(db)
    assert neon.fetches == [(pool, _POOL_KEY[pool_tier])]
    # and the usage really was read: it was persisted on the tenant
    row = (await db_module.list_tenants_with_neon(db))[0]
    assert row["compute_cu_hours_used"] == 1.0


async def test_playtester_flipped_from_a_standard_pool_is_still_throttled(db, calls, neon):
    """The way a playtester is normally made: the person signs in (a free tenant,
    database in the standard account), then the plan is changed. The ceiling must
    still bite: usage is read and the cap lands, both with the standard key."""
    from meridian import hosted

    pool = await _neon_pool(db, neon, "np-flipped", "standard")
    calls.cu_hours = 260.0
    pt = await _provisioned(db, "flipped-pt@example.com", "playtester", pool=pool)
    await hosted.run_overage_check(db)

    assert neon.fetches == [(pool, "key-standard")]
    assert neon.cap_attempts == [(pool, "key-standard", 0.25)] and neon.refused_caps == []
    assert calls.throttles == [(pool, 0.25)] and calls.meter == []
    assert [s for _e, s, _h in calls.emails] == ["Meridian: compute limit reached — sessions throttled"]
    assert (await db_module.get_tenant_by_id(db, pt["id"]))["compute_throttled_at"]


async def test_throttle_is_lifted_with_the_key_of_the_pool_it_was_applied_to(db, calls, neon):
    from meridian import hosted

    pool = await _neon_pool(db, neon, "np-lift", "standard")
    calls.cu_hours = 1.0
    await _provisioned(
        db, "lift-pt@example.com", "playtester", pool=pool,
        compute_throttled_at="2026-01-01T00:00:00+00:00",
        overage_reset_at="2000-01-15T00:00:00+00:00",
    )
    await hosted.run_overage_check(db)
    # a refused lift leaves Neon throttled while the DB flag says otherwise
    assert neon.cap_attempts == [(pool, "key-standard", 2.0)] and neon.refused_caps == []
    assert calls.restores == [(pool, 2.0)]


async def test_database_outside_the_pool_registry_falls_back_to_the_plans_key(db, calls, neon):
    """A manually assigned database has no neon_pool_projects row: the plan's own
    account is the best remaining guess, as before."""
    from meridian import hosted

    calls.cu_hours = 1.0
    await _provisioned(db, "manual-pt@example.com", "playtester", pool="np-manual-pt")
    await _provisioned(db, "manual-std@example.com", "standard", pool="np-manual-std")
    await hosted.run_overage_check(db)
    assert sorted(neon.fetches) == [("np-manual-pt", "key-pro"), ("np-manual-std", "key-standard")]


async def test_storage_job_polls_the_account_that_owns_the_database(db, calls, neon, monkeypatch):
    from meridian import hosted

    reads: list[tuple[str, str]] = []

    async def _gb(project, key):
        reads.append((project, key))
        return 0.0

    monkeypatch.setattr(hosted, "get_neon_storage_gb", _gb)
    for tier in ("standard", "pro"):
        await _neon_pool(db, neon, f"np-store-{tier}", tier)
        await _provisioned(db, f"store-{tier}-pt@example.com", "playtester", pool=f"np-store-{tier}")
    await hosted.run_storage_overage_check(db)
    assert sorted(reads) == [("np-store-pro", "key-pro"), ("np-store-standard", "key-standard")]


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


async def test_a_refused_compute_cap_is_logged_not_silent(monkeypatch, caplog):
    """The real cap helper never raises; a 4xx used to vanish, leaving the tenant
    row saying 'throttled' (or 'restored') while Neon kept the old ceiling."""
    from meridian import hosted

    class _Http:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def patch(self, *_a, **_k):
            return types.SimpleNamespace(status_code=403)

    monkeypatch.setattr("httpx.AsyncClient", _Http)
    with caplog.at_level(logging.WARNING, logger="meridian.hosted"):
        await hosted._set_neon_max_cu("np-refused", "secret-key", 0.25)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "np-refused" in logged and "403" in logged and "secret-key" not in logged


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


async def test_owner_alerts_for_compute_and_storage_do_not_erase_each_other(db, calls):
    """Both alerts are de-duplicated through the same notification_prefs blob; a
    stale copy written back by the second one used to drop the first's flag, so
    the next day's run alerted again."""
    from meridian import hosted

    calls.cu_hours, calls.storage_gb = 260.0, 20.0
    pt = await _provisioned(db, "both-pt@example.com", "playtester", pool="np-both")
    await _provisioned(db, "both-pro@example.com", "pro", pool="np-both")
    await hosted.run_overage_check(db)
    assert len(calls.owner_alerts) == 2
    prefs = (await db_module.get_tenant_by_id(db, pt["id"]))["notification_prefs"]
    assert "playtester_compute_alert_month" in prefs and "playtester_storage_alert_month" in prefs

    await hosted.run_overage_check(db)
    assert len(calls.owner_alerts) == 2


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
    # the usage card's fixed-limits note must stay true where the pool is shared
    # and the server only alerts the owner instead of throttling
    assert "usage past the grace allowance is restricted" in src
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
        "usage past the grace allowance is restricted",
        "me.entitlement_plan || me.plan",                # tunnel indicator follows entitlement
    ):
        assert marker in bundle, f"{stale} ({marker!r} missing)"
    assert bundle.count('playtester: "#0891b2"') == 2, stale  # two badge colour maps
    manifest = json.loads((static / "asset-manifest.json").read_text(encoding="utf-8"))
    assert manifest["bundle_hash"] == hashlib.sha256(raw).hexdigest()[:12], stale
