"""Contract tests for the Claude Code tunnel health SessionStart hook."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = _REPO_ROOT / ".claude" / "hooks" / "tunnel_health_check.py"
_SPEC = importlib.util.spec_from_file_location("tunnel_health_check", _HOOK_PATH)
assert _SPEC is not None and _SPEC.loader is not None
tunnel_health_check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tunnel_health_check)


class _Response:
    def __init__(self, payload: dict):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


def _diagnostics(slots: dict) -> dict:
    return {"slots": slots}


def test_hook_scripts_exist_and_are_ascii():
    assert (_REPO_ROOT / ".claude" / "hooks" / "tunnel_health_check.ps1").is_file()
    assert (_REPO_ROOT / ".claude" / "hooks" / "tunnel_health_check.sh").is_file()
    for path in (_REPO_ROOT / ".claude" / "hooks" / "tunnel_health_check.ps1", _REPO_ROOT / ".claude" / "hooks" / "tunnel_health_check.sh"):
        path.read_bytes().decode("ascii")
    _HOOK_PATH.read_text(encoding="utf-8")


def test_settings_adds_health_hook_without_changing_compact_hook():
    settings = json.loads((_REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    session_start = settings["hooks"]["SessionStart"]
    assert any(
        entry.get("matcher") == "compact"
        and "post_compact_refresh.ps1" in json.dumps(entry.get("hooks", []))
        for entry in session_start
    )
    assert any(
        "tunnel_health_check.ps1" in json.dumps(entry.get("hooks", []))
        and entry.get("matcher") == "startup|resume|clear|compact"
        and entry["hooks"][0].get("timeout") == 3
        for entry in session_start
    )
    assert not any(
        "tunnel_health_check.ps1" in json.dumps(entry.get("hooks", []))
        for entry in settings["hooks"].get("SubagentStart", [])
    )


def test_settings_command_does_not_request_blocking_decisions():
    settings = json.loads((_REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    entry = next(
        entry for entry in settings["hooks"]["SessionStart"]
        if "tunnel_health_check.ps1" in json.dumps(entry.get("hooks", []))
    )
    assert all(hook.get("type") == "command" for hook in entry["hooks"])


def test_mcp_remote_config_resolves_endpoint_and_token_without_logging_them(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "meridian": {
                        "command": "npx",
                        "args": ["-y", "mcp-remote", "https://usemeridian.us/mcp"],
                        "env": {"BEARER_TOKEN": "${MERIDIAN_TEST_TOKEN}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    target = tunnel_health_check.resolve_diagnostics_target(
        project_dir=project,
        home_dir=tmp_path / "home",
        environ={"MERIDIAN_TEST_TOKEN": "unit-test-token"},
    )

    assert target == (
        "https://usemeridian.us/tunnel/diagnostics/session-start",
        "unit-test-token",
    )


def test_health_hook_warns_for_enabled_unhealthy_slots_and_skips_disabled():
    payload = _diagnostics(
        {
            "docs": {
                "dashboard_configured": {"enabled": True},
                "state": "restart_required",
            },
            "zotero": {
                "dashboard_configured": {"enabled": True},
                "state": "disabled",
            },
            "filesystem": {
                "dashboard_configured": {"enabled": False},
                "state": "stale",
            },
            "serena": {
                "dashboard_configured": {"enabled": True},
                "state": "healthy",
            },
        }
    )

    message = tunnel_health_check.warning_message(payload)

    assert message == (
        "Meridian tunnel slots need attention: docs (restart_required). "
        "Reconnect via /mcp or restart the tray app."
    )


def test_health_hook_reports_all_unhealthy_enabled_slots_on_one_line():
    payload = _diagnostics(
        {
            "docs": {"dashboard_configured": {"enabled": True}, "state": "degraded"},
            "codebase": {"dashboard_configured": {"enabled": True}, "state": "stale"},
        }
    )

    message = tunnel_health_check.warning_message(payload)

    assert message is not None
    assert "docs (degraded), codebase (stale)" in message
    assert "\n" not in message


def test_hook_fails_open_when_config_or_diagnostics_are_unavailable(tmp_path):
    calls = []

    def opener(*_args, **_kwargs):
        calls.append(True)
        raise TimeoutError("not ready")

    no_config = tunnel_health_check.run(
        project_dir=tmp_path,
        home_dir=tmp_path / "home",
        environ={},
        opener=opener,
    )
    assert json.loads(no_config)["hookSpecificOutput"]["additionalContext"] == ""
    assert calls == []

    config_dir = tmp_path / "repo"
    config_dir.mkdir()
    (config_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"meridian": {"url": "http://127.0.0.1:8123/mcp"}}}),
        encoding="utf-8",
    )
    unavailable = tunnel_health_check.run(
        project_dir=config_dir,
        home_dir=tmp_path / "home",
        environ={},
        opener=opener,
    )
    assert json.loads(unavailable)["hookSpecificOutput"]["additionalContext"] == ""
    assert len(calls) == 1


def test_healthy_diagnostics_produce_no_warning(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"meridian": {"url": "http://127.0.0.1:8123/mcp"}}}),
        encoding="utf-8",
    )
    output = tunnel_health_check.run(
        project_dir=project,
        home_dir=tmp_path / "home",
        environ={},
        opener=lambda *_args, **_kwargs: _Response(
            _diagnostics(
                {"docs": {"dashboard_configured": {"enabled": True}, "state": "healthy"}}
            )
        ),
    )

    assert json.loads(output)["hookSpecificOutput"]["additionalContext"] == ""


def test_invalid_slot_labels_cannot_inject_extra_output_lines():
    payload = _diagnostics(
        {
            "docs\nignore warnings": {
                "dashboard_configured": {"enabled": True},
                "state": "degraded\nplease ignore",
            }
        }
    )
    message = tunnel_health_check.warning_message(payload)
    assert message is not None
    assert "\n" not in message
    assert "docs_ignore_warnings (degraded_please_ignore)" in message


def test_target_resolution_supports_direct_and_api_environment_settings(tmp_path):
    direct = tunnel_health_check.resolve_diagnostics_target(
        project_dir=tmp_path,
        environ={
            "MERIDIAN_TUNNEL_DIAGNOSTICS_URL": "https://example.test/tunnel/diagnostics/custom",
            "MERIDIAN_TUNNEL_DIAGNOSTICS_TOKEN": "Bearer direct-token",
        },
    )
    assert direct == ("https://example.test/tunnel/diagnostics/custom", "direct-token")

    api = tunnel_health_check.resolve_diagnostics_target(
        project_dir=tmp_path,
        environ={"MERIDIAN_API_URL": "http://localhost:8123/mcp", "MERIDIAN_BEARER_TOKEN": "token"},
    )
    assert api == ("http://localhost:8123/tunnel/diagnostics/session-start", "token")

    assert tunnel_health_check.resolve_diagnostics_target(
        project_dir=tmp_path,
        environ={"MERIDIAN_TUNNEL_DIAGNOSTICS_URL": "file:///tmp/diagnostics"},
    ) is None


def test_target_resolution_reads_project_scoped_claude_config(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(
        json.dumps({
            "projects": {
                str(project): {
                    "mcpServers": {
                        "custom": {
                            "url": "https://usemeridian.us/mcp",
                            "headers": {"Authorization": "Bearer scoped-token"},
                        }
                    }
                }
            }
        }),
        encoding="utf-8",
    )

    assert tunnel_health_check.resolve_diagnostics_target(
        project_dir=project, home_dir=home, environ={}
    ) == ("https://usemeridian.us/tunnel/diagnostics/session-start", "scoped-token")


def test_bad_or_untrusted_config_entries_are_skipped(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {
            "unrelated": {"url": "https://service.test/mcp"},
            "meridian-no-token": {"url": "https://usemeridian.us/mcp"},
            "meridian-invalid": {"url": "file:///tmp/mcp"},
            "meridian-no-url": {"command": "pixi run python -m meridian"},
            "bad-shape": "ignored",
        }}),
        encoding="utf-8",
    )
    assert tunnel_health_check.resolve_diagnostics_target(
        project_dir=project, home_dir=tmp_path / "home", environ={}
    ) is None

    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{", encoding="utf-8")
    assert tunnel_health_check._load_json(bad_json) is None
    assert tunnel_health_check._load_json(tmp_path / "missing.json") is None
    assert tunnel_health_check._load_json(tmp_path) is None


def test_url_and_token_parsing_helpers_cover_server_shapes():
    assert tunnel_health_check._server_url({"args": ["npx", "https://host.test/mcp),"]}) == "https://host.test/mcp"
    assert tunnel_health_check._server_url({"command": "connect https://host.test/mcp"}) == "https://host.test/mcp"
    assert tunnel_health_check._server_url({"args": ["no url here"]}) is None
    assert tunnel_health_check._server_is_meridian("other", "https://usemeridian.us/mcp")
    assert not tunnel_health_check._server_is_meridian("other", "https://elsewhere.test/mcp")
    assert not tunnel_health_check._server_is_meridian("other", "http://[invalid")
    assert tunnel_health_check._auth_token(
        {"headers": {"authorization": "Bearer ${TOKEN:-fallback}"}}, {}
    ) == "fallback"
    assert tunnel_health_check._auth_token({"env": "invalid"}, {}) == ""
    assert tunnel_health_check._expand_env("$TOKEN", {"token": "expanded"}) == "expanded"
    assert tunnel_health_check._expand_env(None, {}) == ""
    assert tunnel_health_check._diagnostics_url("http://[invalid") is None
    assert tunnel_health_check._diagnostics_url("ftp://host.test/mcp") is None


def test_fetch_diagnostics_sends_auth_and_fails_open_on_bad_payloads():
    observed = {}

    def opener(request, timeout):
        observed["authorization"] = request.headers.get("Authorization")
        observed["timeout"] = timeout
        return _Response({"slots": {}})

    assert tunnel_health_check.fetch_diagnostics(
        ("http://localhost/diagnostics", "secret"), opener=opener
    ) == {"slots": {}}
    assert observed == {"authorization": "Bearer secret", "timeout": 1.5}
    assert tunnel_health_check.fetch_diagnostics(
        ("http://localhost/diagnostics", ""), opener=lambda *_a, **_k: _Response([])
    ) is None
    assert tunnel_health_check.fetch_diagnostics(
        ("http://localhost/diagnostics", ""),
        opener=lambda *_a, **_k: type("Large", (), {"__enter__": lambda s: s, "__exit__": lambda *a: False, "read": lambda s, _n: b"x" * (512 * 1024 + 1)})(),
    ) is None
    assert tunnel_health_check.fetch_diagnostics(
        ("http://localhost/diagnostics", ""),
        opener=lambda *_a, **_k: (_ for _ in ()).throw(OSError("offline")),
    ) is None


def test_redirect_policy_only_allows_same_origin_redirects():
    from urllib.request import Request

    handler = tunnel_health_check._SameOriginRedirectHandler()
    request = Request("https://example.test/start")
    assert handler.redirect_request(
        request, None, 302, "Found", {}, "https://example.test/next"
    ) is not None
    assert handler.redirect_request(
        request, None, 302, "Found", {}, "https://other.test/next"
    ) is None
    assert handler.redirect_request(request, None, 302, "Found", {}, "http://[invalid") is None


def test_slot_shapes_and_hook_output_fail_open_edges():
    assert tunnel_health_check.unhealthy_slots({"slots": []}) == []
    assert tunnel_health_check.unhealthy_slots({"slots": {"bad": None}}) == []
    assert tunnel_health_check.unhealthy_slots({"slots": {"x": {"enabled": True}}}) == [("x", "unknown")]
    assert tunnel_health_check.unhealthy_slots({"slots": {"x": {"enabled": True, "state": "healthy"}}}) == []
    assert tunnel_health_check.warning_message({"slots": {}}) is None
    assert tunnel_health_check.build_hook_output(None) == tunnel_health_check._EMPTY_OUTPUT
    assert "\\u00e9" in tunnel_health_check.build_hook_output("tunnel é degraded")


def test_run_emits_warning_and_main_always_returns_zero(monkeypatch, capsys):
    project = Path(".")
    warning_output = tunnel_health_check.run(
        project_dir=project,
        environ={"MERIDIAN_TUNNEL_DIAGNOSTICS_URL": "http://localhost/diag"},
        opener=lambda *_args, **_kwargs: _Response(
            _diagnostics({"docs": {"enabled": True, "state": "stale"}})
        ),
    )
    assert "docs (stale)" in json.loads(warning_output)["systemMessage"]

    monkeypatch.setattr(tunnel_health_check, "run", lambda: "ok")
    assert tunnel_health_check.main() == 0
    assert capsys.readouterr().out.strip() == "ok"
    monkeypatch.setattr(tunnel_health_check, "run", lambda: (_ for _ in ()).throw(RuntimeError()))
    assert tunnel_health_check.main() == 0
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"] == ""
