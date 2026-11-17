"""Tests for the shared staleness vocabulary (sprint item oz-outputs-staleness-vocabulary).

Covers:
  - test_every_source_maps: each of the four mapped sources, given a
    representative result, maps to a record whose state is one of
    fresh / stale / unknown / degraded and whose reason is non-empty.
  - Per-source state rules: provenance_status._staleness, the stale_by_script
    state, the check_staleness script-hash check, and find_stale_by_script.
  - get_provenance_status: the additive ``staleness_record`` envelope is
    present, and every pre-existing key keeps its name.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import annotate as AN  # noqa: E402
from meridian_outputs import provenance_status as PS  # noqa: E402
from meridian_outputs import staleness as SV  # noqa: E402

try:
    import duckdb  # noqa: F401

    _DUCKDB_AVAILABLE = True
except ImportError:  # pragma: no cover - environment dependent
    _DUCKDB_AVAILABLE = False

duckdb_required = pytest.mark.skipif(
    not _DUCKDB_AVAILABLE, reason="duckdb not installed"
)


# ---------------------------------------------------------------------------
# Representative source results
# ---------------------------------------------------------------------------

PROV_STALE = {
    "exists_on_disk": True,
    "recorded_content_hash": "aa",
    "current_content_hash": "bb",
    "stale": True,
    "reason": "current content hash differs from the hash recorded at provenance time -- the file has changed since",
}
PROV_FRESH = {
    "exists_on_disk": True,
    "recorded_content_hash": "aa",
    "current_content_hash": "aa",
    "stale": False,
    "reason": "content hash unchanged since provenance was recorded",
}
PROV_NO_RECORDED_HASH = {
    "exists_on_disk": True,
    "recorded_content_hash": None,
    "current_content_hash": "aa",
    "stale": False,
    "reason": "no content hash was captured when this record was made -- staleness cannot be confirmed either way",
}
PROV_UNHASHABLE = {
    "exists_on_disk": True,
    "recorded_content_hash": "aa",
    "current_content_hash": None,
    "stale": False,
    "reason": "path exists but its current content could not be hashed -- staleness cannot be confirmed either way",
}
PROV_EMPTY_REASON_UNCONFIRMED = {
    "exists_on_disk": None,
    "recorded_content_hash": None,
    "current_content_hash": None,
    "stale": False,
    "reason": "",
}

SCRIPT_STALE = {
    "path": "/o/x.csv",
    "script_path": "/s.py",
    "tagged_script_hash": "h1",
    "current_script_hash": "h2",
    "is_stale": True,
    "reason": "generating script content changed since tagging",
}
SCRIPT_FRESH = {
    "path": "/o/x.csv",
    "script_path": "/s.py",
    "tagged_script_hash": "h2",
    "current_script_hash": "h2",
    "is_stale": False,
    "reason": "generating script unchanged since tagging",
}
SCRIPT_NO_TAGGED_HASH = {
    "path": "/o/x.csv",
    "script_path": "/s.py",
    "tagged_script_hash": None,
    "current_script_hash": "h2",
    "is_stale": False,
    "reason": "generating script unchanged since tagging",
}
SCRIPT_NO_SCRIPT_RESOLVED = {
    "path": "/o/x.csv",
    "script_path": None,
    "tagged_script_hash": "h1",
    "current_script_hash": None,
    "is_stale": False,
    "reason": "no generating script resolved at tag time",
}

STATUS_STALE_BY_SCRIPT = {
    "path": "/o/x.csv",
    "provenance_type": "stale_by_script",
    "script_staleness": SCRIPT_STALE,
    "staleness": PROV_FRESH,
    "inconclusive": False,
}
STATUS_EXACT_SCRIPT_FRESH = {
    "path": "/o/x.csv",
    "provenance_type": "exact",
    "script_staleness": SCRIPT_FRESH,
    "staleness": PROV_FRESH,
    "inconclusive": False,
}
STATUS_UNTAGGED = {
    "path": "/o/y.csv",
    "provenance_type": "unregistered",
    "script_staleness": None,
    "staleness": None,
    "inconclusive": False,
}
STATUS_UNCONVERGED = {
    "path": "/o/z.csv",
    "provenance_type": "unknown",
    "script_staleness": None,
    "staleness": None,
    "inconclusive": True,
}


def _representatives() -> dict[str, object]:
    return {
        SV.SOURCE_PROVENANCE_STALENESS: PROV_STALE,
        SV.SOURCE_STALE_BY_SCRIPT_STATE: STATUS_STALE_BY_SCRIPT,
        SV.SOURCE_CHECK_STALENESS: SCRIPT_STALE,
        SV.SOURCE_FIND_STALE_BY_SCRIPT: "/o/x.csv",
    }


# ---------------------------------------------------------------------------
# Vocabulary and dispatch
# ---------------------------------------------------------------------------

def test_vocabulary_is_exactly_four_states() -> None:
    assert SV.STATES == ("fresh", "stale", "unknown", "degraded")


def test_every_source_maps() -> None:
    reps = _representatives()
    assert set(reps) == set(SV.SOURCES)
    for source in SV.SOURCES:
        record = SV.map_staleness(source, reps[source])
        assert record.source == source
        assert record.state in SV.STATES
        assert record.reason


def test_unknown_source_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown staleness source"):
        SV.map_staleness("not.a.source", {})


def test_record_to_dict_has_envelope_keys() -> None:
    record = SV.map_staleness(SV.SOURCE_FIND_STALE_BY_SCRIPT, "/o/x.csv")
    assert record.to_dict() == {
        "source": SV.SOURCE_FIND_STALE_BY_SCRIPT,
        "state": "stale",
        "reason": record.reason,
        "path": "/o/x.csv",
    }


def test_local_stale_by_script_literal_matches_provenance_status() -> None:
    assert SV._STALE_BY_SCRIPT_TYPE == PS.STALE_BY_SCRIPT


# ---------------------------------------------------------------------------
# provenance_status._staleness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("block", "expected"),
    [
        (PROV_STALE, "stale"),
        (PROV_FRESH, "fresh"),
        (PROV_NO_RECORDED_HASH, "unknown"),
        (PROV_UNHASHABLE, "unknown"),
    ],
)
def test_provenance_staleness_states(block: dict, expected: str) -> None:
    record = SV.map_staleness(SV.SOURCE_PROVENANCE_STALENESS, block)
    assert record.state == expected
    assert record.reason


def test_provenance_staleness_unconfirmed_without_reason_gets_default_reason() -> None:
    record = SV.map_staleness(SV.SOURCE_PROVENANCE_STALENESS, PROV_EMPTY_REASON_UNCONFIRMED)
    assert record.state == "unknown"
    assert record.reason == "staleness cannot be confirmed either way"


def test_provenance_staleness_path_override_is_recorded() -> None:
    record = SV.map_staleness(SV.SOURCE_PROVENANCE_STALENESS, PROV_STALE, path="/o/x.csv")
    assert record.path == "/o/x.csv"


# ---------------------------------------------------------------------------
# stale_by_script provenance state
# ---------------------------------------------------------------------------

def test_stale_by_script_state_is_stale_with_script_reason() -> None:
    record = SV.map_staleness(SV.SOURCE_STALE_BY_SCRIPT_STATE, STATUS_STALE_BY_SCRIPT)
    assert record.state == "stale"
    assert record.reason == SCRIPT_STALE["reason"]
    assert record.path == "/o/x.csv"


def test_stale_by_script_state_without_script_dict_gets_default_reason() -> None:
    record = SV.map_staleness(
        SV.SOURCE_STALE_BY_SCRIPT_STATE,
        {"path": "/o/x.csv", "provenance_type": "stale_by_script", "script_staleness": None},
    )
    assert record.state == "stale"
    assert record.reason == "generating script changed since tagging"


def test_stale_by_script_state_fresh_when_script_unchanged() -> None:
    record = SV.map_staleness(SV.SOURCE_STALE_BY_SCRIPT_STATE, STATUS_EXACT_SCRIPT_FRESH)
    assert record.state == "fresh"


def test_stale_by_script_state_untagged_is_unknown_not_fresh() -> None:
    record = SV.map_staleness(SV.SOURCE_STALE_BY_SCRIPT_STATE, STATUS_UNTAGGED)
    assert record.state == "unknown"


def test_stale_by_script_state_unconverged_absence_is_degraded() -> None:
    record = SV.map_staleness(SV.SOURCE_STALE_BY_SCRIPT_STATE, STATUS_UNCONVERGED)
    assert record.state == "degraded"
    assert "has not converged" in record.reason


# ---------------------------------------------------------------------------
# check_staleness script-hash check
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (SCRIPT_STALE, "stale"),
        (SCRIPT_FRESH, "fresh"),
        (SCRIPT_NO_TAGGED_HASH, "degraded"),
        (SCRIPT_NO_SCRIPT_RESOLVED, "unknown"),
        (
            {
                "path": "/o/x.csv",
                "script_path": "/s.py",
                "tagged_script_hash": "h1",
                "current_script_hash": None,
                "is_stale": True,
                "reason": "generating script no longer readable/present",
            },
            "stale",
        ),
    ],
)
def test_check_staleness_states(result: dict, expected: str) -> None:
    record = SV.map_staleness(SV.SOURCE_CHECK_STALENESS, result)
    assert record.state == expected
    assert record.reason


def test_check_staleness_degraded_reason_keeps_source_reason() -> None:
    record = SV.map_staleness(SV.SOURCE_CHECK_STALENESS, SCRIPT_NO_TAGGED_HASH)
    assert "not verified" in record.reason
    assert SCRIPT_NO_TAGGED_HASH["reason"] in record.reason


def test_check_staleness_path_comes_from_result_when_not_overridden() -> None:
    record = SV.map_staleness(SV.SOURCE_CHECK_STALENESS, SCRIPT_FRESH)
    assert record.path == "/o/x.csv"
    overridden = SV.map_staleness(SV.SOURCE_CHECK_STALENESS, SCRIPT_FRESH, path="/o/other.csv")
    assert overridden.path == "/o/other.csv"


def test_check_staleness_without_path_key_has_none_path() -> None:
    bare = {k: v for k, v in SCRIPT_FRESH.items() if k != "path"}
    record = SV.map_staleness(SV.SOURCE_CHECK_STALENESS, bare)
    assert record.path is None


# ---------------------------------------------------------------------------
# find_stale_by_script
# ---------------------------------------------------------------------------

def test_find_stale_by_script_maps_each_path_to_stale() -> None:
    record = SV.map_staleness(SV.SOURCE_FIND_STALE_BY_SCRIPT, "/o/x.csv")
    assert record.state == "stale"
    assert record.path == "/o/x.csv"
    assert "hash" in record.reason


def test_find_stale_by_script_path_override() -> None:
    record = SV.map_staleness(SV.SOURCE_FIND_STALE_BY_SCRIPT, "/o/x.csv", path="/o/override.csv")
    assert record.path == "/o/override.csv"


# ---------------------------------------------------------------------------
# get_provenance_status envelope (additive)
# ---------------------------------------------------------------------------

LEGACY_KEYS = {
    "path",
    "provenance_type",
    "record",
    "directory_note",
    "staleness",
    "script_staleness",
    "archival",
    "convergence",
    "inconclusive",
}


def test_envelope_for_unknown_path_keeps_legacy_keys(tmp_path: Path) -> None:
    status = PS.get_provenance_status(str(tmp_path), str(tmp_path / "never_seen.csv"))
    assert LEGACY_KEYS <= set(status)
    record = status["staleness_record"]
    assert record["source"] == SV.SOURCE_STALE_BY_SCRIPT_STATE
    assert record["state"] in ("unknown", "degraded")
    assert record["reason"]


@duckdb_required
def test_envelope_exact_fresh_and_stale(tmp_path: Path) -> None:
    f = tmp_path / "envelope_case.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    AN.record_provenance(str(tmp_path), str(f))

    fresh = PS.get_provenance_status(str(tmp_path), str(f))
    assert LEGACY_KEYS <= set(fresh)
    assert fresh["staleness_record"]["source"] == SV.SOURCE_PROVENANCE_STALENESS
    assert fresh["staleness_record"]["state"] == "fresh"

    f.write_text("a,b\n999,999\n", encoding="utf-8")
    stale = PS.get_provenance_status(str(tmp_path), str(f))
    assert stale["staleness"]["stale"] is True
    assert stale["staleness_record"]["state"] == "stale"
    assert stale["staleness_record"]["path"] == str(f)


def test_stale_by_script_status_envelope_is_stale() -> None:
    record = PS._staleness_record_for_status(STATUS_STALE_BY_SCRIPT)
    assert record.source == SV.SOURCE_STALE_BY_SCRIPT_STATE
    assert record.state == "stale"


def test_block_status_envelope_uses_provenance_source() -> None:
    record = PS._staleness_record_for_status(
        {"path": "/o/x.csv", "provenance_type": "exact", "staleness": PROV_NO_RECORDED_HASH},
    )
    assert record.source == SV.SOURCE_PROVENANCE_STALENESS
    assert record.state == "unknown"


def test_relocated_status_with_changed_script_reads_stale() -> None:
    """Content-hash tier only. A content-hash RELOCATED status's own staleness
    block compares the matched hash to itself, so it reads fresh. A changed
    generating script must still show, through the stale_by_script source,
    while provenance_type stays relocated. A basename RELOCATED status has no
    content hash, so its block reads unknown, not fresh. That case is covered
    in test_provenance_status.py."""
    status = {
        "path": "/o/new.csv",
        "provenance_type": "relocated",
        "relocation_evidence": "content_hash",
        "staleness": PROV_FRESH,
        "script_staleness": SCRIPT_STALE,
        "inconclusive": False,
    }
    record = PS._staleness_record_for_status(status)
    assert record.source == SV.SOURCE_STALE_BY_SCRIPT_STATE
    assert record.state == "stale"
    assert record.path == "/o/new.csv"


def test_relocated_status_with_fresh_script_reads_fresh() -> None:
    status = {
        "path": "/o/new.csv",
        "provenance_type": "relocated",
        "staleness": PROV_FRESH,
        "script_staleness": SCRIPT_FRESH,
        "inconclusive": False,
    }
    record = PS._staleness_record_for_status(status)
    assert record.source == SV.SOURCE_PROVENANCE_STALENESS
    assert record.state == "fresh"


def test_error_envelope_keeps_error_shape_without_staleness_record(tmp_path: Path) -> None:
    """An {"error": ...} response has no provenance state to map. It must come
    back unchanged, with no staleness_record whose reason would describe an
    output that was never looked up."""
    no_dir = PS.get_provenance_status("", str(tmp_path / "x.csv"))
    assert no_dir == {"error": "outputs_dir is required"}
    assert "staleness_record" not in no_dir

    no_path = PS.get_provenance_status(str(tmp_path), "")
    assert no_path == {"error": "path is required"}
    assert "staleness_record" not in no_path
