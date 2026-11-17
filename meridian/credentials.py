"""Credential reference resolver for research providers (RunPod first).

A configuration stores a REFERENCE, never a secret, and resolves it when the
credential is needed. Supported references:

* ``env:NAME``: the value of the environment variable ``NAME``. Unset or
  empty is an error that names ``NAME`` only. LOCAL ONLY: refused whenever
  ``hosted`` is true, before any environment variable is read.
* ``keyring:SERVICE/ACCOUNT``: a password from the ``keyring`` package
  (the first ``/`` separates service from account). LOCAL ONLY: refused
  whenever ``hosted`` is true, before the keyring is read.
* ``runpodctl:default``: the top-level ``apikey`` value (or ``apiKey`` as a
  fallback) in the RunPod CLI config file at ``~/.runpod/config.toml``.
  LOCAL ONLY: refused whenever ``hosted`` is true, and the file is not read then.

Errors name the reference kind and the identifier the user wrote (env var
name, keyring service and account, config path or missing key name). Errors
never contain a resolved secret value, and they never echo an unsupported
reference string (which could be a pasted key). :func:`describe_source`
returns only the kind, for responses.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from meridian.exception_chain import raise_unchained

KIND_ENV = "env"
KIND_KEYRING = "keyring"
KIND_RUNPODCTL = "runpodctl"
_KINDS = frozenset({KIND_ENV, KIND_KEYRING, KIND_RUNPODCTL})

#: Path of the RunPod CLI config, relative to the user's home directory.
RUNPODCTL_CONFIG_RELPATH = (".runpod", "config.toml")
#: The only runpodctl profile reference this resolver accepts.
RUNPODCTL_PROFILE = "default"
#: Top-level key holding the API key in the RunPod CLI config. The CLI writes
#: lowercase ``apikey``; this was confirmed against the developer's real config by
#: key name only (no value was read into logs or output). ``apiKey`` is accepted
#: as a fallback in case another CLI version uses camelCase.
RUNPODCTL_KEY_NAME = "apikey"
RUNPODCTL_KEY_NAME_FALLBACK = "apiKey"

_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_UNSUPPORTED_REF = (
    "unsupported credential reference; expected env:NAME, "
    "keyring:SERVICE/ACCOUNT or runpodctl:default"
)


class CredentialError(ValueError):
    """A credential reference could not be resolved. The message names the
    reference kind and the missing name or path, never a secret value."""


@dataclass(frozen=True)
class ResolvedCredential:
    """A resolved secret plus its source kind. The secret is excluded from
    ``repr`` so it cannot leak through logs or tracebacks of this object."""

    kind: str
    secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.secret:
            raise CredentialError(f"{self.kind} credential resolved to an empty value")


def _split_ref(ref: object) -> "tuple[str, str]":
    if not isinstance(ref, str) or ":" not in ref:
        raise CredentialError(_UNSUPPORTED_REF)
    kind, _, body = ref.partition(":")
    if kind not in _KINDS:
        raise CredentialError(_UNSUPPORTED_REF)
    return kind, body


def runpodctl_config_path() -> Path:
    """The RunPod CLI config path under the current user's home directory."""
    return Path.home().joinpath(*RUNPODCTL_CONFIG_RELPATH)


def _resolve_env(name: str) -> ResolvedCredential:
    if not _ENV_NAME_RE.fullmatch(name):
        raise CredentialError("env credential reference has an invalid environment variable name")
    value = os.environ.get(name)
    if not value:
        raise CredentialError(f"environment variable {name} is not set or is empty")
    return ResolvedCredential(kind=KIND_ENV, secret=value)


def _resolve_keyring(body: str) -> ResolvedCredential:
    service, sep, account = body.partition("/")
    if not sep or not service or not account:
        raise CredentialError("keyring credential reference must look like keyring:SERVICE/ACCOUNT")
    # Each failure is recorded inside its except block and raised after the block (see
    # meridian.exception_chain). A backend error may carry the secret in its args, so it
    # must not stay reachable through __context__.
    failure: "str | None" = None
    value: "str | None" = None
    try:
        import keyring
    except ImportError:
        failure = "the keyring package is not installed"
    else:
        try:
            value = keyring.get_password(service, account)
        except Exception:  # noqa: BLE001 — backend errors may echo values; report only the lookup key
            failure = f"keyring lookup failed for service {service!r}, account {account!r}"
    if failure is not None:
        raise_unchained(CredentialError(failure))
    if not value:
        raise CredentialError(f"no keyring entry for service {service!r}, account {account!r}")
    return ResolvedCredential(kind=KIND_KEYRING, secret=value)


def _parse_runpodctl_config(raw: bytes) -> "dict[str, Any] | None":
    try:
        # utf-8-sig strips a byte order mark, which PowerShell 5.1 writes and tomllib rejects.
        return tomllib.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        # Returns None rather than raising: UnicodeDecodeError.args holds the whole file,
        # and parser error text can quote it, so neither is kept or chained.
        return None


def _resolve_runpodctl(profile: str) -> ResolvedCredential:
    if profile != RUNPODCTL_PROFILE:
        raise CredentialError(f"only the {RUNPODCTL_PROFILE!r} runpodctl profile is supported")
    path = runpodctl_config_path()
    # Failures are recorded inside the except blocks and raised after them (see
    # meridian.exception_chain), so the original exception is not left in __context__.
    failure: "str | None" = None
    raw = b""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        failure = f"runpodctl config not found at {path}"
    except OSError:
        failure = f"runpodctl config at {path} could not be read"
    if failure is not None:
        raise_unchained(CredentialError(failure))
    data = _parse_runpodctl_config(raw)
    if data is None:
        raise_unchained(
            CredentialError(f"runpodctl config at {path} is malformed; expected key {RUNPODCTL_KEY_NAME!r}")
        )
    value = data.get(RUNPODCTL_KEY_NAME)
    if not isinstance(value, str) or not value:
        value = data.get(RUNPODCTL_KEY_NAME_FALLBACK)
    if not isinstance(value, str) or not value:
        raise CredentialError(
            f"runpodctl config at {path} has no {RUNPODCTL_KEY_NAME!r} or "
            f"{RUNPODCTL_KEY_NAME_FALLBACK!r} key"
        )
    return ResolvedCredential(kind=KIND_RUNPODCTL, secret=value)


def resolve_credential(ref: str, *, hosted: bool) -> ResolvedCredential:
    """Resolve a credential reference to its secret.

    ``hosted`` is required and keyword-only so every caller states the
    deployment mode. When it is true, ``env:``, ``keyring:`` and ``runpodctl:``
    references are refused before any environment variable, keyring entry or
    file is read. A hosted server must not send its own environment, keyring or
    config file as a credential.
    """
    kind, body = _split_ref(ref)
    if kind == KIND_ENV:
        if hosted:
            raise CredentialError("env credentials are local-only and are refused on hosted deployments")
        return _resolve_env(body)
    if kind == KIND_KEYRING:
        if hosted:
            raise CredentialError("keyring credentials are local-only and are refused on hosted deployments")
        return _resolve_keyring(body)
    if hosted:
        raise CredentialError("runpodctl credentials are local-only and are refused on hosted deployments")
    return _resolve_runpodctl(body)


def describe_source(ref: str) -> str:
    """Return only the reference kind (``env``, ``keyring`` or
    ``runpodctl``). It names no variable, service, account, path or value,
    and it reads nothing. Raises :class:`CredentialError` for an unsupported
    reference."""
    kind, _ = _split_ref(ref)
    return kind
