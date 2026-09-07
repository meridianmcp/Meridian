"""Local fallback figure-slot manifest gate (sprint item 5cc3d745, "W31-C").

WHY THIS EXISTS: an executor promoting a batch of figure-slot assets (e.g.
finalizing a set of typography/content revisions that already passed
``figure_invariant_gate.compare_figure_invariants`` one slot at a time) needs
to prove, BEFORE the promotion actually touches disk, that EVERY slot the
batch was supposed to cover has an accounted-for disposition -- not just that
the slots someone remembered to classify look fine in isolation. A silent
gap (a slot nobody classified) or a silent conflict (a slot classified two
different ways) is exactly the kind of defect that a per-slot gate like
``figure_invariant_gate`` cannot catch, because that gate only ever sees the
slots it is handed -- it has no notion of the full expected set.

This module answers a different, manifest-level question: given the full set
of figure slots a promotion batch was supposed to resolve, and four
caller-supplied disposition buckets (``promoted``, ``held``, ``skipped``,
``ambiguous``), is that manifest COMPLETE -- every expected slot appears in
exactly one bucket, with a real ``reason`` and ``owner`` recorded -- or does
it have a gap or a conflict that must block promotion?

Four explicit buckets, one classification per slot
----------------------------------------------------
  - ``promoted`` -- this slot's revision is being finalized as part of this
    batch.
  - ``held`` -- this slot is deliberately NOT being promoted yet (e.g. still
    under review), but its disposition IS known and recorded.
  - ``skipped`` -- this slot is explicitly out of scope for this promotion
    batch (e.g. unaffected by the current revision), recorded so its absence
    from ``promoted``/``held`` is never mistaken for an oversight.
  - ``ambiguous`` -- this slot's disposition could not be confidently
    determined (e.g. its own upstream comparison, such as
    ``figure_invariant_gate.compare_figure_invariants``, itself returned
    ``AMBIGUOUS`` or ``SOURCE_MISMATCH``) and is being surfaced rather than
    silently defaulted into ``held`` or ``skipped``.

Every entry in every bucket must carry a non-blank ``reason`` (why this slot
landed in this bucket) and ``owner`` (who/what made that call) -- an entry
missing either is treated as untrustworthy input, not silently accepted with
a blank explanation.

Three explicit manifest-level verdicts (never a bare bool)
------------------------------------------------------------
  1. :data:`MANIFEST_COMPLETE` -- every id in ``expected_slot_ids`` appears
     in EXACTLY ONE of the four buckets, every entry is well-formed (real
     ``slot_id``/``reason``/``owner``), and no bucket entry names a slot
     outside ``expected_slot_ids``. Fails OPEN only in this single case --
     this is what :func:`~tools.meridian_fallbacks.transactional_merge.
     promote` requires before it will call
     :func:`~tools.meridian_fallbacks.transactional_merge.apply_patch_manifest`
     at all.
  2. :data:`MANIFEST_INCOMPLETE` -- one or more expected slots have NO
     classification in any bucket. A simple gap, not a conflict.
  3. :data:`MANIFEST_CONTRADICTORY` -- the manifest actively conflicts with
     itself: a slot classified in more than one bucket (including twice in
     the SAME bucket), a bucket entry naming a slot outside
     ``expected_slot_ids``, a malformed entry (missing/blank ``slot_id``,
     ``reason``, or ``owner``, or an entry whose own ``bucket`` field
     disagrees with the list it was supplied in), or -- as a last-resort
     defensive check -- an unexplained mismatch between the raw count of
     bucket entries and the expected slot count that none of the above
     specific checks accounts for.

:func:`reconcile_slot_manifest` never raises for well-formed argument TYPES
(sequences of :class:`SlotClassification` or plain mappings) -- every
malformed *entry* becomes a reported ``structural_errors`` string instead,
exactly like ``figure_invariant_gate``'s and ``docx_completion_gate.py``'s
own "become a reported failure, not a crash" discipline. It raises
:class:`TypeError` only when an argument is not a sequence of mappings /
:class:`SlotClassification` at all, which is a caller programming error.

Deliberately NOT a docx/output reader, and NOT a resolver
-------------------------------------------------------------
Exactly like every sibling gate in this package, this module is a PURE
function of already-decided classification data -- it never reads a
``.docx``, never reads an outputs directory, and never calls the
``meridian-docs``/``meridian-outputs`` MCP extensions or imports
``figure_invariant_gate``, ``docs_intel.py``, or ``provenance_status.py``
internals. A caller resolves each slot's disposition (by whatever means,
including running ``figure_invariant_gate.compare_figure_invariants`` per
slot first) and hands this module the resulting bucket assignments. Stdlib
only (``dataclasses``, ``json``, ``argparse``, ``collections``, ``typing``).

Fixtures for a complete manifest, each incomplete/contradictory failure
mode, the empty-expected-set edge case, and JSON round-tripping live in
``tools/meridian_fallbacks/tests/test_figure_slot_manifest.py`` -- which
also covers ``transactional_merge.promote()``'s enforcement of this gate's
verdict before any asset promotion is allowed to touch disk.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

__all__ = [
    "GATE_SCHEMA_VERSION",
    "SLOT_PROMOTED",
    "SLOT_HELD",
    "SLOT_SKIPPED",
    "SLOT_AMBIGUOUS",
    "SLOT_BUCKETS",
    "MANIFEST_COMPLETE",
    "MANIFEST_INCOMPLETE",
    "MANIFEST_CONTRADICTORY",
    "MANIFEST_VERDICTS",
    "SlotClassification",
    "reconcile_slot_manifest",
    "main",
]

GATE_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# The four per-slot disposition buckets. Order here is the canonical order
# used for `buckets` in the returned dict and for CLI/JSON input.
# ---------------------------------------------------------------------------
SLOT_PROMOTED = "promoted"
SLOT_HELD = "held"
SLOT_SKIPPED = "skipped"
SLOT_AMBIGUOUS = "ambiguous"
SLOT_BUCKETS: tuple[str, str, str, str] = (
    SLOT_PROMOTED,
    SLOT_HELD,
    SLOT_SKIPPED,
    SLOT_AMBIGUOUS,
)

# ---------------------------------------------------------------------------
# Manifest-level verdicts, ranked in the priority order
# reconcile_slot_manifest actually evaluates them in (see its docstring).
# ---------------------------------------------------------------------------
MANIFEST_COMPLETE = "manifest_complete"
MANIFEST_INCOMPLETE = "manifest_incomplete"
MANIFEST_CONTRADICTORY = "manifest_contradictory"
MANIFEST_VERDICTS: tuple[str, str, str] = (
    MANIFEST_COMPLETE,
    MANIFEST_INCOMPLETE,
    MANIFEST_CONTRADICTORY,
)


@dataclass
class SlotClassification:
    """One figure slot's disposition within a promotion manifest.

    ``bucket`` must be one of :data:`SLOT_BUCKETS`. ``reason`` and ``owner``
    are both required and must be non-blank -- a classification with no
    recorded reason or owner is exactly the "silently accepted without an
    accounting" failure mode this gate exists to catch, so
    :func:`reconcile_slot_manifest` treats a blank/missing value as a
    structural error rather than a valid classification.
    """

    slot_id: str
    bucket: str
    reason: str
    owner: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "bucket": self.bucket,
            "reason": self.reason,
            "owner": self.owner,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SlotClassification":
        try:
            return cls(
                slot_id=data["slot_id"],
                bucket=data["bucket"],
                reason=data["reason"],
                owner=data["owner"],
            )
        except KeyError as exc:
            raise ValueError(f"slot classification missing required field: {exc}") from exc


def _normalize_bucket_entries(
    entries: "Sequence[SlotClassification | Mapping[str, Any]]",
    *,
    bucket: str,
) -> "tuple[list[SlotClassification], list[str]]":
    """Coerce every entry supplied for one bucket to a
    :class:`SlotClassification`, returning ``(classifications, errors)``.

    Never raises for a malformed dict-shaped entry (missing/blank
    ``slot_id``/``reason``/``owner``, or a ``bucket`` field that disagrees
    with the list the entry was actually supplied in) -- that becomes a
    ``structural_errors`` string instead, and the entry is excluded from the
    returned classifications so it can never silently participate in
    duplicate/unclassified accounting. Raises :class:`TypeError` only when an
    entry is neither a :class:`SlotClassification` nor a mapping at all --
    a caller programming error, not a manifest-content problem.
    """
    classifications: list[SlotClassification] = []
    errors: list[str] = []
    for index, raw in enumerate(entries):
        if isinstance(raw, SlotClassification):
            data: dict[str, Any] = raw.to_dict()
        elif isinstance(raw, Mapping):
            data = dict(raw)
        else:
            raise TypeError(
                f"{bucket}[{index}] must be a SlotClassification or a mapping, "
                f"got {type(raw).__name__!r}"
            )

        slot_id = data.get("slot_id")
        reason = data.get("reason")
        owner = data.get("owner")
        declared_bucket = data.get("bucket", bucket)

        if not isinstance(slot_id, str) or not slot_id.strip():
            errors.append(f"{bucket}[{index}]: missing or blank 'slot_id'")
            continue
        if declared_bucket != bucket:
            errors.append(
                f"{bucket}[{index}] (slot_id={slot_id!r}): entry declares "
                f"bucket={declared_bucket!r}, but was supplied in the "
                f"{bucket!r} bucket list"
            )
            continue
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{bucket}[{index}] (slot_id={slot_id!r}): missing or blank 'reason'")
            continue
        if not isinstance(owner, str) or not owner.strip():
            errors.append(f"{bucket}[{index}] (slot_id={slot_id!r}): missing or blank 'owner'")
            continue

        classifications.append(
            SlotClassification(slot_id=slot_id, bucket=bucket, reason=reason, owner=owner)
        )
    return classifications, errors


def reconcile_slot_manifest(
    expected_slot_ids: Sequence[str],
    promoted: "Sequence[SlotClassification | Mapping[str, Any]]" = (),
    held: "Sequence[SlotClassification | Mapping[str, Any]]" = (),
    skipped: "Sequence[SlotClassification | Mapping[str, Any]]" = (),
    ambiguous: "Sequence[SlotClassification | Mapping[str, Any]]" = (),
) -> dict[str, Any]:
    """Reconcile ``expected_slot_ids`` against the four disposition buckets
    and return one fail-closed manifest-level verdict dict.

    Each of ``promoted``/``held``/``skipped``/``ambiguous`` accepts a
    sequence of :class:`SlotClassification` instances or plain mappings with
    the same fields (``bucket`` may be omitted from a mapping -- it defaults
    to whichever bucket argument the entry was supplied under).

    Evaluated in this fixed priority order (first match wins):

      1. :data:`MANIFEST_CONTRADICTORY` -- one or more malformed entries
         (``structural_errors``), OR a slot classified in more than one
         bucket -- including twice within the SAME bucket
         (``duplicate_assignments``), OR a bucket entry naming a slot not in
         ``expected_slot_ids`` (``unknown_slot_ids``).
      2. :data:`MANIFEST_INCOMPLETE` -- (only once the manifest is
         internally consistent) one or more ``expected_slot_ids`` have no
         classification in any bucket (``unclassified_slot_ids``).
      3. :data:`MANIFEST_CONTRADICTORY` (defensive fallback) -- the raw
         count of bucket entries does not equal ``len(expected_slot_ids)``
         even though none of the specific checks above explains why. This is
         REACHABLE, not merely theoretical: it fires whenever
         ``expected_slot_ids`` itself contains a duplicate id (e.g.
         ``["fig-1", "fig-1", "fig-2"]``) and every DISTINCT id is otherwise
         classified exactly once. That manifest has no unclassified slot
         (every distinct expected id got exactly one classification), no
         duplicate assignment (no ``slot_id`` appears more than once across
         the accepted classifications), and no unknown slot -- so none of
         the specific checks above fires -- yet ``expected`` (built via
         ``list(expected_slot_ids)``, which never dedupes its input) is
         longer than the number of distinct ids actually classified, so the
         raw entry count no longer equals ``len(expected)``. Hitting this
         fallback therefore means either that specific "duplicate in the
         expected list" shape, or some other inconsistency this function's
         own logic did not anticipate -- either way, failing closed rather
         than reporting COMPLETE on an unexplained mismatch is correct.
      4. :data:`MANIFEST_COMPLETE` -- every expected slot is classified
         exactly once, every entry is well-formed, and no unknown slot
         appears in any bucket.

    An empty ``expected_slot_ids`` with all four buckets also empty is
    :data:`MANIFEST_COMPLETE` (vacuously -- there is nothing to classify and
    nothing was).

    Returns a fully JSON-serializable dict: ``schema_version``, ``verdict``,
    ``reasons`` (list[str]), ``expected_slot_ids``, ``buckets`` (each bucket
    name mapped to its list of accepted ``SlotClassification.to_dict()``
    entries -- rejected/malformed entries are never included here, only in
    ``structural_errors``), ``unclassified_slot_ids``,
    ``duplicate_assignments`` (slot_id -> sorted list of bucket names it was
    found in), ``unknown_slot_ids``, ``structural_errors``, and ``counts``
    (``expected_total``/``classified_total``/``counts_match``).
    """
    expected = list(expected_slot_ids)
    expected_set = set(expected)

    bucket_inputs: dict[str, Sequence[Any]] = {
        SLOT_PROMOTED: promoted,
        SLOT_HELD: held,
        SLOT_SKIPPED: skipped,
        SLOT_AMBIGUOUS: ambiguous,
    }

    structural_errors: list[str] = []
    all_classifications: list[SlotClassification] = []
    buckets_out: dict[str, list[dict[str, Any]]] = {}

    for bucket in SLOT_BUCKETS:
        classifications, errors = _normalize_bucket_entries(bucket_inputs[bucket], bucket=bucket)
        structural_errors.extend(errors)
        all_classifications.extend(classifications)
        buckets_out[bucket] = [c.to_dict() for c in classifications]

    occurrence_counts = Counter(c.slot_id for c in all_classifications)
    duplicate_assignments: dict[str, list[str]] = {
        slot_id: sorted({c.bucket for c in all_classifications if c.slot_id == slot_id})
        for slot_id, count in occurrence_counts.items()
        if count > 1
    }

    classified_slot_ids = set(occurrence_counts)
    unknown_slot_ids = sorted(classified_slot_ids - expected_set)
    unclassified_slot_ids = sorted(expected_set - classified_slot_ids)

    raw_entry_total = sum(len(bucket_inputs[b]) for b in SLOT_BUCKETS)
    counts_match = raw_entry_total == len(expected)

    reasons: list[str] = []
    if structural_errors:
        reasons.extend(structural_errors)
    if duplicate_assignments:
        reasons.append(
            "slot(s) classified more than once: "
            + ", ".join(f"{sid}->{buckets}" for sid, buckets in sorted(duplicate_assignments.items()))
        )
    if unknown_slot_ids:
        reasons.append(
            "slot(s) classified but absent from expected_slot_ids: " + ", ".join(unknown_slot_ids)
        )
    if unclassified_slot_ids:
        reasons.append(
            "slot(s) in expected_slot_ids with no classification in any bucket: "
            + ", ".join(unclassified_slot_ids)
        )

    if structural_errors or duplicate_assignments or unknown_slot_ids:
        verdict = MANIFEST_CONTRADICTORY
    elif unclassified_slot_ids:
        verdict = MANIFEST_INCOMPLETE
    elif not counts_match:
        verdict = MANIFEST_CONTRADICTORY
        reasons.append(
            f"bucket entry count ({raw_entry_total}) does not match the expected slot "
            f"count ({len(expected)}), with no other check explaining why -- failing closed"
        )
    else:
        verdict = MANIFEST_COMPLETE
        reasons.append(
            f"all {len(expected)} expected slot(s) classified exactly once across "
            "promoted/held/skipped/ambiguous"
        )

    return {
        "schema_version": GATE_SCHEMA_VERSION,
        "verdict": verdict,
        "reasons": reasons,
        "expected_slot_ids": expected,
        "buckets": buckets_out,
        "unclassified_slot_ids": unclassified_slot_ids,
        "duplicate_assignments": dict(sorted(duplicate_assignments.items())),
        "unknown_slot_ids": unknown_slot_ids,
        "structural_errors": structural_errors,
        "counts": {
            "expected_total": len(expected),
            "classified_total": raw_entry_total,
            "counts_match": counts_match,
        },
    }


# ---------------------------------------------------------------------------
# CLI -- genuinely runnable standalone, matching this package's other gates
# (figure_invariant_gate.py / output_provenance_gate.py / docx_completion_
# gate.py): an executor with no MCP connection can shell out to this file
# directly against an already-authored manifest JSON file.
# ---------------------------------------------------------------------------

def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="figure_slot_manifest",
        description=(
            "Local fallback figure-slot manifest gate: reconciles an "
            "expected set of figure-slot ids against promoted/held/skipped/"
            "ambiguous classification buckets and reports, fail-closed, "
            "whether the manifest is complete enough to promote."
        ),
    )
    parser.add_argument(
        "manifest_json",
        help=(
            "Path to a JSON file with 'expected_slot_ids' (list[str]) and "
            "any of 'promoted'/'held'/'skipped'/'ambiguous' (each a list of "
            "{slot_id, reason, owner} objects)."
        ),
    )
    args = parser.parse_args(argv)

    with open(args.manifest_json, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    result = reconcile_slot_manifest(
        raw.get("expected_slot_ids") or [],
        promoted=raw.get("promoted") or [],
        held=raw.get("held") or [],
        skipped=raw.get("skipped") or [],
        ambiguous=raw.get("ambiguous") or [],
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == MANIFEST_COMPLETE else 1


if __name__ == "__main__":
    sys.exit(main())
