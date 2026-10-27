"""Per-output reproduction record (meridian_outputs.environment).

record_provenance already stored the generating script, its content hash and
the params. These tests pin the rest of what a reader needs to reproduce an
output: the interpreter, platform, package version and the installed
versions of the direct runtime dependencies (``reproduction``), an optional
``run_id`` linking the output to its run manifest, and backward compatibility
for ledger rows written before either field existed.
"""
from __future__ import annotations

import importlib.metadata
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import meridian_outputs
from meridian_outputs import annotate as AN
from meridian_outputs import environment as ENV
from meridian_outputs import outputs_local as OL
from meridian_outputs import run_manifest as RM

_ENV_KEYS = {
    "schema_version", "python_version", "platform",
    "package_name", "package_version", "dependencies",
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh environment cache per test, and no DuckDB priority indexing
    (best-effort, not under test here)."""
    monkeypatch.setattr(ENV, "_cache", {})
    monkeypatch.setattr(OL, "register_priority_path", lambda *a, **k: None)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestCaptureEnvironment:
    def test_default_snapshot_shape_and_values(self) -> None:
        env = ENV.capture_environment()
        assert set(env) == _ENV_KEYS
        assert env["schema_version"] == ENV.ENVIRONMENT_SCHEMA_VERSION
        assert env["python_version"] == sys.version.split()[0]
        assert env["package_name"] == "meridian_outputs"
        assert env["package_version"] == meridian_outputs.__version__
        assert isinstance(env["platform"], str) and env["platform"]
        assert set(env["dependencies"]) == {
            "duckdb", "tantivy", "xxhash", "mcp", "watchdog", "pyarrow", "psutil", "pypdf",
        }

    def test_installed_dependency_reports_its_metadata_version(self) -> None:
        env = ENV.capture_environment(["pytest"])
        assert env["dependencies"] == {"pytest": importlib.metadata.version("pytest")}

    def test_missing_or_broken_dependency_is_none_and_never_raises(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _version(name: str) -> str:
            if name == "missing-dist":
                raise importlib.metadata.PackageNotFoundError(name)
            if name == "broken-dist":
                raise RuntimeError("corrupt METADATA")
            return "9.9.9"

        monkeypatch.setattr(ENV.importlib.metadata, "version", _version)
        env = ENV.capture_environment(["missing-dist", "broken-dist", "fine-dist"])
        assert env["dependencies"] == {
            "broken-dist": None, "fine-dist": "9.9.9", "missing-dist": None,
        }

    def test_cached_per_process_and_returns_copies(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = {"n": 0}
        real = importlib.metadata.version

        def _counting(name: str) -> str:
            calls["n"] += 1
            return real(name)

        monkeypatch.setattr(ENV.importlib.metadata, "version", _counting)
        first = ENV.capture_environment(["pytest"])
        first["dependencies"]["pytest"] = "tampered"
        second = ENV.capture_environment(["pytest"])
        assert calls["n"] == 1
        assert second["dependencies"]["pytest"] == real("pytest")

    def test_platform_failure_degrades_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> str:
            raise OSError("no platform")

        monkeypatch.setattr(ENV.platform, "platform", _boom)
        assert ENV.capture_environment([])["platform"] is None

    def test_no_command_line_is_captured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["run.py", "--token", "sk_secret_value"])
        env = ENV.capture_environment()
        assert "sk_secret_value" not in json.dumps(env)
        assert set(env) == _ENV_KEYS


class TestRecordProvenanceReproduction:
    def test_record_stores_reproduction(self, tmp_path: Path) -> None:
        out = _write(tmp_path / "a.csv", "x\n1\n")
        result = AN.record_provenance(str(tmp_path), str(out))
        repro = result["reproduction"]
        assert repro["python_version"] == sys.version.split()[0]
        assert repro["package_version"] == meridian_outputs.__version__
        assert repro == ENV.capture_environment()

        stored = json.loads(
            (tmp_path / ".meridian-outputs-cache" / "provenance_ledger.json").read_text(encoding="utf-8")
        )["a.csv"]
        assert stored["reproduction"] == repro
        assert AN.get_provenance(str(tmp_path), str(out))["reproduction"] == repro

    def test_params_round_trip_exactly(self, tmp_path: Path) -> None:
        out = _write(tmp_path / "a.csv", "x\n1\n")
        params = {
            "lr": 0.01, "epochs": 3, "use_pca": False, "layers": [64, 32],
            "nested": {"seed": 7, "name": "run-a", "none": None},
        }
        AN.record_provenance(str(tmp_path), str(out), params=params)
        assert AN.get_provenance(str(tmp_path), str(out))["params"] == params
        assert AN.list_provenance(str(tmp_path))[0]["params"] == params

    def test_run_id_is_stored_and_joins_to_the_run_manifest(self, tmp_path: Path) -> None:
        manifest = RM.start_run_manifest(
            str(tmp_path), run_id="run-42", command_name="build",
            _git_runner=lambda argv: None,
        )
        out = _write(tmp_path / "a.csv", "x\n1\n")
        result = AN.record_provenance(str(tmp_path), str(out), run_id="run-42")
        assert result["run_id"] == "run-42"
        record = AN.get_provenance(str(tmp_path), str(out))
        assert record["run_id"] == "run-42"
        assert RM.get_run_manifest(str(tmp_path), record["run_id"]) is not None

        repro = record["reproduction"]
        assert manifest["package_identity"] == {
            "name": repro["package_name"],
            "version": repro["package_version"],
            "python_version": repro["python_version"],
            "platform": repro["platform"],
        }

    def test_run_id_defaults_to_none(self, tmp_path: Path) -> None:
        out = _write(tmp_path / "a.csv", "x\n1\n")
        assert AN.record_provenance(str(tmp_path), str(out))["run_id"] is None

    def test_legacy_row_without_reproduction_is_returned_unchanged(self, tmp_path: Path) -> None:
        cache = tmp_path / ".meridian-outputs-cache"
        cache.mkdir()
        out = _write(tmp_path / "old.csv", "x\n1\n")
        legacy = {
            "path": "old.csv", "generating_script": "train.py", "params": {"lr": 0.1},
            "sprint_item_id": None, "decision_id": None, "note": "pre-reproduction",
            "recorded_at": 1.0, "recorded_at_iso": "1970-01-01T00:00:01+00:00",
            "content_hash": None,
        }
        (cache / "provenance_ledger.json").write_text(
            json.dumps({"old.csv": legacy}), encoding="utf-8",
        )
        expected = dict(legacy, path=str(out.resolve()))
        assert AN.get_provenance(str(tmp_path), str(out)) == expected
        assert AN.list_provenance(str(tmp_path)) == [expected]

    def test_run_manifest_still_idempotent_with_shared_helper(self, tmp_path: Path) -> None:
        a = RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        ENV._cache.clear()  # a recomputed snapshot must hash identically
        b = RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        assert a["manifest_hash"] == b["manifest_hash"]
        assert set(a["package_identity"]) == {"name", "version", "python_version", "platform"}


class TestServerWrapper:
    def test_record_provenance_tool_forwards_run_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pytest.importorskip("mcp")
        from meridian_outputs import server

        out = _write(tmp_path / "a.csv", "x\n1\n")
        result = server.record_provenance(str(tmp_path), str(out), run_id="run-7")
        assert result["run_id"] == "run-7"
        assert AN.get_provenance(str(tmp_path), str(out))["run_id"] == "run-7"

        seen: dict = {}

        def _capture(outputs_dir, path, **kwargs):
            seen.update(kwargs)
            return {}

        monkeypatch.setattr(server.annotate, "record_provenance", _capture)
        server.record_provenance(str(tmp_path), str(out), run_id="run-8")
        assert seen["run_id"] == "run-8"
