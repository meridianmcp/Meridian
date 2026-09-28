"""Tests for scripts/install_linux_launcher.sh (sprint item 52cdabaa).

A .desktop application-menu launcher + optional systemd --user autostart
unit for Linux -- deliberately NOT an AppImage or .deb/.rpm package (see the
script's own module-level comment and the sprint item notes for the
rationale). This mirrors how the rest of this repo's shell installers
(install_tunnel.sh, install_watcher.sh) are tested: static source-content
assertions rather than actually executing the script, since the full test
suite also runs on Windows dev machines with no real systemd/desktop
environment to install anything into.

The .desktop and systemd-unit "well-formed" checks go one step further than
a bare substring match: the heredoc content is extracted from the script
source and parsed with configparser (the .desktop / systemd-unit format is
plain INI), so a genuinely malformed file (missing a required key, broken
section header, ...) would fail the parse or the key assertions -- not just
a keyword search.
"""
from __future__ import annotations

import configparser
import re
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "install_linux_launcher.sh"
)


def _src() -> str:
    return _SCRIPT_PATH.read_text(encoding="utf-8")


def _extract_heredoc(src: str, write_marker: str) -> str:
    """Pull the body of a `cat > "$X" << EOF ... EOF` heredoc out of the
    script source. `write_marker` disambiguates which `cat > ... << EOF`
    call to extract when a script writes more than one file (this script
    writes both the .desktop entry and the systemd unit)."""
    start = src.index(write_marker)
    heredoc_start = src.index("<< EOF", start) + len("<< EOF")
    # Body runs to the next line that is exactly "EOF".
    end_match = re.search(r"\nEOF\b", src[heredoc_start:])
    assert end_match, f"could not find closing EOF for heredoc at {write_marker!r}"
    body = src[heredoc_start : heredoc_start + end_match.start()]
    return body.strip("\n")


def _parse_ini(body: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read_string(body)
    return parser


class TestDesktopEntryWellFormed:
    def test_desktop_entry_parses_as_valid_ini(self):
        body = _extract_heredoc(_src(), 'cat > "$DESKTOP_FILE"')
        parser = _parse_ini(body)
        assert parser.sections() == ["Desktop Entry"]

    def test_desktop_entry_has_required_keys(self):
        body = _extract_heredoc(_src(), 'cat > "$DESKTOP_FILE"')
        parser = _parse_ini(body)
        entry = parser["Desktop Entry"]
        # Required/expected keys per the freedesktop.org Desktop Entry spec
        # for a launchable Type=Application entry.
        for key in ("Type", "Name", "Exec", "Icon"):
            assert key in entry, f"missing required .desktop key: {key}"
        assert entry["Type"] == "Application"
        assert entry["Name"] == "Meridian"

    def test_desktop_entry_exec_invokes_the_meridian_entry_point(self):
        body = _extract_heredoc(_src(), 'cat > "$DESKTOP_FILE"')
        parser = _parse_ini(body)
        # Exec must reference the resolved `meridian` binary (which itself
        # dispatches to the existing `python -m meridian` entry point --
        # meridian.__main__:main via the console-script in pyproject.toml),
        # not some new, separate binary this item would have had to build.
        assert parser["Desktop Entry"]["Exec"] == "${MERIDIAN_BIN}"
        src = _src()
        assert 'MERIDIAN_BIN="$(command -v meridian' in src

    def test_desktop_entry_terminal_is_false(self):
        body = _extract_heredoc(_src(), 'cat > "$DESKTOP_FILE"')
        parser = _parse_ini(body)
        assert parser["Desktop Entry"]["Terminal"] == "false"

    def test_desktop_file_written_with_readable_permissions(self):
        src = _src()
        assert 'chmod 644 "$DESKTOP_FILE"' in src


class TestIconResolutionIsBestEffort:
    def test_icon_lookup_never_hard_fails_on_missing_icon(self):
        src = _src()
        # The python3 icon-lookup subshell is guarded with `|| true`, and a
        # missing/failed lookup falls back to a generic themed icon name --
        # a missing icon must never abort the whole install.
        assert "python3 - <<'PYEOF' 2>/dev/null || true" in src
        assert 'ICON_VALUE="utilities-terminal"' in src

    def test_icon_source_resolved_via_installed_meridian_package(self):
        src = _src()
        assert "import meridian" in src
        assert "icon-512.png" in src

    def test_bundled_icon_actually_exists_in_repo(self):
        # Sanity check on the asset path referenced above -- if this ever
        # moves, the fallback branch would silently become the only path
        # taken and this test should catch that.
        icon_path = (
            Path(__file__).resolve().parent.parent
            / "meridian"
            / "static"
            / "icon-512.png"
        )
        assert icon_path.is_file()


class TestSystemdUnitWellFormed:
    def test_systemd_unit_parses_as_valid_ini(self):
        body = _extract_heredoc(_src(), 'cat > "$SERVICE_FILE"')
        parser = _parse_ini(body)
        assert set(parser.sections()) == {"Unit", "Service", "Install"}

    def test_systemd_unit_has_required_keys(self):
        body = _extract_heredoc(_src(), 'cat > "$SERVICE_FILE"')
        parser = _parse_ini(body)
        assert "Description" in parser["Unit"]
        assert parser["Service"]["ExecStart"] == "${MERIDIAN_BIN}"
        assert parser["Service"]["Restart"] == "on-failure"
        assert parser["Install"]["WantedBy"] == "default.target"

    def test_systemd_unit_is_gated_behind_autostart_opt_in(self):
        src = _src()
        assert 'if [ "${MERIDIAN_AUTOSTART:-0}" = "1" ]; then' in src
        # Not enabling autostart must still succeed (installing just the
        # .desktop entry), with a clear hint on how to opt in later.
        assert "MERIDIAN_AUTOSTART=1" in src

    def test_systemd_unit_enable_sequence_matches_existing_installers(self):
        src = _src()
        # Same activation sequence install_tunnel.sh/install_watcher.sh use.
        assert "systemctl --user daemon-reload" in src
        assert "systemctl --user enable meridian.service" in src
        assert "systemctl --user start meridian.service" in src

    def test_uninstall_instructions_are_printed(self):
        src = _src()
        assert "systemctl --user disable --now meridian" in src


class TestInstallScriptLogic:
    """Static checks mirroring tests/test_install_script.py's approach for
    the .ps1 installers: assert on install-script SOURCE structure/order
    rather than actually executing OS-level install calls (no real
    systemd/desktop environment is guaranteed in the test runner)."""

    def test_is_linux_only(self):
        src = _src()
        assert 'OS="$(uname -s)"' in src
        assert "Linux*) : ;;" in src
        # Non-Linux must exit non-zero with guidance to the OS-appropriate
        # installer, not silently attempt a Linux-specific install.
        guard_idx = src.index('case "$OS" in')
        exit_idx = src.index("exit 1", guard_idx)
        assert exit_idx > guard_idx
        guard_block = src[guard_idx:exit_idx]
        assert "install_tunnel.sh" in guard_block
        assert "install-windows.ps1" in guard_block

    def test_errors_when_meridian_not_on_path(self):
        src = _src()
        assert 'MERIDIAN_BIN="$(command -v meridian || true)"' in src
        assert 'if [ -z "$MERIDIAN_BIN" ]; then' in src
        guard_idx = src.index('if [ -z "$MERIDIAN_BIN" ]; then')
        exit_idx = src.index("exit 1", guard_idx)
        assert exit_idx > guard_idx, "must hard-fail before attempting to write any files"
        # And that guard must run before the .desktop file is written.
        write_idx = src.index('cat > "$DESKTOP_FILE"')
        assert exit_idx < write_idx

    def test_set_euo_pipefail_present(self):
        # Matches install_tunnel.sh/install_watcher.sh's own strict-mode header.
        assert "set -euo pipefail" in _src()

    def test_explicitly_not_appimage_or_deb_rpm(self):
        """Documents the deliberate scope boundary from the sprint item:
        no AppImage/.deb/.rpm packaging machinery is introduced here."""
        src = _src().lower()
        assert "appimage" in src
        assert ".deb/.rpm" in src or (".deb" in src and ".rpm" in src)
        # And, crucially, none of that packaging machinery is actually
        # invoked anywhere in the script.
        for forbidden in ("appimagetool", "dpkg-deb", "rpmbuild", "fpm "):
            assert forbidden not in _src().lower()

    def test_update_desktop_database_refresh_is_best_effort(self):
        src = _src()
        assert "update-desktop-database" in src
        idx = src.index("if command -v update-desktop-database")
        block = src[idx : idx + 200]
        assert "|| true" in block


class TestRoutePresenceIsExplicitlyOutOfScope:
    """This item deliberately does NOT wire an HTTP route (unlike
    install_tunnel.sh/install_watcher.sh's /install_tunnel.sh and
    /install_watcher.sh routes in meridian/routes/hooks.py) -- that would
    touch a shared routing file outside this item's claimed
    touches_resources. Documented here so the gap is visible rather than
    silently assumed-done; see the sprint item completion notes."""

    def test_script_file_exists_and_is_executable_shebang(self):
        assert _SCRIPT_PATH.is_file()
        first_line = _src().splitlines()[0]
        assert first_line == "#!/usr/bin/env bash"


@pytest.mark.parametrize(
    "cat_marker",
    ['cat > "$DESKTOP_FILE"', 'cat > "$SERVICE_FILE"'],
)
def test_heredocs_do_not_leak_unresolved_double_dollar_braces(cat_marker):
    """Every ${VAR} in the written files must be a real shell variable this
    script defines somewhere above the heredoc (not a typo'd/undefined
    reference that would end up littering the installed file with an empty
    string at runtime)."""
    src = _src()
    body = _extract_heredoc(src, cat_marker)
    refs = set(re.findall(r"\$\{(\w+)\}", body))
    preamble = src[: src.index(cat_marker)]
    for ref in refs:
        assert re.search(rf"\b{ref}=", preamble), f"${{{ref}}} used but never assigned before {cat_marker!r}"
