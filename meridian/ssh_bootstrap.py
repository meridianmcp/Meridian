"""Bootstrap Serena and codebase-memory-mcp on an SSH host.

The code tools run on the machine that holds the code. This module plans the
install and runs it over SSH, so the host needs no editor extension.

Trust model
- Every remote command is an argv list. ``SshExecutor`` quotes each element
  with ``shlex.quote`` and passes one string to ``ssh``. Nothing runs with
  ``shell=True``, and no user text is interpolated into a shell program.
- codebase-memory-mcp is the pinned release asset from
  :mod:`meridian.tunnel_client` (``_CBM_REPO``, ``_CBM_PINNED_TAG``,
  ``_CBM_PINNED_ASSETS``). The archive's SHA-256 is checked on the remote
  before it is extracted or installed. A mismatch stops the bootstrap there.
- Serena is pinned to ``SERENA_PACKAGE`` from :mod:`meridian.client_setup`.
- Serena is the only network service this bootstrap starts. It binds to
  127.0.0.1. codebase-memory-mcp is a stdio server that clients launch
  themselves, so the bootstrap installs it and starts no listener for it.
- The bootstrap never reads the identity file. It passes the path to ``ssh``
  with ``-i`` and nothing else.

Host-key policy: ``StrictHostKeyChecking=accept-new``. The first connection
records the host key in known_hosts. A later key change fails the connection.
This is trust on first use. Verify the fingerprint out of band on an
untrusted network.

Remote paths are relative to the SSH login directory. sshd starts exec
sessions there, which is the home directory, so ``.meridian/bin`` means
``$HOME/.meridian/bin``.

Out of scope for this module: the tunnel client on the host (the next step),
and installing ``uv``. ``uv`` must already be on the host's PATH; the
bootstrap checks for it and stops if it is missing.
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Protocol

from .client_setup import SERENA_PACKAGE
from .serena_pool import SERENA_POOL_BASE_PORT
from .tunnel_client import _CBM_PINNED_ASSETS, _CBM_PINNED_TAG, _CBM_REPO

logger = logging.getLogger(__name__)

SERENA_AGENT = "serena-agent"
SERENA_VERSION = SERENA_PACKAGE.partition("==")[2]
if not SERENA_VERSION:  # pragma: no cover - guards a broken pin constant
    raise RuntimeError(f"client_setup.SERENA_PACKAGE is not pinned: {SERENA_PACKAGE!r}")

# Remote locations, relative to the SSH login directory.
BIN_DIR = ".meridian/bin"
CACHE_DIR = ".meridian/cache/ssh-bootstrap"
RUN_DIR = ".meridian/run"
LOG_DIR = ".meridian/logs"
CBM_BIN = "codebase-memory-mcp"
SERENA_UNIT = "meridian-serena"
SERENA_PIDFILE = f"{RUN_DIR}/serena.pid"
SERENA_LOGFILE = f"{LOG_DIR}/serena.log"

STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_FAILED = "failed"

_SUPPORTED_OS = {"linux": "linux", "darwin": "darwin", "macos": "darwin", "osx": "darwin"}
_ARCH_ALIASES = {
    "amd64": "amd64",
    "x86_64": "amd64",
    "x64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
}
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:%-]{0,252}$")
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]{0,63}$")
_KEY_MARKER = "-----BEGIN"
_MAX_REPO_PATH = 1024
_OUTPUT_TAIL_CHARS = 400
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

# Launcher that starts a command fully detached (new session, stdio on a log
# file) and records its pid. It runs as one argv element of ``python3 -c``.
_DETACH_SRC = """\
import os, subprocess, sys
pidfile, logfile, cmd = sys.argv[1], sys.argv[2], sys.argv[3:]
with open(os.devnull, "rb") as devnull, open(logfile, "ab") as log:
    proc = subprocess.Popen(cmd, stdin=devnull, stdout=log, stderr=log,
                            start_new_session=True, close_fds=True)
tmp = pidfile + ".tmp"
with open(tmp, "w", encoding="ascii") as fh:
    fh.write(f"{proc.pid}\\n")
os.replace(tmp, pidfile)
"""

# Prints "alive" and exits 0 when the pid in the pidfile is a live process.
_PIDCHECK_SRC = """\
import os, sys
try:
    with open(sys.argv[1], encoding="ascii") as fh:
        pid = int(fh.read().strip())
    os.kill(pid, 0)
except PermissionError:
    pass
except (OSError, ValueError):
    sys.exit(1)
print("alive")
"""


class BootstrapError(ValueError):
    """The target description is invalid."""


class UnsupportedTargetError(BootstrapError):
    """The target platform has no pinned build or is not supported."""


@dataclass(frozen=True)
class BootstrapTarget:
    """Where to bootstrap. Holds paths and names only, never key material.

    ``identity_file`` is a path to a private key. The file is passed to ``ssh``
    and is never opened here. ``repo_path`` is the project Serena serves
    (``--project``); it is relative to the login directory or absolute.
    """

    host: str
    user: str | None = None
    port: int = 22
    identity_file: Path | None = None
    os_family: str = "linux"
    arch: str = "amd64"
    repo_path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not _HOST_RE.match(self.host):
            raise BootstrapError(
                f"invalid host {self.host!r}: use a host name or address, "
                "with no leading dash or whitespace"
            )
        if self.user is not None and (
            not isinstance(self.user, str) or not _USER_RE.match(self.user)
        ):
            raise BootstrapError(f"invalid user {self.user!r}")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise BootstrapError(f"invalid port {self.port!r}: expected 1-65535")
        if self.identity_file is not None:
            text = str(self.identity_file)
            if (
                not text.strip()
                or text.startswith("-")
                or "\n" in text
                or "\r" in text
                or _KEY_MARKER in text
            ):
                raise BootstrapError(
                    "identity_file must be a path to a key file; key contents and "
                    "option-like values are rejected"
                )
            object.__setattr__(self, "identity_file", Path(text))
        os_key = _SUPPORTED_OS.get(str(self.os_family).strip().lower())
        if os_key is None:
            raise UnsupportedTargetError(
                f"unsupported OS family {self.os_family!r}: this bootstrap supports linux and darwin hosts"
            )
        arch_key = _ARCH_ALIASES.get(str(self.arch).strip().lower())
        if arch_key is None:
            raise UnsupportedTargetError(f"unsupported CPU architecture {self.arch!r}")
        object.__setattr__(self, "os_family", os_key)
        object.__setattr__(self, "arch", arch_key)
        if self.repo_path is not None:
            repo = self.repo_path
            if (
                not repo.strip()
                or repo.startswith("-")
                or "\n" in repo
                or "\r" in repo
                or "\x00" in repo
                or len(repo) > _MAX_REPO_PATH
            ):
                raise BootstrapError("repo_path must be a single path, not option-like or multi-line")


class ExecResult(NamedTuple):
    exit_code: int
    stdout: str
    stderr: str


class Executor(Protocol):
    """Runs one remote argv and returns its exit code and output."""

    def run(self, argv: Sequence[str]) -> ExecResult: ...


@dataclass(frozen=True)
class Probe:
    """A read-only remote command plus the predicate that reads its result."""

    argv: tuple[str, ...]
    check: Callable[[ExecResult], bool]
    label: str


@dataclass(frozen=True)
class Step:
    """One remote action.

    ``when`` runs first. When its check is false, the step is not applicable and
    is skipped. ``skip_if`` runs next. When its check is true, the work is
    already done and the step is skipped. ``expect`` runs on a successful action
    and must return true, or the bootstrap stops at this step.
    """

    key: str
    description: str
    argv: tuple[str, ...]
    expect: Callable[[ExecResult], bool] | None = None
    skip_if: Probe | None = None
    when: Probe | None = None


@dataclass(frozen=True)
class StepResult:
    key: str
    description: str
    status: str
    exit_code: int | None
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "description": self.description,
            "status": self.status,
            "exit_code": self.exit_code,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class BootstrapReport:
    host: str
    user: str | None
    port: int
    os_family: str
    arch: str
    steps: tuple[StepResult, ...]
    failed_step: str | None

    @property
    def ok(self) -> bool:
        return self.failed_step is None

    def to_dict(self) -> dict[str, object]:
        # Deliberately omits identity_file: a path, but not needed in a report.
        return {
            "host": self.host,
            "user": self.user,
            "port": self.port,
            "os_family": self.os_family,
            "arch": self.arch,
            "ok": self.ok,
            "failed_step": self.failed_step,
            "steps": [step.to_dict() for step in self.steps],
        }


# ---------------------------------------------------------------------------
# Output checks
# ---------------------------------------------------------------------------


def _exit_zero(result: ExecResult) -> bool:
    return result.exit_code == 0


def _exit_nonzero(result: ExecResult) -> bool:
    return result.exit_code != 0


def _stdout_equals(expected: str) -> Callable[[ExecResult], bool]:
    def check(result: ExecResult) -> bool:
        return result.stdout.strip() == expected

    return check


def _digest_matches(expected_hex: str) -> Callable[[ExecResult], bool]:
    """True when the digest tool exited 0 and its first token equals the pin."""

    def check(result: ExecResult) -> bool:
        if result.exit_code != 0:
            return False
        tokens = result.stdout.split()
        if not tokens:
            return False
        return hmac.compare_digest(
            tokens[0].lower().encode("ascii", "replace"),
            expected_hex.lower().encode("ascii"),
        )

    return check


def _uv_tool_listed(name: str, version: str) -> Callable[[ExecResult], bool]:
    """True when ``uv tool list`` shows ``<name> v<version>`` as a header line."""
    wanted = (name, f"v{version}")

    def check(result: ExecResult) -> bool:
        if result.exit_code != 0:
            return False
        return any(tuple(line.split()[:2]) == wanted for line in result.stdout.splitlines())

    return check


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def _digest_tool(os_family: str) -> tuple[str, ...]:
    if os_family == "darwin":
        return ("shasum", "-a", "256")
    return ("sha256sum",)


def _serena_serve_argv(repo_path: str | None, port: int) -> tuple[str, ...]:
    """Serena's streamable-http server, bound to loopback, headless."""
    argv = [
        "uvx", "--from", SERENA_PACKAGE, "serena", "start-mcp-server",
        "--context", "claude-code",
        "--transport", "streamable-http",
        "--host", "127.0.0.1",
        "--port", str(port),
        "--open-web-dashboard", "false",
    ]
    if repo_path:
        argv += ["--project", repo_path]
    return tuple(argv)


def plan_bootstrap(target: BootstrapTarget) -> list[Step]:
    """Return the ordered steps that bootstrap ``target``.

    Pure: this makes no remote calls. Raises :class:`UnsupportedTargetError`
    when no pinned codebase-memory-mcp build exists for the platform.
    """
    pin = _CBM_PINNED_ASSETS.get((target.os_family, target.arch))
    if pin is None:
        raise UnsupportedTargetError(
            f"no pinned codebase-memory-mcp build for {target.os_family}/{target.arch}"
        )
    asset_name, asset_sha256 = pin
    if not asset_name.endswith(".tar.gz"):
        raise UnsupportedTargetError(f"pinned asset {asset_name} is not a tar.gz archive")

    tag = _CBM_PINNED_TAG
    stage = f"{CACHE_DIR}/cbm-{tag}"
    archive = f"{stage}/{asset_name}"
    url = f"https://github.com/{_CBM_REPO}/releases/download/{tag}/{asset_name}"
    versioned = f"{BIN_DIR}/{CBM_BIN}-{tag}"
    link = f"{BIN_DIR}/{CBM_BIN}"
    digest = _digest_tool(target.os_family)
    port = SERENA_POOL_BASE_PORT
    serena_serve = _serena_serve_argv(target.repo_path, port)
    cbm_installed = Probe(("test", "-x", versioned), _exit_zero, f"{CBM_BIN} {tag} installed")
    serena_listed = _uv_tool_listed(SERENA_AGENT, SERENA_VERSION)
    serena_active = Probe(
        ("systemctl", "--user", "is-active", SERENA_UNIT),
        _stdout_equals("active"),
        f"{SERENA_UNIT} unit already active",
    )
    systemd_available = Probe(
        ("systemctl", "--user", "show-environment"),
        _exit_zero,
        "systemd --user available",
    )
    systemd_unavailable = Probe(
        ("systemctl", "--user", "show-environment"),
        _exit_nonzero,
        "systemd --user unavailable",
    )
    pidfile_alive = Probe(
        ("python3", "-c", _PIDCHECK_SRC, SERENA_PIDFILE),
        lambda r: r.exit_code == 0 and r.stdout.strip() == "alive",
        "Serena pidfile process alive",
    )
    health_argv = (
        "curl", "-sS", "-o", "/dev/null", "--max-time", "5",
        "--retry", "30", "--retry-delay", "2", "--retry-connrefused",
        f"http://127.0.0.1:{port}/mcp",
    )

    return [
        Step("prereq-uv", "Check uv is on PATH (Serena installs through uv)",
             ("uv", "--version")),
        Step("prereq-uvx", "Check uvx is on PATH (the Serena service runs through uvx)",
             ("uvx", "--version")),
        Step("prereq-curl", "Check curl is on PATH",
             ("curl", "--version")),
        Step("prereq-tar", "Check tar is on PATH",
             ("tar", "--version")),
        Step("prereq-digest", "Check the SHA-256 tool works",
             (*digest, "/dev/null"), expect=_digest_matches(_EMPTY_SHA256)),
        Step("dirs", "Create the Meridian bin, cache, run and log directories",
             ("mkdir", "-p", BIN_DIR, stage, RUN_DIR, LOG_DIR)),
        Step("serena-install", f"Install {SERENA_AGENT}=={SERENA_VERSION} with uv",
             ("uv", "tool", "install", SERENA_PACKAGE),
             skip_if=Probe(("uv", "tool", "list"), serena_listed,
                           f"{SERENA_AGENT} {SERENA_VERSION} installed with uv")),
        Step("serena-verify", f"Confirm {SERENA_AGENT} {SERENA_VERSION} is the installed version",
             ("uv", "tool", "list"), expect=serena_listed),
        Step("cbm-download",
             f"Download the pinned codebase-memory-mcp {tag} archive ({asset_name})",
             ("curl", "-fsSL", "--proto", "=https", "--tlsv1.2", "-o", archive, url),
             skip_if=cbm_installed),
        Step("cbm-verify-sha256",
             "Check the archive SHA-256 against the pin before extracting anything",
             (*digest, archive),
             expect=_digest_matches(asset_sha256),
             skip_if=cbm_installed),
        Step("cbm-extract", "Extract the verified archive into the staging directory",
             ("tar", "-xzf", archive, "-C", stage),
             skip_if=cbm_installed),
        Step("cbm-install",
             f"Install the {CBM_BIN} executable as {versioned}",
             ("find", stage, "-type", "f", "-name", CBM_BIN,
              "-exec", "install", "-m", "0755", "{}", versioned, ";"),
             skip_if=cbm_installed),
        Step("cbm-verify-binary", "Confirm the installed executable exists and is executable",
             ("test", "-x", versioned)),
        Step("cbm-link", f"Point {link} at the pinned build",
             ("ln", "-sfn", f"{CBM_BIN}-{tag}", link),
             skip_if=Probe(("test", "-L", link), _exit_zero, f"{link} link present")),
        Step("serena-start-systemd",
             f"Start Serena as the systemd --user unit {SERENA_UNIT} on 127.0.0.1",
             ("systemd-run", "--user", f"--unit={SERENA_UNIT}", "--collect",
              "--property=Restart=on-failure", *serena_serve),
             when=systemd_available,
             skip_if=serena_active),
        Step("serena-start-pidfile",
             "Start Serena detached with a pidfile on 127.0.0.1 (no systemd --user)",
             ("python3", "-c", _DETACH_SRC, SERENA_PIDFILE, SERENA_LOGFILE, *serena_serve),
             when=systemd_unavailable,
             skip_if=pidfile_alive),
        Step("serena-listening", f"Wait for Serena to answer on 127.0.0.1:{port}",
             health_argv),
    ]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _tail(text: str) -> str:
    text = text.strip()
    return text[-_OUTPUT_TAIL_CHARS:] if len(text) > _OUTPUT_TAIL_CHARS else text


def _run_step(step: Step, executor: Executor) -> StepResult:
    if step.when is not None:
        probe = executor.run(step.when.argv)
        if not step.when.check(probe):
            return StepResult(step.key, step.description, STATUS_NOT_APPLICABLE,
                              probe.exit_code, f"precondition not met: {step.when.label}")
    if step.skip_if is not None:
        probe = executor.run(step.skip_if.argv)
        if step.skip_if.check(probe):
            return StepResult(step.key, step.description, STATUS_SKIPPED,
                              probe.exit_code, f"already done: {step.skip_if.label}")
    outcome = executor.run(step.argv)
    if outcome.exit_code != 0:
        detail = _tail(outcome.stderr) or _tail(outcome.stdout) or f"exit code {outcome.exit_code}"
        return StepResult(step.key, step.description, STATUS_FAILED, outcome.exit_code, detail)
    if step.expect is not None and not step.expect(outcome):
        # The output itself is not echoed: it may be sensitive, and the check is the point.
        return StepResult(step.key, step.description, STATUS_FAILED, outcome.exit_code,
                          "output check failed; later steps were not run")
    return StepResult(step.key, step.description, STATUS_OK, outcome.exit_code, "")


def bootstrap(target: BootstrapTarget, executor: Executor) -> BootstrapReport:
    """Run the plan on ``executor``. Stops at the first failing step.

    Idempotent: each state-changing step first probes whether it is already
    done, and skips it when so. Re-running a finished bootstrap changes nothing.
    """
    results: list[StepResult] = []
    failed_step: str | None = None
    for step in plan_bootstrap(target):
        result = _run_step(step, executor)
        results.append(result)
        logger.info("ssh-bootstrap host=%s step=%s status=%s", target.host, step.key, result.status)
        if result.status == STATUS_FAILED:
            failed_step = step.key
            break
    return BootstrapReport(
        host=target.host,
        user=target.user,
        port=target.port,
        os_family=target.os_family,
        arch=target.arch,
        steps=tuple(results),
        failed_step=failed_step,
    )


def remote_command_string(argv: Sequence[str]) -> str:
    """Quote an argv for the remote shell, one element at a time."""
    if not argv:
        raise BootstrapError("empty remote command")
    parts: list[str] = []
    for arg in argv:
        if not isinstance(arg, str) or "\x00" in arg:
            raise BootstrapError("remote argv elements must be strings without NUL bytes")
        parts.append(shlex.quote(arg))
    return " ".join(parts)


@dataclass(frozen=True)
class SshExecutor:
    """Runs remote argv over the system ``ssh`` client, with no shell on this side.

    ``BatchMode=yes`` stops ssh from prompting, so an unreachable or
    unauthenticated host fails instead of hanging. ``accept-new`` is the
    documented host-key policy (see the module docstring).
    """

    target: BootstrapTarget
    timeout_seconds: float = 900.0
    ssh_program: str = "ssh"

    def build_argv(self, remote_argv: Sequence[str]) -> list[str]:
        t = self.target
        argv = [
            self.ssh_program,
            "-T",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=20",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-p", str(t.port),
        ]
        if t.identity_file is not None:
            argv += ["-i", str(t.identity_file), "-o", "IdentitiesOnly=yes"]
        destination = f"{t.user}@{t.host}" if t.user else t.host
        argv.append(destination)
        argv.append(remote_command_string(remote_argv))
        return argv

    def run(self, argv: Sequence[str]) -> ExecResult:
        command = self.build_argv(argv)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ExecResult(124, "", f"ssh timed out after {self.timeout_seconds:.0f}s")
        except OSError as exc:
            return ExecResult(127, "", f"could not start ssh: {exc.strerror or type(exc).__name__}")
        return ExecResult(completed.returncode, completed.stdout, completed.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _display_arg(arg: str) -> str:
    return "<inline script>" if "\n" in arg else arg


def _print_plan(steps: Sequence[Step], stream) -> None:
    for index, step in enumerate(steps, 1):
        print(f"{index:2}. [{step.key}] {step.description}", file=stream)
        if step.when is not None:
            print(f"      only when: {step.when.label}", file=stream)
        if step.skip_if is not None:
            print(f"      skip when: {step.skip_if.label}", file=stream)
        print("      $ " + " ".join(shlex.quote(_display_arg(a)) for a in step.argv), file=stream)


def cli_main(argv: Sequence[str] | None = None, *, executor_factory=None) -> int:
    """``python -m meridian ssh-bootstrap`` entry point.

    Exit codes: 0 when every step passed or was skipped, 1 when a step failed,
    2 for invalid arguments. ``--plan`` prints the steps and runs nothing.
    """
    parser = argparse.ArgumentParser(
        prog="meridian ssh-bootstrap",
        description="Install Serena and codebase-memory-mcp on an SSH host, bound to 127.0.0.1.",
    )
    parser.add_argument("--host", required=True, help="Host name or address.")
    parser.add_argument("--user", default=None, help="SSH user (defaults to the ssh config).")
    parser.add_argument("--port", type=int, default=22, help="SSH port (default 22).")
    parser.add_argument(
        "--identity-file",
        default=None,
        help="Path to a private key. Passed to ssh with -i; Meridian never reads the file.",
    )
    parser.add_argument("--os", dest="os_family", default="linux",
                        help="Remote OS family: linux or darwin (default linux).")
    parser.add_argument("--arch", default="amd64", help="Remote CPU: amd64 or arm64 (default amd64).")
    parser.add_argument("--repo", dest="repo_path", default=None,
                        help="Project path Serena serves (--project).")
    parser.add_argument("--plan", action="store_true", help="Print the plan and run nothing.")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
    args = parser.parse_args(argv)

    try:
        target = BootstrapTarget(
            host=args.host,
            user=args.user,
            port=args.port,
            identity_file=Path(args.identity_file) if args.identity_file else None,
            os_family=args.os_family,
            arch=args.arch,
            repo_path=args.repo_path,
        )
        steps = plan_bootstrap(target)
    except BootstrapError as exc:
        print(f"ssh-bootstrap: {exc}", file=sys.stderr)
        return 2

    if args.plan:
        _print_plan(steps, sys.stdout)
        return 0

    factory = executor_factory or SshExecutor
    report = bootstrap(target, factory(target))
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        for result in report.steps:
            print(f"[{result.status:>14}] {result.key}: {result.description}")
            if result.status == STATUS_FAILED and result.detail:
                print(f"                 {result.detail}", file=sys.stderr)
        print("ssh-bootstrap: ok" if report.ok else f"ssh-bootstrap: failed at {report.failed_step}")
    return 0 if report.ok else 1
