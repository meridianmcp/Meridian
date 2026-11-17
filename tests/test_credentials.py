"""Tests for meridian.credentials (the credential reference resolver).

No test reads the real home directory. The runpodctl cases point HOME and
USERPROFILE at a temporary directory and assert that Path.home() resolves
there before anything is read. The keyring cases inject a fake module.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from meridian import credentials
from meridian.credentials import (
    CredentialError,
    ResolvedCredential,
    describe_source,
    resolve_credential,
)

_SECRET = "rpa_TESTONLY_placeholder_secret_0123456789"


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert Path.home() == tmp_path  # the real home directory is never touched
    return tmp_path


def _write_config(home: Path, text: str) -> Path:
    config_dir = home / ".runpod"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _chain_links(exc: BaseException) -> list[BaseException]:
    """``exc`` and every exception reachable from it through ``__context__`` or ``__cause__``."""
    links: list[BaseException] = []
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if any(current is seen for seen in links):
            continue
        links.append(current)
        pending.extend(link for link in (current.__context__, current.__cause__) if link is not None)
    return links


def _assert_secret_absent_from_chain(exc: BaseException) -> None:
    """No exception in the chain may show the secret through str, repr or args."""
    for link in _chain_links(exc):
        texts = [str(link), repr(link), repr(link.args), *map(str, link.args), *map(repr, link.args)]
        assert not any(_SECRET in text for text in texts), f"{type(link).__name__} in the chain carries the secret"


# ---------------------------------------------------------------------------
# env:NAME
# ---------------------------------------------------------------------------


def test_env_ref_resolves_when_set(monkeypatch):
    # Local only: env references are refused when hosted (see the hosted tests below).
    monkeypatch.setenv("MERIDIAN_TEST_RUNPOD_KEY", _SECRET)
    cred = resolve_credential("env:MERIDIAN_TEST_RUNPOD_KEY", hosted=False)
    assert cred.kind == "env"
    assert cred.secret == _SECRET


def test_env_ref_unset_error_names_the_variable_only(monkeypatch):
    monkeypatch.delenv("MERIDIAN_TEST_UNSET_VAR", raising=False)
    with pytest.raises(CredentialError) as exc_info:
        resolve_credential("env:MERIDIAN_TEST_UNSET_VAR", hosted=False)
    assert "MERIDIAN_TEST_UNSET_VAR" in str(exc_info.value)


def test_env_ref_empty_value_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("MERIDIAN_TEST_EMPTY_VAR", "")
    with pytest.raises(CredentialError, match="MERIDIAN_TEST_EMPTY_VAR"):
        resolve_credential("env:MERIDIAN_TEST_EMPTY_VAR", hosted=False)


def test_env_ref_is_refused_when_hosted_before_any_environment_read(monkeypatch):
    # A hosted server must not send its own environment as a credential.
    def must_not_be_called(name):
        raise AssertionError("hosted resolution must not read environment variables")

    monkeypatch.setattr(credentials, "_resolve_env", must_not_be_called)
    with pytest.raises(CredentialError, match="local-only"):
        resolve_credential("env:MERIDIAN_TEST_RUNPOD_KEY", hosted=True)


def test_hosted_env_refusal_is_the_same_whether_or_not_the_variable_exists(monkeypatch):
    # Otherwise the error would reveal whether a server variable exists.
    monkeypatch.setenv("MERIDIAN_TEST_SET_ON_SERVER", _SECRET)
    monkeypatch.delenv("MERIDIAN_TEST_UNSET_ON_SERVER", raising=False)
    messages = []
    for name in ("MERIDIAN_TEST_SET_ON_SERVER", "MERIDIAN_TEST_UNSET_ON_SERVER"):
        with pytest.raises(CredentialError) as exc_info:
            resolve_credential(f"env:{name}", hosted=True)
        messages.append(str(exc_info.value))
    assert messages[0] == messages[1]
    assert _SECRET not in messages[0]


@pytest.mark.parametrize("ref", ["env:1STARTS_WITH_DIGIT", "env:has-dash", "env:"])
def test_env_ref_rejects_invalid_names(ref):
    with pytest.raises(CredentialError):
        resolve_credential(ref, hosted=False)


def test_env_ref_with_trailing_newline_is_not_a_valid_name(monkeypatch):
    # "$" in a regex matches before a trailing newline, so this name used to pass
    # validation. The variable without the newline is set, so only validation can
    # make this raise the invalid-name error.
    monkeypatch.setenv("MERIDIAN_TEST_NEWLINE_VAR", _SECRET)
    with pytest.raises(CredentialError, match="invalid environment variable name") as exc_info:
        resolve_credential("env:MERIDIAN_TEST_NEWLINE_VAR\n", hosted=False)
    assert _SECRET not in str(exc_info.value)


# ---------------------------------------------------------------------------
# keyring:SERVICE/ACCOUNT
# ---------------------------------------------------------------------------


def test_keyring_ref_reads_service_and_account_from_fake_module(monkeypatch):
    calls: list[tuple[str, str]] = []

    def get_password(service, account):
        calls.append((service, account))
        return _SECRET

    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(get_password=get_password))
    cred = resolve_credential("keyring:meridian/runpod", hosted=False)
    assert cred == ResolvedCredential(kind="keyring", secret=_SECRET)
    assert calls == [("meridian", "runpod")]


def test_keyring_missing_entry_names_service_and_account(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(get_password=lambda s, a: None))
    with pytest.raises(CredentialError, match="no keyring entry") as exc_info:
        resolve_credential("keyring:meridian/runpod", hosted=False)
    assert "meridian" in str(exc_info.value) and "runpod" in str(exc_info.value)


def test_keyring_not_installed_is_reported(monkeypatch):
    # A None entry in sys.modules makes `import keyring` raise ImportError.
    monkeypatch.setitem(sys.modules, "keyring", None)
    with pytest.raises(CredentialError, match="not installed"):
        resolve_credential("keyring:meridian/runpod", hosted=False)


def test_keyring_ref_is_refused_when_hosted_before_the_keyring_is_read(monkeypatch):
    # A hosted server must not read its own keyring under a caller-chosen service and account.
    calls: list[tuple[str, str]] = []

    def get_password(service, account):
        calls.append((service, account))
        return _SECRET

    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(get_password=get_password))
    with pytest.raises(CredentialError, match="local-only") as exc_info:
        resolve_credential("keyring:meridian/runpod", hosted=True)
    assert calls == []
    message = str(exc_info.value)
    assert "keyring" in message
    assert "meridian" not in message and "runpod" not in message
    assert _SECRET not in message


def test_keyring_backend_error_does_not_echo_its_message(monkeypatch):
    def boom(service, account):
        raise RuntimeError(f"backend leaked {_SECRET}")

    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(get_password=boom))
    with pytest.raises(CredentialError) as exc_info:
        resolve_credential("keyring:meridian/runpod", hosted=False)
    assert _SECRET not in str(exc_info.value)


@pytest.mark.parametrize("ref", ["keyring:noaccount", "keyring:/account", "keyring:service/"])
def test_keyring_ref_requires_both_service_and_account(ref, monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(get_password=lambda s, a: _SECRET))
    with pytest.raises(CredentialError, match="SERVICE/ACCOUNT"):
        resolve_credential(ref, hosted=False)


# ---------------------------------------------------------------------------
# runpodctl:default (local only)
# ---------------------------------------------------------------------------


def test_runpodctl_ref_reads_api_key_from_home_config(fake_home):
    _write_config(fake_home, f'{credentials.RUNPODCTL_KEY_NAME} = "{_SECRET}"\n')
    cred = resolve_credential("runpodctl:default", hosted=False)
    assert cred.kind == "runpodctl"
    assert cred.secret == _SECRET


def test_runpodctl_ref_accepts_the_camel_case_fallback_key(fake_home):
    _write_config(fake_home, f'{credentials.RUNPODCTL_KEY_NAME_FALLBACK} = "{_SECRET}"\n')
    assert resolve_credential("runpodctl:default", hosted=False).secret == _SECRET


def test_runpodctl_lowercase_key_is_the_name_the_cli_writes():
    # The CLI writes lowercase apikey; the constant must match it exactly.
    assert credentials.RUNPODCTL_KEY_NAME == "apikey"
    assert credentials.RUNPODCTL_KEY_NAME_FALLBACK == "apiKey"


def test_runpodctl_config_with_utf8_bom_is_read(fake_home):
    # PowerShell 5.1 writes a UTF-8 byte order mark, which tomllib rejects unless it is stripped.
    config_dir = fake_home / ".runpod"
    config_dir.mkdir(parents=True, exist_ok=True)
    body = f'{credentials.RUNPODCTL_KEY_NAME} = "{_SECRET}"\n'.encode("utf-8")
    (config_dir / "config.toml").write_bytes(b"\xef\xbb\xbf" + body)
    cred = resolve_credential("runpodctl:default", hosted=False)
    assert cred.secret == _SECRET


def test_runpodctl_missing_config_names_path_only(fake_home):
    with pytest.raises(CredentialError) as exc_info:
        resolve_credential("runpodctl:default", hosted=False)
    message = str(exc_info.value)
    assert "not found" in message
    assert str(fake_home) in message


def test_runpodctl_missing_key_names_the_key_not_any_value(fake_home):
    _write_config(fake_home, f'otherKey = "{_SECRET}"\n')
    with pytest.raises(CredentialError) as exc_info:
        resolve_credential("runpodctl:default", hosted=False)
    message = str(exc_info.value)
    assert credentials.RUNPODCTL_KEY_NAME in message
    assert _SECRET not in message


def test_runpodctl_non_string_key_is_refused_without_echoing_it(fake_home):
    _write_config(fake_home, f"{credentials.RUNPODCTL_KEY_NAME} = 12345\n")
    with pytest.raises(CredentialError) as exc_info:
        resolve_credential("runpodctl:default", hosted=False)
    assert "12345" not in str(exc_info.value)


def test_runpodctl_malformed_config_does_not_echo_file_content(fake_home):
    # Unterminated string: the secret-bearing text must not reach the error.
    _write_config(fake_home, f'{credentials.RUNPODCTL_KEY_NAME} = "{_SECRET}\n')
    with pytest.raises(CredentialError) as exc_info:
        resolve_credential("runpodctl:default", hosted=False)
    message = str(exc_info.value)
    assert "malformed" in message
    assert _SECRET not in message
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    _assert_secret_absent_from_chain(exc_info.value)


def test_runpodctl_non_default_profile_is_refused(fake_home):
    _write_config(fake_home, f'{credentials.RUNPODCTL_KEY_NAME} = "{_SECRET}"\n')
    with pytest.raises(CredentialError, match="default"):
        resolve_credential("runpodctl:work", hosted=False)


def test_runpodctl_is_refused_when_hosted_before_any_file_access(monkeypatch):
    def must_not_be_called():
        raise AssertionError("hosted resolution must not touch the runpodctl config")

    monkeypatch.setattr(credentials, "runpodctl_config_path", must_not_be_called)
    with pytest.raises(CredentialError, match="local-only"):
        resolve_credential("runpodctl:default", hosted=True)


# ---------------------------------------------------------------------------
# Unsupported references, describe_source, and secret-safe objects.
# ---------------------------------------------------------------------------


def test_unsupported_ref_does_not_echo_a_pasted_key():
    with pytest.raises(CredentialError) as exc_info:
        resolve_credential(_SECRET, hosted=False)
    assert _SECRET not in str(exc_info.value)


def test_unknown_scheme_does_not_echo_the_reference():
    with pytest.raises(CredentialError) as exc_info:
        resolve_credential(f"raw:{_SECRET}", hosted=False)
    assert _SECRET not in str(exc_info.value)


@pytest.mark.parametrize(
    "ref, kind",
    [
        ("env:SOME_VARIABLE_NAME", "env"),
        ("keyring:service/account", "keyring"),
        ("runpodctl:default", "runpodctl"),
    ],
)
def test_describe_source_returns_only_the_kind(ref, kind):
    assert describe_source(ref) == kind


def test_describe_source_reads_nothing_and_names_nothing(monkeypatch):
    def must_not_be_called():
        raise AssertionError("describe_source must not touch the runpodctl config")

    monkeypatch.setattr(credentials, "runpodctl_config_path", must_not_be_called)
    described = describe_source("env:SOME_SECRET_VARIABLE_NAME")
    assert described == "env"
    assert "SOME_SECRET_VARIABLE_NAME" not in described
    assert describe_source("runpodctl:default") == "runpodctl"


@pytest.mark.parametrize("ref", [_SECRET, f"raw:{_SECRET}"])
def test_describe_source_rejects_unsupported_refs_without_echoing_them(ref):
    with pytest.raises(CredentialError) as exc_info:
        describe_source(ref)
    assert _SECRET not in str(exc_info.value)


def test_resolved_credential_repr_and_str_hide_the_secret():
    cred = ResolvedCredential(kind="env", secret=_SECRET)
    assert _SECRET not in repr(cred)
    assert _SECRET not in str(cred)


def test_resolved_credential_refuses_an_empty_secret():
    with pytest.raises(CredentialError):
        ResolvedCredential(kind="env", secret="")


# ---------------------------------------------------------------------------
# Exception chains: the secret must not be reachable through __context__ or __cause__.
# ---------------------------------------------------------------------------


def test_keyring_backend_error_chain_never_carries_the_secret(monkeypatch):
    def boom(service, account):
        raise RuntimeError(f"backend leaked {_SECRET}")

    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(get_password=boom))
    with pytest.raises(CredentialError) as exc_info:
        resolve_credential("keyring:meridian/runpod", hosted=False)
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    _assert_secret_absent_from_chain(exc_info.value)


def test_keyring_not_installed_chain_is_clear(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)
    with pytest.raises(CredentialError) as exc_info:
        resolve_credential("keyring:meridian/runpod", hosted=False)
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None


def test_runpodctl_non_utf8_config_chain_never_carries_the_secret(fake_home):
    # A stray 0xFF byte makes decoding fail. UnicodeDecodeError.args then holds the whole file, key included.
    body = f'{credentials.RUNPODCTL_KEY_NAME} = "{_SECRET}"\n'.encode("utf-8")
    config_dir = fake_home / ".runpod"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_bytes(body + b"\xff")
    with pytest.raises(CredentialError) as exc_info:
        resolve_credential("runpodctl:default", hosted=False)
    assert "malformed" in str(exc_info.value)
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    _assert_secret_absent_from_chain(exc_info.value)


def test_runpodctl_unreadable_config_chain_is_clear(fake_home, monkeypatch):
    def unreadable(self):
        raise PermissionError(f"denied reading {_SECRET}")

    _write_config(fake_home, f'{credentials.RUNPODCTL_KEY_NAME} = "{_SECRET}"\n')
    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(CredentialError) as exc_info:
        resolve_credential("runpodctl:default", hosted=False)
    assert "could not be read" in str(exc_info.value)
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    _assert_secret_absent_from_chain(exc_info.value)
