"""Input-change staleness (run_manifest.check_input_staleness).

A run manifest hashed its inputs at start but nothing ever compared them
again, so an output stayed "fresh" after one of its source inputs changed.
These tests pin the new check: a stat-first comparison against the
``input_stat`` baseline recorded at start (excluded from the identity hash),
re-hashing only on a stat mismatch or in strict mode, and the additive
``input_staleness`` key on ``get_provenance_status``.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import annotate as AN
from meridian_outputs import outputs_local as OL
from meridian_outputs import run_manifest as RM


@pytest.fixture(autouse=True)
def _no_fast_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """record_provenance's best-effort DuckDB registration is not under test."""
    monkeypatch.setattr(OL, "register_priority_path", lambda *a, **k: None)


def _no_git(argv: list[str]) -> None:
    return None


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _start(outputs: Path, run_id: str, inputs: list[str]) -> dict:
    return RM.start_run_manifest(
        str(outputs), run_id=run_id, command_name="build",
        input_paths=inputs, _git_runner=_no_git,
    )


def _by_input(result: dict) -> dict[str, str]:
    return {i["input"]: i["status"] for i in result["inputs"]}


class TestCheckInputStaleness:
    def test_edited_input_marks_the_runs_outputs_stale(self, tmp_path: Path) -> None:
        inp = _write(tmp_path / "data" / "in.csv", "a\n1\n")
        out1 = _write(tmp_path / "out1.csv", "r\n1\n")
        out2 = _write(tmp_path / "out2.csv", "r\n2\n")
        _start(tmp_path, "r1", [str(inp)])
        RM.finalize_run_manifest(str(tmp_path), "r1", output_paths=[str(out1), str(out2)])

        fresh = RM.check_input_staleness(str(tmp_path), "r1")
        assert fresh["stale"] is False
        assert fresh["stale_outputs"] == []
        assert _by_input(fresh) == {"data/in.csv": "unchanged"}

        inp.write_text("a\n1\n2\n", encoding="utf-8")
        result = RM.check_input_staleness(str(tmp_path), "r1")
        assert result["stale"] is True
        assert result["baseline"] == "stat_and_hash"
        assert _by_input(result) == {"data/in.csv": "changed"}
        assert result["stale_inputs"] == ["data/in.csv"]
        assert result["stale_outputs"] == sorted([str(out1), str(out2)])
        assert "data/in.csv (changed)" in result["reason"]

    def test_touched_input_with_identical_bytes_is_not_stale(self, tmp_path: Path) -> None:
        inp = _write(tmp_path / "in.csv", "a\n1\n")
        _start(tmp_path, "r1", [str(inp)])
        st = os.stat(inp)
        os.utime(inp, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))

        result = RM.check_input_staleness(str(tmp_path), "r1")
        assert result["stale"] is False
        assert result["hashed_count"] == 1
        assert result["inputs"][0]["status"] == "unchanged"
        assert "content hash unchanged" in result["inputs"][0]["reason"]

    def test_deleted_input_is_stale(self, tmp_path: Path) -> None:
        inp = _write(tmp_path / "in.csv", "a\n1\n")
        _start(tmp_path, "r1", [str(inp)])
        inp.unlink()
        result = RM.check_input_staleness(str(tmp_path), "r1")
        assert result["stale"] is True
        assert _by_input(result) == {"in.csv": "missing"}

    def test_input_absent_at_start_then_present_is_stale(self, tmp_path: Path) -> None:
        later = tmp_path / "later.csv"
        never = tmp_path / "never.csv"
        _start(tmp_path, "r1", [str(later), str(never)])
        assert RM.check_input_staleness(str(tmp_path), "r1")["stale"] is False

        _write(later, "a\n1\n")
        result = RM.check_input_staleness(str(tmp_path), "r1")
        assert result["stale"] is True
        assert _by_input(result) == {"later.csv": "appeared", "never.csv": "absent"}
        assert result["stale_inputs"] == ["later.csv"]

    def test_relative_and_absolute_spellings_resolve_identically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        outputs = tmp_path / "outputs"
        inp = _write(outputs / "data" / "in.csv", "a\n1\n")
        sibling = _write(tmp_path / "raw" / "src.csv", "s\n1\n")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)  # relative inputs must NOT resolve against the cwd

        rel = _start(outputs, "rel", ["data/in.csv", "../raw/src.csv"])
        absolute = _start(outputs, "abs", [str(inp), str(sibling)])
        both = _start(outputs, "both", ["data/in.csv", str(inp)])

        assert set(rel["input_stat"]) == set(absolute["input_stat"]) == {
            "data/in.csv", "../raw/src.csv",
        }
        assert rel["input_stat"]["data/in.csv"] == absolute["input_stat"]["data/in.csv"]
        assert list(both["input_stat"]) == ["data/in.csv"]
        assert rel["input_stat"]["data/in.csv"]["exists"] is True

        inp.write_text("a\n1\n2\n", encoding="utf-8")
        for run_id in ("rel", "abs", "both"):
            assert RM.check_input_staleness(str(outputs), run_id)["stale_inputs"] == ["data/in.csv"]

    def test_relative_input_is_stat_ed_and_hashed_as_one_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The staleness baseline (input_stat) stats and hashes ONE file, the
        input resolved against outputs_dir. The identity hash (file_hashes)
        keeps its original meaning, the path as given against the working
        directory (S-F5: changing it broke resuming existing runs)."""
        outputs = tmp_path / "outputs"
        inp = _write(outputs / "data" / "in.csv", "a\n1\n")
        cwd_copy = _write(tmp_path / "cwd" / "data" / "in.csv", "same name, different file\n")
        monkeypatch.chdir(tmp_path / "cwd")

        rec = _start(outputs, "r1", ["data/in.csv"])
        expected = RM.fingerprint.script_content_hash(str(inp))
        assert rec["input_stat"]["data/in.csv"]["sha256"] == expected
        assert rec["input_identity"]["file_hashes"] == {
            "data/in.csv": RM.fingerprint.script_content_hash(str(cwd_copy)),
        }
        assert RM.check_input_staleness(str(outputs), "r1")["stale"] is False
        assert _start(outputs, "r1", ["data/in.csv"])["manifest_hash"] == rec["manifest_hash"]

        inp.write_text("a\n1\n2\n", encoding="utf-8")
        result = RM.check_input_staleness(str(outputs), "r1")
        assert result["stale"] is True
        assert _by_input(result) == {"data/in.csv": "changed"}

    def test_run_recorded_with_working_directory_identity_still_resumes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """S-F5: a run started before input_stat existed recorded file_hashes
        as script_content_hash of each path as given (relative to the working
        directory), and that value is inside manifest_hash. Restarting it
        must return the stored record, not raise 'DIFFERENT hash'; staleness
        still follows the real input under outputs_dir."""
        outputs = tmp_path / "outputs"
        inp = _write(outputs / "data" / "in.csv", "a\n1\n")
        cwd_copy = _write(tmp_path / "cwd" / "data" / "in.csv", "same name, different file\n")
        monkeypatch.chdir(tmp_path / "cwd")

        # The record as the pre-input_stat code wrote it: identity hashes of
        # the path as given, no input_stat.
        fresh = _start(outputs, "old-run", ["data/in.csv"])
        old_hashes = {"data/in.csv": RM.fingerprint.script_content_hash(str(cwd_copy))}
        old = {k: v for k, v in fresh.items() if k != "input_stat"}
        old["input_identity"] = {
            "file_hashes": old_hashes, "file_set_hash": RM._file_set_hash(old_hashes),
        }
        old["manifest_hash"] = RM.run_manifest_hash(old)
        ledger_file = outputs / ".meridian-outputs-cache" / "run_manifest_ledger.json"
        ledger_file.write_text(json.dumps({"old-run": old}), encoding="utf-8")

        resumed = _start(outputs, "old-run", ["data/in.csv"])
        assert resumed == json.loads(json.dumps(old))  # the stored record, unchanged
        assert resumed["manifest_hash"] == old["manifest_hash"]

        # A run started now from the same working directory: its staleness
        # baseline is the real input under outputs_dir, checked from anywhere.
        _start(outputs, "new-run", ["data/in.csv"])
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        assert RM.check_input_staleness(str(outputs), "new-run")["stale"] is False
        inp.write_text("a\n1\n2\n", encoding="utf-8")
        result = RM.check_input_staleness(str(outputs), "new-run")
        assert result["stale"] is True
        assert _by_input(result) == {"data/in.csv": "changed"}

    @staticmethod
    def _strip_input_stat(outputs: Path, run_id: str) -> None:
        """Rewrite ``run_id`` as a record written before input_stat existed."""
        ledger_file = outputs / ".meridian-outputs-cache" / "run_manifest_ledger.json"
        ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
        del ledger[run_id]["input_stat"]
        ledger_file.write_text(json.dumps(ledger), encoding="utf-8")

    def test_legacy_baseline_of_relative_input_is_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A legacy record's identity hash of a relative input is of the path
        as given against the start-time working directory (S-F5), which the
        record does not keep. Even when that directory happened to be
        outputs_dir, nothing in the record says so: the input is unknown
        and staleness is unknown, before and after an edit (S-R4-16)."""
        outputs = tmp_path / "outputs"
        inp = _write(outputs / "data" / "in.csv", "a\n1\n")
        monkeypatch.chdir(outputs)
        _start(outputs, "r1", ["data/in.csv"])
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        self._strip_input_stat(outputs, "r1")

        before = RM.check_input_staleness(str(outputs), "r1")
        assert before["baseline"] == "legacy_hash_only"
        assert before["stale"] is None
        assert _by_input(before) == {"data/in.csv": "unknown"}
        assert before["stale_inputs"] == []
        assert before["hashed_count"] == 0
        assert "cannot be confirmed" in before["reason"]
        inp.write_text("a\n2\n", encoding="utf-8")
        after = RM.check_input_staleness(str(outputs), "r1")
        assert after["stale"] is None
        assert _by_input(after) == {"data/in.csv": "unknown"}

    def test_legacy_relative_input_never_reads_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """S-R4-16: the legacy fallback compared the outputs_dir file with a
        hash of the working-directory copy. An untouched outputs file read
        "changed", and an outputs file edited to match that copy read
        "unchanged" -- a real change reported fresh."""
        outputs = tmp_path / "outputs"
        inp = _write(outputs / "data" / "in.csv", "outputs copy\n")
        _write(tmp_path / "cwd" / "data" / "in.csv", "working-directory copy\n")
        absolute = _write(tmp_path / "raw" / "abs.csv", "a\n1\n")
        monkeypatch.chdir(tmp_path / "cwd")
        _start(outputs, "r1", ["data/in.csv", str(absolute)])
        self._strip_input_stat(outputs, "r1")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        untouched = RM.check_input_staleness(str(outputs), "r1", strict=True)
        assert _by_input(untouched) == {"data/in.csv": "unknown", "../raw/abs.csv": "unchanged"}
        assert untouched["stale"] is None

        inp.write_text("working-directory copy\n", encoding="utf-8")  # a real change
        for strict in (False, True):
            changed = RM.check_input_staleness(str(outputs), "r1", strict=strict)
            assert _by_input(changed)["data/in.csv"] == "unknown"
            assert changed["stale"] is not False

        # An absolute legacy input is still compared, and a confirmed change wins.
        absolute.write_text("a\n2\n", encoding="utf-8")
        result = RM.check_input_staleness(str(outputs), "r1")
        assert result["stale"] is True
        assert result["stale_inputs"] == ["../raw/abs.csv"]
        assert _by_input(result)["data/in.csv"] == "unknown"

    def test_strict_catches_same_size_same_mtime_edit_default_misses_it(
        self, tmp_path: Path,
    ) -> None:
        inp = _write(tmp_path / "in.csv", "value=1\n")
        _start(tmp_path, "r1", [str(inp)])
        before = os.stat(inp)
        inp.write_text("value=2\n", encoding="utf-8")  # same size
        os.utime(inp, ns=(before.st_atime_ns, before.st_mtime_ns))  # same mtime
        after = os.stat(inp)
        assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)

        default = RM.check_input_staleness(str(tmp_path), "r1")
        assert default["stale"] is False  # documented limit of the stat fast path
        assert default["hashed_count"] == 0
        strict = RM.check_input_staleness(str(tmp_path), "r1", strict=True)
        assert strict["stale"] is True
        assert strict["strict"] is True
        assert _by_input(strict) == {"in.csv": "changed"}

    def test_strict_rehash_of_unchanged_input_is_not_stale(self, tmp_path: Path) -> None:
        inp = _write(tmp_path / "in.csv", "a\n1\n")
        _start(tmp_path, "r1", [str(inp)])
        result = RM.check_input_staleness(str(tmp_path), "r1", strict=True)
        assert result["stale"] is False
        assert result["hashed_count"] == 1
        assert "strict" in result["inputs"][0]["reason"]

    def test_unchanged_stat_makes_no_hash_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        inputs = [str(_write(tmp_path / f"in{i}.csv", f"a\n{i}\n")) for i in range(20)]
        _start(tmp_path, "r1", inputs)
        calls = {"n": 0}

        def _counting(path: str, **kwargs) -> str | None:
            calls["n"] += 1
            return None

        monkeypatch.setattr(RM.fingerprint, "script_content_hash", _counting)
        result = RM.check_input_staleness(str(tmp_path), "r1")
        assert result["stale"] is False
        assert len(result["inputs"]) == 20
        assert calls["n"] == 0
        assert result["hashed_count"] == 0

    def test_restart_with_identical_inputs_stays_idempotent(self, tmp_path: Path) -> None:
        inp = _write(tmp_path / "in.csv", "a\n1\n")
        first = _start(tmp_path, "r1", [str(inp)])
        st = os.stat(inp)
        os.utime(inp, ns=(st.st_atime_ns, st.st_mtime_ns + 3_000_000_000))  # touch only
        second = _start(tmp_path, "r1", [str(inp)])
        assert second["manifest_hash"] == first["manifest_hash"]
        assert second["created_at"] == first["created_at"]

        changed = dict(first, input_stat={"in.csv": {"exists": False}})
        assert RM.run_manifest_hash(changed) == first["manifest_hash"]
        # input_identity keeps its long-standing shape: keyed by the path as given.
        assert list(first["input_identity"]["file_hashes"]) == [str(inp)]

    def test_legacy_manifest_without_input_stat_rehashes(self, tmp_path: Path) -> None:
        inp = _write(tmp_path / "in.csv", "a\n1\n")
        _start(tmp_path, "r1", [str(inp)])
        ledger_file = tmp_path / ".meridian-outputs-cache" / "run_manifest_ledger.json"
        ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
        del ledger["r1"]["input_stat"]
        ledger_file.write_text(json.dumps(ledger), encoding="utf-8")

        unchanged = RM.check_input_staleness(str(tmp_path), "r1")
        assert unchanged["baseline"] == "legacy_hash_only"
        assert unchanged["stale"] is False
        assert unchanged["hashed_count"] == 1
        inp.write_text("a\n2\n", encoding="utf-8")
        assert RM.check_input_staleness(str(tmp_path), "r1")["stale_inputs"] == ["in.csv"]

    def test_unreadable_and_unverifiable_inputs_fail_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        inp = _write(tmp_path / "in.csv", "a\n1\n")
        no_hash = {"exists": True, "size": None, "mtime_ns": None, "sha256": None}
        assert RM._classify_input(str(inp), no_hash, strict=False)[0] == "unverifiable"

        _start(tmp_path, "r1", [str(inp)])
        monkeypatch.setattr(RM.fingerprint, "script_content_hash", lambda *a, **k: None)
        result = RM.check_input_staleness(str(tmp_path), "r1", strict=True)
        assert _by_input(result) == {"in.csv": "unreadable"}
        assert result["stale"] is True

    @pytest.mark.parametrize("realpath_rejects", [False, True])
    def test_input_path_with_nul_byte_records_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, realpath_rejects: bool,
    ) -> None:
        """S-R4-17: os.stat raises ValueError (not OSError) for an embedded
        NUL byte, which escaped _stat_or_none and failed start_run_manifest.
        Such a path names no file: it is recorded absent with a None hash,
        as dev did. ``realpath_rejects`` reproduces POSIX, where realpath
        (inside _resolve_ref) raises the same ValueError first."""
        if realpath_rejects:
            real_resolve = RM._resolve_ref

            def _posix_like(outputs_dir: str, raw: str) -> str:
                if "\x00" in raw:
                    raise ValueError("lstat: embedded null character in path")
                return real_resolve(outputs_dir, raw)

            monkeypatch.setattr(RM, "_resolve_ref", _posix_like)
        assert RM._stat_or_none(str(tmp_path / "bad\x00name.csv")) is None
        rec = _start(tmp_path, "r1", ["bad\x00name.csv"])
        assert rec["input_identity"]["file_hashes"] == {"bad\x00name.csv": None}
        assert rec["input_stat"] == {
            "bad\x00name.csv": {"exists": False, "size": None, "mtime_ns": None, "sha256": None},
        }
        result = RM.check_input_staleness(str(tmp_path), "r1")
        assert result["stale"] is False
        assert _by_input(result) == {"bad\x00name.csv": "absent"}

    def test_run_without_inputs_and_unknown_runs(self, tmp_path: Path) -> None:
        _start(tmp_path, "r1", [])
        result = RM.check_input_staleness(str(tmp_path), "r1")
        assert result["stale"] is False
        assert result["reason"] == "run recorded no inputs"
        with pytest.raises(RM.RunManifestError):
            RM.check_input_staleness(str(tmp_path), "never-started")
        with pytest.raises(RM.RunManifestError):
            RM.check_input_staleness("", "r1")

    def test_run_ids_for_output(self, tmp_path: Path) -> None:
        inp = _write(tmp_path / "in.csv", "a\n1\n")
        out = _write(tmp_path / "out.csv", "r\n1\n")
        _start(tmp_path, "r1", [str(inp)])
        RM.finalize_run_manifest(str(tmp_path), "r1", output_paths=["out.csv"])
        _start(tmp_path, "r2", [str(inp)])

        assert RM.run_ids_for_output(str(tmp_path), str(out)) == ["r1"]
        assert RM.run_ids_for_output(str(tmp_path), str(out), record_run_id="r2") == ["r1", "r2"]
        assert RM.run_ids_for_output(str(tmp_path), str(out), record_run_id="nope") == ["r1"]
        assert RM.run_ids_for_output(str(tmp_path), "") == []


class TestProvenanceStatusInputStaleness:
    _EXACT_KEYS = {
        "path", "provenance_type", "record", "directory_note", "staleness",
        "script_staleness", "archival", "convergence", "inconclusive",
    }

    def test_status_keeps_its_keys_and_adds_input_staleness(self, tmp_path: Path) -> None:
        pytest.importorskip("duckdb")
        from meridian_outputs import provenance_status as PS

        inp = _write(tmp_path / "in.csv", "a\n1\n")
        other_inp = _write(tmp_path / "other_in.csv", "b\n1\n")
        out = _write(tmp_path / "out.csv", "r\n1\n")
        other_out = _write(tmp_path / "other_out.csv", "r\n2\n")
        _start(tmp_path, "r1", [str(inp)])
        _start(tmp_path, "r2", [str(other_inp)])
        RM.finalize_run_manifest(str(tmp_path), "r2", output_paths=[str(other_out)])
        AN.record_provenance(str(tmp_path), str(out), run_id="r1")  # joined by run_id only
        AN.record_provenance(str(tmp_path), str(other_out))

        status = PS.get_provenance_status(str(tmp_path), str(out))
        assert status["provenance_type"] == PS.EXACT
        assert set(status) == self._EXACT_KEYS | {"input_staleness"}
        assert status["input_staleness"]["stale"] is False
        assert [r["run_id"] for r in status["input_staleness"]["runs"]] == ["r1"]
        assert status["staleness"]["stale"] is False

        inp.write_text("a\n1\n2\n", encoding="utf-8")
        status = PS.get_provenance_status(str(tmp_path), str(out))
        assert status["input_staleness"]["stale"] is True
        assert status["input_staleness"]["runs"][0]["stale_inputs"] == ["in.csv"]
        assert status["staleness"]["stale"] is True
        assert "an input changed since its run started" in status["staleness"]["reason"]
        assert status["provenance_type"] == PS.EXACT  # no new provenance_type

        # An output of a run whose inputs did not change is unaffected.
        other = PS.get_provenance_status(str(tmp_path), str(other_out))
        assert other["input_staleness"]["stale"] is False
        assert other["staleness"]["stale"] is False

    def test_uncovered_and_unrecorded_paths(self, tmp_path: Path) -> None:
        pytest.importorskip("duckdb")
        from meridian_outputs import provenance_status as PS

        plain = _write(tmp_path / "plain.csv", "r\n1\n")
        AN.record_provenance(str(tmp_path), str(plain))
        assert PS.get_provenance_status(str(tmp_path), str(plain))["input_staleness"] is None

        inp = _write(tmp_path / "in.csv", "a\n1\n")
        orphan = _write(tmp_path / "orphan.csv", "r\n9\n")
        _start(tmp_path, "r1", [str(inp)])
        RM.finalize_run_manifest(str(tmp_path), "r1", output_paths=[str(orphan)])
        inp.write_text("changed\n", encoding="utf-8")
        status = PS.get_provenance_status(str(tmp_path), str(orphan))
        assert status["record"] is None and status["staleness"] is None
        assert status["input_staleness"]["stale"] is True  # reported, shape untouched

        assert "input_staleness" not in PS.get_provenance_status(str(tmp_path), "")

    def _covered_output(self, tmp_path: Path) -> Path:
        inp = _write(tmp_path / "in.csv", "a\n1\n")
        out = _write(tmp_path / "out.csv", "r\n1\n")
        _start(tmp_path, "r1", [str(inp)])
        RM.finalize_run_manifest(str(tmp_path), "r1", output_paths=[str(out)])
        AN.record_provenance(str(tmp_path), str(out), run_id="r1")
        return out

    @pytest.mark.parametrize("garbage", [b"", b"{not json", b'{"r1": null}'])
    def test_corrupt_run_manifest_ledger_is_unknown_not_fresh(
        self, tmp_path: Path, garbage: bytes,
    ) -> None:
        pytest.importorskip("duckdb")
        from meridian_outputs import ledger_io as LIO
        from meridian_outputs import provenance_status as PS
        from meridian_outputs import research_evidence as RE

        out = self._covered_output(tmp_path)
        before = PS.get_provenance_status(str(tmp_path), str(out))
        assert before["input_staleness"]["stale"] is False
        assert PS.evidence_record_from_provenance_status(before).resolver.status is (
            RE.ResolverStatus.VERIFIED
        )

        ledger = tmp_path / ".meridian-outputs-cache" / "run_manifest_ledger.json"
        ledger.write_bytes(garbage)
        status = PS.get_provenance_status(str(tmp_path), str(out))
        assert status["provenance_type"] == PS.EXACT
        assert status["input_staleness"]["stale"] is None
        assert status["input_staleness"]["error"] == "LedgerCorruptError"
        assert "run_manifest_ledger.json" in status["input_staleness"]["reason"]
        assert str(tmp_path) not in status["input_staleness"]["reason"]
        assert status["staleness"]["stale"] is None
        assert "input staleness unknown" in status["staleness"]["reason"]
        assert PS.evidence_record_from_provenance_status(status).resolver.status is (
            RE.ResolverStatus.AMBIGUOUS
        )
        with pytest.raises(LIO.LedgerCorruptError):
            RM.run_ids_for_output(str(tmp_path), str(out))
        with pytest.raises(LIO.LedgerCorruptError):
            RM.check_input_staleness(str(tmp_path), "r1")

    def test_failed_run_check_is_unknown_not_fresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pytest.importorskip("duckdb")
        from meridian_outputs import provenance_status as PS
        from meridian_outputs import research_evidence as RE

        out = self._covered_output(tmp_path)

        def _boom(*_a, **_k):
            raise OSError("input volume went away")

        monkeypatch.setattr(RM, "check_input_staleness", _boom)
        status = PS.get_provenance_status(str(tmp_path), str(out))
        assert status["input_staleness"]["stale"] is None
        assert status["input_staleness"]["runs"] == [
            {"run_id": "r1", "stale": None, "error": "input volume went away"},
        ]
        assert status["staleness"]["stale"] is None
        assert PS.evidence_record_from_provenance_status(status).resolver.status is (
            RE.ResolverStatus.AMBIGUOUS
        )


class TestServerWrapper:
    def test_check_input_staleness_tool(self, tmp_path: Path) -> None:
        pytest.importorskip("mcp")
        from meridian_outputs import server

        inp = _write(tmp_path / "in.csv", "value=1\n")
        _start(tmp_path, "r1", [str(inp)])
        before = os.stat(inp)
        inp.write_text("value=2\n", encoding="utf-8")
        os.utime(inp, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert server.check_input_staleness(str(tmp_path), "r1")["stale"] is False
        assert server.check_input_staleness(str(tmp_path), "r1", strict=True)["stale"] is True
