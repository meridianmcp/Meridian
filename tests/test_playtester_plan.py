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
    from meridian import hosted
    ent = plans.tenant_entitlement_plan(_t(plan), default="standard")
    return hosted.PLAN_LIMITS.get(ent, hosted.PLAN_LIMITS["free"])


def _probe_direct_limits_row(plan):
    from meridian import hosted
    return hosted.PLAN_LIMITS.get(plan, hosted.PLAN_LIMITS["free"])


def _probe_storage_ceiling(plan):
    from meridian import hosted
    ent = plans.tenant_entitlement_plan(_t(plan), default="standard")
    return hosted._PLAN_STORAGE_LIMIT_GB.get(ent, 1.0)


def _probe_member_limit(plan):
    import meridian.server as srv
    ent = plans.tenant_entitlement_plan(_t(plan), default="standard")
    return srv._WORKSPACE_MEMBER_LIMITS.get(ent, 25)


def _probe_pool_tier(plan):
    # The tier provision_neon_db allocates from (see the end-to-end test below).
    return plans.tenant_entitlement_plan(_t(plan), default="standard")


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
    client = _hosted_client(monkeypatch, tmp_path)
    with client:
        # Staff on a pro plan with a stale end date: never shown as expired.
        _seed(client, "staff@example.com", "pro", is_internal=True, inactivity_expires_at=PAST)
        me = client.get("/me").json()
        assert me["is_internal"] is True
        assert me["expired"] is False and me["days_remaining"] is None
        assert me["plan"] == "pro" and me["is_playtester"] is False
        # Staff on a free plan: still the free entitlement (is_internal is a
        # separate flag, honoured by the tunnel gate only).
        _seed(client, "staff-free@example.com", "free", is_internal=True)
        me = client.get("/me").json()
        assert me["entitlement_plan"] == "free" and me["is_internal"] is True


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
    import meridian.server as srv

    client = _hosted_client(monkeypatch, tmp_path)
    with client:
        monkeypatch.setitem(srv._WORKSPACE_MEMBER_LIMITS, "standard", 1)
        monkeypatch.setitem(srv._WORKSPACE_MEMBER_LIMITS, "pro", 5)

        async def _two_members(_db, _tenant_id):
            return 2

        monkeypatch.setattr(db_module, "count_workspace_members", _two_members)
        for i, (plan, status) in enumerate(
            (("standard", 402), ("pro", 201), ("playtester", 201))
        ):
            _seed(client, f"ws-{i}@example.com", plan)
            r = client.post("/workspace/invite", json={"email": f"invitee-{i}@example.com"})
            assert r.status_code == status, (plan, r.text)


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
        self.throttles: list[tuple] = []
        self.emails: list[tuple[str, str, str]] = []
        self.owner_alerts: list[tuple[str, str]] = []
        self.cancelled: list[str] = []
        self.dropped: list[str] = []
        self.deleted: list[str] = []


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
        rec.throttles.append((project_id, max_cu))

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
    monkeypatch.setattr(hosted, "_send_owner_alert", _owner)
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


async def _provisioned(db, email: str, plan: str, *, stripe: bool = True, **fields: Any) -> dict[str, Any]:
    t = await db_module.upsert_tenant(db, email)
    return await db_module.update_tenant(
        db, t["id"], plan=plan, neon_project_id=f"np-{plan}-{email.split('@')[0]}",
        stripe_customer_id="cus_stray" if stripe else None,
        compute_overage_cap_usd=500.0, storage_overage_cap_usd=500.0, **fields,
    )


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
    from meridian.routes import tunnel as tn

    assert tn._is_tunnel_allowed({"plan": "free", "is_internal": True}) is True
    assert tn._is_tunnel_allowed({"plan": "free", "is_internal": False}) is False
    assert tn._is_tunnel_allowed({"plan": "playtester", "is_internal": False}) is True
    assert tn._is_tunnel_allowed({"plan": "playtest", "is_internal": True}) is True
    assert tn._is_tunnel_allowed({"plan": "playtest"}) is False


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
