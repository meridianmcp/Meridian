"""Tests for the dev install (``--install --dev``) in meridian_desktop/install.py.

A dev install points the Start Menu shortcut (and the desktop one with --desktop) at the source
tree. Every test injects a fake shortcut writer, a fake interpreter folder and a fake source tree,
all under pytest's temp dir. An autouse guard fails any test that reaches subprocess, so no real
shortcut is written and no PowerShell runs. The registry, the real install folder and the real
profile are never touched.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from meridian_desktop import install as inst
from meridian_desktop.settings_store import Settings, SettingsStore, default_settings_path

FAKE_FILE = b"MZ-fake"


@pytest.fixture(autouse=True)
def no_real_side_effects(monkeypatch):
    """Registry off, and no subprocess. A test that needs a shortcut writer must inject a fake one."""
    monkeypatch.setenv(inst.NO_REGISTRY_ENV, "1")

    def refuse(*args, **kwargs):
        raise AssertionError("a dev-install test reached subprocess; inject a fake shortcut writer")

    monkeypatch.setattr(inst.subprocess, "run", refuse)
    monkeypatch.setattr(inst.subprocess, "Popen", refuse)


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "LOCALAPPDATA": str(tmp_path / "local"),
        "APPDATA": str(tmp_path / "roaming"),
        "USERPROFILE": str(tmp_path / "profile"),
        inst.NO_REGISTRY_ENV: "1",
    }


def _interpreter(tmp_path: Path, *, pythonw: bool = True) -> Path:
    """A fake running python.exe in a Scripts folder, with a pythonw.exe beside it when ``pythonw``."""
    folder = tmp_path / "Python" / "Scripts"
    folder.mkdir(parents=True, exist_ok=True)
    interpreter = folder / "python.exe"
    interpreter.write_bytes(FAKE_FILE)
    if pythonw:
        (folder / "pythonw.exe").write_bytes(FAKE_FILE)
    return interpreter


def _repo(tmp_path: Path) -> Path:
    """A fake source tree: just the package entry point, which is what the dev install checks for."""
    repo = tmp_path / "repository"
    (repo / "meridian_desktop").mkdir(parents=True)
    (repo / "meridian_desktop" / "__main__.py").write_text("", encoding="utf-8")
    return repo


def _bundle(tmp_path: Path) -> Path:
    source = tmp_path / "bundle"
    (source / "_internal").mkdir(parents=True)
    (source / inst.EXE_NAME).write_bytes(FAKE_FILE)
    (source / "_internal" / "data.txt").write_text("v1", encoding="utf-8")
    return source


class _DevRecorder:
    """Stands in for the PowerShell writer. Records each shortcut and leaves a file, as the real writer does."""

    def __init__(self) -> None:
        self.calls: list[inst.DevShortcut] = []

    def __call__(self, spec: inst.DevShortcut) -> None:
        self.calls.append(spec)
        spec.shortcut.parent.mkdir(parents=True, exist_ok=True)
        spec.shortcut.write_bytes(b"lnk")


class FakeRegistry:
    """In-memory HKCU: key -> {value name: string}. Starts empty, so any write shows up."""

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, str]] = {}

    def write_strings(self, key: str, values: dict[str, str]) -> None:
        self.keys.setdefault(key, {}).update(values)

    def delete_value(self, key: str, name: str) -> bool:
        values = self.keys.get(key, {})
        if name not in values:
            return False
        del values[name]
        return True

    def delete_key(self, key: str) -> bool:
        return self.keys.pop(key, None) is not None

    def run_value(self) -> str | None:
        return self.keys.get(inst.RUN_KEY, {}).get(inst.RUN_VALUE)


def test_dev_shortcut_starts_pythonw_with_the_module_from_the_repository(tmp_path):
    env = _env(tmp_path)
    interpreter = _interpreter(tmp_path)
    repo = _repo(tmp_path)
    recorder = _DevRecorder()

    result = inst.install_dev(env, writer=recorder, interpreter=interpreter, repo=repo)

    assert len(recorder.calls) == 1
    spec = recorder.calls[0]
    assert spec.shortcut == inst.start_menu_shortcut(env, dev=True)
    assert spec.target == interpreter.with_name("pythonw.exe")
    assert spec.arguments == "-m meridian_desktop"
    assert spec.working_dir == repo
    assert spec.icon == repo / "packaging" / "assets" / "meridian-desktop.ico"
    assert result == inst.DevInstallResult(
        interpreter=spec.target,
        repo=repo,
        shortcuts=(inst.start_menu_shortcut(env, dev=True),),
    )


def test_dev_shortcut_falls_back_to_python_exe_when_pythonw_is_missing(tmp_path):
    interpreter = _interpreter(tmp_path, pythonw=False)
    recorder = _DevRecorder()

    inst.install_dev(_env(tmp_path), writer=recorder, interpreter=interpreter, repo=_repo(tmp_path))

    assert recorder.calls[0].target == interpreter
    assert recorder.calls[0].target.name == "python.exe"


def test_dev_refuses_when_neither_pythonw_nor_python_is_beside_the_interpreter(tmp_path):
    recorder = _DevRecorder()
    frozen_exe = tmp_path / "Programs" / inst.EXE_NAME

    with pytest.raises(inst.InstallError, match="no pythonw.exe or python.exe"):
        inst.install_dev(_env(tmp_path), writer=recorder, interpreter=frozen_exe, repo=_repo(tmp_path))
    assert recorder.calls == []


def test_dev_refuses_a_folder_that_is_not_the_source_tree(tmp_path):
    recorder = _DevRecorder()
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(inst.InstallError, match="is not a Meridian Desktop source tree"):
        inst.install_dev(_env(tmp_path), writer=recorder, interpreter=_interpreter(tmp_path), repo=empty)
    assert recorder.calls == []


def test_desktop_flag_adds_the_same_dev_shortcut_to_the_desktop(tmp_path):
    env = _env(tmp_path)
    repo = _repo(tmp_path)
    recorder = _DevRecorder()

    result = inst.install_dev(env, desktop=True, writer=recorder, interpreter=_interpreter(tmp_path), repo=repo)

    assert [call.shortcut for call in recorder.calls] == [inst.start_menu_shortcut(env, dev=True), inst.desktop_shortcut(env, dev=True)]
    assert result.shortcuts == (inst.start_menu_shortcut(env, dev=True), inst.desktop_shortcut(env, dev=True))
    assert {call.target for call in recorder.calls} == {result.interpreter}
    assert {call.arguments for call in recorder.calls} == {"-m meridian_desktop"}
    assert {call.working_dir for call in recorder.calls} == {repo}
    assert {call.icon for call in recorder.calls} == {repo / "packaging" / "assets" / "meridian-desktop.ico"}


def test_dev_defaults_to_the_running_interpreter_and_this_checkout(tmp_path, monkeypatch):
    interpreter = _interpreter(tmp_path)
    monkeypatch.setattr(sys, "executable", str(interpreter))
    recorder = _DevRecorder()

    inst.install_dev(_env(tmp_path), writer=recorder)

    spec = recorder.calls[0]
    assert spec.target == interpreter.with_name("pythonw.exe")
    assert spec.working_dir == inst.REPO
    assert spec.icon == inst.REPO / inst.ICON_RELATIVE
    assert spec.icon.is_file()


def test_dev_install_copies_nothing_and_leaves_the_registry_alone(tmp_path):
    env = _env(tmp_path)
    registry = FakeRegistry()
    SettingsStore(default_settings_path(env)).save(Settings(autostart=True))

    def refuse(spec):
        raise AssertionError("--install --dev must not use the frozen writer")

    code = inst.main(
        ["--install", "--dev"],
        env=env,
        writer=refuse,
        dev_writer=_DevRecorder(),
        registry=registry,
        interpreter=_interpreter(tmp_path),
        repo=_repo(tmp_path),
    )

    assert code == 0
    assert not inst.install_dir(env).exists()
    # No Apps and Features entry and no start-at-sign-in entry, even with autostart saved on.
    assert registry.keys == {}


def test_dev_install_leaves_an_installed_bundle_and_its_entries_alone(tmp_path):
    env = _env(tmp_path)
    registry = FakeRegistry()
    SettingsStore(default_settings_path(env)).save(Settings(autostart=True))
    inst.install(_bundle(tmp_path), env, writer=lambda shortcut, target: None, registry=registry)
    installed = inst.install_dir(env) / "versions" / inst.__version__
    apps_before = dict(registry.keys[inst.UNINSTALL_KEY])
    run_before = registry.run_value()
    assert run_before is not None
    recorder = _DevRecorder()

    code = inst.main(
        ["--install", "--dev", "--desktop"],
        env=env,
        dev_writer=recorder,
        registry=registry,
        interpreter=_interpreter(tmp_path),
        repo=_repo(tmp_path),
    )

    assert code == 0
    assert (installed / inst.EXE_NAME).read_bytes() == FAKE_FILE
    assert (installed / "_internal" / "data.txt").read_text(encoding="utf-8") == "v1"
    assert registry.keys[inst.UNINSTALL_KEY] == apps_before
    assert registry.run_value() == run_before
    assert len(recorder.calls) == 2


def test_main_install_dev_reports_the_mode_and_each_shortcut(tmp_path, capsys):
    env = _env(tmp_path)
    interpreter = _interpreter(tmp_path)
    repo = _repo(tmp_path)

    def refuse(spec):
        raise AssertionError("--install --dev must not use the frozen writer")

    code = inst.main(
        ["--install", "--dev", "--desktop"],
        env=env,
        writer=refuse,
        dev_writer=_DevRecorder(),
        interpreter=interpreter,
        repo=repo,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "(mode: dev)" in out
    assert f"{interpreter.with_name('pythonw.exe')} -m meridian_desktop from {repo}" in out
    assert f"shortcut: {inst.start_menu_shortcut(env, dev=True)}" in out
    assert f"shortcut: {inst.desktop_shortcut(env, dev=True)}" in out
    assert "installed to" not in out


def test_main_install_dev_reports_a_missing_interpreter_and_returns_1(tmp_path, capsys):
    code = inst.main(
        ["--install", "--dev"],
        env=_env(tmp_path),
        dev_writer=_DevRecorder(),
        interpreter=tmp_path / "Programs" / inst.EXE_NAME,
        repo=_repo(tmp_path),
    )

    assert code == 1
    assert "install failed" in capsys.readouterr().err


def test_uninstall_removes_the_dev_shortcuts_and_never_the_source_tree(tmp_path):
    env = _env(tmp_path)
    repo = _repo(tmp_path)
    inst.install_dev(env, desktop=True, writer=_DevRecorder(), interpreter=_interpreter(tmp_path), repo=repo)
    assert inst.start_menu_shortcut(env, dev=True).is_file()
    assert inst.desktop_shortcut(env, dev=True).is_file()

    lines = inst.uninstall(env, registry=FakeRegistry())

    assert not inst.start_menu_shortcut(env, dev=True).exists()
    assert not inst.desktop_shortcut(env, dev=True).exists()
    assert (repo / "meridian_desktop" / "__main__.py").is_file()
    assert lines == [f"removed {inst.start_menu_shortcut(env, dev=True)}", f"removed {inst.desktop_shortcut(env, dev=True)}"]


def test_dev_shortcut_script_sets_arguments_folder_and_icon_from_the_environment():
    script = inst._DEV_POWERSHELL_SCRIPT
    assert "$s.TargetPath = $env:MERIDIAN_TARGET;" in script
    assert "$s.Arguments = $env:MERIDIAN_ARGS;" in script
    assert "$s.WorkingDirectory = $env:MERIDIAN_WORKDIR;" in script
    assert "$s.IconLocation = $env:MERIDIAN_ICON + ',0';" in script


def test_create_dev_shortcut_passes_everything_through_the_environment(tmp_path, monkeypatch):
    shortcut = tmp_path / "Start Menu" / "Meridian Desktop.lnk"
    target = tmp_path / "Python x'y" / "pythonw.exe"
    repo = tmp_path / "repo with spaces"
    icon = repo / "packaging" / "assets" / "meridian-desktop.ico"
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs["env"]
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        shortcut.write_bytes(b"lnk")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(inst.subprocess, "run", fake_run)
    inst.create_dev_shortcut(
        inst.DevShortcut(
            shortcut=shortcut,
            target=target,
            arguments="-m meridian_desktop",
            working_dir=repo,
            icon=icon,
        )
    )

    passed = seen["env"]
    assert passed["MERIDIAN_LNK"] == str(shortcut)
    assert passed["MERIDIAN_TARGET"] == str(target)
    assert passed["MERIDIAN_ARGS"] == "-m meridian_desktop"
    assert passed["MERIDIAN_WORKDIR"] == str(repo)
    assert passed["MERIDIAN_ICON"] == str(icon)
    command = " ".join(seen["argv"])
    assert str(target) not in command
    assert str(repo) not in command
    assert str(icon) not in command


def test_create_dev_shortcut_reports_a_powershell_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(inst.subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", "boom"))
    spec = inst.DevShortcut(
        shortcut=tmp_path / "x.lnk",
        target=tmp_path / "pythonw.exe",
        arguments="-m meridian_desktop",
        working_dir=tmp_path,
        icon=tmp_path / "x.ico",
    )

    with pytest.raises(inst.InstallError, match="could not write the shortcut"):
        inst.create_dev_shortcut(spec)


def test_dev_is_refused_without_install_so_uninstall_never_ignores_it(tmp_path):
    with pytest.raises(SystemExit) as refused:
        inst.main(["--uninstall", "--dev"], env=_env(tmp_path), registry=FakeRegistry())
    assert refused.value.code == 2


def test_dev_is_refused_with_a_bundle_source_and_writes_nothing(tmp_path):
    env = _env(tmp_path)
    recorder = _DevRecorder()

    with pytest.raises(SystemExit) as refused:
        inst.main(
            ["--install", "--dev", "--source", str(_bundle(tmp_path))],
            env=env,
            dev_writer=recorder,
            interpreter=_interpreter(tmp_path),
            repo=_repo(tmp_path),
        )

    assert refused.value.code == 2
    assert recorder.calls == []
    assert not inst.install_dir(env).exists()
