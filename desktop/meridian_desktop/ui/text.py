"""Read-only text in the desktop UI can be selected and copied.

Every label that shows text gets TextSelectableByMouse and TextSelectableByKeyboard, so a user
can highlight it and copy it. A read-only table gets a Copy action (Ctrl+C, and the right-click
menu) that copies the selected cells as tab-separated text. Editable fields are not touched.

A drag cannot cross from one label to the next, so text that is read as one block (the AI clients
and tools card on Home) is one label: block_html turns its lines into one rich-text document, each
line a paragraph with the look it had as a label of its own. A block also gets its own Copy action
that copies all the text it shows (enable_widget_copy). set_label_text keeps a selection when the
text changes elsewhere, so a redraw does not clear it.
"""

from __future__ import annotations

from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QAction,
    QFont,
    QGuiApplication,
    QKeySequence,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextDocumentFragment,
    QTextOption,
)
from PySide6.QtGui import Qt as GuiQt
from PySide6.QtWidgets import QLabel, QLayout, QPlainTextEdit, QTableWidget, QWidget

SELECTABLE_TEXT = Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
COPY_TEXT = "Copy"
# A line break inside one paragraph. Copied text has a newline for it.
LINE_BREAK = chr(0x2028)

_TextWidget = TypeVar("_TextWidget", QLabel, QPlainTextEdit)


def make_selectable(widget: _TextWidget) -> _TextWidget:
    """Let the user select and copy the widget's text by mouse and keyboard. Returns the widget.

    For a QPlainTextEdit, call this after setReadOnly(True), which otherwise allows mouse selection only.
    """
    widget.setTextInteractionFlags(SELECTABLE_TEXT)
    return widget


def _utf16(text: str) -> array:
    """The text as UTF-16 code units, which is how Qt counts positions in it."""
    return array("H", text.encode("utf-16-le"))


def _kept_range(old: str, new: str, start: int, end: int) -> tuple[int, int] | None:
    """Where the selection ``start``..``end`` of ``old`` falls in ``new``, or None when its text is gone.

    The texts share a start and an end, and differ in one span between them. A selection before or after that span keeps
    its text, so it moves with it. A selection that reaches into the span grows to cover the new span. A selection that
    lies inside the span is dropped, because the text it selected is no longer there.
    """
    before, after = _utf16(old), _utf16(new)
    prefix = 0
    limit = min(len(before), len(after))
    while prefix < limit and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and before[-1 - suffix] == after[-1 - suffix]:
        suffix += 1
    changed_end = len(before) - suffix
    if prefix <= start and end <= changed_end and prefix < changed_end:
        return None
    shift = len(after) - len(before)

    def place(position: int, *, is_end: bool) -> int:
        if position <= prefix:
            return position
        if position >= changed_end:
            return position + shift
        return changed_end + shift if is_end else prefix

    return place(start, is_end=False), place(end, is_end=True)


def _label_text(label: QLabel) -> str:
    """The text a label shows, as plain text. A line break and a paragraph break in a block read as newlines.

    Each replacement is one character for one character, so positions in the text still match the label's document.
    """
    text = label.text()
    if label.textFormat() == Qt.TextFormat.RichText or (
        label.textFormat() == Qt.TextFormat.AutoText and GuiQt.mightBeRichText(text)
    ):
        plain = QTextDocumentFragment.fromHtml(text).toPlainText()
        return plain.replace(LINE_BREAK, "\n").replace(chr(0x2029), "\n")
    return text


def set_label_text(label: QLabel, text: str) -> None:
    """Show ``text`` in the label. The same text again is left alone, so a selection in it stays.

    New text keeps a selection whose text is still there: it moves with that text, and one that reaches into the part
    that changed grows to cover it (see _kept_range). So a redraw that changes one line does not clear a selection.
    """
    if label.text() == text:
        return
    if not label.hasSelectedText():
        label.setText(text)
        return
    old_plain = _label_text(label)
    start = label.selectionStart()
    end = start + len(_utf16(label.selectedText()))
    label.setText(text)
    kept = _kept_range(old_plain, _label_text(label), start, end)
    # setSelection lays the new text out first. A dropped selection is cleared the same way, because until the label
    # lays out its new text, hasSelectedText still reports the old selection.
    if kept is not None and kept[1] > kept[0]:
        label.setSelection(kept[0], kept[1] - kept[0])
    else:
        label.setSelection(0, 0)


@dataclass(frozen=True)
class BlockLine:
    """One line of a text block, drawn as one paragraph. ``text`` is plain text, and is shown as it is.

    ``point_size`` 0 keeps the label's own size. ``space_after`` is the gap below the paragraph and ``right_margin`` the
    room kept free at its right (for a button beside it), in pixels.
    """

    text: str
    bold: bool = False
    point_size: float = 0.0
    space_after: float = 0.0
    right_margin: float = 0.0


def block_html(lines: Sequence[BlockLine], font: QFont) -> str:
    """The lines as one rich-text document, for a QLabel in RichText format, in the label's ``font``.

    Each line is inserted as plain text, so nothing in it is read as markup. A newline in a line is a line break inside
    its paragraph, with no gap, as a plain-text label shows it.
    """
    document = QTextDocument()
    document.setDocumentMargin(0)
    document.setDefaultFont(font)
    cursor = QTextCursor(document)
    for index, line in enumerate(lines):
        block_format = QTextBlockFormat()
        block_format.setTopMargin(0)
        block_format.setLeftMargin(0)
        block_format.setBottomMargin(line.space_after)
        block_format.setRightMargin(line.right_margin)
        char_format = QTextCharFormat()
        if line.bold:
            char_format.setFontWeight(QFont.Weight.Bold)
        if line.point_size > 0:
            char_format.setFontPointSize(line.point_size)
        if index == 0:
            cursor.setBlockFormat(block_format)
            cursor.setBlockCharFormat(char_format)
        else:
            cursor.insertBlock(block_format, char_format)
        text = line.text.replace("\r\n", "\n").replace("\n", LINE_BREAK)
        cursor.insertText(text, char_format)
    return document.toHtml()


def _laid_out(label: QLabel) -> tuple[QTextDocument, float]:
    """The label's rich text laid out as QLabel lays out its own, with the y where the text starts.

    The text is laid out in the label's font, with no margin round the document, with the label's wrap and alignment,
    and as wide as its contents less its margin. A label with an indent is not handled.
    """
    document = QTextDocument()
    document.setDefaultFont(label.font())
    document.setHtml(label.text())
    option = document.defaultTextOption()
    option.setAlignment(label.alignment())
    option.setWrapMode(QTextOption.WrapMode.WordWrap if label.wordWrap() else QTextOption.WrapMode.ManualWrap)
    document.setDefaultTextOption(option)
    frame_format = document.rootFrame().frameFormat()
    frame_format.setMargin(0)
    document.rootFrame().setFrameFormat(frame_format)
    margin = label.margin()
    area = label.contentsRect().adjusted(margin, margin, -margin, -margin)
    document.setTextWidth(area.width())
    return document, float(area.top())


def paragraph_tops(label: QLabel) -> list[int]:
    """Where each paragraph of a rich-text label starts, in the label's own coordinates (see _laid_out)."""
    document, top = _laid_out(label)
    layout = document.documentLayout()
    tops: list[int] = []
    block = document.begin()
    while block.isValid():
        tops.append(round(top + layout.blockBoundingRect(block).top()))
        block = block.next()
    return tops


def hidden_on_purpose(widget: QWidget) -> bool:
    """True for a widget that hide() or setVisible(False) hid. A widget that a layout has not shown yet is not hidden."""
    return widget.isHidden() and widget.testAttribute(Qt.WidgetAttribute.WA_WState_ExplicitShowHide)


def _layout_labels(layout: QLayout, found: list[QLabel]) -> None:
    for index in range(layout.count()):
        item = layout.itemAt(index)
        widget = item.widget()
        if widget is not None:
            _widget_labels(widget, found)
        elif item.layout() is not None:
            _layout_labels(item.layout(), found)


def _widget_labels(widget: QWidget, found: list[QLabel]) -> None:
    if hidden_on_purpose(widget):
        return
    if isinstance(widget, QLabel):
        found.append(widget)
    elif widget.layout() is not None:
        _layout_labels(widget.layout(), found)


def visible_text(root: QWidget) -> str:
    """The text of every label in ``root``'s layout, one per line, in layout order: top to bottom, left to right.

    A label hidden on purpose (or inside a widget hidden on purpose), an empty label and the text of a button are left
    out. The order comes from the layouts, not from where the widgets are drawn.
    """
    found: list[QLabel] = []
    if root.layout() is not None:
        _layout_labels(root.layout(), found)
    return "\n".join(_label_text(label) for label in found if label.text())


def enable_widget_copy(widget: QWidget, text: Callable[[], str] | None = None) -> QAction:
    """A Copy action on ``widget``: in its right-click menu, and on Ctrl+C while the widget itself has focus.

    It copies ``text()``, by default visible_text(widget). The widget takes focus from a click on its own area or from
    Tab. Ctrl+C in a label inside it still copies only what is selected in that label.
    """
    read = text if text is not None else (lambda: visible_text(widget))
    action = QAction(COPY_TEXT, widget)
    action.setShortcut(QKeySequence(QKeySequence.StandardKey.Copy))
    action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)

    def copy_all() -> None:
        copied = read()
        if copied:
            QGuiApplication.clipboard().setText(copied)

    action.triggered.connect(copy_all)
    widget.addAction(action)
    widget.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
    if widget.focusPolicy() == Qt.FocusPolicy.NoFocus:
        widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    return action


def selected_cells_text(table: QTableWidget) -> str:
    """The selected cells: one line per row, cells separated by tabs. Empty when nothing is selected."""
    cells: dict[int, dict[int, str]] = {}
    for item in table.selectedItems():
        cells.setdefault(item.row(), {})[item.column()] = item.text()
    if not cells:
        return ""
    columns = sorted({column for row_cells in cells.values() for column in row_cells})
    return "\n".join(
        "\t".join(row_cells.get(column, "") for column in columns)
        for _row, row_cells in sorted(cells.items())
    )


def enable_table_copy(table: QTableWidget) -> None:
    """Ctrl+C and a right-click Copy action copy the table's selected cells to the clipboard."""
    action = QAction("Copy", table)
    action.setShortcut(QKeySequence(QKeySequence.StandardKey.Copy))
    action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)

    def copy_selection() -> None:
        text = selected_cells_text(table)
        if text:
            QGuiApplication.clipboard().setText(text)

    action.triggered.connect(copy_selection)
    table.addAction(action)
    table.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
