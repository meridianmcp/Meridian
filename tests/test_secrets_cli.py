"""Tests for the secrets broker command line (secrets broker slice 2).

Each test injects an in-memory credential store and a temporary vault, so the OS
credential store is never touched. Passphrases come from a fake reader, not the
terminal. The approval window is mocked wherever a SENSITIVE item is run.
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

import pytest

from meridian import secrets_broker as sb
from meridian import secrets_cli as cli
from meridian import secrets_prompt

PASSPHRASE = "FAKE-passphrase-123"
WRONG_PASSPHRASE = "FAKE-wrong-passphrase-9"
VALUE = "FAKE-VALUE-123"
NAME = "FAKE_API_KEY"
GROUP = "fake-group"
PURPOSE = "fake test key"
PY = sys.executable
RECOVERY_CODE = re.compile(r"[A-Z2-7]{4}(?:-[A-Z2-7]{4}){7}")
COMMON_PASSPHRASE = "000000000000"  # a bundled common-list entry (see test_secrets_broker)


class InMemoryCredentialStore:
    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def read(self, target: str) -> str | None:
        return self.items.get(target)

    def write(self, target: str, text: str) -> None:
        self.items[target] = text


class _Reader:
    """Fake getpass: returns scripted answers and records every prompt it was asked."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected passphrase prompt: {prompt!r}")
        return self.answers.pop(0)


class _FakeBroker:
    """Stands in for a broker so each run status can be mapped to an exit code."""

    def __init__(self, result: sb.RunResult) -> None:
        self.result = result

    def unlock(self, passphrase: str) -> None:
        pass

    def run_with_secret(self, name, argv, *, approver, client):
        return self.result

    def list_names(self):
        return []

    def close(self) -> None:
        pass


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def store() -> InMemoryCredentialStore:
    return InMemoryCredentialStore()


@pytest.fixture
def factory(vault_dir, store):
    return lambda: sb.SecretsBroker(vault_dir, store)


def _run(argv, factory, reader=None, approver=None):
    out, err = io.StringIO(), io.StringIO()
    code = cli.cli_main(
        argv,
        broker_factory=factory,
        passphrase_reader=reader if reader is not None else _Reader(),
        approver=approver,
        stdout=out,
        stderr=err,
    )
    return code, out.getvalue(), err.getvalue()


def _seed(vault_dir: Path, store: InMemoryCredentialStore, *items: dict) -> str:
    """Initialise the broker and store the given items. Returns the recovery code."""
    broker = sb.SecretsBroker(vault_dir, store)
    recovery = broker.initialize(PASSPHRASE)
    for item in items:
        broker.put_secret(
            item["name"],
            item["value"],
            group=item.get("group", GROUP),
            purpose=item.get("purpose", PURPOSE),
            allowed_commands=item.get("allowed", [PY]),
            sensitivity=item.get("sensitivity", "SECRET"),
        )
    broker.close()
    return recovery


def _match_code(name: str, expected: str) -> str:
    return (
        "import os, sys\n"
        f"sys.stdout.write('match' if os.environ[{name!r}] == {expected!r} else 'mismatch')\n"
    )


def _marker_code(marker: Path) -> str:
    return f"open({str(marker)!r}, 'w').close()"


# -- init -------------------------------------------------------------------


def test_init_creates_the_vault_and_prints_the_recovery_code_once(factory, store, vault_dir):
    reader = _Reader(PASSPHRASE, PASSPHRASE)
    code, out, err = _run(["init"], factory, reader)
    assert code == 0
    codes = RECOVERY_CODE.findall(out)
    assert len(codes) == 1
    recovery = codes[0]
    assert "WRITE THIS DOWN" in out
    assert PASSPHRASE not in out and PASSPHRASE not in err
    assert len(reader.prompts) == 2
    # The code is never stored: not in the credential store, not in the vault.
    stored = json.dumps(store.items)
    assert recovery not in stored and recovery.replace("-", "") not in stored
    unlocked = sb.SecretsBroker(vault_dir, store)
    unlocked.unlock_with_recovery(recovery)
    assert unlocked.is_unlocked()
    unlocked.close()


def test_init_refuses_a_short_passphrase_and_stores_nothing(factory, store):
    reader = _Reader("FAKE-tiny-1")
    code, out, err = _run(["init"], factory, reader)
    assert code == 1
    assert "at least 12 characters" in err
    assert "nothing was stored" in err
    assert "FAKE-tiny-1" not in out + err
    assert len(reader.prompts) == 1  # no confirmation prompt after a failed check
    assert store.items == {}


def test_init_refuses_a_common_passphrase_without_echoing_it(factory, store):
    code, out, err = _run(["init"], factory, _Reader(COMMON_PASSPHRASE))
    assert code == 1
    assert "common-password list" in err
    assert COMMON_PASSPHRASE not in out + err
    assert store.items == {}


def test_init_refuses_when_the_repeat_does_not_match(factory, store):
    code, _, err = _run(["init"], factory, _Reader("FAKE-pass-12", "FAKE-pass-13"))
    assert code == 1
    assert "do not match" in err
    assert store.items == {}


def test_init_refuses_when_already_initialized(factory, store, vault_dir):
    _seed(vault_dir, store)
    code, _, err = _run(["init"], factory, _Reader())
    assert code == 1
    assert "already initialized" in err


def test_init_rejects_extra_arguments(factory):
    code, _, err = _run(["init", "extra"], factory, _Reader())
    assert code == 2
    assert "usage" in err


# -- list -------------------------------------------------------------------


def test_list_shows_names_groups_classes_and_purposes_but_no_values(factory, store, vault_dir):
    _seed(
        vault_dir,
        store,
        {"name": "FAKE_ZETA_KEY", "value": VALUE, "group": "g2"},
        {"name": "FAKE_ALPHA_KEY", "value": VALUE, "group": "g1", "sensitivity": "SENSITIVE"},
        {"name": "FAKE_PUBLIC_ITEM", "value": "FAKE-public-1", "group": "g3", "sensitivity": "PUBLIC"},
    )
    reader = _Reader()
    code, out, _ = _run(["list"], factory, reader)
    assert code == 0
    assert reader.prompts == []  # no passphrase needed
    lines = out.splitlines()
    assert lines[0] == "NAME\tGROUP\tCLASS\tPURPOSE"
    assert lines[1] == f"FAKE_ALPHA_KEY\tg1\tSENSITIVE\t{PURPOSE}"
    assert lines[2] == f"FAKE_PUBLIC_ITEM\tg3\tPUBLIC\t{PURPOSE}"
    assert lines[3] == f"FAKE_ZETA_KEY\tg2\tSECRET\t{PURPOSE}"
    assert VALUE not in out
    assert "FAKE-public-1" not in out


def test_list_on_an_empty_vault_says_so(factory):
    code, out, _ = _run(["list"], factory, _Reader())
    assert code == 0
    assert out == "No secrets are stored.\n"


def test_list_shows_a_control_character_in_a_purpose_escaped_on_one_line(factory, store, vault_dir):
    _seed(vault_dir, store, {"name": NAME, "value": VALUE, "purpose": "first\nsecond"})
    code, out, _ = _run(["list"], factory, _Reader())
    assert code == 0
    assert len(out.splitlines()) == 2
    assert "first\\nsecond" in out


def test_list_extra_arguments_are_a_usage_error(factory):
    code, _, err = _run(["list", "--all"], factory, _Reader())
    assert code == 2
    assert "usage" in err


# -- run --------------------------------------------------------------------


def test_run_passes_a_completed_command_its_redacted_output_and_exit_code(factory, store, vault_dir, tmp_path):
    _seed(vault_dir, store, {"name": NAME, "value": VALUE})
    reader = _Reader(PASSPHRASE)
    code, out, err = _run(["run", NAME, "--", PY, "-c", _match_code(NAME, VALUE)], factory, reader)
    assert code == 0
    assert out == "match"
    assert err == ""
    assert reader.prompts == ["Passphrase: "]


def test_run_returns_the_commands_own_exit_code(factory, store, vault_dir):
    _seed(vault_dir, store, {"name": NAME, "value": VALUE})
    code, _, _ = _run(["run", NAME, "--", PY, "-c", "import sys; sys.exit(5)"], factory, _Reader(PASSPHRASE))
    assert code == 5


def test_run_with_the_wrong_passphrase_does_not_run_and_echoes_nothing(factory, store, vault_dir, tmp_path):
    _seed(vault_dir, store, {"name": NAME, "value": VALUE})
    marker = tmp_path / "ran.txt"
    code, out, err = _run(
        ["run", NAME, "--", PY, "-c", _marker_code(marker)], factory, _Reader(WRONG_PASSPHRASE)
    )
    assert code == 1
    assert not marker.exists()
    assert WRONG_PASSPHRASE not in out + err


def test_run_of_an_unknown_name_is_an_error(factory, store, vault_dir):
    _seed(vault_dir, store, {"name": NAME, "value": VALUE})
    code, _, err = _run(["run", "FAKE_MISSING", "--", PY, "-c", "pass"], factory, _Reader(PASSPHRASE))
    assert code == 1
    assert "no secret named" in err


def test_a_sensitive_run_asks_the_window_and_is_denied_when_the_person_says_no(factory, store, vault_dir, tmp_path, monkeypatch):
    _seed(vault_dir, store, {"name": NAME, "value": VALUE, "sensitivity": "SENSITIVE"})
    asked: list[tuple] = []

    def fake_window(*args):
        asked.append(args)
        return False

    monkeypatch.setattr(secrets_prompt, "ask_approval", fake_window)
    marker = tmp_path / "ran.txt"
    code, out, err = _run(["run", NAME, "--", PY, "-c", _marker_code(marker)], factory, _Reader(PASSPHRASE))
    assert code == 3
    assert not marker.exists()
    assert "denied" in err
    assert len(asked) == 1
    name, purpose, group, command, argv, client = asked[0]
    assert (name, purpose, group) == (NAME, PURPOSE, GROUP)
    assert command == PY
    assert argv == (PY, "-c", _marker_code(marker))
    assert client.startswith("meridian secrets run (pid ")
    assert VALUE not in json.dumps([str(part) for part in asked[0]])
    entries = [json.loads(line) for line in (vault_dir / sb.AUDIT_FILE_NAME).read_text(encoding="utf-8").splitlines()]
    assert [entry["outcome"] for entry in entries] == ["denied"]


def test_a_sensitive_run_the_person_approves_runs_once(factory, store, vault_dir, tmp_path, monkeypatch):
    _seed(vault_dir, store, {"name": NAME, "value": VALUE, "sensitivity": "SENSITIVE"})
    monkeypatch.setattr(secrets_prompt, "ask_approval", lambda *args: True)
    marker = tmp_path / "ran.txt"
    code, _, _ = _run(["run", NAME, "--", PY, "-c", _marker_code(marker)], factory, _Reader(PASSPHRASE))
    assert code == 0
    assert marker.exists()


def test_an_explicit_approver_replaces_the_window(factory, store, vault_dir, tmp_path, monkeypatch):
    _seed(vault_dir, store, {"name": NAME, "value": VALUE, "sensitivity": "SENSITIVE"})

    def must_not_open(*args):
        raise AssertionError("the default window must not open when an approver is given")

    monkeypatch.setattr(secrets_prompt, "ask_approval", must_not_open)
    marker = tmp_path / "ran.txt"
    code, _, _ = _run(
        ["run", NAME, "--", PY, "-c", _marker_code(marker)],
        factory,
        _Reader(PASSPHRASE),
        approver=lambda request: True,
    )
    assert code == 0
    assert marker.exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["run"],
        ["run", NAME],
        ["run", NAME, "--"],
        ["run", "FAKE_A", "FAKE_B", "--", PY],
        ["run", "--", PY],
    ],
)
def test_run_usage_errors_exit_two_without_prompting(factory, argv):
    reader = _Reader()
    code, _, err = _run(argv, factory, reader)
    assert code == 2
    assert "usage: python -m meridian secrets run" in err
    assert reader.prompts == []


@pytest.mark.parametrize(
    "status, exit_code, expected_code",
    [
        ("completed", 7, 7),
        ("completed", None, 1),
        ("timed_out", None, cli.EXIT_TIMED_OUT),
        ("denied", None, cli.EXIT_DENIED),
        ("approval_required", None, 1),
    ],
)
def test_each_run_status_maps_to_one_exit_code(status, exit_code, expected_code):
    fake = _FakeBroker(sb.RunResult(status=status, exit_code=exit_code, stdout="", stderr=""))
    code, _, _ = _run(["run", NAME, "--", PY, "-c", "pass"], lambda: fake, _Reader(PASSPHRASE))
    assert code == expected_code


def test_a_timed_out_run_reports_it_on_stderr():
    fake = _FakeBroker(sb.RunResult(status="timed_out", exit_code=None, stdout="partial", stderr=""))
    code, out, err = _run(["run", NAME, "--", PY, "-c", "pass"], lambda: fake, _Reader(PASSPHRASE))
    assert code == cli.EXIT_TIMED_OUT
    assert out == "partial"
    assert "timed out" in err


# -- dispatch and errors ----------------------------------------------------


def test_no_arguments_prints_usage_and_exits_two(factory):
    code, out, err = _run([], factory)
    assert code == 2
    assert "usage: python -m meridian secrets" in err
    assert out == ""


def test_help_prints_usage_and_exits_zero(factory):
    code, out, _ = _run(["--help"], factory)
    assert code == 0
    assert "init" in out and "list" in out and "run NAME" in out


def test_an_unknown_subcommand_is_a_usage_error(factory):
    code, _, err = _run(["export"], factory)
    assert code == 2
    assert "unknown command 'export'" in err


def test_a_broker_error_becomes_exit_one_without_a_traceback():
    def failing_factory():
        raise sb.CredentialStoreError("credential store is unavailable")

    code, _, err = _run(["list"], failing_factory)
    assert code == 1
    assert "credential store is unavailable" in err


def test_a_platform_without_a_credential_store_is_reported_not_raised():
    def unsupported():
        raise NotImplementedError("Linux credential store adapter is not implemented in slice 1")

    code, _, err = _run(["init"], unsupported, _Reader(PASSPHRASE, PASSPHRASE))
    assert code == 1
    assert "not implemented" in err


def test_the_package_entrypoint_dispatches_secrets_list(monkeypatch, vault_dir, store, capsys):
    from meridian.__main__ import main

    _seed(vault_dir, store, {"name": NAME, "value": VALUE})
    monkeypatch.setattr(sb, "open_default_broker", lambda *args, **kwargs: sb.SecretsBroker(vault_dir, store))
    assert main(["secrets", "list"]) == 0
    assert "FAKE_API_KEY" in capsys.readouterr().out


def test_the_package_entrypoint_gives_secrets_usage_on_its_own(capsys):
    from meridian.__main__ import main

    assert main(["secrets"]) == 2
    assert "usage: python -m meridian secrets" in capsys.readouterr().err
