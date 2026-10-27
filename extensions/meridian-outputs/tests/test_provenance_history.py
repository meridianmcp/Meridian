"""Version history for record_provenance (annotate.get_provenance_history).

Re-recording a path used to overwrite its only ledger entry. Now the
superseded record moves into a bounded ``history`` list inside the same
ledger entry, written by the same atomic replace, and get_provenance /
list_provenance keep their old shape unless asked for history.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import annotate as AN
from meridian_outputs import outputs_local as OL

_RECORD_FIELDS = {f.name for f in dataclasses.fields(AN.ProvenanceRecord)}


@pytest.fixture(autouse=True)
def _no_fast_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """The best-effort DuckDB priority registration is not under test."""
    monkeypatch.setattr(OL, "register_priority_path", lambda *a, **k: None)


def _ledger(outputs: Path) -> dict:
    path = outputs / ".meridian-outputs-cache" / "provenance_ledger.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestVersioning:
    def test_first_record_is_version_one_with_empty_history(self, tmp_path: Path) -> None:
        out = _write(tmp_path / "a.csv", "x\n1\n")
        result = AN.record_provenance(str(tmp_path), str(out), note="first")
        assert result["version"] == 1

        entry = _ledger(tmp_path)["a.csv"]
        assert entry["history"] == []
        assert entry["history_dropped"] == 0

        hist = AN.get_provenance_history(str(tmp_path), str(out))
        assert hist["current_version"] == 1
        assert hist["history_dropped"] == 0
        assert hist["history_limit"] == 50
        assert [v["version"] for v in hist["versions"]] == [1]
        assert hist["versions"][0]["is_current"] is True
        assert hist["versions"][0]["note"] == "first"
        assert hist["path"] == str(out.resolve())

    def test_rerecord_keeps_the_prior_record_in_history(self, tmp_path: Path) -> None:
        script = _write(tmp_path / "gen.py", "print('v1')\n")
        out = _write(tmp_path / "a.csv", "x\n1\n")
        first = AN.record_provenance(
            str(tmp_path), str(out), generating_script=str(script), params={"lr": 0.1},
        )
        script.write_text("print('v2')\n", encoding="utf-8")
        out.write_text("x\n2\n", encoding="utf-8")
        second = AN.record_provenance(
            str(tmp_path), str(out), generating_script=str(script), params={"lr": 0.2},
        )
        assert second["version"] == 2
        assert first["content_hash"] != second["content_hash"]
        assert first["script_content_hash"] != second["script_content_hash"]

        prior = _ledger(tmp_path)["a.csv"]["history"][0]
        assert prior["content_hash"] == first["content_hash"]
        assert prior["params"] == {"lr": 0.1}
        assert prior["script_content_hash"] == first["script_content_hash"]
        assert "history" not in prior and "history_dropped" not in prior

        current = AN.get_provenance(str(tmp_path), str(out))
        assert current["params"] == {"lr": 0.2}
        assert current["content_hash"] == second["content_hash"]
        assert "history" not in current and "history_dropped" not in current

    def test_three_records_are_versions_one_to_three_oldest_first(self, tmp_path: Path) -> None:
        out = _write(tmp_path / "a.csv", "x\n1\n")
        for note in ("n1", "n2", "n3"):
            AN.record_provenance(str(tmp_path), str(out), note=note)

        hist = AN.get_provenance_history(str(tmp_path), str(out))
        assert [v["version"] for v in hist["versions"]] == [1, 2, 3]
        assert [v["note"] for v in hist["versions"]] == ["n1", "n2", "n3"]
        assert [v["is_current"] for v in hist["versions"]] == [False, False, True]
        assert hist["current_version"] == 3
        assert AN.get_provenance(str(tmp_path), str(out))["note"] == "n3"

    def test_identical_rerecord_still_creates_a_version(self, tmp_path: Path) -> None:
        out = _write(tmp_path / "a.csv", "x\n1\n")
        AN.record_provenance(str(tmp_path), str(out), note="same")
        again = AN.record_provenance(str(tmp_path), str(out), note="same")
        assert again["version"] == 2
        assert len(_ledger(tmp_path)["a.csv"]["history"]) == 1


class TestRetentionCap:
    def test_cap_exceeded_drops_oldest_and_counts_them(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(AN, "_HISTORY_LIMIT", 3)
        out = _write(tmp_path / "a.csv", "x\n1\n")
        results = [
            AN.record_provenance(str(tmp_path), str(out), note=f"n{i}") for i in range(1, 7)
        ]
        assert [r["version"] for r in results] == [1, 2, 3, 4, 5, 6]

        entry = _ledger(tmp_path)["a.csv"]
        assert len(entry["history"]) == 3
        assert entry["history_dropped"] == 2
        assert entry["note"] == "n6"  # current record intact

        hist = AN.get_provenance_history(str(tmp_path), str(out))
        assert [v["version"] for v in hist["versions"]] == [3, 4, 5, 6]
        assert [v["note"] for v in hist["versions"]] == ["n3", "n4", "n5", "n6"]
        assert hist["history_dropped"] == 2
        assert hist["history_limit"] == 3

    def test_default_cap_is_fifty(self, tmp_path: Path) -> None:
        out = _write(tmp_path / "a.csv", "x\n1\n")
        for i in range(52):
            AN.record_provenance(str(tmp_path), str(out), note=f"n{i}")
        entry = _ledger(tmp_path)["a.csv"]
        assert len(entry["history"]) == 50
        assert entry["history_dropped"] == 1
        assert entry["history"][0]["note"] == "n1"
        assert AN.get_provenance_history(str(tmp_path), str(out))["current_version"] == 52


class TestLegacyAndSpellings:
    def test_legacy_absolute_row_becomes_history_zero_under_relative_key(
        self, tmp_path: Path,
    ) -> None:
        outputs = tmp_path / "outputs"
        cache = outputs / ".meridian-outputs-cache"
        cache.mkdir(parents=True)
        legacy = _write(outputs / "legacy.csv", "x\n1\n")
        legacy_abs = str(legacy.resolve())
        legacy_key = AN._normalize_path(legacy_abs)
        (cache / "provenance_ledger.json").write_text(
            json.dumps({legacy_key: {
                "path": legacy_abs, "generating_script": None,
                "params": {"old": True}, "note": "legacy",
                "recorded_at": 1.0, "recorded_at_iso": "1970-01-01T00:00:01+00:00",
                "content_hash": None,
            }}),
            encoding="utf-8",
        )

        result = AN.record_provenance(str(outputs), str(legacy), note="new")
        assert result["version"] == 2

        ledger = _ledger(outputs)
        assert list(ledger) == ["legacy.csv"]
        assert ledger["legacy.csv"]["history"][0]["path"] == "legacy.csv"
        assert ledger["legacy.csv"]["history"][0]["note"] == "legacy"
        assert json.dumps(str(tmp_path))[1:-1] not in json.dumps(ledger)  # no machine path

        hist = AN.get_provenance_history(str(outputs), str(legacy))
        assert [(v["version"], v["note"]) for v in hist["versions"]] == [(1, "legacy"), (2, "new")]
        assert hist["versions"][0]["path"] == legacy_abs

    def test_legacy_row_without_history_reads_as_single_version(self, tmp_path: Path) -> None:
        cache = tmp_path / ".meridian-outputs-cache"
        cache.mkdir()
        out = _write(tmp_path / "a.csv", "x\n1\n")
        (cache / "provenance_ledger.json").write_text(
            json.dumps({"a.csv": {"path": "a.csv", "note": "old", "params": {}}}),
            encoding="utf-8",
        )
        hist = AN.get_provenance_history(str(tmp_path), str(out))
        assert hist["current_version"] == 1
        assert hist["versions"][0]["note"] == "old"
        assert "history" not in AN.get_provenance(str(tmp_path), str(out))

    def test_relative_and_absolute_spellings_share_one_chain(self, tmp_path: Path) -> None:
        out = _write(tmp_path / "sub" / "out.csv", "x\n1\n")
        spellings = [(str(out), "absolute"), ("sub/out.csv", "relative")]
        if os.sep == "\\":
            spellings.append(("sub\\out.csv", "backslash"))
        for spelling, note in spellings:
            AN.record_provenance(str(tmp_path), spelling, note=note)

        assert len(AN.list_provenance(str(tmp_path))) == 1
        hist = AN.get_provenance_history(str(tmp_path), "sub/out.csv")
        assert [v["note"] for v in hist["versions"]] == [note for _, note in spellings]
        assert AN.get_provenance_history(str(tmp_path), str(out)) == hist

    def test_unknown_or_outside_path_has_no_history(self, tmp_path: Path) -> None:
        outputs = tmp_path / "outputs"
        outputs.mkdir()
        assert AN.get_provenance_history(str(outputs), str(outputs / "never.csv")) is None
        assert AN.get_provenance_history(str(outputs), str(tmp_path / "outside.csv")) is None
        assert AN.get_provenance_history(str(outputs), "") is None


class TestAtomicityAndShape:
    def test_replace_failure_leaves_ledger_and_history_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        out = _write(tmp_path / "a.csv", "x\n1\n")
        AN.record_provenance(str(tmp_path), str(out), note="n1")
        AN.record_provenance(str(tmp_path), str(out), note="n2")
        ledger_file = tmp_path / ".meridian-outputs-cache" / "provenance_ledger.json"
        before = ledger_file.read_bytes()

        def _boom(src: str, dst: str) -> None:
            raise OSError("simulated replace failure")

        with monkeypatch.context() as m:
            m.setattr(os, "replace", _boom)
            result = AN.record_provenance(str(tmp_path), str(out), note="n3")

        assert "error" in result
        assert ledger_file.read_bytes() == before
        hist = AN.get_provenance_history(str(tmp_path), str(out))
        assert [v["note"] for v in hist["versions"]] == ["n1", "n2"]

    def test_list_and_get_keep_their_shape_across_a_multi_path_ledger(
        self, tmp_path: Path,
    ) -> None:
        a = _write(tmp_path / "a.csv", "x\n1\n")
        b = _write(tmp_path / "b.csv", "x\n2\n")
        AN.record_provenance(str(tmp_path), str(a), note="a1")
        AN.record_provenance(str(tmp_path), str(b), note="b1")
        AN.record_provenance(str(tmp_path), str(a), note="a2")

        records = AN.list_provenance(str(tmp_path))
        assert [os.path.basename(r["path"]) for r in records] == ["a.csv", "b.csv"]
        for record in records:
            assert set(record) == _RECORD_FIELDS
        assert set(AN.get_provenance(str(tmp_path), str(a))) == _RECORD_FIELDS
        assert records[0]["note"] == "a2"

    def test_include_history_exposes_resolved_snapshots(self, tmp_path: Path) -> None:
        a = _write(tmp_path / "a.csv", "x\n1\n")
        AN.record_provenance(str(tmp_path), str(a), note="a1")
        AN.record_provenance(str(tmp_path), str(a), note="a2")

        full = AN.get_provenance(str(tmp_path), str(a), include_history=True)
        assert full["history_dropped"] == 0
        assert [h["note"] for h in full["history"]] == ["a1"]
        assert full["history"][0]["path"] == str(a.resolve())
        listed = AN.list_provenance(str(tmp_path), include_history=True)
        assert listed[0]["history"] == full["history"]

    def test_history_parts_tolerates_malformed_values(self) -> None:
        assert AN._history_parts({"history": "nope", "history_dropped": -4}) == ([], 0)
        assert AN._history_parts({"history": [1, {"note": "ok"}], "history_dropped": True}) == (
            [{"note": "ok"}], 0,
        )


class TestServerWrapper:
    def test_mcp_tool_returns_the_annotate_history(self, tmp_path: Path) -> None:
        pytest.importorskip("mcp")
        from meridian_outputs import server

        out = _write(tmp_path / "a.csv", "x\n1\n")
        server.record_provenance(str(tmp_path), str(out), note="n1")
        recorded = server.record_provenance(str(tmp_path), str(out), note="n2")
        assert recorded["version"] == 2
        via_tool = server.get_provenance_history(str(tmp_path), str(out))
        assert via_tool == AN.get_provenance_history(str(tmp_path), str(out))
        assert [v["note"] for v in via_tool["versions"]] == ["n1", "n2"]
        assert "history" not in server.get_provenance(str(tmp_path), str(out))
