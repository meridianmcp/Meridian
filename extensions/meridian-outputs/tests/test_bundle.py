"""Tests for meridian_outputs.bundle (gap set C, "backup-export").

One checksummed JSON envelope holding the four Outputs ledgers plus index
metadata: export, verify, and a fail-closed import.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

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


def _fingerprint_import_setup(root: Path) -> tuple[Path, Path]:
    """A bundle whose only change for the target is one fingerprint entry
    (a.csv), copied into a target that already has its own entry (b.csv)
    and an untagged c.csv for a concurrent writer to tag."""
    gen = root / "gen.py"
    gen.write_text("print(1)\n", encoding="utf-8")
    src = _empty_target(root, "fp_src")
    (src / "a.csv").write_text("x\n1\n", encoding="utf-8")
    fingerprint.tag_output(str(src / "a.csv"), str(src), script_path=str(gen))
    _export(src)
    target = _empty_target(root, "fp_dst")
    (target / "b.csv").write_text("x\n2\n", encoding="utf-8")
    (target / "c.csv").write_text("x\n3\n", encoding="utf-8")
    fingerprint.tag_output(str(target / "b.csv"), str(target), script_path=str(gen))
    _copy_bundle(src, "backup/bundle.json", target)
    dry = B.import_bundle("incoming.json", str(target))
    assert dry["would_write"] == ["fingerprint_ledger"], dry
    return target, gen


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

    def test_export_into_a_fresh_cache_gitignores_it(self, tmp_path) -> None:
        """B-F13: the ledger readers no longer create the cache dir, so an
        export to the default path created .meridian-outputs-cache/bundles
        with no .gitignore entry, leaving the bundle (and the cache) for
        git to pick up."""
        fresh = tmp_path / "fresh_outputs"
        fresh.mkdir()
        result = B.export_bundle(str(fresh))
        assert result["exported"] is True, result
        gitignore = fresh / ".gitignore"
        assert gitignore.is_file()
        assert "/.meridian-outputs-cache/" in gitignore.read_text(encoding="utf-8").splitlines()

    def test_export_outside_the_cache_does_not_create_it(self, tmp_path) -> None:
        fresh = tmp_path / "fresh_outputs"
        fresh.mkdir()
        assert B.export_bundle(str(fresh), "backup/bundle.json")["exported"] is True
        assert not (fresh / ".meridian-outputs-cache").exists()
        assert not (fresh / ".gitignore").exists()

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

    @pytest.mark.parametrize("rel", [
        ".meridian-outputs-cache/index.duckdb.lock",
        ".MERIDIAN-OUTPUTS-CACHE/index.duckdb.lock",
        ".meridian-outputs-cache/bundles/../index.duckdb.lock",
        ".meridian-outputs-cache/provenance_ledger.json.lock",
        ".meridian-outputs-cache/derived/manifest.json",
        ".meridian-outputs-cache/derived/artifacts/abc.png",
        ".meridian-outputs-cache/bundles",
    ])
    def test_cache_files_outside_bundles_dir_are_refused_even_with_overwrite(
        self, populated, rel,
    ) -> None:
        """B1: the lock, derived-cache and other live files in the cache are
        never an export target -- index.duckdb.lock being replaced wedged
        every index writer."""
        cache = populated["outputs"] / ".meridian-outputs-cache"
        lock = cache / "index.duckdb.lock"
        manifest = cache / "derived" / "manifest.json"
        artifact = cache / "derived" / "artifacts" / "abc.png"
        artifact.parent.mkdir(parents=True)
        for path in (lock, manifest, artifact):
            path.write_bytes(b"live state")
        try:
            before = _snapshot(populated["outputs"])
            result = B.export_bundle(str(populated["outputs"]), rel, overwrite=True)
            assert result["exported"] is False
            assert "bundles/ subdirectory" in result["error"]
            for path in (lock, manifest, artifact):
                assert path.read_bytes() == b"live state"
            assert not (cache / "bundles").exists()
            assert _snapshot(populated["outputs"]) == before
        finally:
            # A planted lock file reads as "held" to IndexFileLock, which
            # would stall the index cache's close at teardown -- the very
            # wedge this test guards against.
            lock.unlink()

    def test_existing_user_file_is_refused_without_overwrite(self, populated) -> None:
        """B1: an existing file at bundle_path is never silently replaced."""
        results = populated["outputs"] / "results.csv"
        results.write_text("x,y\n1,2\n", encoding="utf-8")
        refused = B.export_bundle(str(populated["outputs"]), "results.csv")
        assert refused == {
            "exported": False,
            "error": "bundle_path rejected: a file already exists there "
                     "(pass overwrite=True to replace it)",
        }
        assert results.read_text(encoding="utf-8") == "x,y\n1,2\n"

        replaced = B.export_bundle(str(populated["outputs"]), "results.csv", overwrite=True)
        assert replaced["exported"] is True
        assert B.verify_bundle(str(results))["verified"] is True

    def test_existing_bundle_at_default_path_needs_overwrite(self, populated) -> None:
        od = str(populated["outputs"])
        first = B.export_bundle(od)
        assert first["exported"] is True
        again = B.export_bundle(od)
        assert again["exported"] is False
        assert "already exists" in again["error"]
        assert B.export_bundle(od, overwrite=True)["exported"] is True

    _STREAM_NAMES = [
        "results.csv:hidden",
        "backup.json:evil",
        ".meridian-outputs-cache/bundles/b.json::$DATA",
        "sub/name:stream:$DATA",
    ]
    _POSIX_COLON_NAME = ".meridian-outputs-cache/bundles/bundle-2026-10-20T10:00:00Z.json"

    @pytest.mark.parametrize("rel", _STREAM_NAMES)
    def test_stream_suffixed_name_is_refused(self, populated, rel, monkeypatch) -> None:
        """B1: on Windows ``name:stream`` writes an alternate data stream of
        an existing file, so a ':' outside the drive letter is refused there.
        The Windows check is forced on so this runs on every host (B-F11
        made the refusal Windows-only)."""
        monkeypatch.setattr(B, "_IS_WINDOWS", True)
        results = populated["outputs"] / "results.csv"
        results.write_text("x,y\n1,2\n", encoding="utf-8")
        result = B.export_bundle(str(populated["outputs"]), rel, overwrite=True)
        assert result == {
            "exported": False,
            "error": "bundle_path rejected: ':' is only allowed in a drive letter",
        }
        assert results.read_text(encoding="utf-8") == "x,y\n1,2\n"
        assert not (populated["outputs"] / "backup.json").exists()
        assert not (populated["outputs"] / "sub").exists()

    @pytest.mark.parametrize("rel", [*_STREAM_NAMES, _POSIX_COLON_NAME])
    def test_colon_is_an_ordinary_character_off_windows(self, rel, monkeypatch) -> None:
        """B-F11: ':' was refused on every platform, although only Windows
        reads it as a stream separator."""
        monkeypatch.setattr(B, "_IS_WINDOWS", False, raising=False)
        assert B._stream_separator_error(rel) is None
        monkeypatch.setattr(B, "_IS_WINDOWS", True)
        assert B._stream_separator_error(rel) == (
            "bundle_path rejected: ':' is only allowed in a drive letter"
        )

    def test_posix_colon_name_is_refused_on_windows_only(self, populated) -> None:
        """B-F11, on the real platform: a timestamped name with colons is
        refused on Windows and exported (and verifies) everywhere else."""
        od = str(populated["outputs"])
        result = B.export_bundle(od, self._POSIX_COLON_NAME)
        if os.name == "nt":
            assert result == {
                "exported": False,
                "error": "bundle_path rejected: ':' is only allowed in a drive letter",
            }
            assert not (populated["outputs"] / ".meridian-outputs-cache" / "bundles").exists()
        else:
            assert result["exported"] is True, result
            assert result["bundle_path"] == self._POSIX_COLON_NAME
            assert B.verify_bundle(self._POSIX_COLON_NAME, outputs_dir=od)["verified"] is True

    def test_drive_qualified_absolute_path_inside_outputs_is_allowed(self, populated) -> None:
        target = populated["outputs"] / "abs_bundle.json"
        result = B.export_bundle(str(populated["outputs"]), str(target))
        assert result["exported"] is True
        assert result["bundle_path"] == "abs_bundle.json"

    def test_verify_refuses_cache_paths_outside_bundles_dir(self, populated) -> None:
        report = B.verify_bundle(
            ".meridian-outputs-cache/derived/manifest.json",
            outputs_dir=str(populated["outputs"]),
        )
        assert report["verified"] is False
        assert "bundles/ subdirectory" in report["errors"][0]

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
        # overwrite=True: os.replace is the overwrite publish; a create-only
        # export links instead (see the create-only tests below).
        def _fail(_src, _dst):
            raise OSError("disk full")

        monkeypatch.setattr(B.os, "replace", _fail)
        result = B.export_bundle(str(populated["outputs"]), "backup/x.json", overwrite=True)
        assert result == {"exported": False, "error": "failed to write bundle (OSError)"}
        leftovers = list((populated["outputs"] / "backup").iterdir())
        assert leftovers == []

    @staticmethod
    def _spy_publish(monkeypatch, record) -> None:
        """Record os.link and os.replace (the two publish steps) as "publish"."""
        real_link, real_replace = os.link, os.replace
        monkeypatch.setattr(B.os, "link", lambda s, d: (record("publish"), real_link(s, d))[1])
        monkeypatch.setattr(B.os, "replace", lambda s, d: (record("publish"), real_replace(s, d))[1])

    @pytest.mark.parametrize("overwrite", [False, True])
    def test_write_is_fsynced_before_replace(self, populated, monkeypatch, overwrite) -> None:
        calls: list[str] = []
        real_fsync = os.fsync
        monkeypatch.setattr(B.os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
        self._spy_publish(monkeypatch, calls.append)
        result = B.export_bundle(str(populated["outputs"]), "backup/bundle.json", overwrite=overwrite)
        assert result["exported"] is True, result
        # The file fsync precedes the publish (a link without overwrite, a
        # rename with it); on POSIX the directory fsync after it adds a
        # second fsync.
        assert calls[:2] == ["fsync", "publish"]
        assert calls.count("publish") == 1

    @pytest.mark.parametrize("overwrite", [False, True])
    def test_directory_is_fsynced_after_replace(self, populated, monkeypatch, overwrite) -> None:
        events: list[str] = []
        self._spy_publish(monkeypatch, events.append)
        monkeypatch.setattr(ledger_io, "_fsync_directory", lambda d: events.append(f"dir:{d}"))
        result = B.export_bundle(str(populated["outputs"]), "backup/bundle.json", overwrite=overwrite)
        assert result["exported"] is True, result
        assert len(events) == 2 and events[0] == "publish"
        assert events[1].startswith("dir:")
        assert os.path.samefile(events[1][len("dir:"):], populated["outputs"] / "backup")

    def test_transient_sharing_violation_on_replace_is_retried(self, populated, monkeypatch) -> None:
        # overwrite=True: the replace is where a lock-free reader holding
        # the target open causes a sharing violation.
        real_replace = os.replace
        failures = {"left": 2}

        def _flaky(src, dst):
            if failures["left"]:
                failures["left"] -= 1
                raise PermissionError("sharing violation")
            real_replace(src, dst)

        monkeypatch.setattr(ledger_io, "_SHARING_RETRY_SLEEP_SECONDS", 0.0)
        monkeypatch.setattr(B.os, "replace", _flaky)
        result = B.export_bundle(str(populated["outputs"]), "backup/bundle.json", overwrite=True)
        assert result["exported"] is True, result
        assert failures["left"] == 0
        assert B.verify_bundle(str(populated["outputs"] / "backup" / "bundle.json"))["verified"] is True
        assert [p.name for p in (populated["outputs"] / "backup").iterdir()] == ["bundle.json"]

    def test_concurrent_create_only_exports_cannot_both_succeed(self, populated, monkeypatch) -> None:
        """B-F12: overwrite=False was a check then os.replace. An export that
        passed the check replaced a bundle another export published while it
        read the ledgers, and both returned exported=True."""
        od = str(populated["outputs"])
        target = populated["outputs"] / "backup" / "bundle.json"
        real_problem = B._ledger_file_problem
        racer: dict[str, Any] = {}

        def _another_export_publishes_first(path):
            if not racer:  # once, after this export's existence check passed
                racer["started"] = True
                racer["result"] = B.export_bundle(od, "backup/bundle.json")
                racer["bytes"] = target.read_bytes()
            return real_problem(path)

        monkeypatch.setattr(B, "_ledger_file_problem", _another_export_publishes_first)
        result = B.export_bundle(od, "backup/bundle.json")
        assert racer["result"]["exported"] is True
        assert result["exported"] is False, "the second export replaced the first"
        assert target.read_bytes() == racer["bytes"]
        assert result == {"exported": False, "error": B._EXISTS_ERROR}
        assert [p.name for p in target.parent.iterdir()] == ["bundle.json"]

    @pytest.mark.parametrize("hard_links", ["available", "missing", "refused"])
    def test_create_only_publish_refuses_an_existing_target(
        self, tmp_path, monkeypatch, hard_links,
    ) -> None:
        """B-F12: a second create-only publish to the same target raises
        FileExistsError and the first file's bytes survive, whether the
        name is claimed by os.link or, without hard links, by an O_EXCL
        reservation."""
        if hard_links == "missing":
            monkeypatch.delattr(B.os, "link")
        elif hard_links == "refused":
            def _no_hard_links(_src, _dst):
                raise PermissionError(1, "hard links are not supported here")

            monkeypatch.setattr(B.os, "link", _no_hard_links)
        target = tmp_path / "bundles" / "bundle.json"
        B._atomic_write_json(str(target), {"first": 1}, indent=None, create_only=True)
        first = target.read_bytes()
        assert json.loads(first) == {"first": 1}
        with pytest.raises(FileExistsError):
            B._atomic_write_json(str(target), {"second": 2}, indent=None, create_only=True)
        assert target.read_bytes() == first
        assert [p.name for p in target.parent.iterdir()] == ["bundle.json"]

    def test_failed_create_only_publish_leaves_no_reservation_or_temp_file(
        self, populated, monkeypatch,
    ) -> None:
        """Without hard links the name is reserved first; a rename that then
        fails removes the empty reservation, so a retry is not refused as
        "already exists"."""
        monkeypatch.delattr(B.os, "link")

        def _fail(_src, _dst):
            raise OSError("disk full")

        monkeypatch.setattr(B.os, "replace", _fail)
        result = B.export_bundle(str(populated["outputs"]), "backup/x.json")
        assert result == {"exported": False, "error": "failed to write bundle (OSError)"}
        assert list((populated["outputs"] / "backup").iterdir()) == []
        monkeypatch.undo()
        assert B.export_bundle(str(populated["outputs"]), "backup/x.json")["exported"] is True


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

    def test_resealed_non_string_script_path_fails_verify_and_import(self, populated) -> None:
        """B2: digests can be recomputed, so a fingerprint row with
        script_path 5 used to verify, import, and then raise TypeError in
        every fingerprint.check_staleness call on the target."""
        _export(populated["outputs"])
        target = _empty_target(populated["root"])
        dst = _copy_bundle(populated["outputs"], "backup/bundle.json", target)
        envelope = _load(dst)
        row = next(iter(envelope["members"]["fingerprint_ledger"]["payload"].values()))
        row["script_path"] = 5
        dst.write_text(json.dumps(_reseal(envelope)), encoding="utf-8")

        report = B.verify_bundle(str(dst))
        assert report["verified"] is False
        assert report["mismatches"] == []  # the digests were recomputed
        assert report["malformed"] == ["fingerprint_ledger"]
        assert report["members"]["fingerprint_ledger"]["shape_error"] == (
            "a fingerprint_ledger entry's script_path is not a string or null"
        )
        result = B.import_bundle("incoming.json", str(target), dry_run=False)
        assert result["refused"] is True
        assert result["reason"] == "verification_failed"
        assert _snapshot(target) == {name: None for name in _LEDGER_FILES}
        assert fingerprint.check_staleness(str(target)) == []

    @pytest.mark.parametrize("field, value, reason", [
        ("script_hash", 5, "a fingerprint_ledger entry's script_hash is not a string or null"),
        ("content_digest", {"digest": 5}, "a fingerprint_ledger entry's content_digest is "
                                          "not a string, null or digest object"),
        ("content_digest", [1], "a fingerprint_ledger entry's content_digest is "
                                "not a string, null or digest object"),
    ])
    def test_resealed_bad_fingerprint_hash_fields_are_malformed(
        self, populated, field, value, reason,
    ) -> None:
        _export(populated["outputs"])
        path = populated["outputs"] / "backup" / "bundle.json"
        envelope = _load(path)
        row = next(iter(envelope["members"]["fingerprint_ledger"]["payload"].values()))
        row[field] = value
        path.write_text(json.dumps(_reseal(envelope)), encoding="utf-8")
        report = B.verify_bundle(str(path))
        assert report["verified"] is False
        assert report["malformed"] == ["fingerprint_ledger"]
        assert report["members"]["fingerprint_ledger"]["shape_error"] == reason

    @pytest.mark.parametrize("mutate, reason", [
        (lambda rec, edge: rec.__setitem__("local_paths", "fig.csv"),
         "an artifact_registry record's local_paths is not a list of objects"),
        (lambda rec, edge: rec.__setitem__("local_paths", None),
         "an artifact_registry record's local_paths is not a list of objects"),
        (lambda rec, edge: rec.__setitem__("local_paths", [5]),
         "an artifact_registry record's local_paths is not a list of objects"),
        (lambda rec, edge: rec["local_paths"][0].__setitem__("last_seen", 5),
         "an artifact_registry record's local_paths last_seen is not a string or null"),
        (lambda rec, edge: rec.pop("artifact_id"),
         "an artifact_registry record's artifact_id is not a string"),
        (lambda rec, edge: edge.__setitem__("relation", None),
         "an artifact_registry edge's relation is not a string"),
        (lambda rec, edge: edge.__setitem__("metadata", [1]),
         "an artifact_registry edge's metadata is not an object"),
    ])
    def test_resealed_bad_registry_rows_are_refused(self, populated, mutate, reason) -> None:
        """B2: an artifact whose local_paths is not a list of objects (or an
        edge missing a sort key) used to raise on every registry lookup
        after import."""
        _export(populated["outputs"])
        target = _empty_target(populated["root"])
        dst = _copy_bundle(populated["outputs"], "backup/bundle.json", target)
        envelope = _load(dst)
        registry = envelope["members"]["artifact_registry"]["payload"]
        mutate(registry["artifacts"][populated["artifact_id"]], registry["edges"][0])
        dst.write_text(json.dumps(_reseal(envelope)), encoding="utf-8")

        report = B.verify_bundle(str(dst))
        assert report["verified"] is False
        assert report["malformed"] == ["artifact_registry"]
        assert report["members"]["artifact_registry"]["shape_error"] == reason
        result = B.import_bundle("incoming.json", str(target), dry_run=False)
        assert result["reason"] == "verification_failed"
        assert _snapshot(target) == {name: None for name in _LEDGER_FILES}

    def test_export_refuses_a_live_ledger_row_that_would_not_verify(self, populated) -> None:
        path = _ledger_path(populated["outputs"], "fingerprint_ledger")
        ledger = json.loads(path.read_text(encoding="utf-8"))
        next(iter(ledger.values()))["script_path"] = 5
        path.write_text(json.dumps(ledger), encoding="utf-8")
        result = B.export_bundle(str(populated["outputs"]), "x.json")
        assert result["exported"] is False
        assert result["errors"] == [
            "fingerprint_ledger: a fingerprint_ledger entry's script_path is not a string or null",
        ]
        assert not (populated["outputs"] / "x.json").exists()

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

    def test_concurrent_modification_result_is_a_refusal(self) -> None:
        """B3: a concurrent_modification stop declines to write anything
        more, so it reports refused=True, not refused=False beside
        imported=False. Exercised through the helper: the write loop that
        returns it is owned by the import-write-path change."""
        result = {"imported": False, "dry_run": False, "refused": False, "reason": None,
                  "written_members": ["artifact_registry"]}
        out = B._concurrent_modification_result(result)
        assert out is result
        assert out["refused"] is True
        assert out["imported"] is False
        assert out["reason"] == "concurrent_modification"
        assert out["written_members"] == ["artifact_registry"]

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

    def test_commit_inside_the_write_window_is_not_lost(self, tmp_path, monkeypatch) -> None:
        """A writer that commits between the import's pre-write re-check and
        its replace must not be overwritten (L1)."""
        target, gen = _fingerprint_import_setup(tmp_path)
        ledger = os.path.normcase(os.path.abspath(_ledger_path(target, "fingerprint_ledger")))
        real_write = B._atomic_write_json
        writer: dict[str, Any] = {}

        def _tag_c() -> None:
            try:
                fingerprint.tag_output(str(target / "c.csv"), str(target), script_path=str(gen))
            except BaseException as exc:  # noqa: BLE001 -- surfaced by the assert below
                writer["error"] = exc

        def _write_with_a_racing_writer(path, obj, *, indent):
            if os.path.normcase(os.path.abspath(path)) == ledger and "thread" not in writer:
                writer["thread"] = threading.Thread(target=_tag_c, daemon=True)
                writer["thread"].start()
                # Without the cross-process ledger lock the tag commits right
                # here, inside the window. Under the lock it waits for this
                # import to release it, then commits on top of the import.
                writer["thread"].join(timeout=1.5)
            real_write(path, obj, indent=indent)

        monkeypatch.setattr(B, "_atomic_write_json", _write_with_a_racing_writer)
        result = B.import_bundle("incoming.json", str(target), dry_run=False)
        writer["thread"].join(timeout=60)
        assert not writer["thread"].is_alive()
        assert "error" not in writer, writer.get("error")
        keys = set(fingerprint._read_ledger(str(target)))
        assert {"b.csv", "c.csv"} <= keys, f"a concurrent commit was overwritten: {keys}"
        if result["imported"]:
            assert result["written_members"] == ["fingerprint_ledger"]
            assert "a.csv" in keys
        else:
            assert result["reason"] == "concurrent_modification"

    def test_same_signature_change_after_the_plan_is_detected(self, tmp_path, monkeypatch) -> None:
        """A rewrite that keeps (mtime_ns, size) -- same size inside one mtime
        tick -- passes the signature check; the content re-read under the lock
        still catches it."""
        target, gen = _fingerprint_import_setup(tmp_path)
        monkeypatch.setattr(B, "_file_signature", lambda path: (1, 1))
        real_read = fingerprint._read_ledger
        calls = {"n": 0}

        def _read_then_race(outputs_dir):
            calls["n"] += 1
            data = real_read(outputs_dir)
            if calls["n"] == 1:  # the plan's read; another writer commits after it
                fingerprint.tag_output(str(target / "c.csv"), str(target), script_path=str(gen))
            return data

        monkeypatch.setattr(fingerprint, "_read_ledger", _read_then_race)
        result = B.import_bundle("incoming.json", str(target), dry_run=False)
        assert result["imported"] is False
        assert result["reason"] == "concurrent_modification"
        assert result["written_members"] == []
        assert set(real_read(str(target))) == {"b.csv", "c.csv"}

    def test_busy_cross_process_ledger_lock_is_a_write_failure(self, tmp_path, monkeypatch) -> None:
        target, _gen = _fingerprint_import_setup(tmp_path)
        before = _snapshot(target)

        def _busy(self, *, timeout=None):
            raise outputs_local.IndexLockAcquireError("held by another process")

        monkeypatch.setattr(outputs_local.IndexFileLock, "acquire", _busy)
        result = B.import_bundle("incoming.json", str(target), dry_run=False)
        assert result["imported"] is False
        assert result["reason"] == "write_failed"
        assert result["written_members"] == []
        assert _snapshot(target) == before

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


class TestSourceEdgeFields:
    @pytest.mark.parametrize("field, value", [
        ("source_locator", 5),
        ("source_locator", ["../scripts/gen.py"]),
        ("relation", 5),
    ])
    def test_non_string_edge_field_is_refused_and_export_keeps_working(
        self, populated, field, value,
    ) -> None:
        """B-F10: bind_source_edge stored a non-string source_locator or
        relation, and every later export then refused the registry, whose
        edge fields must be strings."""
        od = str(populated["outputs"])
        before = _snapshot(populated["outputs"])["artifact_registry"]
        fields: dict[str, Any] = {"source_locator": "data/raw.csv", "relation": "produced_by"}
        fields[field] = value
        with pytest.raises(ValueError, match=f"{field} must be a non-empty string"):
            artifact_registry.bind_source_edge(od, populated["artifact_id"], **fields)
        assert _snapshot(populated["outputs"])["artifact_registry"] == before
        assert len(artifact_registry._read_registry(od)["edges"]) == 1
        assert B.export_bundle(od, "x.json")["exported"] is True


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

    def test_mcp_export_passes_overwrite_through(self, populated) -> None:
        import inspect

        from meridian_outputs import server as srv

        params = inspect.signature(srv.export_outputs_bundle).parameters
        assert params["overwrite"].default is False
        od = str(populated["outputs"])
        assert srv.export_outputs_bundle(od)["exported"] is True
        again = srv.export_outputs_bundle(od)
        assert again["exported"] is False and "already exists" in again["error"]
        assert srv.export_outputs_bundle(od, overwrite=True)["exported"] is True

    def test_readme_documents_the_tools(self) -> None:
        readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
        for name in ("export_outputs_bundle", "verify_outputs_bundle", "import_outputs_bundle"):
            assert f"`{name}`" in readme
