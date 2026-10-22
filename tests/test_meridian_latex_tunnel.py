import asyncio

import meridian.routes.tunnel as tunnel_routes
import meridian.setup_bundle as setup_bundle
import meridian.tunnel_plugins as tunnel_plugins
from meridian.tunnel_client import _ws_office_url
from meridian.tunnel_plugins import (
    DEFAULT_LATEX_PORT,
    _BUILTIN_DEFAULT_PORTS,
    _CUSTOM_PORT_START,
    extension_uvx_command,
    plugin_by_slot,
)


def test_docs_and_outputs_have_checkout_free_git_install_commands(tmp_path):
    assert extension_uvx_command("meridian-docs", repo_root=tmp_path) == [
        "uvx",
        "--from",
        "git+https://github.com/meridianmcp/Meridian.git"
        "#subdirectory=extensions/meridian-docs",
        "meridian-docs-mcp",
    ]
    assert extension_uvx_command("meridian-outputs", repo_root=tmp_path) == [
        "uvx",
        "--from",
        "git+https://github.com/meridianmcp/Meridian.git"
        "#subdirectory=extensions/meridian-outputs",
        "meridian-outputs-mcp",
    ]


def test_docs_and_outputs_keep_no_cache_for_local_source(tmp_path):
    for extension, entrypoint in (
        ("meridian-docs", "meridian-docs-mcp"),
        ("meridian-outputs", "meridian-outputs-mcp"),
    ):
        local = tmp_path / "extensions" / extension
        local.mkdir(parents=True)
        (local / "pyproject.toml").write_text("[project]\nname='test'\n")
        assert extension_uvx_command(extension, repo_root=tmp_path) == [
            "uvx", "--no-cache", "--from", str(local), entrypoint,
        ]


def test_latex_is_a_fixed_builtin_slot_with_mcp_entrypoint():
    plugin = plugin_by_slot(None, "latex")
    assert plugin is not None
    assert plugin["name"] == "meridian-latex"
    assert plugin["port"] == DEFAULT_LATEX_PORT == 8822
    assert plugin["url_prefix"] == "/latex"
    assert plugin["enabled"] is False
    assert DEFAULT_LATEX_PORT in _BUILTIN_DEFAULT_PORTS
    assert _CUSTOM_PORT_START == DEFAULT_LATEX_PORT + 1
    assert plugin["command"] == [
        "npx", "-y", "--package", "@meridianmcp/mcp", "meridian-latex", "mcp",
    ]


def test_latex_tunnel_routes_and_registries_are_connected():
    assert tunnel_routes._label_maps("latex") == (
        tunnel_routes._tunnel_latex_sockets,
        tunnel_routes._pending_latex_reqs,
    )
    assert tunnel_routes.SLOT_DISPLAY_NAMES["latex"] == "meridian-latex"
    routes = tunnel_routes.router.routes
    assert any(route.path == "/tunnel-latex/{tenant_id}" for route in routes)
    base_methods = {
        method
        for route in routes
        if route.path == "/latex/mcp/{tenant_id}"
        for method in (route.methods or set())
    }
    subpath_methods = {
        method
        for route in routes
        if route.path == "/latex/mcp/{tenant_id}/{rest:path}"
        for method in (route.methods or set())
    }
    assert {"GET", "POST", "OPTIONS"} <= base_methods
    assert {"GET", "POST", "OPTIONS"} <= subpath_methods


def test_latex_slot_is_reported_as_active():
    tenant_id = "test-latex-slot"
    tunnel_routes._tunnel_latex_sockets[tenant_id] = object()
    try:
        assert tunnel_routes.has_active_tunnel(tenant_id)
        assert tenant_id in tunnel_routes.active_tunnel_tenant_ids()
        status = asyncio.run(tunnel_routes.tunnel_status(tenant_id))
        assert status["latex_active"] is True
    finally:
        tunnel_routes._tunnel_latex_sockets.pop(tenant_id, None)


def test_latex_client_url_uses_registered_tunnel_route():
    url = _ws_office_url("https://usemeridian.us", "tenant-1", "token", "latex")
    assert url.startswith("wss://usemeridian.us/tunnel-latex/tenant-1?")


def test_setup_bundle_uses_checkout_free_latex_mcp_command(tmp_path):
    entries = setup_bundle._server_entries(
        tmp_path,
        "claude-code",
        meridian_mode="hosted",
        meridian_url="https://usemeridian.us",
        windows=True,
    )
    latex = entries[setup_bundle._server_name("latex", tmp_path)]
    assert latex["args"][-2:] == ["meridian-latex", "mcp"]


def test_setup_bundle_uses_monorepo_sources_without_extension_checkout(tmp_path, monkeypatch):
    fake_module = tmp_path / "installed" / "meridian" / "tunnel_plugins.py"
    monkeypatch.setattr(tunnel_plugins, "__file__", str(fake_module))
    entries = setup_bundle._server_entries(
        tmp_path,
        "claude-code",
        meridian_mode="hosted",
        meridian_url="https://usemeridian.us",
        windows=True,
    )
    docs = entries[setup_bundle._server_name("docs", tmp_path)]
    outputs = entries[setup_bundle._server_name("outputs", tmp_path)]
    assert docs["args"] == [
        "--from",
        "git+https://github.com/meridianmcp/Meridian.git"
        "#subdirectory=extensions/meridian-docs",
        "meridian-docs-mcp",
    ]
    assert outputs["args"] == [
        "--from",
        "git+https://github.com/meridianmcp/Meridian.git"
        "#subdirectory=extensions/meridian-outputs",
        "meridian-outputs-mcp",
    ]
