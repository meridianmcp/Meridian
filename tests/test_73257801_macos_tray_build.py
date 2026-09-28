"""73257801 -- regression coverage for the macOS tray build.

meridian/tray_main.py itself has ZERO platform-specific code (confirmed by
grep for win32/sys.platform/platform.system/darwin before this item was
worked -- see the sprint item's own notes). The only place platform
actually matters is meridian-tray.spec (PyInstaller packaging) and the new
build-tray-mac CI job in .github/workflows/release.yml. This file is a
PACKAGING/CI regression test, not an application-logic one -- it would have
caught:

  * reverting meridian-tray.spec's darwin branch back to a Windows-only
    hardcoded ``pystray._win32`` hiddenimport (which makes PyInstaller's own
    Analysis step fail outright when it actually runs on a macOS runner);
  * building a bare onefile Mach-O binary on macOS instead of a real .app
    bundle (pystray's menu-bar icon does not work outside a bundle);
  * a missing/misconfigured build-tray-mac job, or a YAML syntax error in
    release.yml, or the release job shipping without the new artifact.

Runs entirely offline: the spec file's PyInstaller globals (Analysis, PYZ,
EXE, COLLECT, BUNDLE) are faked out so this never needs PyInstaller,
pystray, or Pillow installed (those are dev/build-only deps -- see
pixi.toml's [feature.dev.pypi-dependencies] comment) and never needs an
actual macOS runner to check the spec's own branching logic.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "meridian-tray.spec"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"


# ---------------------------------------------------------------------------
# meridian-tray.spec -- platform-conditional PyInstaller build graph
# ---------------------------------------------------------------------------


class _FakeAnalysis:
    """Stand-in for PyInstaller's Analysis(): records what it was given."""

    def __init__(self, scripts: list, **kwargs: Any) -> None:
        self.scripts = scripts
        self.hiddenimports = kwargs.get("hiddenimports", [])
        self.datas = kwargs.get("datas", [])
        # Real Analysis exposes these as build products; the spec file only
        # reads them back (a.pure / a.zipped_data / a.scripts / a.binaries /
        # a.zipfiles / a.datas) to hand to PYZ/EXE/COLLECT.
        self.pure = []
        self.zipped_data = []
        self.binaries = []
        self.zipfiles = []


class _Recorder:
    def __init__(self) -> None:
        self.exe_calls: list[dict] = []
        self.collect_calls: list[dict] = []
        self.bundle_calls: list[dict] = []


def _run_spec(monkeypatch: pytest.MonkeyPatch, platform: str) -> _Recorder:
    """Execute meridian-tray.spec's real source under a faked sys.platform
    and faked PyInstaller globals, and return what got called."""
    monkeypatch.setattr(sys, "platform", platform)
    rec = _Recorder()

    def _fake_pyz(*args: Any, **kwargs: Any) -> str:
        return "PYZ_OBJ"

    def _fake_exe(*args: Any, **kwargs: Any) -> str:
        rec.exe_calls.append({"args": args, "kwargs": kwargs})
        return "EXE_OBJ"

    def _fake_collect(*args: Any, **kwargs: Any) -> str:
        rec.collect_calls.append({"args": args, "kwargs": kwargs})
        return "COLLECT_OBJ"

    def _fake_bundle(*args: Any, **kwargs: Any) -> str:
        rec.bundle_calls.append({"args": args, "kwargs": kwargs})
        return "BUNDLE_OBJ"

    namespace: dict[str, Any] = {
        "__name__": "__pyinstaller_spec__",
        "__file__": str(SPEC_PATH),
        "Analysis": _FakeAnalysis,
        "PYZ": _fake_pyz,
        "EXE": _fake_exe,
        "COLLECT": _fake_collect,
        "BUNDLE": _fake_bundle,
    }
    source = SPEC_PATH.read_text(encoding="utf-8")
    exec(compile(source, str(SPEC_PATH), "exec"), namespace)  # noqa: S102
    rec.namespace = namespace  # type: ignore[attr-defined]
    return rec


def test_spec_file_exists() -> None:
    assert SPEC_PATH.is_file()


def test_windows_hiddenimports_use_win32_backend_only(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _run_spec(monkeypatch, "win32")
    hiddenimports = rec.namespace["a"].hiddenimports  # type: ignore[attr-defined]
    assert "pystray._win32" in hiddenimports
    assert "pystray._darwin" not in hiddenimports


def test_macos_hiddenimports_use_darwin_backend_only(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _run_spec(monkeypatch, "darwin")
    hiddenimports = rec.namespace["a"].hiddenimports  # type: ignore[attr-defined]
    assert "pystray._darwin" in hiddenimports
    assert "pystray._win32" not in hiddenimports


def test_windows_build_is_onefile_exe_with_no_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _run_spec(monkeypatch, "win32")
    assert len(rec.exe_calls) == 1
    assert rec.exe_calls[0]["kwargs"].get("onefile") is True
    assert rec.exe_calls[0]["kwargs"].get("name") == "meridian-tray"
    # Windows never builds an app bundle -- COLLECT/BUNDLE are mac-only.
    assert rec.collect_calls == []
    assert rec.bundle_calls == []


def test_macos_build_produces_app_bundle_via_collect_and_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _run_spec(monkeypatch, "darwin")
    # pystray needs a real .app bundle on macOS for its menu-bar icon to
    # work at all -- a bare onefile binary (onefile=True, Windows' shape)
    # never shows a menu-bar item, so macOS must go through onedir + COLLECT
    # + BUNDLE instead.
    assert len(rec.exe_calls) == 1
    exe_kwargs = rec.exe_calls[0]["kwargs"]
    assert exe_kwargs.get("exclude_binaries") is True
    assert "onefile" not in exe_kwargs

    assert len(rec.collect_calls) == 1
    assert rec.collect_calls[0]["kwargs"].get("name") == "meridian-tray"

    assert len(rec.bundle_calls) == 1
    bundle_kwargs = rec.bundle_calls[0]["kwargs"]
    assert bundle_kwargs.get("name") == "meridian-tray.app"
    # LSUIElement=True keeps this a menu-bar-only app (no Dock icon), same
    # UX intent as the Windows build's console=False.
    assert bundle_kwargs.get("info_plist", {}).get("LSUIElement") is True


def test_macos_bundle_does_not_reference_the_windows_ico_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """meridian-tray.ico is a Windows icon format -- PyInstaller's BUNDLE()
    icon= parameter on macOS needs a .icns file, which doesn't exist yet
    (tracked as a follow-up on the sprint item, not required for v1). A
    regression that started passing the .ico path straight through would
    break the macOS build outright."""
    rec = _run_spec(monkeypatch, "darwin")
    bundle_kwargs = rec.bundle_calls[0]["kwargs"]
    assert bundle_kwargs.get("icon") != "meridian/static/meridian-tray.ico"


# ---------------------------------------------------------------------------
# .github/workflows/release.yml -- the new build-tray-mac CI job
# ---------------------------------------------------------------------------


def _load_release_workflow() -> dict:
    # BaseLoader (not the default full loader) matches this repo's existing
    # workflow-contract test precedent (test_f4ab2787_ci_workflow_contract.py)
    # -- every scalar comes back as a plain string, which is all these
    # structural assertions need and avoids any YAML-tag surprises.
    data = yaml.load(RELEASE_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(data, dict)
    return data


def test_release_workflow_is_syntactically_valid_yaml() -> None:
    # A bare parse failure would raise inside _load_release_workflow already,
    # but assert the top-level shape too so a truncated/malformed file (e.g.
    # a bad indent swallowing every job under one key) doesn't slip through.
    workflow = _load_release_workflow()
    assert "jobs" in workflow
    assert isinstance(workflow["jobs"], dict)


def test_build_tray_mac_job_exists_and_mirrors_the_mac_arm64_runner_choice() -> None:
    jobs = _load_release_workflow()["jobs"]
    assert "build-tray-mac" in jobs
    job = jobs["build-tray-mac"]
    # macos-14 matches build-mac-arm64's existing precedent (Apple Silicon
    # only -- macos-13/Intel is effectively unavailable per decision
    # 80f1d4bc, already accepted for the slim client build).
    assert job["runs-on"] == jobs["build-mac-arm64"]["runs-on"] == "macos-14"


def test_build_tray_mac_job_builds_via_the_shared_pixi_task() -> None:
    jobs = _load_release_workflow()["jobs"]
    steps = jobs["build-tray-mac"]["steps"]
    run_commands = [s["run"] for s in steps if "run" in s]
    # Same pixi task the Windows tray job uses (`build-tray` in pixi.toml is
    # already generic -- `pyinstaller meridian-tray.spec --clean` -- so no
    # new pixi task was needed, only the spec's own platform branch).
    assert any("pixi run -e dev build-tray" in cmd for cmd in run_commands)


def test_build_tray_mac_job_uploads_a_zipped_app_bundle() -> None:
    jobs = _load_release_workflow()["jobs"]
    steps = jobs["build-tray-mac"]["steps"]
    run_commands = [s["run"] for s in steps if "run" in s]
    # ditto (not a bare `zip`) is required to preserve a .app bundle's
    # resource forks/extended attributes -- a plain zip can corrupt them.
    assert any("ditto" in cmd and "meridian-tray.app" in cmd for cmd in run_commands)

    upload_steps = [
        s for s in steps if s.get("uses", "").startswith("actions/upload-artifact")
    ]
    assert len(upload_steps) == 1
    assert upload_steps[0]["with"]["name"] == "meridian-tray-mac"
    assert upload_steps[0]["with"]["path"] == "meridian-tray-mac.zip"


def test_release_job_depends_on_build_tray_mac() -> None:
    jobs = _load_release_workflow()["jobs"]
    assert "build-tray-mac" in jobs["release"]["needs"]


def test_release_job_ships_the_mac_tray_artifact() -> None:
    jobs = _load_release_workflow()["jobs"]
    files_block = jobs["release"]["steps"][-1]["with"]["files"]
    assert "dist-artifacts/meridian-tray-mac/meridian-tray-mac.zip" in files_block


def test_release_job_still_ships_every_pre_existing_artifact() -> None:
    """Guards against the new artifact line accidentally replacing rather
    than joining the existing files: block (e.g. a bad string edit)."""
    jobs = _load_release_workflow()["jobs"]
    files_block = jobs["release"]["steps"][-1]["with"]["files"]
    for expected in (
        "dist-artifacts/meridian-windows/meridian.exe",
        "dist-artifacts/meridian-tray-windows/meridian-tray.exe",
        "dist-artifacts/meridian-linux/meridian-linux",
        "dist-artifacts/meridian-mac-arm64/meridian-mac-arm64",
    ):
        assert expected in files_block
