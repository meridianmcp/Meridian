"""Tests for sprint item 819ac6de (MDE-1) — repair capability manifests and
fail-closed handoff executability.

Scope: before this fix, ``handoff.build_effective_capability_contract`` (the
one wrapper both trusted handoff channels -- ``start_session``'s orientation
and every ``generate_handoff`` mode -- call to emit the machine-readable
capability contract) never supplied ``capability_contract.build_capability_
contract`` with a real ``availability_checker``. The real ac80aaaf
availability-evaluation machinery (``meridian.capability_availability`` +
``mcp/handlers/project_tools.py``'s ``_build_live_inventory``/
``check_capability_availability``) had already landed and was fully tested
in isolation (see tests/test_capability_availability.py), but
``capability_contract._resolve_availability``'s own guessed sibling-module
auto-discovery (looking for a ``check_availability`` function that was never
actually defined) could never find it -- so ``executable``/
``executable_reasons`` in a REAL start_session/generate_handoff response
could structurally never reflect a truly unavailable ``required``
capability; it was permanently stuck reporting ``"unknown"`` availability
and, consequently, ``executable=True`` regardless of real tool state.

Covers (this file is new -- no prior test_handoff.py existed):

1. ``handoff._summarize_capability_availability`` -- pure, deterministic
   bucketing of ``evaluate_manifest_availability`` verdicts into the
   ``{available, missing, degraded}`` shape
   ``capability_contract``'s ``AvailabilityChecker`` contract expects,
   fail-closed for unresolved (``unknown``) status.
2. ``handoff._availability_checker_from_live_inventory`` -- the checker
   factory closed over an already-fetched live-inventory snapshot.
3. ``handoff.build_effective_capability_contract`` end-to-end with
   ``live_inventory`` / ``tenant`` -- missing-tool (fail-closed for
   required), fallback-chain rescue, degraded_ok/optional non-blocking
   paths, recovery (tool comes back -> executable flips back True), and
   backward compatibility (no tenant/live_inventory, and an empty legacy
   manifest) with zero behavior change for existing callers.
"""
from __future__ import annotations

from meridian import capability_availability as ca
from meridian import db as db_module
from meridian import handoff as handoff_module


def _valid_capability(**overrides):
    base = {
        "id": "code-search",
        "purpose": "find symbols/functions/classes",
        "required_tools": ["codebase__find_symbol"],
    }
    base.update(overrides)
    return base


def _inventory(**overrides):
    base = {
        "tunnel_reachable": True,
        "builtin_tools": {"start_session", "log_task"},
        "plugins": {
            "codebase": {"enabled": True, "invocable": True, "tools": {"find_symbol", "search_graph"}},
            "filesystem": {"enabled": True, "invocable": True, "tools": {"read_file", "write_file"}},
        },
        "stdio_registry": {},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _summarize_capability_availability — pure bucketing, fail-closed for
# unresolved status.
# ---------------------------------------------------------------------------

def test_summarize_availability_buckets_by_status():
    evaluated = [
        {"capability_id": "avail", "status": ca.STATUS_AVAILABLE, "availability_policy": "required"},
        {"capability_id": "miss", "status": ca.STATUS_MISSING, "availability_policy": "required"},
        {"capability_id": "degr", "status": ca.STATUS_DEGRADED, "availability_policy": "required"},
    ]
    result = handoff_module._summarize_capability_availability(evaluated)
    assert result == {"available": ["avail"], "missing": ["miss"], "degraded": ["degr"]}


def test_summarize_availability_unknown_status_fails_closed_to_missing_for_required_and_optional():
    evaluated = [
        {"capability_id": "req-unknown", "status": ca.STATUS_UNKNOWN, "availability_policy": "required"},
        {"capability_id": "opt-unknown", "status": ca.STATUS_UNKNOWN, "availability_policy": "optional"},
    ]
    result = handoff_module._summarize_capability_availability(evaluated)
    assert result["missing"] == ["opt-unknown", "req-unknown"]
    assert result["available"] == []
    assert result["degraded"] == []


def test_summarize_availability_unknown_status_degrades_not_missing_for_degraded_ok():
    evaluated = [
        {"capability_id": "dok-unknown", "status": ca.STATUS_UNKNOWN, "availability_policy": "degraded_ok"},
    ]
    result = handoff_module._summarize_capability_availability(evaluated)
    assert result["degraded"] == ["dok-unknown"]
    assert result["missing"] == []


def test_summarize_availability_deterministic_sorted_regardless_of_input_order():
    forward = [
        {"capability_id": "zebra", "status": ca.STATUS_MISSING, "availability_policy": "required"},
        {"capability_id": "alpha", "status": ca.STATUS_MISSING, "availability_policy": "required"},
    ]
    reversed_ = list(reversed(forward))
    assert (
        handoff_module._summarize_capability_availability(forward)
        == handoff_module._summarize_capability_availability(reversed_)
        == {"available": [], "missing": ["alpha", "zebra"], "degraded": []}
    )


def test_summarize_availability_ignores_malformed_entries():
    evaluated = ["not-a-dict", {"status": ca.STATUS_AVAILABLE}, {"capability_id": "ok", "status": ca.STATUS_AVAILABLE}]
    result = handoff_module._summarize_capability_availability(evaluated)
    assert result == {"available": ["ok"], "missing": [], "degraded": []}


def test_summarize_availability_empty_input():
    assert handoff_module._summarize_capability_availability([]) == {
        "available": [], "missing": [], "degraded": [],
    }


# ---------------------------------------------------------------------------
# _availability_checker_from_live_inventory — factory + real
# evaluate_manifest_availability wiring.
# ---------------------------------------------------------------------------

def test_checker_factory_evaluates_whatever_capabilities_it_is_called_with():
    """The checker must evaluate the EFFECTIVE (post-profile-merge) list it
    is actually called with, not a value baked in at factory-build time --
    otherwise a profile-layer-only capability would silently never be
    evaluated for availability."""
    inv = _inventory(tunnel_reachable=False)
    checker = handoff_module._availability_checker_from_live_inventory(inv)

    result_a = checker([_valid_capability(id="a")])
    result_b = checker([_valid_capability(id="a"), _valid_capability(id="b", required_tools=["start_session"])])
    assert result_a == {"available": [], "missing": ["a"], "degraded": []}
    # "b" uses a builtin tool -> always available, tunnel-independent.
    assert result_b == {"available": ["b"], "missing": ["a"], "degraded": []}


# ---------------------------------------------------------------------------
# build_effective_capability_contract — end-to-end with live_inventory.
# ---------------------------------------------------------------------------

async def test_missing_required_tool_fails_closed_via_live_inventory(db):
    project = await db_module.create_project(db, "mde1-missing-required")
    await db_module.set_project_capability_manifest(
        db, project["id"],
        [_valid_capability(id="alpha", required_tools=["ghost_plugin__ghost_tool"], availability_policy="required")],
    )
    contract = await handoff_module.build_effective_capability_contract(
        db, project["id"], live_inventory=_inventory(),
    )
    assert contract is not None
    assert contract["availability"]["status"] == "checked"
    assert contract["availability"]["missing"] == ["alpha"]
    assert contract["executable"] is False
    assert any("missing_required_capabilities" in r for r in contract["executable_reasons"])
    assert "alpha" in contract["executable_reasons"][0]


async def test_fallback_chain_rescues_required_capability_and_stays_executable(db):
    project = await db_module.create_project(db, "mde1-fallback-rescue")
    await db_module.set_project_capability_manifest(
        db, project["id"],
        [_valid_capability(
            id="alpha",
            required_tools=["ghost_plugin__ghost_tool"],
            fallback_chain=["codebase__find_symbol"],
            availability_policy="required",
        )],
    )
    contract = await handoff_module.build_effective_capability_contract(
        db, project["id"], live_inventory=_inventory(),
    )
    assert contract["availability"]["missing"] == []
    assert contract["availability"]["degraded"] == ["alpha"]
    # A rescued fallback is NOT "missing" -- the capability is usable, just
    # not on its primary tool.
    assert contract["executable"] is True
    assert contract["executable_reasons"] == []


async def test_degraded_ok_policy_never_blocks_executability(db):
    project = await db_module.create_project(db, "mde1-degraded-ok")
    await db_module.set_project_capability_manifest(
        db, project["id"],
        [_valid_capability(id="alpha", required_tools=["ghost_plugin__ghost_tool"], availability_policy="degraded_ok")],
    )
    contract = await handoff_module.build_effective_capability_contract(
        db, project["id"], live_inventory=_inventory(),
    )
    assert contract["availability"]["degraded"] == ["alpha"]
    assert contract["executable"] is True
    assert contract["executable_reasons"] == []


async def test_optional_policy_missing_tool_stays_executable(db):
    project = await db_module.create_project(db, "mde1-optional")
    await db_module.set_project_capability_manifest(
        db, project["id"],
        [_valid_capability(id="alpha", required_tools=["ghost_plugin__ghost_tool"], availability_policy="optional")],
    )
    contract = await handoff_module.build_effective_capability_contract(
        db, project["id"], live_inventory=_inventory(),
    )
    assert contract["availability"]["missing"] == ["alpha"]
    assert contract["executable"] is True
    assert contract["executable_reasons"] == []


async def test_recovery_tool_coming_back_flips_executable_true_again(db):
    """A capability that is genuinely missing now and genuinely available
    later (the tool/plugin came back) must have executable flip back to
    True and the contract_hash change -- this is what makes 'recovery'
    observable to a receiving executor, not just the initial failure."""
    project = await db_module.create_project(db, "mde1-recovery")
    await db_module.set_project_capability_manifest(
        db, project["id"],
        [_valid_capability(id="alpha", required_tools=["codebase__find_symbol"], availability_policy="required")],
    )

    down = await handoff_module.build_effective_capability_contract(
        db, project["id"], live_inventory=_inventory(tunnel_reachable=False),
    )
    assert down["executable"] is False
    assert down["availability"]["missing"] == ["alpha"]

    up = await handoff_module.build_effective_capability_contract(
        db, project["id"], live_inventory=_inventory(tunnel_reachable=True),
    )
    assert up["executable"] is True
    assert up["availability"]["available"] == ["alpha"]
    assert down["contract_hash"] != up["contract_hash"]


async def test_empty_legacy_manifest_stays_backward_compatible_with_live_inventory(db):
    """An empty/legacy manifest must never be reported as having an
    unavailable capability -- there is nothing declared to check, so all
    four buckets are empty lists (not 'unknown' strings, since a real
    checker DID run) and executable stays True. ``unverified`` (added by
    capability_contract's separately-landed fail-closed fix, see the
    ``no_tenant_or_live_inventory``/``live_inventory_probe_failure`` tests
    above) is additive to this dict shape, not a behavior change for the
    empty-manifest case: there are no capability ids to leave unverified."""
    project = await db_module.create_project(db, "mde1-empty-legacy")
    contract = await handoff_module.build_effective_capability_contract(
        db, project["id"], live_inventory=_inventory(),
    )
    assert contract["requested"]["capabilities"] == []
    assert contract["availability"]["status"] == "checked"
    assert contract["availability"] == {
        "status": "checked", "available": [], "missing": [], "degraded": [], "unverified": [],
    }
    assert contract["executable"] is True
    assert contract["executable_reasons"] == []


async def test_no_tenant_or_live_inventory_keeps_prior_unknown_degrade(db):
    """Every EXISTING caller that passes neither argument (all four
    production call sites, unmodified by this fix) still degrades the
    ``availability`` dict to the SAME ``"unknown"``/``"unknown"`` shape as
    before this change existed -- this fix adds no new arguments-required
    behavior for those callers.

    ``executable`` itself, however, is intentionally NOT byte-identical to
    the pre-fail-closed world: the separately-landed capability_contract
    fail-closed fix (unverified-required-capability handling, see
    tests/test_capability_contract.py's
    ``test_contract_unknown_availability_fails_closed_for_required``) means
    a ``required`` capability whose availability could not be verified --
    whether because no checker ran at all (this test) or because a real
    checker ran and returned ``unknown`` -- now correctly blocks
    executability rather than silently defaulting to ``True``. That fix
    composes with this one: MDE-1 supplies a real checker when it can;
    capability_contract fails closed when no checker (or an inconclusive
    one) leaves a required capability's status unresolved either way."""
    project = await db_module.create_project(db, "mde1-no-wiring")
    await db_module.set_project_capability_manifest(
        db, project["id"],
        [_valid_capability(id="alpha", availability_policy="required")],
    )
    contract = await handoff_module.build_effective_capability_contract(db, project["id"])
    assert contract["availability"]["status"] == "unknown"
    assert contract["availability"]["missing"] == "unknown"
    assert contract["availability"]["unverified"] == ["alpha"]
    assert contract["executable"] is False
    assert "required_capabilities_unverified:alpha" in contract["executable_reasons"]


async def test_tenant_kwarg_derives_live_inventory_via_project_tools(db, monkeypatch):
    """A caller that supplies ``tenant`` (rather than an already-built
    ``live_inventory``) gets the SAME real wiring, derived the identical way
    ``check_capability_availability`` itself derives it."""
    project = await db_module.create_project(db, "mde1-tenant-kwarg")
    await db_module.set_project_capability_manifest(
        db, project["id"],
        [_valid_capability(id="alpha", required_tools=["ghost_plugin__ghost_tool"], availability_policy="required")],
    )

    from meridian.mcp.handlers import project_tools as project_tools_module

    async def _fake_build_live_inventory(tenant):
        assert tenant == {"id": "tenant-123"}
        return _inventory()

    monkeypatch.setattr(project_tools_module, "_build_live_inventory", _fake_build_live_inventory)

    contract = await handoff_module.build_effective_capability_contract(
        db, project["id"], tenant={"id": "tenant-123"},
    )
    assert contract["availability"]["status"] == "checked"
    assert contract["availability"]["missing"] == ["alpha"]
    assert contract["executable"] is False


async def test_live_inventory_probe_failure_degrades_gracefully(db, monkeypatch):
    """A failure deriving the live inventory from ``tenant`` (tunnel probe
    blew up, project_tools import failed, whatever) must degrade to the
    'unknown' availability shape -- never crash or turn the whole contract
    into None. A tunnel-probing problem must never make the mandatory
    start_session/generate_handoff response unavailable (``contract is not
    None`` below).

    A required capability left unverified by that degrade is correctly
    reported non-executable (capability_contract's fail-closed fix -- see
    the sibling test above for the full rationale): "gracefully" means the
    caller gets a well-formed, honest contract back, not that a probe
    failure is invisible to ``executable``."""
    project = await db_module.create_project(db, "mde1-probe-failure")
    await db_module.set_project_capability_manifest(
        db, project["id"], [_valid_capability(id="alpha", availability_policy="required")],
    )

    from meridian.mcp.handlers import project_tools as project_tools_module

    async def _boom(tenant):
        raise RuntimeError("tunnel probe blew up")

    monkeypatch.setattr(project_tools_module, "_build_live_inventory", _boom)

    contract = await handoff_module.build_effective_capability_contract(
        db, project["id"], tenant={"id": "tenant-456"},
    )
    assert contract is not None
    assert contract["availability"]["status"] == "unknown"
    assert contract["availability"]["unverified"] == ["alpha"]
    assert contract["executable"] is False
    assert "required_capabilities_unverified:alpha" in contract["executable_reasons"]


async def test_live_inventory_wins_over_tenant_when_both_supplied(db, monkeypatch):
    project = await db_module.create_project(db, "mde1-live-inventory-wins")
    await db_module.set_project_capability_manifest(
        db, project["id"], [_valid_capability(id="alpha", availability_policy="required")],
    )

    from meridian.mcp.handlers import project_tools as project_tools_module

    async def _fail_if_called(tenant):
        raise AssertionError("_build_live_inventory must not be called when live_inventory is already given")

    monkeypatch.setattr(project_tools_module, "_build_live_inventory", _fail_if_called)

    contract = await handoff_module.build_effective_capability_contract(
        db, project["id"], tenant={"id": "tenant-789"}, live_inventory=_inventory(),
    )
    assert contract["availability"]["status"] == "checked"
    assert contract["availability"]["available"] == ["alpha"]


async def test_board_stale_and_missing_required_both_reported_through_real_wiring(db):
    project = await db_module.create_project(db, "mde1-both-reasons")
    await db_module.set_project_capability_manifest(
        db, project["id"],
        [_valid_capability(id="alpha", required_tools=["ghost_plugin__ghost_tool"], availability_policy="required")],
    )
    contract = await handoff_module.build_effective_capability_contract(
        db, project["id"], board_stale=True, live_inventory=_inventory(),
    )
    assert contract["executable"] is False
    assert len(contract["executable_reasons"]) == 2


async def test_build_effective_capability_contract_wrapper_still_never_raises_with_live_inventory(db, monkeypatch):
    """Existing 'never raises' guarantee must hold even with the new
    tenant/live_inventory plumbing active."""
    from meridian import capability_contract as _cc_mod

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(_cc_mod, "build_capability_contract", _boom)
    project = await db_module.create_project(db, "mde1-wrapper-boom-live-inv")
    contract = await handoff_module.build_effective_capability_contract(
        db, project["id"], live_inventory=_inventory(),
    )
    assert contract is None
