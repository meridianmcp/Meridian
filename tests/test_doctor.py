from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from meridian import doctor, setup_bundle


def _fake_local_runner(monkeypatch):
    class FakeRunner:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def doctor(self):
            return SimpleNamespace(checks=(SimpleNamespace(name="ready", severity="ok", detail="ready"),))

    from meridian import local_runner

    monkeypatch.setattr(local_runner, "LocalRunner", FakeRunner)


def _managed_config(repo: Path, host: str) -> dict:
    entries = setup_bundle._server_entries(
        repo,
        host,
        meridian_mode="self-hosted",
        meridian_url=setup_bundle.DEFAULT_MERIDIAN_URL,
        windows=False,
    )
    return {"mcp_servers" if host == "codex" else "mcpServers": entries}


def test_read_config_reads_json_and_toml(tmp_path):
    json_path = tmp_path / "config.json"
    json_path.write_text('{"mcpServers": {}}', encoding="utf-8")
    toml_path = tmp_path / "config.toml"
    toml_path.write_text("[mcp_servers]\n", encoding="utf-8")

    assert doctor._setup_read_config(json_path, "json") == {"mcpServers": {}}
    assert doctor._setup_read_config(toml_path, "toml") == {"mcp_servers": {}}


def test_missing_host_config_has_exact_setup_remediation(tmp_path, monkeypatch):
    _fake_local_runner(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    report = doctor.diagnose([repo], ["claude-code"])

    missing = next(check for check in report.checks if check.name == f"claude-code.{repo}")
    assert missing.severity == "warn"
    assert f'meridian setup --repo "{repo}" --host claude-code' in missing.detail
    assert report.healthy is True


def test_diagnose_detects_serena_project_routing_mismatch_and_does_not_write_config(tmp_path, monkeypatch):
    _fake_local_runner(monkeypatch)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "C:/fake/runtime.exe")
    repo = tmp_path / "paper repo"
    repo.mkdir()
    config_path = repo / ".mcp.json"
    config = _managed_config(repo, "claude-code")
    serena_name = setup_bundle._server_name("serena", repo)
    config["mcpServers"][serena_name]["args"][-1] = str(tmp_path / "wrong-repo")
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    before = config_path.read_bytes()

    report = doctor.diagnose([repo], ["claude-code"])

    routing = next(check for check in report.checks if check.name.endswith(".serena"))
    assert routing.severity == "fail"
    assert "Serena --project does not route" in routing.detail
    assert config_path.read_bytes() == before


def test_diagnose_reports_missing_npx_runtime(tmp_path, monkeypatch):
    _fake_local_runner(monkeypatch)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = repo / ".mcp.json"
    config_path.write_text(json.dumps(_managed_config(repo, "claude-code")), encoding="utf-8")

    report = doctor.diagnose([repo], ["claude-code"])

    runtime = next(check for check in report.checks if check.name == "runtime.npx")
    assert runtime.severity == "fail"
    assert "Install Node.js" in runtime.detail


def test_hosted_health_warns_when_auth_token_is_absent(monkeypatch):
    calls = []
    monkeypatch.delenv("BEARER_TOKEN", raising=False)
    monkeypatch.setattr(doctor, "_setup_request_status", lambda request, timeout: calls.append((request, timeout)) or 200)

    checks = doctor._setup_hosted_checks("https://example.test/mcp", 1.5, False)

    assert [check.severity for check in checks] == ["ok", "warn"]
    assert calls[0][0].full_url == "https://example.test/health"
    assert calls[0][1] == 1.5


def test_hosted_auth_failure_does_not_echo_token(monkeypatch):
    token = "secret-test-token"
    statuses = iter((200, 401))
    seen = []
    monkeypatch.setenv("BEARER_TOKEN", token)

    def request_status(request, _timeout):
        seen.append(request)
        return next(statuses)

    monkeypatch.setattr(doctor, "_setup_request_status", request_status)
    checks = doctor._setup_hosted_checks("https://example.test/mcp", 1.0, False)

    assert checks[-1].severity == "fail"
    assert "refresh the host's BEARER_TOKEN" in checks[-1].detail
    assert token not in json.dumps([check.as_dict() for check in checks])
    assert seen[1].get_header("Authorization") == f"Bearer {token}"


def test_hosted_timeout_and_network_opt_out(monkeypatch):
    monkeypatch.setattr(doctor, "_setup_request_status", lambda *_args: (_ for _ in ()).throw(TimeoutError()))
    checks = doctor._setup_hosted_checks("https://example.test/mcp", 0.2, False)
    assert checks[0].severity == "fail"
    assert "timed out" in checks[0].detail

    checks = doctor._setup_hosted_checks("https://example.test/mcp", 0.2, True)
    assert checks[0].severity == "warn"
    assert "--no-network" in checks[0].detail


def test_cli_json_returns_failure_code_for_invalid_runtime(tmp_path, monkeypatch, capsys):
    _fake_local_runner(monkeypatch)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    path = repo / ".mcp.json"
    path.write_text(json.dumps(_managed_config(repo, "claude-code")), encoding="utf-8")

    rc = doctor.cli_main(["--repo", str(repo), "--host", "claude-code", "--json", "--no-network"])

    output = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert output["healthy"] is False
    assert output["checks"]


def test_timeout_argument_is_bounded(tmp_path, monkeypatch):
    _fake_local_runner(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        doctor.cli_main(["--repo", str(tmp_path), "--timeout", "45"])
    assert exc.value.code == 2


def test_package_cli_dispatches_doctor_to_shared_cli(monkeypatch):
    from meridian import __main__ as cli
    from meridian import tray_main

    monkeypatch.setattr(cli, "main", lambda argv: 17)

    assert tray_main.main(["doctor", "--json"]) == 17


def test_shared_cli_dispatches_doctor_subcommand(monkeypatch):
    from meridian import __main__ as cli

    monkeypatch.setattr(doctor, "cli_main", lambda argv: 23)

    assert cli._dispatch_subcommand(["doctor", "--no-network"]) == 23
