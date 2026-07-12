"""Shared structural-parser interface for docparse (67402ce7).

``docs_intel`` (OOXML/.docx) and ``latex_intel`` (LaTeX/.tex) grew independently
but converged on the *same* structural contract: parse a document's source into a
heading outline (a flat, document-ordered ``headings`` list) plus a ``tree`` that
nests those headings by level, and expose a one-call ``analyze`` entrypoint that
layers format-specific extras (bibliography/citations, field codes, ...) on top of
that structure.

This module names that convergence explicitly. :class:`StructuralParser` is an
**interface-only** abstract base — it defines the shared *shape* both formats
already produce and a single genuinely-shared helper (:meth:`build_tree`, the
level-nesting algorithm ``latex_intel._build_tree`` and the pure-heading half of
``docs_intel``'s content tree both implement identically). It deliberately does
**not** touch any per-format parsing logic: the DOCX zip/XML walk and the LaTeX
``latexwalker`` walk stay exactly where they are. Subclassing is a way to declare
"this parser honours the common structural contract", not a place to move
format-specific behaviour.

Contract (what every subclass's ``parse_structure`` returns):

* ``heading_count: int`` — number of headings discovered.
* ``headings: list[dict]`` — flat, document-ordered outline. Each heading carries
  at least a ``level: int`` and a ``text: str`` (formats may add ``kind`` /
  ``para_id`` / etc.).
* ``tree: list[dict]`` — the same headings nested by level; each node is the
  heading dict plus a ``children: list`` of the headings nested beneath it.

``analyze`` is the top-level entrypoint: ``parse_structure`` plus any
format-specific augmentation, returned as one dict that *spreads the structure
keys* so a caller sees ``heading_count`` / ``headings`` / ``tree`` uniformly
across formats.

Pure interface — no I/O, no third-party dependency; safe to import anywhere.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class StructuralParser(ABC):
    """Common structural-parsing interface for docparse format layers.

    A subclass owns one document format (DOCX, LaTeX, ...). It must implement
    :meth:`parse_structure` (source -> heading outline + tree, the shared shape)
    and :meth:`analyze` (the one-call entrypoint that adds format-specific extras
    on top of the structure). The concrete :meth:`build_tree` helper is provided
    so subclasses that nest a flat heading outline by level do not each re-derive
    the identical stack algorithm.

    This base is interface-only: it holds no format state and performs no parsing.
    The existing module-level functions (``parse_latex_structure`` /
    ``document_outline`` / ``analyze_latex`` / ...) remain the public API and are
    unchanged; a subclass simply delegates to them so the two surfaces are
    provably conformant to one contract.
    """

    @abstractmethod
    def parse_structure(self, source: Any) -> dict[str, Any]:
        """Parse ``source`` into ``{heading_count, headings, tree, ...}``.

        ``headings`` is the flat, document-ordered outline (each entry carries at
        least ``level`` and ``text``); ``tree`` nests those headings by level
        (each node = the heading dict + a ``children`` list). Implementations must
        never raise — a malformed/empty source degrades to an empty-but-well-formed
        result (``heading_count == 0``, empty ``headings``/``tree``).
        """
        raise NotImplementedError

    @abstractmethod
    def analyze(self, source: Any) -> dict[str, Any]:
        """One-call entrypoint: the structure plus format-specific augmentation.

        Returns a dict that *spreads* the :meth:`parse_structure` keys
        (``heading_count`` / ``headings`` / ``tree``) so a caller reads structure
        uniformly across formats, alongside any format-specific extras (a LaTeX
        bibliography + citations, DOCX field codes, ...). Never raises.
        """
        raise NotImplementedError

    @staticmethod
    def build_tree(headings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Nest a flat, document-ordered heading outline into a tree by level.

        Each input heading is a dict carrying at least ``level`` (and typically
        ``text`` — any other keys, e.g. ``kind``, are preserved). Output nodes are
        the same dicts with a fresh ``children: list`` added. A heading attaches
        under the nearest preceding heading of a *strictly smaller* level;
        otherwise it becomes a root.

        This is the single shared structural algorithm both format layers use to
        turn an outline into a hierarchy — identical semantics to the historical
        ``latex_intel._build_tree`` and the heading-nesting half of
        ``docs_intel``. Preserving every original key (rather than copying only a
        fixed subset) keeps it format-agnostic.
        """
        roots: list[dict[str, Any]] = []
        stack: list[dict[str, Any]] = []
        for h in headings:
            node = {**h, "children": []}
            while stack and stack[-1]["level"] >= node["level"]:
                stack.pop()
            if stack:
                stack[-1]["children"].append(node)
            else:
                roots.append(node)
            stack.append(node)
        return roots
