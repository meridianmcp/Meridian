"""Tests for the versioned install layout in meridian_desktop/install.py: the flat-install copy, the version kept for
rollback, pruning, the current pointer, the shortcut writers an update uses, and the undo of each install step.

Every path comes from an explicit environment under pytest's temp dir. The registry is a fake that starts empty and holds
the values a test sets, so no test reaches the real hive.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from meridian_desktop import __version__
from meridian_desktop import install as inst
from meridian_desktop import updater
from meridian_desktop.settings_store import Settings, SettingsStore, default_settings_path


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "LOCALAPPDATA": str(tmp_path / "local"),
        "APPDATA": str(tmp_path / "roaming"),
        "USERPROFILE": str(tmp_path / "profile"),
        inst.NO_REGISTRY_ENV: "1",
    }


def _bundle(tmp_path: Path, *, marker: str = "new") -> Path:
    source = tmp_path / "bundle"
    (source / "_internal").mkdir(parents=True)
    (source / inst.EXE_NAME).write_bytes(b"MZ-" + marker.encode())
    (source / "_internal" / "data.txt").write_text(marker, encoding="utf-8")
    return source


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path]] = []

    def __call__(self, shortcut: Path, target: Path) -> None:
        self.calls.append((shortcut, target))


class FakeRegistry:
    """In-memory stand-in for HKEY_CURRENT_USER. Values are read back as the installer reads them."""

    def __init__(self, values: dict[str, dict[str, str]] | None = None) -> None:
        self.keys: dict[str, dict[str, str]] = {key: dict(vals) for key, vals in (values or {}).items()}

    def write_strings(self, key: str, values: dict[str, str]) -> None:
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


class _FailingRunWrites(FakeRegistry):
    """A registry that refuses to write the start-at-sign-in key, as a locked-down machine would."""

    def write_strings(self, key: str, values: dict[str, str]) -> None:
        if key == inst.RUN_KEY:
            raise PermissionError(13, "Access is denied")
        super().write_strings(key, values)


def _apps(version: str) -> dict[str, dict[str, str]]:
    """The Apps and Features values a flat install from `version` left behind."""
    return {inst.UNINSTALL_KEY: {"DisplayVersion": version}}


def _flat_install(root: Path, marker: str) -> None:
    """A flat install: the exe and _internal sit directly in the install folder, as before versions existed."""
    (root / "_internal").mkdir(parents=True)
    (root / inst.EXE_NAME).write_bytes(b"MZ-" + marker.encode())
    (root / "_internal" / "data.txt").write_text(marker, encoding="utf-8")


def _version_exe(root: Path, version: str) -> Path:
    return root / "versions" / version / inst.EXE_NAME


def test_a_flat_install_moves_into_versions_under_the_version_it_recorded(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install(root, "old")
    recorder = _Recorder()

    inst.install(_bundle(tmp_path), env, writer=recorder, registry=FakeRegistry(_apps("0.1.0")))

    assert _version_exe(root, "0.1.0").read_bytes() == b"MZ-old"
    assert (root / "versions" / "0.1.0" / "_internal" / "data.txt").read_text(encoding="utf-8") == "old"
    assert not (root / inst.EXE_NAME).exists()
    assert not (root / "_internal").exists()
    assert _version_exe(root, __version__).read_bytes() == b"MZ-new"
    assert updater.read_current(root) == __version__
    assert recorder.calls == [(inst.start_menu_shortcut(env), _version_exe(root, __version__))]


def test_the_previous_flat_version_stays_for_rollback(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install(root, "old")

    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=FakeRegistry(_apps("0.1.0")))

    assert updater.exe_for(root, "0.1.0").is_file()
    assert updater.rollback(__version__, root, []) == "0.1.0"


def test_a_flat_install_that_records_no_version_is_kept_as_the_lowest_version(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install(root, "old")

    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=FakeRegistry())

    assert _version_exe(root, inst.UNKNOWN_FLAT_VERSION).read_bytes() == b"MZ-old"


def test_a_flat_install_that_records_a_bad_version_is_kept_as_the_lowest_version(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install(root, "old")

    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=FakeRegistry(_apps("v0.1.0-beta")))

    assert _version_exe(root, inst.UNKNOWN_FLAT_VERSION).is_file()


def test_installing_from_the_flat_folder_itself_copies_it_and_copies_from_the_copy(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install(root, "old")

    inst.install(root, env, writer=_Recorder(), registry=FakeRegistry(_apps("0.1.0")))

    assert _version_exe(root, "0.1.0").read_bytes() == b"MZ-old"
    assert _version_exe(root, __version__).read_bytes() == b"MZ-old"
    assert not (root / inst.EXE_NAME).exists()


def test_a_flat_install_of_the_same_version_is_replaced_by_the_new_bundle(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install(root, "same")

    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=FakeRegistry(_apps(__version__)))

    assert _version_exe(root, __version__).read_bytes() == b"MZ-new"
    assert sorted(p.name for p in (root / "versions").iterdir()) == [__version__]


def test_the_previous_version_is_kept_and_older_ones_and_partial_copies_are_pruned(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    for version in ("0.0.9", "0.1.0"):
        _version_exe(root, version).parent.mkdir(parents=True)
        _version_exe(root, version).write_bytes(b"MZ-" + version.encode())
    (root / "versions" / "0.0.8.partial").mkdir()
    updater.write_current(root, "0.1.0")

    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=FakeRegistry())

    assert sorted(p.name for p in (root / "versions").iterdir()) == ["0.1.0", __version__]


def test_the_shortcut_points_at_the_version_current_json_names_after_an_install(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    recorder = _Recorder()

    inst.install(_bundle(tmp_path), env, writer=recorder, registry=FakeRegistry())

    assert updater.read_current(root) == __version__
    assert all(target == _version_exe(root, __version__) for _shortcut, target in recorder.calls)


def test_autostart_follows_the_version_current_json_names(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _version_exe(root, "0.1.0").parent.mkdir(parents=True)
    _version_exe(root, "0.1.0").write_bytes(b"MZ")
    updater.write_current(root, "0.1.0")

    assert inst.autostart_target(env) == _version_exe(root, "0.1.0")


def test_update_shortcut_writers_cover_only_the_shortcuts_that_exist(tmp_path, monkeypatch):
    env = _env(tmp_path)
    written: list[tuple[Path, Path]] = []
    monkeypatch.setattr(inst, "create_shortcut", lambda shortcut, target: written.append((shortcut, target)))
    assert inst.update_shortcut_writers(env) == []  # nothing installed, so nothing is created

    start = inst.start_menu_shortcut(env)
    start.parent.mkdir(parents=True)
    start.write_bytes(b"lnk")
    writers = inst.update_shortcut_writers(env)
    assert len(writers) == 1
    exe = _version_exe(inst.install_dir(env), "0.2.0")
    writers[0](exe)
    assert written == [(start, exe)]


def test_update_shortcut_writers_include_the_run_entry_only_when_autostart_is_on(tmp_path, monkeypatch):
    env = _env(tmp_path)
    registry = FakeRegistry()
    monkeypatch.setattr(inst, "default_registry", lambda env: registry)
    SettingsStore(default_settings_path(env)).save(Settings(autostart=True))
    exe = _version_exe(inst.install_dir(env), "0.2.0")

    writers = inst.update_shortcut_writers(env)

    assert len(writers) == 1
    writers[0](exe)
    assert registry.keys[inst.RUN_KEY] == {inst.RUN_VALUE: f'"{exe}"'}


def test_uninstall_removes_every_version_folder_and_the_pointer_files(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install(root, "old")
    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=FakeRegistry(_apps("0.1.0")))
    assert (root / updater.CURRENT_NAME).is_file()

    inst.uninstall(env, registry=FakeRegistry(), scheduler=None)

    assert not root.exists()


@pytest.mark.parametrize("flat", [True, False])
def test_the_installed_exe_is_never_left_in_the_install_folder_root(tmp_path, flat):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    if flat:
        _flat_install(root, "old")
    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=FakeRegistry(_apps("0.1.0")))
    assert not (root / inst.EXE_NAME).exists()
    assert _version_exe(root, __version__).is_file()


def test_a_successful_run_leaves_only_the_new_version_and_current_json(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install(root, "old")
    start = inst.start_menu_shortcut(env)
    start.parent.mkdir(parents=True)
    start.write_bytes(b"lnk")

    inst.install(_bundle(tmp_path), env, writer=_Recorder(), registry=FakeRegistry(_apps(__version__)))

    # The flat copy was recorded as this same version, so it is replaced: nothing else is left behind.
    assert sorted(p.name for p in root.iterdir()) == [updater.CURRENT_NAME, updater.VERSIONS_DIR]
    assert sorted(p.name for p in (root / "versions").iterdir()) == [__version__]
    assert _version_exe(root, __version__).read_bytes() == b"MZ-new"
    assert updater.read_current(root) == __version__


# --- a flat install that fails part-way: each step is injected, and everything the run changed is undone ----------


def _state(root: Path) -> dict[str, bytes | None]:
    """Every path under root with its bytes (None for a folder), so a test can show that nothing changed."""
    paths = sorted(root.rglob("*"))
    return {str(path.relative_to(root)): (path.read_bytes() if path.is_file() else None) for path in paths}


def _flat_install_with_three_entries(root: Path) -> None:
    _flat_install(root, "old")
    (root / "b.txt").write_bytes(b"b")
    (root / "c.txt").write_bytes(b"c")


class _LinkWriter:
    """A shortcut writer that writes the target's path into the .lnk file. Call number fail_on_call writes the file
    and then raises, as a PowerShell that dies after its save would."""

    def __init__(self, fail_on_call: int | None = None) -> None:
        self.fail_on_call = fail_on_call
        self.calls = 0

    def __call__(self, shortcut: Path, target: Path) -> None:
        self.calls += 1
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        shortcut.write_bytes(str(target).encode("utf-8"))
        if self.calls == self.fail_on_call:
            raise OSError("the shortcut could not be saved")


def _existing_shortcuts(env: dict[str, str], *, desktop: bool = False) -> dict[Path, bytes]:
    """The shortcuts the flat install already has, with the bytes they hold. A failed run must leave them as these."""
    links = {inst.start_menu_shortcut(env): b"start menu, pointing at the flat exe"}
    if desktop:
        links[inst.desktop_shortcut(env)] = b"desktop, pointing at the flat exe"
    for link, data in links.items():
        link.parent.mkdir(parents=True, exist_ok=True)
        link.write_bytes(data)
    return links


def _assert_shortcuts_unchanged(links: dict[Path, bytes]) -> None:
    for link, data in links.items():
        assert link.read_bytes() == data


def _fail_copy_number(monkeypatch, number: int) -> None:
    """Make the numbered copy fail. File and folder copies are counted together, as one disk error would hit them."""
    calls = {"count": 0}

    def failing_after_real(real):
        def failing(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == number:
                raise OSError("the disk went away")
            return real(*args, **kwargs)

        return failing

    monkeypatch.setattr(inst.shutil, "copy2", failing_after_real(inst.shutil.copy2))
    monkeypatch.setattr(inst.shutil, "copytree", failing_after_real(inst.shutil.copytree))


def _fail_rename(monkeypatch, refused: Callable[[Path, Path], bool]) -> None:
    """Make each rename that refused(source, destination) picks fail, as a file in use would."""
    real_rename = inst.os.rename

    def guarded(source, destination, *args, **kwargs):
        if refused(Path(source), Path(destination)):
            raise PermissionError(13, "Access is denied")
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(inst.os, "rename", guarded)


def test_a_failed_copy_of_the_third_root_entry_leaves_the_flat_install_as_it_was(tmp_path, monkeypatch):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install_with_three_entries(root)
    links = _existing_shortcuts(env)
    before = _state(root)
    _fail_copy_number(monkeypatch, 3)  # the copies, in order: the exe, _internal, b.txt, c.txt
    writer = _LinkWriter()

    with pytest.raises(inst.InstallError) as caught:
        inst.install(_bundle(tmp_path), env, writer=writer, registry=FakeRegistry(_apps("0.1.0")))

    assert str(caught.value) == f"could not copy Meridian Desktop into {root} (the disk went away)"
    assert _state(root) == before  # the flat install is intact, and nothing under versions\ is left for the failed run
    assert not (root / "versions").exists()
    assert writer.calls == 0
    _assert_shortcuts_unchanged(links)


def test_a_failed_publish_of_the_new_version_leaves_the_flat_install_as_it_was(tmp_path, monkeypatch):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install(root, "old")
    links = _existing_shortcuts(env)
    before = _state(root)
    # The old copy's own rename (to 0.1.0) succeeds. The rename of the new bundle into place does not.
    _fail_rename(monkeypatch, lambda src, dst: src.name.endswith(".partial") and dst.name == __version__)
    writer = _LinkWriter()

    with pytest.raises(inst.InstallError) as caught:
        inst.install(_bundle(tmp_path), env, writer=writer, registry=FakeRegistry(_apps("0.1.0")))

    assert str(caught.value) == f"could not copy Meridian Desktop into {root} (Access is denied)"
    assert _state(root) == before
    assert writer.calls == 0
    _assert_shortcuts_unchanged(links)


def test_a_failed_current_json_write_leaves_the_flat_install_as_it_was(tmp_path, monkeypatch):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install(root, "old")
    links = _existing_shortcuts(env)
    before = _state(root)

    def refuse(install_root, version):
        raise OSError("the disk is full")

    monkeypatch.setattr(inst.updater, "write_current", refuse)
    writer = _LinkWriter()

    with pytest.raises(inst.InstallError) as caught:
        inst.install(_bundle(tmp_path), env, writer=writer, registry=FakeRegistry(_apps("0.1.0")))

    assert str(caught.value) == f"could not copy Meridian Desktop into {root} (the disk is full)"
    assert _state(root) == before
    assert writer.calls == 0
    _assert_shortcuts_unchanged(links)


def test_a_failed_shortcut_write_puts_each_shortcut_back_and_removes_one_it_created(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install(root, "old")
    links = _existing_shortcuts(env)  # the Start Menu shortcut exists; the desktop one does not
    before = _state(root)
    registry = FakeRegistry(_apps("0.1.0"))
    registry_before = {key: dict(values) for key, values in registry.keys.items()}
    # The start menu shortcut is written. The desktop one is written, then its save fails.
    writer = _LinkWriter(fail_on_call=2)

    with pytest.raises(inst.InstallError) as caught:
        inst.install(_bundle(tmp_path), env, desktop=True, writer=writer, registry=registry)

    assert str(caught.value) == "could not write the shortcut Meridian Desktop.lnk (the shortcut could not be saved)"
    assert _state(root) == before
    _assert_shortcuts_unchanged(links)  # the start menu shortcut is back on the flat exe
    assert not inst.desktop_shortcut(env).exists()  # it did not exist before this run, so the undo removes it
    assert registry.keys == registry_before


def test_a_failed_start_with_windows_write_puts_the_apps_entry_and_the_shortcuts_back(tmp_path):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install(root, "old")
    SettingsStore(default_settings_path(env)).save(Settings(autostart=True))
    links = _existing_shortcuts(env)
    before = _state(root)
    registry = _FailingRunWrites(_apps("0.1.0"))
    writer = _LinkWriter()

    with pytest.raises(inst.InstallError) as caught:
        inst.install(_bundle(tmp_path), env, writer=writer, registry=registry)

    assert str(caught.value) == "could not update the Windows registry for Meridian Desktop (Access is denied)"
    assert _state(root) == before
    _assert_shortcuts_unchanged(links)
    # The Apps and Features values were written before the Run value failed. The undo puts them back exactly.
    assert registry.keys == _apps("0.1.0")
    assert inst.RUN_KEY not in registry.keys


def test_a_part_of_the_old_copy_that_cannot_be_removed_is_reported(tmp_path, monkeypatch, capsys):
    env = _env(tmp_path)
    root = inst.install_dir(env)
    _flat_install(root, "old")
    real_rmtree = inst.shutil.rmtree

    def refuse_the_parked_internal(path, *args, **kwargs):
        name = Path(path).name
        if name.startswith("_internal.removing-"):
            raise PermissionError(13, "Access is denied")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(inst.shutil, "rmtree", refuse_the_parked_internal)

    code = inst.main(
        ["--install", "--source", str(_bundle(tmp_path))],
        env=env,
        writer=_LinkWriter(),
        registry=FakeRegistry(_apps("0.1.0")),
    )

    out = capsys.readouterr().out
    assert code == 0  # the install succeeded; only the old copy is left
    assert "warning: could not remove the old copy" in out
    assert "_internal.removing-" in out
    assert "(Access is denied)" in out
    assert _version_exe(root, __version__).read_bytes() == b"MZ-new"
    assert updater.read_current(root) == __version__
    assert not (root / inst.EXE_NAME).exists()  # the exe goes last, and it could be removed


def test_the_recorder_writes_the_values_install_writes_for_the_version_it_is_given(tmp_path):
    registry = FakeRegistry()
    exe = tmp_path / "versions" / "0.2.0" / inst.EXE_NAME
    inst.apps_entry_recorder({}, registry=registry)(exe, "0.2.0")
    assert registry.keys[inst.UNINSTALL_KEY] == inst.apps_entry(exe, "0.2.0")
    assert inst.apps_entry(exe)["DisplayVersion"] == __version__  # install() still names the build it installs
