"""Tests for sprint item 45049071 — optional OpenAI Secure MCP Tunnel
transport adapter (meridian/openai_tunnel_adapter.py).

Covers:
1. normalize_config — schema validation/normalization, required-when-enabled
   rules per transport, secret-shaped rejection, local-path tolerance
   (deliberately narrower than capability_manifest's own screen).
2. default_capability_entry — round-trips through
   capability_manifest.normalize_capability/normalize_manifest cleanly.
3. resolve_state / build_diagnostics / combined_diagnostics — deterministic,
   network-free lifecycle derivation, including the reported_status
   injectable seam and its ERROR-degrade-on-unrecognized-state behavior.
4. tunnel_plugins reservation of the adapter's own name (45049071 addendum
   to 9811d04c's is_reserved_custom_name).
"""
from __future__ import annotations

import pytest

from meridian import capability_manifest as cm
from meridian import openai_tunnel_adapter as ota
from meridian import tunnel_plugins as tp


# ---------------------------------------------------------------------------
# normalize_config
# ---------------------------------------------------------------------------

def test_normalize_config_none_yields_disabled_default():
    normalized = ota.normalize_config(None)
    assert normalized == {
        "enabled": False,
        "tunnel_id": None,
        "transport": None,
        "command": None,
        "url": None,
        "allowed_tools": [],
        "approval_policy": "always_ask",
        "tenant_id": None,
        "project_id": None,
        "env": None,
    }


def test_normalize_config_empty_dict_same_as_none():
    assert ota.normalize_config({}) == ota.normalize_config(None)


def test_normalize_config_rejects_non_dict():
    with pytest.raises(ota.OpenAITunnelAdapterError):
        ota.normalize_config("not a dict")  # type: ignore[arg-type]


def test_normalize_config_stdio_minimal_valid():
    normalized = ota.normalize_config({
        "enabled": True,
        "transport": "stdio",
        "command": ["npx", "-y", "@openai/mcp-tunnel"],
    })
    assert normalized["enabled"] is True
    assert normalized["transport"] == "stdio"
    assert normalized["command"] == ["npx", "-y", "@openai/mcp-tunnel"]
    assert normalized["allowed_tools"] == []
    assert normalized["approval_policy"] == "always_ask"


def test_normalize_config_stdio_command_as_string_is_split():
    normalized = ota.normalize_config({
        "enabled": True, "transport": "stdio", "command": "npx -y @openai/mcp-tunnel",
    })
    assert normalized["command"] == ["npx", "-y", "@openai/mcp-tunnel"]


def test_normalize_config_stdio_requires_command_when_enabled():
    with pytest.raises(ota.OpenAITunnelAdapterError, match="command is required"):
        ota.normalize_config({"enabled": True, "transport": "stdio"})


def test_normalize_config_http_minimal_valid():
    normalized = ota.normalize_config({
        "enabled": True, "transport": "http", "url": "https://openai.example/mcp",
    })
    assert normalized["transport"] == "http"
    assert normalized["url"] == "https://openai.example/mcp"


def test_normalize_config_http_requires_url_when_enabled():
    with pytest.raises(ota.OpenAITunnelAdapterError, match="url is required"):
        ota.normalize_config({"enabled": True, "transport": "http"})


def test_normalize_config_url_must_be_http_scheme():
    with pytest.raises(ota.OpenAITunnelAdapterError, match="http"):
        ota.normalize_config({
            "enabled": True, "transport": "http", "url": "ftp://not-http",
        })


def test_normalize_config_requires_transport_when_enabled():
    with pytest.raises(ota.OpenAITunnelAdapterError, match="transport is required"):
        ota.normalize_config({"enabled": True})


def test_normalize_config_disabled_does_not_require_transport():
    # Disabled config can be a bare skeleton -- no hard requirements kick in.
    normalized = ota.normalize_config({"enabled": False})
    assert normalized["enabled"] is False
    assert normalized["transport"] is None


def test_normalize_config_rejects_invalid_transport():
    with pytest.raises(ota.OpenAITunnelAdapterError, match="transport"):
        ota.normalize_config({"enabled": True, "transport": "carrier-pigeon"})


def test_normalize_config_rejects_invalid_approval_policy():
    with pytest.raises(ota.OpenAITunnelAdapterError, match="approval_policy"):
        ota.normalize_config({"approval_policy": "yolo"})


def test_normalize_config_allowed_tools_dedupes_preserving_order():
    normalized = ota.normalize_config({
        "allowed_tools": ["search", "fetch", "search", " fetch "],
    })
    assert normalized["allowed_tools"] == ["search", "fetch"]


def test_normalize_config_allowed_tools_rejects_non_string_list():
    with pytest.raises(ota.OpenAITunnelAdapterError, match="allowed_tools"):
        ota.normalize_config({"allowed_tools": ["ok", 5]})


def test_normalize_config_allowed_tools_default_is_empty_secure_default():
    normalized = ota.normalize_config({})
    assert normalized["allowed_tools"] == []


def test_normalize_config_env_coerced_to_str_str():
    normalized = ota.normalize_config({"env": {"FOO": 1, "": "dropped", "BAR": "baz"}})
    assert normalized["env"] == {"FOO": "1", "BAR": "baz"}


def test_normalize_config_env_empty_dict_normalizes_to_none():
    normalized = ota.normalize_config({"env": {}})
    assert normalized["env"] is None


def test_normalize_config_env_rejects_non_dict():
    with pytest.raises(ota.OpenAITunnelAdapterError, match="env"):
        ota.normalize_config({"env": "not a dict"})


def test_normalize_config_rejects_secret_shaped_tunnel_id():
    with pytest.raises(ota.OpenAITunnelAdapterError, match="secret-shaped"):
        ota.normalize_config({"tunnel_id": "sk-abcdefghijklmnopqrstuvwx"})


def test_normalize_config_rejects_secret_shaped_env_value():
    with pytest.raises(ota.OpenAITunnelAdapterError, match="secret-shaped"):
        ota.normalize_config({"env": {"OPENAI_API_KEY": "sk-abcdefghijklmnop"}})


def test_normalize_config_allows_opaque_tunnel_id_reference():
    # A non-secret-shaped opaque reference (e.g. a UUID-style handle) is fine.
    normalized = ota.normalize_config({"tunnel_id": "tun_9f3c2b7a-example"})
    assert normalized["tunnel_id"] == "tun_9f3c2b7a-example"


def test_normalize_config_allows_local_absolute_command_path():
    # Deliberately narrower than capability_manifest's screen -- a local
    # stdio launcher path is normal, local-machine-only config here.
    normalized = ota.normalize_config({
        "enabled": True,
        "transport": "stdio",
        "command": [r"C:\Users\dev\bin\openai-mcp-tunnel.exe", "--flag"],
    })
    assert normalized["command"][0] == r"C:\Users\dev\bin\openai-mcp-tunnel.exe"


def test_normalize_config_tenant_and_project_id_passthrough():
    normalized = ota.normalize_config({"tenant_id": "t1", "project_id": "p1"})
    assert normalized["tenant_id"] == "t1"
    assert normalized["project_id"] == "p1"


def test_normalize_config_blank_optional_string_rejected():
    with pytest.raises(ota.OpenAITunnelAdapterError, match="tenant_id"):
        ota.normalize_config({"tenant_id": "   "})


# ---------------------------------------------------------------------------
# default_capability_entry — round-trips through capability_manifest
# ---------------------------------------------------------------------------

def test_default_capability_entry_has_expected_shape():
    entry = ota.default_capability_entry()
    assert entry["id"] == ota.OPENAI_TUNNEL_CAPABILITY_ID == "openai_secure_mcp_tunnel"
    assert entry["availability_policy"] == "optional"
    assert entry["fallback_chain"] == ["meridian_tunnel"]
    assert entry["required_tools"] == ["openai_secure_mcp_tunnel"]


def test_default_capability_entry_passes_normalize_capability_already():
    # default_capability_entry() itself calls normalize_capability, so
    # re-normalizing must be a no-op (idempotent).
    entry = ota.default_capability_entry()
    assert cm.normalize_capability(entry) == entry


def test_default_capability_entry_round_trips_through_normalize_manifest():
    entry = ota.default_capability_entry()
    manifest = cm.normalize_manifest([entry])
    assert manifest == [entry]
    # manifest_hash must not raise on a manifest containing this entry.
    assert isinstance(cm.manifest_hash(manifest), str)


def test_default_capability_entry_overrides_apply_before_normalization():
    entry = ota.default_capability_entry(availability_policy="required")
    assert entry["availability_policy"] == "required"


# ---------------------------------------------------------------------------
# resolve_state / build_diagnostics / combined_diagnostics
# ---------------------------------------------------------------------------

def test_resolve_state_disabled_is_not_configured():
    state, detail = ota.resolve_state(ota.normalize_config(None))
    assert state is ota.OpenAITunnelState.NOT_CONFIGURED
    assert detail


def test_resolve_state_enabled_no_reported_status_is_configured():
    config = ota.normalize_config({
        "enabled": True, "transport": "stdio", "command": ["echo", "hi"],
    })
    state, detail = ota.resolve_state(config)
    assert state is ota.OpenAITunnelState.CONFIGURED
    assert detail


def test_resolve_state_reported_status_maps_recognized_state():
    config = ota.normalize_config({
        "enabled": True, "transport": "stdio", "command": ["echo", "hi"],
    })
    state, detail = ota.resolve_state(
        config, reported_status={"state": "connected", "detail": "ok"},
    )
    assert state is ota.OpenAITunnelState.CONNECTED
    assert detail == "ok"


def test_resolve_state_reported_status_unrecognized_state_degrades_to_error():
    config = ota.normalize_config({
        "enabled": True, "transport": "stdio", "command": ["echo", "hi"],
    })
    state, detail = ota.resolve_state(
        config, reported_status={"state": "teleporting"},
    )
    assert state is ota.OpenAITunnelState.ERROR
    assert detail


def test_resolve_state_reported_status_missing_state_key_degrades_to_error():
    config = ota.normalize_config({
        "enabled": True, "transport": "stdio", "command": ["echo", "hi"],
    })
    state, _detail = ota.resolve_state(config, reported_status={"detail": "no state field"})
    assert state is ota.OpenAITunnelState.ERROR


def test_build_diagnostics_not_configured_shape():
    diag = ota.build_diagnostics(None)
    d = diag.to_dict()
    assert d["state"] == "not_configured"
    assert d["transport"] is None
    assert d["allowed_tool_count"] == 0
    assert d["approval_policy"] == "always_ask"


def test_build_diagnostics_configured_shape():
    diag = ota.build_diagnostics({
        "enabled": True, "transport": "http", "url": "https://openai.example/mcp",
        "allowed_tools": ["search", "fetch"], "tenant_id": "t1", "project_id": "p1",
        "approval_policy": "auto_approve_allowlisted",
    })
    d = diag.to_dict()
    assert d["state"] == "configured"
    assert d["transport"] == "http"
    assert d["allowed_tool_count"] == 2
    assert d["tenant_id"] == "t1"
    assert d["project_id"] == "p1"
    assert d["approval_policy"] == "auto_approve_allowlisted"


def test_build_diagnostics_raises_on_invalid_config():
    with pytest.raises(ota.OpenAITunnelAdapterError):
        ota.build_diagnostics({"enabled": True, "transport": "carrier-pigeon"})


def test_combined_diagnostics_namespaces_both_transports():
    result = ota.combined_diagnostics(
        "tenant-1",
        openai_config={"enabled": True, "transport": "stdio", "command": ["echo"]},
        meridian_tunnel_active=True,
    )
    assert result["tenant_id"] == "tenant-1"
    assert result["meridian_tunnel"] == {"active": True}
    assert result["openai_tunnel"]["state"] == "configured"


def test_combined_diagnostics_default_openai_config_is_not_configured():
    result = ota.combined_diagnostics("tenant-2", meridian_tunnel_active=False)
    assert result["openai_tunnel"]["state"] == "not_configured"
    assert result["meridian_tunnel"] == {"active": False}


def test_combined_diagnostics_never_conflates_the_two_states():
    # Meridian tunnel active, OpenAI adapter not configured -- these must
    # stay independent, never merged into one flag.
    result = ota.combined_diagnostics(
        "tenant-3", openai_config=None, meridian_tunnel_active=True,
    )
    assert result["meridian_tunnel"]["active"] is True
    assert result["openai_tunnel"]["state"] == "not_configured"


# ---------------------------------------------------------------------------
# tunnel_plugins — reserved name integration (45049071 addendum to 9811d04c)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["openai", "openai-tunnel", "openai_secure_mcp_tunnel",
                                   "OpenAI", "OPENAI_SECURE_MCP_TUNNEL"])
def test_openai_tunnel_names_are_reserved_custom_names(name):
    assert tp.is_reserved_custom_name(name) is True


def test_unrelated_names_still_not_reserved():
    for n in ("fetch", "git", "my-plugin"):
        assert tp.is_reserved_custom_name(n) is False


def test_validate_custom_plugin_rejects_openai_reserved_name():
    entry, error = tp.validate_custom_plugin("openai", ["some", "command"])
    assert entry is None
    assert error is not None
