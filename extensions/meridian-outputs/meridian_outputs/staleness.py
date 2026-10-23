"""One staleness vocabulary for meridian-outputs (sprint item oz-outputs-staleness-vocabulary).

Four producers inside this package each answer some version of "is this
output still current?", with different field names and different notions of
"unknown":

  1. ``provenance_status._staleness`` (provenance_status.py:300) -- the
     existence and content-hash check on an EXACT ``annotate`` record. Returns
     ``{exists_on_disk, recorded_content_hash, current_content_hash, stale,
     reason}``.
  2. The ``stale_by_script`` provenance state (provenance_status.py:297) -- an
     exact record whose independently fingerprint-tagged generating script has
     changed since tagging. Surfaced on the ``get_provenance_status`` result as
     ``provenance_type``, with the script comparison under ``script_staleness``.
  3. The script-hash check behind ``check_staleness`` (server.py:1237), which is
     :func:`fingerprint.check_staleness` -- one ``StalenessResult`` dict per
     tagged output.
  4. The script-hash check behind ``find_stale_by_script`` (server.py:1262),
     which is :func:`fingerprint.find_stale_by_script` -- a list of stale output
     paths.

Vocabulary
----------
  ``fresh``     the check ran and the output matches its recorded baseline.
  ``stale``     the check ran and found a difference, or the recorded file or
                script is gone.
  ``unknown``   the source has no basis to decide either way: nothing recorded
                to compare against, no generating script linked, identity not
                confirmed, or the index is still converging.
  ``degraded``  the source answered, but on weaker evidence than a full check:
                "not stale" with no baseline hash to compare against, or an
                absence taken from an index that has not converged.

Every record carries a ``reason``: copied from the source where the source
gives one, derived otherwise. Per-source field names are NOT renamed. This
module only reads those shapes and writes a separate :class:`StalenessRecord`,
so existing callers keep their current keys and values.

Core producers named here but NOT mapped and NOT changed by this module. They
live in the core repo, outside this package, and keep their own shapes:

  - ``meridian.outputs_indexer.check_promotion_source_freshness``
    (meridian/outputs_indexer.py:2042), the source-half freshness check for a
    script -> artifact -> document promotion.
  - ``meridian.embedded_staleness`` (``check_embedded_staleness``), the
    sha256-vs-live-source drift check for embedded figures and tables.

This module is pure: no I/O and no imports from sibling modules, so
``provenance_status`` can import it without an import cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "FRESH",
    "STALE",
    "UNKNOWN",
    "DEGRADED",
    "STATES",
    "SOURCE_PROVENANCE_STALENESS",
    "SOURCE_STALE_BY_SCRIPT_STATE",
    "SOURCE_CHECK_STALENESS",
    "SOURCE_FIND_STALE_BY_SCRIPT",
    "SOURCES",
    "StalenessRecord",
    "map_staleness",
]

FRESH = "fresh"
STALE = "stale"
UNKNOWN = "unknown"
DEGRADED = "degraded"
STATES = (FRESH, STALE, UNKNOWN, DEGRADED)

SOURCE_PROVENANCE_STALENESS = "provenance_status._staleness"
SOURCE_STALE_BY_SCRIPT_STATE = "provenance_status.stale_by_script"
SOURCE_CHECK_STALENESS = "server.check_staleness"
SOURCE_FIND_STALE_BY_SCRIPT = "server.find_stale_by_script"
SOURCES = (
    SOURCE_PROVENANCE_STALENESS,
    SOURCE_STALE_BY_SCRIPT_STATE,
    SOURCE_CHECK_STALENESS,
    SOURCE_FIND_STALE_BY_SCRIPT,
)

# Mirrors provenance_status.STALE_BY_SCRIPT. Kept as a literal so this module
# stays import-free; tests/test_staleness_vocab.py asserts the two are equal.
_STALE_BY_SCRIPT_TYPE = "stale_by_script"


@dataclass(frozen=True)
class StalenessRecord:
    """One source result, restated on the shared vocabulary."""

    source: str
    state: str
    reason: str
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "state": self.state,
            "reason": self.reason,
            "path": self.path,
        }


def map_staleness(source: str, result: Any, *, path: str | None = None) -> StalenessRecord:
    """Map one result from ``source`` onto the shared vocabulary.

    The ``result`` shape depends on ``source``:

      * ``SOURCE_PROVENANCE_STALENESS``  -- the dict returned by
        ``provenance_status._staleness``.
      * ``SOURCE_STALE_BY_SCRIPT_STATE`` -- a ``get_provenance_status`` status
        dict (its ``provenance_type``, ``script_staleness`` and ``inconclusive``
        keys are read).
      * ``SOURCE_CHECK_STALENESS``       -- one ``fingerprint.StalenessResult``
        dict, as ``server.check_staleness`` returns it.
      * ``SOURCE_FIND_STALE_BY_SCRIPT``  -- one output path string, as
        ``server.find_stale_by_script`` returns it. That tool lists only stale
        paths, so every path it returns maps to ``stale``.

    ``path`` overrides the path recorded on the result. Raises ``ValueError``
    for an unknown ``source``.
    """
    if source == SOURCE_PROVENANCE_STALENESS:
        return _map_provenance_staleness(result, path)
    if source == SOURCE_STALE_BY_SCRIPT_STATE:
        return _map_stale_by_script_state(result, path)
    if source == SOURCE_CHECK_STALENESS:
        return _map_script_check(SOURCE_CHECK_STALENESS, result, path)
    if source == SOURCE_FIND_STALE_BY_SCRIPT:
        return StalenessRecord(
            source=SOURCE_FIND_STALE_BY_SCRIPT,
            state=STALE,
            reason=(
                "generating script's current content hash differs from the "
                "hash recorded when this output was tagged"
            ),
            path=path if path is not None else str(result),
        )
    raise ValueError(f"unknown staleness source: {source!r}")


def _path_of(result: dict[str, Any], path: str | None) -> str | None:
    if path is not None:
        return path
    value = result.get("path")
    return None if value is None else str(value)


def _map_provenance_staleness(block: dict[str, Any], path: str | None) -> StalenessRecord:
    """``provenance_status._staleness`` -- content and existence check."""
    source = SOURCE_PROVENANCE_STALENESS
    resolved = path if path is not None else _path_of(block, None)
    reason = str(block.get("reason") or "")
    if block.get("stale"):
        return StalenessRecord(source, STALE, reason, resolved)
    # A "not stale" answer is only a confirmation when both hashes exist.
    # Without a recorded hash or a current hash, the source itself says the
    # question cannot be settled, so the record is unknown.
    if block.get("recorded_content_hash") is None or block.get("current_content_hash") is None:
        return StalenessRecord(
            source,
            UNKNOWN,
            reason or "staleness cannot be confirmed either way",
            resolved,
        )
    return StalenessRecord(source, FRESH, reason, resolved)


def _map_stale_by_script_state(status: dict[str, Any], path: str | None) -> StalenessRecord:
    """``stale_by_script`` provenance state, read from a status dict."""
    source = SOURCE_STALE_BY_SCRIPT_STATE
    resolved = path if path is not None else _path_of(status, None)
    script = status.get("script_staleness")
    if status.get("provenance_type") == _STALE_BY_SCRIPT_TYPE:
        reason = ""
        if isinstance(script, dict):
            reason = str(script.get("reason") or "")
        return StalenessRecord(
            source,
            STALE,
            reason or "generating script changed since tagging",
            resolved,
        )
    if script is None:
        # Never fingerprint-tagged. That is not the same as "not stale".
        if status.get("inconclusive"):
            return StalenessRecord(
                source,
                DEGRADED,
                "output has no generating-script tag, and the outputs index "
                "has not converged, so this absence is not confirmed",
                resolved,
            )
        return StalenessRecord(
            source,
            UNKNOWN,
            "output was never fingerprint-tagged; no generating-script hash to compare",
            resolved,
        )
    return _map_script_check(source, script, resolved)


def _map_script_check(source: str, result: dict[str, Any], path: str | None) -> StalenessRecord:
    """Script-hash check: ``fingerprint.StalenessResult`` as a dict."""
    resolved = _path_of(result, path)
    reason = str(result.get("reason") or "")
    if result.get("is_stale"):
        return StalenessRecord(source, STALE, reason, resolved)
    if result.get("script_path") is None:
        # The tag was written without a resolvable generating script.
        return StalenessRecord(source, UNKNOWN, reason, resolved)
    if result.get("tagged_script_hash") is None:
        # The check reports "unchanged", but nothing was recorded to compare
        # against. Keep the assurance from being read as verified.
        return StalenessRecord(
            source,
            DEGRADED,
            "no tagged script hash on record, so 'unchanged' is not verified: "
            + reason,
            resolved,
        )
    return StalenessRecord(source, FRESH, reason, resolved)
