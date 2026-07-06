"""fd726a05 — vendor-neutral file-claim guard decision core."""
from __future__ import annotations

from meridian.claim_guard import evaluate_claim_guard


def test_allows_when_no_claims():
    assert evaluate_claim_guard(None, "s1")["allow"] is True
    assert evaluate_claim_guard({}, "s1")["allow"] is True
    assert evaluate_claim_guard(
        {"file_lock": None, "symbol_claims": [], "read_claims": []}, "s1"
    )["allow"] is True


def test_blocks_write_when_other_holds_write_lock():
    claims = {"file_lock": {"session_id": "other"}, "symbol_claims": [], "read_claims": []}
    r = evaluate_claim_guard(claims, "s1", mode="write")
    assert r["allow"] is False
    assert r["reason"] == "write_locked"
    assert r["holder"] == "other"


def test_own_write_lock_does_not_block_self():
    claims = {"file_lock": {"session_id": "s1"}, "symbol_claims": [], "read_claims": []}
    assert evaluate_claim_guard(claims, "s1")["allow"] is True


def test_write_lock_by_other_blocks_even_a_read():
    claims = {"file_lock": {"session_id": "other"}, "symbol_claims": [], "read_claims": []}
    r = evaluate_claim_guard(claims, "s1", mode="read")
    assert r["allow"] is False
    assert r["reason"] == "write_locked"


def test_read_claim_blocks_write_but_not_another_read():
    claims = {"file_lock": None, "symbol_claims": [], "read_claims": [{"session_id": "other"}]}
    w = evaluate_claim_guard(claims, "s1", mode="write")
    assert w["allow"] is False and w["reason"] == "read_locked" and w["holder"] == "other"
    # a reader is blocked only by a writer, not by other readers
    assert evaluate_claim_guard(claims, "s1", mode="read")["allow"] is True


def test_other_symbol_claim_blocks_whole_file_write():
    claims = {
        "file_lock": None,
        "symbol_claims": [{"session_id": "other", "symbol": "AuthRouter"}],
        "read_claims": [],
    }
    r = evaluate_claim_guard(claims, "s1", mode="write")
    assert r["allow"] is False and r["reason"] == "symbol_locked" and r["holder"] == "other"


def test_own_symbol_claim_does_not_block_self():
    claims = {
        "file_lock": None,
        "symbol_claims": [{"session_id": "s1", "symbol": "AuthRouter"}],
        "read_claims": [],
    }
    assert evaluate_claim_guard(claims, "s1", mode="write")["allow"] is True


def test_fail_open_on_garbage():
    assert evaluate_claim_guard("not-a-dict", "s1")["allow"] is True
    assert evaluate_claim_guard(
        {"file_lock": "weird", "symbol_claims": "nope", "read_claims": 5}, "s1"
    )["allow"] is True
