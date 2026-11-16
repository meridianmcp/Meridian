"""Tests for meridian_desktop/runner_client.py.

No test starts the core runner or reads a real home directory. The argv and verb tests use a fake
spawn that records each call. The output tests run a fixed script in a real child Python, in place
of the core command, so the client's timeout, pipe and exit-code handling are real and no Meridian
code runs. The missing-command tests use a path that does not exist, so nothing is started.
"""

import subprocess
import sys

import pytest

from meridian_desktop.runner_client import (
    DEFAULT_TAIL_BYTES,
    DEFAULT_TIMEOUT,
    ERR_BAD_OUTPUT,
    ERR_BAD_SCOPE,
    ERR_NONZERO_EXIT,
    ERR_NOT_AVAILABLE,
    ERR_TIMEOUT,
    ERR_TOO_LARGE,
    MAX_OUTPUT_BYTES,
    MAX_TAIL_BYTES,
    MODULE,
    MUTATING_VERBS,
    READ_VERBS,
    RunnerClient,
    RunnerVerbRefused,
    scope_is_usable,
)

CORE_PYTHON = "core-python"
SCOPE = "scope-a"


class FakeSpawn:
    """Records each call and returns a canned CompletedProcess, or raises ``raises``."""

    def __init__(
        self,
        *,
        stdout: bytes = b"{}",
        stderr: bytes = b"",
        returncode: int = 0,
        raises: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self._stdout = stdout
        self._stderr = stderr
        self._returncode = returncode
        self._raises = raises

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        if self._raises is not None:
            raise self._raises
        return subprocess.CompletedProcess(argv, self._returncode, self._stdout, self._stderr)


def _child_spawn(script: str):
    """A spawn that runs ``script`` in a real child Python instead of the core command.

    The client's keyword arguments (timeout, pipes, cwd) are passed through unchanged.
    """

    def spawn(argv, **kwargs):
        return subprocess.run([sys.executable, "-c", script], **kwargs)

    return spawn


def test_runner_client_never_invokes_mutating_verbs():
    spawn = FakeSpawn()
    client = RunnerClient(python=CORE_PYTHON, spawn=spawn)
    refused = (*MUTATING_VERBS, "list-scripts", "", "STATUS", "status ", "status --scope x", None)
    for verb in refused:
        with pytest.raises(RunnerVerbRefused):
            client.run(verb, SCOPE)
    assert spawn.calls == [], "a refused verb must raise before any process is started"

    assert READ_VERBS == ("status", "tail-log")
    client.status(SCOPE)
    client.tail_log(SCOPE)
    assert [argv[3] for argv, _ in spawn.calls] == ["status", "tail-log"]
    for argv, _ in spawn.calls:
        assert not set(argv) & set(MUTATING_VERBS)


def test_argv_is_a_list_with_no_shell_and_one_scope_element():
    spawn = FakeSpawn()
    scope = "two words; rm -rf $HOME && echo x"
    RunnerClient(python=CORE_PYTHON, spawn=spawn).status(scope)
    argv, kwargs = spawn.calls[0]
    assert isinstance(argv, list)
    assert argv == [CORE_PYTHON, "-m", MODULE, "status", "--scope", scope]
    assert kwargs["shell"] is False


def test_tail_log_passes_a_bounded_max_bytes():
    spawn = FakeSpawn()
    client = RunnerClient(python=CORE_PYTHON, spawn=spawn)
    client.tail_log(SCOPE)
    client.tail_log(SCOPE, max_bytes=4096)
    assert DEFAULT_TAIL_BYTES == 8000
    assert spawn.calls[0][0][-2:] == ["--max-bytes", str(DEFAULT_TAIL_BYTES)]
    assert spawn.calls[1][0][-2:] == ["--max-bytes", "4096"]
    assert [argv[3] for argv, _ in spawn.calls] == ["tail-log", "tail-log"]
    for bad in (0, -1, True, MAX_TAIL_BYTES + 1, "10", 1.5):
        with pytest.raises(ValueError):
            client.tail_log(SCOPE, max_bytes=bad)
    assert len(spawn.calls) == 2, "an invalid max_bytes must not spawn"


@pytest.mark.parametrize("scope", ["", "   ", "-x", "--scope", "a\x00b", "a\nb", "x" * 129])
def test_bad_scope_is_refused_without_a_process(scope):
    spawn = FakeSpawn()
    result = RunnerClient(python=CORE_PYTHON, spawn=spawn).status(scope)
    assert result.ok is False
    assert result.error_code == ERR_BAD_SCOPE
    assert spawn.calls == []


def test_scope_length_limit_is_inclusive():
    assert scope_is_usable("x" * 128)
    assert not scope_is_usable("x" * 129)
    assert not scope_is_usable(None)


def test_no_interpreter_configured_reports_not_available_without_spawning():
    spawn = FakeSpawn()
    result = RunnerClient(spawn=spawn).status(SCOPE)
    assert result.ok is False
    assert result.error_code == ERR_NOT_AVAILABLE
    assert spawn.calls == []


def test_timeout_is_passed_through_and_a_timeout_returns_an_error_object():
    spawn = FakeSpawn(raises=subprocess.TimeoutExpired(cmd="core", timeout=DEFAULT_TIMEOUT))
    result = RunnerClient(python=CORE_PYTHON, spawn=spawn).status(SCOPE)
    assert DEFAULT_TIMEOUT == 10.0
    assert spawn.calls[0][1]["timeout"] == DEFAULT_TIMEOUT
    assert result.ok is False
    assert result.error_code == ERR_TIMEOUT
    assert result.exit_code is None


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "10"])
def test_client_rejects_a_bad_timeout(timeout):
    with pytest.raises(ValueError):
        RunnerClient(python=CORE_PYTHON, timeout=timeout)


def test_missing_command_reports_not_available():
    spawn = FakeSpawn(raises=FileNotFoundError(2, "No such file"))
    result = RunnerClient(python=CORE_PYTHON, spawn=spawn).status(SCOPE)
    assert result.ok is False
    assert result.error_code == ERR_NOT_AVAILABLE
    assert result.exit_code is None


def test_missing_core_module_on_stderr_reports_not_available():
    spawn = FakeSpawn(
        returncode=1,
        stdout=b"",
        stderr=b"ModuleNotFoundError: No module named 'meridian'",
    )
    result = RunnerClient(python=CORE_PYTHON, spawn=spawn).status(SCOPE)
    assert result.error_code == ERR_NOT_AVAILABLE
    assert result.exit_code == 1


def test_missing_interpreter_on_this_computer_reports_not_available(tmp_path):
    client = RunnerClient(python=str(tmp_path / "no-such-python.exe"))
    result = client.status(SCOPE)
    assert result.ok is False
    assert result.error_code == ERR_NOT_AVAILABLE


def test_missing_working_directory_reports_not_available(tmp_path):
    client = RunnerClient(python=sys.executable, cwd=tmp_path / "absent")
    result = client.status(SCOPE)
    assert result.ok is False
    assert result.error_code == ERR_NOT_AVAILABLE


@pytest.mark.parametrize(
    "stdout",
    [b"plain text", b"", b"[1, 2]", b"\xff\xfe", b'{"a": '],
)
def test_non_json_stdout_returns_error_with_exit_code(stdout):
    spawn = FakeSpawn(stdout=stdout, returncode=3)
    result = RunnerClient(python=CORE_PYTHON, spawn=spawn).status(SCOPE)
    assert result.ok is False
    assert result.error_code == ERR_BAD_OUTPUT
    assert result.exit_code == 3
    assert "3" in result.error_message


def test_nonzero_exit_with_a_json_object_is_an_error():
    spawn = FakeSpawn(stdout=b'{"a": 1}', returncode=1)
    result = RunnerClient(python=CORE_PYTHON, spawn=spawn).status(SCOPE)
    assert result.ok is False
    assert result.error_code == ERR_NONZERO_EXIT
    assert result.exit_code == 1
    assert result.payload is None


def test_success_returns_the_parsed_object():
    body = b'{"scope": "scope-a", "child": {"state": "running"}}'
    spawn = FakeSpawn(stdout=body, returncode=0)
    result = RunnerClient(python=CORE_PYTHON, spawn=spawn).status(SCOPE)
    assert result.ok is True
    assert result.payload == {"scope": "scope-a", "child": {"state": "running"}}
    assert result.error_code is None
    assert result.exit_code == 0


def test_oversize_output_is_refused_before_parsing():
    spawn = FakeSpawn(stdout=b"{" + b" " * MAX_OUTPUT_BYTES + b"}", returncode=0)
    result = RunnerClient(python=CORE_PYTHON, spawn=spawn).status(SCOPE)
    assert result.ok is False
    assert result.error_code == ERR_TOO_LARGE
    assert result.payload is None


def test_error_messages_never_echo_runner_output():
    spawn = FakeSpawn(
        stdout=b"SECRET-LINE sk_meridian_abcdef token=abc123",
        stderr=b"C:/Users/someone/secret.txt",
        returncode=2,
    )
    result = RunnerClient(python=CORE_PYTHON, spawn=spawn).tail_log(SCOPE)
    assert result.error_code == ERR_BAD_OUTPUT
    for leaked in ("SECRET", "sk_meridian", "token=", "Users", "secret.txt"):
        assert leaked not in result.error_message


def test_real_child_json_object_is_parsed():
    client = RunnerClient(python=CORE_PYTHON, spawn=_child_spawn("print('{\"ok_line\": 1}')"))
    result = client.status(SCOPE)
    assert result.ok is True
    assert result.payload == {"ok_line": 1}


def test_real_child_that_runs_past_the_timeout_returns_timeout():
    client = RunnerClient(
        python=CORE_PYTHON,
        timeout=0.5,
        spawn=_child_spawn("import time; time.sleep(60)"),
    )
    result = client.status(SCOPE)
    assert result.ok is False
    assert result.error_code == ERR_TIMEOUT


def test_real_child_non_json_output_keeps_its_exit_code():
    client = RunnerClient(
        python=CORE_PYTHON,
        spawn=_child_spawn("import sys; print('not json'); sys.exit(4)"),
    )
    result = client.status(SCOPE)
    assert result.ok is False
    assert result.error_code == ERR_BAD_OUTPUT
    assert result.exit_code == 4
