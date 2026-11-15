"""Arrange project rows as a tree by parent_project_id. Pure Python: no Qt, no I/O.

A row whose parent_project_id names another row in the list is a child of that row. Every other
row is a root: a row with no parent, an orphan whose parent is not in the list, a row that names
itself as its parent, and a row in a loop of parents, where no row has a root above it.

Roots keep the order they were given, and so do the children under each parent. The walk is depth
first, so each child follows its parent and then its own children. Every row appears exactly once.
A root is at depth 0, and a child is one deeper than its parent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

ID_KEY = "id"
PARENT_KEY = "parent_project_id"

Arranged = tuple[Mapping[str, Any], int]


def _parent_position(row: Mapping[str, Any], position: int, index_of: Mapping[Any, int]) -> int | None:
    """The position of this row's parent in the list, or None when the row is a root."""
    parent_id = row.get(PARENT_KEY)
    if parent_id is None or parent_id == "":
        return None
    parent = index_of.get(parent_id)
    if parent is None or parent == position:
        return None
    return parent


def arrange(rows: Sequence[Mapping[str, Any]]) -> list[Arranged]:
    """Return (row, depth) pairs in display order. Each row appears once, and no row is dropped."""
    index_of: dict[Any, int] = {}
    for position, row in enumerate(rows):
        index_of.setdefault(row.get(ID_KEY), position)

    roots: list[int] = []
    children: dict[int, list[int]] = {}
    for position, row in enumerate(rows):
        parent = _parent_position(row, position, index_of)
        if parent is None:
            roots.append(position)
        else:
            children.setdefault(parent, []).append(position)

    ordered: list[Arranged] = []
    placed: set[int] = set()

    def place_tree(start: int) -> None:
        stack: list[tuple[int, int]] = [(start, 0)]
        while stack:
            position, depth = stack.pop()
            if position in placed:
                continue
            placed.add(position)
            ordered.append((rows[position], depth))
            # Pushed in reverse, so the first child in the list is the next one popped.
            for child in reversed(children.get(position, [])):
                stack.append((child, depth + 1))

    for root in roots:
        place_tree(root)
    # A row still unplaced is in a loop of parents with no root above it. Its first such row becomes a root.
    for position in range(len(rows)):
        if position not in placed:
            place_tree(position)
    return ordered


__all__ = ["ID_KEY", "PARENT_KEY", "arrange"]
