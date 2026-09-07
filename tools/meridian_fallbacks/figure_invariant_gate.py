"""Local fallback figure-invariant gate (sprint item db63385b, "W31-B").

WHY THIS EXISTS: an executor revising a figure inside a document (a
typography pass -- font, color, layout, caption styling) needs a way to
prove, BEFORE claiming the edit complete, that nothing beyond typography
actually changed. A naive diff of the figure's rendered appearance cannot
tell "the numbers moved because the font got wider" apart from "the numbers
moved because someone swapped in different data" -- and a caption-string
comparison is actively unsafe: two structurally different figures can share
an identical caption (a mislabeled decoy), while a genuinely relabeled
figure keeps its real identity even though its caption text changed. This
module compares two already-extracted figure-slot payloads -- a trusted
CANONICAL slot and a CANDIDATE revision of it -- against three independent
questions, in fail-closed priority order:

  1. Is either slot's generating source resolvable at all?
  2. Is the resolvable source AMBIGUOUS (more than one equally-plausible
     origin)?
  3. Do the two slots share the SAME bound-source identity?
  4. Only once identity is confirmed: did the NUMERIC or TEXT content
     change (a real content edit), independent of TYPOGRAPHY (a cosmetic
     edit, which is explicitly never compared)?

No literal "C.11/C.12/C.20"-style item ever named a concrete decoy taxonomy
in this repository (checked: a full case-insensitive grep of the tracked
tree turned up nothing), so the three decoy classes this gate defends
against are named generically from first principles, matching this
package's own established design process for every prior gate in it
(``output_provenance_gate.py``, ``docx_completion_gate.py``): a NUMERIC
decoy (same claimed source, different numbers), a TEXT decoy (same claimed
source, different labels/text -- and, symmetrically, a candidate with the
SAME caption text but a genuinely DIFFERENT source, since caption strings
are never trusted for identity), and a SOURCE decoy (a candidate that
resolves to a different generating source entirely, however similar its
surface presentation looks).

Bound-source identity -- reusing an existing precedent, not a new one
-----------------------------------------------------------------------
``extensions/meridian-docs/meridian_docs/docs_intel.py``'s
``_verify_image_ownership`` already establishes this codebase's own
"never loose labels" precedent: a figure's true identity is its OOXML
``r:embed`` relationship id (rId), not its caption text -- two figures
sharing an rId are a hard duplicate even when their captions differ
(``test_copy_section_rejects_rid50_reuse_at_figure_5_21_and_rolls_back``).
This module's ``bound_source`` field reuses exactly that primitive (plus
the analogous identity primitives ``extensions/meridian-outputs``' own
provenance system already uses for a non-docx output: a resolved output
path, or a generating-script path) -- never a caption. See
``extensions/meridian-docs/tests/test_docx_write_integrity.py``'s
integration test for this module for the direct cross-check.

Deliberately NOT a file/DOCX reader
------------------------------------
Every check here is a PURE function of already-extracted payload dicts --
numeric/text content plus a bound-source identity descriptor -- matching
``docx_completion_gate.py``'s "pure function of already-read bytes/data"
discipline. This module never opens a ``.docx``, never touches the
canonical thesis document, and never calls the ``meridian-outputs``/
``meridian-docs`` MCP extensions or imports them -- a caller is expected to
have already resolved a figure's payload via those tools (or via this
package's own ``output_provenance_gate.py``) and hand the resulting plain
dict in here. Stdlib only (``dataclasses``, ``json``, ``argparse``,
``collections``) -- same "no hard import on the thing this is a fallback
for" discipline as every sibling module in this package.

Provenance-type vocabulary reused, not reinvented
----------------------------------------------------
The ``provenance_type``/``PROVENANCE_*`` fields below are kept
STRING-IDENTICAL to ``extensions/meridian-outputs/meridian_outputs/
provenance_status.py``'s own ``EXACT``/``RELOCATED``/``AMBIGUOUS``/
``DIRECTORY_FALLBACK``/``UNREGISTERED``/``UNKNOWN``/``STALE_BY_SCRIPT``
constants (and, transitively, this package's own
``output_provenance_gate.py``, which mirrors the same five of the seven
that predate the relocation/ambiguity extension) -- a caller can feed
either module's result dict's ``provenance_type`` straight into this
gate's ``FigureSlotPayload.provenance_type`` with no translation. This
module answers a DIFFERENT, higher-level question ("does the underlying
figure content invariant hold across a revision") on top of that
vocabulary; it is not a second, competing provenance system.

Five explicit verdicts (never a bare bool, never silently folded together)
------------------------------------------------------------------------------
  1. :data:`INVARIANT_HOLDS` -- bound source confirmed identical AND numeric
     content AND text content are byte-for-byte unchanged. Only typography
     (recorded in ``typography_diff`` for observability) may differ.
  2. :data:`INVARIANT_VIOLATION` -- bound source confirmed identical, but
     numeric and/or text content changed. The NUMERIC- and TEXT-decoy case.
  3. :data:`SOURCE_MISMATCH` -- the two slots do NOT share the same
     bound-source identity (including a candidate resolved only via
     content-hash RELOCATION rather than an exact match at the identity the
     canonical slot declares -- treated conservatively as a mismatch, never
     silently accepted as an equivalent identity). The SOURCE-decoy case,
     and also what a same-caption/different-source decoy resolves to.
  4. :data:`AMBIGUOUS` -- one or both slots' provenance signal itself says
     the source cannot be pinned to a single origin (multiple
     equally-plausible candidates). Fails closed -- never silently accepted
     as a match just because SOME resolution exists.
  5. :data:`NO_GENERATOR` -- neither a bound-source identity nor any usable
     provenance signal exists for one of the slots at all. Its own explicit
     "nothing to compare against" state -- never conflated with
     :data:`AMBIGUOUS` (which means "too many candidates", not "zero
     information") and never a crash.

Fixtures for every one of the five states, plus JSON round-tripping (every
"receipt" in this package must survive ``json.dumps``/``json.loads`` with no
data loss), live in ``tools/meridian_fallbacks/tests/
test_figure_invariant_gate.py``.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

__all__ = [
    "GATE_SCHEMA_VERSION",
    "INVARIANT_HOLDS",
    "INVARIANT_VIOLATION",
    "SOURCE_MISMATCH",
    "AMBIGUOUS",
    "NO_GENERATOR",
    "FIGURE_INVARIANT_VERDICTS",
    "PROVENANCE_EXACT",
    "PROVENANCE_RELOCATED",
    "PROVENANCE_AMBIGUOUS",
    "PROVENANCE_DIRECTORY_FALLBACK",
    "PROVENANCE_UNREGISTERED",
    "PROVENANCE_UNKNOWN",
    "PROVENANCE_STALE_BY_SCRIPT",
    "FigureSlotPayload",
    "compare_figure_invariants",
    "main",
]

GATE_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Explicit verdicts, ranked in the priority order compare_figure_invariants
# actually evaluates them in (see that function's docstring).
# ---------------------------------------------------------------------------
INVARIANT_HOLDS = "invariant_holds"
INVARIANT_VIOLATION = "invariant_violation"
SOURCE_MISMATCH = "source_mismatch"
AMBIGUOUS = "ambiguous"
NO_GENERATOR = "no_generator"
FIGURE_INVARIANT_VERDICTS: tuple[str, str, str, str, str] = (
    INVARIANT_HOLDS,
    INVARIANT_VIOLATION,
    SOURCE_MISMATCH,
    AMBIGUOUS,
    NO_GENERATOR,
)

# ---------------------------------------------------------------------------
# provenance_type vocabulary -- string-identical to
# extensions/meridian-outputs/meridian_outputs/provenance_status.py's own
# EXACT/RELOCATED/AMBIGUOUS/DIRECTORY_FALLBACK/UNREGISTERED/UNKNOWN/
# STALE_BY_SCRIPT constants (and this package's own output_provenance_gate.py,
# which predates the RELOCATED/AMBIGUOUS extension). NOT imported from either
# -- same "no hard import on the thing this is a fallback for" discipline as
# every other module in this package. A caller threads either module's
# provenance_type string straight into FigureSlotPayload.provenance_type.
# ---------------------------------------------------------------------------
PROVENANCE_EXACT = "exact"
PROVENANCE_RELOCATED = "relocated"
PROVENANCE_AMBIGUOUS = "ambiguous"
PROVENANCE_DIRECTORY_FALLBACK = "directory_fallback"
PROVENANCE_UNREGISTERED = "unregistered"
PROVENANCE_UNKNOWN = "unknown"
PROVENANCE_STALE_BY_SCRIPT = "stale_by_script"

# provenance_type values that, ON THEIR OWN (with no bound_source value
# either), mean "we have no information at all" rather than "we have a
# confirmed-but-weak signal". DIRECTORY_FALLBACK/STALE_BY_SCRIPT/EXACT/
# RELOCATED/AMBIGUOUS are all real signals even without a bound_source value;
# these two, and a bare None/"", are not.
_NO_SIGNAL_PROVENANCE_TYPES = frozenset({None, "", PROVENANCE_UNKNOWN, PROVENANCE_UNREGISTERED})


def _qn(kind: Any, value: Any) -> tuple[Any, Any]:
    """Small helper kept name-symmetric with this package's sibling
    modules' ``_qn``-style helpers; used only for the bound-source key."""
    return (kind, value)


# ---------------------------------------------------------------------------
# FigureSlotPayload -- the pure data model this gate compares. A caller
# builds one of these from whatever already-extracted figure data it has
# (a docx figure block, a resolved output row, ...); this module never
# derives one by reading a file itself.
# ---------------------------------------------------------------------------

@dataclass
class FigureSlotPayload:
    """One figure slot's already-extracted comparison payload.

    ``bound_source`` is the ONLY field this gate treats as identity -- a
    mapping with at least ``kind`` (e.g. ``"rid"``, ``"resolved_path"``,
    ``"generating_script"``, or ``"ambiguous"`` for an explicit
    caller-declared tie) and ``value`` (the actual rId/path/script string,
    or ``None``). ``caption_text`` is carried through for audit/reporting
    ONLY -- it is never read by :func:`compare_figure_invariants` when
    deciding identity or content equality, per the "never loose labels"
    precedent ``docs_intel._verify_image_ownership`` already established
    for this codebase.

    ``numeric_values``/``text_content`` are flat sequences of hashable
    scalars (numbers/strings) -- e.g. table-cell values in row-major order,
    or axis-tick/legend text -- compared for exact (order-sensitive)
    equality once identity is confirmed. ``typography`` is a plain dict of
    style-only attributes (font, size, color, alignment, layout position);
    it is always DIFFED for the report's observability but NEVER allowed to
    affect the verdict -- that is the entire point of this gate.

    ``provenance_type``/``provenance_candidates`` are optional and mirror
    ``extensions/meridian-outputs/meridian_outputs/provenance_status.py``'s
    own ``provenance_type``/``candidates`` fields verbatim; they refine how
    confidently ``bound_source`` was resolved (see the module docstring's
    provenance-vocabulary section) without replacing ``bound_source`` as the
    identity itself.
    """

    bound_source: dict[str, Any] = field(default_factory=dict)
    numeric_values: Sequence[Any] = field(default_factory=tuple)
    text_content: Sequence[Any] = field(default_factory=tuple)
    typography: dict[str, Any] = field(default_factory=dict)
    caption_text: str | None = None
    provenance_type: str | None = None
    provenance_candidates: Sequence[Any] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # Normalize bound_source to always carry both keys, defaulted to
        # None, so downstream comparisons never need a defensive .get(...)
        # dance -- a payload built with bound_source={} behaves identically
        # to one built with bound_source={"kind": None, "value": None}.
        normalized = dict(self.bound_source or {})
        normalized.setdefault("kind", None)
        normalized.setdefault("value", None)
        self.bound_source = normalized
        self.numeric_values = tuple(self.numeric_values)
        self.text_content = tuple(self.text_content)
        self.typography = dict(self.typography or {})
        self.provenance_candidates = tuple(self.provenance_candidates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bound_source": dict(self.bound_source),
            "numeric_values": list(self.numeric_values),
            "text_content": list(self.text_content),
            "typography": dict(self.typography),
            "caption_text": self.caption_text,
            "provenance_type": self.provenance_type,
            "provenance_candidates": list(self.provenance_candidates),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FigureSlotPayload":
        """Build a payload from a plain mapping, tolerating missing keys
        (every field has a safe default) -- a caller need only supply the
        fields it actually has extracted."""
        return cls(
            bound_source=dict(data.get("bound_source") or {}),
            numeric_values=tuple(data.get("numeric_values") or ()),
            text_content=tuple(data.get("text_content") or ()),
            typography=dict(data.get("typography") or {}),
            caption_text=data.get("caption_text"),
            provenance_type=data.get("provenance_type"),
            provenance_candidates=tuple(data.get("provenance_candidates") or ()),
        )


def _coerce_payload(raw: "FigureSlotPayload | Mapping[str, Any]", *, role: str) -> FigureSlotPayload:
    if isinstance(raw, FigureSlotPayload):
        return raw
    if isinstance(raw, Mapping):
        return FigureSlotPayload.from_dict(raw)
    raise TypeError(
        f"{role} payload must be a FigureSlotPayload or a mapping, got {type(raw).__name__!r}"
    )


# ---------------------------------------------------------------------------
# Classification helpers -- each answers exactly one yes/no question, kept
# separate so compare_figure_invariants' priority order reads as a flat
# if/elif chain rather than nested conditionals.
# ---------------------------------------------------------------------------

def _has_bound_source_value(payload: FigureSlotPayload) -> bool:
    return bool(payload.bound_source.get("value"))


def _is_unresolvable_source(payload: FigureSlotPayload) -> bool:
    """True when this slot carries NEITHER a bound-source value NOR any
    provenance signal that would itself count as "something is known" (see
    the module-level ``_NO_SIGNAL_PROVENANCE_TYPES`` set). This is the
    :data:`NO_GENERATOR` condition -- "nothing to compare against", distinct
    from :data:`AMBIGUOUS` ("too many things to compare against")."""
    if _has_bound_source_value(payload):
        return False
    return payload.provenance_type in _NO_SIGNAL_PROVENANCE_TYPES


def _is_ambiguous_source(payload: FigureSlotPayload) -> bool:
    """True when this slot's own signal says its identity cannot be pinned
    to a single origin. An explicit ``bound_source={"kind": "ambiguous"}``
    marker (a caller that already knows it has a tie, independent of any
    outputs-provenance lookup) is honored the same as
    ``provenance_type == PROVENANCE_AMBIGUOUS``. A ``PROVENANCE_RELOCATED``
    status carrying more than one candidate is internally inconsistent with
    that status's own single-match contract (see provenance_status.py's
    docstring: RELOCATED means exactly one hash match, AMBIGUOUS means two
    or more) -- treated as ambiguous defensively rather than trusting a
    mislabeled single-match claim."""
    if payload.bound_source.get("kind") == "ambiguous":
        return True
    if payload.provenance_type == PROVENANCE_AMBIGUOUS:
        return True
    if payload.provenance_type == PROVENANCE_RELOCATED and len(payload.provenance_candidates) > 1:
        return True
    return False


def _is_relocated_source(payload: FigureSlotPayload) -> bool:
    """True when this slot's identity was resolved only via content-hash
    RELOCATION (provenance_status.py's ``RELOCATED``) rather than an exact
    match at the path/identity originally declared. Content identity IS
    confirmed in that case, but at a DIFFERENT recorded origin than the one
    being compared against -- this gate treats that conservatively as
    :data:`SOURCE_MISMATCH` rather than silently accepting a relocation as
    an equivalent match, per this module's fail-closed design."""
    return payload.provenance_type == PROVENANCE_RELOCATED


def _source_key(payload: FigureSlotPayload) -> tuple[Any, Any]:
    return _qn(payload.bound_source.get("kind"), payload.bound_source.get("value"))


def _diff_sequence(canonical: Sequence[Any], candidate: Sequence[Any]) -> dict[str, Any]:
    """Order-sensitive equality (``changed``) plus a multiset added/removed
    breakdown for diagnostics. ``reordered`` is True only when the two
    sequences contain the exact same multiset of values in a different
    order -- a real (if narrow) typography-adjacent case (e.g. a table's
    row order changed for layout reasons without any cell value changing)
    that is worth surfacing distinctly from a genuine content change, even
    though this gate still reports it under ``changed`` (order is part of
    the extracted content contract a caller declares, not typography)."""
    canonical_list = list(canonical)
    candidate_list = list(candidate)
    canonical_counts = Counter(canonical_list)
    candidate_counts = Counter(candidate_list)
    removed = sorted((canonical_counts - candidate_counts).elements(), key=repr)
    added = sorted((candidate_counts - canonical_counts).elements(), key=repr)
    changed = canonical_list != candidate_list
    return {
        "changed": changed,
        "canonical": canonical_list,
        "candidate": candidate_list,
        "removed": removed,
        "added": added,
        "reordered": changed and not removed and not added,
    }


def _diff_typography(canonical: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Informational only -- NEVER consulted by compare_figure_invariants
    when computing a verdict. Exists so a caller/reviewer can positively
    confirm what typography actually changed, not just that the verdict
    ignored it."""
    keys = sorted(set(canonical) | set(candidate), key=repr)
    changed_keys = [k for k in keys if canonical.get(k) != candidate.get(k)]
    return {
        "changed_keys": changed_keys,
        "canonical": dict(canonical),
        "candidate": dict(candidate),
    }


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

def compare_figure_invariants(
    canonical: "FigureSlotPayload | Mapping[str, Any]",
    candidate: "FigureSlotPayload | Mapping[str, Any]",
) -> dict[str, Any]:
    """Compare a trusted CANONICAL figure-slot payload against a CANDIDATE
    revision and return one fail-closed verdict dict.

    Accepts either a :class:`FigureSlotPayload` or a plain mapping with the
    same fields (missing fields default sanely -- see
    :meth:`FigureSlotPayload.from_dict`) for either argument.

    Evaluated in this fixed priority order (first match wins):

      1. :data:`NO_GENERATOR` -- either slot has no bound-source value and
         no provenance signal at all.
      2. :data:`AMBIGUOUS` -- either slot's own signal says its identity
         cannot be pinned to one origin.
      3. :data:`SOURCE_MISMATCH` -- either slot's identity was resolved only
         via content-hash relocation, OR the two slots' bound-source keys
         differ outright.
      4. :data:`INVARIANT_VIOLATION` -- identity confirmed identical, but
         ``numeric_values`` and/or ``text_content`` differ.
      5. :data:`INVARIANT_HOLDS` -- identity confirmed identical AND
         numeric/text content unchanged. ``typography`` may differ freely.

    Never raises for a well-formed payload (mapping or
    :class:`FigureSlotPayload`, with any/all fields omitted) -- every branch
    above is reached by construction. Raises :class:`TypeError` only when an
    argument is neither, which is a caller programming error rather than
    something to report as a verdict (this module has no file/bytes to have
    gone missing or corrupted -- there is nothing to gracefully degrade to).

    Returns a fully JSON-serializable dict: ``schema_version``, ``verdict``,
    ``reasons`` (list[str]), ``canonical_bound_source``/
    ``candidate_bound_source``, ``canonical_provenance_type``/
    ``candidate_provenance_type``, ``numeric_diff``/``text_diff`` (``None``
    unless identity was confirmed and content was actually compared), and
    ``typography_diff`` (always computed; never affects ``verdict``).
    """
    canonical_payload = _coerce_payload(canonical, role="canonical")
    candidate_payload = _coerce_payload(candidate, role="candidate")

    reasons: list[str] = []
    numeric_diff: dict[str, Any] | None = None
    text_diff: dict[str, Any] | None = None

    if _is_unresolvable_source(canonical_payload):
        verdict = NO_GENERATOR
        reasons.append(
            "canonical payload has no resolvable bound source and no usable "
            "provenance signal -- nothing to compare the candidate against"
        )
    elif _is_unresolvable_source(candidate_payload):
        verdict = NO_GENERATOR
        reasons.append(
            "candidate payload has no resolvable bound source and no usable "
            "provenance signal -- nothing to confirm its identity against "
            "the canonical slot"
        )
    elif _is_ambiguous_source(canonical_payload) or _is_ambiguous_source(candidate_payload):
        verdict = AMBIGUOUS
        ambiguous_side = candidate_payload if _is_ambiguous_source(candidate_payload) else canonical_payload
        reasons.append(
            "bound-source identity is ambiguous (provenance_type="
            f"{ambiguous_side.provenance_type!r}, "
            f"{len(ambiguous_side.provenance_candidates)} candidate(s)) -- "
            "cannot confirm a single origin; failing closed rather than guessing"
        )
    elif _is_relocated_source(candidate_payload) or _is_relocated_source(canonical_payload):
        verdict = SOURCE_MISMATCH
        reasons.append(
            "bound source was resolved only via content-hash relocation "
            "(provenance_type=relocated), not an exact match at the "
            "declared identity -- treated as a source mismatch, never "
            "silently accepted as an equivalent match"
        )
    elif _source_key(canonical_payload) != _source_key(candidate_payload):
        verdict = SOURCE_MISMATCH
        reasons.append(
            f"bound source differs: canonical={_source_key(canonical_payload)!r} "
            f"candidate={_source_key(candidate_payload)!r} -- caption text is "
            "never used to establish identity, only the bound source itself"
        )
    else:
        numeric_diff = _diff_sequence(canonical_payload.numeric_values, candidate_payload.numeric_values)
        text_diff = _diff_sequence(canonical_payload.text_content, candidate_payload.text_content)
        if numeric_diff["changed"] or text_diff["changed"]:
            verdict = INVARIANT_VIOLATION
            if numeric_diff["changed"]:
                reasons.append("numeric content differs despite an identical bound source")
            if text_diff["changed"]:
                reasons.append("text content differs despite an identical bound source")
        else:
            verdict = INVARIANT_HOLDS
            reasons.append(
                "bound source matches and numeric/text content are unchanged -- "
                "only typography/layout may have changed"
            )

    return {
        "schema_version": GATE_SCHEMA_VERSION,
        "verdict": verdict,
        "reasons": reasons,
        "canonical_bound_source": dict(canonical_payload.bound_source),
        "candidate_bound_source": dict(candidate_payload.bound_source),
        "canonical_provenance_type": canonical_payload.provenance_type,
        "candidate_provenance_type": candidate_payload.provenance_type,
        "numeric_diff": numeric_diff,
        "text_diff": text_diff,
        "typography_diff": _diff_typography(canonical_payload.typography, candidate_payload.typography),
    }


# ---------------------------------------------------------------------------
# CLI -- genuinely runnable standalone, matching this package's other
# gates (output_provenance_gate.py / docx_completion_gate.py): an executor
# with no MCP connection can shell out to this file directly against two
# already-extracted JSON payload files.
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="figure_invariant_gate",
        description=(
            "Local fallback figure-invariant gate: compares a canonical and "
            "a candidate figure-slot payload (already-extracted numeric/text "
            "content plus a bound-source identity descriptor -- never a "
            "caption string) and reports, fail-closed, whether only "
            "typography/layout changed."
        ),
    )
    parser.add_argument("canonical_json", help="Path to a JSON file holding the canonical FigureSlotPayload fields.")
    parser.add_argument("candidate_json", help="Path to a JSON file holding the candidate FigureSlotPayload fields.")
    args = parser.parse_args(argv)

    with open(args.canonical_json, "r", encoding="utf-8") as fh:
        canonical_raw = json.load(fh)
    with open(args.candidate_json, "r", encoding="utf-8") as fh:
        candidate_raw = json.load(fh)

    result = compare_figure_invariants(canonical_raw, candidate_raw)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == INVARIANT_HOLDS else 1


if __name__ == "__main__":
    sys.exit(main())
