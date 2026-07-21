"""meridian_outputs.classify -- broader canonical-vs-archival output classification.

Sprint item 2820ab1f.

Investigation summary (2026-07-20): the ``classify_outputs`` MCP tool was
NOT a stub. It already existed, fully implemented and tested, as a thin
``server.py`` wrapper around
:func:`outputs_local.classify_canonical_archival` (a real two-stage
design: stage 1 is a ``*_old`` / ``*_old_N`` filename heuristic, stage 2 is
a content-hash byte-identity check against the named twin). All of its
existing tests (``test_classify_outputs_api``, ``test_classify_outputs_sorted``,
``test_classify_outputs_deterministic`` in ``tests/test_outputs_local.py``)
pass unmodified. There is no pre-existing ``classify.py`` -- that was an
incorrect assumption about where the tool lived.

It was, however, genuinely too NARROW for the motivating real case from
tonight's session: two same-purpose CSVs
(``parabolic_radius_sweep_130_results_FULL130.csv`` vs a
``..._FULL130.csv.bak_41img_mislabeled`` sibling) were only distinguishable
by manually reading file size/mtime, because:

  1. ``outputs_local``'s stage-1 regex only recognises the ``_old``/``_old_N``
     stem-suffix convention -- a whole extra suffix appended AFTER the real
     extension (``.bak_41img_mislabeled``, editor ``~`` backups, ``_backup``/
     ``_deprecated``/``_mislabeled``/``_wip``/``_copy``/``_stale`` conventions)
     is never even considered a candidate, so the file falls straight through
     to "not a name-pattern candidate" with no further signal.
  2. Nothing in the tool's output surfaces file size/mtime at all -- exactly
     the two facts a human ended up having to check by hand.

This module does NOT duplicate ``outputs_local``'s stage-1/stage-2 logic or
its content-hashing (``_xxh3_file``/``_sha256_file`` are private
implementation details of that module, reserved for its own persistent FTS
index) -- it delegates to the PUBLIC
:func:`outputs_local.classify_canonical_archival` for everything that
already works, and only ADDS:

  1. A broader stage-1b filename heuristic (tried only when
     outputs_local's own convention found nothing) covering the naming
     patterns above.
  2. ``size`` / ``mtime`` / ``mtime_iso`` surfaced directly on every
     classification record, so "which one is newer/bigger" no longer
     requires a manual file-properties check outside the tool.
  3. When the broader heuristic finds a plausible canonical twin but this
     module can't reuse ``outputs_local``'s private hasher to *confirm*
     byte-identity, it is honest about that: it compares size (a
     cheap, always-correct "these definitely differ" signal, same idea as
     the size-prefilter ``outputs_local`` itself uses in its own rebuild
     path) and attaches an explicit "unconfirmed, same size, verify with a
     hash if you need certainty" note plus a size/mtime pair rather than
     ever asserting a false-confidence duplicate verdict.

Coordination with sibling modules (``annotate.py`` already landed;
``fingerprint.py``/``provenance.py`` were being built in parallel):
mirrors ``annotate.py``'s own precedent of importing only
``outputs_local``'s PUBLIC API (no leading-underscore names) rather than
reaching into its private helpers.

NOT wired into ``server.py``'s ``@mcp.tool() classify_outputs`` yet -- that
tool still delegates directly to ``outputs_local.classify_outputs``
(unchanged) and is intentionally left alone here (server.py and
outputs_local.py are both out of scope for this fix; see sprint item
2820ab1f's instructions). :func:`classify_outputs` below is the drop-in
superset a follow-up wiring change would point the tool at -- same
``{total, classifications}`` shape, additive fields only.
"""
from __future__ import annotations

import os
import posixpath
import re
from datetime import datetime, timezone
from typing import Any

from .outputs_local import classify_canonical_archival

__all__ = ["classify_outputs"]

# Stage-1b: broader archival-naming heuristic. Only tried when
# outputs_local's own `_old`/`_old_N` convention did NOT already classify
# the path -- purely additive, never overrides a confirmed outputs_local
# result. Patterns are conservative (explicit keywords) to avoid false
# "is this archival?" guesses on ordinary output names.
_STEM_SUFFIX_RE = re.compile(
    r"[_-](?:backup|bak|deprecated|mislabeled|wip|copy|stale|archived?)"
    r"(?:[_-]\S*)?$",
    re.IGNORECASE,
)
# A whole extra suffix appended AFTER the real extension, e.g.
# `run.csv.bak_41img_mislabeled` or `run.csv~` -- tonight's motivating case.
_WHOLE_SUFFIX_RE = re.compile(
    r"\.(?:bak|orig|backup)(?:[_.].*)?$|~$",
    re.IGNORECASE,
)


def _broader_canonical_guess(path: str) -> str | None:
    """Best-effort canonical-twin-name guess beyond outputs_local's `_old`
    convention. Returns None if no broader pattern matches.

    Uses posixpath (not os.path) so the guess is deterministic across
    Windows/POSIX, matching outputs_local's own ``_canonical_name`` convention.
    """
    directory = posixpath.dirname(path)
    base = posixpath.basename(path)
    stem, ext = posixpath.splitext(base)

    if _STEM_SUFFIX_RE.search(stem):
        candidate = f"{_STEM_SUFFIX_RE.sub('', stem)}{ext}"
        return posixpath.join(directory, candidate) if directory else candidate

    if _WHOLE_SUFFIX_RE.search(base):
        candidate = _WHOLE_SUFFIX_RE.sub("", base)
        if candidate and candidate != base:
            return posixpath.join(directory, candidate) if directory else candidate

    return None


def _stat_signal(path: str) -> dict[str, Any]:
    """Cheap size/mtime signal -- the exact manual check tonight's case
    needed and the existing classify_outputs never surfaced."""
    try:
        st = os.stat(path)
    except OSError:
        return {"size": None, "mtime": None, "mtime_iso": None}
    mtime_iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
    return {"size": st.st_size, "mtime": st.st_mtime, "mtime_iso": mtime_iso}


def classify_outputs(paths: list[str]) -> dict[str, Any]:
    """Classify output file paths as canonical or archival.

    Superset of :func:`outputs_local.classify_outputs`: identical stable
    sorted-by-path output shape, and identical stage-1/stage-2 verdicts for
    every path outputs_local's own ``_old``/``_old_N`` convention already
    resolves (delegated, not reimplemented) -- PLUS the broader naming
    heuristic and size/mtime signal described in this module's docstring.

    Returns:
      {total, classifications} where each classification has: path,
      is_archival, canonical_path, reason, size, mtime, mtime_iso.
    """
    sorted_paths = sorted(str(p) for p in paths if p)
    path_set = set(sorted_paths)
    base = classify_canonical_archival(sorted_paths)

    classifications: list[dict[str, Any]] = []
    for p in sorted_paths:
        rec = base[p]
        entry: dict[str, Any] = {
            "path": rec.path,
            "is_archival": rec.is_archival,
            "canonical_path": rec.canonical_path,
            "reason": rec.reason,
        }
        entry.update(_stat_signal(p))

        # Only reach for the broader heuristic when outputs_local's own
        # convention found nothing conclusive.
        if not rec.is_archival:
            guess = _broader_canonical_guess(p)
            if guess is not None and guess in path_set:
                twin_signal = _stat_signal(guess)
                this_size, twin_size = entry["size"], twin_signal["size"]
                this_mtime, twin_mtime = entry["mtime"], twin_signal["mtime"]
                entry["canonical_path"] = guess
                if this_size is not None and twin_size is not None and this_size != twin_size:
                    # BUGFIX (found live): content genuinely differs from the
                    # guessed twin, so this must NOT be flagged archival --
                    # is_archival correctly stays whatever outputs_local's own
                    # stage-1/stage-2 check already determined (rec.is_archival).
                    entry["reason"] = (
                        f"broader archival-naming heuristic matched (canonical twin "
                        f"guess '{guess}' present in batch), but sizes differ "
                        f"({this_size} vs {twin_size} bytes) -- content genuinely "
                        "differs, not a duplicate"
                    )
                else:
                    # BUGFIX (found live): a same-size match against a present
                    # canonical twin is exactly the case this heuristic exists to
                    # catch -- entry["is_archival"] was never actually being set
                    # here, so every match still reported is_archival=False
                    # despite finding and naming the real canonical twin.
                    entry["is_archival"] = True
                    relation = "unknown"
                    if this_mtime is not None and twin_mtime is not None:
                        relation = (
                            "newer" if this_mtime > twin_mtime else
                            "older" if this_mtime < twin_mtime else
                            "same-mtime"
                        )
                    entry["reason"] = (
                        f"broader archival-naming heuristic matched (canonical twin "
                        f"guess '{guess}' present in batch, same size) -- unconfirmed "
                        "duplicate (verify with a hash for certainty); this file is "
                        f"{relation} than that twin by mtime"
                    )
            elif guess is not None:
                entry["reason"] = (
                    f"{rec.reason}; broader heuristic guesses canonical twin "
                    f"'{guess}', not present in this batch"
                )

        classifications.append(entry)

    return {"total": len(sorted_paths), "classifications": classifications}
