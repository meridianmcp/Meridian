"""Accuracy checks for the install docs in README.md and docs/quickstart.md
(sprint item 87c38570).

Static content assertions, matching the other docs and installer tests in this
repo. Each block pins a claim the docs now make to the source fact it was
verified against:

* release assets are unsigned and only checksummed (.github/workflows/release.yml)
* meridian.exe / meridian-linux / meridian-mac-arm64 are the tunnel client;
  a frozen build with no mode flag defaults to --tunnel (meridian/__main__.py)
* the dashboard server listens on port 7878 by default (meridian/__main__.py)
* Claude Code's user-scope MCP config lives in ~/.claude.json (meridian/cbm_registry.py)
* the local HTTP MCP endpoint is POST /mcp (meridian/server.py)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
QUICKSTART = REPO / "docs" / "quickstart.md"
RELEASE_WORKFLOW = REPO / ".github" / "workflows" / "release.yml"
MAIN_PY = REPO / "meridian" / "__main__.py"
SERVER_PY = REPO / "meridian" / "server.py"
CBM_REGISTRY_PY = REPO / "meridian" / "cbm_registry.py"

RELEASE_ASSETS = (
    "meridian.exe",
    "meridian-tray.exe",
    "meridian-tray-mac.dmg",
    "meridian-linux",
    "meridian-mac-arm64",
    "SHA256SUMS",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def readme() -> str:
    return _read(README)


@pytest.fixture(scope="module")
def quickstart() -> str:
    return _read(QUICKSTART)


def test_release_assets_are_unsigned_and_checksummed_only():
    workflow = _read(RELEASE_WORKFLOW)
    assert "SHA256SUMS" in workflow
    # No real signing or notarization command runs in the release workflow.
    assert not re.search(r"\b(codesign|signtool|cosign|notarytool|altool)\b", workflow)


def test_readme_and_quickstart_name_the_real_release_assets(readme, quickstart):
    workflow = _read(RELEASE_WORKFLOW)
    for asset in RELEASE_ASSETS:
        assert asset in workflow, f"{asset} is not produced by release.yml"
    assert "meridian-tray.exe" in quickstart
    assert "meridian-tray-mac.dmg" in quickstart
    assert "SHA256SUMS" in readme


def test_readme_no_longer_claims_signed_binaries_or_stale_version(readme):
    assert "Signed binaries" not in readme
    assert "current: v0.2.6" not in readme
    assert "unsigned" in readme


def test_binary_downloads_are_not_documented_as_the_dashboard(readme, quickstart):
    # Frozen builds with no mode flag run the tunnel client (see __main__.py),
    # so no install doc may tell users to run them as the dashboard.
    main_src = _read(MAIN_PY)
    assert "def _frozen_default_to_tunnel" in main_src
    assert "args.tunnel = True" in main_src
    assert "./meridian" not in quickstart
    assert "uv tool install meridian-server\nmeridian\n" in readme


def test_dashboard_port_is_7878_as_documented():
    main_src = _read(MAIN_PY)
    assert 'os.environ.get("MERIDIAN_PORT", "7878")' in main_src
    assert "http://127.0.0.1:7878/" in _read(REPO / "README.md")
    assert "http://127.0.0.1:7878/" in _read(QUICKSTART)


def test_no_stale_data_dir_or_claude_config_path(readme, quickstart):
    assert "~/.meridian/" not in readme
    assert "~/.claude/mcp.json" not in quickstart
    # The documented user-scope config path is the one Meridian itself reads.
    assert "/.claude.json" in _read(CBM_REGISTRY_PY)
    assert "~/.claude.json" in readme
    assert "~/.claude.json" in quickstart


def test_local_http_mcp_endpoint_is_documented_and_served(readme, quickstart):
    assert '@app.post("/mcp")' in _read(SERVER_PY)
    assert "http://127.0.0.1:7878/mcp" in readme
    assert "http://127.0.0.1:7878/mcp" in quickstart


def test_claude_ai_onboarding_matches_browser_connector_guide(readme):
    # claude.ai connectors cannot reach http://localhost; the hosted connector
    # needs no extension (docs/browser-connector.md).
    assert "usemeridian.us/install-mcp" not in readme
    assert "https://usemeridian.us/mcp" in readme
    assert "browser connector guide" in readme
    assert (REPO / "docs" / "browser-connector.md").is_file()


def test_readme_anchor_pairs_are_balanced(readme):
    starts = re.findall(r"<!-- MERIDIAN:ANCHOR:START ([\w-]+) -->", readme)
    ends = re.findall(r"<!-- MERIDIAN:ANCHOR:END ([\w-]+) -->", readme)
    assert starts, "README lost its MERIDIAN:ANCHOR markers"
    assert sorted(starts) == sorted(ends)
