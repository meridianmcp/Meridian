"""Tests for the Meridian core locator (meridian_desktop/core_locator.py) and the Qt side that runs it (RunnerActions).

Every folder is a fake tree under pytest's temp directory. The Python programs are empty files, and the probe is a
fake: nothing here starts a process, opens a socket or reads the real Documents folder. The Qt tests run the search
on the thread pool through RunnerActions, with a fake search, so a test shows the search never runs on the UI thread.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from meridian_desktop import core_locator
from meridian_desktop.core_locator import (
    FOUND,
    NOT_FOUND,
    PLAIN_NO_CHECKOUT_TEXT,
    PROBE_PROGRAM,
    SEARCH_BUDGET_SECONDS,
    SOURCE_SAVED,
    SOURCE_SEARCHED,
    LocatedCore,
    candidate_checkouts,
    documents_folders,
    find_python,
    locate_core,
    probe_python,
    remember_core,
    workspace_name,
)
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui.runner_actions import (
    CHOOSE_CANCELLED_TEXT,
    CHOSEN_TEXT,
    LOCATE_FAILED_TEXT,
    RunnerActions,
)


# ----------------------------------------------------------------------------- fake folders and probes


def make_checkout(root: Path, *, pixi: bool = True, package: bool = True, pyproject: bool = False) -> Path:
    """A fake Meridian checkout: the meridian package and a pixi.toml (or pyproject.toml) that names the workspace."""
    root.mkdir(parents=True, exist_ok=True)
    if pixi:
        (root / "pixi.toml").write_text('[workspace]\nname = "meridian"\nchannels = ["conda-forge"]\n', encoding="utf-8")
    if pyproject:
        (root / "pyproject.toml").write_text('[project]\nname = "meridian"\n', encoding="utf-8")
    if package:
        (root / "meridian").mkdir(exist_ok=True)
        (root / "meridian" / "__init__.py").write_text('__version__ = "0.1.6"\n', encoding="utf-8")
    return root


def make_env(workspace_envs: Path, name: str, *, mtime: float | None = None) -> Path:
    """A fake pixi environment named ``name`` under workspace-envs. Returns its python.exe."""
    env = workspace_envs / name / "envs" / "default"
    env.mkdir(parents=True, exist_ok=True)
    python = env / "python.exe"
    python.write_bytes(b"MZ")
    if mtime is not None:
        os.utime(workspace_envs / name, (mtime, mtime))
    return python


def make_python(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ")
    return path


class Probe:
    """A fake probe. A Python passes when it is in ``good``. Each call is recorded, in order."""

    def __init__(self, good=()):
        self.good = {str(path) for path in good}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, python: str, checkout: str) -> bool:
        self.calls.append((python, checkout))
        return python in self.good

    @property
    def pythons(self) -> list[str]:
        return [python for python, _checkout in self.calls]


def _store(settings_path: Path, **values) -> SettingsStore:
    store = SettingsStore(settings_path)
    if values:
        store.save(Settings(**values))
    return store


@pytest.fixture
def home(tmp_path) -> Path:
    """The fake user home. It holds a Documents folder and a .pixi folder, and nothing else of note."""
    folder = tmp_path / "home"
    (folder / "Documents").mkdir(parents=True)
    (folder / ".pixi" / "workspace-envs").mkdir(parents=True)
    return folder


@pytest.fixture
def repo_root(home) -> Path:
    """The desktop repo, inside the fake Documents folder, as it is on the real machine."""
    return home / "Documents" / "meridian-desktop"


def _locate(store, home, repo_root, probe, clock=time.monotonic) -> LocatedCore:
    return locate_core(store, repo_root=repo_root, home=home, probe=probe, clock=clock)


# ----------------------------------------------------------------------------- the search, rule by rule


def test_a_saved_pair_that_works_is_kept_and_no_folder_is_searched(home, repo_root, settings_path):
    saved = make_checkout(home / "saved" / "repository")
    saved_python = make_python(home / "saved" / "python.exe")
    make_checkout(home / "Documents" / "Meridian" / "repository")  # a candidate that must not be used
    store = _store(settings_path, core_checkout=str(saved), core_python=str(saved_python))
    probe = Probe(good=[saved_python])

    located = _locate(store, home, repo_root, probe)

    assert located.found and located.source == SOURCE_SAVED
    assert (located.checkout, located.python) == (str(saved), str(saved_python))
    assert probe.calls == [(str(saved_python), str(saved))]


def test_the_candidate_under_documents_meridian_repository_is_found_with_its_pixi_environment(home, repo_root, settings_path):
    checkout = make_checkout(home / "Documents" / "Meridian" / "repository")
    workspace_envs = home / ".pixi" / "workspace-envs"
    python = make_env(workspace_envs, "meridian-111")
    (checkout / ".pixi" / "envs").mkdir(parents=True)
    (checkout / ".pixi" / "envs" / "README.txt").write_text(
        f"Environments are stored in a custom detached-environments directory: {workspace_envs}\\meridian-111\\envs.\n",
        encoding="utf-8",
    )
    store = _store(settings_path)

    located = _locate(store, home, repo_root, Probe(good=[python]))

    assert located.found and located.source == SOURCE_SEARCHED
    assert located.checkout == str(checkout)
    assert located.python == str(python)


def test_the_parent_documents_folder_of_the_desktop_repo_is_searched(tmp_path, settings_path):
    # The desktop repo is in a Documents folder that is not the user's own, so the user's Documents folder is empty.
    documents = tmp_path / "elsewhere" / "Documents"
    checkout = make_checkout(documents / "Meridian" / "repository")
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    python = make_python(tmp_path / "env" / "python.exe")
    store = _store(settings_path)

    located = _locate(store, home, documents / "meridian-desktop", Probe(good=[python]))

    # No pixi environment is named or found here, so the checkout's own in-repo env is the only Python candidate.
    assert not located.found
    in_repo = make_python(checkout / ".pixi" / "envs" / "default" / "python.exe")
    located = _locate(store, home, documents / "meridian-desktop", Probe(good=[in_repo]))
    assert located.found and located.checkout == str(checkout) and located.python == str(in_repo)


def test_a_worktree_under_the_desktop_repo_reaches_the_same_documents_folder(home, settings_path):
    checkout = make_checkout(home / "Documents" / "Meridian" / "repository")
    python = make_python(checkout / ".pixi" / "envs" / "default" / "python.exe")
    worktree = home / "Documents" / "meridian-desktop" / ".claude" / "worktrees" / "desk-ux-core-auto"
    store = _store(settings_path)

    located = _locate(store, home, worktree, Probe(good=[python]))

    assert located.found and located.checkout == str(checkout)


def test_the_meridian_folder_itself_is_a_candidate_when_it_holds_the_package(home, repo_root, settings_path):
    folder = make_checkout(home / "Documents" / "Meridian")
    python = make_python(folder / ".pixi" / "envs" / "default" / "python.exe")
    store = _store(settings_path)

    located = _locate(store, home, repo_root, Probe(good=[python]))

    assert located.found and located.checkout == str(folder)


def test_a_folder_without_the_package_or_the_project_file_is_not_a_candidate(home, repo_root, settings_path):
    make_checkout(home / "Documents" / "Meridian" / "repository", package=False)
    other = home / "Documents" / "Meridian" / "other"
    other.mkdir(parents=True)
    (other / "meridian").mkdir()
    (other / "meridian" / "__init__.py").write_text("", encoding="utf-8")  # no pixi.toml, no pyproject.toml
    probe = Probe()
    store = _store(settings_path)

    located = _locate(store, home, repo_root, probe)

    assert located.status == NOT_FOUND
    assert located.reason == PLAIN_NO_CHECKOUT_TEXT
    assert probe.calls == []  # nothing is probed when no folder qualifies


def test_a_pyproject_toml_is_enough_to_make_a_candidate(home, repo_root, settings_path):
    checkout = make_checkout(home / "Documents" / "Meridian" / "repository", pixi=False, pyproject=True)
    python = make_python(checkout / ".pixi" / "envs" / "default" / "python.exe")
    store = _store(settings_path)

    located = _locate(store, home, repo_root, Probe(good=[python]))

    assert located.found and located.checkout == str(checkout)


def test_the_environment_the_checkout_names_is_tried_before_the_others(home, repo_root, settings_path):
    checkout = make_checkout(home / "Documents" / "Meridian" / "repository")
    workspace_envs = home / ".pixi" / "workspace-envs"
    newer = make_env(workspace_envs, "meridian-222", mtime=2_000_000_000)
    named = make_env(workspace_envs, "meridian-111", mtime=1_000_000_000)
    (checkout / ".pixi" / "envs").mkdir(parents=True)
    (checkout / ".pixi" / "envs" / "README.txt").write_text(
        f"detached-environments directory: {workspace_envs}\\meridian-111\\envs.\n", encoding="utf-8"
    )
    probe = Probe(good=[named, newer])
    store = _store(settings_path)

    located = _locate(store, home, repo_root, probe)

    assert located.python == str(named)
    assert probe.pythons[0] == str(named)


def test_the_newest_named_environments_are_tried_first_and_only_a_few_of_them(home, repo_root, settings_path):
    checkout = make_checkout(home / "Documents" / "Meridian" / "repository")
    workspace_envs = home / ".pixi" / "workspace-envs"
    pythons = [make_env(workspace_envs, f"meridian-{100 + n}", mtime=1_000_000_000 + n) for n in range(8)]
    make_env(workspace_envs, "meridian-desktop-999", mtime=2_000_000_000)  # another workspace, never tried
    store = _store(settings_path, core_checkout=str(checkout))
    probe = Probe()

    located = _locate(store, home, repo_root, probe)

    assert not located.found
    expected = [str(path) for path in reversed(pythons)][: core_locator.MAX_ENVIRONMENTS]
    assert probe.pythons == expected


def test_a_python_that_fails_the_probe_is_refused_and_nothing_is_saved(home, repo_root, settings_path):
    checkout = make_checkout(home / "Documents" / "Meridian" / "repository")
    make_python(checkout / ".pixi" / "envs" / "default" / "python.exe")
    store = _store(settings_path)
    before = store.path.exists()

    located = _locate(store, home, repo_root, Probe(good=[]))

    assert located.status == NOT_FOUND
    assert "No Python that imports meridian" in located.reason
    assert remember_core(store, located) is False
    assert store.path.exists() == before  # nothing was written


def test_a_saved_checkout_with_no_working_python_is_reported_and_its_python_is_searched(home, repo_root, settings_path):
    checkout = make_checkout(home / "saved" / "repository")
    python = make_python(checkout / ".pixi" / "envs" / "default" / "python.exe")
    store = _store(settings_path, core_checkout=str(checkout))

    located = _locate(store, home, repo_root, Probe(good=[python]))

    assert located.found and located.source == SOURCE_SEARCHED
    assert (located.checkout, located.python) == (str(checkout), str(python))


def test_a_python_saved_by_hand_that_fails_is_reported_and_never_replaced(home, repo_root, settings_path):
    checkout = make_checkout(home / "saved" / "repository")
    by_hand = make_python(home / "by-hand" / "python.exe")
    other = make_python(checkout / ".pixi" / "envs" / "default" / "python.exe")
    store = _store(settings_path, core_checkout=str(checkout), core_python=str(by_hand))
    before = store.path.read_text(encoding="utf-8")

    located = _locate(store, home, repo_root, Probe(good=[other]))

    assert located.status == NOT_FOUND
    assert "does not import meridian" in located.reason
    assert remember_core(store, located) is False
    assert store.path.read_text(encoding="utf-8") == before


def test_a_manual_checkout_that_is_gone_is_reported_and_never_replaced(home, repo_root, settings_path):
    gone = home / "moved-away" / "repository"
    make_checkout(home / "Documents" / "Meridian" / "repository")  # a valid candidate that must not replace the choice
    store = _store(settings_path, core_checkout=str(gone))
    before = store.path.read_text(encoding="utf-8")
    probe = Probe()

    located = _locate(store, home, repo_root, probe)

    assert located.status == NOT_FOUND
    assert str(gone) in located.reason
    assert probe.calls == []
    assert remember_core(store, located) is False
    assert store.path.read_text(encoding="utf-8") == before


def test_a_manual_checkout_with_no_python_gets_its_python_searched_and_keeps_the_checkout(home, repo_root, settings_path):
    checkout = make_checkout(home / "chosen" / "repository")
    python = make_python(checkout / ".pixi" / "envs" / "default" / "python.exe")
    store = _store(settings_path, core_checkout=str(checkout))  # chosen by hand, so its flag is off

    located = _locate(store, home, repo_root, Probe(good=[python]))
    assert remember_core(store, located) is True

    saved = store.load().settings
    assert saved.core_checkout == str(checkout)
    assert saved.core_checkout_auto is False  # the owner's checkout keeps its flag
    assert saved.core_python == str(python)
    assert saved.core_python_auto is True


def test_a_found_pair_is_saved_as_automatic_and_the_next_search_reads_it_as_saved(home, repo_root, settings_path):
    checkout = make_checkout(home / "Documents" / "Meridian" / "repository")
    python = make_python(checkout / ".pixi" / "envs" / "default" / "python.exe")
    store = _store(settings_path)

    located = _locate(store, home, repo_root, Probe(good=[python]))
    assert remember_core(store, located) is True
    saved = store.load().settings
    assert (saved.core_checkout, saved.core_python) == (str(checkout), str(python))
    assert saved.core_checkout_auto is True and saved.core_python_auto is True

    again = _locate(store, home, repo_root, Probe(good=[python]))
    assert again.found and again.source == SOURCE_SAVED


def test_an_automatic_path_that_went_away_is_replaced_by_the_next_search(home, repo_root, settings_path):
    checkout = make_checkout(home / "Documents" / "Meridian" / "repository")
    python = make_python(checkout / ".pixi" / "envs" / "default" / "python.exe")
    store = _store(
        settings_path,
        core_checkout=str(home / "old" / "repository"),
        core_python=str(home / "old" / "python.exe"),
        core_checkout_auto=True,
        core_python_auto=True,
    )

    located = _locate(store, home, repo_root, Probe(good=[python]))
    assert remember_core(store, located) is True

    saved = store.load().settings
    assert (saved.core_checkout, saved.core_python) == (str(checkout), str(python))


def test_a_stale_automatic_path_is_cleared_when_nothing_is_found_so_the_controls_turn_off(home, repo_root, settings_path):
    store = _store(
        settings_path,
        core_checkout=str(home / "old" / "repository"),
        core_python=str(home / "old" / "python.exe"),
        core_checkout_auto=True,
        core_python_auto=True,
    )

    located = _locate(store, home, repo_root, Probe())
    assert located.status == NOT_FOUND
    assert remember_core(store, located) is True

    saved = store.load().settings
    assert saved.core_checkout == "" and saved.core_python == ""
    assert saved.core_checkout_auto is False and saved.core_python_auto is False


def test_a_found_pair_never_overwrites_a_checkout_the_owner_set_while_the_search_ran(home, repo_root, settings_path):
    found = make_checkout(home / "Documents" / "Meridian" / "repository")
    python = make_python(found / ".pixi" / "envs" / "default" / "python.exe")
    located = _locate(_store(settings_path), home, repo_root, Probe(good=[python]))
    chosen = make_checkout(home / "chosen" / "repository")
    store = _store(settings_path, core_checkout=str(chosen))  # the owner chose this after the search began

    assert remember_core(store, located) is True

    saved = store.load().settings
    assert saved.core_checkout == str(chosen)  # the owner's choice stays
    assert saved.core_checkout_auto is False
    assert saved.core_python == str(python)  # the empty field is filled


def test_remember_writes_nothing_for_a_result_that_was_not_found_and_has_no_stale_path(home, repo_root, settings_path):
    store = _store(settings_path)
    located = _locate(store, home, repo_root, Probe())

    assert remember_core(store, located) is False


def test_locating_never_writes_settings(home, repo_root, settings_path):
    checkout = make_checkout(home / "Documents" / "Meridian" / "repository")
    python = make_python(checkout / ".pixi" / "envs" / "default" / "python.exe")
    store = _store(settings_path)

    located = _locate(store, home, repo_root, Probe(good=[python]))

    assert located.found
    assert not store.path.exists()


def test_choosing_a_path_by_hand_marks_it_as_the_owners_so_the_search_keeps_off_it(home, settings_path):
    checkout = make_checkout(home / "chosen" / "repository")
    store = _store(settings_path, core_checkout=str(checkout), core_checkout_auto=True)

    saved = store.set_core_path("core_checkout", str(checkout))

    assert saved.core_checkout_auto is False
    assert store.load().settings.core_checkout_auto is False


def test_the_auto_flags_are_written_only_when_they_are_true(settings_path):
    store = SettingsStore(settings_path)
    store.save(Settings(core_checkout="C:/x", core_python="C:/x/python.exe", core_checkout_auto=True))
    written = json.loads(settings_path.read_text(encoding="utf-8"))
    assert written["core_checkout_auto"] is True
    assert "core_python_auto" not in written

    store.save(Settings(core_checkout="C:/x", core_python="C:/x/python.exe"))
    written = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "core_checkout_auto" not in written and "core_python_auto" not in written


def test_a_settings_file_that_did_not_load_cleanly_is_copied_aside_before_a_found_pair_is_written(
    home, repo_root, settings_path
):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("{ not json", encoding="utf-8")
    checkout = make_checkout(home / "Documents" / "Meridian" / "repository")
    python = make_python(checkout / ".pixi" / "envs" / "default" / "python.exe")
    store = SettingsStore(settings_path)

    located = _locate(store, home, repo_root, Probe(good=[python]))
    assert remember_core(store, located) is True

    backup = settings_path.with_name(settings_path.name + ".unreadable.bak")
    assert backup.read_text(encoding="utf-8") == "{ not json"
    assert store.load().settings.core_checkout == str(checkout)


def test_the_search_stops_starting_probes_when_its_budget_is_spent(tmp_path, monkeypatch):
    # The clock reads the start, then a time past the budget before the first probe can begin.
    ticks = iter([0.0, SEARCH_BUDGET_SECONDS + 1.0])
    candidates = [str(make_python(tmp_path / f"env-{n}" / "python.exe")) for n in range(3)]
    monkeypatch.setattr(core_locator, "python_candidates", lambda _checkout, _roots: candidates)
    probe = Probe(good=candidates)

    result = find_python(tmp_path / "checkout", pixi_roots=[], probe=probe, clock=lambda: next(ticks))

    assert result == ""
    assert probe.calls == []  # the budget was spent before the first probe


def test_documents_folders_use_the_nearest_documents_ancestor_and_the_users_own(tmp_path):
    repo = tmp_path / "Documents" / "meridian-desktop" / ".claude" / "worktrees" / "x"
    home = tmp_path / "home"
    folders = documents_folders(repo, home)
    assert folders == [tmp_path / "Documents", home / "Documents"]


def test_a_repo_outside_any_documents_folder_searches_only_the_users_documents(tmp_path):
    folders = documents_folders(tmp_path / "apps" / "meridian-desktop", tmp_path / "home")
    assert folders == [tmp_path / "home" / "Documents"]


def test_candidate_checkouts_try_the_repository_folder_before_the_meridian_folder(tmp_path):
    documents = tmp_path / "Documents"
    make_checkout(documents / "Meridian" / "repository")
    make_checkout(documents / "Meridian")
    assert candidate_checkouts([documents]) == [documents / "Meridian" / "repository", documents / "Meridian"]


def test_workspace_name_is_read_from_the_workspace_table_only(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pixi.toml").write_text(
        '[project]\nname = "not-this"\n\n[workspace]\nname = "meridian"\n\n[dependencies]\nname = "x"\n',
        encoding="utf-8",
    )
    assert workspace_name(checkout) == "meridian"


# ----------------------------------------------------------------------------- the probe


class _Run:
    """A fake for subprocess.run. It records the call and returns or raises a canned reply."""

    def __init__(self, reply):
        self.reply = reply
        self.calls: list[tuple[list, dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


def _completed(code, stdout: bytes):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=b"")


def test_the_probe_accepts_only_the_checkouts_own_package(tmp_path):
    checkout = make_checkout(tmp_path / "checkout")
    python = make_python(tmp_path / "python.exe")
    own = str(checkout / "meridian" / "__init__.py").encode()
    run = _Run(_completed(0, own + b"\nTrue\n"))

    assert probe_python(str(python), str(checkout), run=run) is True
    argv, kwargs = run.calls[0]
    assert argv == [str(python), "-c", PROBE_PROGRAM]
    assert kwargs["shell"] is False
    assert kwargs["cwd"] == str(checkout)
    assert kwargs["timeout"] == core_locator.PROBE_TIMEOUT_SECONDS


def test_the_probe_refuses_a_python_that_imports_another_checkout(tmp_path):
    checkout = make_checkout(tmp_path / "checkout")
    other = make_checkout(tmp_path / "other")
    python = make_python(tmp_path / "python.exe")
    run = _Run(_completed(0, str(other / "meridian" / "__init__.py").encode() + b"\nTrue\n"))

    assert probe_python(str(python), str(checkout), run=run) is False


@pytest.mark.parametrize(
    "reply",
    [
        _completed(1, b""),
        _completed(0, b"only one line\n"),
        _completed(0, b"<path>\nFalse\n"),
        subprocess.TimeoutExpired(cmd="python", timeout=10),
        OSError("no such program"),
    ],
)
def test_the_probe_refuses_failures_without_raising(tmp_path, reply):
    checkout = make_checkout(tmp_path / "checkout")
    python = make_python(tmp_path / "python.exe")
    run = _Run(reply)

    assert probe_python(str(python), str(checkout), run=run) is False


def test_the_probe_never_runs_a_program_that_is_not_a_file(tmp_path):
    checkout = make_checkout(tmp_path / "checkout")
    run = _Run(_completed(0, b""))

    assert probe_python(str(tmp_path / "missing.exe"), str(checkout), run=run) is False
    assert run.calls == []


# ----------------------------------------------------------------------------- the Qt side, through RunnerActions


def _settings_store(settings_path: Path) -> SettingsStore:
    return SettingsStore(settings_path)


def test_the_boot_search_runs_off_the_ui_thread_and_reports_back(qtbot, settings_path):
    ran_on: list[threading.Thread] = []
    checkout = Path("C:/fake/Meridian/repository")

    def fake_search(store):
        ran_on.append(threading.current_thread())
        return LocatedCore(FOUND, str(checkout), "C:/fake/python.exe", SOURCE_SEARCHED)

    store = _settings_store(settings_path)
    actions = RunnerActions(store, locate=fake_search)
    changes: list[None] = []
    actions.coreChanged.connect(lambda: changes.append(None))

    actions.find_core()
    qtbot.waitUntil(lambda: actions.located is not None, timeout=5000)

    assert ran_on and ran_on[0] is not threading.main_thread()
    assert len(changes) == 2  # once when the search starts, once when it finishes
    assert actions.located.found
    assert store.load().settings.core_checkout == str(checkout)
    assert actions.core().core_ready is True


def test_a_search_that_raises_is_reported_by_class_name_only(qtbot, settings_path):
    def failing(store):
        raise RuntimeError("C:/secret/path/that/must/not/show")

    actions = RunnerActions(_settings_store(settings_path), locate=failing)
    actions.find_core()
    qtbot.waitUntil(lambda: actions.located is not None, timeout=5000)

    assert actions.located.status == NOT_FOUND
    assert actions.located.reason == LOCATE_FAILED_TEXT.format("RuntimeError")
    assert "secret" not in actions.located.reason


def test_a_second_find_while_one_is_running_is_not_doubled(qtbot, settings_path):
    gate = threading.Event()
    calls: list[int] = []

    def slow(store):
        calls.append(1)
        gate.wait(5)
        return LocatedCore(NOT_FOUND, reason="none")

    actions = RunnerActions(_settings_store(settings_path), locate=slow)
    actions.find_core()
    actions.find_core()
    assert actions.locating is True
    gate.set()
    qtbot.waitUntil(lambda: actions.located is not None, timeout=5000)

    assert calls == [1]


def test_a_result_from_a_search_that_a_newer_one_replaced_is_dropped(qtbot, settings_path):
    started = threading.Event()
    gate = threading.Event()

    def search(store):
        if not started.is_set():
            # The first search holds here until the test releases it, after a newer search has finished.
            started.set()
            gate.wait(5)
            return LocatedCore(NOT_FOUND, reason="first")
        return LocatedCore(NOT_FOUND, reason="second")

    actions = RunnerActions(_settings_store(settings_path), locate=search)
    actions.find_core()
    qtbot.waitUntil(started.is_set, timeout=5000)
    # Choosing a folder starts a newer search while the first still runs. The choice resets the running flag first.
    actions._locating = False
    actions.find_core()
    qtbot.waitUntil(lambda: actions.located is not None and actions.located.reason == "second", timeout=5000)
    gate.set()
    qtbot.wait(100)

    assert actions.located.reason == "second"


def test_choosing_a_core_folder_saves_it_and_searches_its_python(qtbot, settings_path, tmp_path):
    checkout = make_checkout(tmp_path / "chosen" / "repository")
    python = make_python(tmp_path / "python.exe")

    def fake_search(store):
        return LocatedCore(FOUND, str(checkout), str(python), SOURCE_SEARCHED)

    store = _settings_store(settings_path)
    actions = RunnerActions(store, locate=fake_search, choose_folder=lambda parent, start: str(checkout))

    message = actions.choose_core_folder(None)
    qtbot.waitUntil(lambda: actions.located is not None, timeout=5000)

    assert message == CHOSEN_TEXT
    saved = store.load().settings
    assert saved.core_checkout == str(checkout)
    assert saved.core_checkout_auto is False  # the owner chose it, so the search never replaces it
    assert saved.core_python == str(python)


def test_cancelling_the_folder_choice_changes_nothing_and_searches_nothing(qtbot, settings_path):
    calls: list[int] = []
    store = _settings_store(settings_path)
    actions = RunnerActions(store, locate=lambda s: calls.append(1) or LocatedCore(NOT_FOUND), choose_folder=lambda p, s: "")

    assert actions.choose_core_folder(None) == CHOOSE_CANCELLED_TEXT
    assert calls == []
    assert not store.path.exists()


def test_choosing_a_folder_that_is_not_a_checkout_is_refused_in_plain_words(qtbot, settings_path, tmp_path):
    not_a_checkout = tmp_path / "plain"
    not_a_checkout.mkdir()
    store = _settings_store(settings_path)
    actions = RunnerActions(store, locate=lambda s: LocatedCore(NOT_FOUND), choose_folder=lambda p, s: str(not_a_checkout))

    message = actions.choose_core_folder(None)

    assert message.startswith("Not saved.")
    assert not store.load().settings.core_checkout


def test_the_controls_are_off_while_the_last_search_found_nothing_even_if_a_saved_path_is_there(qtbot, settings_path, tmp_path):
    store = _settings_store(settings_path)
    store.save(Settings(core_checkout=str(tmp_path / "gone"), core_python=str(tmp_path / "gone" / "python.exe")))
    actions = RunnerActions(store, locate=lambda s: LocatedCore(NOT_FOUND, reason="none"))
    actions.find_core()
    qtbot.waitUntil(lambda: actions.located is not None, timeout=5000)

    core = actions.core()
    assert core.core_ready is False
    assert core.cwd == "" and core.python == ""


def test_enabling_the_tunnel_controls_keeps_a_copy_of_a_settings_file_that_did_not_load(qtbot, settings_path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("{ broken", encoding="utf-8")
    store = _settings_store(settings_path)
    actions = RunnerActions(store)

    message = actions.enable_tunnel_controls()

    assert message.startswith("Tunnel controls turned on.")
    assert store.load().settings.tunnel_control_enabled is True
    assert settings_path.with_name(settings_path.name + ".unreadable.bak").read_text(encoding="utf-8") == "{ broken"
