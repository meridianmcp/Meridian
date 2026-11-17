"""The bridge token file is readable by its owner only on Windows, and an install fails rather than leave it open."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from meridian import zotero_bridge_api as bridge


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("MERIDIAN_LOCAL_RUNNER_STATE_DIR", str(tmp_path / "state"))


def test_on_windows_the_token_is_restricted_to_its_owner_with_icacls(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(bridge.sys, "platform", "win32")
    monkeypatch.setattr(bridge.getpass, "getuser", lambda: "alice")

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    path = tmp_path / "api_token"
    assert bridge.ensure_api_token(path) == bridge.TOKEN_CREATED
    assert calls == [["icacls", str(path), "/inheritance:r", "/grant:r", "alice:F"]]


def test_an_existing_token_is_restricted_again_so_a_rerun_repairs_the_acl(monkeypatch, tmp_path):
    path = tmp_path / "api_token"
    bridge.ensure_api_token(path)
    calls = []
    monkeypatch.setattr(bridge.sys, "platform", "win32")
    monkeypatch.setattr(bridge.getpass, "getuser", lambda: "alice")
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    assert bridge.ensure_api_token(path) == bridge.TOKEN_KEPT
    assert len(calls) == 1 and calls[0][0] == "icacls"


def test_off_windows_no_icacls_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge.sys, "platform", "linux")

    def never(argv, **kwargs):
        raise AssertionError("icacls must not run off Windows")

    monkeypatch.setattr(bridge.subprocess, "run", never)
    assert bridge.ensure_api_token(tmp_path / "api_token") == bridge.TOKEN_CREATED


def test_an_icacls_failure_fails_the_install_and_says_why(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge.sys, "platform", "win32")
    monkeypatch.setattr(bridge.getpass, "getuser", lambda: "alice")
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=5, stdout="", stderr="Access is denied."),
    )
    with pytest.raises(OSError, match="Could not restrict the bridge token file to alice: Access is denied"):
        bridge.ensure_api_token(tmp_path / "api_token")


@pytest.mark.skipif(sys.platform != "win32", reason="icacls is a Windows tool")
def test_on_windows_icacls_really_leaves_the_owner_and_no_other_account(tmp_path):
    path = tmp_path / "api_token"
    bridge.ensure_api_token(path)
    listing = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True).stdout
    # icacls prints the file's own path first, and that path contains "Users". Only the access entries are checked.
    listing = listing.replace(str(path), "")
    assert "Everyone" not in listing
    assert "Users" not in listing
    assert "CodexSandbox" not in listing
    assert "SYSTEM" not in listing
