"""Tests for the layered tunnel/connector diagnostics view (sprint f1e0df55).

``build_tunnel_diagnostics`` (meridian/routes/tunnel.py) is the single builder
shared by the ``GET /tunnel/diagnostics/{tenant_id}`` HTTP route and the
``get_tunnel_diagnostics`` MCP tool. These tests exercise the pure
classification/redaction helpers directly, then the full builder against the
per-process socket/health registries (no real WebSocket/network touched), then
both surfaces that call it.
"""
from __future__ import annotations

import json

import pytest

import meridian.server  # noqa: F401 — load through the normal path (avoids a
# circular import between meridian.mcp.handler and meridian.server).
from meridian.mcp.handler import _dispatch_mcp_tool
from meridian.routes import tunnel as tn


_TENANT = {"id": "tenant-f1e0df55", "plan": "pro"}


@pytest.fixture(autouse=True)
def _clean_diag_state():
    """Reset per-process tunnel registries so tests never leak state."""
    def _reset():
        for d in (
            tn._tunnel_sockets, tn._tunnel_code_sockets, tn._tunnel_extract_sockets,
            tn._tunnel_ppt_sockets, tn._tunnel_word_sockets, tn._tunnel_dc_sockets,
            tn._tunnel_docs_sockets, tn._tunnel_zotero_sockets,
            tn._tunnel_outputs_sockets, tn._tunnel_debug_sockets,
            tn._tunnel_tool_routes, tn._slot_health, tn._slot_status_detail,
            tn._slot_unhealthy_since, tn._tools_list_changed_pending,
            tn._tenant_owner_instance,
        ):
            d.clear()
    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# _diag_redact — credential scrubbing
# ---------------------------------------------------------------------------

def test_diag_redact_scrubs_value_by_key_name():
    out = tn._diag_redact({"token": "sk_live_abc123", "name": "fs"})
    assert out["token"] == "[redacted]"
    assert out["name"] == "fs"


def test_diag_redact_scrubs_nested_dict_and_list():
    out = tn._diag_redact({
        "env": {"API_KEY": "secret-value", "OTHER": "keep-me"},
        "items": [{"password": "hunter2"}, {"ok": "fine"}],
    })
    assert out["env"]["API_KEY"] == "[redacted]"
    assert out["env"]["OTHER"] == "keep-me"
    assert out["items"][0]["password"] == "[redacted]"
    assert out["items"][1]["ok"] == "fine"


def test_diag_redact_scrubs_inline_secret_in_free_text():
    out = tn._diag_redact({"command": "uvx some-mcp --api-key=sk_live_xxx --port=1"})
    assert "sk_live_xxx" not in out["command"]
    assert "[redacted]" in out["command"]
    assert "--port=1" in out["command"]  # non-secret args survive


def test_diag_redact_leaves_ordinary_strings_alone():
    out = tn._diag_redact({"description": "Filesystem connector", "port": 8808})
    assert out["description"] == "Filesystem connector"
    assert out["port"] == 8808


# ---------------------------------------------------------------------------
# _diag_slot_label — five distinct states, precedence
# ---------------------------------------------------------------------------

def test_label_quarantined_wins_over_everything():
    label = tn._diag_slot_label(
        dashboard_enabled=True, process_active=True, healthy_flag=False,
        detail={"state": "quarantined", "quarantine_reason": "3 deterministic failures"},
    )
    assert label == "quarantined"


def test_label_quarantine_reason_alone_is_sufficient():
    label = tn._diag_slot_label(
        dashboard_enabled=True, process_active=False, healthy_flag=False,
        detail={"quarantine_reason": "dependency missing"},
    )
    assert label == "quarantined"


@pytest.mark.parametrize("bad_state", [
    "degraded", "transport_closed", "tools_list_timeout",
    "startup_timeout", "child_crashed", "dependency_missing",
])
def test_label_degraded_states(bad_state):
    label = tn._diag_slot_label(
        dashboard_enabled=True, process_active=True, healthy_flag=False,
        detail={"state": bad_state, "reason": "unhealthy", "detail": "probe failed"},
    )
    assert label == "degraded"


def test_label_bare_unhealthy_with_no_specific_state_is_degraded():
    label = tn._diag_slot_label(
        dashboard_enabled=True, process_active=True, healthy_flag=False,
        detail=None,
    )
    assert label == "degraded"


@pytest.mark.parametrize("restart_state", ["idle_killed", "stopped"])
def test_label_lifecycle_teardown_is_restart_required_when_still_enabled(restart_state):
    label = tn._diag_slot_label(
        dashboard_enabled=True, process_active=False, healthy_flag=True,
        detail={"state": restart_state},
    )
    assert label == "restart_required"


def test_label_split_brain_enabled_but_no_history_is_stale():
    """Dashboard says enabled, nothing running, and we have zero diagnostic
    history — we genuinely don't know why (never connected vs. mid-restart)."""
    label = tn._diag_slot_label(
        dashboard_enabled=True, process_active=False, healthy_flag=True,
        detail=None,
    )
    assert label == "stale"


def test_label_split_brain_disabled_but_still_running_is_restart_required():
    """A process is live that the dashboard says should be off."""
    label = tn._diag_slot_label(
        dashboard_enabled=False, process_active=True, healthy_flag=True,
        detail=None,
    )
    assert label == "restart_required"


def test_label_enabled_not_running_with_history_is_restart_required_not_stale():
    """Some diagnostic history exists (even non-adverse) — distinguishes a
    known "it stopped" from a genuinely unknown "stale" state."""
    label = tn._diag_slot_label(
        dashboard_enabled=True, process_active=False, healthy_flag=True,
        detail={"reason": "unhealthy", "detail": "watchdog gave up"},
    )
    assert label == "restart_required"


def test_label_consistently_off_is_healthy():
    label = tn._diag_slot_label(
        dashboard_enabled=False, process_active=False, healthy_flag=True,
        detail=None,
    )
    assert label == "healthy"


def test_label_consistently_on_no_issues_is_healthy():
    label = tn._diag_slot_label(
        dashboard_enabled=True, process_active=True, healthy_flag=True,
        detail=None,
    )
    assert label == "healthy"


def test_all_five_states_are_distinct_strings():
    """Sanity: the five labels the sprint item calls out are all reachable and
    all different — a regression that collapses two of them together (e.g.
    stale silently becoming healthy) should fail this."""
    seen = {
        tn._diag_slot_label(dashboard_enabled=True, process_active=True,
                             healthy_flag=False, detail={"state": "quarantined"}),
        tn._diag_slot_label(dashboard_enabled=True, process_active=True,
                             healthy_flag=False, detail={"state": "degraded"}),
        tn._diag_slot_label(dashboard_enabled=True, process_active=False,
                             healthy_flag=True, detail={"state": "idle_killed"}),
        tn._diag_slot_label(dashboard_enabled=True, process_active=False,
                             healthy_flag=True, detail=None),
        tn._diag_slot_label(dashboard_enabled=True, process_active=True,
                             healthy_flag=True, detail=None),
    }
    assert seen == {"quarantined", "degraded", "restart_required", "stale", "healthy"}


# ---------------------------------------------------------------------------
# _diag_remediation — exact, actionable text per label
# ---------------------------------------------------------------------------

def test_remediation_healthy_is_a_noop():
    assert tn._diag_remediation("healthy", None) == "No action needed."


def test_remediation_quarantined_mentions_restart():
    text = tn._diag_remediation("quarantined", {"quarantine_reason": "bad config"})
    assert "restart" in text.lower()
    assert "bad config" in text


def test_remediation_degraded_surfaces_detail():
    text = tn._diag_remediation("degraded", {"detail": "tools/list timed out"})
    assert "tools/list timed out" in text


def test_remediation_restart_required_mentions_restart():
    assert "restart" in tn._diag_remediation("restart_required", None).lower()


def test_remediation_stale_mentions_reconnect_or_restart():
    text = tn._diag_remediation("stale", None).lower()
    assert "reconnect" in text or "restart" in text


# ---------------------------------------------------------------------------
# _config_generation / _config_manifest_hash — determinism + drift detection
# ---------------------------------------------------------------------------

def test_config_generation_stable_for_identical_input():
    cfg = {"fs": {"enabled": True}}
    assert tn._config_generation(cfg) == tn._config_generation(json.loads(json.dumps(cfg)))


def test_config_generation_changes_with_content():
    a = tn._config_generation({"fs": {"enabled": True}})
    b = tn._config_generation({"fs": {"enabled": False}})
    assert a != b


def test_config_manifest_hash_stable_and_sensitive():
    cfg1 = {"fs": {"enabled": True}}
    cfg2 = {"fs": {"enabled": True}}
    cfg3 = {"fs": {"enabled": False}}
    assert tn._config_manifest_hash(cfg1) == tn._config_manifest_hash(cfg2)
    assert tn._config_manifest_hash(cfg1) != tn._config_manifest_hash(cfg3)
    assert len(tn._config_manifest_hash(cfg1)) == 64  # sha256 hex digest


# ---------------------------------------------------------------------------
# build_tunnel_diagnostics — full builder integration
# ---------------------------------------------------------------------------

def test_build_diagnostics_unauthenticated_stub_when_no_tenant():
    result = tn.build_tunnel_diagnostics(None)
    assert result["authenticated"] is False
    assert result["tenant_id"] is None
    assert result["slots"] == {}
    assert "run_id" in result and result["run_id"]
    assert result["server_routing_cache"] == {"routed_tool_count": 0, "cache_populated": False}


def test_build_diagnostics_reports_run_id_and_timestamp():
    r1 = tn.build_tunnel_diagnostics(_TENANT)
    r2 = tn.build_tunnel_diagnostics(_TENANT)
    assert r1["run_id"] != r2["run_id"]  # correlation id, unique per call
    assert isinstance(r1["generated_at"], float)


def test_build_diagnostics_never_labels_dashboard_only_setting_as_process_active():
    """The core requirement: a slot enabled only in the dashboard config, with
    no live socket, must report process_active=False (never conflated)."""
    tenant = dict(_TENANT, tunnel_plugins=json.dumps({"filesystem": {"enabled": True}}))
    result = tn.build_tunnel_diagnostics(tenant)
    fs = result["slots"]["fs"]
    assert fs["dashboard_configured"]["enabled"] is True
    assert fs["process_active"] is False
    assert fs["state"] == "stale"  # enabled, nothing running, no history


def test_build_diagnostics_healthy_slot_when_socket_live_and_no_bad_report():
    tenant = dict(_TENANT, tunnel_plugins=json.dumps({"filesystem": {"enabled": True}}))
    tn._tunnel_sockets[_TENANT["id"]] = object()  # fake live socket
    result = tn.build_tunnel_diagnostics(tenant)
    fs = result["slots"]["fs"]
    assert fs["process_active"] is True
    assert fs["state"] == "healthy"
    assert fs["remediation"] == "No action needed."


def test_build_diagnostics_degraded_slot_surfaces_last_error_and_remediation():
    tenant = dict(_TENANT, tunnel_plugins=json.dumps({"filesystem": {"enabled": True}}))
    tn._tunnel_sockets[_TENANT["id"]] = object()
    tn._slot_health.setdefault(_TENANT["id"], {})["fs"] = False
    tn._slot_status_detail.setdefault(_TENANT["id"], {})["fs"] = {
        "reason": "unhealthy", "detail": "tools/list failed", "state": "degraded",
    }
    result = tn.build_tunnel_diagnostics(tenant)
    fs = result["slots"]["fs"]
    assert fs["state"] == "degraded"
    assert fs["last_error"] == "tools/list failed"
    assert "degraded" in fs["remediation"].lower()


def test_build_diagnostics_quarantined_slot():
    tenant = dict(_TENANT, tunnel_plugins=json.dumps({"code-intel": {"enabled": True}}))
    tn._slot_health.setdefault(_TENANT["id"], {})["code"] = False
    tn._slot_status_detail.setdefault(_TENANT["id"], {})["code"] = {
        "reason": "quarantined", "detail": "3 deterministic failures",
        "state": "quarantined", "quarantine_reason": "missing dependency 'foo'",
        "retry_count": 3,
    }
    result = tn.build_tunnel_diagnostics(tenant)
    code = result["slots"]["code"]
    assert code["state"] == "quarantined"
    assert code["quarantine_reason"] == "missing dependency 'foo'"
    assert code["retry_count"] == 3


def test_build_diagnostics_recovered_slot_clears_status_detail():
    """A slot that WAS unhealthy and recovers has its status_detail cleared by
    _record_slot_health — diagnostics should then show it healthy, matching
    the "recovered" state the sprint item explicitly asks tests to cover."""
    tid = _TENANT["id"]
    tenant = dict(_TENANT, tunnel_plugins=json.dumps({"filesystem": {"enabled": True}}))
    tn._tunnel_sockets[tid] = object()
    tn._record_slot_health(tid, "fs", False, reason="unhealthy", detail="probe failed")
    assert tn.build_tunnel_diagnostics(tenant)["slots"]["fs"]["state"] == "degraded"
    tn._record_slot_health(tid, "fs", True)  # recovery
    result = tn.build_tunnel_diagnostics(tenant)
    assert result["slots"]["fs"]["state"] == "healthy"
    assert result["slots"]["fs"]["last_error"] is None
    # Recovery also marks the tenant's tools/list cache stale (54ddd609).
    assert result["connector_manifest"]["tools_list_stale"] is True


def test_build_diagnostics_split_brain_disabled_but_socket_still_live():
    """Partial/split-brain: dashboard config was saved to disable the slot,
    but the tunnel hasn't restarted to pick that up — socket still live."""
    tenant = dict(_TENANT, tunnel_plugins=json.dumps({"code-intel": {"enabled": False}}))
    tn._tunnel_code_sockets[_TENANT["id"]] = object()
    result = tn.build_tunnel_diagnostics(tenant)
    code = result["slots"]["code"]
    assert code["dashboard_configured"]["enabled"] is False
    assert code["process_active"] is True
    assert code["state"] == "restart_required"


def test_build_diagnostics_routing_cache_reflects_last_tools_list():
    tid = _TENANT["id"]
    tn._tunnel_tool_routes[tid] = {"filesystem__read_file": "fs", "code__search_graph": "code"}
    result = tn.build_tunnel_diagnostics(dict(_TENANT))
    assert result["server_routing_cache"] == {"routed_tool_count": 2, "cache_populated": True}


def test_build_diagnostics_manifest_hash_reflects_persisted_config():
    tenant_a = dict(_TENANT, tunnel_plugins=json.dumps({"filesystem": {"enabled": True}}))
    tenant_b = dict(_TENANT, tunnel_plugins=json.dumps({"filesystem": {"enabled": False}}))
    hash_a = tn.build_tunnel_diagnostics(tenant_a)["connector_manifest"]["manifest_hash"]
    hash_b = tn.build_tunnel_diagnostics(tenant_b)["connector_manifest"]["manifest_hash"]
    assert hash_a != hash_b


def test_build_diagnostics_all_credentials_redacted_end_to_end():
    """Belt-and-suspenders: even if a slot's persisted description/command ever
    carried something credential-shaped, it never survives to the response."""
    tenant = dict(_TENANT, tunnel_plugins=json.dumps({
        "filesystem": {"enabled": True, "command": ["npx", "-y", "server", "--token=sk_live_verysecret"]},
    }))
    result = tn.build_tunnel_diagnostics(tenant)
    blob = json.dumps(result)
    assert "sk_live_verysecret" not in blob


# ---------------------------------------------------------------------------
# HTTP route — GET /tunnel/diagnostics/{tenant_id}
# ---------------------------------------------------------------------------

class _FakeQueryParams(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class _FakeRequest:
    def __init__(self, query_params=None):
        self.query_params = _FakeQueryParams(query_params or {})


@pytest.mark.asyncio
async def test_http_route_delegates_to_builder(monkeypatch):
    async def _fake_tenant(_request):
        return dict(_TENANT)
    monkeypatch.setattr(tn, "_get_tenant_from_request", _fake_tenant)

    resp = await tn.tunnel_diagnostics(_TENANT["id"], _FakeRequest())
    body = json.loads(resp.body)
    assert body["authenticated"] is True
    assert body["tenant_id"] == _TENANT["id"]
    assert "slots" in body


@pytest.mark.asyncio
async def test_http_route_passes_hostname_query_param(monkeypatch):
    seen = {}

    async def _fake_tenant(_request):
        return dict(_TENANT)
    monkeypatch.setattr(tn, "_get_tenant_from_request", _fake_tenant)

    def _fake_builder(tenant, hostname=None):
        seen["hostname"] = hostname
        return {"ok": True}
    monkeypatch.setattr(tn, "build_tunnel_diagnostics", _fake_builder)

    await tn.tunnel_diagnostics(_TENANT["id"], _FakeRequest({"hostname": "laptop-1"}))
    assert seen["hostname"] == "laptop-1"


@pytest.mark.asyncio
async def test_http_route_unauthenticated_returns_stub(monkeypatch):
    async def _no_tenant(_request):
        return None
    monkeypatch.setattr(tn, "_get_tenant_from_request", _no_tenant)

    resp = await tn.tunnel_diagnostics("whatever", _FakeRequest())
    body = json.loads(resp.body)
    assert body["authenticated"] is False
    assert body["slots"] == {}


# ---------------------------------------------------------------------------
# MCP tool — get_tunnel_diagnostics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_tool_registered_and_read_only():
    from meridian.mcp_tools import _MCP_TOOLS_LIST, _READ_ONLY_TOOLS

    names = {t["name"] for t in _MCP_TOOLS_LIST}
    assert "get_tunnel_diagnostics" in names
    assert "get_tunnel_diagnostics" in _READ_ONLY_TOOLS
    tool = next(t for t in _MCP_TOOLS_LIST if t["name"] == "get_tunnel_diagnostics")
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["annotations"]["destructiveHint"] is False


@pytest.mark.asyncio
async def test_mcp_tool_dispatch_matches_builder(db, tmp_path):
    tenant = dict(_TENANT, tunnel_plugins=json.dumps({"filesystem": {"enabled": True}}))
    tn._tunnel_sockets[_TENANT["id"]] = object()

    result = await _dispatch_mcp_tool(
        "get_tunnel_diagnostics", {}, db, str(tmp_path), tenant=tenant,
    )
    assert result["authenticated"] is True
    assert result["slots"]["fs"]["state"] == "healthy"


@pytest.mark.asyncio
async def test_mcp_tool_dispatch_self_hosted_no_tenant(db, tmp_path):
    result = await _dispatch_mcp_tool(
        "get_tunnel_diagnostics", {}, db, str(tmp_path), tenant=None,
    )
    assert result["authenticated"] is False
    assert result["slots"] == {}


@pytest.mark.asyncio
async def test_mcp_tool_dispatch_passes_hostname_arg(db, tmp_path, monkeypatch):
    seen = {}

    def _fake_builder(tenant, hostname=None):
        seen["hostname"] = hostname
        return {"ok": True}
    monkeypatch.setattr(tn, "build_tunnel_diagnostics", _fake_builder)

    await _dispatch_mcp_tool(
        "get_tunnel_diagnostics", {"hostname": "workstation"}, db, str(tmp_path),
        tenant=dict(_TENANT),
    )
    assert seen["hostname"] == "workstation"
