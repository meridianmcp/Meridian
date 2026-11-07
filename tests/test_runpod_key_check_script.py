"""Tests for scripts/runpod_key_check.py, the read-only RunPod key check an agent shell runs locally.

Nothing here touches the real keyring or the network. The keyring is a fake module put
in sys.modules, and the RunPod GET is a fake callable or a fake httpx client. SENTINEL
and OTHER stand in for keys and must never appear in any output line.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import httpx
import pytest

SENTINEL = "SENTINEL-RUNPOD-KEY-DO-NOT-LEAK-5c1e"
OTHER = "OTHER-KEYRING-KEY-DO-NOT-LEAK-9d2b"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "runpod_key_check.py"


@pytest.fixture(scope="module")
def check():
    spec = importlib.util.spec_from_file_location("runpod_key_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)


def _fake_keyring(monkeypatch, value):
    """Install a fake keyring module that returns ``value``. Returns the (service, account) pairs it was asked for."""
    asked: list[tuple[str, str]] = []

    def get_password(service, account):
        asked.append((service, account))
        return value

    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(get_password=get_password))
    return asked


def _text(lines):
    return "\n".join(lines)


def _never_called(secret):
    raise AssertionError("no request may be made in this case")


def test_sources_are_the_env_var_then_the_desktop_keychain_entry_and_never_runpodctl(check):
    refs = [ref for _label, ref in check.SOURCES]

    assert refs == ["env:RUNPOD_API_KEY", "keyring:meridian-desktop/runpod-api-key"]
    assert not any("runpodctl" in ref for ref in refs)


def test_the_pods_url_is_the_runpod_v2_listing(check):
    assert check.PODS_URL == "https://api.runpod.io/v2/pods"


def test_keychain_reference_reads_the_desktop_service_and_account(check, monkeypatch):
    # Pins the desktop contract: meridian_desktop/secret_store.py writes service
    # "meridian-desktop" and account "runpod-api-key", and the core must read that pair.
    asked = _fake_keyring(monkeypatch, SENTINEL)

    code, _lines = check.run_check(get_status=lambda secret: 200)

    assert asked == [("meridian-desktop", "runpod-api-key")]
    assert code == 0


def test_a_key_in_the_keychain_is_probed_once_and_accepted(check, monkeypatch):
    _fake_keyring(monkeypatch, SENTINEL)
    probed: list[str] = []

    def get(secret):
        probed.append(secret)
        return 200

    code, lines = check.run_check(get_status=get)
    text = _text(lines)

    assert code == 0
    assert probed == [SENTINEL]
    assert "environment variable RUNPOD_API_KEY: not found" in text
    assert "keychain entry meridian-desktop / runpod-api-key: present" in text
    assert "Key used: keychain entry meridian-desktop / runpod-api-key." in text
    assert "RunPod API: HTTP 200, key accepted." in text
    assert SENTINEL not in text


def test_the_environment_key_is_used_first_and_the_keychain_is_still_reported(check, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", SENTINEL)
    _fake_keyring(monkeypatch, OTHER)
    probed: list[str] = []

    def get(secret):
        probed.append(secret)
        return 200

    code, lines = check.run_check(get_status=get)
    text = _text(lines)

    assert code == 0
    assert probed == [SENTINEL]
    assert "environment variable RUNPOD_API_KEY: present" in text
    assert "keychain entry meridian-desktop / runpod-api-key: present" in text
    assert "Key used: environment variable RUNPOD_API_KEY." in text
    assert SENTINEL not in text
    assert OTHER not in text


def test_no_key_in_any_source_makes_no_request_and_exits_1(check, monkeypatch):
    _fake_keyring(monkeypatch, None)

    code, lines = check.run_check(get_status=_never_called)
    text = _text(lines)

    assert code == 1
    assert "environment variable RUNPOD_API_KEY: not found" in text
    assert "keychain entry meridian-desktop / runpod-api-key: not found" in text
    assert "RunPod API: not called" in text


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_a_refused_key_reports_the_http_code_and_exits_1(check, monkeypatch, status):
    _fake_keyring(monkeypatch, SENTINEL)

    code, lines = check.run_check(get_status=lambda secret: status)
    text = _text(lines)

    assert code == 1
    assert f"RunPod API: HTTP {status}, key not accepted." in text
    assert SENTINEL not in text


def test_a_transport_failure_reports_no_http_response_and_never_the_exception_text(check, monkeypatch):
    _fake_keyring(monkeypatch, SENTINEL)

    def unreachable(secret):
        raise httpx.ConnectError(f"could not reach the host while sending {SENTINEL}")

    code, lines = check.run_check(get_status=unreachable)
    text = _text(lines)

    assert code == 1
    assert "RunPod API: no HTTP response" in text
    assert SENTINEL not in text


def test_an_unexpected_error_reports_only_its_class_name(check, monkeypatch):
    _fake_keyring(monkeypatch, SENTINEL)

    def broken(secret):
        raise RuntimeError(f"leaked {SENTINEL}")

    code, lines = check.run_check(get_status=broken)
    text = _text(lines)

    assert code == 1
    assert "request failed before a response (RuntimeError)" in text
    assert SENTINEL not in text


def test_a_stored_value_that_is_not_a_usable_key_is_refused_before_any_request(check, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", f"value with spaces {SENTINEL}")

    code, lines = check.run_check(get_status=_never_called)
    text = _text(lines)

    assert code == 1
    assert "RunPod API: not called" in text
    assert "not a usable API key" in text
    assert SENTINEL not in text
    assert "value with spaces" not in text


def test_a_redirect_is_reported_as_not_accepted_and_is_not_followed(check, monkeypatch):
    _fake_keyring(monkeypatch, SENTINEL)

    code, lines = check.run_check(get_status=lambda secret: 302)

    assert code == 1
    assert "RunPod API: HTTP 302, key not accepted." in _text(lines)


def test_http_probe_is_one_get_to_the_pod_listing_with_no_redirects(check, monkeypatch):
    seen: dict[str, object] = {"calls": []}

    class FakeClient:
        def __init__(self, *, timeout, follow_redirects):
            seen["init"] = (timeout, follow_redirects)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def get(self, url, *, params, headers):
            seen["calls"].append(("GET", url, params))  # type: ignore[union-attr]
            seen["auth"] = headers["Authorization"]
            return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(check.httpx, "Client", FakeClient)

    assert check.http_get_status(SENTINEL) == 200
    assert seen["calls"] == [("GET", check.PODS_URL, {"limit": 1})]
    assert seen["init"] == (check.TIMEOUT_SECONDS, False)
    assert seen["auth"] == f"Bearer {SENTINEL}"


def test_key_shape_matches_the_client_rule(check):
    assert check.key_shape_ok("abc123-_.")
    assert not check.key_shape_ok("has space")
    assert not check.key_shape_ok("tab\tinside")
    assert not check.key_shape_ok("non-ascii-é")
    assert not check.key_shape_ok("bell\x07")


def test_main_prints_the_status_and_returns_the_exit_code_without_the_key(check, monkeypatch, capsys):
    _fake_keyring(monkeypatch, SENTINEL)
    monkeypatch.setattr(check, "http_get_status", lambda secret: 200)

    exit_code = check.main()
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "RunPod API: HTTP 200, key accepted." in out
    assert SENTINEL not in out
