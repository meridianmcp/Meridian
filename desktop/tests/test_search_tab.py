"""The Search tab: lists of code, outputs and research, grouped by source, with no search box.

Offscreen Qt, pytest-qt, fake transports only. No test opens a socket. The research read happens on first show and on
Refresh. Code and outputs are never read, so no transport is ever called for them.
"""

import json
import threading

from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit, QPushButton, QTableWidget

from meridian_desktop import local_client
from meridian_desktop.search_kinds import (
    KIND_RESEARCH,
    NO_SOURCE_TEXT,
    NOT_CHECKED_TEXT,
    STATE_RESULTS,
    Group,
    KindList,
    ListOutcome,
    Row,
)
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.search_tab import (
    FAILED_TEXT,
    INTRO_TEXT,
    READING_TEXT,
    RESULT_COLUMNS,
    SearchTab,
    TableLine,
    kinds_for,
    status_text,
    table_lines,
)
from meridian_desktop.ui.text import SELECTABLE_TEXT, selected_cells_text

URL = "http://127.0.0.1:7878"
CODE_LINE = NO_SOURCE_TEXT["code"]
OUTPUTS_LINE = NO_SOURCE_TEXT["outputs"]
NOT_RUNNING_LINE = (
    "Research: not connected. The local Meridian server is not running at http://127.0.0.1:7878. "
    "Start Meridian on this computer, then press Refresh."
)
ROUTE_MISSING_LINE = (
    "Research: not connected. The local server has no research runs route yet. Update Meridian, then press Refresh."
)
RUNS_LINE = "Research: 3 run(s) in 2 group(s), grouped by provider."
NOT_REPORTED = "not reported"
RUNS = {
    "runs": [
        {"id": "run-alpha", "status": "completed", "provider": "openalex", "started_at": "2026-10-02"},
        {"id": "run-beta", "status": "running", "provider": "pubmed", "started_at": "2026-10-03"},
        {"id": "run-gamma", "status": "done", "provider": "openalex", "started_at": "2026-10-04"},
    ],
    "count": 3,
}
RESEARCH_ROWS = [
    ("Research: openalex (2 run(s))", None, None, None, None),
    ("run-alpha", "completed", "2026-10-02", NOT_REPORTED, NOT_REPORTED),
    ("run-gamma", "done", "2026-10-04", NOT_REPORTED, NOT_REPORTED),
    ("Research: pubmed (1 run(s))", None, None, None, None),
    ("run-beta", "running", "2026-10-03", NOT_REPORTED, NOT_REPORTED),
]
ALL_KINDS_HEADERS = [
    ("Code: no data source yet", None, None, None, None),
    ("Outputs: no data source yet", None, None, None, None),
]


class Reply:
    def __init__(self, status, body=b""):
        self.status = status
        self.body = body


class Route:
    """A transport for the research route: records every (base URL, path) it is asked, and answers with ``reply``."""

    def __init__(self, reply=None):
        self.reply = reply if reply is not None else Reply(404, b"")
        self.calls = []

    def __call__(self, base_url, path, timeout):
        self.calls.append((base_url, path))
        return self.reply


class NeverCalled:
    """A transport for a kind with no list route. Any call is recorded, and the tests assert there are none."""

    def __init__(self):
        self.calls = []

    def __call__(self, base_url, path, timeout):
        self.calls.append((base_url, path))
        return Reply(404, b"")


def _json(status, payload):
    return Reply(status, json.dumps(payload).encode("utf-8"))


def _tab(qtbot, url=URL, transports=None, *, show=True):
    # A tab shown with no transport would read the real loopback address. Tests give a fake route instead.
    fakes = {KIND_RESEARCH: Route()} if transports is None else transports
    tab = SearchTab(lambda: url, transports=fakes)
    qtbot.addWidget(tab)
    if show:
        tab.show()
        qtbot.waitExposed(tab)
    return tab


def _status_lines(tab):
    return tab.findChild(QLabel, "search-status").text().split("\n")


def _table(tab):
    return tab.findChild(QTableWidget, "search-results")


def _table_rows(tab):
    """Each row as a tuple of cell texts. A header row's one cell spans the table, so it reads as (text, None, ...)."""
    table = _table(tab)
    rows = []
    for row in range(table.rowCount()):
        spanned = table.columnSpan(row, 0) == table.columnCount()
        cells = []
        for column in range(table.columnCount()):
            item = table.item(row, column)
            cells.append(item.text() if item is not None and (column == 0 or not spanned) else None)
        rows.append(tuple(cells))
    return rows


def _choose(tab, text):
    tab.findChild(QComboBox, "search-kind").setCurrentIndex(
        tab.findChild(QComboBox, "search-kind").findText(text)
    )


def _wait_done(qtbot, tab):
    button = tab.findChild(QPushButton, "search-refresh")
    qtbot.waitUntil(lambda: button.isEnabled(), timeout=5000)


def _wait_read(qtbot, tab):
    _wait_done(qtbot, tab)
    qtbot.waitUntil(lambda: READING_TEXT not in _status_lines(tab), timeout=5000)


def test_the_intro_is_the_owners_text_and_is_selectable(qtbot):
    tab = _tab(qtbot)
    label = tab.findChild(QLabel, "search-intro")
    assert label.text() == INTRO_TEXT
    assert "This tab has no search box." in INTRO_TEXT
    assert label.textInteractionFlags() == SELECTABLE_TEXT


def test_there_is_no_search_box_and_the_only_button_is_refresh(qtbot):
    tab = _tab(qtbot)
    assert tab.findChildren(QLineEdit) == []
    assert [button.text() for button in tab.findChildren(QPushButton)] == ["Refresh"]


def test_first_show_reads_research_only_and_sends_nothing_for_code_or_outputs(qtbot):
    research = Route(_json(200, RUNS))
    code = NeverCalled()
    outputs = NeverCalled()
    tab = _tab(qtbot, transports={KIND_RESEARCH: research, "code": code, "outputs": outputs})
    _wait_read(qtbot, tab)
    assert len(research.calls) == 1
    assert research.calls[0][0] == URL
    assert research.calls[0][1].startswith("/desktop/research/runs?")
    assert code.calls == [] and outputs.calls == []


def test_before_any_read_code_and_outputs_say_no_data_source_and_nothing_is_sent(qtbot):
    research = Route(_json(200, RUNS))
    tab = _tab(qtbot, transports={KIND_RESEARCH: research}, show=False)
    assert _status_lines(tab) == [CODE_LINE, OUTPUTS_LINE, NOT_CHECKED_TEXT]
    assert research.calls == []


def test_refresh_reads_research_again_and_nothing_else(qtbot):
    research = Route(_json(200, RUNS))
    code = NeverCalled()
    tab = _tab(qtbot, transports={KIND_RESEARCH: research, "code": code})
    _wait_read(qtbot, tab)
    tab.findChild(QPushButton, "search-refresh").click()
    qtbot.waitUntil(lambda: len(research.calls) == 2, timeout=5000)
    _wait_read(qtbot, tab)
    assert code.calls == []


def test_all_kinds_shows_code_and_outputs_as_headers_then_research_groups(qtbot):
    tab = _tab(qtbot, transports={KIND_RESEARCH: Route(_json(200, RUNS))})
    _wait_read(qtbot, tab)
    assert _status_lines(tab) == [CODE_LINE, OUTPUTS_LINE, RUNS_LINE]
    assert _table_rows(tab) == ALL_KINDS_HEADERS + RESEARCH_ROWS


def test_research_runs_are_grouped_by_provider_with_a_spanning_header_per_group(qtbot):
    tab = _tab(qtbot, transports={KIND_RESEARCH: Route(_json(200, RUNS))})
    _wait_read(qtbot, tab)
    _choose(tab, "Research")
    assert _table_rows(tab) == RESEARCH_ROWS
    table = _table(tab)
    assert table.columnSpan(0, 0) == len(RESULT_COLUMNS)
    assert table.columnSpan(3, 0) == len(RESULT_COLUMNS)
    assert table.columnSpan(1, 0) == 1
    assert _status_lines(tab) == [RUNS_LINE]


def test_a_kind_with_no_groups_is_one_header_row_with_its_state(qtbot):
    tab = _tab(qtbot, transports={KIND_RESEARCH: Route(_json(200, {"runs": []}))})
    _wait_read(qtbot, tab)
    assert _table_rows(tab) == [
        ("Code: no data source yet", None, None, None, None),
        ("Outputs: no data source yet", None, None, None, None),
        ("Research: nothing to show yet", None, None, None, None),
    ]
    assert _status_lines(tab)[-1] == "Research: no research runs yet."


def test_the_kind_filter_shows_one_kind_and_asks_nothing_new(qtbot):
    research = Route(_json(200, RUNS))
    code = NeverCalled()
    tab = _tab(qtbot, transports={KIND_RESEARCH: research, "code": code})
    _wait_read(qtbot, tab)
    calls_before = len(research.calls)
    combo = tab.findChild(QComboBox, "search-kind")
    assert [combo.itemText(index) for index in range(combo.count())] == ["All kinds", "Code", "Outputs", "Research"]

    _choose(tab, "Code")
    assert _status_lines(tab) == [CODE_LINE]
    assert _table_rows(tab) == [("Code: no data source yet", None, None, None, None)]

    _choose(tab, "Research")
    assert _status_lines(tab) == [RUNS_LINE]
    assert _table_rows(tab) == RESEARCH_ROWS
    assert code.calls == []
    assert len(research.calls) == calls_before


def test_a_missing_research_route_says_not_connected_and_shows_only_its_header(qtbot):
    tab = _tab(qtbot, transports={KIND_RESEARCH: Route()})
    _wait_read(qtbot, tab)
    assert _status_lines(tab) == [CODE_LINE, OUTPUTS_LINE, ROUTE_MISSING_LINE]
    assert _table_rows(tab)[-1] == ("Research: not connected", None, None, None, None)


def test_when_nothing_listens_the_not_running_sentence_is_shown_once(qtbot):
    def down(base_url, path, timeout):
        raise local_client.LocalClientError("not_running", "nothing listens")

    tab = _tab(qtbot, transports={KIND_RESEARCH: down})
    _wait_read(qtbot, tab)
    assert _status_lines(tab).count(NOT_RUNNING_LINE) == 1


def test_a_non_loopback_url_sends_nothing_and_says_why(qtbot):
    research = Route(_json(200, RUNS))
    tab = _tab(qtbot, url="http://example.com", transports={KIND_RESEARCH: research})
    _wait_read(qtbot, tab)
    assert research.calls == []
    assert "not on this computer" in _status_lines(tab)[-1]


def test_refresh_is_off_while_a_read_runs(qtbot):
    release = []

    class Slow(Route):
        def __call__(self, base_url, path, timeout):
            gate = threading.Event()
            release.append(gate)
            gate.wait(5)
            return _json(200, RUNS)

    tab = _tab(qtbot, transports={KIND_RESEARCH: Slow()})
    qtbot.waitUntil(lambda: len(release) == 1, timeout=5000)
    button = tab.findChild(QPushButton, "search-refresh")
    assert not button.isEnabled()
    assert _status_lines(tab) == [READING_TEXT]
    button.click()  # a press while a read runs does nothing
    assert len(release) == 1
    release[0].set()
    _wait_read(qtbot, tab)


def test_a_stale_answer_is_dropped(qtbot):
    tab = _tab(qtbot, transports={KIND_RESEARCH: Route(_json(200, RUNS))})
    _wait_read(qtbot, tab)
    before = _status_lines(tab)
    stale = ListOutcome((KindList(KIND_RESEARCH, STATE_RESULTS, "stale line"),))
    shared_delivery().delivered.emit(Delivered(tab._owner, (tab._generation - 1, (stale, None))))  # noqa: SLF001
    assert _status_lines(tab) == before


def test_a_failed_job_shows_the_research_failure_and_how_to_retry(qtbot):
    tab = _tab(qtbot, transports={KIND_RESEARCH: Route(_json(200, RUNS))})
    _wait_read(qtbot, tab)
    shared_delivery().delivered.emit(Delivered(tab._owner, (tab._generation, (None, "RuntimeError"))))  # noqa: SLF001
    assert _status_lines(tab)[-1] == FAILED_TEXT.format("RuntimeError")
    assert _status_lines(tab)[-1].endswith("Press Refresh to try again.")


def test_a_failed_read_from_the_server_side_is_the_research_error_line(qtbot):
    def boom(base_url, path, timeout):
        raise RuntimeError("socket exploded")

    tab = _tab(qtbot, transports={KIND_RESEARCH: boom})
    _wait_read(qtbot, tab)
    assert _status_lines(tab)[-1].startswith("Research: the runs could not be read.")


def test_the_table_cannot_be_edited_and_every_label_is_selectable(qtbot):
    tab = _tab(qtbot)
    table = _table(tab)
    assert table.editTriggers() == table.EditTrigger.NoEditTriggers
    for label in tab.findChildren(QLabel):
        if label.text():
            assert label.textInteractionFlags() == SELECTABLE_TEXT, label.objectName()


def test_copy_of_a_selected_group_header_keeps_its_text(qtbot):
    tab = _tab(qtbot, transports={KIND_RESEARCH: Route(_json(200, RUNS))})
    _wait_read(qtbot, tab)
    _choose(tab, "Research")
    table = _table(tab)
    table.item(0, 0).setSelected(True)
    assert selected_cells_text(table) == "Research: openalex (2 run(s))"


def test_kinds_for_maps_the_filter_to_its_kinds():
    assert kinds_for("") == ("code", "outputs", "research")
    assert kinds_for("code") == ("code",)
    assert kinds_for("research") == ("research",)
    assert kinds_for("zotero") == ("code", "outputs", "research")


def test_table_lines_mark_group_headers_and_run_rows():
    runs = (Row("run-a", "done", "2026-10-02", "", ""),)
    lines = table_lines((KindList(KIND_RESEARCH, STATE_RESULTS, "x", (Group("openalex", runs),)),))
    assert lines == (
        TableLine(("Research: openalex (1 run(s))",), True),
        TableLine(("run-a", "done", "2026-10-02", NOT_REPORTED, NOT_REPORTED), False),
    )


def test_status_text_has_one_line_per_listed_kind():
    lines = (KindList("code", "no_source", "Code line"), KindList("research", "empty", "Research line"))
    assert status_text(lines) == "Code line\nResearch line"
