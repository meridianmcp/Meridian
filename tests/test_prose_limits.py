"""Output and work limits: the restatement scan stops at a limit, repeats are not compared
copy by copy, and each flag kind is capped with the truncation reported."""

import pytest

from meridian.prose import restatement_candidates, run_prose_check
from meridian.prose.check import MAX_FLAGS_PER_KIND


def _restating_block(count: int) -> str:
    # Every sentence shares five content types with every other sentence, and differs
    # only by its own number. So every pair is a restatement candidate.
    return " ".join(f"Worker {i} clears the shared cache daily." for i in range(count))


def test_limit_stops_the_scan_and_returns_the_first_flags_in_sentence_order():
    flags = restatement_candidates(_restating_block(6), limit=3)
    assert len(flags) == 3
    assert [(f.first.sentence, f.second.sentence) for f in flags] == [(1, 2), (1, 3), (1, 4)]


def test_no_limit_returns_every_pair():
    assert len(restatement_candidates(_restating_block(6))) == 15


def test_limit_must_be_positive():
    with pytest.raises(ValueError):
        restatement_candidates(_restating_block(3), limit=0)


def test_repeated_sentence_is_not_compared_copy_by_copy():
    # Two thousand copies of one sentence: one distinct sentence, so no restatement scan.
    text = " ".join(["Cache clears daily."] * 2000)
    assert restatement_candidates(text) == ()


def test_each_flag_kind_is_capped_and_the_truncation_is_reported():
    result = run_prose_check(_restating_block(40))
    assert "restatement_candidate" in result["truncated_kinds"]
    assert result["counts_by_kind"]["restatement_candidate"] == MAX_FLAGS_PER_KIND
    listed = [f for f in result["flags"] if f["kind"] == "restatement_candidate"]
    assert len(listed) == MAX_FLAGS_PER_KIND
    assert any("capped" in note for note in result["notes"])


def test_uncapped_result_reports_no_truncation():
    result = run_prose_check("Meridian uses Postgres.")
    assert result["truncated_kinds"] == []
    assert not any("capped" in note for note in result["notes"])
