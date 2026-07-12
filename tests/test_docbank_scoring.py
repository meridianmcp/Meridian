"""Unit coverage for the DocBank scoring pipeline (2c18bd07).

Exercises every stage of ``docparse.docbank_scoring`` on tiny synthetic inputs —
no DocBank download, no LaTeX install required:

* DocBank per-page label-file parsing (incl. tolerant handling of junk lines),
* the DocBank-label -> scored-category mapping (and the deliberate exclusion of
  categories ``latex_intel`` cannot produce, per 26e08530),
* reading-order token alignment (equal- and unequal-length streams), and
* per-category + micro/macro precision / recall / F1 on a hand-built example
  whose expected metrics are computed by hand in the test.
"""
from __future__ import annotations

import math

from docparse.docbank_scoring import (
    HEADING,
    OTHER,
    REFERENCE,
    SCORED_CATEGORIES,
    AlignedPair,
    Token,
    align_streams,
    map_docbank_label,
    parse_docbank_labels,
    score_alignment,
    score_docbank_page,
    structure_to_token_stream,
)


# --- DocBank label-file parsing --------------------------------------------

# token x0 y0 x1 y1 R G B font label  (the standard DocBank per-page format).
_SAMPLE_DOCBANK = """\
Introduction 100 100 250 120 0 0 0 Times section
Methods 100 130 220 150 0 0 0 Times section
We 100 200 130 215 0 0 0 Times paragraph
find 135 200 175 215 0 0 0 Times paragraph
[12] 100 400 130 415 0 0 0 Times reference
Smith 135 400 190 415 0 0 0 Times reference

Figure 100 500 160 520 0 0 0 Times caption
"""


def test_parse_docbank_labels_reads_tokens_in_order():
    tokens = parse_docbank_labels(_SAMPLE_DOCBANK)
    # 7 valid token lines (the blank line is skipped).
    assert len(tokens) == 7
    assert [t.text for t in tokens] == [
        "Introduction",
        "Methods",
        "We",
        "find",
        "[12]",
        "Smith",
        "Figure",
    ]
    # bbox parsed from cols 1..4.
    assert tokens[0].bbox == (100, 100, 250, 120)
    # raw label preserved, category mapped.
    assert tokens[0].raw_label == "section"
    assert tokens[0].category == HEADING
    # 'paragraph' + 'caption' both collapse to OTHER (not producible categories).
    assert tokens[2].category == OTHER
    assert tokens[-1].category == OTHER
    # 'reference' -> REFERENCE.
    assert tokens[4].category == REFERENCE
    assert tokens[5].category == REFERENCE


def test_parse_docbank_labels_skips_malformed_and_blank_lines():
    text = "\n".join(
        [
            "",  # blank
            "   ",  # whitespace only
            "short line only",  # < 10 columns -> skipped
            "Good 1 2 3 4 0 0 0 Times title",  # valid
        ]
    )
    tokens = parse_docbank_labels(text)
    assert len(tokens) == 1
    assert tokens[0].text == "Good"
    assert tokens[0].category == HEADING  # 'title' -> heading


def test_parse_docbank_labels_bad_bbox_degrades_to_none():
    # Non-integer bbox columns must not raise; bbox becomes None, token kept.
    tokens = parse_docbank_labels("Tok x0 y0 x1 y1 0 0 0 Times section")
    assert len(tokens) == 1
    assert tokens[0].bbox is None
    assert tokens[0].category == HEADING


def test_parse_docbank_labels_empty_input():
    assert parse_docbank_labels("") == []
    assert parse_docbank_labels(None) == []  # type: ignore[arg-type]


# --- Category mapping -------------------------------------------------------


def test_map_docbank_label_scored_categories():
    assert map_docbank_label("section") == HEADING
    assert map_docbank_label("title") == HEADING
    assert map_docbank_label("reference") == REFERENCE


def test_map_docbank_label_unproducible_categories_are_other():
    # Per 26e08530: the parser cannot emit these, so they must NOT be scored.
    for lbl in ("figure", "table", "equation", "caption", "abstract", "author"):
        assert map_docbank_label(lbl) == OTHER


def test_map_docbank_label_is_case_insensitive_and_tolerant():
    assert map_docbank_label("SECTION") == HEADING
    assert map_docbank_label("  Reference  ") == REFERENCE
    # Out-of-vocabulary label -> OTHER, never raises.
    assert map_docbank_label("totally-unknown") == OTHER
    assert map_docbank_label("") == OTHER


def test_scored_categories_excludes_other_and_unproducible():
    assert set(SCORED_CATEGORIES) == {HEADING, REFERENCE}
    assert OTHER not in SCORED_CATEGORIES


# --- latex_intel structure -> token stream ---------------------------------


def test_structure_to_token_stream_interleaves_headings_and_citations():
    analysis = {
        "headings": [
            {"level": 2, "kind": "section", "text": "Introduction"},
            {"level": 2, "kind": "section", "text": "Related Work"},
        ],
        "citations": [
            # Encloses under heading 1 ("Related Work"): emitted after it.
            {"key": "smith2020", "marker_text": "\\cite{smith2020}", "section_ordinal": 1},
            # Precedes any heading: leads the stream.
            {"key": "lead", "marker_text": "\\cite{lead}", "section_ordinal": None},
        ],
    }
    stream = structure_to_token_stream(analysis)
    labelled = [(t.text, t.category) for t in stream]
    assert labelled == [
        ("\\cite{lead}", REFERENCE),  # None-ordinal citation leads
        ("Introduction", HEADING),  # heading 0
        ("Related", HEADING),  # heading 1, word 1
        ("Work", HEADING),  # heading 1, word 2
        ("\\cite{smith2020}", REFERENCE),  # citation after heading 1
    ]


def test_structure_to_token_stream_falls_back_to_key_when_marker_empty():
    analysis = {
        "headings": [],
        "citations": [{"key": "onlykey", "marker_text": "", "section_ordinal": None}],
    }
    stream = structure_to_token_stream(analysis)
    assert [(t.text, t.category) for t in stream] == [("onlykey", REFERENCE)]


def test_structure_to_token_stream_empty_analysis():
    assert structure_to_token_stream({}) == []


# --- Alignment --------------------------------------------------------------


def test_align_streams_equal_length_index_alignment():
    pred = [Token("a", HEADING), Token("b", REFERENCE)]
    gold = [Token("a", HEADING), Token("b", OTHER)]
    pairs = align_streams(pred, gold)
    assert pairs == [
        AlignedPair(HEADING, HEADING),
        AlignedPair(REFERENCE, OTHER),
    ]


def test_align_streams_pads_shorter_side_with_gaps():
    pred = [Token("a", HEADING)]
    gold = [Token("a", HEADING), Token("b", REFERENCE), Token("c", OTHER)]
    pairs = align_streams(pred, gold)
    assert pairs == [
        AlignedPair(HEADING, HEADING),
        AlignedPair(None, REFERENCE),  # predictor missing -> gap on pred side
        AlignedPair(None, OTHER),
    ]
    # And symmetrically when prediction is longer.
    pairs2 = align_streams(gold, pred)
    assert pairs2 == [
        AlignedPair(HEADING, HEADING),
        AlignedPair(REFERENCE, None),
        AlignedPair(OTHER, None),
    ]


# --- Metrics: hand-built example with known expected numbers ---------------


def _approx(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=0, abs_tol=1e-9)


def test_score_alignment_known_per_category_and_averages():
    r"""Hand-built 6-token page with pre-computed expected P/R/F1.

    Aligned (predicted, gold) by reading-order index:

        i  pred       gold        heading   reference
        0  heading    heading     TP
        1  heading    heading     TP
        2  heading    other       FP
        3  other      heading     FN
        4  reference  reference              TP
        5  other      reference              FN

    heading:   TP=2 FP=1 FN=1  -> P=2/3  R=2/3  F1=2/3
    reference: TP=1 FP=0 FN=1  -> P=1.0  R=1/2  F1=2/3
    micro:     TP=3 FP=1 FN=2  -> P=3/4  R=3/5  F1=2*(.75*.6)/(1.35)=2/3
    macro:     P=(2/3+1)/2=5/6  R=(2/3+1/2)/2=7/12  F1=(2/3+2/3)/2=2/3
    """
    pred = [
        Token("H", HEADING),
        Token("H", HEADING),
        Token("H", HEADING),
        Token("x", OTHER),
        Token("R", REFERENCE),
        Token("x", OTHER),
    ]
    gold = [
        Token("H", HEADING),
        Token("H", HEADING),
        Token("x", OTHER),
        Token("H", HEADING),
        Token("R", REFERENCE),
        Token("R", REFERENCE),
    ]
    report = score_alignment(align_streams(pred, gold))

    h = report.per_category[HEADING]
    assert (h.tp, h.fp, h.fn) == (2, 1, 1)
    assert _approx(h.precision, 2 / 3)
    assert _approx(h.recall, 2 / 3)
    assert _approx(h.f1, 2 / 3)

    ref = report.per_category[REFERENCE]
    assert (ref.tp, ref.fp, ref.fn) == (1, 0, 1)
    assert _approx(ref.precision, 1.0)
    assert _approx(ref.recall, 1 / 2)
    assert _approx(ref.f1, 2 / 3)

    # Micro: pooled counts.
    assert _approx(report.micro_precision, 3 / 4)
    assert _approx(report.micro_recall, 3 / 5)
    assert _approx(report.micro_f1, 2 / 3)

    # Macro: unweighted mean of per-category P/R/F1.
    assert _approx(report.macro_precision, 5 / 6)
    assert _approx(report.macro_recall, 7 / 12)
    assert _approx(report.macro_f1, 2 / 3)


def test_score_alignment_perfect_match_is_all_ones():
    stream = [Token("H", HEADING), Token("R", REFERENCE), Token("x", OTHER)]
    report = score_alignment(align_streams(stream, stream))
    for cat in SCORED_CATEGORIES:
        cs = report.per_category[cat]
        assert (cs.precision, cs.recall, cs.f1) == (1.0, 1.0, 1.0)
    assert report.micro_f1 == 1.0
    assert report.macro_f1 == 1.0


def test_score_alignment_no_overlap_is_all_zeros():
    # Predicts everything OTHER; gold has real categories -> all FN, zero scores.
    pred = [Token("x", OTHER), Token("x", OTHER)]
    gold = [Token("H", HEADING), Token("R", REFERENCE)]
    report = score_alignment(align_streams(pred, gold))
    for cat in SCORED_CATEGORIES:
        cs = report.per_category[cat]
        assert (cs.precision, cs.recall, cs.f1) == (0.0, 0.0, 0.0)
    assert report.micro_f1 == 0.0
    assert report.macro_f1 == 0.0


def test_score_docbank_page_end_to_end_from_docbank_and_structure():
    r"""Full pipeline: parse a DocBank page + a latex_intel structure, then score.

    DocBank gold page (reading order): section 'Intro', paragraph 'body',
    reference '[1]'.
    latex_intel structure: one section 'Intro' + one citation under it.

    Predicted stream (index-aligned):
        0 heading('Intro')     vs gold heading('Intro')  -> heading TP
        1 reference(cite)      vs gold other('body')     -> heading n/a; ref FP
        2 (gap)                vs gold reference('[1]')   -> ref FN

    heading:   TP=1 FP=0 FN=0 -> P=R=F1=1.0
    reference: TP=0 FP=1 FN=1 -> P=R=F1=0.0
    """
    docbank = (
        "Intro 0 0 1 1 0 0 0 Times section\n"
        "body 0 2 1 3 0 0 0 Times paragraph\n"
        "[1] 0 4 1 5 0 0 0 Times reference\n"
    )
    gold = parse_docbank_labels(docbank)
    analysis = {
        "headings": [{"level": 2, "kind": "section", "text": "Intro"}],
        "citations": [
            {"key": "a", "marker_text": "\\cite{a}", "section_ordinal": 0}
        ],
    }
    pred = structure_to_token_stream(analysis)
    report = score_docbank_page(pred, gold)

    h = report.per_category[HEADING]
    assert (h.tp, h.fp, h.fn) == (1, 0, 0)
    assert h.f1 == 1.0

    ref = report.per_category[REFERENCE]
    assert (ref.tp, ref.fp, ref.fn) == (0, 1, 1)
    assert ref.f1 == 0.0
