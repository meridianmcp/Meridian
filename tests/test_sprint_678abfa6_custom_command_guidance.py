"""Sprint 678abfa6: Tunnel Plugins custom-command guidance (pieces 1-2).

The custom-plugin command runs on the machine that runs ``meridian --tunnel``,
not on Meridian's servers. The Tunnel Plugins card now says so inline and shows
an OS-shaped example. Piece 3 (live path validation over the tunnel WebSocket)
is not covered here.
"""

from __future__ import annotations

from pathlib import Path

from dashboard_src import dashboard_source

_BUNDLE = Path(__file__).resolve().parent.parent / "meridian" / "static" / "dashboard.bundle.js"


def test_custom_command_says_it_runs_on_the_tunnel_machine():
    src = dashboard_source()
    assert "Where the command runs:" in src
    assert "on the machine running <code>meridian --tunnel</code>, not on Meridian's servers." in src
    # A server-container path (e.g. /app/...) is the failure mode behind the 97dc51ab incident.
    assert "/app/..." in src


def test_custom_command_example_is_os_shaped():
    src = dashboard_source()
    assert "const _cmdExamples" in src
    # The TS source escapes backslashes in the Windows example.
    assert r"'C:\\Users\\you\\tools\\my-server.exe --port 9000'" in src
    assert "'/Users/you/tools/my-server --port 9000'" in src
    assert "'/home/you/tools/my-server --port 9000'" in src
    for key in ("windows:", "macos:", "linux:"):
        assert key in src
    # The example follows the detected OS, falling back to Linux.
    assert "_cmdExamples[detectedOs] || _cmdExamples.linux" in src


def test_custom_command_input_placeholder_is_os_shaped():
    src = dashboard_source()
    assert 'id="tp-custom-command-${projectId}"' in src
    assert "uvx mcp-server-fetch or ${escapeHtml(_cmdEx.placeholder)}" in src


def test_served_bundle_carries_the_guidance():
    bundle = _BUNDLE.read_text(encoding="utf-8")
    assert "Where the command runs:" in bundle
    assert "not on Meridian's servers" in bundle
    assert "_cmdExamples" in bundle
