"""Regression tests for PAPER-S4's tab/break paragraph-text fidelity fix.

Found via the ooxml-graph-paper project (PAPER-30/36/38): the paper's own
independent gold extractor previously dropped <w:tab/> and <w:br/> entirely
when reconstructing paragraph text (only <w:t> was concatenated). Fixing
that gold-side bug alone exposed that Meridian's own real paragraph-text
extraction -- docparse.docs_intel._paragraph_text and its vendored copy,
meridian_docs._vendored_content_tree._paragraph_text -- shared the IDENTICAL
bug: a tab-separated run like "(a)\\tsome text" reconstructed as "(a)some
text" with no separator at all. This is task_945705a3 / PAPER-S4.

Both locations must convert <w:tab/> to a literal tab and <w:br/>/<w:cr/> to
a newline, matching python-docx's own .text convention -- so a document
compared against python-docx's extraction (or Word's own rendering) is not
silently missing separators.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

# <ci-fix> — `docparse` belongs to the separate, external ooxml-graph-paper
# project (see module docstring); it is never installed alongside this repo's
# own CI/pixi environment, only in a local dev checkout that also happens to
# have that sibling project available. Guard the import so the cross-project
# comparison runs opportunistically when present, without hard-crashing test
# COLLECTION (not just this file) everywhere it isn't -- which is what an
# unguarded import does, since a collection error in one file aborts the
# whole pytest run.
try:
    from docparse.docs_intel import _paragraph_text as docparse_paragraph_text
except ImportError:
    docparse_paragraph_text = None
from meridian_docs._vendored_content_tree import _paragraph_text as vendored_paragraph_text

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _q(tag: str) -> str:
    return f"{{{_W}}}{tag}"


def _build_paragraph(*run_specs: list[tuple[str, str]]) -> ET.Element:
    """run_specs: each run is a list of (kind, value) pairs, kind in
    ("t", "tab", "br", "cr"); value is the text for "t", ignored otherwise."""
    p = ET.Element(_q("p"))
    for spec in run_specs:
        r = ET.SubElement(p, _q("r"))
        for kind, value in spec:
            if kind == "t":
                el = ET.SubElement(r, _q("t"))
                el.text = value
            else:
                ET.SubElement(r, _q(kind))
    return p


# <ci-fix> — always include the real, in-repo vendored implementation; only
# include the external docparse one when it's actually importable, so the
# regression protection this repo cares about runs unconditionally while the
# cross-project comparison is best-effort.
_IMPLS = [vendored_paragraph_text]
_IDS = ["meridian_docs._vendored_content_tree"]
if docparse_paragraph_text is not None:
    _IMPLS.insert(0, docparse_paragraph_text)
    _IDS.insert(0, "docparse.docs_intel")


@pytest.mark.parametrize("paragraph_text", _IMPLS, ids=_IDS)
class TestParagraphTextTabBreakFidelity:
    def test_converts_tab_to_literal_tab_character(self, paragraph_text):
        p = _build_paragraph([("t", "(a)"), ("tab", ""), ("t", "there is text")])
        assert paragraph_text(p) == "(a)\tthere is text"

    def test_converts_br_and_cr_to_newline(self, paragraph_text):
        p = _build_paragraph(
            [("t", "line one"), ("br", ""), ("t", "line two"), ("cr", ""), ("t", "line three")]
        )
        assert paragraph_text(p) == "line one\nline two\nline three"

    def test_preserves_plain_text_with_no_tab_or_break(self, paragraph_text):
        p = _build_paragraph([("t", "Hello "), ("t", "world")])
        assert paragraph_text(p) == "Hello world"

    def test_handles_multiple_runs_with_mixed_content(self, paragraph_text):
        p = _build_paragraph(
            [("t", "(a)"), ("tab", "")],
            [("t", "clause text"), ("br", ""), ("t", "continued")],
        )
        assert paragraph_text(p) == "(a)\tclause text\ncontinued"

    def test_empty_paragraph_is_empty_string(self, paragraph_text):
        p = ET.Element(_q("p"))
        assert paragraph_text(p) == ""
