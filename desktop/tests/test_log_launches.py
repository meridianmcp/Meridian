"""Tests for meridian_desktop/log_launches.py: grouping a log's lines by local server launch, and the text the view shows.

Pure Python, with no Qt and no file. The lines are fixed strings in the two stamp forms the desktop writes, so nothing
reads the real logs or the real home directory.
"""

import datetime as dt

import pytest

from meridian_desktop.log_launches import (
    ALL_KEY,
    ALL_LABEL,
    BEFORE_KEY,
    BEFORE_LABEL,
    DEFAULT_GAP,
    GAP_RULE_TEXT,
    MARKER_RULE_TEXT,
    NO_MATCH_TEXT,
    NONE_RULE_TEXT,
    RULE_GAP,
    RULE_MARKER,
    RULE_NONE,
    LaunchGroup,
    group_by_launch,
    launch_view,
)

# Control-log lines: a tunnel stop before any local start, two local launches (the second one failed), and a tunnel start
# inside the first launch. Every stamp is UTC with a colon in the offset, as runner_controls writes it.
MARKED = [
    "2026-11-10T09:00:00+00:00 stop scope=tunnel outcome=confirmed pid=9",
    "2026-11-10T10:00:00+00:00 start scope=local outcome=confirmed pid=101",
    "2026-11-10T10:05:00+00:00 start scope=tunnel outcome=confirmed pid=55",
    "2026-11-10T11:00:00+00:00 stop scope=local outcome=confirmed pid=101",
    "2026-11-10T12:00:00+00:00 start scope=local outcome=failed pid=not reported",
]

# Timed lines with no marker. The gap from 08:10 to 09:45 is 95 minutes, so it splits. The gap from 09:45 to 10:15 is
# exactly 30 minutes, which is not more than the default gap, so it does not split. The untimed line joins the run it follows.
GAPPED = [
    "2026-11-10T08:00:00+00:00 stop scope=tunnel outcome=confirmed pid=9",
    "2026-11-10T08:10:00+00:00 stop scope=tunnel outcome=confirmed pid=9",
    "2026-11-10T09:45:00+00:00 stop scope=tunnel outcome=confirmed pid=9",
    "a line with no time in it",
    "2026-11-10T10:15:00+00:00 stop scope=tunnel outcome=confirmed pid=9",
]


def test_marker_rule_opens_a_group_at_each_local_start_and_keeps_the_lines_after_it():
    grouped = group_by_launch(MARKED)

    assert grouped.rule == RULE_MARKER
    assert grouped.rule_text == MARKER_RULE_TEXT
    assert [group.key for group in grouped.groups] == [BEFORE_KEY, "launch-1", "launch-2"]
    before, first, second = grouped.groups
    assert before == LaunchGroup(BEFORE_KEY, BEFORE_LABEL, (MARKED[0],))
    assert first.lines == (MARKED[1], MARKED[2], MARKED[3])
    assert first.label == "Launch 1 of 2: started 2026-11-10T10:00:00+00:00, outcome confirmed, pid 101"
    assert second.lines == (MARKED[4],)
    assert second.label == "Launch 2 of 2: started 2026-11-10T12:00:00+00:00, outcome failed, pid not reported"


def test_marker_rule_wins_over_the_timestamp_gap_even_when_the_gaps_are_long():
    # The gaps here are hours, so the gap rule would split at each one. The marker rule keeps the tunnel line in launch 1.
    grouped = group_by_launch(MARKED)

    assert grouped.rule == RULE_MARKER
    assert MARKED[2] in grouped.groups[1].lines


def test_no_before_group_when_the_first_line_is_a_local_start():
    lines = [MARKED[1], MARKED[3]]

    grouped = group_by_launch(lines)

    assert [group.key for group in grouped.groups] == ["launch-1"]
    assert grouped.groups[0].lines == tuple(lines)
    assert grouped.groups[0].label.startswith("Launch 1 of 1: started 2026-11-10T10:00:00+00:00")


def test_a_started_line_for_the_tunnel_scope_is_not_a_local_marker():
    grouped = group_by_launch([MARKED[0], MARKED[2]])

    assert grouped.rule == RULE_GAP


def test_gap_rule_splits_where_more_than_the_gap_passes_between_timed_entries():
    grouped = group_by_launch(GAPPED)

    assert grouped.rule == RULE_GAP
    assert grouped.rule_text == GAP_RULE_TEXT.format(minutes=30)
    assert "30 minutes" in grouped.rule_text
    assert [group.key for group in grouped.groups] == ["run-1", "run-2"]
    first, second = grouped.groups
    assert first.label == "Run 1 of 2: first entry 2026-11-10T08:00:00+00:00"
    assert first.lines == (GAPPED[0], GAPPED[1])
    assert second.label == "Run 2 of 2: first entry 2026-11-10T09:45:00+00:00"
    assert second.lines == (GAPPED[2], GAPPED[3], GAPPED[4])


def test_gap_rule_reads_the_desktop_log_stamp_form_too():
    lines = [
        "2026-11-10T08:00:00+0000 INFO meridian_desktop: Meridian Desktop started.",
        "2026-11-10T08:01:00+0000 INFO meridian_desktop: one more line",
        "2026-11-10T11:00:00+0000 INFO meridian_desktop: Meridian Desktop started.",
    ]

    grouped = group_by_launch(lines)

    assert grouped.rule == RULE_GAP
    assert [group.lines for group in grouped.groups] == [(lines[0], lines[1]), (lines[2],)]
    assert grouped.groups[1].label == "Run 2 of 2: first entry 2026-11-10T11:00:00+0000"


def test_untimed_lines_before_the_first_timed_entry_join_the_first_run():
    lines = [
        "no time at the top",
        "2026-11-10T08:00:00+00:00 stop scope=tunnel outcome=confirmed pid=9",
        "2026-11-10T08:05:00+00:00 stop scope=tunnel outcome=confirmed pid=9",
    ]

    grouped = group_by_launch(lines)

    assert [group.key for group in grouped.groups] == ["run-1"]
    assert grouped.groups[0].lines == tuple(lines)
    assert grouped.groups[0].label == "Run 1 of 1: first entry 2026-11-10T08:00:00+00:00"


def test_a_time_with_no_offset_is_taken_as_utc_so_it_compares_with_offset_times():
    lines = [
        "2026-11-10T08:00:00 one",
        "2026-11-10T08:10:00+00:00 two",
        "2026-11-10T09:00:00 three",
    ]

    grouped = group_by_launch(lines)

    assert grouped.rule == RULE_GAP
    assert [group.lines for group in grouped.groups] == [(lines[0], lines[1]), (lines[2],)]


def test_a_custom_gap_is_used_and_named_in_the_rule_text():
    lines = [
        "2026-11-10T08:00:00+00:00 one",
        "2026-11-10T08:06:00+00:00 two",
    ]

    grouped = group_by_launch(lines, gap=dt.timedelta(minutes=5))

    assert grouped.rule == RULE_GAP
    assert "5 minutes" in grouped.rule_text
    assert len(grouped.groups) == 2
    assert len(group_by_launch(lines).groups) == 1
    assert DEFAULT_GAP == dt.timedelta(minutes=30)


@pytest.mark.parametrize("gap", [dt.timedelta(0), dt.timedelta(seconds=-5)])
def test_a_gap_that_is_not_positive_is_refused(gap):
    with pytest.raises(ValueError):
        group_by_launch(["2026-11-10T08:00:00+00:00 one"], gap=gap)


def test_lines_with_no_marker_and_no_time_form_one_group_with_no_heading():
    grouped = group_by_launch(["alpha", "beta"])

    assert grouped.rule == RULE_NONE
    assert grouped.rule_text == NONE_RULE_TEXT
    assert grouped.groups == (LaunchGroup(ALL_KEY, ALL_LABEL, ("alpha", "beta")),)


def test_no_lines_give_no_groups():
    grouped = group_by_launch([])

    assert grouped.rule == RULE_NONE
    assert grouped.groups == ()


def test_lines_may_come_from_any_iterable():
    grouped = group_by_launch(line for line in MARKED)

    assert grouped.rule == RULE_MARKER
    assert len(grouped.groups) == 3


def test_view_with_nothing_selected_shows_every_group_under_its_heading():
    grouped = group_by_launch(MARKED)

    text = launch_view(grouped, selected=None)

    assert text == "\n\n".join(
        [
            f"{BEFORE_LABEL}\n{MARKED[0]}",
            f"{grouped.groups[1].label}\n{MARKED[1]}\n{MARKED[2]}\n{MARKED[3]}",
            f"{grouped.groups[2].label}\n{MARKED[4]}",
        ]
    )


def test_view_with_one_launch_selected_shows_only_that_launch():
    grouped = group_by_launch(MARKED)

    assert launch_view(grouped, selected="launch-2") == f"{grouped.groups[2].label}\n{MARKED[4]}"


def test_search_is_a_case_insensitive_substring_within_the_selected_launch():
    grouped = group_by_launch(MARKED)

    text = launch_view(grouped, selected="launch-1", query="  TUNNEL ")

    assert text == f"{grouped.groups[1].label}\n{MARKED[2]}"


def test_search_with_no_selection_leaves_out_a_launch_with_no_matching_line():
    grouped = group_by_launch(MARKED)

    text = launch_view(grouped, selected=None, query="pid=not reported")

    assert text == f"{grouped.groups[2].label}\n{MARKED[4]}"


def test_a_blank_search_filters_nothing():
    grouped = group_by_launch(MARKED)

    assert launch_view(grouped, selected=None, query="   ") == launch_view(grouped, selected=None)


def test_a_search_that_matches_no_line_says_so_and_names_no_text():
    grouped = group_by_launch(MARKED)

    assert launch_view(grouped, selected=None, query="no-such-words-here") == NO_MATCH_TEXT
    assert "no-such-words-here" not in NO_MATCH_TEXT


def test_a_selection_that_is_no_longer_a_group_shows_nothing():
    grouped = group_by_launch(MARKED)

    assert launch_view(grouped, selected="launch-9") == ""


def test_an_ungrouped_log_shows_its_lines_with_no_heading_and_is_searched_the_same_way():
    grouped = group_by_launch(["alpha", "beta"])

    assert launch_view(grouped, selected=None) == "alpha\nbeta"
    assert launch_view(grouped, selected=None, query="BE") == "beta"
    assert launch_view(grouped, selected=None, query="gamma") == NO_MATCH_TEXT


def test_an_empty_view_of_no_lines_is_empty():
    assert launch_view(group_by_launch([]), selected=None) == ""
