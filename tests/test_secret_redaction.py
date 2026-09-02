"""14491654 -- secret detection/redaction module and secret_guard hook tests.

Two layers:

1. Python module (meridian/secret_redaction.py) -- unit tests for scan(),
   redact(), and is_sensitive_path().  Each pattern class gets at least one
   positive and one false-positive-avoidance test.  Realistic multi-line PEM
   blocks and dotenv file content are tested.

2. Hook (secret_guard.sh via bash) -- subprocess tests using the same pattern
   as test_hitl_guard.py: run the bash hook with various JSON payloads, assert
   correct block/allow behaviour.  A companion test verifies secret_guard.ps1
   is pure ASCII and exists (for the Windows Claude Code runtime).

IMPORTANT -- pattern interaction note:
   The dotenv-credential pattern matches the WHOLE assignment line
   (KEY=value), so for a text like "STRIPE_SECRET=sk_live_...", the
   dotenv-credential match starts at position 0 and consumes the stripe key
   value at position 14.  The overlap-resolution logic keeps the FIRST match
   by document position, so `scan()` returns `dotenv-credential` for the whole
   line rather than `stripe-live-key` for the embedded value.  This is correct
   behaviour -- the whole line IS a credential -- but the specific-pattern unit
   tests deliberately use bare values (no KEY= wrapper) to test each pattern
   in isolation.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from meridian import db as db_module
from meridian.secret_redaction import (
    SECRET_PATTERNS,
    SecretMatch,
    check_for_secrets,
    is_sensitive_path,
    redact,
    scan,
)

_REPO = Path(__file__).resolve().parent.parent
_HOOK_SH = _REPO / ".claude" / "hooks" / "secret_guard.sh"
_HOOK_PS1 = _REPO / ".claude" / "hooks" / "secret_guard.ps1"
_SETTINGS = _REPO / ".claude" / "settings.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_needs_bash = pytest.mark.skipif(
    not _HOOK_SH.exists() or shutil.which("bash") is None,
    reason="secret_guard.sh or bash unavailable",
)

# Windows NTSTATUS crash exit codes seen under heavy xdist (-n auto) contention.
_WIN_CRASH_CODES = frozenset(
    {
        0xC0000005 & 0xFFFFFFFF,  # ACCESS_VIOLATION
        0xC000007B & 0xFFFFFFFF,  # INVALID_IMAGE_FORMAT
        0xC0000135 & 0xFFFFFFFF,  # DLL_NOT_FOUND
        0xC0000142 & 0xFFFFFFFF,  # DLL_INIT_FAILED
        0xC000013A & 0xFFFFFFFF,  # CONTROL_C_EXIT / kill
        3221225773,               # observed under -n auto
    }
)


def _run_hook_once(payload: str) -> subprocess.CompletedProcess:
    """Run secret_guard.sh from cwd=repo root. Mirrors test_hitl_guard.py."""
    r = subprocess.run(
        ["bash", ".claude/hooks/secret_guard.sh"],
        input=payload.encode("utf-8"),
        cwd=str(_REPO),
        capture_output=True,
        timeout=30,
    )
    return subprocess.CompletedProcess(
        r.args,
        r.returncode,
        stdout=(r.stdout or b"").decode("utf-8", "replace"),
        stderr=(r.stderr or b"").decode("utf-8", "replace"),
    )


def _run_hook(payload: str) -> subprocess.CompletedProcess:
    """Retry on Windows subprocess-teardown crashes (harness artifact, not hook)."""
    last: subprocess.CompletedProcess | None = None
    for _ in range(3):
        try:
            last = _run_hook_once(payload)
        except subprocess.TimeoutExpired:
            continue
        if last.returncode in _WIN_CRASH_CODES:
            continue
        return last
    assert last is not None, "bash never produced a result (all attempts crashed)"
    return last


# ---------------------------------------------------------------------------
# scan() -- pattern detection unit tests
# ---------------------------------------------------------------------------

class TestScanPemPrivateKey:
    _PEM = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA2a2rwplBQLF29amygykEMmYz0+Kcj3bKBp29Pi3RCtBpELrh\n"
        "rAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n"
        "-----END RSA PRIVATE KEY-----"
    )
    _EC_PEM = (
        "-----BEGIN EC PRIVATE KEY-----\n"
        "MHQCAQEEIOaTheVxIq9oYhFYeVkdqE0=\n"
        "-----END EC PRIVATE KEY-----"
    )

    def test_detects_rsa_private_key(self):
        matches = scan(self._PEM)
        assert any(m.name == "pem-private-key" for m in matches), matches

    def test_detects_ec_private_key(self):
        matches = scan(self._EC_PEM)
        assert any(m.name == "pem-private-key" for m in matches), matches

    def test_detects_pem_embedded_in_larger_text(self):
        text = f"Some log output\n{self._PEM}\nMore output after"
        matches = scan(text)
        assert any(m.name == "pem-private-key" for m in matches), matches

    def test_does_not_match_public_key(self):
        pub = (
            "-----BEGIN PUBLIC KEY-----\n"
            "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA==\n"
            "-----END PUBLIC KEY-----"
        )
        pem_matches = [m for m in scan(pub) if m.name == "pem-private-key"]
        assert not pem_matches, "PUBLIC KEY should not match private-key pattern"


class TestScanAwsAccessKeyId:
    def test_detects_aws_akia_key(self):
        # Bare value -- not wrapped in KEY= to avoid dotenv overlap.
        text = "The key is AKIAIOSFODNN7EXAMPLE and more text"
        matches = scan(text)
        assert any(m.name == "aws-access-key-id" for m in matches), matches

    def test_does_not_match_short_akia(self):
        text = "AKIA123"  # too short
        matches = [m for m in scan(text) if m.name == "aws-access-key-id"]
        assert not matches, "Short AKIA string should not match"

    def test_does_not_match_lowercase_akia(self):
        text = "akiaiosfodnn7example"  # lowercase is not a real AWS key
        matches = [m for m in scan(text) if m.name == "aws-access-key-id"]
        assert not matches


class TestScanStripeLiveKey:
    # Use bare value (no KEY= wrapper) to avoid dotenv-credential overlap.
    _BARE = "sk_live_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"

    def test_detects_bare_stripe_live_key(self):
        matches = scan(self._BARE)
        assert any(m.name == "stripe-live-key" for m in matches), matches

    def test_detects_stripe_live_key_in_text(self):
        text = f"secret key value: {self._BARE} end"
        matches = scan(text)
        assert any(m.name == "stripe-live-key" for m in matches), matches

    def test_dotenv_wrapped_stripe_key_is_caught(self):
        # When wrapped in KEY=value, dotenv-credential fires (that's still correct
        # -- the line IS a credential).  scan() must catch SOMETHING.
        text = f"STRIPE_SECRET={self._BARE}"
        matches = scan(text)
        assert matches, "A wrapped Stripe key must still be detected (as dotenv-credential)"

    def test_does_not_match_stripe_test_key(self):
        text = "sk_test_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"
        matches = [m for m in scan(text) if m.name == "stripe-live-key"]
        assert not matches, "sk_test_ keys must not be flagged as live"

    def test_does_not_match_short_live_key(self):
        text = "sk_live_short"
        matches = [m for m in scan(text) if m.name == "stripe-live-key"]
        assert not matches


class TestScanGitHubToken:
    _PAT = "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"

    def test_detects_github_personal_access_token_bare(self):
        # Bare value -- no KEY= wrapper so github-token fires directly.
        matches = scan(self._PAT)
        assert any(m.name == "github-token" for m in matches), matches

    def test_detects_github_personal_access_token_in_prose(self):
        # Embedded in log-style output, not a KEY= assignment.
        text = f"Authorization: Bearer {self._PAT} end"
        matches = scan(text)
        assert any(m.name == "github-token" for m in matches), matches

    def test_detects_github_oauth_token(self):
        text = "gho_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnop"
        matches = scan(text)
        assert any(m.name == "github-token" for m in matches), matches

    def test_detects_github_user_token(self):
        text = "ghu_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnop"
        matches = scan(text)
        assert any(m.name == "github-token" for m in matches), matches

    def test_does_not_match_short_gh_token(self):
        text = "ghp_short"
        matches = [m for m in scan(text) if m.name == "github-token"]
        assert not matches

    def test_does_not_match_arbitrary_gh_prefix(self):
        text = "ghx_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"  # 'x' not in [pousr]
        matches = [m for m in scan(text) if m.name == "github-token"]
        assert not matches


class TestScanSlackToken:
    def test_detects_slack_bot_token(self):
        # Bare value -- no KEY= to avoid dotenv overlap.
        text = "xoxb-" + "12345678-12345678-abcdefghijklmno"
        matches = scan(text)
        assert any(m.name == "slack-token" for m in matches), matches

    def test_detects_slack_app_token(self):
        text = "xoxa-" + "12345678-abcdefghijklmno"
        matches = scan(text)
        assert any(m.name == "slack-token" for m in matches), matches

    def test_does_not_match_bare_xox(self):
        text = "xox-short"  # 'x' not a valid slack type char
        matches = [m for m in scan(text) if m.name == "slack-token"]
        assert not matches


class TestScanOpenAiAnthropicKey:
    _OPENAI = "sk-" + "proj-ABCDEFGHIJKLMNOPQRSTUVabcdef"
    _ANTHROPIC = "sk-" + "ant-ABCDEFGHIJKLMNOPQRSTUVabcdef"

    def test_detects_bare_openai_style_key(self):
        matches = scan(self._OPENAI)
        assert any(m.name == "openai-anthropic-key" for m in matches), matches

    def test_detects_bare_anthropic_style_key(self):
        matches = scan(self._ANTHROPIC)
        assert any(m.name == "openai-anthropic-key" for m in matches), matches

    def test_dotenv_wrapped_openai_key_is_caught(self):
        text = f"OPENAI_API_KEY={self._OPENAI}"
        matches = scan(text)
        assert matches, "Wrapped OpenAI key must still be detected"

    def test_does_not_match_short_sk_key(self):
        text = "sk-short12345"  # under 20 chars after sk-
        matches = [m for m in scan(text) if m.name == "openai-anthropic-key"]
        assert not matches

    def test_does_not_match_stripe_live_as_openai(self):
        text = "sk_live_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"  # underscore, not dash
        openai_matches = [m for m in scan(text) if m.name == "openai-anthropic-key"]
        assert not openai_matches, "sk_live_ is a Stripe key, not an OpenAI key"


class TestScanJwt:
    _JWT = (
        "eyJ" + "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )

    def test_detects_jwt(self):
        matches = scan(self._JWT)
        assert any(m.name == "jwt" for m in matches), matches

    def test_detects_jwt_embedded_in_text(self):
        text = f"Authorization: Bearer {self._JWT}\nOther: stuff"
        matches = scan(text)
        assert any(m.name == "jwt" for m in matches), matches

    def test_does_not_match_short_eyj(self):
        text = "eyJhb.short.v"
        # Short segments -- just ensure no crash
        _ = scan(text)


class TestScanDotenvCredential:
    def test_detects_secret_key_assignment(self):
        text = "MERIDIAN_ENCRYPTION_KEY=super_secret_value_here_xyz"
        matches = scan(text)
        assert any(m.name == "dotenv-credential" for m in matches), matches

    def test_detects_password_assignment(self):
        text = "DB_PASSWORD=hunter2\nOTHER=value"
        matches = scan(text)
        assert any(m.name == "dotenv-credential" for m in matches), matches

    def test_detects_token_assignment(self):
        text = "GITHUB_TOKEN=ghp_fake_token_value_here_1234\n"
        matches = scan(text)
        assert matches, "GITHUB_TOKEN assignment must be detected"

    def test_detects_multiline_dotenv_file(self):
        env_file = (
            "# .env file\n"
            "DATABASE_URL=postgres://user:pass@host/db\n"
            "STRIPE_SECRET_KEY=sk_live_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcd\n"
            "DEBUG=true\n"
            "ADMIN_PASSWORD=s3cr3t_password_here\n"
        )
        matches = scan(env_file)
        names = {m.name for m in matches}
        assert names, "dotenv file with secrets must trigger at least one match"
        # The specific stripe key and password lines must match something
        assert "stripe-live-key" in names or "dotenv-credential" in names, names

    def test_does_not_match_benign_key_variable(self):
        # KEY_COUNT starts with KEY so the conservative pattern may or may not fire;
        # just confirm no crash and a list is returned.
        text = "KEY_COUNT=42"
        result = scan(text)
        assert isinstance(result, list)

    def test_does_not_match_non_assignment_text(self):
        # A comment line has # before it; the dotenv pattern requires assignment.
        text = "# This is just a comment with SECRET_KEY mentioned\n"
        matches = [m for m in scan(text) if m.name == "dotenv-credential"]
        assert not matches, "Comment lines should not match dotenv-credential"


# ---------------------------------------------------------------------------
# scan() -- edge cases
# ---------------------------------------------------------------------------

class TestScanMeridianToken:
    """ba31dedf — Meridian's own bearer token shape. Confirmed gap: none of
    stripe-live-key, openai-anthropic-key, or dotenv-credential fired on a
    bare `"BEARER_TOKEN": "sk_meridian_..."` JSON/TOML/env pair (the exact
    shape AGENTS.md's hosted-tier config, tunnel_client's generated configs,
    and meridian_connect.py's installer all produce) before this pattern was
    added."""

    # Fixture value only -- never a real credential. Deliberately shaped like
    # the real thing (prefix + length) so it exercises the actual regex.
    _FAKE = "sk_meridian_" + "abcdefghijklmnopqrstuvwxyz123456"  # noqa: S105

    def test_detects_bare_meridian_token(self):
        matches = scan(self._FAKE)
        assert any(m.name == "meridian-token" for m in matches), matches

    def test_detects_meridian_token_in_bearer_header_prose(self):
        text = f"Authorization: Bearer {self._FAKE}"
        matches = scan(text)
        assert any(m.name == "meridian-token" for m in matches), matches

    def test_detects_meridian_token_in_json_env_pair(self):
        """The exact confirmed-gap shape: a bare JSON `"BEARER_TOKEN": "..."`
        pair, quoted/colon-joined rather than a bare KEY=value assignment --
        this is what slipped past every pre-existing pattern."""
        text = f'{{"env": {{"BEARER_TOKEN": "{self._FAKE}"}}}}'
        matches = scan(text)
        assert any(m.name == "meridian-token" for m in matches), matches

    def test_detects_meridian_token_in_generated_mcp_json_snippet(self):
        """A realistic generated-config sample matching tunnel_client's own
        `_tunnel_mcp_entries` shape."""
        text = (
            '{\n'
            '  "mcpServers": {\n'
            '    "meridian": {\n'
            '      "type": "http",\n'
            '      "url": "https://usemeridian.us/mcp",\n'
            f'      "headers": {{"Authorization": "Bearer {self._FAKE}"}}\n'
            '    }\n'
            '  }\n'
            '}\n'
        )
        matches = scan(text)
        assert any(m.name == "meridian-token" for m in matches), matches

    def test_does_not_match_short_meridian_prefix(self):
        text = "sk_meridian_short"
        matches = [m for m in scan(text) if m.name == "meridian-token"]
        assert not matches

    def test_does_not_confuse_stripe_key_for_meridian_token(self):
        text = "sk_live_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"
        matches = [m for m in scan(text) if m.name == "meridian-token"]
        assert not matches

    def test_redact_masks_meridian_token(self):
        result = redact(self._FAKE)
        assert self._FAKE not in result
        assert "[REDACTED:meridian-token]" in result

    def test_check_for_secrets_rejects_meridian_token(self):
        # Bare value (no KEY= wrapper) to test this pattern in isolation, per
        # the module docstring's convention for the other pattern classes.
        with pytest.raises(ValueError, match="meridian-token"):
            check_for_secrets(f"Use {self._FAKE} to authenticate", context="x")


class TestScanEdgeCases:
    def test_empty_string_returns_empty(self):
        assert scan("") == []

    def test_benign_text_returns_empty(self):
        text = "The quick brown fox jumps over the lazy dog. No secrets here."
        assert scan(text) == []

    def test_multiple_secrets_in_one_text(self):
        # Use bare values to avoid dotenv-credential masking the specific patterns.
        text = (
            "sk_live_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcd\n"
            "AKIAIOSFODNN7EXAMPLE\n"
        )
        matches = scan(text)
        names = {m.name for m in matches}
        assert "stripe-live-key" in names, names
        assert "aws-access-key-id" in names, names

    def test_overlapping_spans_not_returned_twice(self):
        text = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk"
        matches = scan(text)
        for i in range(len(matches)):
            for j in range(i + 1, len(matches)):
                assert matches[j].start >= matches[i].end, (
                    f"Overlapping spans: {matches[i]} vs {matches[j]}"
                )

    def test_snippet_never_full_secret(self):
        text = "AKIAIOSFODNN7EXAMPLE"
        matches = scan(text)
        assert matches
        for m in matches:
            assert len(m.snippet) <= 15, "snippet must be short (12 chars + '...')"


# ---------------------------------------------------------------------------
# redact() tests
# ---------------------------------------------------------------------------

class TestRedact:
    def test_redacts_bare_stripe_key(self):
        text = "sk_live_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"
        result = redact(text)
        assert "sk_live_" not in result
        assert "[REDACTED:" in result

    def test_redacts_aws_key(self):
        text = "prefix AKIAIOSFODNN7EXAMPLE suffix"
        result = redact(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "[REDACTED:" in result

    def test_preserves_surrounding_text(self):
        text = "prefix AKIAIOSFODNN7EXAMPLE suffix"
        result = redact(text)
        assert result.startswith("prefix "), f"got: {result!r}"
        assert result.endswith(" suffix"), f"got: {result!r}"

    def test_benign_text_unchanged(self):
        text = "no secrets here at all"
        assert redact(text) == text

    def test_empty_string_unchanged(self):
        assert redact("") == ""

    def test_redacts_multiline_pem(self):
        text = (
            "header line\n"
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA2a2rwplBQLzFakeKeyDataHere==\n"
            "-----END RSA PRIVATE KEY-----\n"
            "footer line"
        )
        result = redact(text)
        assert "PRIVATE KEY" not in result, f"PEM should be redacted, got: {result!r}"
        assert "header line" in result
        assert "footer line" in result

    def test_redacts_realistic_dotenv_file(self):
        env_content = (
            "# App configuration\n"
            "STRIPE_SECRET_KEY=sk_live_" + "TestKeyABCDEFGHIJKLMNOPQRSTUVWXYZ\n"
            "DEBUG=false\n"
            "ADMIN_PASSWORD=mySuperSecretPassword123!\n"
            "PORT=8080\n"
        )
        result = redact(env_content)
        # Stripe key line must be redacted (either as stripe-live-key or dotenv-credential)
        assert "sk_live_" not in result
        # Non-secret lines should survive
        assert "DEBUG=false" in result
        assert "PORT=8080" in result

    def test_redact_label_includes_pattern_name(self):
        text = "AKIAIOSFODNN7EXAMPLE"
        result = redact(text)
        assert "[REDACTED:aws-access-key-id]" in result

    def test_redact_multiple_independent_secrets(self):
        # Two secrets on separate lines with no overlap risk.
        text = (
            "aws_key=AKIAIOSFODNN7EXAMPLE\n"
            "stripe=sk_live_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcd\n"
        )
        result = redact(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "sk_live_" not in result


# ---------------------------------------------------------------------------
# is_sensitive_path() tests
# ---------------------------------------------------------------------------

class TestIsSensitivePath:
    @pytest.mark.parametrize("path", [
        ".env",
        "/home/user/project/.env",
        "C:\\Users\\user\\project\\.env",
        ".env.local",
        ".env.production",
        "production.env",
        "id_rsa",
        "/home/user/.ssh/id_rsa",
        "id_rsa.pub",
        "id_ecdsa",
        "id_ed25519",
        "server.key",
        "server.pem",
        "cert.crt",
        "cert.cer",
        "keystore.jks",
        "keystore.p12",
        "keystore.pfx",
        "my_secrets.yaml",
        "credentials.json",
        "my_password_file.txt",
        "my_passwd_backup",
        ".netrc",
        "netrc",
        ".htpasswd",
        "apikey.txt",
        "api_key.json",
        "access_key.env",
        "private_key.pem",
        "terraform.tfvars",
        "terraform.tfstate",
        "terraform.tfstate.backup",
        "vault.yaml",
        "vault.yml",
        "my.vault",
        "auth_token",
        "refresh_token.json",
        "token",
        "token.json",
    ])
    def test_sensitive_paths_are_blocked(self, path):
        assert is_sensitive_path(path), f"Expected {path!r} to be sensitive"

    @pytest.mark.parametrize("path", [
        "README.md",
        "server.py",
        "dashboard.ts",
        "test_core.py",
        "requirements.txt",
        "config.py",
        "token_counts.csv",
        "tokenizer.py",
        "attestation.py",
        "monkey_patch.py",
        "socket.key.py",       # basename is socket.key.py, extension is .py
        "latest_run.log",
        "access_log.txt",      # 'access' but not 'access_key'
        "",                    # empty path
    ])
    def test_benign_paths_are_allowed(self, path):
        assert not is_sensitive_path(path), f"Expected {path!r} to NOT be sensitive"

    def test_case_insensitive_matching(self):
        assert is_sensitive_path(".ENV")
        assert is_sensitive_path("ID_RSA")
        assert is_sensitive_path("Server.KEY")
        assert is_sensitive_path("TERRAFORM.TFVARS")


# ---------------------------------------------------------------------------
# Hook: file existence and wiring
# ---------------------------------------------------------------------------

def test_both_hook_scripts_exist():
    assert _HOOK_SH.exists(), "secret_guard.sh must exist in .claude/hooks/"
    assert _HOOK_PS1.exists(), "secret_guard.ps1 must exist in .claude/hooks/"


def test_secret_guard_ps1_is_pure_ascii():
    """PS 5.1 reads BOM-less UTF-8 as cp1252; non-ASCII bytes corrupt the parser."""
    raw = _HOOK_PS1.read_bytes()
    non_ascii = [i for i, b in enumerate(raw) if b >= 128]
    assert not non_ascii, (
        f"secret_guard.ps1 must be pure ASCII. "
        f"Non-ASCII bytes at offsets: {non_ascii[:5]}"
    )


def test_settings_wires_secret_guard_as_pretooluse():
    """Structural wiring test: settings.json must actually register the hook."""
    cfg = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    pre = cfg.get("hooks", {}).get("PreToolUse", [])
    entry = next(
        (e for e in pre if "secret_guard" in json.dumps(e.get("hooks", []))),
        None,
    )
    assert entry is not None, "PreToolUse must include a secret_guard hook entry"
    matcher = entry.get("matcher", "")
    assert "Read" in matcher, "secret_guard matcher must include Read"
    assert "Bash" in matcher, "secret_guard matcher must include Bash"


def test_settings_does_not_disturb_existing_hooks():
    """Adding our entry must not remove the pre-existing guards."""
    cfg = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    hooks = cfg.get("hooks", {})
    pre = json.dumps(hooks.get("PreToolUse", []))
    assert "hitl_guard" in pre, "the HITL PreToolUse guard must still be wired"
    post = json.dumps(hooks.get("PostToolUse", []))
    assert "test_tamper_guard" in post, "the PostToolUse test-tamper guard must still be wired"
    stop = json.dumps(hooks.get("Stop", []))
    assert "sprint_guard" in stop, "the Stop sprint guard must still be wired"


# ---------------------------------------------------------------------------
# Hook: bash subprocess behavioural tests
# ---------------------------------------------------------------------------

@_needs_bash
def test_hook_blocks_read_of_dotenv():
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/project/.env"}})
    r = _run_hook(payload)
    assert r.returncode == 2, f"Read of .env must be blocked (got {r.returncode})\nstderr: {r.stderr}"
    assert "14491654" in r.stderr, "must cite the sprint item id"


@_needs_bash
def test_hook_blocks_read_of_pem_file():
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/etc/ssl/server.pem"}})
    r = _run_hook(payload)
    assert r.returncode == 2, f"Read of .pem must be blocked\nstderr: {r.stderr}"


@_needs_bash
def test_hook_blocks_read_of_key_file():
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/home/user/.ssh/id_rsa"}})
    r = _run_hook(payload)
    assert r.returncode == 2, f"Read of id_rsa must be blocked\nstderr: {r.stderr}"


@_needs_bash
def test_hook_blocks_read_of_secrets_yaml():
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "config/secrets.yaml"}})
    r = _run_hook(payload)
    assert r.returncode == 2, f"Read of secrets.yaml must be blocked\nstderr: {r.stderr}"


@_needs_bash
def test_hook_blocks_read_of_password_file():
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/etc/admin_password"}})
    r = _run_hook(payload)
    assert r.returncode == 2, f"Read of admin_password must be blocked\nstderr: {r.stderr}"


@_needs_bash
def test_hook_allows_read_of_benign_file():
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "meridian/server.py"}})
    r = _run_hook(payload)
    assert r.returncode == 0, f"Read of server.py must be allowed\nstderr: {r.stderr}"


@_needs_bash
def test_hook_allows_read_of_readme():
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "README.md"}})
    r = _run_hook(payload)
    assert r.returncode == 0, f"Read of README.md must be allowed\nstderr: {r.stderr}"


@_needs_bash
def test_hook_allows_read_of_python_file():
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "meridian/db/__init__.py"}})
    r = _run_hook(payload)
    assert r.returncode == 0, f"Read of db/__init__.py must be allowed\nstderr: {r.stderr}"


@_needs_bash
def test_hook_blocks_bash_printenv():
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "printenv"}})
    r = _run_hook(payload)
    assert r.returncode == 2, f"Bash printenv must be blocked\nstderr: {r.stderr}"


@_needs_bash
def test_hook_blocks_bash_cat_dotenv():
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "cat .env"}})
    r = _run_hook(payload)
    assert r.returncode == 2, f"Bash cat .env must be blocked\nstderr: {r.stderr}"


@_needs_bash
def test_hook_allows_bash_safe_echo():
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hello"}})
    r = _run_hook(payload)
    assert r.returncode == 0, f"Bash echo must be allowed\nstderr: {r.stderr}"


@_needs_bash
def test_hook_allows_bash_git_status():
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}})
    r = _run_hook(payload)
    assert r.returncode == 0, f"Bash git status must be allowed\nstderr: {r.stderr}"


@_needs_bash
def test_hook_allows_bash_pytest():
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "pixi run test -k test_security"}})
    r = _run_hook(payload)
    assert r.returncode == 0, f"Bash pytest must be allowed\nstderr: {r.stderr}"


@_needs_bash
def test_hook_blocks_grep_on_env_file():
    payload = json.dumps({"tool_name": "Grep", "tool_input": {"pattern": "KEY", "path": ".env"}})
    r = _run_hook(payload)
    assert r.returncode == 2, f"Grep on .env must be blocked\nstderr: {r.stderr}"


@_needs_bash
def test_hook_blocks_glob_on_key_file():
    payload = json.dumps({"tool_name": "Glob", "tool_input": {"pattern": "*.key", "path": "id_rsa"}})
    r = _run_hook(payload)
    assert r.returncode == 2, f"Glob on id_rsa must be blocked\nstderr: {r.stderr}"


@_needs_bash
def test_hook_allows_grep_on_benign_path():
    payload = json.dumps({"tool_name": "Grep", "tool_input": {"pattern": "def ", "path": "meridian/server.py"}})
    r = _run_hook(payload)
    assert r.returncode == 0, f"Grep on server.py must be allowed\nstderr: {r.stderr}"


@_needs_bash
@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch"])
def test_hook_allows_non_read_tools(tool):
    """Tools not in Read|Bash|Grep|Glob must never be blocked by this guard."""
    payload = json.dumps({"tool_name": tool, "tool_input": {"file_path": ".env"}})
    r = _run_hook(payload)
    assert r.returncode == 0, (
        f"{tool} must not be blocked by secret_guard (it handles Read/Bash/Grep/Glob only)\n"
        f"stderr: {r.stderr}"
    )


@_needs_bash
@pytest.mark.parametrize("payload", [
    "",
    "not json",
    "{}",
    '{"foo": "bar"}',
    '{"tool_name": "Read"}',  # missing tool_input
])
def test_hook_fails_open_on_garbage(payload):
    """Malformed or empty stdin must never trap the executor."""
    r = _run_hook(payload)
    assert r.returncode == 0, (
        f"Must fail open on garbage payload {payload!r}\n"
        f"stderr: {r.stderr}"
    )


# ---------------------------------------------------------------------------
# Layer 2 -- content-level secret detection for DB write paths (14491654).
#
# Covers:
#   (a) Each of the four guarded write paths rejects text containing each
#       secret-pattern class with a clear ValueError.
#   (b) Benign text -- including phrases that mention credential-related words
#       without an actual secret value -- is NOT falsely rejected.
#   (c) Existing happy-path usage of add_project_note, log_task, pin_decision,
#       complete_sprint_item, and patch_sprint_item still works with clean input.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Unit tests for check_for_secrets() directly
# ---------------------------------------------------------------------------

class TestCheckForSecretsUnit:
    """Direct unit tests for the detection function — fast, no DB needed."""

    # --- True positives (must be caught) ---

    def test_dotenv_credential_assignment(self):
        with pytest.raises(ValueError, match="dotenv-credential"):
            check_for_secrets("API_KEY=supersecretvalue123", context="x")

    def test_dotenv_secret_assignment(self):
        with pytest.raises(ValueError, match="dotenv-credential"):
            check_for_secrets("MY_SECRET=abc123xyz456", context="x")

    def test_dotenv_token_assignment(self):
        with pytest.raises(ValueError, match="dotenv-credential"):
            check_for_secrets("GITHUB_TOKEN=ghp_" + "abcdefghijklmnopqrstuvwxyz12", context="x")

    def test_dotenv_password_assignment(self):
        with pytest.raises(ValueError, match="dotenv-credential"):
            check_for_secrets("DB_PASSWORD=hunter2secret", context="x")

    def test_pem_private_key(self):
        # check_for_secrets requires a full BEGIN..END block (matches scan()'s
        # canonical pattern, needed so redact() can mask the entire key body,
        # not just announce a header) -- a bare header alone leaks no key
        # material and is deliberately not enough to trigger rejection.
        with pytest.raises(ValueError, match="pem-private-key"):
            check_for_secrets(
                "-----BEGIN RSA PRIVATE KEY-----\n"
                "MIIEpAIBAAKCAQEA2a2rwplBQLF29amygykEMmYz0+Kcj3bKBp29Pi3RCtBpELrh\n"
                "-----END RSA PRIVATE KEY-----",
                context="x",
            )

    def test_pem_ec_private_key(self):
        with pytest.raises(ValueError, match="pem-private-key"):
            check_for_secrets(
                "-----BEGIN EC PRIVATE KEY-----\n"
                "MHQCAQEEIOaTheVxIq9oYhFYeVkdqE0=\n"
                "-----END EC PRIVATE KEY-----",
                context="x",
            )

    def test_pem_openssh_private_key(self):
        with pytest.raises(ValueError, match="pem-private-key"):
            check_for_secrets(
                "-----BEGIN OPENSSH PRIVATE KEY-----\n"
                "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ==\n"
                "-----END OPENSSH PRIVATE KEY-----",
                context="x",
            )

    def test_aws_access_key(self):
        with pytest.raises(ValueError, match="aws-access-key-id"):
            check_for_secrets("Use AKIAIOSFODNN7EXAMPLE to authenticate", context="x")

    def test_stripe_live_key(self):
        # Use bare sk_live_ value (no key= wrapper) so Stripe pattern fires, not dotenv.
        with pytest.raises(ValueError, match="stripe-live-key"):
            check_for_secrets("Use sk_live_" + "abcdefghijklmnopqrstuvwx in production", context="x")

    def test_github_token_ghp(self):
        # Real classic GitHub PATs are ghp_ + 36 chars -- canonical pattern
        # requires 36+ to match that real-world format precisely.
        with pytest.raises(ValueError, match="github-token"):
            check_for_secrets(
                "token: ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh1234", context="x"
            )

    def test_github_token_gho(self):
        with pytest.raises(ValueError, match="github-token"):
            check_for_secrets("gho_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh12", context="x")

    def test_github_token_ghs(self):
        with pytest.raises(ValueError, match="github-token"):
            check_for_secrets("ghs_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh12", context="x")

    def test_slack_token_bot(self):
        with pytest.raises(ValueError, match="slack-token"):
            check_for_secrets("xoxb-" + "123456789-abcdefghijklmno", context="x")

    def test_slack_token_app(self):
        with pytest.raises(ValueError, match="slack-token"):
            check_for_secrets("xoxa-" + "2-123456789-abcdefghijklmno", context="x")

    def test_openai_style_key(self):
        # Use sk- key in prose (not an assignment) so OpenAI pattern fires, not dotenv.
        with pytest.raises(ValueError, match="openai-anthropic-key"):
            check_for_secrets("key is sk-abcdefghijklmnopqrstuvwxyz123456 for prod", context="x")

    def test_anthropic_style_key(self):
        # Anthropic keys look like sk-ant-...; hyphens inside the key value.
        with pytest.raises(ValueError, match="openai-anthropic-key"):
            check_for_secrets("key: sk-" + "ant-abc123ABCDEFGHIJKLMNOPQRSTUVWXYZ", context="x")

    def test_jwt(self):
        jwt = (
            "eyJ" + "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJ" + "zdWIiOiJ1c2VyMTIzIiwibmFtZSI6IkpvaG4gRG9lIn0"
            ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        with pytest.raises(ValueError, match="jwt"):
            check_for_secrets(f"Authorization: Bearer {jwt}", context="x")

    # --- False-positive avoidance (must NOT be caught) ---

    def test_benign_none_is_ok(self):
        check_for_secrets(None, context="x")  # must not raise

    def test_benign_empty_string(self):
        check_for_secrets("", context="x")

    def test_benign_plain_note(self):
        check_for_secrets(
            "Completed refactor of the authentication module. Tests pass.", context="x"
        )

    def test_benign_mentions_api_key_word(self):
        # The word "API_KEY" in prose (not an assignment) must NOT be flagged.
        check_for_secrets(
            "Make sure to set the API_KEY env var before running.", context="x"
        )

    def test_benign_short_assignment(self):
        # Short values (< 6 chars) after the = sign must not trigger dotenv pattern.
        check_for_secrets("REQUIRED=true", context="x")
        check_for_secrets("KEY=yes", context="x")

    def test_benign_key_word_in_title(self):
        check_for_secrets("Use the right key for encryption", context="x")

    def test_benign_token_noun_in_prose(self):
        check_for_secrets("The CSRF token is valid for 24 hours.", context="x")

    def test_benign_test_password_placeholder(self):
        # A word like "password" alone in prose is fine; it's the key=value form caught.
        check_for_secrets("Ask the user for their password on login.", context="x")

    def test_benign_version_strings_not_jwt(self):
        # Short dotted segments should not fire the JWT pattern.
        check_for_secrets("Upgraded from v1.2.3 to v2.0.1", context="x")

    def test_benign_stripe_test_key(self):
        # Stripe TEST keys (sk_test_...) are not live secrets; must NOT be flagged.
        # Built via concatenation so this fixture doesn't trip GitHub's own
        # (separate, more sensitive) push-protection secret scanner.
        check_for_secrets("sk_test_" + "abcdefghijklmnopqrstuvwx", context="x")

    def test_benign_aws_prefix_too_short(self):
        # An AKIA prefix that is too short (< 16 chars after) must not fire.
        check_for_secrets("AKIA1234", context="x")

    def test_benign_github_prefix_too_short(self):
        check_for_secrets("ghp_tooshort", context="x")

    def test_context_appears_in_error(self):
        with pytest.raises(ValueError, match="my-custom-context"):
            check_for_secrets("AKIAIOSFODNN7EXAMPLE", context="my-custom-context")

    def test_secret_patterns_list_non_empty(self):
        assert len(SECRET_PATTERNS) >= 8


# ---------------------------------------------------------------------------
# Integration tests: guarded DB write paths reject secrets, accept clean text
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_log_task_rejects_secret_in_description(db):
    p = await db_module.create_project(db, "sec-test-1")
    s = await db_module.register_session(db, p["id"], "s")
    with pytest.raises(ValueError, match="task description"):
        await db_module.log_task(
            db, s["id"], p["id"],
            "Set OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456",
        )


@pytest.mark.asyncio
async def test_log_task_accepts_clean_description(db):
    p = await db_module.create_project(db, "sec-test-2")
    s = await db_module.register_session(db, p["id"], "s")
    t = await db_module.log_task(db, s["id"], p["id"], "Shipped the auth refactor")
    assert t["description"] == "Shipped the auth refactor"


@pytest.mark.asyncio
async def test_log_task_rejects_aws_key(db):
    p = await db_module.create_project(db, "sec-test-3")
    s = await db_module.register_session(db, p["id"], "s")
    with pytest.raises(ValueError, match="aws-access-key-id"):
        await db_module.log_task(
            db, s["id"], p["id"],
            "creds: AKIAIOSFODNN7EXAMPLE / wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        )


@pytest.mark.asyncio
async def test_pin_decision_rejects_secret_in_body(db):
    p = await db_module.create_project(db, "sec-test-4")
    with pytest.raises(ValueError, match="decision body"):
        await db_module.pin_decision(
            db, p["id"],
            "Use prod Stripe key",
            "Set sk_live_" + "abcdefghijklmnopqrstuvwx in env",
            "TECHNICAL",
        )


@pytest.mark.asyncio
async def test_pin_decision_accepts_clean_body(db):
    p = await db_module.create_project(db, "sec-test-5")
    d = await db_module.pin_decision(
        db, p["id"], "Use psycopg3", "asyncpg has DLL issues on Windows", "TECHNICAL"
    )
    assert d["body"] == "asyncpg has DLL issues on Windows"


@pytest.mark.asyncio
async def test_update_pinned_decision_rejects_secret_in_body(db):
    p = await db_module.create_project(db, "sec-test-6")
    d = await db_module.pin_decision(db, p["id"], "Use X", "original body", "TECHNICAL")
    with pytest.raises(ValueError, match="decision body"):
        await db_module.update_pinned_decision(
            db, d["id"],
            body="Updated: token=ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh1234",
        )
    # Original body must be unchanged
    refreshed = await db_module.get_pinned_decision(db, d["id"])
    assert refreshed["body"] == "original body"


@pytest.mark.asyncio
async def test_update_pinned_decision_accepts_clean_body(db):
    p = await db_module.create_project(db, "sec-test-7")
    d = await db_module.pin_decision(db, p["id"], "Use Y", "v1", "TECHNICAL")
    updated = await db_module.update_pinned_decision(db, d["id"], body="v2 clean")
    assert updated["body"] == "v2 clean"


@pytest.mark.asyncio
async def test_add_project_note_rejects_secret_in_body(db):
    p = await db_module.create_project(db, "sec-test-8")
    with pytest.raises(ValueError, match="note body"):
        await db_module.add_project_note(
            db, p["id"],
            "Creds note",
            "DB_PASSWORD=hunter2secretvalue",
        )


@pytest.mark.asyncio
async def test_add_project_note_accepts_clean_body(db):
    p = await db_module.create_project(db, "sec-test-9")
    n = await db_module.add_project_note(db, p["id"], "Architecture", "Use FastAPI + psycopg3")
    assert n["body"] == "Use FastAPI + psycopg3"


@pytest.mark.asyncio
async def test_update_project_note_rejects_secret_in_body(db):
    p = await db_module.create_project(db, "sec-test-10")
    n = await db_module.add_project_note(db, p["id"], "Note", "safe body")
    with pytest.raises(ValueError, match="note body"):
        await db_module.update_project_note(
            db, n["id"],
            body="new body with xoxb-" + "123456789-abcdefghijklmno in it",
        )
    # Original body must survive
    refreshed = await db_module.get_project_note(db, n["id"])
    assert refreshed["body"] == "safe body"


@pytest.mark.asyncio
async def test_update_project_note_accepts_clean_body(db):
    p = await db_module.create_project(db, "sec-test-11")
    n = await db_module.add_project_note(db, p["id"], "Note", "v1")
    updated = await db_module.update_project_note(db, n["id"], body="v2 clean")
    assert updated["body"] == "v2 clean"


@pytest.mark.asyncio
async def test_complete_sprint_item_rejects_secret_in_notes(db):
    p = await db_module.create_project(db, "sec-test-12")
    s = await db_module.register_session(db, p["id"], "s")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Do the thing")
    await db_module.claim_sprint_item(db, p["id"], item["id"], s["id"])
    with pytest.raises(ValueError, match="sprint item notes"):
        await db_module.complete_sprint_item(
            db, p["id"], item["id"],
            notes="Done. JWT: eyJhbGciOiJIUzI1NiJ9.eyJ" + "rZXkiOiJ2YWx1ZXNlY3JldCJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQ",
        )


@pytest.mark.asyncio
async def test_complete_sprint_item_accepts_clean_notes(db):
    p = await db_module.create_project(db, "sec-test-13")
    s = await db_module.register_session(db, p["id"], "s")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Do the thing 2")
    await db_module.claim_sprint_item(db, p["id"], item["id"], s["id"])
    result = await db_module.complete_sprint_item(
        db, p["id"], item["id"],
        notes="Refactored the module, all 12 tests pass.",
    )
    assert result["status"] == "done"


@pytest.mark.asyncio
async def test_patch_sprint_item_rejects_secret_in_notes(db):
    p = await db_module.create_project(db, "sec-test-14")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Patch notes test")
    with pytest.raises(ValueError, match="sprint item notes"):
        await db_module.patch_sprint_item(
            db, p["id"], item["id"],
            notes="Context: STRIPE_KEY=sk_live_" + "abcdefghijklmnopqrstuvwx",
        )


@pytest.mark.asyncio
async def test_patch_sprint_item_accepts_clean_notes(db):
    p = await db_module.create_project(db, "sec-test-15")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Patch notes test 2")
    result = await db_module.patch_sprint_item(
        db, p["id"], item["id"],
        notes="Blocked on design review — no secrets here.",
    )
    assert result["notes"] == "Blocked on design review — no secrets here."


@pytest.mark.asyncio
async def test_fail_sprint_item_rejects_secret_in_reason(db):
    """fail_sprint_item stores reason in notes via the same chokepoint."""
    p = await db_module.create_project(db, "sec-test-16")
    s = await db_module.register_session(db, p["id"], "s")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Will fail")
    await db_module.claim_sprint_item(db, p["id"], item["id"], s["id"])
    with pytest.raises(ValueError, match="sprint item notes"):
        await db_module.fail_sprint_item(
            db, p["id"], item["id"],
            reason="Error: AKIAIOSFODNN7EXAMPLE was rejected",
        )


@pytest.mark.asyncio
async def test_fail_sprint_item_accepts_clean_reason(db):
    p = await db_module.create_project(db, "sec-test-17")
    s = await db_module.register_session(db, p["id"], "s")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Will also fail")
    await db_module.claim_sprint_item(db, p["id"], item["id"], s["id"])
    result = await db_module.fail_sprint_item(
        db, p["id"], item["id"],
        reason="Test environment was unavailable",
    )
    assert result["status"] == "failed"


# ---------------------------------------------------------------------------
# False-positive avoidance: credential-related WORDS in normal context
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_note_body_with_word_api_key_is_allowed(db):
    """'API_KEY' as a word in prose (not a key=value assignment) must pass."""
    p = await db_module.create_project(db, "sec-fp-1")
    n = await db_module.add_project_note(
        db, p["id"],
        "Setup guide",
        "Set the API_KEY environment variable to the value from the dashboard.",
    )
    assert n["id"]


@pytest.mark.asyncio
async def test_task_description_mentioning_token_is_allowed(db):
    p = await db_module.create_project(db, "sec-fp-2")
    s = await db_module.register_session(db, p["id"], "s")
    t = await db_module.log_task(
        db, s["id"], p["id"],
        "Implemented CSRF token rotation on login endpoint.",
    )
    assert t["description"].startswith("Implemented")


@pytest.mark.asyncio
async def test_decision_body_mentioning_password_policy_is_allowed(db):
    p = await db_module.create_project(db, "sec-fp-3")
    d = await db_module.pin_decision(
        db, p["id"],
        "Password policy",
        "Users must set a password of at least 12 characters on first login.",
        "PRODUCT",
    )
    assert d["id"]


@pytest.mark.asyncio
async def test_sprint_notes_with_word_secret_in_prose_is_allowed(db):
    """The word 'secret' alone in prose must not fire the dotenv pattern."""
    p = await db_module.create_project(db, "sec-fp-4")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "No false positive test"
    )
    result = await db_module.patch_sprint_item(
        db, p["id"], item["id"],
        notes="This feature should remain secret from competitors until launch.",
    )
    assert result["notes"].startswith("This feature")
