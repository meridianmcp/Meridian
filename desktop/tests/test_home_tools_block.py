"""The AI clients and tools card on Home is one selectable text block, so a drag selects every line and Ctrl+C copies them.

Offscreen Qt, pytest-qt, fakes only. No test opens a socket, starts a process or reads the real settings or keychain.
A drag is simulated as a selection over the block's text, which is what a drag from the first line to the last makes.
"""

import re
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QTextDocument
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QWidget

from meridian_desktop import local_client
from meridian_desktop.home_status import CardView, tools_view
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.tools_status import YES, ToolRow, ToolsStatus
from meridian_desktop.ui.home_tab import HomeDeps, HomeTab, _Card
from meridian_desktop.ui.text import SELECTABLE_TEXT, paragraph_tops, visible_text

from test_home_tab import FakeZotero, _deps, _shown, _tunnel_not_available

CARD_TITLE = "AI clients and tools"
ALIGN_TOLERANCE_PX = 2
# The block's paragraphs before the rows: the title, the headline, the detail and the checked line.
LINES_BEFORE_ROWS = 4


def _two_rows_needing_attention() -> ToolsStatus:
    rows = (
        ToolRow("Codex", "Meridian entries", "no", "unchecked", "Not in Codex's settings yet."),
        ToolRow("This computer", "Local Meridian server", YES, YES, "The local server answers."),
    )
    return ToolsStatus(rows=rows, message="Checked. No tool is started or stopped.", checked_at=datetime(2026, 10, 23, 14, 5))


def _expected_row_lines() -> list[str]:
    """Each row's text and detail, as the card draws them, in order."""
    view = tools_view(_two_rows_needing_attention())
    return [line for row in view.rows for line in (row.text, row.detail) if line]


def _tools_tab(qtbot, tmp_path, tools=None):
    tab = _shown(qtbot, _deps(tmp_path, tools=tools or (lambda: _two_rows_needing_attention())))
    card = tab.findChild(QFrame, "home-card-tools")
    qtbot.waitUntil(lambda: card.findChild(QLabel, "home-tools-headline").text() == "1 need attention", timeout=5000)
    return tab, card


def _block(card) -> QLabel:
    block = card.findChild(QLabel, "home-tools-text")
    assert block is not None, "the tools card has no text block"
    return block


def _plain(block: QLabel) -> str:
    """The block's text as plain text, one line per paragraph."""
    document = QTextDocument()
    document.setHtml(block.text())
    return document.toPlainText()


def _lines(text: str) -> list[str]:
    """The non-empty lines of a text. splitlines reads a paragraph break and a line break as a line end."""
    return [line for line in text.splitlines() if line.strip()]


def _select_all(block: QLabel) -> None:
    """A drag from the block's first character to its last."""
    document = QTextDocument()
    document.setHtml(block.text())
    block.setSelection(0, document.characterCount() - 1)


def _assert_in_order(text: str, expected: list[str]) -> None:
    position = 0
    for line in expected:
        found = text.find(line, position)
        assert found >= 0, f"{line!r} is missing, or out of order, in {text!r}"
        position = found + len(line)


def _bare_card(qtbot):
    """A block card on its own. The parent is returned too, so it lives as long as the test needs the card."""
    parent = QWidget()
    qtbot.addWidget(parent)
    card = _Card(
        "tools",
        CARD_TITLE,
        work=None,
        render=None,
        on_action=lambda key: None,
        parent=parent,
        block_text=True,
    )
    parent.show()
    return parent, card


def test_the_tools_card_draws_its_lines_as_one_selectable_block(qtbot, tmp_path):
    _tab, card = _tools_tab(qtbot, tmp_path)
    block = _block(card)

    assert block.isVisible()
    assert block.textInteractionFlags() & SELECTABLE_TEXT == SELECTABLE_TEXT
    # Every line of the card is in the block, in order: title, headline, then each row and its detail.
    _assert_in_order(_plain(block), [CARD_TITLE, "1 need attention", *_expected_row_lines()])
    # Each line keeps its own label under its old name, but none of them is shown: the block shows the text.
    for name in ("home-tools-title", "home-tools-headline", "home-tools-detail", "home-tools-checked"):
        line = card.findChild(QLabel, name)
        assert line is not None and not line.isVisible(), name
    assert visible_text(card).count("1 need attention") == 1


def test_a_drag_over_the_whole_card_selects_every_line(qtbot, tmp_path):
    _tab, card = _tools_tab(qtbot, tmp_path)
    block = _block(card)

    _select_all(block)

    selected = block.selectedText()
    _assert_in_order(selected, [CARD_TITLE, "1 need attention", *_expected_row_lines()])
    assert _lines(selected) == _lines(_plain(block))


def test_ctrl_c_copies_every_line_of_the_card(qtbot, tmp_path):
    _tab, card = _tools_tab(qtbot, tmp_path)
    copy = next(action for action in card.actions() if action.text() == "Copy")

    copy.trigger()

    copied = QGuiApplication.clipboard().text()
    _assert_in_order(copied, [CARD_TITLE, "1 need attention", *_expected_row_lines()])
    # The copied text has plain spaces and newlines, not the block's internal no-break spaces and line separators.
    assert chr(0x00A0) not in copied and chr(0x2028) not in copied


def test_the_card_menu_copies_the_card_and_a_right_click_on_the_block_opens_it(qtbot, tmp_path):
    _tab, card = _tools_tab(qtbot, tmp_path)
    block = _block(card)

    assert card.contextMenuPolicy() == Qt.ContextMenuPolicy.ActionsContextMenu
    assert block.contextMenuPolicy() == Qt.ContextMenuPolicy.NoContextMenu
    assert any(action.text() == "Copy" for action in card.actions())


def test_each_row_button_sits_at_the_right_level_with_its_row(qtbot, tmp_path):
    _tab, card = _tools_tab(qtbot, tmp_path)
    block = _block(card)
    tops = paragraph_tops(block)
    # Each row is a text paragraph and a detail paragraph, so the second row starts two paragraphs after the first.
    first_row_top = block.geometry().top() + tops[LINES_BEFORE_ROWS]
    second_row_top = block.geometry().top() + tops[LINES_BEFORE_ROWS + 2]

    first = card.findChild(QPushButton, "home-tools-row-0-action")
    second = card.findChild(QPushButton, "home-tools-row-1-action")
    assert first is not None and second is not None
    assert abs(first.geometry().top() - first_row_top) <= ALIGN_TOLERANCE_PX
    assert abs(second.geometry().top() - second_row_top) <= ALIGN_TOLERANCE_PX
    # A button sits inside the block's right edge, so the text beside it is never drawn under it.
    assert first.geometry().right() <= block.geometry().right()
    assert second.geometry().right() <= block.geometry().right()


def test_a_row_button_still_runs_its_action(qtbot, tmp_path):
    reads = []

    def tools():
        reads.append(1)
        return _two_rows_needing_attention()

    _tab, card = _tools_tab(qtbot, tmp_path, tools=tools)
    second = card.findChild(QPushButton, "home-tools-row-1-action")
    assert second.text() == "Check again"

    second.click()

    qtbot.waitUntil(lambda: len(reads) == 2, timeout=5000)


def test_the_buttons_follow_their_rows_when_the_card_is_narrowed(qtbot, tmp_path):
    _tab, card = _tools_tab(qtbot, tmp_path)
    block = _block(card)

    card.resize(260, card.height())
    qtbot.wait(20)

    tops = paragraph_tops(block)
    second = card.findChild(QPushButton, "home-tools-row-1-action")
    assert abs(second.geometry().top() - (block.geometry().top() + tops[LINES_BEFORE_ROWS + 2])) <= ALIGN_TOLERANCE_PX
    assert second.geometry().right() <= block.geometry().right()


def test_a_redraw_that_changes_one_line_keeps_a_selection_in_another(qtbot):
    _parent, card = _bare_card(qtbot)
    card.show_view(CardView(headline="1 need attention", detail="Checked.", checked="Checked at 14:05."))
    block = card.findChild(QLabel, "home-tools-text")
    start = _plain(block).index("need attention")
    block.setSelection(start, len("need attention"))

    # Only the checked line changes, after the selection, so the selection keeps its text.
    card.show_view(CardView(headline="1 need attention", detail="Checked.", checked="Checked at 14:06."))

    assert block.selectedText() == "need attention"
    assert "14:06" in _plain(block)


def test_a_block_card_with_no_rows_draws_its_headline_and_no_buttons(qtbot):
    _parent, card = _bare_card(qtbot)

    card.show_view(CardView(headline="Checking...", detail="Checking the tools."))

    assert _plain(card.findChild(QLabel, "home-tools-text")).splitlines() == [
        CARD_TITLE,
        "Checking...",
        "Checking the tools.",
    ]
    assert card.findChildren(QPushButton, "home-tools-row-0-action") == []


def test_the_other_home_cards_keep_their_own_labels(qtbot, tmp_path):
    tab = _shown(qtbot, _deps(tmp_path))

    for key in ("local", "tunnel", "services", "zotero", "updates"):
        card = tab.findChild(QFrame, f"home-card-{key}")
        assert card.findChild(QLabel, f"home-{key}-text") is None, key


def test_home_builds_with_the_text_block_card(qtbot, tmp_path):
    tab = HomeTab(
        HomeDeps(
            store=SettingsStore(tmp_path / "settings.json"),
            probe=lambda url: local_client.HEALTH_NOT_RUNNING,
            tunnel=_tunnel_not_available,
            zotero=FakeZotero(),
            tools=_two_rows_needing_attention,
        )
    )
    qtbot.addWidget(tab)

    assert tab.findChild(QFrame, "home-card-tools").findChild(QLabel, "home-tools-text") is not None
