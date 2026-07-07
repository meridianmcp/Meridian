"""badc7b34 — extend the fd726a05 vendor-neutral PreToolUse claim guard.

The guard (``meridian.claim_guard.evaluate_claim_guard``) is the pure, network-free
decision core each vendor's PreToolUse hook calls after fetching ``get_file_claims``.
It previously covered only WHOLE-FILE writes/reads. This extends it with a
SYMBOL-SCOPED write mode (``symbol="Foo"``) that mirrors the server-side
``claim_symbol`` semantics: only another session's claim on the *same* symbol
blocks; a different symbol coexists, and shared read claims don't block a narrow
symbol write. Pure unit tests — no server, no network, no sleeps.
"""
from __future__ import annotations

from meridian.claim_guard import evaluate_claim_guard


# ---------------------------------------------------------------------------
# Regression guard: existing whole-file behavior must not change.
# ---------------------------------------------------------------------------

def test_whole_file_write_still_blocked_by_write_lock():
    claims = {"file_lock": {"session_id": "other"}, "symbol_claims": [], "read_claims": []}
    r = evaluate_claim_guard(claims, "s1", mode="write")
    assert r["allow"] is False
    assert r["reason"] == "write_locked"
    assert r["holder"] == "other"
    # New key is always present and is None for a whole-file (symbol-less) claim.
    assert r["symbol"] is None


def test_whole_file_write_still_blocked_by_any_symbol_claim():
    claims = {
        "file_lock": None,
        "symbol_claims": [{"session_id": "other", "symbol_name": "AuthRouter"}],
        "read_claims": [],
    }
    r = evaluate_claim_guard(claims, "s1", mode="write")
    assert r["allow"] is False and r["reason"] == "symbol_locked" and r["holder"] == "other"


def test_whole_file_write_still_blocked_by_read_claim():
    claims = {"file_lock": None, "symbol_claims": [], "read_claims": [{"session_id": "other"}]}
    r = evaluate_claim_guard(claims, "s1", mode="write")
    assert r["allow"] is False and r["reason"] == "read_locked"


def test_read_mode_unaffected_and_symbol_arg_ignored():
    # A reader is blocked only by a writer; the symbol arg is meaningless for reads.
    claims = {"file_lock": None, "symbol_claims": [{"session_id": "other", "symbol_name": "X"}],
              "read_claims": [{"session_id": "other"}]}
    r = evaluate_claim_guard(claims, "s1", mode="read", symbol="X")
    assert r["allow"] is True
    # symbol is normalized away for read mode (a read claim is file-wide).
    assert r["symbol"] is None
    assert r["mode"] == "read"


def test_fail_open_on_garbage_with_symbol():
    r = evaluate_claim_guard("not-a-dict", "s1", symbol="Foo")
    assert r["allow"] is True and r["reason"] == "no-claim-data"
    assert r["symbol"] == "Foo"


# ---------------------------------------------------------------------------
# New: symbol-scoped write mode (badc7b34).
# ---------------------------------------------------------------------------

def test_symbol_write_blocked_by_same_symbol_held_by_other():
    claims = {
        "file_lock": None,
        "symbol_claims": [{"session_id": "other", "symbol_name": "AuthRouter"}],
        "read_claims": [],
    }
    r = evaluate_claim_guard(claims, "s1", mode="write", symbol="AuthRouter")
    assert r["allow"] is False
    assert r["reason"] == "symbol_locked"
    assert r["holder"] == "other"
    assert r["symbol"] == "AuthRouter"


def test_symbol_write_allowed_when_other_holds_different_symbol():
    # The core extension: two sessions safely own DIFFERENT symbols in one file.
    claims = {
        "file_lock": None,
        "symbol_claims": [{"session_id": "other", "symbol_name": "AuthRouter"}],
        "read_claims": [],
    }
    r = evaluate_claim_guard(claims, "s1", mode="write", symbol="TokenStore")
    assert r["allow"] is True
    assert r["reason"] == "clear"
    assert r["holder"] is None
    assert r["symbol"] == "TokenStore"


def test_symbol_write_not_blocked_by_read_claims():
    # claim_symbol ignores read claims — a narrow symbol write coexists with readers.
    claims = {
        "file_lock": None,
        "symbol_claims": [],
        "read_claims": [{"session_id": "other"}, {"session_id": "another"}],
    }
    r = evaluate_claim_guard(claims, "s1", mode="write", symbol="Foo")
    assert r["allow"] is True and r["reason"] == "clear"


def test_symbol_write_still_blocked_by_whole_file_write_lock():
    # File ⊃ symbol: the whole-file lock owner may touch any symbol, so a symbol
    # write by another session is blocked regardless of which symbol it targets.
    claims = {
        "file_lock": {"session_id": "other"},
        "symbol_claims": [],
        "read_claims": [],
    }
    r = evaluate_claim_guard(claims, "s1", mode="write", symbol="Anything")
    assert r["allow"] is False
    assert r["reason"] == "write_locked"
    assert r["holder"] == "other"


def test_own_same_symbol_claim_does_not_block_self():
    claims = {
        "file_lock": None,
        "symbol_claims": [{"session_id": "s1", "symbol_name": "AuthRouter"}],
        "read_claims": [],
    }
    r = evaluate_claim_guard(claims, "s1", mode="write", symbol="AuthRouter")
    assert r["allow"] is True and r["reason"] == "clear"


def test_symbol_name_matching_is_exact_and_whitespace_tolerant():
    # Leading/trailing whitespace on either side must not cause a false miss or hit.
    claims = {
        "file_lock": None,
        "symbol_claims": [{"session_id": "other", "symbol_name": "  parse_config "}],
        "read_claims": [],
    }
    blocked = evaluate_claim_guard(claims, "s1", mode="write", symbol=" parse_config")
    assert blocked["allow"] is False and blocked["reason"] == "symbol_locked"
    # A substring/prefix must NOT collide — only an exact (stripped) name match.
    ok = evaluate_claim_guard(claims, "s1", mode="write", symbol="parse")
    assert ok["allow"] is True and ok["reason"] == "clear"


def test_symbol_alias_key_accepted_for_cross_vendor_payloads():
    # get_file_claims uses symbol_name; accept a bare `symbol` alias too.
    claims = {
        "file_lock": None,
        "symbol_claims": [{"session_id": "other", "symbol": "AuthRouter"}],
        "read_claims": [],
    }
    r = evaluate_claim_guard(claims, "s1", mode="write", symbol="AuthRouter")
    assert r["allow"] is False and r["reason"] == "symbol_locked"


def test_multiple_symbol_claims_only_matching_one_blocks():
    claims = {
        "file_lock": None,
        "symbol_claims": [
            {"session_id": "a", "symbol_name": "Alpha"},
            {"session_id": "b", "symbol_name": "Beta"},
            {"session_id": "c", "symbol_name": "Gamma"},
        ],
        "read_claims": [],
    }
    r = evaluate_claim_guard(claims, "s1", mode="write", symbol="Beta")
    assert r["allow"] is False
    assert r["reason"] == "symbol_locked"
    assert r["holder"] == "b"


def test_empty_symbol_string_falls_back_to_whole_file_write():
    # An empty/whitespace symbol is not a symbol scope — behave as whole-file write,
    # so ANY other-session symbol claim blocks (the pre-extension semantics).
    claims = {
        "file_lock": None,
        "symbol_claims": [{"session_id": "other", "symbol_name": "AuthRouter"}],
        "read_claims": [],
    }
    r = evaluate_claim_guard(claims, "s1", mode="write", symbol="   ")
    assert r["allow"] is False and r["reason"] == "symbol_locked"
    assert r["symbol"] is None


def test_symbol_write_ignores_malformed_symbol_rows():
    # Rows without a session_id or symbol name must not block, and must not raise.
    claims = {
        "file_lock": None,
        "symbol_claims": [
            "garbage",
            {"symbol_name": "AuthRouter"},          # no session -> not a live holder
            {"session_id": "other"},                # no symbol name -> can't match
        ],
        "read_claims": [],
    }
    r = evaluate_claim_guard(claims, "s1", mode="write", symbol="AuthRouter")
    assert r["allow"] is True and r["reason"] == "clear"
