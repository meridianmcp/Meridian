"""5a8a2d2e — list_plugins/get_plugin_details falsely report cold-fetch-family
slots (powerpoint/meridian-docs/word/desktop-commander/zotero/meridian-outputs/
debug) as unreachable/0-tools when they are actually just still cold-spawning.

Root cause: ``_fetch_slot_tools`` (routes/tunnel.py) is the sole wall-clock gate
list_plugins/get_plugin_details use to decide whether a slot is
active/invocable, and it was bounded to a flat ``_SLOT_TOOLS_FETCH_BUDGET`` of
4.0s with a hardcoded ``range(4)`` attempt cap. The tunnel CLIENT's own
``ensure_running()`` cold-spawn readiness probe for exactly this slot family
(``tunnel_client._COLD_FETCH_SLOTS``) can legitimately take up to ~55s
(``_PREFLIGHT_BUDGET_COLD_FETCH`` = 4 attempts x 5s delay x 10s per-attempt
timeout) BEFORE it even relays the tools/list request this function is
waiting on — a ~10x mismatch that made a slot merely still starting up read as
genuinely dead.

Fix: ``_fetch_slot_tools`` now accepts an explicit ``budget=`` override and its
retry loop is budget-exhaustion-bound instead of attempt-count-bound (the old
``range(4)`` cap silently defeated any budget bigger than ~13.5s). list_plugins
and get_plugin_details pass a much larger, cold-fetch-slot-aware budget
(``_slot_tools_fetch_budget``) for the cold-fetch family only; fs/code/extract
(never cold) keep the original flat 4.0s budget so the common case stays fast.

Unit-level with mocks only — no real socket/network/tunnel touched.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.responses import Response

import meridian.server  # noqa: F401 — load through the normal path (avoids a
# circular import between meridian.mcp.handler and meridian.server).
from meridian.mcp.handler import _dispatch_mcp_tool
from meridian.routes import tunnel as tn


_TENANT = {"id": "tenant-5a8a2d2e", "plan": "pro"}

_COLD_FETCH_LABELS = ("dc", "ppt", "word", "docs", "zotero", "outputs", "debug")
_FAST_LABELS = ("fs", "code", "extract")


@pytest.fixture(autouse=True)
def _clean_tunnel_state():
    """Reset per-process registries so tests never leak state between runs."""
    def _reset():
        for d in (
            tn._tunnel_sockets, tn._tunnel_code_sockets, tn._tunnel_extract_sockets,
            tn._tunnel_ppt_sockets, tn._tunnel_word_sockets, tn._tunnel_dc_sockets,
            tn._tunnel_docs_sockets, tn._tunnel_zotero_sockets,
            tn._tunnel_outputs_sockets, tn._tunnel_debug_sockets,
            tn._tunnel_tool_routes,
        ):
            d.clear()
    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# 1. _slot_tools_fetch_budget — the cold-fetch/fast split
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label", _COLD_FETCH_LABELS)
def test_cold_fetch_family_gets_the_larger_budget(label):
    assert tn._slot_tools_fetch_budget(label) == tn._SLOT_TOOLS_FETCH_BUDGET_COLD
    assert tn._SLOT_TOOLS_FETCH_BUDGET_COLD > tn._SLOT_TOOLS_FETCH_BUDGET


@pytest.mark.parametrize("label", _FAST_LABELS)
def test_always_warm_slots_keep_the_flat_default_budget(label):
    assert tn._slot_tools_fetch_budget(label) == tn._SLOT_TOOLS_FETCH_BUDGET


def test_cold_fetch_budget_covers_the_documented_worst_case_cold_spawn():
    """The client's own ~55s worst-case cold-spawn readiness window
    (tunnel_client._PREFLIGHT_BUDGET_COLD_FETCH: 4 * 5s delay + 10s timeout each
    ~= 55s) must fit inside the cold budget with room to spare for the actual
    relay after ensure_running() returns."""
    from meridian.tunnel_client import _PREFLIGHT_BUDGET_COLD_FETCH

    attempts, delay = _PREFLIGHT_BUDGET_COLD_FETCH
    worst_case_cold_spawn = attempts * (delay + 10.0)
    assert tn._SLOT_TOOLS_FETCH_BUDGET_COLD >= worst_case_cold_spawn


def test_cold_fetch_slots_here_match_the_client_side_source_of_truth():
    """The set this fix keys off of must stay in lockstep with the client's
    own _COLD_FETCH_SLOTS — a drift here would silently un-fix this bug for
    any slot added to one set but not the other."""
    from meridian.tunnel_client import _COLD_FETCH_SLOTS

    assert _COLD_FETCH_SLOTS == frozenset(_COLD_FETCH_LABELS)


# ---------------------------------------------------------------------------
# 2. _fetch_slot_tools — budget-exhaustion-bound retries, not attempt-count-bound
# ---------------------------------------------------------------------------

def test_fetch_slot_tools_uses_the_full_explicit_budget_for_retries():
    """A slot that fails many times before succeeding — more than the OLD
    hardcoded 4-attempt cap would ever allow — still succeeds when a large
    explicit budget is supplied, because the retry loop is now bound by the
    budget itself rather than a fixed attempt count."""
    tn._tunnel_docs_sockets["t-cold-docs"] = object()
    calls = {"n": 0}

    async def eventually_succeeds(
        tenant_id, method, path, query, headers, body, sockets, pending, label,
    ):
        calls["n"] += 1
        if calls["n"] < 9:  # more attempts than the old range(4) cap allowed
            resp = {"error": {"message": "still cold-spawning"}}
            return Response(content=json.dumps(resp).encode(), status_code=503,
                             media_type="application/json")
        resp = {"result": {"tools": [{"name": "convert_document"}]}}
        return Response(content=json.dumps(resp).encode(), status_code=200,
                         media_type="application/json")

    import meridian.routes.tunnel as tn_mod
    orig_do_proxy = tn_mod._do_proxy
    tn_mod._do_proxy = eventually_succeeds
    try:
        label, tools = asyncio.run(
            tn._fetch_slot_tools(
                "t-cold-docs", "docs", budget=tn._SLOT_TOOLS_FETCH_BUDGET_COLD,
            )
        )
    finally:
        tn_mod._do_proxy = orig_do_proxy

    assert label == "docs"
    assert [t["name"] for t in tools] == ["convert_document"]
    assert calls["n"] == 9


def test_same_flaky_slot_fails_under_the_flat_default_budget():
    """The identical flaky-then-succeeds slot from the previous test does NOT
    get through under the flat (fast-slot) default budget — proving the fast
    path (fs/code/extract) is unaffected and stays bounded to the short budget,
    i.e. this fix does not make the common case slower or more lenient."""
    tn._tunnel_code_sockets["t-flaky-fast"] = object()
    calls = {"n": 0}

    async def eventually_succeeds(
        tenant_id, method, path, query, headers, body, sockets, pending, label,
    ):
        calls["n"] += 1
        if calls["n"] < 9:
            resp = {"error": {"message": "still cold-spawning"}}
            return Response(content=json.dumps(resp).encode(), status_code=503,
                             media_type="application/json")
        resp = {"result": {"tools": [{"name": "trace_path"}]}}
        return Response(content=json.dumps(resp).encode(), status_code=200,
                         media_type="application/json")

    import meridian.routes.tunnel as tn_mod
    orig_do_proxy = tn_mod._do_proxy
    tn_mod._do_proxy = eventually_succeeds
    try:
        start = time.monotonic()
        label, tools = asyncio.run(tn._fetch_slot_tools("t-flaky-fast", "code"))
        elapsed = time.monotonic() - start
    finally:
        tn_mod._do_proxy = orig_do_proxy

    assert label == "code"
    assert tools == []  # gave up before attempt #9 ever answered
    assert calls["n"] < 9
    assert elapsed < tn._SLOT_TOOLS_FETCH_BUDGET + 1.0


def test_genuinely_dead_slot_still_reports_empty_within_a_large_budget():
    """A slot that NEVER answers must still return promptly (bounded near the
    supplied budget, not hang) even with the larger cold-fetch budget — the
    removal of the hardcoded attempt cap must not turn a dead slot into an
    unbounded wait."""
    tn._tunnel_ppt_sockets["t-dead-ppt"] = object()

    async def always_fails(
        tenant_id, method, path, query, headers, body, sockets, pending, label,
    ):
        resp = {"error": {"message": "connection refused"}}
        return Response(content=json.dumps(resp).encode(), status_code=503,
                         media_type="application/json")

    import meridian.routes.tunnel as tn_mod
    orig_do_proxy = tn_mod._do_proxy
    tn_mod._do_proxy = always_fails
    try:
        start = time.monotonic()
        label, tools = asyncio.run(
            tn._fetch_slot_tools("t-dead-ppt", "ppt", budget=2.0)
        )
        elapsed = time.monotonic() - start
    finally:
        tn_mod._do_proxy = orig_do_proxy

    assert label == "ppt"
    assert tools == []
    assert elapsed < 3.0  # bounded near the small explicit budget, not runaway


def test_default_budget_behavior_is_unchanged_when_no_budget_is_passed():
    """Omitting ``budget`` entirely must behave exactly as before — the flat
    ``_SLOT_TOOLS_FETCH_BUDGET`` — for backward compatibility with every
    existing caller that doesn't opt in to the cold-fetch-aware budget."""
    tn._tunnel_code_sockets["t-slow-default"] = object()

    async def hangs(
        tenant_id, method, path, query, headers, body, sockets, pending, label,
    ):
        await asyncio.sleep(30)
        raise AssertionError("should never complete")

    import meridian.routes.tunnel as tn_mod
    orig_do_proxy = tn_mod._do_proxy
    tn_mod._do_proxy = hangs
    try:
        start = time.monotonic()
        label, tools = asyncio.run(tn._fetch_slot_tools("t-slow-default", "code"))
        elapsed = time.monotonic() - start
    finally:
        tn_mod._do_proxy = orig_do_proxy

    assert label == "code"
    assert tools == []
    assert elapsed < tn._SLOT_TOOLS_FETCH_BUDGET + 1.0


# ---------------------------------------------------------------------------
# 3. Integration — list_plugins / get_plugin_details request the cold budget
#    ONLY for the cold-fetch family, leaving fs/code/extract untouched.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_plugins_requests_the_cold_budget_only_for_cold_slots(monkeypatch, db, tmp_path):
    tenant_id = _TENANT["id"]
    monkeypatch.setattr(tn, "has_active_tunnel", lambda _tid: True)

    budgets_seen: dict[str, "float | None"] = {}

    async def _fake_fetch(_tenant_id, label, *, budget=None):
        budgets_seen[label] = budget
        return label, []

    monkeypatch.setattr(tn, "_fetch_slot_tools", _fake_fetch)

    await _dispatch_mcp_tool("list_plugins", {}, db, str(tmp_path), tenant=_TENANT)

    for label in _COLD_FETCH_LABELS:
        assert budgets_seen[label] == tn._SLOT_TOOLS_FETCH_BUDGET_COLD, (
            f"cold-fetch slot {label!r} should request the larger budget"
        )
    for label in _FAST_LABELS:
        assert budgets_seen[label] == tn._SLOT_TOOLS_FETCH_BUDGET, (
            f"always-warm slot {label!r} should keep the flat default budget"
        )


@pytest.mark.asyncio
async def test_get_plugin_details_requests_the_cold_budget_for_a_cold_slot(monkeypatch, db, tmp_path):
    tenant_id = _TENANT["id"]
    monkeypatch.setattr(tn, "has_active_tunnel", lambda _tid: True)

    budgets_seen: dict[str, "float | None"] = {}

    async def _fake_fetch(_tenant_id, label, *, budget=None):
        budgets_seen[label] = budget
        return label, [{"name": "convert_document"}]

    monkeypatch.setattr(tn, "_fetch_slot_tools", _fake_fetch)

    result = await _dispatch_mcp_tool(
        "get_plugin_details", {"name": "meridian-docs"}, db, str(tmp_path), tenant=_TENANT,
    )
    assert "error" not in result
    assert budgets_seen.get("docs") == tn._SLOT_TOOLS_FETCH_BUDGET_COLD


@pytest.mark.asyncio
async def test_get_plugin_details_requests_the_flat_budget_for_a_fast_slot(monkeypatch, db, tmp_path):
    tenant_id = _TENANT["id"]
    monkeypatch.setattr(tn, "has_active_tunnel", lambda _tid: True)

    budgets_seen: dict[str, "float | None"] = {}

    async def _fake_fetch(_tenant_id, label, *, budget=None):
        budgets_seen[label] = budget
        return label, [{"name": "read_file"}]

    monkeypatch.setattr(tn, "_fetch_slot_tools", _fake_fetch)

    result = await _dispatch_mcp_tool(
        "get_plugin_details", {"name": "filesystem"}, db, str(tmp_path), tenant=_TENANT,
    )
    assert "error" not in result
    assert budgets_seen.get("fs") == tn._SLOT_TOOLS_FETCH_BUDGET
