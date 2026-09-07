"""Top-level, CI-visible manifest-consistency coverage for
``tools/meridian_fallbacks/capability_manifest.json`` (sprint item 8c047a44,
"DOCS-R2-E: make convergence, reindex, local-pointer, and manifest evidence
explicit and generated").

WHY THIS FILE EXISTS AT THE TOP LEVEL (not just under
``tools/meridian_fallbacks/tests/``): ``pixi.toml``'s ``test`` task only
globs ``tests/`` (``python scripts/run_tests.py tests/ ...``), and nothing
under ``.github/`` references ``tools/meridian_fallbacks`` at all -- the
existing ``tools/meridian_fallbacks/tests/test_capability_parity.py`` (which
already has its OWN hash/size-parity assertion,
``TestManifestModuleByteParity::
test_every_manifest_module_hash_and_size_match_real_file``) is invisible to
both CI and the documented ``pixi run test`` entry point. That invisibility
is the direct, confirmed reason the ``figure_slot_manifest.py`` drift this
item's discovery phase found (commit ``55cf5006`` edited the file without a
manifest refresh) shipped silently: nothing in the normal test run ever
caught it. This file ports the essential parity assertion to a location CI
and ``pixi run test`` both actually collect, and adds new coverage for the
generator (``tools/meridian_fallbacks/generate_capability_manifest.py``,
new in this item) that ``test_capability_parity.py`` has no equivalent of.

This file does NOT replace ``tools/meridian_fallbacks/tests/
test_capability_parity.py`` -- that file's other seven concerns (module
inventory completeness, ``related_sprint_items`` landed-status, the
``fallback_chain_example`` schema-validity, cross-package status-string
parity, the mcp pin regression guard, ``implementation_notes`` resolvability,
and the end-to-end fallback scenarios) are untouched and still exercised
whenever that file happens to run. Fixing pixi.toml's test-discovery glob to
also pick up ``tools/*/tests/`` is real, valuable follow-up work but is a
repo-wide test-topology change out of this item's own declared scope.
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools.meridian_fallbacks import generate_capability_manifest as gen

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PKG_DIR = _REPO_ROOT / "tools" / "meridian_fallbacks"
_MANIFEST_PATH = _PKG_DIR / "capability_manifest.json"


@pytest.fixture
def manifest_text() -> str:
    return _MANIFEST_PATH.read_text(encoding="utf-8")


@pytest.fixture
def manifest(manifest_text: str) -> dict:
    return json.loads(manifest_text)


# ---------------------------------------------------------------------------
# The parity assertion itself, ported to a CI-visible location.
# ---------------------------------------------------------------------------

class TestManifestMatchesRealFilesOnDisk:
    def test_every_manifest_module_hash_and_size_match_real_file(self, manifest):
        """Ports tools/meridian_fallbacks/tests/test_capability_parity.py's
        own assertion of the same name -- kept intentionally in sync in
        spirit (same failure message shape), not merely a duplicate: this
        copy is the one CI and `pixi run test` actually collect."""
        drift = gen.compute_module_drift(manifest, pkg_dir=_PKG_DIR)
        assert not drift, (
            "capability_manifest.json has drifted from the tracked source:\n"
            + "\n".join(
                f"{d['module']}: manifest sha256={d['recorded_sha256']} "
                f"size={d['recorded_byte_size']} but actual "
                f"sha256={d['actual_sha256']} size={d['actual_byte_size']}"
                for d in drift
            )
        )

    def test_every_manifest_module_file_exists(self, manifest):
        for mod_name, entry in manifest["modules"].items():
            path = _PKG_DIR / entry["file"]
            assert path.is_file(), (
                f"manifest module {mod_name!r} names a file that does not exist: {path}"
            )


# ---------------------------------------------------------------------------
# compute_module_drift -- pure function, exercised directly against
# synthetic manifests (no real files touched).
# ---------------------------------------------------------------------------

class TestComputeModuleDrift:
    def test_returns_empty_list_when_converged(self, tmp_path):
        real = tmp_path / "mod.py"
        real.write_text("x = 1\n", encoding="utf-8")
        sha256, size = gen._sha256_and_size(real)
        manifest = {"modules": {"mod": {"file": "mod.py", "sha256": sha256, "byte_size": size}}}
        assert gen.compute_module_drift(manifest, pkg_dir=tmp_path) == []

    def test_reports_a_sha256_mismatch(self, tmp_path):
        real = tmp_path / "mod.py"
        real.write_text("x = 1\n", encoding="utf-8")
        manifest = {"modules": {"mod": {"file": "mod.py", "sha256": "0" * 64, "byte_size": 6}}}
        drift = gen.compute_module_drift(manifest, pkg_dir=tmp_path)
        assert len(drift) == 1
        assert drift[0]["module"] == "mod"
        assert drift[0]["recorded_sha256"] == "0" * 64
        assert drift[0]["actual_sha256"] != "0" * 64

    def test_reports_a_byte_size_mismatch(self, tmp_path):
        real = tmp_path / "mod.py"
        real.write_text("x = 1\n", encoding="utf-8")
        sha256, size = gen._sha256_and_size(real)
        manifest = {"modules": {"mod": {"file": "mod.py", "sha256": sha256, "byte_size": size + 1}}}
        drift = gen.compute_module_drift(manifest, pkg_dir=tmp_path)
        assert len(drift) == 1
        assert drift[0]["actual_byte_size"] == size

    def test_raises_file_not_found_for_a_missing_module_file(self, tmp_path):
        manifest = {"modules": {"gone": {"file": "does_not_exist.py", "sha256": "x", "byte_size": 0}}}
        with pytest.raises(FileNotFoundError):
            gen.compute_module_drift(manifest, pkg_dir=tmp_path)


# ---------------------------------------------------------------------------
# regenerate_manifest_text -- the generator's write path, and its
# round-trip/idempotence guarantee.
# ---------------------------------------------------------------------------

class TestRegenerateManifestText:
    def test_no_op_on_an_already_converged_manifest(self, manifest_text, manifest):
        """Regenerating a manifest that already matches reality must change
        NOTHING -- byte-for-byte identical text, zero reported drift. This
        is the generator's own idempotence guarantee: run it twice in a row
        and the second run is always a no-op."""
        new_text, drift = gen.regenerate_manifest_text(manifest_text, manifest, pkg_dir=_PKG_DIR)
        assert drift == []
        assert new_text == manifest_text

    def test_fixes_an_injected_sha256_and_byte_size_drift_and_nothing_else(
        self, manifest_text, manifest,
    ):
        """The core round-trip claim: inject a wrong sha256/byte_size for one
        real module (figure_slot_manifest, chosen because it's the module
        this item's discovery phase actually found drifted), regenerate, and
        prove (a) the two value fields now exactly match the real file's
        current sha256/byte_size, and (b) the rest of the manifest text is
        completely untouched -- no reformatting, no reordering, no collateral
        edits to any other module's entry.
        """
        drifted_manifest = copy.deepcopy(manifest)
        drifted_manifest["modules"]["figure_slot_manifest"]["sha256"] = "f" * 64
        drifted_manifest["modules"]["figure_slot_manifest"]["byte_size"] = 1
        drifted_text = manifest_text.replace(
            manifest["modules"]["figure_slot_manifest"]["sha256"], "f" * 64,
        ).replace(
            f'"byte_size": {manifest["modules"]["figure_slot_manifest"]["byte_size"]}',
            '"byte_size": 1',
            1,
        )
        # Sanity: the naive text-level injection above actually landed where
        # we think it did before we test the generator against it.
        assert json.loads(drifted_text)["modules"]["figure_slot_manifest"]["sha256"] == "f" * 64

        real_path = _PKG_DIR / manifest["modules"]["figure_slot_manifest"]["file"]
        real_sha256, real_size = gen._sha256_and_size(real_path)

        new_text, drift = gen.regenerate_manifest_text(
            drifted_text, json.loads(drifted_text), pkg_dir=_PKG_DIR,
        )
        assert [d["module"] for d in drift] == ["figure_slot_manifest"]
        fixed = json.loads(new_text)
        assert fixed["modules"]["figure_slot_manifest"]["sha256"] == real_sha256
        assert fixed["modules"]["figure_slot_manifest"]["byte_size"] == real_size

        # Round-trips EXACTLY back to the real, tracked manifest text: fixing
        # the one injected drift and nothing else must reproduce the
        # genuinely-committed file byte-for-byte.
        assert new_text == manifest_text

    def test_raises_for_a_missing_module_file(self, manifest_text, manifest):
        broken = copy.deepcopy(manifest)
        broken["modules"]["mod_gone"] = {
            "file": "does_not_exist.py", "sha256": "x", "byte_size": 0,
        }
        with pytest.raises(FileNotFoundError):
            gen.regenerate_manifest_text(manifest_text, broken, pkg_dir=_PKG_DIR)


# ---------------------------------------------------------------------------
# CLI entry point -- exercised as a real subprocess against a throwaway copy
# of the manifest, proving --check / --write behave as documented end to end.
# ---------------------------------------------------------------------------

class TestGeneratorCli:
    def _run(self, *args: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            [sys.executable, "-m", "tools.meridian_fallbacks.generate_capability_manifest", *args],
            cwd=_REPO_ROOT, capture_output=True, text=True, timeout=30,
        )

    def _copy_pkg(self, tmp_path: Path) -> Path:
        """Copy the whole real package directory (manifest + every real .py
        module it documents) into *tmp_path* so a CLI run against the copied
        manifest resolves ``pkg_dir`` (``--manifest-path``'s parent) to a
        directory that actually contains every module file the manifest
        names -- the CLI has no separate pkg-dir override, it always derives
        it from the manifest path. Returns the path to the copied
        ``capability_manifest.json``.
        """
        pkg_copy = tmp_path / "meridian_fallbacks"
        shutil.copytree(_PKG_DIR, pkg_copy, ignore=shutil.ignore_patterns("__pycache__"))
        return pkg_copy / "capability_manifest.json"

    def test_check_exits_zero_against_the_real_converged_manifest(self):
        result = self._run("--check")
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_check_exits_one_against_a_drifted_copy(self, tmp_path):
        drifted = self._copy_pkg(tmp_path)
        manifest = json.loads(drifted.read_text(encoding="utf-8"))
        manifest["modules"]["figure_slot_manifest"]["sha256"] = "0" * 64
        drifted.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        result = self._run("--check", "--manifest-path", str(drifted))
        assert result.returncode == 1
        assert "DRIFT DETECTED" in result.stderr
        assert "figure_slot_manifest" in result.stderr

    def test_write_fixes_a_drifted_copy_in_place(self, tmp_path):
        drifted = self._copy_pkg(tmp_path)
        real_text = drifted.read_text(encoding="utf-8")
        real_size = json.loads(real_text)["modules"]["figure_slot_manifest"]["byte_size"]
        drifted.write_text(
            real_text.replace(f'"byte_size": {real_size}', '"byte_size": 1', 1),
            encoding="utf-8",
        )
        result = self._run("--write", "--manifest-path", str(drifted))
        assert result.returncode == 0, result.stderr
        assert "Wrote" in result.stdout

        fixed = json.loads(drifted.read_text(encoding="utf-8"))
        real_path = _PKG_DIR / "figure_slot_manifest.py"
        real_sha256, real_size = gen._sha256_and_size(real_path)
        assert fixed["modules"]["figure_slot_manifest"]["sha256"] == real_sha256
        assert fixed["modules"]["figure_slot_manifest"]["byte_size"] == real_size

        # Idempotent: a second --check against the now-fixed copy passes.
        recheck = self._run("--check", "--manifest-path", str(drifted))
        assert recheck.returncode == 0, recheck.stderr

    def test_write_is_a_noop_on_an_already_converged_copy(self, tmp_path):
        drifted = self._copy_pkg(tmp_path)
        real_text = drifted.read_text(encoding="utf-8")
        result = self._run("--write", "--manifest-path", str(drifted))
        assert result.returncode == 0, result.stderr
        assert "nothing to write" in result.stdout
        assert drifted.read_text(encoding="utf-8") == real_text
