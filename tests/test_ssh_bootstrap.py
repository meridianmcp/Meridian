"""Tests for meridian.ssh_bootstrap.

No SSH host is reachable from the build environment. Every bootstrap run here
goes through FakeHost, an in-memory model of a POSIX host that answers the
exact argv the plan sends. SshExecutor is tested by patching subprocess.run.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
from pathlib import Path

import pytest

from meridian import ssh_bootstrap as sb
from meridian import tunnel_client
from meridian.__main__ import _SUBCOMMANDS, _dispatch_subcommand
from meridian.client_setup import SERENA_PACKAGE
from meridian.ssh_bootstrap import (
    BootstrapError,
    BootstrapTarget,
    ExecResult,
    SshExecutor,
    UnsupportedTargetError,
    bootstrap,
    cli_main,
    plan_bootstrap,
    remote_command_string,
)

LINUX_ASSET, LINUX_SHA = tunnel_client._CBM_PINNED_ASSETS[("linux", "amd64")]
DARWIN_ASSET, DARWIN_SHA = tunnel_client._CBM_PINNED_ASSETS[("darwin", "arm64")]
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

EXPECTED_LINUX_KEYS = [
    "prereq-uv",
    "prereq-uvx",
    "prereq-curl",
    "prereq-tar",
    "prereq-digest",
    "dirs",
    "serena-install",
    "serena-verify",
    "cbm-download",
    "cbm-verify-sha256",
    "cbm-extract",
    "cbm-install",
    "cbm-verify-binary",
    "cbm-link",
    "serena-start-systemd",
    "serena-start-pidfile",
    "serena-listening",
]

DEFAULT_TOOLS = frozenset(
    {"uv", "uvx", "curl", "tar", "sha256sum", "shasum", "mkdir", "find", "test", "ln",
     "systemctl", "systemd-run", "python3"}
)


def _ok(stdout: str = "") -> ExecResult:
    return ExecResult(0, stdout, "")


class FakeHost:
    """In-memory POSIX host. Records every argv and answers from a small state model.

    ``archive_sha`` is what ``sha256sum``/``shasum`` report for the downloaded
    archive. ``systemd`` controls whether ``systemctl --user`` has a user manager.
    """

    def __init__(
        self,
        *,
        tools: frozenset[str] = DEFAULT_TOOLS,
        systemd: bool = True,
        archive_sha: str = LINUX_SHA,
    ) -> None:
        self.tools = frozenset(tools)
        self.systemd = systemd
        self.archive_sha = archive_sha
        self.paths: set[str] = set()
        self.symlinks: set[str] = set()
        self.uv_tools: set[tuple[str, str]] = set()
        self.active_units: set[str] = set()
        self.alive_pidfiles: set[str] = set()
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv) -> ExecResult:
        argv = tuple(argv)
        self.calls.append(argv)
        return self._handle(argv)

    def actions(self) -> list[tuple[str, ...]]:
        return list(self.calls)

    def _handle(self, argv: tuple[str, ...]) -> ExecResult:
        head = argv[0]
        if head not in self.tools:
            return ExecResult(127, "", f"{head}: command not found")
        if len(argv) == 2 and argv[1] == "--version":
            return _ok(f"{head} 0.0.0\n")
        if argv in {("sha256sum", "/dev/null"), ("shasum", "-a", "256", "/dev/null")}:
            return _ok(f"{EMPTY_SHA256}  /dev/null\n")
        if head == "mkdir":
            self.paths.update(a for a in argv[1:] if a != "-p")
            return _ok()
        if head == "curl":
            if argv[-1].startswith("https://"):
                self.paths.add(argv[argv.index("-o") + 1])
                return _ok()
            # Serena health check on 127.0.0.1.
            if self.active_units or self.alive_pidfiles:
                return _ok()
            return ExecResult(7, "", "Failed to connect to 127.0.0.1")
        if head in {"sha256sum", "shasum"}:
            path = argv[-1]
            if path in self.paths:
                return _ok(f"{self.archive_sha}  {path}\n")
            return ExecResult(1, "", f"{path}: No such file or directory")
        if head == "tar":
            archive = argv[argv.index("-xzf") + 1]
            stage = argv[argv.index("-C") + 1]
            if archive not in self.paths:
                return ExecResult(2, "", "tar: archive not found")
            self.paths.add(f"{stage}/{sb.CBM_BIN}")
            return _ok()
        if head == "find":
            stage = argv[1]
            name = argv[argv.index("-name") + 1]
            dest = argv[-2]
            if f"{stage}/{name}" in self.paths:
                self.paths.add(dest)
            return _ok()
        if head == "test":
            flag, path = argv[1], argv[2]
            present = path in self.symlinks if flag == "-L" else path in self.paths
            return ExecResult(0 if present else 1, "", "")
        if head == "ln":
            link = argv[-1]
            self.symlinks.add(link)
            self.paths.add(link)
            return _ok()
        if head == "uv":
            if argv[1:] == ("tool", "list"):
                lines = "".join(f"{name} {ver}\n- serena\n" for name, ver in sorted(self.uv_tools))
                return _ok(lines)
            if argv[1:3] == ("tool", "install"):
                name, _, version = argv[3].partition("==")
                self.uv_tools.add((name, f"v{version}"))
                return _ok()
        if head == "systemctl":
            if argv[1:] == ("--user", "show-environment"):
                return _ok() if self.systemd else ExecResult(1, "", "Failed to connect to bus")
            if argv[1:3] == ("--user", "is-active"):
                if argv[3] in self.active_units:
                    return _ok("active\n")
                return ExecResult(3, "inactive\n", "")
        if head == "systemd-run":
            unit = next(a for a in argv if a.startswith("--unit=")).split("=", 1)[1]
            self.active_units.add(unit)
            return _ok()
        if head == "python3":
            if argv[2] == sb._DETACH_SRC:
                pidfile = argv[3]
                self.paths.add(pidfile)
                self.alive_pidfiles.add(pidfile)
                return _ok()
            if argv[2] == sb._PIDCHECK_SRC:
                if argv[3] in self.alive_pidfiles:
                    return _ok("alive\n")
                return ExecResult(1, "", "")
        return ExecResult(1, "", f"fake host: unhandled argv {argv!r}")


def _target(**overrides) -> BootstrapTarget:
    values = {"host": "example.test", "user": "adam", "repo_path": "code/meridian"}
    values.update(overrides)
    return BootstrapTarget(**values)


def _step(steps, key):
    return next(s for s in steps if s.key == key)


def _statuses(report) -> dict[str, str]:
    return {r.key: r.status for r in report.steps}


# ---------------------------------------------------------------------------
# Pins and plan
# ---------------------------------------------------------------------------


def test_serena_pin_comes_from_client_setup():
    assert SERENA_PACKAGE == "serena-agent==1.5.3"
    assert sb.SERENA_VERSION == "1.5.3"
    install = _step(plan_bootstrap(_target()), "serena-install")
    assert install.argv == ("uv", "tool", "install", "serena-agent==1.5.3")
    serve = _step(plan_bootstrap(_target()), "serena-start-systemd").argv
    assert serve[serve.index("uvx") + 1: serve.index("uvx") + 3] == ("--from", "serena-agent==1.5.3")


def test_codebase_memory_pins_are_read_from_tunnel_client():
    assert sb._CBM_PINNED_ASSETS is tunnel_client._CBM_PINNED_ASSETS
    assert sb._CBM_PINNED_TAG is tunnel_client._CBM_PINNED_TAG
    steps = plan_bootstrap(_target())
    download = _step(steps, "cbm-download")
    expected_url = (
        f"https://github.com/{tunnel_client._CBM_REPO}/releases/download/"
        f"{tunnel_client._CBM_PINNED_TAG}/{LINUX_ASSET}"
    )
    assert download.argv[-1] == expected_url
    assert download.argv[download.argv.index("--proto") + 1] == "=https"
    verify = _step(steps, "cbm-verify-sha256")
    assert verify.expect is not None
    assert verify.expect(ExecResult(0, f"{LINUX_SHA}  /x/archive.tar.gz\n", ""))
    assert not verify.expect(ExecResult(0, f"{'0' * 64}  /x/archive.tar.gz\n", ""))
    assert not verify.expect(ExecResult(1, f"{LINUX_SHA}  /x\n", ""))


def test_plan_step_order_linux_systemd():
    assert [s.key for s in plan_bootstrap(_target())] == EXPECTED_LINUX_KEYS


def test_output_checks_fail_closed_on_empty_or_failed_output():
    steps = plan_bootstrap(_target())
    verify = _step(steps, "cbm-verify-sha256")
    assert verify.expect is not None
    assert not verify.expect(ExecResult(0, "   \n", ""))
    serena_verify = _step(steps, "serena-verify")
    assert serena_verify.expect is not None
    assert not serena_verify.expect(ExecResult(1, "serena-agent v1.5.3\n", ""))
    assert not serena_verify.expect(ExecResult(0, "serena-agent v1.4.0\n", ""))
    assert serena_verify.expect(ExecResult(0, "serena-agent v1.5.3\n- serena\n", ""))


def test_sha_is_verified_before_extract_and_install():
    keys = [s.key for s in plan_bootstrap(_target())]
    assert keys.index("cbm-verify-sha256") < keys.index("cbm-extract") < keys.index("cbm-install")


def test_darwin_plan_uses_shasum_and_darwin_asset():
    steps = plan_bootstrap(_target(os_family="darwin", arch="arm64"))
    verify = _step(steps, "cbm-verify-sha256")
    assert verify.argv[:3] == ("shasum", "-a", "256")
    assert verify.argv[-1].endswith(DARWIN_ASSET)
    assert _step(steps, "prereq-digest").argv == ("shasum", "-a", "256", "/dev/null")
    assert _step(steps, "cbm-download").argv[-1].endswith(DARWIN_ASSET)


def test_arch_aliases_normalise_and_bad_platforms_are_refused():
    assert _target(arch="x86_64").arch == "amd64"
    assert _target(arch="aarch64", os_family="macos").os_family == "darwin"
    with pytest.raises(UnsupportedTargetError):
        _target(os_family="windows")
    with pytest.raises(UnsupportedTargetError):
        _target(arch="riscv64")


def test_plan_refuses_a_platform_with_no_pinned_build(monkeypatch):
    monkeypatch.setattr(sb, "_CBM_PINNED_ASSETS", {})
    with pytest.raises(UnsupportedTargetError, match="no pinned codebase-memory-mcp build"):
        plan_bootstrap(_target())


def test_plan_refuses_a_pinned_asset_that_is_not_a_tarball(monkeypatch):
    monkeypatch.setattr(
        sb, "_CBM_PINNED_ASSETS", {("linux", "amd64"): ("codebase-memory-mcp-linux.zip", LINUX_SHA)}
    )
    with pytest.raises(UnsupportedTargetError, match="not a tar.gz"):
        plan_bootstrap(_target())


def test_target_rejects_option_like_and_key_material_inputs(tmp_path):
    with pytest.raises(BootstrapError):
        _target(host="-oProxyCommand=evil")
    with pytest.raises(BootstrapError):
        _target(host="bad host")
    with pytest.raises(BootstrapError):
        _target(port=0)
    with pytest.raises(BootstrapError):
        _target(port=True)
    with pytest.raises(BootstrapError):
        _target(identity_file="-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA")
    with pytest.raises(BootstrapError):
        _target(identity_file="   ")
    with pytest.raises(BootstrapError):
        _target(identity_file="-oIdentityFile=x")
    with pytest.raises(BootstrapError):
        _target(repo_path="line one\nline two")
    with pytest.raises(BootstrapError):
        _target(repo_path="-rf")
    with pytest.raises(BootstrapError):
        _target(user="adam; reboot")


def test_identity_file_is_stored_as_a_path_only(tmp_path):
    key = tmp_path / "id_ed25519"
    target = _target(identity_file=str(key))
    assert target.identity_file == Path(str(key))
    assert isinstance(target.identity_file, Path)


def test_serena_service_binds_to_loopback_and_runs_headless():
    serve = _step(plan_bootstrap(_target()), "serena-start-systemd").argv
    assert serve[serve.index("--host") + 1] == "127.0.0.1"
    assert serve[serve.index("--open-web-dashboard") + 1] == "false"
    assert serve[serve.index("--project") + 1] == "code/meridian"
    assert "0.0.0.0" not in serve


# ---------------------------------------------------------------------------
# SshExecutor: argv, quoting, no shell
# ---------------------------------------------------------------------------


def test_ssh_argv_has_batchmode_host_key_policy_port_and_identity():
    target = _target(port=2222, identity_file=Path("/home/adam/.ssh/id_ed25519"))
    argv = SshExecutor(target).build_argv(["uv", "--version"])
    assert argv[0] == "ssh"
    assert argv[argv.index("-p") + 1] == "2222"
    assert argv[argv.index("-i") + 1] == str(Path("/home/adam/.ssh/id_ed25519"))
    assert "BatchMode=yes" in argv
    assert "StrictHostKeyChecking=accept-new" in argv
    assert "StrictHostKeyChecking=no" not in argv
    assert "IdentitiesOnly=yes" in argv
    assert "adam@example.test" in argv
    assert argv[-1] == "uv --version"


def test_remote_command_is_quoted_element_by_element():
    hostile = "/srv/my repo; rm -rf $HOME $(id) 'x' \"y\""
    target = _target(repo_path=hostile)
    serve = _step(plan_bootstrap(target), "serena-start-systemd").argv
    remote = SshExecutor(target).build_argv(serve)[-1]
    assert shlex.split(remote) == list(serve)
    assert hostile in serve
    assert remote_command_string(["echo", ""]) == "echo ''"


def test_remote_command_rejects_empty_and_nul_arguments():
    with pytest.raises(BootstrapError):
        remote_command_string([])
    with pytest.raises(BootstrapError):
        remote_command_string(["ok", "bad\x00"])


def test_ssh_executor_never_uses_shell_true(monkeypatch):
    recorded: dict = {}

    def fake_run(args, **kwargs):
        recorded["args"] = args
        recorded["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, "out", "err")

    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    result = SshExecutor(_target()).run(["uv", "--version"])
    assert result == ExecResult(0, "out", "err")
    assert isinstance(recorded["args"], list)
    assert recorded["kwargs"].get("shell", False) is False
    assert recorded["kwargs"]["stdin"] == subprocess.DEVNULL
    assert recorded["kwargs"]["timeout"] == 900.0


def test_ssh_executor_maps_timeout_and_missing_ssh(monkeypatch):
    def timeout_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))

    monkeypatch.setattr(sb.subprocess, "run", timeout_run)
    assert SshExecutor(_target(), timeout_seconds=3).run(["uv"]).exit_code == 124

    def missing_run(args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(sb.subprocess, "run", missing_run)
    result = SshExecutor(_target()).run(["uv"])
    assert result.exit_code == 127
    assert "could not start ssh" in result.stderr


# ---------------------------------------------------------------------------
# bootstrap(): fake host
# ---------------------------------------------------------------------------


def test_fresh_bootstrap_runs_every_applicable_step_in_order():
    host = FakeHost()
    report = bootstrap(_target(), host)
    assert report.ok
    assert report.failed_step is None
    assert [r.key for r in report.steps] == EXPECTED_LINUX_KEYS
    statuses = _statuses(report)
    assert statuses["serena-start-systemd"] == "ok"
    assert statuses["serena-start-pidfile"] == "not_applicable"
    assert statuses["cbm-install"] == "ok"
    assert statuses["serena-listening"] == "ok"
    assert ("uv", "tool", "install", "serena-agent==1.5.3") in host.calls
    assert any(c[0] == "systemd-run" for c in host.calls)


def test_sha_mismatch_aborts_before_extract_and_install():
    host = FakeHost(archive_sha="0" * 64)
    report = bootstrap(_target(), host)
    assert not report.ok
    assert report.failed_step == "cbm-verify-sha256"
    assert [r.key for r in report.steps][-1] == "cbm-verify-sha256"
    assert not any(c[:2] == ("tar", "-xzf") for c in host.calls)
    assert not any(c[0] == "find" for c in host.calls)
    assert not any(c[0] == "systemd-run" for c in host.calls)
    failed = report.steps[-1]
    assert failed.status == "failed"
    assert "0" * 64 not in json.dumps(report.to_dict())
    assert LINUX_SHA not in json.dumps(report.to_dict())


def test_idempotent_rerun_skips_completed_steps():
    host = FakeHost()
    first = bootstrap(_target(), host)
    assert first.ok
    calls_after_first = len(host.calls)

    second = bootstrap(_target(), host)
    assert second.ok
    statuses = _statuses(second)
    for key in (
        "serena-install",
        "cbm-download",
        "cbm-verify-sha256",
        "cbm-extract",
        "cbm-install",
        "cbm-link",
        "serena-start-systemd",
    ):
        assert statuses[key] == "skipped", key
    rerun = host.calls[calls_after_first:]
    assert not any(c[:3] == ("uv", "tool", "install") for c in rerun)
    assert not any(c[0] == "systemd-run" for c in rerun)
    assert not any(c[:2] == ("tar", "-xzf") for c in rerun)
    assert not any("https://" in " ".join(c) for c in rerun)


def test_pidfile_fallback_when_systemd_user_is_unavailable():
    host = FakeHost(systemd=False)
    report = bootstrap(_target(), host)
    assert report.ok
    statuses = _statuses(report)
    assert statuses["serena-start-systemd"] == "not_applicable"
    assert statuses["serena-start-pidfile"] == "ok"
    assert not any(c[0] == "systemd-run" for c in host.calls)
    detach = next(c for c in host.calls if c[0] == "python3" and c[2] == sb._DETACH_SRC)
    assert detach[detach.index("--host") + 1] == "127.0.0.1"
    assert detach[3] == sb.SERENA_PIDFILE
    assert detach[4] == sb.SERENA_LOGFILE

    before = len(host.calls)
    rerun = bootstrap(_target(), host)
    assert rerun.ok
    assert _statuses(rerun)["serena-start-pidfile"] == "skipped"
    assert not any(c[2] == sb._DETACH_SRC for c in host.calls[before:] if c[0] == "python3")


def test_failure_stops_at_first_failing_step():
    host = FakeHost(tools=DEFAULT_TOOLS - {"uv"})
    report = bootstrap(_target(), host)
    assert not report.ok
    assert report.failed_step == "prereq-uv"
    assert host.calls == [("uv", "--version")]
    assert len(report.steps) == 1


def test_missing_curl_stops_before_download():
    host = FakeHost(tools=DEFAULT_TOOLS - {"curl"})
    report = bootstrap(_target(), host)
    assert report.failed_step == "prereq-curl"
    assert not any(c[0] == "curl" and "-fsSL" in c for c in host.calls)


def test_failure_detail_is_truncated():
    class NoisyExecutor:
        def run(self, argv):
            return ExecResult(1, "", "x" * 5000)

    report = bootstrap(_target(), NoisyExecutor())
    assert report.failed_step == "prereq-uv"
    assert len(report.steps[-1].detail) <= 400


def test_no_key_material_in_report_or_logs(tmp_path, caplog):
    sentinel = "SENTINEL-KEY-BODY-7f3a9c"
    key_path = tmp_path / "id_ed25519"
    key_path.write_text(sentinel + "\n", encoding="utf-8")
    target = _target(identity_file=key_path)
    caplog.set_level(logging.DEBUG, logger="meridian.ssh_bootstrap")

    host = FakeHost(archive_sha="0" * 64)
    report = bootstrap(target, host)
    rendered = json.dumps(report.to_dict())
    assert sentinel not in rendered
    assert str(key_path) not in rendered
    assert sentinel not in caplog.text
    assert str(key_path) not in caplog.text
    assert host.calls, "the fake host should have seen remote commands"
    assert all(sentinel not in " ".join(c) for c in host.calls)
    assert "identity_file" not in rendered
    assert not report.ok


def test_report_to_dict_has_no_identity_field():
    report = bootstrap(_target(identity_file=Path("/k/id")), FakeHost())
    data = report.to_dict()
    assert "identity_file" not in data
    assert data["host"] == "example.test"
    assert data["ok"] is True
    assert data["failed_step"] is None


# ---------------------------------------------------------------------------
# CLI and dispatch
# ---------------------------------------------------------------------------


def test_cli_plan_prints_steps_and_runs_nothing(capsys):
    def factory(target):
        raise AssertionError("--plan must not build an executor")

    rc = cli_main(["--host", "example.test", "--plan"], executor_factory=factory)
    assert rc == 0
    out = capsys.readouterr().out
    assert "[cbm-verify-sha256]" in out
    assert "uv tool install serena-agent==1.5.3" in out
    assert "--host 127.0.0.1" in out


def test_cli_invalid_target_returns_2(capsys):
    assert cli_main(["--host=-oProxyCommand=x", "--plan"]) == 2
    assert cli_main(["--host", "h", "--os", "windows", "--plan"]) == 2
    assert "ssh-bootstrap:" in capsys.readouterr().err


def test_cli_runs_bootstrap_and_reports_json(capsys):
    rc = cli_main(
        ["--host", "example.test", "--user", "adam", "--repo", "code/meridian", "--json"],
        executor_factory=lambda target: FakeHost(),
    )
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["failed_step"] is None


def test_cli_returns_1_when_a_step_fails(capsys):
    rc = cli_main(
        ["--host", "example.test"],
        executor_factory=lambda target: FakeHost(archive_sha="0" * 64),
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "failed at cbm-verify-sha256" in out


def test_subcommand_is_registered_and_dispatched():
    assert "ssh-bootstrap" in _SUBCOMMANDS
    assert _dispatch_subcommand(["ssh-bootstrap", "--host", "example.test", "--plan"]) == 0
    assert _dispatch_subcommand(["not-a-subcommand"]) is None
