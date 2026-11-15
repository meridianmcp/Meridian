"""Tests for the per-user install (meridian_desktop/install.py) and the icon wiring in packaging/build.py.

Every call passes an explicit environment that points at pytest's temp dir, so no test can
reach the real Start Menu, desktop or install folder. The conftest isolates only LOCALAPPDATA
and USERPROFILE, so these tests never rely on it for APPDATA.

The registry is never real here. Each test runs with NO_REGISTRY_ENV set, and the tests that
check registry values pass a FakeRegistry (or a fake winreg module) in explicitly.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from meridian_desktop import __version__
from meridian_desktop import install as inst
from meridian_desktop.settings_store import Settings, SettingsStore, default_settings_path

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_registry_writes(monkeypatch):
    """Switch the registry off for every test in this file. Tests that check it inject a fake."""
    monkeypatch.setenv(inst.NO_REGISTRY_ENV, "1")


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "LOCALAPPDATA": str(tmp_path / "local"),
        "APPDATA": str(tmp_path / "roaming"),
        "USERPROFILE": str(tmp_path / "profile"),
        inst.NO_REGISTRY_ENV: "1",
    }


def _installed(env: dict[str, str]) -> Path:
    """The folder of the version this build installs: install folder\\versions\\<version>."""
    return inst.install_dir(env) / "versions" / __version__


def _bundle(tmp_path: Path, *, marker: str = "v1") -> Path:
    source = tmp_path / "bundle"
    (source / "_internal").mkdir(parents=True)
    (source / inst.EXE_NAME).write_bytes(b"MZ-fake")
    (source / "_internal" / "data.txt").write_text(marker, encoding="utf-8")
    return source


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path]] = []

    def __call__(self, shortcut: Path, target: Path) -> None:
        self.calls.append((shortcut, target))


class FakeRegistry:
    """In-memory stand-in for HKEY_CURRENT_USER: key -> {value name: string}."""

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, str]] = {}
        self.fail_writes = False

    def write_strings(self, key: str, values: dict[str, str]) -> None:
        if self.fail_writes:
            raise PermissionError(13, "Access is denied")
        self.keys.setdefault(key, {}).update(values)

    def read_string(self, key: str, name: str) -> str | None:
        return self.keys.get(key, {}).get(name)

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


class _Handle:
    def __init__(self, key: str) -> None:
        self.key = key

    def __enter__(self) -> _Handle:
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


class FakeWinreg:
    """Just enough of the winreg module to check the calls WindowsRegistry makes."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self) -> None:
        self.values: dict[str, dict[str, object]] = {}
        self.roots: list[object] = []

    def CreateKeyEx(self, root, sub_key, reserved=0, access=0):
        self.roots.append(root)
        self.values.setdefault(sub_key, {})
        return _Handle(sub_key)

    def OpenKey(self, root, sub_key, reserved=0, access=0):
        self.roots.append(root)
        if sub_key not in self.values:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return _Handle(sub_key)

    def SetValueEx(self, handle, name, reserved, kind, value):
        assert kind == self.REG_SZ
        self.values[handle.key][name] = value

    def DeleteValue(self, handle, name):
        if name not in self.values[handle.key]:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        del self.values[handle.key][name]

    def DeleteKey(self, root, sub_key):
        self.roots.append(root)
        if sub_key not in self.values:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        del self.values[sub_key]


def test_paths_come_from_the_given_environment(tmp_path):
    env = _env(tmp_path)
    assert inst.install_dir(env) == tmp_path / "local" / "Programs" / "Meridian Desktop"
    assert inst.start_menu_shortcut(env) == (
        tmp_path / "roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Meridian Desktop.lnk"
    )
    assert inst.desktop_shortcut(env) == tmp_path / "profile" / "Desktop" / "Meridian Desktop.lnk"


def test_paths_fall_back_to_the_home_folder_when_variables_are_unset(tmp_path):
    env = {"USERPROFILE": str(tmp_path)}
    assert inst.install_dir(env) == tmp_path / "AppData" / "Local" / "Programs" / "Meridian Desktop"
    assert inst.start_menu_shortcut(env) == (
        tmp_path / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Meridian Desktop.lnk"
    )


def test_install_copies_the_bundle_and_writes_one_shortcut(tmp_path):
    env = _env(tmp_path)
    recorder = _Recorder()
    result = inst.install(_bundle(tmp_path), env, writer=recorder)

    target = _installed(env)
    assert (target / inst.EXE_NAME).read_bytes() == b"MZ-fake"
    assert (target / "_internal" / "data.txt").read_text(encoding="utf-8") == "v1"
    assert result.install_dir == inst.install_dir(env)
    assert result.shortcuts == (inst.start_menu_shortcut(env),)
    assert recorder.calls == [(inst.start_menu_shortcut(env), target / inst.EXE_NAME)]


def test_desktop_flag_adds_a_second_shortcut(tmp_path):
    env = _env(tmp_path)
    recorder = _Recorder()
    result = inst.install(_bundle(tmp_path), env, desktop=True, writer=recorder)

    assert result.shortcuts == (inst.start_menu_shortcut(env), inst.desktop_shortcut(env))
    assert [call[0] for call in recorder.calls] == [inst.start_menu_shortcut(env), inst.desktop_shortcut(env)]


def test_install_over_an_older_copy_replaces_it(tmp_path):
    env = _env(tmp_path)
    inst.install(_bundle(tmp_path, marker="v1"), env, writer=_Recorder())
    stale = _installed(env) / "old_only.txt"
    stale.write_text("left over", encoding="utf-8")

    inst.install(_bundle(tmp_path / "second", marker="v2"), env, writer=_Recorder())

    assert (_installed(env) / "_internal" / "data.txt").read_text(encoding="utf-8") == "v2"
    assert not stale.exists()


def test_a_reinstall_leaves_no_renamed_aside_copy_behind(tmp_path):
    env = _env(tmp_path)
    inst.install(_bundle(tmp_path, marker="v1"), env, writer=_Recorder())

    inst.install(_bundle(tmp_path / "second", marker="v2"), env, writer=_Recorder())

    assert sorted(p.name for p in (inst.install_dir(env) / "versions").iterdir()) == [__version__]


def _tree(folder: Path) -> dict[str, bytes | None]:
    """Every path under folder with its bytes (None for a folder), so a test can show that nothing changed."""
    paths = sorted(folder.rglob("*"))
    return {str(path.relative_to(folder)): (path.read_bytes() if path.is_file() else None) for path in paths}


def test_a_locked_existing_copy_is_left_whole_when_it_cannot_be_renamed_aside(tmp_path, monkeypatch):
    env = _env(tmp_path)
    inst.install(_bundle(tmp_path, marker="v1"), env, writer=_Recorder())
    installed = _installed(env)
    before = _tree(installed)
    real_rename = inst.os.rename

    def refuse_the_existing_copy(source, destination, *args, **kwargs):
        # A file inside the running copy is in use, so the copy cannot be renamed. Nothing may be deleted instead.
        if Path(source) == installed:
            raise PermissionError(13, "Access is denied")
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(inst.os, "rename", refuse_the_existing_copy)

    with pytest.raises(inst.InstallError, match="could not copy Meridian Desktop into"):
        inst.install(_bundle(tmp_path / "second", marker="v2"), env, writer=_Recorder())

    assert _tree(installed) == before
    assert not installed.with_name(f"{installed.name}.partial").exists()


def test_install_refuses_a_source_without_the_exe(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(inst.InstallError, match="build the bundle first"):
        inst.install(empty, _env(tmp_path), writer=_Recorder())
    assert not inst.install_dir(_env(tmp_path)).exists()


def test_install_from_the_installed_folder_keeps_it(tmp_path):
    env = _env(tmp_path)
    inst.install(_bundle(tmp_path), env, writer=_Recorder())
    installed = _installed(env)

    inst.install(installed, env, writer=_Recorder())

    assert (installed / inst.EXE_NAME).is_file()


def test_install_writes_the_apps_and_features_entry(tmp_path):
    env = _env(tmp_path)
    registry = FakeRegistry()
    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=registry)

    exe = _installed(env) / inst.EXE_NAME
    assert registry.keys[inst.UNINSTALL_KEY] == {
        "DisplayName": "Meridian Desktop",
        "DisplayVersion": __version__,
        "Publisher": "Meridian",
        "DisplayIcon": f"{exe},0",
        "UninstallString": f'"{exe}" --uninstall',
    }


def test_uninstall_removes_the_folder_both_shortcuts_and_the_apps_entry(tmp_path):
    env = _env(tmp_path)
    registry = FakeRegistry()
    inst.install(_bundle(tmp_path), env, desktop=True, writer=_Recorder(), registry=registry)
    # The writer is a stub, so create the shortcut files the real writer would have made.
    inst.start_menu_shortcut(env).parent.mkdir(parents=True, exist_ok=True)
    inst.start_menu_shortcut(env).write_bytes(b"lnk")
    inst.desktop_shortcut(env).parent.mkdir(parents=True, exist_ok=True)
    inst.desktop_shortcut(env).write_bytes(b"lnk")

    lines = inst.uninstall(env, registry=registry)

    assert not inst.install_dir(env).exists()
    assert not [path for path in inst.install_dir(env).parent.iterdir() if ".removing-" in path.name]
    assert not inst.start_menu_shortcut(env).exists()
    assert not inst.desktop_shortcut(env).exists()
    assert inst.UNINSTALL_KEY not in registry.keys
    assert lines == [
        f"removed {inst.start_menu_shortcut(env)}",
        f"removed {inst.desktop_shortcut(env)}",
        f"removed {inst.install_dir(env)}",
        f"removed Apps and Features entry HKCU\\{inst.UNINSTALL_KEY}",
    ]


def test_uninstall_when_nothing_is_installed_removes_nothing(tmp_path):
    assert inst.uninstall(_env(tmp_path), registry=FakeRegistry()) == []


def test_a_folder_in_use_is_left_whole_and_its_apps_entry_stays(tmp_path, monkeypatch):
    env = _env(tmp_path)
    registry = FakeRegistry()
    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=registry)
    folder = inst.install_dir(env)

    def refuse(src, dst, *args, **kwargs):
        raise PermissionError(13, "in use")

    monkeypatch.setattr(inst.os, "rename", refuse)
    with pytest.raises(inst.InstallError, match="close Meridian Desktop first"):
        inst.uninstall(env, registry=registry)
    # Nothing was deleted, and the entry stays so the user can run the uninstall again.
    assert (_installed(env) / inst.EXE_NAME).is_file()
    assert (_installed(env) / "_internal" / "data.txt").is_file()
    assert inst.UNINSTALL_KEY in registry.keys


def test_a_failed_delete_after_the_rename_is_scheduled(tmp_path, monkeypatch):
    env = _env(tmp_path)
    registry = FakeRegistry()
    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=registry)
    real_rmtree = shutil.rmtree

    def fail_on_the_parked_copy(path, *args, **kwargs):
        if ".removing-" in Path(path).name:
            raise PermissionError(13, "in use")
        return real_rmtree(path, *args, **kwargs)

    scheduled: list[Path] = []
    monkeypatch.setattr(inst.shutil, "rmtree", fail_on_the_parked_copy)

    lines = inst.uninstall(env, registry=registry, scheduler=scheduled.append)

    assert len(scheduled) == 1
    assert ".removing-" in scheduled[0].name
    assert scheduled[0].parent == inst.install_dir(env).parent
    assert any(line.startswith("scheduled removal of") for line in lines)
    assert not inst.install_dir(env).exists()
    assert inst.UNINSTALL_KEY not in registry.keys


def test_a_failed_delete_after_the_rename_without_a_scheduler_is_an_error(tmp_path, monkeypatch):
    env = _env(tmp_path)
    registry = FakeRegistry()
    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=registry)

    def refuse(path, *args, **kwargs):
        raise PermissionError(13, "in use")

    monkeypatch.setattr(inst.shutil, "rmtree", refuse)
    with pytest.raises(inst.InstallError, match="could not finish removing"):
        inst.uninstall(env, registry=registry)
    assert inst.UNINSTALL_KEY in registry.keys


def test_running_from_inside_the_folder_detects_the_frozen_exe(tmp_path, monkeypatch):
    folder = tmp_path / "Programs" / "Meridian Desktop"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(folder / inst.EXE_NAME))
    assert inst._running_from(folder) is True
    assert inst._running_from(tmp_path / "elsewhere") is False
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert inst._running_from(folder) is False


def test_running_from_the_install_folder_hands_its_removal_to_the_scheduler(tmp_path, monkeypatch):
    env = _env(tmp_path)
    registry = FakeRegistry()
    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=registry)
    folder = inst.install_dir(env)

    def locked(src, dst, *args, **kwargs):
        raise PermissionError(13, "in use")

    scheduled: list[Path] = []
    monkeypatch.setattr(inst.os, "rename", locked)
    monkeypatch.setattr(inst, "_running_from", lambda path: path == folder)

    lines = inst.uninstall(env, registry=registry, scheduler=scheduled.append)

    assert scheduled == [folder]
    assert any(line.startswith("scheduled removal of") for line in lines)
    assert (_installed(env) / inst.EXE_NAME).is_file()  # this process deletes nothing it cannot finish
    assert inst.UNINSTALL_KEY not in registry.keys


def test_schedule_folder_removal_passes_the_folder_through_the_environment(tmp_path, monkeypatch):
    folder = tmp_path / "Programs" / "x'y z"
    seen: dict = {}

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs["env"]
        return None

    monkeypatch.setattr(inst.subprocess, "Popen", fake_popen)
    inst.schedule_folder_removal(folder)

    assert seen["env"]["MERIDIAN_REMOVE_DIR"] == str(folder)
    assert str(folder) not in " ".join(seen["argv"])


def test_autostart_is_off_by_default(tmp_path):
    registry = FakeRegistry()
    inst.install(_bundle(tmp_path), _env(tmp_path), writer=_Recorder(), registry=registry)
    assert registry.run_value() is None
    assert inst.RUN_KEY not in registry.keys


def test_autostart_saved_as_on_writes_the_run_entry_for_the_installed_exe(tmp_path):
    env = _env(tmp_path)
    SettingsStore(default_settings_path(env)).save(Settings(autostart=True))
    registry = FakeRegistry()

    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=registry)

    exe = _installed(env) / inst.EXE_NAME
    assert registry.run_value() == f'"{exe}"'


def test_reinstall_with_autostart_off_removes_a_stale_run_entry(tmp_path):
    registry = FakeRegistry()
    registry.keys[inst.RUN_KEY] = {inst.RUN_VALUE: '"C:\\old\\MeridianDesktop.exe"'}
    inst.install(_bundle(tmp_path), _env(tmp_path), writer=_Recorder(), registry=registry)
    assert registry.run_value() is None


def test_turning_autostart_on_without_an_install_says_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    with pytest.raises(inst.InstallError, match="install.cmd"):
        inst.set_autostart(True, _env(tmp_path), registry=FakeRegistry())


def test_turning_autostart_on_from_a_frozen_exe_points_at_that_exe(tmp_path, monkeypatch):
    exe = tmp_path / "dist" / inst.EXE_NAME
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    registry = FakeRegistry()

    assert inst.set_autostart(True, _env(tmp_path), registry=registry) == exe.resolve()
    assert registry.run_value() == f'"{exe.resolve()}"'


def test_turning_autostart_off_removes_the_run_entry(tmp_path):
    registry = FakeRegistry()
    registry.keys[inst.RUN_KEY] = {inst.RUN_VALUE: '"x"'}
    assert inst.set_autostart(False, _env(tmp_path), registry=registry) is None
    assert registry.run_value() is None


def test_uninstall_removes_the_run_entry_first_and_the_apps_entry_last(tmp_path):
    env = _env(tmp_path)
    registry = FakeRegistry()
    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=registry)
    inst.set_autostart(True, env, registry=registry, target=_installed(env) / inst.EXE_NAME)

    lines = inst.uninstall(env, registry=registry)

    assert registry.run_value() is None
    assert inst.UNINSTALL_KEY not in registry.keys
    assert lines[0].startswith("removed start-with-Windows entry")
    assert lines[-1].startswith("removed Apps and Features entry")


def test_a_registry_failure_during_install_is_an_install_error(tmp_path):
    registry = FakeRegistry()
    registry.fail_writes = True
    with pytest.raises(inst.InstallError, match="could not update the Windows registry"):
        inst.install(_bundle(tmp_path), _env(tmp_path), writer=_Recorder(), registry=registry)


def test_default_registry_writes_nothing_when_switched_off_or_off_windows():
    assert isinstance(inst.default_registry({inst.NO_REGISTRY_ENV: "1"}), inst.NullRegistry)
    assert isinstance(inst.default_registry({}), inst.NullRegistry) == (os.name != "nt")


def test_windows_registry_uses_hkcu_and_reports_missing_values_as_false():
    fake = FakeWinreg()
    registry = inst.WindowsRegistry(winreg_module=fake)

    registry.write_strings(inst.RUN_KEY, {inst.RUN_VALUE: '"C:\\x\\MeridianDesktop.exe"'})
    assert fake.values[inst.RUN_KEY] == {inst.RUN_VALUE: '"C:\\x\\MeridianDesktop.exe"'}
    assert set(fake.roots) == {FakeWinreg.HKEY_CURRENT_USER}

    assert registry.delete_value(inst.RUN_KEY, inst.RUN_VALUE) is True
    assert registry.delete_value(inst.RUN_KEY, inst.RUN_VALUE) is False
    assert registry.delete_key(inst.UNINSTALL_KEY) is False

    registry.write_strings(inst.UNINSTALL_KEY, {"DisplayName": "Meridian Desktop"})
    assert registry.delete_key(inst.UNINSTALL_KEY) is True
    assert inst.UNINSTALL_KEY not in fake.values


def test_create_shortcut_passes_paths_through_the_environment_not_the_command(tmp_path, monkeypatch):
    shortcut = tmp_path / "Start Menu" / "Meridian Desktop.lnk"
    target = tmp_path / "Programs" / "x'y z" / inst.EXE_NAME
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs["env"]
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        shortcut.write_bytes(b"lnk")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(inst.subprocess, "run", fake_run)
    inst.create_shortcut(shortcut, target)

    assert str(target) not in " ".join(seen["argv"])
    assert seen["env"]["MERIDIAN_TARGET"] == str(target)
    assert seen["env"]["MERIDIAN_LNK"] == str(shortcut)
    assert seen["env"]["MERIDIAN_WORKDIR"] == str(target.parent)


def test_create_shortcut_reports_a_powershell_failure(tmp_path, monkeypatch):
    def failing_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "boom")

    monkeypatch.setattr(inst.subprocess, "run", failing_run)
    with pytest.raises(inst.InstallError, match="could not write the shortcut"):
        inst.create_shortcut(tmp_path / "x.lnk", tmp_path / inst.EXE_NAME)


def test_main_install_succeeds_with_an_injected_environment(tmp_path, capsys):
    env = _env(tmp_path)
    recorder = _Recorder()
    code = inst.main(["--install", "--source", str(_bundle(tmp_path))], env=env, writer=recorder)

    assert code == 0
    assert "installed to" in capsys.readouterr().out
    assert len(recorder.calls) == 1


def test_main_install_says_which_mode_it_wrote(tmp_path, capsys):
    code = inst.main(["--install", "--source", str(_bundle(tmp_path))], env=_env(tmp_path), writer=_Recorder())

    assert code == 0
    assert "(mode: bundle)" in capsys.readouterr().out


def test_install_without_dev_never_uses_the_dev_writer(tmp_path):
    env = _env(tmp_path)
    recorder = _Recorder()

    def refuse(spec):
        raise AssertionError("--install without --dev must not write a dev shortcut")

    code = inst.main(["--install", "--source", str(_bundle(tmp_path))], env=env, writer=recorder, dev_writer=refuse)

    assert code == 0
    assert recorder.calls == [(inst.start_menu_shortcut(env), _installed(env) / inst.EXE_NAME)]


def test_main_install_and_uninstall_use_the_registry_they_are_given(tmp_path, capsys):
    env = _env(tmp_path)
    registry = FakeRegistry()
    scheduled: list[Path] = []

    code = inst.main(["--install", "--source", str(_bundle(tmp_path))], env=env, writer=_Recorder(), registry=registry)
    assert code == 0
    assert inst.UNINSTALL_KEY in registry.keys
    capsys.readouterr()

    code = inst.main(["--uninstall"], env=env, registry=registry, scheduler=scheduled.append)
    assert code == 0
    assert "removed Apps and Features entry" in capsys.readouterr().out
    assert inst.UNINSTALL_KEY not in registry.keys
    assert scheduled == []


def test_main_install_reports_a_missing_bundle_and_returns_1(tmp_path, capsys):
    code = inst.main(["--install", "--source", str(tmp_path / "nowhere")], env=_env(tmp_path), writer=_Recorder())

    assert code == 1
    assert "install failed" in capsys.readouterr().err


def test_errors_reach_a_message_box_when_there_is_no_console(monkeypatch):
    """The windowed bundle has no console: sys.stdout and sys.stderr are None."""
    shown: list[str] = []
    monkeypatch.setattr(inst, "_message_box", shown.append)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    inst._say("installed to somewhere")
    inst._say("install failed: boom", error=True)

    assert shown == ["install failed: boom"]


def test_main_uninstall_with_nothing_installed_returns_0(tmp_path, capsys):
    assert inst.main(["--uninstall"], env=_env(tmp_path), registry=FakeRegistry()) == 0
    assert "nothing to remove" in capsys.readouterr().out


def test_main_requires_exactly_one_action(tmp_path):
    with pytest.raises(SystemExit):
        inst.main([], env=_env(tmp_path))


def test_entry_point_handles_uninstall_without_starting_the_ui(tmp_path):
    """python -m meridian_desktop --uninstall must return before any Qt import or window."""
    env = dict(os.environ, **_env(tmp_path))
    completed = subprocess.run(
        [sys.executable, "-m", "meridian_desktop", "--uninstall"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "nothing to remove" in completed.stdout


def _load_build():
    spec = importlib.util.spec_from_file_location("build_for_install_tests", REPO / "packaging" / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bundle_build_embeds_the_icon():
    build = _load_build()
    cmd = build.pyinstaller_command([])
    assert "--icon" in cmd
    icon_arg = cmd[cmd.index("--icon") + 1]
    assert Path(icon_arg) == build.ICON
    assert build.ICON.is_file(), "packaging/assets/meridian-desktop.ico is missing; run make_icon.ps1"


def test_icon_file_is_a_real_ico():
    header = (REPO / "packaging" / "assets" / "meridian-desktop.ico").read_bytes()[:6]
    # ICONDIR: reserved 0, type 1 (icon), at least one image.
    assert header[:4] == b"\x00\x00\x01\x00"
    assert int.from_bytes(header[4:6], "little") >= 1
