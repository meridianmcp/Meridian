"""Tests for meridian_outputs.bundle (gap set C, "backup-export").

One checksummed JSON envelope holding the four Outputs ledgers plus index
metadata: export, verify, and a fail-closed import.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import annotate, artifact_registry, fingerprint, ledger_io, outputs_local, run_manifest
from meridian_outputs import bundle as B

_READERS = {
    "artifact_registry": lambda d: artifact_registry._read_registry(d),
    "fingerprint_ledger": lambda d: fingerprint._read_ledger(d),
    "provenance_ledger": lambda d: annotate._read_ledger(d),
    "run_manifest_ledger": lambda d: run_manifest._read_ledger(d),
}
_LEDGER_FILES = {
    "artifact_registry": "artifact_registry.json",
    "fingerprint_ledger": "fingerprint_ledger.json",
    "provenance_ledger": "provenance_ledger.json",
    "run_manifest_ledger": "run_manifest_ledger.json",
}


def _ledger_path(outputs_dir: Path, name: str) -> Path:
    return outputs_dir / ".meridian-outputs-cache" / _LEDGER_FILES[name]


def _snapshot(outputs_dir: Path) -> dict[str, bytes | None]:
    return {
        name: (_ledger_path(outputs_dir, name).read_bytes()
               if _ledger_path(outputs_dir, name).exists() else None)
        for name in _LEDGER_FILES
    }


@pytest.fixture
def populated(tmp_path: Path) -> dict[str, Path]:
    """An outputs dir with all four ledgers populated."""
    outputs = tmp_path / "src_outputs"
    scripts = tmp_path / "scripts"
    outputs.mkdir()
    scripts.mkdir()
    gen = scripts / "gen.py"
    gen.write_text("print('v1')\n", encoding="utf-8")
    fig = outputs / "fig.csv"
    fig.write_text("a,b\n1,2\n", encoding="utf-8")
    table = outputs / "table.json"
    table.write_text('{"rows": 3}', encoding="utf-8")

    od = str(outputs)
    annotate.record_provenance(od, str(fig), generating_script=str(gen), params={"lr": 0.01})
    annotate.record_provenance(od, str(table), generating_script="fig.csv")
    fingerprint.tag_output(str(fig), od, script_path=str(gen))
    run_manifest.start_run_manifest(od, run_id="run-1", command_name="gen",
                                    input_paths=[str(gen)])
    run_manifest.finalize_run_manifest(od, "run-1", output_paths=[str(fig)])
    art = artifact_registry.register_artifact(
        od, "figure", canonical_path=str(fig), generator="gen.py", run_id="run-1",
    )
    artifact_registry.bind_source_edge(od, art["artifact_id"], "../scripts/gen.py",
                                       metadata={"note": "v1"})
    return {"root": tmp_path, "outputs": outputs, "gen": gen, "fig": fig,
            "artifact_id": art["artifact_id"]}


def _empty_target(root: Path, name: str = "dst_outputs") -> Path:
    target = root / name
    target.mkdir()
    return target


def _export(outputs: Path, rel: str = "backup/bundle.json") -> dict:
    result = B.export_bundle(str(outputs), rel)
    assert result["exported"] is True, result
    return result


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _reseal(envelope: dict) -> dict:
    """Recompute every digest after a deliberate edit (simulates a bundle
    from a different, but internally consistent, exporter)."""
    digests = {}
    for name, member in envelope["members"].items():
        digest, size = B._member_digest(member["payload"])
        member["sha256"], member["size"] = digest, size
        digests[name] = digest
    envelope["bundle_digest"] = B._bundle_digest(digests)
    return envelope


def _copy_bundle(src_outputs: Path, rel: str, dst_outputs: Path, dst_rel: str = "incoming.json") -> Path:
    dst = dst_outputs / dst_rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src_outputs / rel, dst)
    return dst


class TestExport:
    def test_envelope_shape_and_member_digests(self, populated) -> None:
        result = _export(populated["outputs"])
        assert result["bundle_path"] == "backup/bundle.json"
        envelope = _load(populated["outputs"] / "backup" / "bundle.json")
        assert envelope["format"] == "meridian-outputs-bundle"
        assert envelope["schema_version"] == 1
        assert envelope["digest_algorithm"] == "sha256"
        assert sorted(envelope["members"]) == list(B.MEMBER_NAMES) == [
            "artifact_registry", "fingerprint_ledger", "index_meta",
            "provenance_ledger", "run_manifest_ledger",
        ]
        for name, member in envelope["members"].items():
            digest, size = B._member_digest(member["payload"])
            assert member["sha256"] == digest == result["members"][name]["sha256"]
            assert member["size"] == size
        assert envelope["bundle_digest"] == result["bundle_digest"]
        assert result["members"]["provenance_ledger"]["entries"] == 2
        assert result["members"]["artifact_registry"]["entries"] == 2  # 1 artifact + 1 edge

    def test_index_meta_records_versions_and_excludes_index(self, populated) -> None:
        _export(populated["outputs"])
        meta = _load(populated["outputs"] / "backup" / "bundle.json")["members"]["index_meta"]["payload"]
        assert meta == {
            "hash_algo_version": outputs_local._HASH_ALGO_VERSION,
            "ledger_digest_algorithm": "sha256",
            "ledger_digest_version": fingerprint._DIGEST_VERSION,
            "index_included": False,
        }
        # The index database is never a member, whether or not one exists.
        members = _load(populated["outputs"] / "backup" / "bundle.json")["members"]
        assert not any("index" in name and name != "index_meta" for name in members)

    def test_local_only_path_values_are_stripped(self, populated) -> None:
        result = _export(populated["outputs"])
        assert result["local_only_paths_stripped"] == 1
        registry = _load(populated["outputs"] / "backup" / "bundle.json")["members"]["artifact_registry"]["payload"]
        sightings = [s for rec in registry["artifacts"].values() for s in rec["local_paths"]]
        assert sightings and all(s["local_only_path"] is None for s in sightings)
        assert all(s["basename"] == "fig.csv" for s in sightings)
        blob = json.dumps(registry)
        for spelling in (str(populated["root"]), os.path.realpath(populated["root"])):
            assert spelling not in blob
            assert spelling.replace("\\", "/") not in blob
        # The live registry still has its local path: export never mutates it.
        live = artifact_registry.get_artifact(str(populated["outputs"]), populated["artifact_id"])
        assert live["local_paths"][0]["local_only_path"] == str(populated["fig"])

    def test_two_exports_of_identical_state_share_a_digest(self, populated) -> None:
        first = _export(populated["outputs"], "a.json")
        second = _export(populated["outputs"], "b.json")
        assert first["bundle_digest"] == second["bundle_digest"]

    def test_digest_changes_when_state_changes(self, populated) -> None:
        first = _export(populated["outputs"], "a.json")
        annotate.record_provenance(str(populated["outputs"]), str(populated["fig"]),
                                   generating_script="other.py")
        second = _export(populated["outputs"], "b.json")
        assert first["bundle_digest"] != second["bundle_digest"]
        assert first["members"]["provenance_ledger"]["sha256"] != \
            second["members"]["provenance_ledger"]["sha256"]
        assert first["members"]["run_manifest_ledger"] == second["members"]["run_manifest_ledger"]

    def test_default_path_lives_in_the_cache_dir(self, populated) -> None:
        result = B.export_bundle(str(populated["outputs"]))
        assert result["bundle_path"] == B.DEFAULT_BUNDLE_RELPATH
        assert (populated["outputs"] / ".meridian-outputs-cache" / "bundles"
                / "meridian-outputs-bundle.json").is_file()

    def test_empty_outputs_dir_exports_empty_members(self, tmp_path) -> None:
        result = _export(tmp_path)
        assert result["members"]["provenance_ledger"]["entries"] == 0
        assert B.verify_bundle(str(tmp_path / "backup" / "bundle.json"))["verified"] is True

    @pytest.mark.parametrize("bad", ["../escape.json", "C:/elsewhere/x.json", "/etc/x.json"])
    def test_bundle_path_outside_outputs_dir_is_refused(self, populated, bad) -> None:
        if bad.startswith("C:") and os.name != "nt":
            pytest.skip("drive-qualified path is only absolute on Windows")
        result = B.export_bundle(str(populated["outputs"]), bad)
        assert result["exported"] is False
        assert "within outputs_dir" in result["error"]
        assert not (populated["root"] / "escape.json").exists()

    @pytest.mark.parametrize("name", [
        "provenance_ledger.json", "fingerprint_ledger.json", "run_manifest_ledger.json",
        "artifact_registry.json", "index.duckdb", "INDEX.DUCKDB.WAL",
    ])
    def test_bundle_path_may_not_name_a_live_ledger_or_index(self, populated, name) -> None:
        before = _snapshot(populated["outputs"])
        result = B.export_bundle(str(populated["outputs"]), f".meridian-outputs-cache/{name}")
        assert result["exported"] is False
        assert "live ledger or index" in result["error"]
        assert _snapshot(populated["outputs"]) == before

    def test_bundle_path_naming_a_directory_is_refused(self, populated) -> None:
        (populated["outputs"] / "somedir").mkdir()
        result = B.export_bundle(str(populated["outputs"]), "somedir")
        assert result == {"exported": False, "error": "bundle_path rejected: it names a directory"}

    def test_missing_outputs_dir(self, tmp_path) -> None:
        assert B.export_bundle(str(tmp_path / "nope"), "x.json") == {
            "exported": False, "error": "outputs_dir does not exist",
        }

    @pytest.mark.parametrize("content", [b"", b"{not json", b"[1, 2]"])
    def test_corrupt_ledger_refuses_export(self, populated, content) -> None:
        _ledger_path(populated["outputs"], "fingerprint_ledger").write_bytes(content)
        result = B.export_bundle(str(populated["outputs"]), "x.json")
        assert result["exported"] is False
        assert any(e.startswith("fingerprint_ledger:") for e in result["errors"])
        assert not (populated["outputs"] / "x.json").exists()

    def test_reader_failure_refuses_export(self, populated, monkeypatch) -> None:
        def _boom(_outputs_dir):
            raise RuntimeError("strict reader quarantined a corrupt ledger")

        monkeypatch.setattr(run_manifest, "_read_ledger", _boom)
        result = B.export_bundle(str(populated["outputs"]), "x.json")
        assert result["exported"] is False
        assert "run_manifest_ledger: reader failed (RuntimeError)" in result["errors"]

    def test_failed_replace_leaves_no_temp_file_and_no_bundle(self, populated, monkeypatch) -> None:
        def _fail(_src, _dst):
            raise OSError("disk full")

        monkeypatch.setattr(B.os, "replace", _fail)
        result = B.export_bundle(str(populated["outputs"]), "backup/x.json")
        assert result == {"exported": False, "error": "failed to write bundle (OSError)"}
        leftovers = list((populated["outputs"] / "backup").iterdir())
        assert leftovers == []

    def test_write_is_fsynced_before_replace(self, populated, monkeypatch) -> None:
        calls: list[str] = []
        real_fsync, real_replace = os.fsync, os.replace
        monkeypatch.setattr(B.os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
        monkeypatch.setattr(B.os, "replace", lambda s, d: (calls.append("replace"), real_replace(s, d))[1])
        _export(populated["outputs"])
        # The file fsync precedes the rename; on POSIX the directory fsync
        # after it adds a second fsync.
        assert calls[:2] == ["fsync", "replace"]
        assert calls.count("replace") == 1

    def test_directory_is_fsynced_after_replace(self, populated, monkeypatch) -> None:
        events: list[str] = []
        real_replace = os.replace
        monkeypatch.setattr(B.os, "replace", lambda s, d: (events.append("replace"), real_replace(s, d))[1])
        monkeypatch.setattr(ledger_io, "_fsync_directory", lambda d: events.append(f"dir:{d}"))
        _export(populated["outputs"])
        assert len(events) == 2 and events[0] == "replace"
        assert events[1].startswith("dir:")
        assert os.path.samefile(events[1][len("dir:"):], populated["outputs"] / "backup")

    def test_transient_sharing_violation_on_replace_is_retried(self, populated, monkeypatch) -> None:
        real_replace = os.replace
        failures = {"left": 2}

        def _flaky(src, dst):
            if failures["left"]:
                failures["left"] -= 1
                raise PermissionError("sharing violation")
            real_replace(src, dst)

        monkeypatch.setattr(ledger_io, "_SHARING_RETRY_SLEEP_SECONDS", 0.0)
        monkeypatch.setattr(B.os, "replace", _flaky)
        _export(populated["outputs"])
        assert failures["left"] == 0
        assert B.verify_bundle(str(populated["outputs"] / "backup" / "bundle.json"))["verified"] is True
        assert [p.name for p in (populated["outputs"] / "backup").iterdir()] == ["bundle.json"]


class TestVerify:
    def test_fresh_bundle_verifies(self, populated) -> None:
        _export(populated["outputs"])
        report = B.verify_bundle("backup/bundle.json", outputs_dir=str(populated["outputs"]))
        assert report["verified"] is True
        assert report["bundle_digest_ok"] is True
        assert report["mismatches"] == report["missing"] == report["unexpected"] == []
        assert report["malformed"] == report["errors"] == []
        assert all(m["ok"] for m in report["members"].values())

    def test_tampered_payload_names_the_member(self, populated) -> None:
        _export(populated["outputs"])
        path = populated["outputs"] / "backup" / "bundle.json"
        envelope = _load(path)
        key = next(iter(envelope["members"]["provenance_ledger"]["payload"]))
        envelope["members"]["provenance_ledger"]["payload"][key]["params"] = {"lr": 999}
        path.write_text(json.dumps(envelope), encoding="utf-8")
        report = B.verify_bundle(str(path))
        assert report["verified"] is False
        assert report["mismatches"] == ["provenance_ledger"]
        assert report["members"]["provenance_ledger"]["ok"] is False
        assert report["bundle_digest_ok"] is False

    def test_swapped_members_are_detected_by_the_bundle_digest(self, populated) -> None:
        _export(populated["outputs"])
        path = populated["outputs"] / "backup" / "bundle.json"
        envelope = _load(path)
        members = envelope["members"]
        members["fingerprint_ledger"], members["provenance_ledger"] = (
            members["provenance_ledger"], members["fingerprint_ledger"],
        )
        path.write_text(json.dumps(envelope), encoding="utf-8")
        report = B.verify_bundle(str(path))
        assert report["mismatches"] == []  # each member is internally consistent
        assert report["bundle_digest_ok"] is False
        assert report["verified"] is False

    def test_missing_unexpected_and_malformed_members(self, populated) -> None:
        _export(populated["outputs"])
        path = populated["outputs"] / "backup" / "bundle.json"
        envelope = _load(path)
        del envelope["members"]["run_manifest_ledger"]
        envelope["members"]["extra"] = {"sha256": "0" * 64, "size": 2, "payload": {}}
        envelope["members"]["index_meta"] = None
        path.write_text(json.dumps(envelope), encoding="utf-8")
        report = B.verify_bundle(str(path))
        assert report["missing"] == ["run_manifest_ledger"]
        assert report["unexpected"] == ["extra"]
        assert report["malformed"] == ["index_meta"]
        assert report["verified"] is False

    def test_consistent_digest_but_bad_shape_is_malformed(self, populated) -> None:
        _export(populated["outputs"])
        path = populated["outputs"] / "backup" / "bundle.json"
        envelope = _load(path)
        envelope["members"]["artifact_registry"]["payload"] = {"artifacts": [], "edges": {}}
        path.write_text(json.dumps(_reseal(envelope)), encoding="utf-8")
        report = B.verify_bundle(str(path))
        assert report["malformed"] == ["artifact_registry"]
        assert "shape_error" in report["members"]["artifact_registry"]
        assert report["verified"] is False

    @pytest.mark.parametrize("mutate, expected_error", [
        (lambda raw: raw[: len(raw) // 2], "bundle is not valid JSON (JSONDecodeError)"),
        (lambda raw: raw.replace('"meridian-outputs-bundle"', '"something-else"'),
         "unrecognised format marker"),
        (lambda raw: raw.replace('"schema_version": 1', '"schema_version": 99'),
         "unsupported schema_version 99"),
        (lambda raw: "[]", "bundle top level is not a JSON object"),
    ])
    def test_unreadable_or_unrecognised_bundles(self, populated, mutate, expected_error) -> None:
        _export(populated["outputs"])
        path = populated["outputs"] / "backup" / "bundle.json"
        path.write_text(mutate(path.read_text(encoding="utf-8")), encoding="utf-8")
        report = B.verify_bundle(str(path))
        assert report["verified"] is False
        assert expected_error in report["errors"]

    def test_members_not_an_object(self, populated) -> None:
        _export(populated["outputs"])
        path = populated["outputs"] / "backup" / "bundle.json"
        envelope = _load(path)
        envelope["members"] = []
        path.write_text(json.dumps(envelope), encoding="utf-8")
        assert B.verify_bundle(str(path))["errors"] == ["members is not a JSON object"]

    def test_size_limit_missing_file_and_bad_paths(self, populated) -> None:
        _export(populated["outputs"])
        path = populated["outputs"] / "backup" / "bundle.json"
        assert "byte limit" in B.verify_bundle(str(path), max_bytes=10)["errors"][0]
        assert B.verify_bundle(str(path) + ".nope")["errors"] == [
            "bundle file not found or unreadable",
        ]
        assert B.verify_bundle("")["errors"] == ["bundle_path is required"]
        outside = B.verify_bundle(str(populated["gen"]), outputs_dir=str(populated["outputs"]))
        assert outside["verified"] is False
        assert "within outputs_dir" in outside["errors"][0]
        missing_root = B.verify_bundle("x.json", outputs_dir=str(populated["root"] / "nope"))
        assert missing_root["errors"] == ["outputs_dir does not exist"]


class TestImport:
    def test_round_trip_into_empty_dir_reproduces_every_ledger(self, populated) -> None:
        src = populated["outputs"]
        _export(src)
        expected = {name: reader(str(src)) for name, reader in _READERS.items()}
        expected["artifact_registry"], _ = B._strip_registry(expected["artifact_registry"])

        assert B.verify_bundle("backup/bundle.json", outputs_dir=str(src))["verified"] is True
        target = _empty_target(populated["root"])
        _copy_bundle(src, "backup/bundle.json", target)
        result = B.import_bundle("incoming.json", str(target), dry_run=False)
        assert result["imported"] is True, result
        assert result["refused"] is False
        assert result["written_members"] == sorted(_LEDGER_FILES)
        for name, reader in _READERS.items():
            assert reader(str(target)) == expected[name], name
        # The imported records are usable through the public read APIs.
        rec = annotate.get_provenance(str(target), str(target / "fig.csv"))
        assert rec["params"] == {"lr": 0.01}
        assert run_manifest.get_run_manifest(str(target), "run-1")["phase"] == "complete"
        verify = artifact_registry.verify_artifact_hash(str(target), populated["artifact_id"])
        assert verify["verified"] is False  # no local sighting on this machine -- and no KeyError
        assert verify["path"] is None

    def test_dry_run_is_the_default_and_writes_nothing(self, populated) -> None:
        _export(populated["outputs"])
        target = _empty_target(populated["root"])
        _copy_bundle(populated["outputs"], "backup/bundle.json", target)
        result = B.import_bundle("incoming.json", str(target))
        assert result["dry_run"] is True
        assert result["imported"] is False
        assert result["refused"] is False
        assert result["would_write"] == sorted(_LEDGER_FILES)
        assert result["members"]["provenance_ledger"]["added"] == 2
        assert result["written_members"] == []
        assert _snapshot(target) == {name: None for name in _LEDGER_FILES}

    def test_tampered_bundle_imports_nothing(self, populated) -> None:
        _export(populated["outputs"])
        target = _empty_target(populated["root"])
        dst = _copy_bundle(populated["outputs"], "backup/bundle.json", target)
        envelope = _load(dst)
        envelope["members"]["fingerprint_ledger"]["payload"] = {}
        dst.write_text(json.dumps(envelope), encoding="utf-8")
        result = B.import_bundle("incoming.json", str(target), dry_run=False)
        assert result["refused"] is True
        assert result["reason"] == "verification_failed"
        assert result["verification"]["mismatches"] == ["fingerprint_ledger"]
        assert _snapshot(target) == {name: None for name in _LEDGER_FILES}

    @pytest.mark.parametrize("mutate", [
        lambda raw: raw[:-40],
        lambda raw: raw.replace('"meridian-outputs-bundle"', '"other-format"'),
        lambda raw: raw.replace('"schema_version": 1', '"schema_version": 2'),
    ])
    def test_unrecognised_bundle_refused_with_no_partial_write(self, populated, mutate) -> None:
        _export(populated["outputs"])
        target = _empty_target(populated["root"])
        annotate.record_provenance(str(target), str(target / "keep.csv"), generating_script="k.py")
        before = _snapshot(target)
        dst = _copy_bundle(populated["outputs"], "backup/bundle.json", target)
        dst.write_text(mutate(dst.read_text(encoding="utf-8")), encoding="utf-8")
        result = B.import_bundle("incoming.json", str(target), dry_run=False)
        assert result["refused"] is True
        assert result["reason"] == "verification_failed"
        assert _snapshot(target) == before

    def test_conflict_without_overwrite_is_refused_and_untouched(self, populated) -> None:
        src = populated["outputs"]
        _export(src)
        target = _empty_target(populated["root"])
        (target / "fig.csv").write_text("different,content\n9,9\n", encoding="utf-8")
        annotate.record_provenance(str(target), str(target / "fig.csv"), generating_script="mine.py")
        before = _snapshot(target)
        _copy_bundle(src, "backup/bundle.json", target)

        result = B.import_bundle("incoming.json", str(target), dry_run=False)
        assert result["refused"] is True
        assert result["reason"] == "conflicts"
        assert result["members"]["provenance_ledger"]["conflicts"] == ["fig.csv"]
        assert result["members"]["provenance_ledger"]["conflict_count"] == 1
        assert result["members"]["provenance_ledger"]["added"] == 1  # table.json
        assert _snapshot(target) == before
        assert annotate.get_provenance(str(target), "fig.csv")["generating_script"] == "mine.py"

    def test_conflict_with_overwrite_replaces_and_keeps_target_only_keys(self, populated) -> None:
        src = populated["outputs"]
        _export(src)
        target = _empty_target(populated["root"])
        (target / "fig.csv").write_text("x\n", encoding="utf-8")
        annotate.record_provenance(str(target), str(target / "fig.csv"), generating_script="mine.py")
        annotate.record_provenance(str(target), str(target / "only_here.csv"), generating_script="t.py")
        _copy_bundle(src, "backup/bundle.json", target)

        result = B.import_bundle("incoming.json", str(target), dry_run=False, overwrite=True)
        assert result["imported"] is True
        assert result["members"]["provenance_ledger"]["replaced"] == 1
        assert annotate.get_provenance(str(target), "fig.csv")["generating_script"] == "../scripts/gen.py"
        assert annotate.get_provenance(str(target), "only_here.csv")["generating_script"] == "t.py"

    def test_reimport_into_the_source_dir_is_all_unchanged(self, populated) -> None:
        src = populated["outputs"]
        _export(src)
        before = _snapshot(src)
        result = B.import_bundle("backup/bundle.json", str(src), dry_run=False)
        assert result["imported"] is True
        assert result["would_write"] == []
        assert result["written_members"] == []
        for summary in result["members"].values():
            assert summary["conflict_count"] == 0 and summary["added"] == 0
        # Registry records compare without local_paths: the stripped bundle
        # copy does not conflict with the live record that still has them.
        assert result["members"]["artifact_registry"]["unchanged"] == 2
        assert _snapshot(src) == before

    def test_registry_overwrite_keeps_target_local_sightings(self, populated) -> None:
        src = populated["outputs"]
        od = str(src)
        aid = populated["artifact_id"]
        _export(src)
        # Change a portable field locally so the bundle's record conflicts.
        registry = artifact_registry._read_registry(od)
        registry["artifacts"][aid]["role"] = "archival"
        artifact_registry._write_registry(od, registry)

        refused = B.import_bundle("backup/bundle.json", od, dry_run=False)
        assert refused["reason"] == "conflicts"
        assert refused["members"]["artifact_registry"]["conflicts"] == [f"artifact:{aid}"]

        result = B.import_bundle("backup/bundle.json", od, dry_run=False, overwrite=True)
        assert result["imported"] is True
        record = artifact_registry.get_artifact(od, aid)
        assert record.get("role") is None
        assert record["local_paths"][0]["local_only_path"] == str(populated["fig"])

    def test_registry_edges_merge_on_their_idempotency_key(self, populated) -> None:
        src = populated["outputs"]
        od = str(src)
        _export(src)
        registry = artifact_registry._read_registry(od)
        registry["edges"][0]["edge_id"] = "a-different-uuid"  # same logical edge
        artifact_registry._write_registry(od, registry)
        same = B.import_bundle("backup/bundle.json", od)
        assert same["members"]["artifact_registry"]["conflict_count"] == 0

        registry["edges"][0]["metadata"] = {"note": "v2"}
        artifact_registry._write_registry(od, registry)
        changed = B.import_bundle("backup/bundle.json", od)
        assert changed["reason"] == "conflicts"
        assert changed["members"]["artifact_registry"]["conflicts"][0].startswith("edge:")

        overwritten = B.import_bundle("backup/bundle.json", od, dry_run=False, overwrite=True)
        assert overwritten["imported"] is True
        assert artifact_registry._read_registry(od)["edges"][0]["metadata"] == {"note": "v1"}

    def test_hash_algo_mismatch_is_refused_unless_accepted(self, populated) -> None:
        src = populated["outputs"]
        _export(src)
        target = _empty_target(populated["root"])
        dst = _copy_bundle(src, "backup/bundle.json", target)
        envelope = _load(dst)
        envelope["members"]["index_meta"]["payload"]["hash_algo_version"] = 1
        dst.write_text(json.dumps(_reseal(envelope)), encoding="utf-8")
        assert B.verify_bundle(str(dst))["verified"] is True

        refused = B.import_bundle("incoming.json", str(target), dry_run=False)
        assert refused["refused"] is True
        assert refused["reason"] == "hash_algo_version_mismatch"
        assert refused["hash_algo"]["bundle_hash_algo_version"] == 1
        assert refused["hash_algo"]["local_hash_algo_version"] == outputs_local._HASH_ALGO_VERSION
        assert _snapshot(target) == {name: None for name in _LEDGER_FILES}

        accepted = B.import_bundle("incoming.json", str(target), dry_run=False,
                                   accept_hash_algo_mismatch=True)
        assert accepted["imported"] is True
        assert accepted["hash_algo"]["mismatch"] is True
        assert accepted["hash_algo"]["accepted"] is True

    def test_ledger_digest_version_mismatch_is_also_refused(self, populated) -> None:
        _export(populated["outputs"])
        target = _empty_target(populated["root"])
        dst = _copy_bundle(populated["outputs"], "backup/bundle.json", target)
        envelope = _load(dst)
        envelope["members"]["index_meta"]["payload"]["ledger_digest_version"] = 2
        dst.write_text(json.dumps(_reseal(envelope)), encoding="utf-8")
        assert B.import_bundle("incoming.json", str(target))["reason"] == "hash_algo_version_mismatch"

    def test_unreadable_target_ledger_refuses(self, populated) -> None:
        _export(populated["outputs"])
        target = _empty_target(populated["root"])
        _copy_bundle(populated["outputs"], "backup/bundle.json", target)
        cache = target / ".meridian-outputs-cache"
        cache.mkdir()
        (cache / "run_manifest_ledger.json").write_text("{broken", encoding="utf-8")
        result = B.import_bundle("incoming.json", str(target), dry_run=False)
        assert result["reason"] == "target_ledger_unreadable"
        assert any(e.startswith("run_manifest_ledger:") for e in result["target_errors"])
        assert (cache / "run_manifest_ledger.json").read_text(encoding="utf-8") == "{broken"
        assert not (cache / "provenance_ledger.json").exists()

    def test_target_reader_failure_refuses(self, populated, monkeypatch) -> None:
        _export(populated["outputs"])

        def _boom(_outputs_dir):
            raise RuntimeError("strict reader")

        monkeypatch.setattr(fingerprint, "_read_ledger", _boom)
        result = B.import_bundle("backup/bundle.json", str(populated["outputs"]))
        assert result["reason"] == "target_ledger_unreadable"
        assert "fingerprint_ledger: reader failed (RuntimeError)" in result["target_errors"]

    def test_concurrent_modification_stops_the_write(self, populated, monkeypatch) -> None:
        _export(populated["outputs"])
        target = _empty_target(populated["root"])
        _copy_bundle(populated["outputs"], "backup/bundle.json", target)
        real_signature = B._file_signature
        seen: dict[str, int] = {}

        def _drifting(path):
            seen[path] = seen.get(path, 0) + 1
            if seen[path] == 2:  # the pre-write re-check
                return (1, 1)
            return real_signature(path)

        monkeypatch.setattr(B, "_file_signature", _drifting)
        result = B.import_bundle("incoming.json", str(target), dry_run=False)
        assert result["imported"] is False
        assert result["reason"] == "concurrent_modification"
        assert result["written_members"] == []
        assert _snapshot(target) == {name: None for name in _LEDGER_FILES}

    def test_write_failure_is_reported(self, populated, monkeypatch) -> None:
        _export(populated["outputs"])
        target = _empty_target(populated["root"])
        _copy_bundle(populated["outputs"], "backup/bundle.json", target)

        def _fail(*_a, **_k):
            raise OSError("read-only volume")

        monkeypatch.setattr(B, "_atomic_write_json", _fail)
        result = B.import_bundle("incoming.json", str(target), dry_run=False)
        assert result["imported"] is False
        assert result["reason"] == "write_failed"
        assert result["written_members"] == []

    def test_import_replace_failure_leaves_no_temp_file(self, populated, monkeypatch) -> None:
        _export(populated["outputs"])
        target = _empty_target(populated["root"])
        _copy_bundle(populated["outputs"], "backup/bundle.json", target)

        def _fail(_src, _dst):
            raise OSError("disk full")

        monkeypatch.setattr(B.os, "replace", _fail)
        result = B.import_bundle("incoming.json", str(target), dry_run=False)
        assert result["imported"] is False
        assert result["reason"] == "write_failed"
        assert result["written_members"] == []
        cache = target / ".meridian-outputs-cache"
        assert [p.name for p in cache.iterdir() if p.name.endswith(".tmp")] == []
        assert _snapshot(target) == {name: None for name in _LEDGER_FILES}

    def test_busy_ledger_lock_is_a_write_failure_not_a_hang(self, populated, monkeypatch) -> None:
        _export(populated["outputs"])
        target = _empty_target(populated["root"])
        _copy_bundle(populated["outputs"], "backup/bundle.json", target)

        class _BusyLock:
            def acquire(self, timeout=-1):
                return False

            def release(self):  # pragma: no cover - never acquired
                raise AssertionError

        monkeypatch.setattr(artifact_registry, "_write_lock", _BusyLock())
        result = B.import_bundle("incoming.json", str(target), dry_run=False)
        assert result["reason"] == "write_failed"
        assert result["written_members"] == []

    def test_import_paths_and_missing_dir(self, populated) -> None:
        _export(populated["outputs"])
        assert B.import_bundle("x.json", str(populated["root"] / "nope"))["reason"] == "outputs_dir_missing"
        outside = B.import_bundle(str(populated["gen"]), str(populated["outputs"]))
        assert outside["reason"] == "verification_failed"
        assert "within outputs_dir" in outside["verification"]["errors"][0]

    def test_conflict_list_is_capped(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(B, "_CONFLICT_LIST_CAP", 2)
        summary = B._summarise({"added": [], "unchanged": [], "replaced": [],
                                "conflicts": ["a", "b", "c"]})
        assert summary["conflicts"] == ["a", "b"]
        assert summary["conflict_count"] == 3
        assert summary["conflicts_truncated"] is True


class TestWiring:
    def test_package_exports(self) -> None:
        import meridian_outputs

        for name in ("export_bundle", "verify_bundle", "import_bundle"):
            assert getattr(meridian_outputs, name) is getattr(B, name)
            assert name in meridian_outputs.__all__

    def test_mcp_tools_registered_and_forward(self, populated) -> None:
        from meridian_outputs import server as srv

        registered = {t.name for t in srv.mcp._tool_manager.list_tools()}
        assert {"export_outputs_bundle", "verify_outputs_bundle",
                "import_outputs_bundle"} <= registered

        od = str(populated["outputs"])
        exported = srv.export_outputs_bundle(od)
        assert exported["exported"] is True
        assert exported["bundle_path"] == B.DEFAULT_BUNDLE_RELPATH
        assert srv.verify_outputs_bundle(od, B.DEFAULT_BUNDLE_RELPATH)["verified"] is True
        dry = srv.import_outputs_bundle(od, B.DEFAULT_BUNDLE_RELPATH)
        assert dry["dry_run"] is True and dry["refused"] is False
        refused = srv.verify_outputs_bundle(od, str(populated["gen"]))
        assert refused["verified"] is False

    def test_server_default_bundle_path_matches_module_constant(self) -> None:
        import inspect

        from meridian_outputs import server as srv

        default = inspect.signature(srv.export_outputs_bundle).parameters["bundle_path"].default
        assert default == B.DEFAULT_BUNDLE_RELPATH

    def test_readme_documents_the_tools(self) -> None:
        readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
        for name in ("export_outputs_bundle", "verify_outputs_bundle", "import_outputs_bundle"):
            assert f"`{name}`" in readme
