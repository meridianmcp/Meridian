"""Regression tests for the CodeQL clear-text logging/storage and stack-trace fixes.

* Config writes spawn no subprocess. A token-bearing config's temp file is owner-only
  from creation on POSIX, and a temp file that cannot be created leaves the target alone.
* Tunnel client stderr lines mask bearer-token-shaped text.
* Error text returned to HTTP callers is a fixed string, and the exception is logged
  instead: admin git status, notes, pkg-guard, tunnel fs-root helpers, 429 detail.
* The pkg-guard registry lookups fail open with a fixed message.
* local_ingest writes the bearer-token header file with O_EXCL and mode 0600.
* _deps logs a redacted tenant id when a tenant's DB URL cannot be decrypted.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import stat
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parents[1]
_CONNECT_PY = _REPO / "scripts" / "meridian_connect.py"
_DOCS_EXT = _REPO / "extensions" / "meridian-docs"

# Fixture values only: they match the token shape redact() masks.
_FAKE_TOKEN = "sk_meridian_faketoken_not_real_0000"  # noqa: S105
_FAKE_TENANT_TOKEN = "sk_meridian_codeqlfixture0000000000"  # noqa: S105


def _load_connect():
    spec = importlib.util.spec_from_file_location("meridian_connect_codeql_fix", _CONNECT_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Config writes: no subprocess, owner-only temp file, fail closed
# ---------------------------------------------------------------------------

def test_private_write_keeps_line_endings_and_is_owner_only(tmp_path):
    connect = _load_connect()
    target = tmp_path / "cfg.toml"

    connect._write_text_atomic(target, "a = 1\r\nb = 2\n", private=True)

    assert target.read_bytes() == b"a = 1\r\nb = 2\n"
    assert not (tmp_path / "cfg.toml.meridian-tmp").exists()
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def test_private_temp_file_is_created_with_o_excl_and_mode_0600(tmp_path, monkeypatch):
    connect = _load_connect()
    seen: dict = {}
    real_open = os.open

    def _recording_open(path, flags, mode=0o777, *args, **kwargs):
        if str(path).endswith(".meridian-tmp"):
            seen["flags"], seen["mode"] = flags, mode
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(connect.os, "open", _recording_open)

    connect._write_text_atomic(tmp_path / "cfg.toml", "x = 1\n", private=True)

    assert seen["flags"] & os.O_EXCL and seen["flags"] & os.O_CREAT
    assert seen["mode"] == 0o600


def test_plain_write_does_not_use_the_private_temp_file_path(tmp_path, monkeypatch):
    connect = _load_connect()

    def _never(*_a, **_k):
        raise AssertionError("a non-private config write must keep the original code path")

    monkeypatch.setattr(connect.os, "open", _never)
    target = tmp_path / "settings.json"

    connect._write_text_atomic(target, "{}\n")

    assert target.read_text(encoding="utf-8") == "{}\n"


@pytest.mark.parametrize("private", [False, True])
def test_config_writes_never_spawn_a_subprocess(tmp_path, monkeypatch, private):
    connect = _load_connect()

    def _never(*_a, **_k):
        raise AssertionError("config writes must not spawn icacls or any other subprocess")

    monkeypatch.setattr(connect.subprocess, "run", _never)
    target = tmp_path / "settings.json"

    connect._write_text_atomic(target, "{}\n", private=private)

    assert target.read_text(encoding="utf-8") == "{}\n"


@pytest.mark.parametrize("private", [False, True])
def test_write_replaces_a_stale_temp_file(tmp_path, private):
    connect = _load_connect()
    target = tmp_path / "cfg.toml"
    target.write_text("old\n", encoding="utf-8")
    (tmp_path / "cfg.toml.meridian-tmp").write_text("stale leftover\n", encoding="utf-8")

    connect._write_text_atomic(target, "new\n", private=private)

    assert target.read_text(encoding="utf-8") == "new\n"
    assert not (tmp_path / "cfg.toml.meridian-tmp").exists()


@pytest.mark.parametrize("private", [False, True])
def test_write_fails_closed_and_leaves_target_untouched(tmp_path, private):
    connect = _load_connect()
    target = tmp_path / "settings.json"
    target.write_text("original\n", encoding="utf-8")
    # A directory where the temp file should go, so the temp file cannot be created.
    (tmp_path / "settings.json.meridian-tmp").mkdir()

    with pytest.raises(OSError):
        connect._write_text_atomic(target, "new token config\n", private=private)

    assert target.read_text(encoding="utf-8") == "original\n"


# ---------------------------------------------------------------------------
# local_ingest: bearer-token header file is created with O_EXCL and mode 0600
# ---------------------------------------------------------------------------

def test_curl_header_file_is_created_with_o_excl_and_owner_only_mode(monkeypatch):
    if str(_DOCS_EXT) not in sys.path:
        sys.path.insert(0, str(_DOCS_EXT))
    from meridian_docs import local_ingest

    seen: dict = {}
    real_open = os.open

    def _recording_open(path, flags, mode=0o777, *args, **kwargs):
        if str(path).endswith("headers.txt"):
            seen["flags"] = flags
            seen["mode"] = mode
        return real_open(path, flags, mode, *args, **kwargs)

    def _fake_run(command, **_kwargs):
        headers_path = command[command.index("--header") + 1][1:]  # strip the leading "@"
        seen["content"] = Path(headers_path).read_text(encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=b'{"ok": true}', stderr=b"")

    monkeypatch.setattr(local_ingest.shutil, "which", lambda _name: "/usr/bin/curl")
    monkeypatch.setattr(local_ingest.subprocess, "run", _fake_run)
    monkeypatch.setattr(local_ingest.os, "open", _recording_open)

    out = local_ingest._call_mcp_tool_via_curl(
        "https://example.test/mcp",
        b"{}",
        {"Authorization": f"Bearer {_FAKE_TOKEN}"},
        5,
    )

    assert out == '{"ok": true}'
    assert seen["flags"] & os.O_EXCL and seen["flags"] & os.O_CREAT
    assert seen["mode"] == 0o600
    assert f"Authorization: Bearer {_FAKE_TOKEN}" in seen["content"]


# ---------------------------------------------------------------------------
# Tunnel client stderr: bearer-token-shaped text is masked
# ---------------------------------------------------------------------------

def test_tunnel_stderr_line_masks_token_shaped_text(monkeypatch, capsys):
    from meridian import tunnel_client

    monkeypatch.setattr(tunnel_client, "_SHELL_FALLBACK_COUNTS", {})
    tunnel_client._note_shell_fallback(f"proc_watchdog_relaunch:{_FAKE_TOKEN}")

    err = capsys.readouterr().err
    assert "shell fallback engaged" in err
    assert _FAKE_TOKEN not in err
    assert "[REDACTED:" in err


# ---------------------------------------------------------------------------
# HTTP error text: fixed strings, exception logged server-side
# ---------------------------------------------------------------------------

def test_admin_git_status_error_text_is_fixed(client, monkeypatch):
    import subprocess

    def _boom(*_a, **_k):
        raise OSError("git could not read /srv/secret-checkout")

    monkeypatch.setattr(subprocess, "run", _boom)

    body = client.get("/admin/git-status").json()

    assert body["ok"] is False
    assert body["error"] == "could not read git status; see server log"
    assert "secret-checkout" not in str(body)


def test_document_structure_parse_error_text_is_fixed(client, monkeypatch, tmp_path):
    from meridian import docs_intel

    project = client.post("/projects", json={"name": "codeql-docstruct"}).json()

    def _boom(_fp):
        raise ValueError("zip member /var/lib/secret.xml is corrupt")

    monkeypatch.setattr(docs_intel, "document_outline", _boom)
    target = tmp_path / "paper.docx"
    target.write_bytes(b"not really a docx")

    body = client.get(
        f"/projects/{project['id']}/document-structure", params={"path": str(target)}
    ).json()

    assert body == {"error": "could not parse document"}


def test_journal_presets_listing_error_text_is_fixed(client, monkeypatch):
    from meridian.routes import notes as notes_routes

    def _boom(user_presets_path=None):
        raise ValueError("preset file /home/secret/presets.json is invalid")

    monkeypatch.setattr(notes_routes, "_resolve_journal_style_preset_getter", lambda: (None, _boom))

    body = client.get("/journal-style-presets").json()

    assert body == {"error": "could not list journal style presets"}


def test_pkg_guard_internal_error_text_is_fixed(client, monkeypatch):
    from meridian import pkg_install_guard

    def _boom(_command):
        raise RuntimeError("guard state at /home/secret/.meridian-guard")

    monkeypatch.setattr(pkg_install_guard, "gate_command", _boom)

    body = client.post("/pkg-guard/check", json={"command": "pip install requests"}).json()

    assert body["action"] == "allow"
    assert body["message"] == "pkg-guard internal error (failing open); see server log"
    assert "secret" not in str(body)


@pytest.mark.parametrize(
    "helper,message",
    [
        ("send_add_fs_roots_control", "could not send add_fs_roots; see server log"),
        ("send_set_fs_roots_control", "could not send set_fs_roots; see server log"),
    ],
)
def test_fs_root_send_failure_returns_a_fixed_message(monkeypatch, helper, message):
    from meridian.routes import tunnel as tunnel_routes

    class _BrokenSocket:
        async def send_json(self, _payload):
            raise RuntimeError(f"socket closed while sending {_FAKE_TOKEN}")

    monkeypatch.setitem(tunnel_routes._tunnel_sockets, "codeql-tenant", _BrokenSocket())

    result = _run(getattr(tunnel_routes, helper)("codeql-tenant", ["/srv/projects"]))

    assert result == {"status": "error", "message": message}
    assert _FAKE_TOKEN not in str(result)


def test_429_detail_keeps_the_limit_suffix():
    from meridian import limits

    exc = limits.LimitExceeded("open_hitl_per_project", 1000, 1000)

    assert str(exc) == limits.limit_429_detail("open_hitl_per_project", 1000)
    assert str(exc).endswith("(limit: open_hitl_per_project=1000)")
    assert str(exc).startswith(limits.LIMIT_429_MESSAGE)


# ---------------------------------------------------------------------------
# pkg_install_guard: registry failures fail open with a fixed message
# ---------------------------------------------------------------------------

def test_registry_lookup_failure_message_hides_exception_text(monkeypatch):
    from meridian import pkg_install_guard

    def _boom(*_a, **_k):
        raise OSError("connect to internal-proxy.corp:3128 refused")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    for result, registry in (
        (pkg_install_guard.verify_pypi("codeql-fixture-pkg-zz"), "PyPI"),
        (pkg_install_guard.verify_npm("codeql-fixture-pkg-zz"), "npm registry"),
    ):
        assert result.error and "could not complete (see server log)" in result.error, registry
        assert "internal-proxy" not in result.error, registry
        assert "OSError" not in result.error, registry


# ---------------------------------------------------------------------------
# _deps: a tenant id is logged redacted when its DB URL cannot be decrypted
# ---------------------------------------------------------------------------

def test_tenant_decrypt_failure_logs_a_redacted_tenant_id(monkeypatch, caplog):
    from fastapi import HTTPException

    from meridian import _deps
    from meridian import db as db_module
    from meridian import tenant_crypto

    async def _tenant(_auth_db, _tenant_id):
        return {"neon_db_url": "v2:ciphertext", "plan": "free", "neon_project_id": "proj"}

    def _bad_key(_tenant_id, _blob):
        raise ValueError("bad key material")

    monkeypatch.setattr(db_module, "get_tenant_by_id", _tenant)
    monkeypatch.setattr(tenant_crypto, "decrypt_tenant_db_url", _bad_key)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=object())))

    with caplog.at_level("WARNING"):
        with pytest.raises(HTTPException):
            _run(_deps._open_tenant_db_by_id(request, _FAKE_TENANT_TOKEN))

    assert "Failed to decrypt neon_db_url" in caplog.text
    assert "codeqlfixture" not in caplog.text
    assert "[REDACTED:meridian-token]" in caplog.text
