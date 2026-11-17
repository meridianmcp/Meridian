"""225861b8 — the read-only InvenTree tunnel slot.

The slot is a built-in on its own port. Its spawn environment forces read-only mode whatever
the stored config says, the InvenTree token is never kept in stored or returned config, and its
routes and labels match the other slots.
"""

from __future__ import annotations

import meridian.routes.tunnel as tunnel_routes
import meridian.tunnel_plugins as tp


def _plugin() -> dict:
    return next(p for p in tp.BUILTIN_PLUGINS if p["slot"] == "inventree")


def test_inventree_is_a_fixed_builtin_slot_on_its_own_port() -> None:
    plugin = _plugin()
    assert plugin["name"] == "meridian-inventree"
    assert plugin["port"] == tp.DEFAULT_INVENTREE_PORT == 8823
    assert plugin["url_prefix"] == "/inventree"
    assert plugin["enabled"] is False  # off by default; the owner switches it on
    assert plugin["prefix"] is None
    assert plugin["session_mode"] == "stateless"
    assert "inventree" in tp.SLOTS
    assert tp.DEFAULT_INVENTREE_PORT in tp._BUILTIN_DEFAULT_PORTS
    assert tp._CUSTOM_PORT_START > tp.DEFAULT_INVENTREE_PORT


def test_inventree_ports_are_unique_among_the_builtins() -> None:
    ports = [p["port"] for p in tp.BUILTIN_PLUGINS]
    assert len(ports) == len(set(ports))


def test_inventree_command_runs_the_extension_entry_point() -> None:
    cmd = _plugin()["command"]
    assert cmd[0] == "uvx"
    assert cmd[-1] == "meridian-inventree-mcp"
    assert "--from" in cmd
    assert "meridian-inventree" in cmd[cmd.index("--from") + 1]


def test_inventree_catalog_entry_is_bundled() -> None:
    names = {p["slot"]: p for p in tp.KNOWN_PLUGIN_TOOLS if isinstance(p, dict) and "slot" in p}
    assert names["inventree"]["bundled"] is True
    assert names["inventree"]["package"] == "meridian-inventree"


def test_enforced_env_wins_over_a_stored_override() -> None:
    env = tp.enforce_slot_env("inventree", {"MERIDIAN_INVENTREE_READ_ONLY": "0", "OTHER": "1"})
    assert env is not None
    assert env["MERIDIAN_INVENTREE_READ_ONLY"] == "1"
    assert env["OTHER"] == "1"


def test_enforced_env_keeps_the_process_environment(monkeypatch) -> None:
    monkeypatch.setenv("INVENTREE_TOKEN", "value-from-the-process-environment")
    env = tp.enforce_slot_env("inventree", None)
    assert env is not None
    assert env["INVENTREE_TOKEN"] == "value-from-the-process-environment"
    assert env["MERIDIAN_INVENTREE_READ_ONLY"] == "1"


def test_other_slots_get_their_env_back_unchanged() -> None:
    env = {"A": "1"}
    assert tp.enforce_slot_env("latex", env) is env
    assert tp.enforce_slot_env("outputs", None) is None


def test_stored_config_never_keeps_the_inventree_token() -> None:
    raw = {
        "meridian-inventree": {
            "env": {"INVENTREE_TOKEN": "tok_secret_value_0123", "INVENTREE_URL": "http://127.0.0.1:8000"},
        },
    }
    cleaned = tp.sanitize_plugin_config_secrets(raw)
    env = cleaned["meridian-inventree"]["env"]
    assert "INVENTREE_TOKEN" not in env
    assert env["INVENTREE_URL"] == "http://127.0.0.1:8000"
    assert "tok_secret_value_0123" not in repr(cleaned)


def test_normalized_config_drops_the_token_whatever_its_case() -> None:
    normalized = tp.normalize_plugins_config(
        {"meridian-inventree": {"env": {"inventree_token": "tok_secret_value_0123", "INVENTREE_URL": "http://127.0.0.1:8000"}}}
    )
    env = normalized["meridian-inventree"]["env"]
    assert "inventree_token" not in {key.casefold() for key in env}
    assert "tok_secret_value_0123" not in repr(normalized)
    assert env["INVENTREE_URL"] == "http://127.0.0.1:8000"


def test_zotero_key_is_still_stripped() -> None:
    raw = {"zotero": {"env": {"ZOTERO_API_KEY": "zotero-key-value", "ZOTERO_LOCAL": "true"}}}
    env = tp.sanitize_plugin_config_secrets(raw)["zotero"]["env"]
    assert env == {"ZOTERO_LOCAL": "true"}


def test_routes_and_labels_match_the_other_slots() -> None:
    paths = {route.path for route in tunnel_routes.router.routes}
    assert "/tunnel-inventree/{tenant_id}" in paths
    assert "/inventree/mcp/{tenant_id}" in paths
    assert "/inventree/mcp/{tenant_id}/{rest:path}" in paths
    assert "inventree" in tunnel_routes._TUNNEL_LABELS
    assert tunnel_routes.SLOT_DISPLAY_NAMES["inventree"] == "meridian-inventree"
    sockets, pending = tunnel_routes._label_maps("inventree")
    assert sockets is tunnel_routes._tunnel_inventree_sockets
    assert pending is tunnel_routes._pending_inventree_reqs


def test_has_active_tunnel_sees_an_inventree_socket(monkeypatch) -> None:
    monkeypatch.setitem(tunnel_routes._tunnel_inventree_sockets, "tenant-inventree-test", object())
    assert tunnel_routes.has_active_tunnel("tenant-inventree-test") is True
