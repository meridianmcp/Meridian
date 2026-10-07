"""meridian/versioning.py -- the next-version rule and the version-label guard.

The rows come from ``tests/fixtures/next_version_cases.json``, the same file
``meridian/static/dashboard-versions.test.ts`` runs against the TypeScript
implementation, so the two cannot drift apart silently.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.versioning import (
    MAX_VERSION_LENGTH,
    next_version,
    validate_version_label,
)

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "next_version_cases.json").read_text(
        encoding="utf-8"
    )
)
_NEXT_CASES = _FIXTURE["next_version"]
_LABEL_CASES = _FIXTURE["validate_version_label"]


@pytest.mark.parametrize(
    "case", _NEXT_CASES, ids=[repr(c["input"])[:40] for c in _NEXT_CASES]
)
def test_next_version_matches_shared_fixture(case):
    assert next_version(case["input"]) == case["expected"]


@pytest.mark.parametrize(
    "case", _LABEL_CASES, ids=[repr(c["input"])[:40] for c in _LABEL_CASES]
)
def test_validate_version_label_matches_shared_fixture(case):
    if case["label"] is None:
        with pytest.raises(ValueError):
            validate_version_label(case["input"])
    else:
        assert validate_version_label(case["input"]) == case["label"]


def test_the_owners_examples_are_present_in_the_fixture():
    # The request named these two explicitly; keep them from being edited out.
    pairs = {(c["input"], c["expected"]) for c in _NEXT_CASES}
    assert ("v2.1", "v2.2") in pairs
    assert ("v2.2", "v2.3") in pairs


def test_fixture_length_boundaries_are_what_they_claim():
    # The fixture spells the 63/64/65-character labels out literally; make
    # sure a hand edit did not shift them off the real limit.
    by_why = {c["why"]: c["input"] for c in _LABEL_CASES if "why" in c}
    assert len(by_why["exactly 63 characters"]) == MAX_VERSION_LENGTH - 1
    assert len(by_why["exactly 64 characters is the limit"]) == MAX_VERSION_LENGTH
    assert len(by_why["65 characters"]) == MAX_VERSION_LENGTH + 1
    long_next = next(
        c["input"] for c in _NEXT_CASES if c.get("why") == "longer than 64 characters"
    )
    assert len(long_next) > MAX_VERSION_LENGTH


@pytest.mark.parametrize("value", [None, 2, 2.1, ["v2.1"], b"v2.1", object()])
def test_next_version_non_string_is_none(value):
    assert next_version(value) is None


@pytest.mark.parametrize("value", [None, 2, ["v2.1"], b"v2.1"])
def test_validate_version_label_non_string_raises(value):
    with pytest.raises(ValueError):
        validate_version_label(value)


def test_next_version_respects_the_length_cap_on_both_sides():
    # 63 characters in, 64 out: still inside the cap, so it is returned.
    at_cap = ("1." * 27) + ("9" * 9)
    assert len(at_cap) == MAX_VERSION_LENGTH - 1
    out = next_version(at_cap)
    assert out == ("1." * 27) + "1000000000"
    assert len(out) == MAX_VERSION_LENGTH
    # 64 characters in, 65 out ("99" -> "100"): refuse rather than hand back a
    # label validate_version_label would then reject.
    grows_past_cap = ("1." * 31) + "99"
    assert len(grows_past_cap) == MAX_VERSION_LENGTH
    assert next_version(grows_past_cap) is None
    # Already over the cap on the way in.
    assert next_version(("1." * 28) + ("9" * 9)) is None


def test_next_version_result_always_revalidates():
    # Anything next_version returns must be acceptable to validate_version_label,
    # otherwise "Move to next" could offer a number the server then refuses.
    for case in _NEXT_CASES:
        out = next_version(case["input"])
        if out is not None:
            assert validate_version_label(out) == out
