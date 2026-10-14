"""Regression tests for local Outputs path containment and portable ledgers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian_outputs import annotate, fingerprint, outputs_local
from meridian_outputs.path_security import _require_within


def test_require_within_rejects_parent_and_symlink_escapes(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")

    with pytest.raises(ValueError, match="within outputs_dir"):
        _require_within(str(outputs), "../outside.txt")
    with pytest.raises(ValueError, match="within outputs_dir"):
        _require_within(str(outputs), str(outside))

    link = outputs / "outside-link"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    with pytest.raises(ValueError, match="within outputs_dir"):
        _require_within(str(outputs), str(link / "outside.txt"))


def test_index_paths_skips_paths_outside_outputs_root(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("a\n1\n", encoding="utf-8")

    index = object.__new__(outputs_local.OutputsFtsIndex)
    index.outputs_dir = str(outputs)
    assert index.index_paths([str(outside)]) == {
        "indexed": 0,
        "queued": 0,
        "paths": [],
    }


def test_provenance_and_fingerprint_ledgers_store_relative_paths(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    output = outputs / "results.csv"
    output.write_text("id,value\n1,2\n", encoding="utf-8")
    script = tmp_path / "make_results.py"
    script.write_text("print('v1')\n", encoding="utf-8")

    recorded = annotate.record_provenance(
        str(outputs), str(output), generating_script=str(script),
    )
    assert "error" not in recorded
    provenance_path = outputs / ".meridian-outputs-cache" / "provenance_ledger.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    assert list(provenance) == ["results.csv"]
    assert provenance["results.csv"]["path"] == "results.csv"
    assert provenance["results.csv"]["generating_script"] == "../make_results.py"
    assert str(tmp_path) not in provenance_path.read_text(encoding="utf-8")
    assert annotate.get_provenance(str(outputs), str(output))["path"] == str(output.resolve())

    fingerprint.tag_output(str(output), str(outputs), script_path=str(script))
    fingerprint_path = outputs / ".meridian-outputs-cache" / "fingerprint_ledger.json"
    fingerprint_ledger = json.loads(fingerprint_path.read_text(encoding="utf-8"))
    assert list(fingerprint_ledger) == ["results.csv"]
    assert fingerprint_ledger["results.csv"]["path"] == "results.csv"
    assert fingerprint_ledger["results.csv"]["script_path"] == "../make_results.py"
    assert str(tmp_path) not in fingerprint_path.read_text(encoding="utf-8")

    script.write_text("print('v2')\n", encoding="utf-8")
    stale = fingerprint.check_staleness(str(outputs))
    assert stale[0].is_stale is True
    assert stale[0].path == str(output.resolve())
    assert fingerprint.find_stale_by_script(str(outputs), str(script)) == [str(output.resolve())]


def test_provenance_and_tag_output_reject_outside_outputs_paths(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("a\n1\n", encoding="utf-8")

    record = annotate.record_provenance(str(outputs), str(outside))
    assert "error" in record
    assert "within outputs_dir" in record["error"]
    with pytest.raises(ValueError, match="within outputs_dir"):
        fingerprint.tag_output(str(outside), str(outputs))
    assert fingerprint.script_content_hash(str(outside), outputs_dir=str(outputs)) is None
    assert not (outputs / ".meridian-outputs-cache").exists()


def test_ledger_updates_scrub_legacy_absolute_path_rows(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    cache = outputs / ".meridian-outputs-cache"
    cache.mkdir(parents=True)
    legacy_output = outputs / "legacy.csv"
    legacy_output.write_text("a\n1\n", encoding="utf-8")
    current_output = outputs / "current.csv"
    current_output.write_text("a\n2\n", encoding="utf-8")
    script = tmp_path / "make_outputs.py"
    script.write_text("print('v1')\n", encoding="utf-8")

    provenance_path = cache / "provenance_ledger.json"
    provenance_path.write_text(
        json.dumps({
            str(legacy_output.resolve()): {
                "path": str(legacy_output.resolve()),
                "generating_script": str(script.resolve()),
            },
        }),
        encoding="utf-8",
    )
    fingerprint_path = cache / "fingerprint_ledger.json"
    fingerprint_path.write_text(
        json.dumps({
            str(legacy_output.resolve()): {
                "path": str(legacy_output.resolve()),
                "script_path": str(script.resolve()),
                "generating_script": str(script.resolve()),
                "script_hash": fingerprint.script_content_hash(str(script)),
            },
        }),
        encoding="utf-8",
    )

    annotate.record_provenance(str(outputs), str(current_output))
    fingerprint.tag_output(str(current_output), str(outputs), script_path=str(script))

    assert str(tmp_path) not in provenance_path.read_text(encoding="utf-8")
    assert str(tmp_path) not in fingerprint_path.read_text(encoding="utf-8")
    assert "legacy.csv" in json.loads(provenance_path.read_text(encoding="utf-8"))
    assert "legacy.csv" in json.loads(fingerprint_path.read_text(encoding="utf-8"))
