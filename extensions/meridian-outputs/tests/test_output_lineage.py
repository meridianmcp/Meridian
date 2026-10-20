"""Tests for provenance.get_output_lineage (gap set C, "joined-lineage").

One view joining an output to its generating sources from all three stores
(artifact-registry source edges, the record_provenance ledger, the
tag_output fingerprint ledger), each source to its own provenance record,
and the artifact's run to its run-manifest inputs. Fail-closed: complete is
False whenever anything is unresolved.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import annotate, artifact_registry, fingerprint
from meridian_outputs import provenance as PV
from meridian_outputs import run_manifest

_ABS_WINDOWS = re.compile(r"[A-Za-z]:[\\/]")


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    """outputs/ (the outputs_dir) beside scripts/ and inputs/ (outside it)."""
    outputs = tmp_path / "outputs"
    scripts = tmp_path / "scripts"
    inputs = tmp_path / "inputs"
    for d in (outputs, scripts, inputs):
        d.mkdir()
    gen = scripts / "gen.py"
    gen.write_text("print('v1')\n", encoding="utf-8")
    out = outputs / "fig.csv"
    out.write_text("a,b\n1,2\n", encoding="utf-8")
    raw = inputs / "raw.csv"
    raw.write_text("x\n1\n", encoding="utf-8")
    return {"root": tmp_path, "outputs": outputs, "gen": gen, "out": out, "raw": raw}


def _assert_no_absolute_path(result: dict, *roots: Path) -> None:
    blob = json.dumps(result)
    for root in roots:
        for spelling in {str(root), os.path.realpath(root), str(root).replace("\\", "/"),
                         os.path.realpath(root).replace("\\", "/")}:
            assert spelling not in blob, f"absolute path leaked: {spelling}"
    if os.name == "nt":
        assert not _ABS_WINDOWS.search(blob), "drive-qualified path leaked"


def _origins(result: dict) -> list[tuple[str, str]]:
    return [(s["origin"], s["locator"]) for s in result["sources"]]


class TestArgumentValidation:
    def test_path_outside_outputs_dir_is_an_error_dict(self, layout) -> None:
        result = PV.get_output_lineage(str(layout["outputs"]), path=str(layout["gen"]))
        assert set(result) == {"error"}
        assert "within outputs_dir" in result["error"]
        _assert_no_absolute_path(result, layout["root"])

    def test_requires_path_or_artifact_id(self, layout) -> None:
        assert PV.get_output_lineage(str(layout["outputs"])) == {
            "error": "path or artifact_id is required",
        }

    def test_missing_outputs_dir(self, tmp_path) -> None:
        assert "error" in PV.get_output_lineage("", path="x.csv")
        assert "error" in PV.get_output_lineage(str(tmp_path / "nope"), path="x.csv")

    @pytest.mark.parametrize("depth", [0, 2, 5, True, "1"])
    def test_depth_other_than_one_is_refused(self, layout, depth) -> None:
        result = PV.get_output_lineage(
            str(layout["outputs"]), path=str(layout["out"]), depth=depth,
        )
        assert "depth must be 1" in result["error"]


class TestSourceUnion:
    def test_annotate_record_and_registry_edge_both_listed_with_origins(self, layout) -> None:
        outputs = str(layout["outputs"])
        annotate.record_provenance(outputs, str(layout["out"]), generating_script=str(layout["gen"]))
        art = artifact_registry.register_artifact(
            outputs, "output", canonical_path=str(layout["out"]), generator="gen.py",
        )
        artifact_registry.bind_source_edge(outputs, art["artifact_id"], "data/upstream.csv")

        result = PV.get_output_lineage(outputs, path=str(layout["out"]))
        assert _origins(result) == [
            ("artifact_registry", "data/upstream.csv"),
            ("provenance_record", "../scripts/gen.py"),
        ]
        assert result["output"]["artifact_id"] == art["artifact_id"]
        assert result["output"]["artifact_evidence"] == "content_hash"
        assert result["output"]["path"] == "fig.csv"
        edge_src = result["sources"][0]
        assert edge_src["relation"] == "produced_by"
        assert edge_src["edge_id"]
        assert result["complete"] is False

    def test_same_script_from_three_stores_is_never_merged(self, layout) -> None:
        outputs = str(layout["outputs"])
        annotate.record_provenance(outputs, str(layout["out"]), generating_script=str(layout["gen"]))
        fingerprint.tag_output(str(layout["out"]), outputs, script_path=str(layout["gen"]))
        art = artifact_registry.register_artifact(
            outputs, "output", canonical_path=str(layout["out"]), generator="gen.py",
        )
        artifact_registry.bind_source_edge(outputs, art["artifact_id"], "../scripts/gen.py")

        result = PV.get_output_lineage(outputs, path=str(layout["out"]))
        assert _origins(result) == [
            ("artifact_registry", "../scripts/gen.py"),
            ("provenance_record", "../scripts/gen.py"),
            ("fingerprint_ledger", "../scripts/gen.py"),
        ]
        assert all(s["status"] == PV.SOURCE_OUTSIDE_OUTPUTS_DIR for s in result["sources"])
        assert {g["origin"] for g in result["unresolved"] if g["kind"] == "source"} == {
            "artifact_registry", "provenance_record", "fingerprint_ledger",
        }

    def test_registry_edge_with_no_ledger_record_is_listed_but_unresolved(self, layout) -> None:
        outputs = str(layout["outputs"])
        upstream = layout["outputs"] / "data" / "upstream.csv"
        upstream.parent.mkdir()
        upstream.write_text("u\n1\n", encoding="utf-8")
        art = artifact_registry.register_artifact(
            outputs, "output", canonical_path=str(layout["out"]), generator="gen.py",
        )
        artifact_registry.bind_source_edge(outputs, art["artifact_id"], "data/upstream.csv")
        artifact_registry.bind_source_edge(outputs, art["artifact_id"], "data/never_written.csv")

        result = PV.get_output_lineage(outputs, path=str(layout["out"]))
        by_locator = {s["locator"]: s for s in result["sources"]}
        assert by_locator["data/upstream.csv"]["status"] == PV.SOURCE_NO_PROVENANCE_RECORD
        assert by_locator["data/upstream.csv"]["location"] == "inside_outputs_dir"
        assert by_locator["data/upstream.csv"]["path"] == "data/upstream.csv"
        assert by_locator["data/never_written.csv"]["status"] == PV.SOURCE_NOT_FOUND
        assert all(s["resolved"] is False for s in result["sources"])
        assert result["complete"] is False
        reasons = {(g["locator"], g["reason"]) for g in result["unresolved"] if g["kind"] == "source"}
        assert reasons == {
            ("data/upstream.csv", "no_provenance_record"),
            ("data/never_written.csv", "not_found"),
        }

    def test_absolute_edge_locator_is_made_portable(self, layout) -> None:
        outputs = str(layout["outputs"])
        art = artifact_registry.register_artifact(
            outputs, "output", canonical_path=str(layout["out"]), generator="gen.py",
        )
        artifact_registry.bind_source_edge(outputs, art["artifact_id"], str(layout["raw"]))
        result = PV.get_output_lineage(outputs, path=str(layout["out"]))
        assert _origins(result) == [("artifact_registry", "../inputs/raw.csv")]
        _assert_no_absolute_path(result, layout["root"])


class TestSourceResolution:
    def test_source_outside_outputs_dir_fails_closed_without_absolute_paths(self, layout) -> None:
        outputs = str(layout["outputs"])
        annotate.record_provenance(outputs, str(layout["out"]), generating_script=str(layout["gen"]))
        result = PV.get_output_lineage(outputs, path=str(layout["out"]))
        (src,) = result["sources"]
        assert src["status"] == "not_recorded_outside_outputs_dir"
        assert src["location"] == "outside_outputs_dir"
        assert src["resolved"] is False
        assert src["path"] is None
        assert src["provenance_record"] is None
        assert result["complete"] is False
        assert {"kind": "source", "origin": "provenance_record",
                "locator": "../scripts/gen.py",
                "reason": "not_recorded_outside_outputs_dir"} in result["unresolved"]
        _assert_no_absolute_path(result, layout["root"])

    def test_in_tree_recorded_source_resolves_and_lineage_is_complete(self, layout) -> None:
        outputs = str(layout["outputs"])
        upstream = layout["outputs"] / "upstream.csv"
        upstream.write_text("u\n1\n", encoding="utf-8")
        annotate.record_provenance(outputs, str(upstream), generating_script="fetch.py",
                                   params={"seed": 1})
        annotate.record_provenance(outputs, str(layout["out"]), generating_script="upstream.csv")

        result = PV.get_output_lineage(outputs, path=str(layout["out"]))
        (src,) = result["sources"]
        assert src["origin"] == "provenance_record"
        assert src["status"] == PV.SOURCE_RECORDED
        assert src["resolved"] is True
        assert src["path"] == "upstream.csv"
        assert src["provenance_record"]["path"] == "upstream.csv"
        assert src["provenance_record"]["generating_script"] == "fetch.py"
        assert src["provenance_record"]["params"] == {"seed": 1}
        assert src["stale"] is False
        assert result["unresolved"] == []
        assert result["complete"] is True
        _assert_no_absolute_path(result, layout["root"])

    def test_store_failure_is_a_gap_not_a_traceback(self, layout, monkeypatch) -> None:
        outputs = str(layout["outputs"])
        annotate.record_provenance(outputs, str(layout["out"]), generating_script="upstream.csv")

        def _boom(_outputs_dir):
            raise RuntimeError("corrupt ledger")

        monkeypatch.setattr(fingerprint, "_read_ledger", _boom)
        result = PV.get_output_lineage(outputs, path=str(layout["out"]))
        assert {"kind": "store", "store": "fingerprint_ledger",
                "reason": "store_unreadable", "error_type": "RuntimeError"} in result["unresolved"]
        assert result["complete"] is False

    def test_registry_resolution_failure_is_a_gap(self, layout, monkeypatch) -> None:
        def _boom(*_a, **_k):
            raise OSError("registry unreadable")

        monkeypatch.setattr(artifact_registry, "resolve_artifact", _boom)
        result = PV.get_output_lineage(str(layout["outputs"]), path=str(layout["out"]))
        assert {"kind": "store", "store": "artifact_registry",
                "reason": "store_unreadable", "error_type": "OSError"} in result["unresolved"]

    def test_edge_read_failure_is_a_gap(self, layout, monkeypatch) -> None:
        outputs = str(layout["outputs"])
        artifact_registry.register_artifact(
            outputs, "output", canonical_path=str(layout["out"]), generator="gen.py",
        )

        def _boom(*_a, **_k):
            raise ValueError("edges unreadable")

        monkeypatch.setattr(artifact_registry, "get_artifact_sources", _boom)
        result = PV.get_output_lineage(outputs, path=str(layout["out"]))
        assert result["sources"] == []
        assert {"kind": "store", "store": "artifact_registry",
                "reason": "store_unreadable", "error_type": "ValueError"} in result["unresolved"]

    def test_provenance_store_failure_marks_output_and_sources(self, layout, monkeypatch) -> None:
        outputs = str(layout["outputs"])
        art = artifact_registry.register_artifact(
            outputs, "output", canonical_path=str(layout["out"]), generator="gen.py",
        )
        artifact_registry.bind_source_edge(outputs, art["artifact_id"], "data/upstream.csv")

        def _boom(*_a, **_k):
            raise RuntimeError("provenance ledger unreadable")

        monkeypatch.setattr(annotate, "get_provenance", _boom)
        result = PV.get_output_lineage(outputs, path=str(layout["out"]))
        assert {"kind": "store", "store": "provenance_record",
                "reason": "store_unreadable", "error_type": "RuntimeError"} in result["unresolved"]
        (src,) = result["sources"]
        assert src["store_error"] == "RuntimeError"
        assert src["resolved"] is False
        gap = next(g for g in result["unresolved"] if g["kind"] == "source")
        assert gap["error_type"] == "RuntimeError"

    def test_run_manifest_store_failure_is_a_gap(self, layout, monkeypatch) -> None:
        outputs = str(layout["outputs"])
        artifact_registry.register_artifact(
            outputs, "output", canonical_path=str(layout["out"]), generator="gen.py",
            run_id="run-x",
        )

        def _boom(*_a, **_k):
            raise RuntimeError("run ledger unreadable")

        monkeypatch.setattr(run_manifest, "get_run_manifest", _boom)
        result = PV.get_output_lineage(outputs, path=str(layout["out"]))
        assert {"kind": "store", "store": "run_manifest",
                "reason": "store_unreadable", "error_type": "RuntimeError"} in result["unresolved"]
        assert result["run"]["found"] is False


class TestStaleness:
    def test_script_changed_after_tagging_sets_stale(self, layout) -> None:
        outputs = str(layout["outputs"])
        fingerprint.tag_output(str(layout["out"]), outputs, script_path=str(layout["gen"]))
        fresh = PV.get_output_lineage(outputs, path=str(layout["out"]))
        (src,) = fresh["sources"]
        assert src["origin"] == "fingerprint_ledger"
        assert src["stale"] is False

        layout["gen"].write_text("print('v2 -- bug fixed')\n", encoding="utf-8")
        changed = PV.get_output_lineage(outputs, path=str(layout["out"]))
        (src,) = changed["sources"]
        assert src["stale"] is True
        assert src["stale_reason"] == "generating script content changed since it was recorded"
        assert src["recorded_script_hash"] != src["current_script_hash"]
        assert changed["output"]["fingerprint"]["script_path"] == "../scripts/gen.py"

    def test_deleted_script_is_stale(self, layout) -> None:
        outputs = str(layout["outputs"])
        fingerprint.tag_output(str(layout["out"]), outputs, script_path=str(layout["gen"]))
        layout["gen"].unlink()
        (src,) = PV.get_output_lineage(outputs, path=str(layout["out"]))["sources"]
        assert src["stale"] is True
        assert src["current_script_hash"] is None

    def test_provenance_record_script_hash_is_compared_too(self, layout) -> None:
        outputs = str(layout["outputs"])
        annotate.record_provenance(outputs, str(layout["out"]), generating_script=str(layout["gen"]))
        layout["gen"].write_text("print('edited')\n", encoding="utf-8")
        (src,) = PV.get_output_lineage(outputs, path=str(layout["out"]))["sources"]
        assert src["origin"] == "provenance_record"
        assert src["stale"] is True

    def test_legacy_tag_without_recorded_hash_reports_unknown_staleness(self, layout) -> None:
        outputs = str(layout["outputs"])
        fingerprint.tag_output(str(layout["out"]), outputs, script_path=str(layout["gen"]))
        ledger_file = Path(fingerprint._ledger_path(outputs))
        ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
        for entry in ledger.values():
            entry.pop("script_hash", None)
            entry.pop("content_digest", None)
        ledger_file.write_text(json.dumps(ledger), encoding="utf-8")
        (src,) = PV.get_output_lineage(outputs, path=str(layout["out"]))["sources"]
        assert src["stale"] is None
        assert src["recorded_script_hash"] is None
        assert src["current_script_hash"] == fingerprint.script_content_hash(str(layout["gen"]))

    def test_tag_without_resolvable_script_reports_unknown_staleness(
        self, layout, monkeypatch,
    ) -> None:
        outputs = str(layout["outputs"])
        json_out = layout["outputs"] / "run.json"
        json_out.write_text('{"generating_script": "nowhere_to_be_found.py"}', encoding="utf-8")
        monkeypatch.chdir(layout["root"])  # nothing named that under cwd either
        fingerprint.tag_output(str(json_out), outputs)
        (src,) = PV.get_output_lineage(outputs, path=str(json_out))["sources"]
        assert src["origin"] == "fingerprint_ledger"
        assert src["locator"] == "nowhere_to_be_found.py"
        assert src["stale"] is None
        assert src["status"] == PV.SOURCE_NOT_FOUND


class TestRunAndArtifact:
    def test_run_inputs_are_attached_with_run_scope(self, layout) -> None:
        outputs = str(layout["outputs"])
        run_manifest.start_run_manifest(
            outputs, run_id="run-1", command_name="gen",
            input_paths=[str(layout["raw"])],
        )
        run_manifest.finalize_run_manifest(outputs, "run-1", output_paths=[str(layout["out"])])
        artifact_registry.register_artifact(
            outputs, "output", canonical_path=str(layout["out"]), generator="gen.py",
            run_id="run-1",
        )
        result = PV.get_output_lineage(outputs, path=str(layout["out"]))
        run = result["run"]
        assert run["run_id"] == "run-1"
        assert run["found"] is True
        assert run["phase"] == "complete"
        assert run["command"] == "gen"
        assert run["inputs"] == [{
            "locator": "../inputs/raw.csv",
            "sha256": fingerprint.script_content_hash(str(layout["raw"])),
            "scope": "run",
            "origin": "run_manifest",
        }]
        assert all(s["scope"] == "output" for s in result["sources"])
        _assert_no_absolute_path(result, layout["root"])

    def test_run_id_without_manifest_is_unresolved(self, layout) -> None:
        outputs = str(layout["outputs"])
        artifact_registry.register_artifact(
            outputs, "output", canonical_path=str(layout["out"]), generator="gen.py",
            run_id="ghost-run",
        )
        result = PV.get_output_lineage(outputs, path=str(layout["out"]))
        assert result["run"]["found"] is False
        assert {"kind": "run", "run_id": "ghost-run",
                "reason": "run_manifest_not_found"} in result["unresolved"]
        assert result["complete"] is False

    def test_lookup_by_artifact_id_uses_its_local_sighting(self, layout) -> None:
        outputs = str(layout["outputs"])
        annotate.record_provenance(outputs, str(layout["out"]), generating_script="upstream.csv")
        art = artifact_registry.register_artifact(
            outputs, "output", canonical_path=str(layout["out"]), generator="gen.py",
        )
        result = PV.get_output_lineage(outputs, artifact_id=art["artifact_id"])
        assert result["output"]["path"] == "fig.csv"
        assert result["output"]["artifact_evidence"] == "artifact_id"
        assert "local_only_path" not in json.dumps(result["output"]["artifact"])
        assert _origins(result) == [("provenance_record", "upstream.csv")]
        _assert_no_absolute_path(result, layout["root"])

    def test_artifact_without_in_tree_sighting_reports_unknown_output(self, layout) -> None:
        outputs = str(layout["outputs"])
        art = artifact_registry.register_artifact(
            outputs, "output", canonical_path=str(layout["raw"]), generator="gen.py",
        )
        result = PV.get_output_lineage(outputs, artifact_id=art["artifact_id"])
        assert result["output"]["path"] is None
        assert {"kind": "output", "artifact_id": art["artifact_id"],
                "reason": "output_path_unknown"} in result["unresolved"]
        assert result["complete"] is False
        _assert_no_absolute_path(result, layout["root"])

    def test_unknown_artifact_id_fails_closed(self, layout) -> None:
        result = PV.get_output_lineage(str(layout["outputs"]), artifact_id="no-such-id")
        assert result["output"]["artifact_id"] == "no-such-id"
        assert result["sources"] == []
        assert result["complete"] is False
        assert {"kind": "artifact", "artifact_id": "no-such-id",
                "reason": "artifact_not_registered"} in result["unresolved"]

    def test_ambiguous_artifact_match_is_reported_not_guessed(self, layout) -> None:
        outputs = str(layout["outputs"])
        twin = layout["outputs"] / "twin.csv"
        twin.write_bytes(layout["out"].read_bytes())
        a = artifact_registry.register_artifact(
            outputs, "output", canonical_path=str(layout["out"]), generator="gen_a.py",
        )
        b = artifact_registry.register_artifact(
            outputs, "output", canonical_path=str(twin), generator="gen_b.py",
        )
        result = PV.get_output_lineage(outputs, path=str(layout["out"]))
        assert result["output"]["artifact_id"] is None
        gap = next(g for g in result["unresolved"] if g["kind"] == "artifact")
        assert gap["reason"] == "artifact_ambiguous"
        assert sorted(gap["candidates"]) == sorted([a["artifact_id"], b["artifact_id"]])


class TestShapeAndDeterminism:
    def test_nothing_recorded_is_incomplete_with_no_sources(self, layout) -> None:
        result = PV.get_output_lineage(str(layout["outputs"]), path=str(layout["out"]))
        assert set(result) == {"output", "sources", "run", "complete", "unresolved"}
        assert result["sources"] == []
        assert result["run"] is None
        assert result["complete"] is False
        assert result["unresolved"] == [{"kind": "output", "reason": "no_source_recorded"}]
        assert result["output"]["provenance_record"] is None
        assert result["output"]["fingerprint"] is None
        assert result["output"]["artifact"] is None

    def test_repeat_call_is_identical_and_ordered(self, layout) -> None:
        outputs = str(layout["outputs"])
        annotate.record_provenance(outputs, str(layout["out"]), generating_script=str(layout["gen"]))
        fingerprint.tag_output(str(layout["out"]), outputs, script_path=str(layout["gen"]))
        art = artifact_registry.register_artifact(
            outputs, "output", canonical_path=str(layout["out"]), generator="gen.py",
        )
        for locator in ("zeta.csv", "alpha.csv", "mid.csv"):
            artifact_registry.bind_source_edge(outputs, art["artifact_id"], locator)
        first = PV.get_output_lineage(outputs, path=str(layout["out"]))
        second = PV.get_output_lineage(outputs, path=str(layout["out"]))
        assert first == second
        assert _origins(first) == [
            ("artifact_registry", "alpha.csv"),
            ("artifact_registry", "mid.csv"),
            ("artifact_registry", "zeta.csv"),
            ("provenance_record", "../scripts/gen.py"),
            ("fingerprint_ledger", "../scripts/gen.py"),
        ]

    def test_output_provenance_status_summary_is_attached(self, layout) -> None:
        outputs = str(layout["outputs"])
        annotate.record_provenance(outputs, str(layout["out"]), generating_script="upstream.csv")
        status = PV.get_output_lineage(outputs, path=str(layout["out"]))["output"]["provenance_status"]
        assert set(status) == {"provenance_type", "stale", "inconclusive"}
        assert status["provenance_type"] in {"exact", "stale_by_script"}


class TestWiring:
    def test_mcp_tool_registered_and_forwards(self, layout, monkeypatch) -> None:
        from meridian_outputs import server as srv

        assert "get_output_lineage" in {t.name for t in srv.mcp._tool_manager.list_tools()}
        seen: dict = {}

        def _spy(outputs_dir, **kwargs):
            seen.update(outputs_dir=outputs_dir, **kwargs)
            return {"ok": True}

        monkeypatch.setattr(PV, "get_output_lineage", _spy)
        assert srv.get_output_lineage("o", path="p", artifact_id="a", depth=1) == {"ok": True}
        assert seen == {"outputs_dir": "o", "path": "p", "artifact_id": "a", "depth": 1}

    def test_readme_documents_the_tool(self) -> None:
        readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
        assert "`get_output_lineage`" in readme
