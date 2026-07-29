"""Deterministic figure/table-vs-safe-category classifier (5fd9d2fd, b7308039
follow-up, 665 chain).

Sprint items that touch a ``.docx`` deliverable span a wide range of actual
risk: replacing or creating a figure/table is genuinely artifact-sensitive
(the wrong file, or no file at all, silently corrupts the deliverable), while
a caption fix, an equation edit, a tweak to an already-embedded drawing, a
plain paragraph/section rewrite, or a code-only verification pass carries no
such risk — none of those produce a NEW external artifact a pointer could
even meaningfully name. Treating all of them alike either over-blocks the
safe majority (forcing a `planned_output` pointer on a caption rename) or
under-protects the risky minority (letting a figure swap through with no
evidence at all). This module is the single, deterministic answer to "which
bucket is this item in, and how sure are we."

Two-tier decision, in this order:

1. **Declared ``artifact_kind`` (2f9cb288) is authoritative when present.**
   Read via :func:`meridian.artifact_declaration.effective_artifact_kind` —
   the ONE clean access path that module already established. A human (or a
   prospecting step) who explicitly set ``artifact_kind="document_only"``
   on an item whose *title* reads like a figure task is making a deliberate
   override; this module never second-guesses that declaration by re-running
   the heuristics below. This is also what makes the classification
   deterministic and explainable across repeated calls: the same declared
   field always wins the same way.
2. **Conservative title/notes/pointer evidence, fallback only.** Legacy
   items (added before 2f9cb288, or that simply never declared a kind) get
   scored against a fixed set of keyword/pointer signals — see
   :func:`_classify_from_fallback_evidence`. "Conservative" means the
   fallback only fires on an actual textual or pointer signal; it never
   invents a classification for an item that gives it nothing to go on
   (that degrades to ``"ambiguous"`` with ``confidence="low"``, never to a
   guessed specific kind).

Bias in the fallback, by design:

* A figure/table CREATION/REPLACEMENT signal (a verb like insert/create/
  replace next to a figure/chart/table noun) — or a concrete pointer at a
  figure/table-typed file — classifies as artifact-sensitive with HIGH
  confidence.
* A bare, INDIRECT figure/table mention (the noun alone, no creation verb
  nearby) still classifies as artifact-sensitive (never silently dropped —
  "a figure/table item must be treated as artifact-sensitive even when
  wording is indirect") but at MEDIUM confidence and flagged ``ambiguous``,
  so a human/executor knows to double-check rather than trust it blindly.
* Caption/renumber-only, equation-only, and embedded-DOCX-drawing signals
  are recognized as their OWN safe categories and are explicitly NOT
  artifact-sensitive — an item classified into one of these must never be
  forced to provide an external artifact pointer (see
  ``artifact_declaration``'s ``require_exact_figure_output_pointer`` /
  ``require_exact_table_output_pointer`` policy flags, which this
  classification is what a caller should gate before applying).
* Pointer evidence never trusts a bare ``.docx`` path, a directory-looking
  uri, or a generic scheme-prefixed resource id (``mcp_tool:...``,
  ``db:...``, ``route:...``, …) as an "exact" figure/table output — only a
  concrete image/tabular-data file extension counts. Mirrors
  ``artifact_declaration``'s own "no inference from a directory or a
  generic mcp_tool: resource id" rule verbatim (see
  ``meridian.mcp_tools._PLANNED_OUTPUT_SCHEMA``'s description).

Pure, synchronous, no DB/network/model call — safe to call from both
``meridian.handoff.build_item_briefing`` (per-item explainability) and a
readiness-level aggregate (:func:`summarize_artifact_classifications`, used
by ``handoff._build_artifact_readiness_warnings``). Never raises: a
malformed/missing field degrades to the least-informative branch rather than
blowing up a mandatory handoff path.
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import artifact_declaration as _artifact_declaration

CLASSIFICATION_SCHEMA_VERSION = 1

# The two artifact-sensitive kinds — mirrors artifact_declaration.ARTIFACT_KINDS'
# "figure"/"table" (its third value, "document_only", is NOT sensitive).
FIGURE_TABLE_KINDS = frozenset({"figure", "table"})

# The "safe" categories this classifier can resolve a LEGACY item into via the
# fallback path — none of these are artifact-sensitive; none require an
# external artifact pointer.
NON_SENSITIVE_KINDS = frozenset({
    "document_only",
    "paragraph_only",
    "caption_only",
    "equation_only",
    "embedded_docx_drawing",
    "code_only",
})

AMBIGUOUS = "ambiguous"

ALL_CLASSIFICATIONS = FIGURE_TABLE_KINDS | NON_SENSITIVE_KINDS | {AMBIGUOUS}


# ---------------------------------------------------------------------------
# Fallback text signals — pure regex, word-boundary, case-insensitive.
# ---------------------------------------------------------------------------

_TOC_RE = re.compile(r"\btables?\s+of\s+contents\b", re.I)

_FIGURE_NOUNS = (
    r"(?:figures?|charts?|graphs?|plots?|diagrams?|images?|screenshots?|"
    r"illustrations?|photos?|pictures?|visuali[sz]ations?|infographics?)"
)
_TABLE_NOUN = r"tables?"

_CREATE_VERBS = (
    r"(?:insert(?:ing)?|add(?:ing)?|creat(?:e|ing)|generat(?:e|ing)|"
    r"produc(?:e|ing)|regenerat(?:e|ing)|replac(?:e|ing)|redraw(?:ing)?|"
    r"embed(?:ding)?|updat(?:e|ing)|swap(?:ping)?|rebuild(?:ing)?|"
    r"re-?generat(?:e|ing))"
)

#  Short, tight proximity window: "insert a new ablation chart" is 3 words /
#  ~24 chars of gap and must match; "add unit tests to verify the docx table
#  writer" is ~35 chars of gap between "add" and "table" and must NOT — a
#  verification-flavored sentence that merely mentions "table" in passing is
#  code-only work, not a table-creation directive. Kept intentionally SHORT
#  (conservative) rather than tuned per corpus; _first_match_excluding below
#  is the second line of defense for any gap that still crosses into
#  verification/prose vocabulary.
_GAP = r"[\w\s,'\"-]{0,24}?"

_FIGURE_CREATE_RE = re.compile(
    rf"\b{_CREATE_VERBS}\b{_GAP}\b{_FIGURE_NOUNS}\b", re.I
)
_FIGURE_CREATE_REV_RE = re.compile(
    rf"\b{_FIGURE_NOUNS}\b{_GAP}\b{_CREATE_VERBS}\b", re.I
)
_TABLE_CREATE_RE = re.compile(
    rf"\b{_CREATE_VERBS}\b{_GAP}\b{_TABLE_NOUN}\b", re.I
)
_TABLE_CREATE_REV_RE = re.compile(
    rf"\b{_TABLE_NOUN}\b{_GAP}\b{_CREATE_VERBS}\b", re.I
)

_FIGURE_WORD_RE = re.compile(rf"\b{_FIGURE_NOUNS}\b", re.I)
_TABLE_WORD_RE = re.compile(rf"\b{_TABLE_NOUN}\b", re.I)

_CAPTION_RE = re.compile(
    r"\b(?:captions?|re-?number(?:ing|ed)?|recaption(?:ing)?)\b", re.I
)
_EQUATION_RE = re.compile(
    r"\b(?:equations?|formulas?|latex|katex|mathml)\b", re.I
)
_EMBEDDED_DRAWING_RE = re.compile(
    r"\b(?:embedded[\s-]+(?:docx[\s-]+)?drawings?|drawingml|"
    r"docx[\s-]+drawings?|native[\s-]+drawings?|embedded[\s-]+shapes?)\b",
    re.I,
)
_CODE_ONLY_RE = re.compile(
    r"\b(?:verify|verification|validate|validation|lint(?:ing)?|"
    r"type[\s-]?check(?:ing)?|unit[\s-]?tests?(?:ing)?|"
    r"regression[\s-]?tests?|code[\s-]?review|refactor(?:ing)?|"
    r"smoke[\s-]?tests?)\b",
    re.I,
)
_PARAGRAPH_RE = re.compile(
    r"\b(?:paragraphs?|sections?|wording|prose|narrative|"
    r"copy[\s-]?edit(?:ing)?|rewrit(?:e|ing)|rewording|text\s+edit(?:s|ing)?)\b",
    re.I,
)
_DOCUMENT_ONLY_EXPLICIT_RE = re.compile(r"\bdocument[\s-]only\b", re.I)


def _first_match(regex: "re.Pattern[str]", text: str) -> "str | None":
    m = regex.search(text)
    return m.group(0).strip() if m else None


def _first_match_excluding(
    regex: "re.Pattern[str]",
    exclude: "tuple[re.Pattern[str], ...]",
    text: str,
) -> "str | None":
    """Like :func:`_first_match`, but skips any match whose own matched span
    ALSO contains one of ``exclude``'s vocabulary.

    Second line of defense (on top of ``_GAP``'s short window) against a
    creation-verb match spuriously spanning into an unrelated clause — e.g.
    "add unit tests to verify the docx table writer" contains "add" and
    "table" close enough together to tempt a naive verb-noun match, but the
    matched text itself contains "verify"/"unit test" (a _CODE_ONLY_RE
    vocabulary hit), so this is code-only verification work, not a
    table-creation directive, and the match is discarded.
    """
    for m in regex.finditer(text):
        span = m.group(0)
        if any(ex.search(span) for ex in exclude):
            continue
        return span.strip()
    return None


# ---------------------------------------------------------------------------
# Pointer evidence — never trusts a bare .docx path, a directory, or a
# generic scheme-prefixed resource id as an "exact" figure/table output.
# ---------------------------------------------------------------------------

_FIGURE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".tif", ".tiff",
    ".eps", ".bmp",
})
_TABLE_EXTENSIONS = frozenset({".csv", ".tsv", ".xlsx", ".xls"})

# A generic scheme prefix (mcp_tool:, db:, route:, pypi:, github:, zotero:,
# finding:, doc:, …) is never treated as a local file uri. Two-or-more-letter
# prefixes only, so a Windows drive letter ("C:\...") is never misread as one.
_GENERIC_SCHEME_RE = re.compile(r"^[a-zA-Z_]{2,}:(?!//)")


def _classify_uri(uri: Any) -> "str | None":
    """Return ``"figure"``/``"table"``/``None`` for ONE candidate uri.

    ``None`` covers every case that is deliberately NOT trusted as an exact
    figure/table output: a web/scheme URL, a generic ``prefix:`` resource id
    (``mcp_tool:``/``db:``/``route:``/…), a directory-looking path (no
    filename extension), and — explicitly — a bare ``.docx`` path. A docx
    file may CONTAIN a figure or table, but pointing at the whole document is
    not evidence of a specific asset.
    """
    if not isinstance(uri, str):
        return None
    u = uri.strip()
    if not u or "://" in u:
        return None
    if _GENERIC_SCHEME_RE.match(u):
        return None
    normalized = u.replace("\\", "/").rstrip("/")
    if not normalized:
        return None
    fname = normalized.rsplit("/", 1)[-1]
    if "." not in fname:
        return None
    ext = "." + fname.rsplit(".", 1)[-1].lower()
    if ext in _FIGURE_EXTENSIONS:
        return "figure"
    if ext in _TABLE_EXTENSIONS:
        return "table"
    return None


def _iter_candidate_uris(
    item: dict[str, Any],
) -> "list[tuple[str, str, str | None]]":
    """Pull every pointer-shaped uri a legacy item might carry, tagged with
    which field it came from (for explainable evidence strings) and, when
    available, the durable ``sprint_item_pointers`` row id it came from.

    Sources, in order: ``planned_output`` (rare on a legacy item, but
    checked for completeness — see module docstring), ``pointer_records``
    (the typed records ``handoff._annotate_resolved_pointers`` attaches from
    the item's durable ``sprint_item_pointers`` rows), and ``file:``-typed
    ``touches_resources`` identifiers. Never raises on malformed input.

    The third tuple element (``pointer_id``, 88f82c15/b730 follow-up) is the
    stored pointer's own ``id`` when the candidate came from a
    ``pointer_records`` entry (the only source with a persisted row to name
    in a remediation message) — ``None`` for ``planned_output``/
    ``touches_resources`` candidates, which have no durable row of their own.
    """
    out: "list[tuple[str, str, str | None]]" = []

    try:
        planned = _artifact_declaration.effective_planned_output(item)
    except Exception:  # noqa: BLE001 — never let a bad field break scanning
        planned = None
    if isinstance(planned, dict):
        for t in planned.get("targets") or []:
            if isinstance(t, dict) and t.get("uri"):
                out.append((str(t["uri"]), "planned_output", None))

    for rec in item.get("pointer_records") or []:
        if not isinstance(rec, dict):
            continue
        rec_id = rec.get("id")
        pointer_id = str(rec_id) if rec_id else None
        for t in rec.get("targets") or []:
            if isinstance(t, dict) and t.get("uri"):
                out.append((str(t["uri"]), "pointer_records", pointer_id))

    raw_resources = item.get("touches_resources")
    ids: list[Any] = []
    if isinstance(raw_resources, str) and raw_resources.strip():
        try:
            parsed = json.loads(raw_resources)
            ids = parsed if isinstance(parsed, list) else [raw_resources]
        except (TypeError, ValueError):
            ids = [raw_resources]
    elif isinstance(raw_resources, list):
        ids = raw_resources
    for ident in ids:
        s = str(ident).strip()
        if not s:
            continue
        if s.startswith("inferred:"):
            s = s[len("inferred:"):]
        head, sep, tail = s.partition(":")
        if sep and head == "file" and tail:
            out.append((tail, "touches_resources", None))

    return out


def _pointer_evidence(item: dict[str, Any]) -> "tuple[str | None, list[str], bool]":
    """Scan every candidate pointer uri; return
    ``(kind_or_none, evidence_lines, mixed)``.

    ``mixed`` is True when BOTH figure- and table-typed evidence were found
    (e.g. an item touching two outputs) — the caller still gets a kind
    (figure wins the tie, documented) but is told to treat it as ambiguous.
    """
    figure_hits: list[str] = []
    table_hits: list[str] = []
    for uri, source, _pointer_id in _iter_candidate_uris(item):
        kind = _classify_uri(uri)
        if kind == "figure":
            figure_hits.append(f"{source} target {uri!r} resolves to a figure-typed file")
        elif kind == "table":
            table_hits.append(f"{source} target {uri!r} resolves to a table-typed file")
    if figure_hits and table_hits:
        return "figure", figure_hits + table_hits, True
    if figure_hits:
        return "figure", figure_hits, False
    if table_hits:
        return "table", table_hits, False
    return None, [], False


# ---------------------------------------------------------------------------
# 88f82c15 (b730 follow-up) — WHY a candidate pointer is insufficient.
#
# ``_classify_uri`` above collapses every non-exact case to a bare ``None``
# ("not evidence"), which is all ``_pointer_evidence``/``classify_artifact_work``
# need. The warn/strict POLICY evaluator (:mod:`meridian.pointers`'s
# ``evaluate_artifact_pointer_policy``) needs more: which SPECIFIC exclusion
# fired, so a structured warning can name the deficiency (a bare .docx vs. a
# directory vs. a generic meridian-outputs/Outputs tool reference) instead of
# a generic "no pointer found". This section mirrors ``_classify_uri``'s
# branches verbatim, in the same order, so the two can never silently
# disagree about what counts as sufficient.
# ---------------------------------------------------------------------------

INSUFFICIENT_MISSING_POINTER = "missing_pointer"
INSUFFICIENT_BARE_DOCX = "insufficient_pointer_bare_docx"
INSUFFICIENT_DIRECTORY = "insufficient_pointer_directory"
INSUFFICIENT_GENERIC_REFERENCE = "insufficient_pointer_generic_reference"
INSUFFICIENT_UNSUPPORTED_TYPE = "insufficient_pointer_unsupported_type"

# Priority order when a single item carries several insufficient candidates
# for different reasons — the MOST actionable/specific one wins, so a caller
# renders one clear remediation instead of an ambiguous list.
_INSUFFICIENCY_PRIORITY = (
    INSUFFICIENT_BARE_DOCX,
    INSUFFICIENT_DIRECTORY,
    INSUFFICIENT_GENERIC_REFERENCE,
    INSUFFICIENT_UNSUPPORTED_TYPE,
)

_INSUFFICIENCY_REMEDIATION: dict[str, str] = {
    INSUFFICIENT_MISSING_POINTER: (
        "Attach a planned_output or sprint_item_pointer target whose uri "
        "resolves to a concrete figure file (.png/.jpg/.jpeg/.gif/.svg/"
        ".webp/.tif/.tiff/.eps/.bmp) or table file (.csv/.tsv/.xlsx/.xls) — "
        "figure/table work needs an exact output pointer, not just a title/"
        "notes description."
    ),
    INSUFFICIENT_BARE_DOCX: (
        "Point at the SPECIFIC figure/table file this item produces (e.g. "
        "outputs/figures/foo.png), not the bare .docx document — a .docx "
        "may CONTAIN the figure/table but does not identify which asset it "
        "is."
    ),
    INSUFFICIENT_DIRECTORY: (
        "Point at the specific output FILE this item produces, not the "
        "containing directory."
    ),
    INSUFFICIENT_GENERIC_REFERENCE: (
        "A generic meridian-outputs/Outputs tool reference (mcp_tool:/"
        "route:/db:/… or another scheme-prefixed resource id) does not name "
        "a specific file — attach a pointer at the concrete figure/table "
        "file uri instead."
    ),
    INSUFFICIENT_UNSUPPORTED_TYPE: (
        "The pointer's file extension is not a recognized figure "
        "(.png/.jpg/.jpeg/.gif/.svg/.webp/.tif/.tiff/.eps/.bmp) or table "
        "(.csv/.tsv/.xlsx/.xls) output type — attach a pointer at a "
        "concrete figure/table file."
    ),
}


def _classify_uri_insufficiency(uri: Any) -> "str | None":
    """Like :func:`_classify_uri`, but names the SPECIFIC reason a candidate
    uri fails to count as exact figure/table evidence, instead of a bare
    ``None``.

    Returns ``None`` when the uri is actually SUFFICIENT (i.e. when
    :func:`_classify_uri` would return ``"figure"``/``"table"``) — every
    exclusion branch below mirrors ``_classify_uri`` verbatim, in the same
    order, so the two functions can never silently disagree about what
    counts.
    """
    if not isinstance(uri, str):
        return None
    u = uri.strip()
    if not u:
        return None
    if "://" in u:
        return INSUFFICIENT_UNSUPPORTED_TYPE
    if _GENERIC_SCHEME_RE.match(u):
        return INSUFFICIENT_GENERIC_REFERENCE
    normalized = u.replace("\\", "/").rstrip("/")
    if not normalized:
        return INSUFFICIENT_DIRECTORY
    fname = normalized.rsplit("/", 1)[-1]
    if "." not in fname:
        return INSUFFICIENT_DIRECTORY
    ext = "." + fname.rsplit(".", 1)[-1].lower()
    if ext == ".docx":
        return INSUFFICIENT_BARE_DOCX
    if ext in _FIGURE_EXTENSIONS or ext in _TABLE_EXTENSIONS:
        return None  # sufficient — _classify_uri would resolve this one
    return INSUFFICIENT_UNSUPPORTED_TYPE


def artifact_pointer_insufficiency_evidence(
    item: dict[str, Any],
) -> "tuple[str | None, list[str]]":
    """For a figure/table-sensitive item with NO concrete pointer evidence
    (:func:`_pointer_evidence` returned a ``None`` kind), determine the
    DOMINANT insufficiency reason and the durable pointer ids it implicates.

    Returns ``(reason_code, affected_pointer_ids)``. ``reason_code`` is
    ``None`` only when the item has ZERO candidate pointer uris at all (no
    ``planned_output``, no ``pointer_records``, no ``file:``-typed
    ``touches_resources``) — the caller substitutes
    :data:`INSUFFICIENT_MISSING_POINTER` in that case, distinguishing "there
    is a pointer but it's the wrong shape" from "there is no pointer at
    all". When multiple candidates fail for different reasons, the most
    actionable one wins by fixed priority (bare docx > directory > generic
    reference > unsupported type — see :data:`_INSUFFICIENCY_PRIORITY`) so a
    caller renders ONE clear remediation instead of an ambiguous list.

    ``affected_pointer_ids`` names the durable ``sprint_item_pointers`` row
    ids (from ``pointer_records``) whose target triggered the DOMINANT
    reason, sorted for determinism. A candidate sourced from
    ``touches_resources``/``planned_output`` carries no durable row id and
    is never included — there is no persisted pointer to name.

    Never raises: reused by :func:`meridian.pointers.evaluate_artifact_pointer_policy`,
    a mandatory handoff-annotation path.
    """
    candidates = _iter_candidate_uris(item)
    if not candidates:
        return None, []
    reasons_by_code: dict[str, list[str]] = {}
    for uri, _source, pointer_id in candidates:
        code = _classify_uri_insufficiency(uri)
        if not code:
            continue  # this ONE candidate is actually sufficient
        bucket = reasons_by_code.setdefault(code, [])
        if pointer_id:
            bucket.append(str(pointer_id))
    if not reasons_by_code:
        return None, []
    for code in _INSUFFICIENCY_PRIORITY:
        if code in reasons_by_code:
            return code, sorted(set(reasons_by_code[code]))
    # Unreachable in practice (every _classify_uri_insufficiency code is
    # listed in _INSUFFICIENCY_PRIORITY) — fall back rather than silently
    # dropping evidence.
    first_code = next(iter(reasons_by_code))
    return first_code, sorted(set(reasons_by_code[first_code]))


# ---------------------------------------------------------------------------
# Main entry point.
# ---------------------------------------------------------------------------

def classify_artifact_work(item: dict[str, Any]) -> dict[str, Any]:
    """Classify ONE sprint item's artifact work.

    Returns::

        {
            "classification": one of ALL_CLASSIFICATIONS,
            "is_artifact_sensitive": bool,   # True only for "figure"/"table"
            "confidence": "high" | "medium" | "low",
            "ambiguous": bool,               # conflicting/weak signals fired
            "rule": str,                     # which rule produced the result
            "evidence": [str, ...],          # explainable matched signals
        }

    Never raises: a malformed ``item`` (not a dict, missing fields) degrades
    to the ``"ambiguous"`` / ``"no_signal_ambiguous"`` result rather than
    throwing, since this must be safe to call from a mandatory handoff path.
    """
    if not isinstance(item, dict):
        item = {}

    try:
        declared_kind = _artifact_declaration.effective_artifact_kind(item)
    except Exception:  # noqa: BLE001
        declared_kind = None

    if declared_kind is not None:
        sensitive = declared_kind in FIGURE_TABLE_KINDS
        return {
            "classification": declared_kind,
            "is_artifact_sensitive": sensitive,
            "confidence": "high",
            "ambiguous": False,
            "rule": "declared_artifact_kind",
            "evidence": [
                f"declared artifact_kind={declared_kind!r} (authoritative, "
                "overrides any title/notes wording)"
            ],
        }

    try:
        return _classify_from_fallback_evidence(item)
    except Exception:  # noqa: BLE001 — a heuristic bug must never break a handoff
        return {
            "classification": AMBIGUOUS,
            "is_artifact_sensitive": False,
            "confidence": "low",
            "ambiguous": True,
            "rule": "fallback_error",
            "evidence": ["fallback classification raised — treated as unknown"],
        }


def _classify_from_fallback_evidence(item: dict[str, Any]) -> dict[str, Any]:
    """The 2. legacy-item fallback path — see module docstring for the
    ordered rule list and the bias rationale for each branch."""
    title = str(item.get("title") or "")
    notes = str(item.get("notes") or "")
    haystack = f"{title}\n{notes}"
    haystack_clean = _TOC_RE.sub(" ", haystack)  # "table of contents" is never table evidence

    evidence: list[str] = []

    _exclude = (_CODE_ONLY_RE, _PARAGRAPH_RE)
    figure_create_hit = _first_match_excluding(
        _FIGURE_CREATE_RE, _exclude, haystack
    ) or _first_match_excluding(_FIGURE_CREATE_REV_RE, _exclude, haystack)
    table_create_hit = _first_match_excluding(
        _TABLE_CREATE_RE, _exclude, haystack_clean
    ) or _first_match_excluding(_TABLE_CREATE_REV_RE, _exclude, haystack_clean)
    figure_word_hit = _first_match(_FIGURE_WORD_RE, haystack)
    table_word_hit = _first_match(_TABLE_WORD_RE, haystack_clean)
    caption_hit = _first_match(_CAPTION_RE, haystack)
    equation_hit = _first_match(_EQUATION_RE, haystack)
    drawing_hit = _first_match(_EMBEDDED_DRAWING_RE, haystack)
    code_only_hit = _first_match(_CODE_ONLY_RE, haystack)
    paragraph_hit = _first_match(_PARAGRAPH_RE, haystack)
    doc_only_hit = _first_match(_DOCUMENT_ONLY_EXPLICIT_RE, haystack)

    pointer_kind, pointer_hits, pointer_mixed = _pointer_evidence(item)
    evidence.extend(pointer_hits)

    # 1. Strong figure/table creation/replacement signal (title/notes verb +
    #    noun, or a concrete pointer at a figure/table-typed file) — highest
    #    priority, high confidence, always artifact-sensitive.
    if figure_create_hit or table_create_hit or pointer_kind:
        if pointer_kind and not (figure_create_hit or table_create_hit):
            kind = pointer_kind
            rule = "pointer_evidence_mixed" if pointer_mixed else f"pointer_evidence_{kind}"
        else:
            kind = "table" if table_create_hit and not figure_create_hit else "figure"
            rule = f"title_notes_strong_{kind}"
            if figure_create_hit:
                evidence.append(f"title/notes creation phrase: {figure_create_hit!r}")
            if table_create_hit:
                evidence.append(f"title/notes creation phrase: {table_create_hit!r}")
        return {
            "classification": kind,
            "is_artifact_sensitive": True,
            "confidence": "high",
            "ambiguous": bool(caption_hit or equation_hit or pointer_mixed),
            "rule": rule,
            "evidence": evidence or [f"matched a {kind} signal"],
        }

    # 2. Caption/renumber-only — never forced to carry an artifact pointer.
    #    Checked BEFORE the weak/indirect figure-word branch below: a phrase
    #    like "renumber figure captions" contains the bare word "figure" but
    #    is caption work, not figure work — the more specific caption signal
    #    must win over the generic noun match, or every caption item would
    #    misclassify as an indirect figure item just because it mentions
    #    which figure the caption belongs to.
    if caption_hit:
        evidence.append(f"caption/renumber phrase: {caption_hit!r}")
        return {
            "classification": "caption_only",
            "is_artifact_sensitive": False,
            "confidence": "high" if not (equation_hit or drawing_hit) else "medium",
            "ambiguous": bool(equation_hit or drawing_hit),
            "rule": "title_notes_caption_only",
            "evidence": evidence,
        }

    # 3. Equation-only — same treatment, same reasoning as caption-only.
    if equation_hit:
        evidence.append(f"equation/formula phrase: {equation_hit!r}")
        return {
            "classification": "equation_only",
            "is_artifact_sensitive": False,
            "confidence": "high" if not drawing_hit else "medium",
            "ambiguous": bool(drawing_hit),
            "rule": "title_notes_equation_only",
            "evidence": evidence,
        }

    # 4. Embedded-DOCX-drawing edit — modifies an ALREADY-embedded drawing
    #    object in place; produces no new external artifact to point at.
    if drawing_hit:
        evidence.append(f"embedded-drawing phrase: {drawing_hit!r}")
        return {
            "classification": "embedded_docx_drawing",
            "is_artifact_sensitive": False,
            "confidence": "high",
            "ambiguous": False,
            "rule": "title_notes_embedded_docx_drawing",
            "evidence": evidence,
        }

    # 5. Code-only verification work — checked BEFORE the weak/indirect
    #    figure-word branch below for the same reason caption/equation/
    #    drawing are checked before it: a specific, explicit signal
    #    (verify/validate/lint/unit test/…) always outranks a bare noun
    #    mention. "Add unit tests to verify the docx table writer" contains
    #    the word "table" but is a verification task, not table-creation
    #    work — the _GAP proximity window + _first_match_excluding above
    #    already keep this out of the STRONG creation-verb branch (1); this
    #    check keeps it out of the WEAK indirect-mention branch (6) too. A
    #    paragraph/text-edit signal takes priority over code_only itself —
    #    a prose rewrite that happens to say "review the wording" is not
    #    code verification work.
    if code_only_hit and not paragraph_hit:
        evidence.append(f"verification/code phrase: {code_only_hit!r}")
        return {
            "classification": "code_only",
            "is_artifact_sensitive": False,
            "confidence": "high",
            "ambiguous": False,
            "rule": "title_notes_code_only",
            "evidence": evidence,
        }

    # 6. Indirect/weak figure/table wording — the noun alone, no creation
    #    verb nearby, and not already explained by a caption/equation/
    #    drawing/code-only signal above. Still artifact-sensitive (never
    #    silently dropped — "a figure/table item must be treated as
    #    artifact-sensitive even when wording is indirect") but medium
    #    confidence and explicitly flagged ambiguous for human review.
    if figure_word_hit or table_word_hit:
        kind = "table" if table_word_hit and not figure_word_hit else "figure"
        evidence.append(
            f"indirect title/notes mention: {(figure_word_hit or table_word_hit)!r}"
        )
        return {
            "classification": kind,
            "is_artifact_sensitive": True,
            "confidence": "medium",
            "ambiguous": True,
            "rule": f"title_notes_weak_{kind}",
            "evidence": evidence,
        }

    # 7. Generic document text edit — paragraph/section wording, or an
    #    explicit "document-only" phrase in the title/notes prose itself
    #    (distinct from the declared artifact_kind field, which is handled
    #    earlier in classify_artifact_work and never reaches this branch).
    if paragraph_hit or doc_only_hit:
        label = "document_only" if doc_only_hit and not paragraph_hit else "paragraph_only"
        if doc_only_hit:
            evidence.append(f"explicit phrase: {doc_only_hit!r}")
        if paragraph_hit:
            evidence.append(f"document-text phrase: {paragraph_hit!r}")
        return {
            "classification": label,
            "is_artifact_sensitive": False,
            "confidence": "medium",
            "ambiguous": False,
            "rule": f"title_notes_{label}",
            "evidence": evidence,
        }

    # 8. Nothing recognizable at all — unknown, never silently defaulted to
    #    a specific kind or to artifact-sensitive.
    return {
        "classification": AMBIGUOUS,
        "is_artifact_sensitive": False,
        "confidence": "low",
        "ambiguous": True,
        "rule": "no_signal_ambiguous",
        "evidence": [
            "no declared artifact_kind and no recognizable title/notes/"
            "pointer signal"
        ],
    }


# ---------------------------------------------------------------------------
# Batch aggregate — for handoff-readiness-level reporting.
# ---------------------------------------------------------------------------

def summarize_artifact_classifications(items: "list[dict[str, Any]] | None") -> dict[str, Any]:
    """Aggregate :func:`classify_artifact_work` over a batch of pending
    sprint items, for readiness-level reporting (see
    ``handoff._build_artifact_readiness_warnings``).

    Returns::

        {
            "counts": {classification: count, ...},
            "sensitive_without_pointer": [item_id, ...],
            "ambiguous_items": [item_id, ...],
        }

    ``sensitive_without_pointer`` lists figure/table-classified items that
    have no CONCRETE figure/table-typed pointer evidence yet (see
    :func:`_pointer_evidence` — the same "never trust a bare docx/directory/
    generic mcp_tool: id" guard the classifier itself applies) AND whose
    effective policy (``artifact_declaration.effective_artifact_policy``)
    has NOT turned checking off — an item the human explicitly set
    ``artifact_pointer_check="off"`` on is intentionally excluded from this
    warning. Never raises: a single item's classification/policy failure is
    skipped, not fatal to the batch.
    """
    counts: dict[str, int] = {}
    sensitive_without_pointer: list[str] = []
    ambiguous_items: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        iid = item.get("id")
        try:
            result = classify_artifact_work(item)
        except Exception:  # noqa: BLE001
            continue
        cls = result.get("classification")
        if isinstance(cls, str):
            counts[cls] = counts.get(cls, 0) + 1
        if result.get("ambiguous") and iid:
            ambiguous_items.append(str(iid))
        if result.get("is_artifact_sensitive") and iid:
            try:
                policy = _artifact_declaration.effective_artifact_policy(item)
            except Exception:  # noqa: BLE001
                policy = _artifact_declaration.default_artifact_policy()
            if policy.get("artifact_pointer_check") != "off":
                pointer_kind, _hits, _mixed = _pointer_evidence(item)
                if pointer_kind is None:
                    sensitive_without_pointer.append(str(iid))
    return {
        "counts": counts,
        "sensitive_without_pointer": sensitive_without_pointer,
        "ambiguous_items": ambiguous_items,
    }
