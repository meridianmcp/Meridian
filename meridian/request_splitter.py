"""Split a pasted request dump into one item per request.

People paste lists of requests from chats, emails and notes. This module turns
such a dump into candidate items for a human to review. It is a pure text
function: no DB and no MCP imports, and it files nothing.

Rules:

- Each numbered line (``1.`` or ``1)``) or bulleted line (``-``, ``*``, ``+``
  or a bullet character) is one item. Its title is the text after the marker.
- A plain text line directly after a list item, with no blank line between, is
  a continuation of that item. It stays in the item's notes.
- Any other run of plain text lines, up to a blank line or a list line, is one
  paragraph item. Its title is the paragraph on one line.
- ``notes`` holds the original text of the item as pasted, with its outer
  whitespace stripped.
- A bare marker with no text after it, and a horizontal rule such as ``---``,
  file nothing and end the item before them.
- Empty or whitespace-only input returns an empty list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: A list line. The text group is None for a bare marker such as "-" or "1.".
#: Leading indentation is allowed so that nested bullets count as items too.
_LIST_LINE = re.compile(r"^\s*(?:[-*+•]|[0-9]+[.)])(?:\s+(?P<text>.*\S))?\s*$")

#: A horizontal rule used as a separator in pasted notes.
_RULE_LINE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")


@dataclass
class _Block:
    """One run of lines: a list item with its continuation, or a plain paragraph."""

    is_list: bool
    lines: list[str] = field(default_factory=list)
    title: str = ""


def _one_line(text: str) -> str:
    """Collapse every run of whitespace to a single space."""
    return " ".join(text.split())


def _group(text: str) -> list[_Block]:
    """Group the lines of ``text`` into list items and paragraphs, in order."""
    blocks: list[_Block] = []
    open_block: _Block | None = None
    for raw in text.splitlines():
        if not raw.strip() or _RULE_LINE.match(raw):
            open_block = None
            continue
        match = _LIST_LINE.match(raw)
        if match is None:
            if open_block is None:
                open_block = _Block(is_list=False)
                blocks.append(open_block)
            open_block.lines.append(raw)
            continue
        open_block = None
        if match.group("text") is not None:
            open_block = _Block(is_list=True, lines=[raw], title=_one_line(match.group("text")))
            blocks.append(open_block)
    return blocks


def split_request_text(text: str | None) -> list[dict[str, str]]:
    """Split pasted text into ``{"title": ..., "notes": ...}`` items, in input order."""
    if not text or not text.strip():
        return []
    items: list[dict[str, str]] = []
    for block in _group(text):
        if block.is_list:
            title = block.title
        else:
            title = _one_line(" ".join(line.strip() for line in block.lines))
        items.append({"title": title, "notes": "\n".join(block.lines).strip()})
    return items
