"""3c3f5618 -- secret redaction gaps and a quadratic-time secret scan.

Regression coverage for the reproduced findings (red-team harness, 2026-10-06):

  RT-HC-004   scan / log-redaction time stays linear on repeated keywords
  RT-HC-001   provider keys whose body contains an underscore are fully masked
  RT-HC-002   export/set statements, JSON pairs and Authorization headers are caught
  RT-HC-003   a PEM block whose END line is cut off is masked to end-of-text
  RT-HC-006   fullwidth look-alikes and zero-width characters fold before matching
  RT-LEAD-002 run_verification output tails are redacted at the DB store boundary
  RT-SFS-006  the ScriptReceipt shared-state projection covers modern keys and GitHub tokens
  RT-SFS-007  log redaction masks credential-named pairs and credential header lines

Secret-shaped fixtures are assembled at runtime so no literal credential-shaped
string appears in this source file.
"""
from __future__ import annotations

import time

import pytest

from meridian.log_redaction import redact_log_text
from meridian.secret_redaction import check_for_secrets, redact, scan

# A provider-shaped key whose body contains underscores (RT-HC-001).
_OPENAI_SHAPED = "sk" + "-" + "proj-" + "abcdefghij_klmnopqrstuv_WXYZ0123"
_GITHUB_SHAPED = "gh" + "p_" + "A" * 36

# Generous bound: the linear implementation finishes in milliseconds; the old
# quadratic scan ran for many seconds on inputs of this size.
_LINEAR_BUDGET_SECONDS = 2.0


# ---------------------------------------------------------------------------
# RT-HC-004: linear-time scanning
# ---------------------------------------------------------------------------

class TestScanIsLinear:
    def test_repeated_credential_keywords_scan_fast(self):
        line = "SECRET" * 30000  # no '=' anywhere: nothing may match
        start = time.perf_counter()
        assert scan(line) == []
        assert time.perf_counter() - start < _LINEAR_BUDGET_SECONDS

    def test_repeated_keywords_with_assignment_still_detected(self):
        line = "API_KEY" * 20000 + "=" + "x" * 8
        start = time.perf_counter()
        matches = scan(line)
        assert time.perf_counter() - start < _LINEAR_BUDGET_SECONDS
        assert [m.name for m in matches] == ["dotenv-credential"]

    def test_repeated_jwt_prefix_scan_fast(self):
        blob = "eyJ" * 30000
        start = time.perf_counter()
        scan(blob)
        assert time.perf_counter() - start < _LINEAR_BUDGET_SECONDS

    def test_repeated_scheme_runs_log_redaction_fast(self):
        blob = "sk-" * 30000
        start = time.perf_counter()
        redact_log_text(blob)
        assert time.perf_counter() - start < _LINEAR_BUDGET_SECONDS


# ---------------------------------------------------------------------------
# RT-HC-001: underscores inside provider key bodies
# ---------------------------------------------------------------------------

class TestProviderKeyUnderscore:
    def test_underscore_in_key_body_fully_redacted(self):
        out = redact("token is " + _OPENAI_SHAPED + " end")
        assert "klmnopqrstuv" not in out
        assert "WXYZ0123" not in out
        assert "[REDACTED:openai-anthropic-key]" in out


# ---------------------------------------------------------------------------
# RT-HC-003: PEM block with the END line cut off
# ---------------------------------------------------------------------------

class TestPemTruncated:
    _BEGIN = "-----BEGIN RSA " + "PRIVATE KEY-----"
    _END = "-----END RSA " + "PRIVATE KEY-----"

    def test_block_without_end_line_is_masked_to_end_of_text(self):
        out = redact(self._BEGIN + "\nMIIEpAIBAAKCAQEAuncutbody\nmore-body-line")
        assert "MIIEpAIBAAKCAQEAuncutbody" not in out
        assert "more-body-line" not in out
        assert "[REDACTED:pem-private-key]" in out

    def test_complete_block_masks_only_its_own_span(self):
        out = redact("before\n" + self._BEGIN + "\nBODYX\n" + self._END + "\nafter")
        assert "BODYX" not in out
        assert "before" in out
        assert "after" in out


# ---------------------------------------------------------------------------
# RT-HC-002: credential carriers other than a bare NAME=value line
# ---------------------------------------------------------------------------

class TestCredentialCarriers:
    @pytest.mark.parametrize(
        "text, secret",
        [
            ("export DATABASE_CREDENTIAL=hunter2hunter2", "hunter2hunter2"),
            ("set API_TOKEN=abcdefgh1234", "abcdefgh1234"),
            ("setx SERVICE_PASSWORD=p4ssw0rd-long", "p4ssw0rd-long"),
            ('{"api_key": "abcdef123456"}', "abcdef123456"),
            ('{"client_secret":"zzzzzz987654"}', "zzzzzz987654"),
            ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
            ("Authorization: Bearer abcdefgh1234567", "abcdefgh1234567"),
        ],
    )
    def test_carrier_value_is_masked(self, text, secret):
        out = redact(text)
        assert secret not in out
        assert "[REDACTED:" in out

    def test_prose_mentioning_authorization_is_not_flagged(self):
        assert scan("Authorization: required for this endpoint") == []
        assert redact("set the timeout to 30 seconds") == "set the timeout to 30 seconds"


# ---------------------------------------------------------------------------
class TestSpecificShapeWinsOverCarrier:
    """A credential carrier is a fallback: a value that a specific shape claims
    keeps that shape's pattern name (overlap resolution in scan())."""

    def test_provider_key_inside_authorization_header_keeps_its_name(self):
        matches = scan("Authorization: Bearer " + _GITHUB_SHAPED + " end")
        assert [m.name for m in matches] == ["github-token"]

    def test_provider_key_inside_json_pair_keeps_its_name(self):
        text = '{"api_key": "' + _OPENAI_SHAPED + '"}'
        matches = scan(text)
        assert [m.name for m in matches] == ["openai-anthropic-key"]

    def test_opaque_authorization_value_reported_as_carrier(self):
        matches = scan("Authorization: Basic dXNlcjpwYXNzd29yZA==")
        assert [m.name for m in matches] == ["credential-carrier"]


# ---------------------------------------------------------------------------
# RT-HC-006: fold look-alikes and format characters before matching
# ---------------------------------------------------------------------------

class TestFoldingBeforeMatch:
    def test_fullwidth_look_alike_key_redacted(self):
        text = "\uff53\uff4b\uff0d" + "A" * 25  # fullwidth "sk-" + body
        out = redact(text)
        assert "A" * 25 not in out
        assert "[REDACTED:openai-anthropic-key]" in out

    def test_zero_width_inserted_key_redacted_with_original_offsets(self):
        text = "sk" + "\u200b" + "-" + "B" * 25 + " tail"
        matches = scan(text)
        assert len(matches) == 1
        m = matches[0]
        assert m.name == "openai-anthropic-key"
        assert m.start == 0
        assert text[m.start:m.end].endswith("B" * 25)
        assert "B" * 25 not in redact(text)
        assert redact(text).endswith(" tail")

    def test_check_for_secrets_rejects_folded_variant(self):
        with pytest.raises(ValueError):
            check_for_secrets("sk" + "\u200b" + "-" + "C" * 25, "note")

    def test_benign_fullwidth_text_is_not_rewritten(self):
        text = "\uff28\uff45\uff4c\uff4c\uff4f \u4e16\u754c, no secret here"
        assert redact(text) == text


# ---------------------------------------------------------------------------
# RT-SFS-007: log redaction of credential-named pairs and header lines
# ---------------------------------------------------------------------------

class TestLogCredentialPairs:
    @pytest.mark.parametrize(
        "text, secret",
        [
            ("login failed password: hunter2hunter2", "hunter2hunter2"),
            ("retrying api_key=abcdef1234", "abcdef1234"),
            ("hdr x-api-key: zz9zz9zz9", "zz9zz9zz9"),
            ("[mcp_auth] Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
            ("Cookie: session=abc123def456", "abc123def456"),
        ],
    )
    def test_credential_named_pair_masked(self, text, secret):
        out = redact_log_text(text)
        assert secret not in out

    def test_fullwidth_credential_name_masked(self):
        out = redact_log_text("\uff50\uff41\uff53\uff53\uff57\uff4f\uff52\uff44: hunter2hunter2")
        assert "hunter2hunter2" not in out

    def test_traceback_source_line_kept(self):
        line = "    token = request.headers.get('x-real')"
        assert redact_log_text(line) == line

    def test_redaction_is_idempotent(self):
        text = "password: hunter2hunter2 Authorization: Basic dXNlcjpwYXNz"
        once = redact_log_text(text)
        assert redact_log_text(once) == once


# ---------------------------------------------------------------------------
# RT-SFS-006: ScriptReceipt shared-state projection
# ---------------------------------------------------------------------------

def test_shared_state_projection_redacts_modern_keys_and_github_tokens():
    from meridian import local_runner as lr

    out = lr._redact_for_shared_state(f"OPENAI={_OPENAI_SHAPED} gh={_GITHUB_SHAPED}")
    assert _OPENAI_SHAPED not in out
    assert _GITHUB_SHAPED not in out


# ---------------------------------------------------------------------------
# RT-LEAD-002: run_verification output tails redacted at the store boundary
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_verification_run_redacts_output_tails(db):
    from meridian import db as db_module

    proj = await db_module.create_project(db, name="vr-redact-3c3f5618")
    run = await db_module.create_verification_run(
        db, proj["id"], "pixi run test", cwd="/repo", actor="sess-1",
    )
    completed = await db_module.complete_verification_run(
        db, run["id"], status="ok", exit_code=0, passed=1, failed=0,
        stdout_tail="env dump token " + _GITHUB_SHAPED,
        stderr_tail="FAILED test_x",
    )
    assert _GITHUB_SHAPED not in completed["stdout_tail"]
    assert "[REDACTED:github-token]" in completed["stdout_tail"]
    assert completed["stderr_tail"] == "FAILED test_x"

    persisted = await db_module.get_verification_run(db, run["id"])
    assert _GITHUB_SHAPED not in persisted["stdout_tail"]
