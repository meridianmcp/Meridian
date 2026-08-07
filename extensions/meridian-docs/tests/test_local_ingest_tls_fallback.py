from __future__ import annotations

import subprocess
import ssl
import urllib.error

from meridian_docs import local_ingest


def test_tls_verification_error_is_narrowly_detected() -> None:
    assert local_ingest._is_tls_verification_error(
        urllib.error.URLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired")
    )
    assert not local_ingest._is_tls_verification_error(
        urllib.error.URLError("timed out")
    )


def test_curl_fallback_keeps_credentials_out_of_command_line(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        header_path = command[command.index("--header") + 1][1:]
        payload_path = command[command.index("--data-binary") + 1][1:]
        seen["headers"] = open(header_path, encoding="utf-8").read()
        seen["payload"] = open(payload_path, "rb").read()
        return subprocess.CompletedProcess(command, 0, b'{"ok":true}', b"")

    monkeypatch.setattr(local_ingest.shutil, "which", lambda name: "curl.exe")
    monkeypatch.setattr(local_ingest.subprocess, "run", fake_run)

    result = local_ingest._call_mcp_tool_via_curl(
        "https://usemeridian.us/mcp",
        b'{"jsonrpc":"2.0"}',
        {
            "Content-Type": "application/json",
            "Authorization": "Bearer secret-value",
        },
        timeout=10,
    )

    assert result == '{"ok":true}'
    assert "secret-value" not in " ".join(map(str, seen["command"]))
    assert "Bearer secret-value" in seen["headers"]
    assert seen["payload"] == b'{"jsonrpc":"2.0"}'
    assert seen["kwargs"]["timeout"] == 15


def test_call_mcp_tool_retries_tls_failure_via_curl(monkeypatch) -> None:
    calls: list[str] = []

    def fail_urllib(*args, **kwargs):
        raise urllib.error.URLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired")

    def fake_curl(url, payload, headers, timeout):
        calls.append(url)
        return '{"result":{"content":[{"type":"text","text":"{\\"ok\\":true}"}]}}'

    monkeypatch.setattr(local_ingest.urllib.request, "urlopen", fail_urllib)
    monkeypatch.setattr(local_ingest, "_call_mcp_tool_via_curl", fake_curl)

    result = local_ingest._call_mcp_tool(
        "ingest_document",
        {"project_id": "project", "content": "text"},
        token="secret-value",
    )

    assert result == {"ok": True}
    assert calls == ["https://usemeridian.us/mcp"]


def test_call_mcp_tool_retries_direct_ssl_failure_via_curl(monkeypatch) -> None:
    calls: list[str] = []

    def fail_urllib(*args, **kwargs):
        raise ssl.SSLCertVerificationError("certificate has expired")

    def fake_curl(url, payload, headers, timeout):
        calls.append(url)
        return '{"result":{"content":[{"type":"text","text":"{\\"ok\\":true}"}]}}'

    monkeypatch.setattr(local_ingest.urllib.request, "urlopen", fail_urllib)
    monkeypatch.setattr(local_ingest, "_call_mcp_tool_via_curl", fake_curl)

    result = local_ingest._call_mcp_tool(
        "ingest_document",
        {"project_id": "project", "content": "text"},
        token="secret-value",
    )

    assert result == {"ok": True}
    assert calls == ["https://usemeridian.us/mcp"]
