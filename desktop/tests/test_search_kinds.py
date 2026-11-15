"""The Search tab's three kinds as lists. Pure Python: no Qt, no socket.

Code and outputs have no list route on the local server, so they say "No data source yet" and send nothing. Research
runs come from the research runs route, are grouped by provider, and the transports here are fakes that record each call.
"""

import json

import pytest

from meridian_desktop import local_client, outputs_client
from meridian_desktop.search_kinds import (
    KIND_CODE,
    KIND_LABELS,
    KIND_OUTPUTS,
    KIND_RESEARCH,
    KINDS,
    NO_SOURCE_TEXT,
    NOT_CHECKED_TEXT,
    STATE_EMPTY,
    STATE_ERROR,
    STATE_NO_SOURCE,
    STATE_NOT_CHECKED,
    STATE_NOT_CONNECTED,
    STATE_RESULTS,
    KindList,
    group_runs,
    no_source_list,
    not_checked_list,
    read_kind,
    read_lists,
)

BASE = "http://127.0.0.1:7878"


class Reply:
    def __init__(self, status, body=b""):
        self.status = status
        self.body = body


def _json(status, payload):
    return Reply(status, json.dumps(payload).encode("utf-8"))


class Recorder:
    """A transport that answers each path prefix from a table, and records every path it was asked for."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.paths = []

    def __call__(self, base_url, path, timeout):
        self.paths.append(path)
        for prefix, answer in self.answers.items():
            if path.startswith(prefix):
                if isinstance(answer, BaseException):
                    raise answer
                return answer
        return Reply(404, b"")


class MustNotBeCalled:
    """A transport for a kind with no list route. Any call fails the test."""

    def __init__(self):
        self.paths = []

    def __call__(self, base_url, path, timeout):
        self.paths.append(path)
        raise AssertionError(f"a kind with no list route must not send a request: {path}")


RUNS = {
    "runs": [
        {"id": "run-alpha", "status": "completed", "provider": "openalex", "started_at": "2026-10-02"},
        {"id": "run-beta", "status": "running", "provider": "pubmed", "started_at": "2026-10-03"},
        {"id": "run-gamma", "status": "done", "provider": "openalex", "started_at": "2026-10-04"},
        {"id": "run-delta", "status": "queued"},
    ],
    "count": 4,
}


# ----------------------------------------------------------------- code and outputs: no list route, nothing sent


def test_code_has_no_list_and_sends_nothing():
    transport = MustNotBeCalled()
    outcome = read_kind(KIND_CODE, BASE, transport=transport)
    assert outcome.state == STATE_NO_SOURCE
    assert outcome.line == NO_SOURCE_TEXT[KIND_CODE]
    assert outcome.groups == ()
    assert transport.paths == []


def test_outputs_has_no_list_and_sends_nothing():
    transport = MustNotBeCalled()
    outcome = read_kind(KIND_OUTPUTS, BASE, transport=transport)
    assert outcome.state == STATE_NO_SOURCE
    assert outcome.line == NO_SOURCE_TEXT[KIND_OUTPUTS]
    assert outcome.groups == ()
    assert transport.paths == []


def test_the_no_source_lines_name_the_missing_grouping_and_say_nothing_was_sent():
    assert NO_SOURCE_TEXT[KIND_CODE].startswith("Code: no data source yet.")
    assert "by repo" in NO_SOURCE_TEXT[KIND_CODE]
    assert NO_SOURCE_TEXT[KIND_OUTPUTS].startswith("Outputs: no data source yet.")
    assert "by project" in NO_SOURCE_TEXT[KIND_OUTPUTS]
    assert NO_SOURCE_TEXT[KIND_CODE].endswith("Nothing was sent.")


def test_no_source_list_and_not_checked_list_describe_a_kind_before_any_read():
    assert no_source_list(KIND_CODE) == KindList(KIND_CODE, STATE_NO_SOURCE, NO_SOURCE_TEXT[KIND_CODE])
    assert not_checked_list(KIND_RESEARCH) == KindList(KIND_RESEARCH, STATE_NOT_CHECKED, NOT_CHECKED_TEXT)
    assert not_checked_list(KIND_OUTPUTS).state == STATE_NO_SOURCE


# ----------------------------------------------------------------- research: grouped by provider


def test_research_runs_are_grouped_by_provider_with_unreported_last():
    transport = Recorder({"/desktop/research/runs": _json(200, RUNS)})
    outcome = read_kind(KIND_RESEARCH, BASE, transport=transport)
    assert outcome.state == STATE_RESULTS
    assert [group.title for group in outcome.groups] == ["openalex", "pubmed", "provider not reported"]
    assert [len(group.rows) for group in outcome.groups] == [2, 1, 1]
    assert outcome.count == 4
    assert outcome.line == "Research: 4 run(s) in 3 group(s), grouped by provider."


def test_research_group_rows_keep_the_order_the_server_sent():
    transport = Recorder({"/desktop/research/runs": _json(200, RUNS)})
    outcome = read_kind(KIND_RESEARCH, BASE, transport=transport)
    openalex = outcome.groups[0]
    assert [row.item for row in openalex.rows] == ["run-alpha", "run-gamma"]
    assert openalex.rows[0].state == "completed"
    assert openalex.rows[0].started == "2026-10-02"
    assert openalex.rows[0].updated == ""  # the server sent no updated time, so the row keeps it empty


def test_a_run_with_no_provider_is_grouped_under_provider_not_reported():
    transport = Recorder({"/desktop/research/runs": _json(200, RUNS)})
    outcome = read_kind(KIND_RESEARCH, BASE, transport=transport)
    unreported = outcome.groups[-1]
    assert unreported.title == "provider not reported"
    assert [row.item for row in unreported.rows] == ["run-delta"]


def test_group_runs_of_nothing_is_nothing():
    assert group_runs([]) == ()


def test_group_runs_sorts_titles_without_regard_to_case():
    runs = (
        outputs_client.ResearchRun("a", "done", "Zebra", "", "", ""),
        outputs_client.ResearchRun("b", "done", "alpha", "", "", ""),
    )
    assert [group.title for group in group_runs(runs)] == ["alpha", "Zebra"]


def test_research_request_is_a_list_request_with_no_query_text():
    transport = Recorder({"/desktop/research/runs": _json(200, RUNS)})
    read_kind(KIND_RESEARCH, BASE, transport=transport)
    assert len(transport.paths) == 1
    path = transport.paths[0]
    assert path.startswith("/desktop/research/runs?")
    assert "q=" not in path
    assert "limit=50" in path


def test_research_with_no_runs_is_the_empty_state_not_an_invented_group():
    transport = Recorder({"/desktop/research/runs": _json(200, {"runs": []})})
    outcome = read_kind(KIND_RESEARCH, BASE, transport=transport)
    assert outcome.state == STATE_EMPTY
    assert outcome.groups == ()
    assert outcome.line == "Research: no research runs yet."


def test_research_route_missing_says_not_connected_and_shows_no_group():
    transport = Recorder({})  # every path answers 404
    outcome = read_kind(KIND_RESEARCH, BASE, transport=transport)
    assert outcome.state == STATE_NOT_CONNECTED
    assert outcome.line == (
        "Research: not connected. The local server has no research runs route yet. Update Meridian, then press Refresh."
    )
    assert outcome.groups == ()


def test_research_server_not_running_is_not_connected_and_names_the_address():
    transport = Recorder({"/desktop/research/runs": local_client.LocalClientError("not_running", "down")})
    outcome = read_kind(KIND_RESEARCH, BASE, transport=transport)
    assert outcome.state == STATE_NOT_CONNECTED
    assert "is not running at http://127.0.0.1:7878" in outcome.line


def test_research_unreadable_body_is_an_error_not_an_empty_list():
    transport = Recorder({"/desktop/research/runs": _json(200, {"runs": "not a list"})})
    outcome = read_kind(KIND_RESEARCH, BASE, transport=transport)
    assert outcome.state == STATE_ERROR
    assert outcome.groups == ()
    assert outcome.line.startswith("Research: the runs could not be read.")


def test_research_non_loopback_address_sends_nothing():
    transport = Recorder({"/desktop/research/runs": _json(200, RUNS)})
    outcome = read_kind(KIND_RESEARCH, "http://example.com", transport=transport)
    assert outcome.state == STATE_ERROR
    assert transport.paths == []


# ----------------------------------------------------------------- the three kinds together


def test_read_lists_reads_only_research_and_keeps_the_kind_order():
    code = MustNotBeCalled()
    outputs = MustNotBeCalled()
    research = Recorder({"/desktop/research/runs": _json(200, RUNS)})
    outcome = read_lists(BASE, KINDS, transports={KIND_CODE: code, KIND_OUTPUTS: outputs, KIND_RESEARCH: research})
    assert [item.kind for item in outcome.kinds] == [KIND_CODE, KIND_OUTPUTS, KIND_RESEARCH]
    assert [item.state for item in outcome.kinds] == [STATE_NO_SOURCE, STATE_NO_SOURCE, STATE_RESULTS]
    assert code.paths == [] and outputs.paths == []
    assert len(research.paths) == 1


def test_one_kinds_failure_does_not_hide_the_others():
    def broken(base_url, path, timeout):
        raise RuntimeError("socket exploded")

    outcome = read_lists(BASE, KINDS, transports={KIND_RESEARCH: broken})
    states = {item.kind: item.state for item in outcome.kinds}
    assert states == {KIND_CODE: STATE_NO_SOURCE, KIND_OUTPUTS: STATE_NO_SOURCE, KIND_RESEARCH: STATE_ERROR}


def test_read_lists_skips_an_unknown_kind():
    outcome = read_lists(BASE, ("code", "zotero"))
    assert [item.kind for item in outcome.kinds] == ["code"]


def test_read_kind_refuses_an_unknown_kind_as_a_caller_error():
    with pytest.raises(ValueError):
        read_kind("zotero", BASE)


def test_kind_labels_are_the_three_kinds():
    assert KIND_LABELS == {KIND_CODE: "Code", KIND_OUTPUTS: "Outputs", KIND_RESEARCH: "Research"}


def test_kindlist_count_is_the_number_of_rows_in_its_groups():
    outcome = read_kind(KIND_RESEARCH, BASE, transport=Recorder({"/desktop/research/runs": _json(200, RUNS)}))
    assert outcome.count == 4
    assert read_kind(KIND_CODE, BASE).count == 0
