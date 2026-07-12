"""DocBank token-alignment + label-mapping scoring pipeline (2c18bd07).

PAPER-EXP evaluation harness that scores ``latex_intel``'s *structural* output
against the `DocBank <https://github.com/doc-analysis/DocBank>`_ token-level
layout benchmark. It answers a narrow, honest question: **of the layout
categories ``latex_intel`` can actually produce, how well do its predicted
category labels agree with DocBank's ground-truth per-token labels, in reading
order?**

Scope of what ``latex_intel`` produces (per item 26e08530 — do NOT over-claim)
------------------------------------------------------------------------------
``latex_intel.analyze_latex`` parses a ``.tex`` source into exactly two kinds of
labelled spans:

* section **headings** (``\\section`` / ``\\subsection`` / ... — a
  *title/heading*-equivalent span), and
* in-text **citations** (``\\cite`` family — a *reference/citation*-equivalent
  span).

It does **not** detect figures, tables, displayed equations, captions, the
abstract, or author blocks — the LaTeX AST walk simply never emits those. So the
scored category space is deliberately limited to the two categories the parser
can produce (:data:`SCORED_CATEGORIES`). Everything else in a DocBank page is
mapped to :data:`OTHER` and excluded from per-category scoring (it still counts
as "not a heading / not a reference" for the aligned comparison). Claiming
figure/table/equation/caption/abstract/author F1 here would be dishonest — the
predictor structurally cannot emit them.

The category space (deliberately small + explicit)
--------------------------------------------------
Two scored categories plus a catch-all:

* ``"heading"``   — title/section-heading-equivalent.
* ``"reference"`` — citation/reference-equivalent.
* ``"other"``     — anything else (:data:`OTHER`); not scored per-category.

Both the DocBank label vocabulary (:data:`DOCBANK_LABELS`) and the mapping from
DocBank labels into this space (:data:`DOCBANK_TO_CATEGORY`) are explicit module
constants, so the evaluation is auditable and easy to extend.

DocBank per-page label-file format
----------------------------------
DocBank ships one whitespace-delimited ``.txt`` file per page. Each **line is one
token** with columns::

    token x0 y0 x1 y1 R G B font_name label

* ``token``                — the literal text token (no interior spaces),
* ``x0 y0 x1 y1``          — the token bounding box (integers, 0..1000 scaled),
* ``R G B``                — the token colour,
* ``font_name``            — the font,
* ``label``                — the DocBank structural label (last column).

Tokens appear in **reading order** (top-to-bottom, left-to-right), which is the
order we align against. :func:`parse_docbank_labels` implements this format
tolerantly: blank lines are skipped, and a line without at least
``token ... label`` (10 columns) is skipped rather than raised on.

Alignment + metrics
-------------------
Both sides are reduced to an ordered stream of category labels (one entry per
token, in reading order). :func:`align_streams` pairs them by reading-order index
(the natural, deterministic alignment when both streams describe the same page's
tokens in the same order); :func:`score_alignment` then computes per-category
precision / recall / F1 over the aligned pairs, plus micro- and macro-averages.

Everything is pure, deterministic, and unit-testable on tiny synthetic inputs —
**no DocBank download and no LaTeX install required** (running the real dataset
is a separate item). ``latex_intel`` is imported lazily inside
:func:`latex_intel_token_stream` so this module still imports (and its parser /
mapping / alignment / metric functions still run) even if ``pylatexenc`` is
absent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# --- Category space ---------------------------------------------------------

#: Title/section-heading-equivalent category.
HEADING = "heading"
#: Citation/reference-equivalent category.
REFERENCE = "reference"
#: Catch-all for every token that is neither a heading nor a reference. Present
#: in the aligned streams (so positional alignment is preserved) but never scored
#: as a category of its own.
OTHER = "other"

#: The categories we actually score P/R/F1 for. Intentionally excludes
#: :data:`OTHER` and every category ``latex_intel`` cannot produce
#: (figure/table/equation/caption/abstract/author) — see the module docstring
#: and item 26e08530.
SCORED_CATEGORIES: tuple[str, ...] = (HEADING, REFERENCE)


# --- DocBank label vocabulary + mapping ------------------------------------

#: The full DocBank structural label vocabulary (the 12 token labels DocBank's
#: per-page ``.txt`` files use). Kept explicit so an unexpected label in a real
#: file is visible as "unknown" rather than silently coerced.
DOCBANK_LABELS: frozenset[str] = frozenset(
    {
        "abstract",
        "author",
        "caption",
        "date",
        "equation",
        "figure",
        "footer",
        "list",
        "paragraph",
        "reference",
        "section",
        "table",
        "title",
    }
)

#: Explicit map from a DocBank label to our scored category space. Only the two
#: labels that correspond to something ``latex_intel`` can produce map to a
#: scored category:
#:
#: * ``section`` / ``title`` -> :data:`HEADING` (heading/title-equivalent),
#: * ``reference`` -> :data:`REFERENCE` (citation/reference-equivalent).
#:
#: Every other DocBank label maps to :data:`OTHER` — NOT because those regions
#: don't exist, but because the LaTeX predictor cannot emit them, so scoring them
#: would be meaningless. ``abstract`` and ``author`` are deliberately ``OTHER``
#: (26e08530): the parser produces neither.
DOCBANK_TO_CATEGORY: dict[str, str] = {
    "section": HEADING,
    "title": HEADING,
    "reference": REFERENCE,
    "abstract": OTHER,
    "author": OTHER,
    "caption": OTHER,
    "date": OTHER,
    "equation": OTHER,
    "figure": OTHER,
    "footer": OTHER,
    "list": OTHER,
    "paragraph": OTHER,
    "table": OTHER,
}


def map_docbank_label(label: str) -> str:
    """Map a raw DocBank ``label`` into our scored category space.

    Case-insensitive. Unknown labels (anything not in :data:`DOCBANK_LABELS`)
    map to :data:`OTHER` — a real file with an out-of-vocab label degrades to
    "other" rather than raising, honouring the never-crash contract.
    """
    return DOCBANK_TO_CATEGORY.get((label or "").strip().lower(), OTHER)


# --- Token model ------------------------------------------------------------


@dataclass(frozen=True)
class Token:
    """One reading-order token with a resolved scored-category label.

    ``text`` is the token string, ``category`` is one of :data:`HEADING`,
    :data:`REFERENCE`, or :data:`OTHER`. ``bbox`` (``(x0, y0, x1, y1)``) and
    ``raw_label`` (the original DocBank label, when the token came from a DocBank
    file) are retained for debugging/inspection but are not used by scoring.
    """

    text: str
    category: str
    bbox: tuple[int, int, int, int] | None = None
    raw_label: str | None = None


# --- DocBank label-file parsing --------------------------------------------


def parse_docbank_labels(content: str) -> list[Token]:
    """Parse a DocBank per-page label file into an ordered list of :class:`Token`.

    Expects the standard DocBank token-label line format (see the module
    docstring)::

        token x0 y0 x1 y1 R G B font_name label

    Tokens are returned in file order, which is DocBank's reading order. The
    parse is deliberately tolerant so it never raises on a slightly-off real
    file:

    * blank / whitespace-only lines are skipped;
    * a line with fewer than 10 whitespace-separated columns is skipped (it is
      not a valid token line);
    * the **first** column is the token text and the **last** column is the
      label — columns 1..4 are the bbox (parsed to ints when possible, else
      ``None``); the RGB/font columns in between are ignored;
    * an out-of-vocabulary label maps to :data:`OTHER` via
      :func:`map_docbank_label`.
    """
    tokens: list[Token] = []
    if not content or not isinstance(content, str):
        return tokens
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        cols = stripped.split()
        # Need at least: token + 4 bbox + 3 rgb + font + label = 10 columns.
        if len(cols) < 10:
            continue
        text = cols[0]
        raw_label = cols[-1]
        bbox: tuple[int, int, int, int] | None
        try:
            bbox = (int(cols[1]), int(cols[2]), int(cols[3]), int(cols[4]))
        except (ValueError, IndexError):
            bbox = None
        tokens.append(
            Token(
                text=text,
                category=map_docbank_label(raw_label),
                bbox=bbox,
                raw_label=raw_label,
            )
        )
    return tokens


# --- latex_intel -> token stream -------------------------------------------


def _tokenize_text(text: str) -> list[str]:
    """Whitespace-split a span's text into word tokens (empty -> ``[]``)."""
    return [t for t in (text or "").split() if t]


def structure_to_token_stream(analysis: dict[str, Any]) -> list[Token]:
    """Reduce a ``latex_intel.analyze_latex`` result to an ordered token stream.

    ``analysis`` is the dict returned by
    :func:`docparse.latex_intel.analyze_latex` (or the subset
    ``parse_latex_structure`` produces plus a ``citations`` list). We emit one
    :class:`Token` per **word** of each labelled span, in document order, so the
    stream is directly comparable to DocBank's per-word token stream:

    * each heading in ``analysis['headings']`` -> its title words, category
      :data:`HEADING`;
    * each citation in ``analysis['citations']`` -> its marker words (or the
      cite key when the marker is empty), category :data:`REFERENCE`.

    Ordering: headings and citations are interleaved by document order using the
    citation's ``section_ordinal`` (the document-order index of the heading that
    encloses it). A citation with ``section_ordinal == i`` is emitted
    immediately after heading ``i``; citations before any heading
    (``section_ordinal is None``) lead the stream. This yields a deterministic
    reading-order approximation from the structural output alone (the parser does
    not retain body paragraphs, so only heading + citation words appear — every
    other DocBank token has no counterpart here, which the alignment handles by
    padding).

    Pure: no LaTeX parsing happens here; it operates on an already-parsed dict.
    """
    headings = list(analysis.get("headings") or [])
    citations = list(analysis.get("citations") or [])

    # Bucket citations by the heading ordinal they follow. ``None`` -> lead.
    by_section: dict[int | None, list[dict]] = {}
    for cit in citations:
        by_section.setdefault(cit.get("section_ordinal"), []).append(cit)

    stream: list[Token] = []

    def _emit_citations(ordinal: int | None) -> None:
        for cit in by_section.get(ordinal, []):
            words = _tokenize_text(cit.get("marker_text", "")) or _tokenize_text(
                cit.get("key", "")
            )
            for w in words:
                stream.append(Token(text=w, category=REFERENCE))

    # Citations preceding the first heading.
    _emit_citations(None)
    for idx, heading in enumerate(headings):
        for w in _tokenize_text(heading.get("text", "")):
            stream.append(Token(text=w, category=HEADING))
        _emit_citations(idx)
    return stream


def latex_intel_token_stream(path_or_source: str) -> list[Token]:
    """Parse a ``.tex`` path/source via ``latex_intel`` and reduce to a stream.

    Convenience wrapper: lazily imports ``docparse.latex_intel`` (so this module
    still imports when ``pylatexenc`` is unavailable), calls ``analyze_latex``,
    and hands the result to :func:`structure_to_token_stream`. Never raises —
    if ``latex_intel`` is unavailable or errors, returns ``[]``.
    """
    try:
        from . import latex_intel  # noqa: PLC0415 — lazy optional dependency
    except Exception:  # noqa: BLE001
        return []
    try:
        analysis = latex_intel.analyze_latex(path_or_source)
    except Exception:  # noqa: BLE001 — never crash the scorer on a parse error
        return []
    return structure_to_token_stream(analysis)


# --- Alignment --------------------------------------------------------------


@dataclass(frozen=True)
class AlignedPair:
    """One aligned ``(predicted_category, gold_category)`` position.

    A ``None`` on either side is a *gap* — a position present in one stream but
    padded on the other (streams of unequal length align up to the longer one).
    """

    predicted: str | None
    gold: str | None


def align_streams(
    predicted: Iterable[Token], gold: Iterable[Token]
) -> list[AlignedPair]:
    """Align two reading-order token streams by positional (index) alignment.

    Both DocBank and the ``latex_intel`` stream list tokens in reading order, so
    the natural, deterministic alignment is index-for-index: position ``i`` of
    the prediction is compared to position ``i`` of the gold. When the streams
    differ in length (they usually do — the predictor omits body text), the
    shorter stream is padded with ``None`` gaps up to the longer length, so every
    token on both sides participates in exactly one pair.

    Returns a list of :class:`AlignedPair` carrying the *category* on each side
    (``None`` for a gap). This positional scheme is the reading-order-index
    alignment called for by the item; :func:`score_alignment` consumes it.
    """
    pred = list(predicted)
    au = list(gold)
    n = max(len(pred), len(au))
    pairs: list[AlignedPair] = []
    for i in range(n):
        p = pred[i].category if i < len(pred) else None
        g = au[i].category if i < len(au) else None
        pairs.append(AlignedPair(predicted=p, gold=g))
    return pairs


# --- Metrics ----------------------------------------------------------------


@dataclass
class CategoryScore:
    """Precision / recall / F1 (+ raw counts) for one category."""

    category: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0


@dataclass
class ScoreReport:
    """Full scoring result: per-category scores plus micro/macro averages."""

    per_category: dict[str, CategoryScore] = field(default_factory=dict)
    micro_precision: float = 0.0
    micro_recall: float = 0.0
    micro_f1: float = 0.0
    macro_precision: float = 0.0
    macro_recall: float = 0.0
    macro_f1: float = 0.0


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    """Precision, recall, F1 from raw counts (0.0 when denominators are 0)."""
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall)
        else 0.0
    )
    return precision, recall, f1


def score_alignment(
    pairs: Iterable[AlignedPair],
    categories: Iterable[str] = SCORED_CATEGORIES,
) -> ScoreReport:
    """Compute per-category + micro/macro P/R/F1 over aligned category pairs.

    For each scored ``category`` (default :data:`SCORED_CATEGORIES`), a token is:

    * a **true positive** when both predicted and gold equal that category;
    * a **false positive** when predicted equals it but gold does not (including
      when gold is :data:`OTHER` or a gap);
    * a **false negative** when gold equals it but predicted does not (including
      when predicted is :data:`OTHER` or a gap).

    :data:`OTHER` and gaps (``None``) are never a category's true positive — they
    only ever contribute to another category's FP/FN, which is exactly right:
    predicting ``heading`` where the gold is body text is a false positive, and a
    gold ``reference`` the predictor missed is a false negative.

    * **Micro** average pools TP/FP/FN across all scored categories, then
      computes one P/R/F1 (dominated by frequent categories).
    * **Macro** average is the unweighted mean of the per-category P/R/F1
      (treats every category equally, regardless of support).

    Returns a :class:`ScoreReport`. Deterministic and side-effect-free.
    """
    cats = list(categories)
    pair_list = list(pairs)
    report = ScoreReport()
    tot_tp = tot_fp = tot_fn = 0
    macro_p = macro_r = macro_f = 0.0

    for cat in cats:
        tp = fp = fn = 0
        for pair in pair_list:
            p_is = pair.predicted == cat
            g_is = pair.gold == cat
            if p_is and g_is:
                tp += 1
            elif p_is and not g_is:
                fp += 1
            elif g_is and not p_is:
                fn += 1
        precision, recall, f1 = _prf(tp, fp, fn)
        report.per_category[cat] = CategoryScore(
            category=cat,
            tp=tp,
            fp=fp,
            fn=fn,
            precision=precision,
            recall=recall,
            f1=f1,
        )
        tot_tp += tp
        tot_fp += fp
        tot_fn += fn
        macro_p += precision
        macro_r += recall
        macro_f += f1

    micro_p, micro_r, micro_f = _prf(tot_tp, tot_fp, tot_fn)
    report.micro_precision, report.micro_recall, report.micro_f1 = (
        micro_p,
        micro_r,
        micro_f,
    )
    denom = len(cats) or 1
    report.macro_precision = macro_p / denom
    report.macro_recall = macro_r / denom
    report.macro_f1 = macro_f / denom
    return report


def score_docbank_page(
    predicted: Iterable[Token],
    gold: Iterable[Token],
    categories: Iterable[str] = SCORED_CATEGORIES,
) -> ScoreReport:
    """End-to-end: align a predicted + gold token stream, then score.

    Thin convenience over :func:`align_streams` + :func:`score_alignment` for the
    common one-page case.
    """
    return score_alignment(align_streams(predicted, gold), categories=categories)
