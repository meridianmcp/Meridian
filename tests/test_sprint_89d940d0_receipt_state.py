"""W31-D (89d940d0): explicit promotion-receipt state for partial Outputs indexing.

Every evidence record built from ``meridian_outputs.provenance_status`` now
carries an explicit ``receipt_state`` (exact / fallback / ambiguous / partial /
unregistered / unknown / unavailable) and a ``clean_provenance`` flag. The flag
is True only for an exact, non-stale record with both content hashes confirmed,
backed by a converged index. An index-dependent answer reached while the outputs
index is still converging, or while it cannot be read at all, is flagged partial
and is never presented as a VERIFIED resolver result.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_EXT = Path(__file__).resolve().parent.parent / "extensions" / "meridian-outputs"
if str(_EXT) not in sys.path:
    sys.path.insert(0, str(_EXT))

from meridian_outputs import annotate as AN  # noqa: E402
from meridian_outputs import outputs_local as OL  # noqa: E402
from meridian_outputs import provenance_status as PS  # noqa: E402
from meridian_outputs import research_evidence as RE  # noqa: E402

try:
    import duckdb as _duckdb_probe  # noqa: F401

    _DUCKDB_AVAILABLE = True
except ImportError:
    _DUCKDB_AVAILABLE = False

duckdb_required = pytest.mark.skipif(
    not _DUCKDB_AVAILABLE, reason="duckdb not installed"
)

_ISO = "2026-10-01T00:00:00+00:00"


def _status(
    ptype: str,
    *,
    converged: "bool | None",
    relocation_evidence: "str | None" = None,
    stale: bool = False,
) -> dict[str, Any]:
    """Synthetic ``get_provenance_status``-shaped dict (no filesystem access).

    ``converged=None`` models an unreadable outputs index (``convergence`` is
    ``None``), which is what ``get_indexed_output_status`` reports when the
    outputs directory itself is missing or invalid.
    """
    convergence = None if converged is None else {"converged": converged}
    has_record = ptype in (PS.EXACT, PS.STALE_BY_SCRIPT, PS.RELOCATED, PS.AMBIGUOUS)
    record = (
        {"path": "/outputs/x.csv", "content_hash": "abc", "recorded_at_iso": _ISO}
        if has_record and ptype != PS.AMBIGUOUS
        else None
    )
    directory_note = (
        {"path": "/outputs/MERIDIAN_NOTES.md", "created_at": _ISO}
        if ptype == PS.DIRECTORY_FALLBACK
        else None
    )
    staleness = None
    if ptype in (PS.EXACT, PS.STALE_BY_SCRIPT):
        staleness = {
            "exists_on_disk": True,
            "recorded_content_hash": "abc",
            "current_content_hash": "abc",
            "stale": stale,
            "reason": "synthetic",
        }
    return {
        "path": "/outputs/x.csv",
        "provenance_type": ptype,
        "record": record,
        "directory_note": directory_note,
        "staleness": staleness,
        "script_staleness": None,
        "archival": None,
        "convergence": convergence,
        "inconclusive": ptype == PS.UNKNOWN and converged is not True,
        "relocation_evidence": relocation_evidence,
        "candidates": (
            [{"path": "/outputs/a.csv"}, {"path": "/outputs/b.csv"}]
            if ptype == PS.AMBIGUOUS
            else None
        ),
    }


def _record(status: dict[str, Any]) -> RE.EvidenceRecord:
    return PS.evidence_record_from_provenance_status(status)


# ---------------------------------------------------------------------------
# State matrix over synthetic statuses
# ---------------------------------------------------------------------------


class TestReceiptStateMatrix:
    def test_exact_converged_fresh_is_clean(self) -> None:
        rec = _record(_status(PS.EXACT, converged=True))
        assert rec.attributes["receipt_state"] == "exact"
        assert rec.attributes["clean_provenance"] is True
        assert rec.partial is False
        assert rec.resolver.status is RE.ResolverStatus.VERIFIED

    def test_exact_unconverged_index_is_exact_but_never_clean(self) -> None:
        rec = _record(_status(PS.EXACT, converged=False))
        assert rec.attributes["receipt_state"] == "exact"
        assert rec.attributes["clean_provenance"] is False
        # Existing contract: an exact record is not made partial by convergence.
        assert rec.partial is False
        assert rec.resolver.status is RE.ResolverStatus.VERIFIED

    def test_exact_unavailable_index_is_never_clean(self) -> None:
        rec = _record(_status(PS.EXACT, converged=None))
        assert rec.attributes["receipt_state"] == "exact"
        assert rec.attributes["clean_provenance"] is False

    def test_exact_stale_is_never_clean(self) -> None:
        rec = _record(_status(PS.EXACT, converged=True, stale=True))
        assert rec.attributes["receipt_state"] == "exact"
        assert rec.attributes["clean_provenance"] is False
        assert rec.resolver.status is RE.ResolverStatus.STALE

    def test_stale_by_script_is_exact_but_never_clean(self) -> None:
        rec = _record(_status(PS.STALE_BY_SCRIPT, converged=True))
        assert rec.attributes["receipt_state"] == "exact"
        assert rec.attributes["clean_provenance"] is False

    def test_directory_fallback_is_fallback(self) -> None:
        rec = _record(_status(PS.DIRECTORY_FALLBACK, converged=True))
        assert rec.attributes["receipt_state"] == "fallback"
        assert rec.attributes["clean_provenance"] is False
        assert rec.partial is False

    def test_content_hash_relocation_stays_fallback_even_unconverged(self) -> None:
        # Content-hash matching uses the annotate ledger, not the walk, so an
        # unconverged index does not make it partial (existing contract kept).
        rec = _record(
            _status(PS.RELOCATED, converged=False, relocation_evidence="content_hash")
        )
        assert rec.attributes["receipt_state"] == "fallback"
        assert rec.attributes["clean_provenance"] is False
        assert rec.partial is False
        assert rec.resolver.status is RE.ResolverStatus.VERIFIED

    def test_basename_relocation_converged_is_fallback(self) -> None:
        rec = _record(
            _status(PS.RELOCATED, converged=True, relocation_evidence="basename")
        )
        assert rec.attributes["receipt_state"] == "fallback"
        assert rec.attributes["clean_provenance"] is False
        assert rec.partial is False

    def test_basename_relocation_unconverged_is_partial_not_verified(self) -> None:
        rec = _record(
            _status(PS.RELOCATED, converged=False, relocation_evidence="basename")
        )
        assert rec.attributes["receipt_state"] == "partial"
        assert rec.attributes["clean_provenance"] is False
        assert rec.partial is True
        assert rec.partial_reason and "not converged" in rec.partial_reason
        assert rec.resolver.status is RE.ResolverStatus.AMBIGUOUS
        assert rec.resolver.confidence <= 0.5

    def test_basename_relocation_unavailable_index_is_unavailable(self) -> None:
        rec = _record(
            _status(PS.RELOCATED, converged=None, relocation_evidence="basename")
        )
        assert rec.attributes["receipt_state"] == "unavailable"
        assert rec.partial is True
        assert rec.resolver.status is RE.ResolverStatus.AMBIGUOUS

    def test_ambiguous_is_ambiguous_and_partial(self) -> None:
        rec = _record(_status(PS.AMBIGUOUS, converged=True))
        assert rec.attributes["receipt_state"] == "ambiguous"
        assert rec.attributes["clean_provenance"] is False
        assert rec.partial is True

    def test_unregistered_converged_is_unregistered(self) -> None:
        rec = _record(_status(PS.UNREGISTERED, converged=True))
        assert rec.attributes["receipt_state"] == "unregistered"
        assert rec.attributes["clean_provenance"] is False
        assert rec.partial is True

    def test_unregistered_unconverged_is_partial(self) -> None:
        # A directory note the walk has not ingested yet could still cover it.
        rec = _record(_status(PS.UNREGISTERED, converged=False))
        assert rec.attributes["receipt_state"] == "partial"
        assert rec.attributes["clean_provenance"] is False
        assert rec.partial is True
        assert rec.resolver.status is RE.ResolverStatus.HELD

    def test_unknown_converged_is_unknown(self) -> None:
        rec = _record(_status(PS.UNKNOWN, converged=True))
        assert rec.attributes["receipt_state"] == "unknown"
        assert rec.attributes["clean_provenance"] is False
        assert rec.partial is True
        assert rec.resolver.status is RE.ResolverStatus.UNAVAILABLE

    def test_unknown_unconverged_is_partial_and_inconclusive(self) -> None:
        rec = _record(_status(PS.UNKNOWN, converged=False))
        assert rec.attributes["receipt_state"] == "partial"
        assert rec.partial is True
        assert rec.resolver.status is RE.ResolverStatus.AMBIGUOUS

    def test_unknown_unavailable_index_is_unavailable(self) -> None:
        rec = _record(_status(PS.UNKNOWN, converged=None))
        assert rec.attributes["receipt_state"] == "unavailable"
        assert rec.partial is True
        assert rec.resolver.status is RE.ResolverStatus.AMBIGUOUS

    def test_receipt_state_survives_into_attributes_losslessly(self) -> None:
        rec = _record(_status(PS.EXACT, converged=False))
        assert "receipt_state" in rec.attributes
        assert "clean_provenance" in rec.attributes
        assert rec.attributes["provenance_type"] == PS.EXACT


class TestCleanProvenanceRequiresConfirmedHashes:
    """An EXACT record whose staleness cannot be confirmed (a recorded or
    current content hash is None) resolves AMBIGUOUS. ``clean_provenance`` must
    agree with that: an unconfirmed hash is never a clean verdict, even on a
    converged index with no staleness flagged.
    """

    @pytest.mark.parametrize(
        "unconfirmed",
        [
            {"recorded_content_hash": None},
            {"current_content_hash": None},
            {"recorded_content_hash": None, "current_content_hash": None},
        ],
        ids=["recorded_hash_none", "current_hash_none", "both_hashes_none"],
    )
    def test_unconfirmed_hash_is_never_clean(
        self, unconfirmed: dict[str, Any],
    ) -> None:
        status = _status(PS.EXACT, converged=True)
        status["staleness"].update(unconfirmed)
        rec = _record(status)

        assert rec.resolver.status is RE.ResolverStatus.AMBIGUOUS
        assert rec.attributes["receipt_state"] == "exact"
        assert rec.attributes["clean_provenance"] is False
        assert rec.is_authoritative is False

    def test_missing_staleness_block_is_never_clean(self) -> None:
        status = _status(PS.EXACT, converged=True)
        status["staleness"] = None
        rec = _record(status)

        assert rec.resolver.status is RE.ResolverStatus.AMBIGUOUS
        assert rec.attributes["clean_provenance"] is False
        assert rec.is_authoritative is False

    def test_both_hashes_confirmed_and_fresh_stays_clean(self) -> None:
        # Control: the hash guard must not over-reject a fully confirmed record.
        status = _status(PS.EXACT, converged=True)
        assert status["staleness"]["recorded_content_hash"] is not None
        assert status["staleness"]["current_content_hash"] is not None
        rec = _record(status)

        assert rec.attributes["clean_provenance"] is True
        assert rec.is_authoritative is True


# ---------------------------------------------------------------------------
# Real filesystem end-to-end (duckdb-backed outputs index)
# ---------------------------------------------------------------------------


@duckdb_required
class TestReceiptStateOnRealIndex:
    def test_exact_record_on_converged_index_is_clean(self, tmp_path: Path) -> None:
        f = tmp_path / "results.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(f))
        OL._get_cached_index(str(tmp_path)).rebuild()

        status = PS.get_provenance_status(str(tmp_path), str(f))
        assert status["convergence"]["converged"] is True
        rec = PS.evidence_record_from_provenance_status(status)

        assert rec.attributes["receipt_state"] == "exact"
        assert rec.attributes["clean_provenance"] is True
        assert rec.partial is False

    def test_exact_record_on_unconverged_index_is_not_clean(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "results.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        for i in range(25):
            (tmp_path / f"bulk_{i:03d}.csv").write_text(f"{i}\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(f))
        # A zero-budget rebuild leaves the walk genuinely in progress (the same
        # setup test_provenance_status uses). The exact annotate record is real,
        # but the index cannot back a clean verdict until it converges.
        idx = OL._get_cached_index(str(tmp_path))
        idx.rebuild(max_seconds=0)
        assert idx.get_convergence_state().converged is False  # sanity

        status = PS.get_provenance_status(str(tmp_path), str(f))
        assert status["convergence"]["converged"] is False
        rec = PS.evidence_record_from_provenance_status(status)

        assert rec.attributes["receipt_state"] == "exact"
        assert rec.attributes["clean_provenance"] is False
        assert rec.partial is False
        assert rec.resolver.status is RE.ResolverStatus.VERIFIED

    def test_content_hash_relocation_on_unconverged_index_is_fallback(
        self, tmp_path: Path,
    ) -> None:
        old = tmp_path / "before.csv"
        old.write_text("a,b\n1,2\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(old))
        new = tmp_path / "after.csv"
        new.write_text("a,b\n1,2\n", encoding="utf-8")
        old.unlink()

        status = PS.get_provenance_status(str(tmp_path), str(new))
        assert status["provenance_type"] == PS.RELOCATED
        rec = PS.evidence_record_from_provenance_status(status)

        assert rec.attributes["receipt_state"] == "fallback"
        assert rec.attributes["clean_provenance"] is False
        assert rec.partial is False

    def test_missing_outputs_dir_is_unavailable_not_absent(
        self, tmp_path: Path,
    ) -> None:
        missing_dir = tmp_path / "no_such_outputs"
        target = missing_dir / "figure.csv"

        status = PS.get_provenance_status(str(missing_dir), str(target))
        assert status["convergence"] is None
        rec = PS.evidence_record_from_provenance_status(status)

        assert rec.attributes["receipt_state"] == "unavailable"
        assert rec.attributes["clean_provenance"] is False
        assert rec.partial is True
        assert rec.resolver.status is not RE.ResolverStatus.VERIFIED
