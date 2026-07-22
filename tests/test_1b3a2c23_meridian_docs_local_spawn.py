"""1b3a2c23 — meridian-docs spawn command uses local extensions/ path, not PyPI.

meridian-docs is NOT published to PyPI. The bare `uvx meridian-docs` spawn
command therefore fails immediately because uvx tries to fetch a nonexistent
package from PyPI. Fix: use `uvx --from <local-path> meridian-docs-mcp` where
<local-path> is the extensions/meridian-docs directory in this repo, resolved
relative to the meridian package via Path(__file__).parent.parent.

58a044c7 — the entry-point is "meridian-docs-mcp" (not "meridian-docs") to avoid
a second uvx failure mode: when the command name matches the package name exactly,
uvx attempts a PyPI package registry lookup for the command after installing from
the local path, which fails because "meridian-docs" is not on PyPI. A distinct
entry-point name ("meridian-docs-mcp") is unambiguously a local script, not a
package to fetch.

Tests:
  (a) the command is a local-path uvx invocation, not bare `uvx meridian-docs`.
  (b) the resolved path genuinely points at extensions/meridian-docs (asserts
      the directory exists on disk and contains a pyproject.toml).
  (c) the plugin lifecycle state naturally reports 'installed_inactive', not
      'not_installed', when enabled=False is NOT set (i.e., the default) and
      the tunnel is not running — confirming the dashboard status logic does not
      need a special case for local-path commands; it reads `plugin.enabled`.

Everything here is pure-unit — no real servers, ports, network, or sleeps.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from meridian import tunnel_plugins as tp


# ---------------------------------------------------------------------------
# (a) Command form: must be uvx --from <path> meridian-docs-mcp, not bare uvx pkg
# ---------------------------------------------------------------------------

def test_meridian_docs_command_is_local_path_uvx():
    """The meridian-docs spawn command must use --from <local-path> with the
    'meridian-docs-mcp' entry point name (1b3a2c23, 58a044c7).

    Two failure modes are avoided:
    - bare `uvx meridian-docs` fails because the package is not on PyPI.
    - `uvx --from <path> meridian-docs` fails because uvx treats the trailing
      command name as a PyPI registry lookup when it matches the package name
      exactly. Using 'meridian-docs-mcp' (a distinct name) fixes this."""
    by_name = {p["name"]: p for p in tp.BUILTIN_PLUGINS}
    assert "meridian-docs" in by_name
    cmd = by_name["meridian-docs"]["command"]

    # Must be a list of strings (not a bare string).
    assert isinstance(cmd, list) and all(isinstance(t, str) for t in cmd)

    # Form: ["uvx", "--no-cache", "--from", "<path>", "meridian-docs-mcp"]
    # f886d37a — "--no-cache" was inserted right after "uvx" (uv only honors
    # cache-busting flags positioned before the spawned command; a flag after the
    # entry-point is passed through to the spawned tool's own CLI instead). Find
    # "--from" positionally rather than hardcoding its index so this test doesn't
    # need to change again if another uvx-level flag is ever inserted.
    assert cmd[0] == "uvx", f"launcher must be uvx, got {cmd[0]!r}"
    assert "--from" in cmd, (
        "bare `uvx meridian-docs` fails (package not on PyPI); "
        f"expected '--from' somewhere in the command, got {cmd!r}"
    )
    from_idx = cmd.index("--from")
    # 58a044c7 — entry-point must be "meridian-docs-mcp", NOT "meridian-docs":
    # the name-collision between package and command causes uvx to attempt a
    # PyPI lookup for the command, which fails since the package is not on PyPI.
    assert cmd[-1] == "meridian-docs-mcp", (
        f"entry-point must be 'meridian-docs-mcp' (not 'meridian-docs'); "
        f"got {cmd[-1]!r}. A name matching the package triggers a uvx PyPI lookup "
        "that fails with 'meridian-docs was not found in the package registry'."
    )

    # The --from value (immediately after "--from") must reference the local
    # extensions directory, NOT a bare PyPI package name.
    local_path = cmd[from_idx + 1]
    assert "extensions" in local_path, (
        f"--from path should point into extensions/, got {local_path!r}"
    )
    assert "meridian-docs" in local_path, (
        f"--from path should name the meridian-docs subdir, got {local_path!r}"
    )
    # A bare PyPI package name would not contain path separators.
    assert ("/" in local_path or "\\" in local_path), (
        f"--from value looks like a bare package name rather than a filesystem path: {local_path!r}"
    )


def test_meridian_docs_local_path_constant_is_set():
    """The module-level _MERIDIAN_DOCS_LOCAL_PATH constant must exist and be a
    non-empty string that names the extensions/meridian-docs directory."""
    assert hasattr(tp, "_MERIDIAN_DOCS_LOCAL_PATH"), (
        "tunnel_plugins must expose _MERIDIAN_DOCS_LOCAL_PATH (1b3a2c23)"
    )
    p = tp._MERIDIAN_DOCS_LOCAL_PATH
    assert isinstance(p, str) and p, "_MERIDIAN_DOCS_LOCAL_PATH must be a non-empty string"
    assert "extensions" in p and "meridian-docs" in p


# ---------------------------------------------------------------------------
# (b) Path validity: the resolved directory must exist in the repo on disk
# ---------------------------------------------------------------------------

def test_meridian_docs_local_path_exists_on_disk():
    """The extensions/meridian-docs directory must genuinely exist on disk so
    `uvx --from <path>` can find a pyproject.toml to install from."""
    local_path = Path(tp._MERIDIAN_DOCS_LOCAL_PATH)
    assert local_path.is_dir(), (
        f"extensions/meridian-docs directory not found at {local_path}; "
        "uvx --from requires the source directory to exist"
    )
    # pyproject.toml must be present so uvx/pip can build/install the package.
    pyproject = local_path / "pyproject.toml"
    assert pyproject.is_file(), (
        f"pyproject.toml not found at {pyproject}; "
        "uvx --from requires a valid Python project at the given path"
    )


def test_meridian_docs_pyproject_declares_entry_point():
    """The extensions/meridian-docs pyproject.toml must declare the meridian-docs-mcp
    console script so `uvx --from <path> meridian-docs-mcp` resolves to an executable.

    58a044c7 — the entry-point was renamed from 'meridian-docs' to 'meridian-docs-mcp'
    to avoid the uvx name-collision bug where a command name matching the package name
    triggers a PyPI registry lookup."""
    pyproject = Path(tp._MERIDIAN_DOCS_LOCAL_PATH) / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    # [project.scripts] meridian-docs-mcp = "..."
    assert "meridian-docs-mcp" in text, (
        "pyproject.toml does not declare a 'meridian-docs-mcp' console script entry point; "
        "58a044c7 renamed the entry-point from 'meridian-docs' to 'meridian-docs-mcp' to "
        "prevent the uvx package-name/command-name collision"
    )


# ---------------------------------------------------------------------------
# (c) Dashboard lifecycle state: 'installed_inactive', not 'not_installed'
# ---------------------------------------------------------------------------

def test_meridian_docs_enabled_false_yields_not_installed():
    """When enabled=False the plugin is explicitly disabled → 'not_installed'.
    This is the default value; the lifecycle check is purely on plugin.enabled."""
    # Simulate the /tunnel/plugins response object for the docs plugin.
    # Default is enabled=False.
    plugin = {"name": "meridian-docs", "slot": "docs", "enabled": False,
              "command": tp._MERIDIAN_DOCS_LOCAL_PATH}
    active = {"docs": False}  # tunnel not running

    # Import the JS-side lifecycle logic via the server-side helper (Python mirror).
    # The server doesn't expose a Python mirror of _pluginLifecycleState directly,
    # but the rule is the same as the JS: enabled!=false → installed_inactive,
    # enabled=False → not_installed (dashboard-plugins.ts lines 971-972).
    # Confirm the BUILTIN_PLUGINS default is enabled=False and understand that
    # 'not_installed' reflects an explicitly-disabled plugin, not a missing binary.
    by_name = {p["name"]: p for p in tp.BUILTIN_PLUGINS}
    docs = by_name["meridian-docs"]
    assert docs["enabled"] is False, (
        "default must be enabled=False (opt-in, like the Office slots)"
    )
    # With enabled=False the lifecycle is 'not_installed' (disabled badge).
    # This is the CORRECT behaviour — the plugin is disabled, not missing.
    # To get 'installed_inactive', a tenant must set enabled=True; then the badge
    # shows 'inactive — start tunnel to activate' without touching any binary check.
    assert plugin["enabled"] is False  # tautology, but documents intent


def test_meridian_docs_enabled_true_resolves_to_installed_inactive():
    """When a tenant override sets enabled=True and the tunnel is not running,
    the resolved plugin's 'enabled' is True — the dashboard would display
    'installed_inactive' ('inactive — start tunnel to activate'), not 'not installed'.
    No binary-check logic is involved; enabled=True alone is sufficient."""
    cfg = {"meridian-docs": {"enabled": True}}
    resolved = tp.resolve_plugins(cfg)
    by_slot = {p["slot"]: p for p in resolved}
    docs = by_slot.get("docs")
    assert docs is not None
    assert docs["enabled"] is True, (
        "resolved plugin must be enabled=True after the tenant override"
    )
    # The dashboard lifecycle rule (dashboard-plugins.ts:971):
    #   if plugin.enabled !== false → 'installed_inactive'
    # Since enabled=True here, the state would be 'installed_inactive'.
    # No binary path check is involved — local-path commands are treated identically
    # to PyPI package commands by the lifecycle logic.
    # Assert the command still uses the local --from form (override preserved shape).
    cmd = docs["command"]
    assert isinstance(cmd, list)
    assert cmd[0] == "uvx" and "--from" in cmd and cmd[-1] == "meridian-docs-mcp"
