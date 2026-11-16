"""Find the Meridian core checkout and the Python that runs it, so the owner does not have to set them by hand. Pure Python: no Qt.

locate_core works out a (checkout, Python) pair. It stops at the first step that gives a usable pair:

1. The checkout and Python saved in settings, when the checkout is a Meridian checkout and the Python imports it.
   A value the owner chose by hand is kept as it is. When it does not work, locate_core says so and does not replace it.
2. The Python of the saved checkout, when only the Python is missing or was found automatically before.
3. The candidate checkouts. These are a Meridian folder with a repository subfolder, or the Meridian folder itself, in
   the Documents folder that holds the desktop repo and in the user's own Documents folder. A candidate must hold
   meridian/__init__.py and a pyproject.toml or pixi.toml. Nothing else on the disk is looked at.

For a checkout, its Python is the first candidate that imports that checkout's meridian package. The candidates are, in
order: the Python saved in settings, the pixi environment that the checkout's own .pixi/envs/README.txt names, the
checkout's in-repo .pixi/envs/default, and the newest pixi environments under the user's .pixi\\workspace-envs folder
that carry the checkout's workspace name from pixi.toml. At most MAX_ENVIRONMENTS of the last kind are tried.

Every Python is checked by probe_python: one fixed one-line program, run with that Python, the checkout as its working
folder, no shell, a list argument, and a short timeout. A Python that fails the probe is refused.

locate_core never writes settings. remember_core saves a pair that was found by search. It writes a field only when that
field is empty, or when the locator found it earlier (core_checkout_auto, core_python_auto). It never overwrites a value
the owner saved by hand. Saving a found pair does not touch the launch-time switches. Those start only with the values
the owner confirmed (docs/local-trust-contract.md), so a change here cannot change what a launch starts.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from meridian_desktop.settings_store import (
    CORE_AUTO_FLAGS,
    CORE_CHECKOUT_KEY,
    CORE_PYTHON_KEY,
    SettingsStore,
    core_path_problem,
)

FOUND = "found"
NOT_FOUND = "not_found"
# The pair saved in settings still works. Nothing needs saving.
SOURCE_SAVED = "saved"
# The pair was found by search. remember_core may save it.
SOURCE_SEARCHED = "searched"

PROBE_TIMEOUT_SECONDS = 10.0
# The whole search for one run stops starting new probes after this many seconds.
SEARCH_BUDGET_SECONDS = 25.0
# The newest pixi environments named after the checkout's workspace that are tried, at most.
MAX_ENVIRONMENTS = 6

MERIDIAN_FOLDER = "Meridian"
REPOSITORY_FOLDER = "repository"
DOCUMENTS_FOLDER = "Documents"
PIXI_FOLDER = ".pixi"
WORKSPACE_ENVS_FOLDER = "workspace-envs"
# One fixed program. It prints the file the checkout's meridian package was imported from, then whether the runner
# module is there. Only the checkout's own package is accepted.
PROBE_PROGRAM = (
    "import importlib.util as u, os, meridian as m; "
    "print(os.path.normcase(os.path.abspath(m.__file__))); "
    "print(u.find_spec('meridian.local_runner') is not None)"
)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_WORKSPACE_TABLE = re.compile(r"^\[workspace\][ \t]*$(.*?)(?=^\[|\Z)", re.MULTILINE | re.DOTALL)
_NAME_LINE = re.compile(r'^[ \t]*name[ \t]*=[ \t]*"([^"]+)"', re.MULTILINE)
# Pixi writes this line when it keeps an environment outside the repo. The first group is the workspace-envs folder,
# the second is the environment's own folder name, for example meridian-6725095497969888438.
_README_ENV = re.compile(r"([^\s'\"]*?workspace-envs)[\\/]+([^\\/\s]+)")

PLAIN_NOT_CHECKOUT_TEXT = "The Meridian core folder saved in Settings is not a Meridian checkout: {path}."
PLAIN_SAVED_PYTHON_TEXT = "The Python saved in Settings does not import meridian from {checkout}."
PLAIN_NO_PYTHON_TEXT = "No Python that imports meridian was found for {checkout}."
PLAIN_NO_CHECKOUT_TEXT = "No Meridian core folder was found in the Documents folders on this computer."

RunFn = Callable[..., subprocess.CompletedProcess]
ProbeFn = Callable[[str, str], bool]


@dataclass(frozen=True)
class LocatedCore:
    """The result of one search. ``found`` is True with a checkout and a Python, or False with a plain reason.

    ``source`` is SOURCE_SAVED when the saved pair still works, and SOURCE_SEARCHED when search found it.
    """

    status: str
    checkout: str = ""
    python: str = ""
    source: str = ""
    reason: str = ""

    @property
    def found(self) -> bool:
        return self.status == FOUND


def _not_found(reason: str) -> LocatedCore:
    return LocatedCore(NOT_FOUND, reason=reason)


def _path_key(text: str) -> str:
    """A key that compares two paths the way Windows does: resolved, and not case-sensitive."""
    return os.path.normcase(os.path.realpath(text))


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def is_checkout(path: Path) -> bool:
    """True for a folder that holds the meridian package and a pixi.toml or pyproject.toml. Read-only."""
    try:
        return (path / "meridian" / "__init__.py").is_file() and (
            (path / "pyproject.toml").is_file() or (path / "pixi.toml").is_file()
        )
    except OSError:
        return False


def probe_python(python: str, checkout: str, *, run: RunFn = subprocess.run) -> bool:
    """True when ``python`` runs, imports the meridian package from ``checkout``, and that checkout has the runner module.

    The program is fixed. No text from the owner is put in the command, and no shell is used. A timeout, a missing
    program, a non-zero exit, or output that names another checkout is False. ``run`` is injectable, so a test never
    starts a process.
    """
    if not python or not checkout or "\x00" in python or not os.path.isfile(python):
        return False
    expected = _path_key(os.path.join(checkout, "meridian", "__init__.py"))
    try:
        completed = run(
            [python, "-c", PROBE_PROGRAM],
            shell=False,
            cwd=checkout,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            creationflags=_NO_WINDOW,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    if completed.returncode != 0:
        return False
    lines = (completed.stdout or b"").decode("utf-8", errors="replace").splitlines()
    if len(lines) < 2:
        return False
    return _path_key(lines[-2].strip()) == expected and lines[-1].strip() == "True"


def workspace_name(checkout: Path) -> str:
    """The [workspace] name from the checkout's pixi.toml, or "" when there is none."""
    table = _WORKSPACE_TABLE.search(_read_text(checkout / "pixi.toml"))
    if table is None:
        return ""
    line = _NAME_LINE.search(table.group(1))
    return line.group(1).strip() if line else ""


def named_environments(checkout: Path) -> list[Path]:
    """The environment folders that the checkout's own .pixi/envs/README.txt names, in the order the file lists them."""
    text = _read_text(checkout / PIXI_FOLDER / "envs" / "README.txt")
    envs: list[Path] = []
    for match in _README_ENV.finditer(text):
        envs.append(Path(match.group(1)) / match.group(2) / "envs")
    return envs


def _env_python(envs_dir: Path) -> Path | None:
    """The Python inside one pixi environment (envs/default). None when it has none."""
    default = envs_dir / "default"
    for candidate in (default / "python.exe", default / "bin" / "python"):
        if candidate.is_file():
            return candidate
    return None


def _workspace_envs(roots: Sequence[Path], workspace: str) -> list[Path]:
    """The newest pixi environment folders under the roots whose name is the workspace name and a number."""
    if not workspace:
        return []
    pattern = re.compile(re.escape(workspace) + r"-\d+")
    stamped: list[tuple[float, Path]] = []
    for root in roots:
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if pattern.fullmatch(entry.name) and entry.is_dir():
                    stamped.append((entry.stat().st_mtime, entry))
            except OSError:
                continue
    stamped.sort(key=lambda item: item[0], reverse=True)
    return [entry / "envs" for _stamp, entry in stamped[:MAX_ENVIRONMENTS]]


def python_candidates(checkout: Path, pixi_roots: Sequence[Path]) -> list[str]:
    """The Python programs that might run this checkout, in the order they are tried. Nothing is run here."""
    env_dirs = named_environments(checkout)
    env_dirs.append(checkout / PIXI_FOLDER / "envs")
    env_dirs.extend(_workspace_envs(pixi_roots, workspace_name(checkout)))
    pythons: list[str] = []
    for envs in env_dirs:
        python = _env_python(envs)
        if python is not None and str(python) not in pythons:
            pythons.append(str(python))
    return pythons


def find_python(
    checkout: Path,
    *,
    pixi_roots: Sequence[Path],
    probe: ProbeFn,
    clock: Callable[[], float] = time.monotonic,
    only: str = "",
) -> str:
    """The first Python that imports this checkout, or "" when none does. ``only`` limits the search to one Python."""
    deadline = clock() + SEARCH_BUDGET_SECONDS
    candidates = [only] if only else python_candidates(checkout, pixi_roots)
    for python in candidates:
        if clock() > deadline:
            break
        if probe(python, str(checkout)):
            return python
    return ""


def documents_folders(repo_root: Path, home: Path) -> list[Path]:
    """The Documents folders to look in: the one that holds the desktop repo, then the user's own Documents folder.

    The repo's Documents folder is its nearest ancestor with that name. A worktree under .claude/worktrees is inside
    the repo, so its ancestors reach the same folder.
    """
    folders: list[Path] = []
    nearest = next((p for p in repo_root.parents if p.name.lower() == DOCUMENTS_FOLDER.lower()), None)
    if nearest is not None:
        folders.append(nearest)
    folders.append(home / DOCUMENTS_FOLDER)
    unique: list[Path] = []
    for folder in folders:
        if _path_key(str(folder)) not in [_path_key(str(item)) for item in unique]:
            unique.append(folder)
    return unique


def candidate_checkouts(folders: Iterable[Path]) -> list[Path]:
    """The checkouts to try, in order: Meridian/repository, then the Meridian folder itself, for each Documents folder."""
    found: list[Path] = []
    for folder in folders:
        meridian = folder / MERIDIAN_FOLDER
        for path in (meridian / REPOSITORY_FOLDER, meridian):
            if is_checkout(path) and _path_key(str(path)) not in [_path_key(str(item)) for item in found]:
                found.append(path)
    return found


def default_pixi_roots(home: Path) -> list[Path]:
    """The user's pixi workspace-envs folder, where pixi keeps its detached environments."""
    return [home / PIXI_FOLDER / WORKSPACE_ENVS_FOLDER]


def locate_core(
    store: SettingsStore,
    *,
    repo_root: Path | None = None,
    home: Path | None = None,
    probe: ProbeFn | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> LocatedCore:
    """Find a usable (checkout, Python) pair. Reads settings and folders, and runs only the probe. Never writes settings.

    ``repo_root`` defaults to the folder that holds this package's parent, and ``home`` to the user's home folder. Both
    are injectable so a test uses a temporary folder tree.
    """
    settings = store.load().settings
    probe_fn: ProbeFn = probe if probe is not None else probe_python
    home_dir = home if home is not None else Path.home()
    repo = repo_root if repo_root is not None else Path(__file__).resolve().parents[1]
    pixi_roots = default_pixi_roots(home_dir)

    saved_checkout = settings.core_checkout.strip()
    saved_python = settings.core_python.strip()
    checkout_by_hand = bool(saved_checkout) and not settings.core_checkout_auto
    python_by_hand = bool(saved_python) and not settings.core_python_auto

    if saved_checkout and is_checkout(Path(saved_checkout)):
        if saved_python and probe_fn(saved_python, saved_checkout):
            return LocatedCore(FOUND, saved_checkout, saved_python, SOURCE_SAVED)
        if python_by_hand:
            return _not_found(PLAIN_SAVED_PYTHON_TEXT.format(checkout=saved_checkout))
        python = find_python(Path(saved_checkout), pixi_roots=pixi_roots, probe=probe_fn, clock=clock)
        if python:
            return LocatedCore(FOUND, saved_checkout, python, SOURCE_SEARCHED)
        return _not_found(PLAIN_NO_PYTHON_TEXT.format(checkout=saved_checkout))

    if checkout_by_hand:
        return _not_found(PLAIN_NOT_CHECKOUT_TEXT.format(path=saved_checkout))

    # The saved checkout is missing, or the locator found it earlier and it is gone: look for a checkout again. A Python
    # the owner chose by hand is the only one tried, so a found checkout never pairs with some other Python.
    only = saved_python if python_by_hand else ""
    checkouts = candidate_checkouts(documents_folders(repo, home_dir))
    for checkout in checkouts:
        python = find_python(checkout, pixi_roots=pixi_roots, probe=probe_fn, clock=clock, only=only)
        if python:
            return LocatedCore(FOUND, str(checkout), python, SOURCE_SEARCHED)
    if checkouts:
        # A checkout is there, but no Python imports it, so say that rather than that no folder exists.
        return _not_found(PLAIN_NO_PYTHON_TEXT.format(checkout=checkouts[0]))
    return _not_found(PLAIN_NO_CHECKOUT_TEXT)


def remember_core(store: SettingsStore, located: LocatedCore) -> bool:
    """Save a pair found by search, into the fields the locator may write. True when settings were written.

    A field the owner set by hand is never written. A field the locator found earlier is replaced. A result that was not
    found by search writes nothing here, except that a stale path the locator found earlier is cleared, so the controls
    turn off until the search finds a new one. The settings file is copied aside first when it did not load cleanly.
    """
    settings = store.load().settings
    changes: dict[str, object] = {}
    if located.found and located.source == SOURCE_SEARCHED:
        for key, value in ((CORE_CHECKOUT_KEY, located.checkout), (CORE_PYTHON_KEY, located.python)):
            current = getattr(settings, key)
            if value == current:
                continue
            if not current or getattr(settings, CORE_AUTO_FLAGS[key]):
                changes[key] = value
                changes[CORE_AUTO_FLAGS[key]] = True
    elif not located.found:
        for key in (CORE_CHECKOUT_KEY, CORE_PYTHON_KEY):
            current = getattr(settings, key)
            flag = CORE_AUTO_FLAGS[key]
            if current and getattr(settings, flag) and core_path_problem(key, current) is not None:
                changes[key] = ""
                changes[flag] = False
    if not changes:
        return False
    store.keep_unreadable_copy()
    store.save(replace(settings, **changes))
    return True


__all__ = [
    "FOUND",
    "MAX_ENVIRONMENTS",
    "NOT_FOUND",
    "PROBE_PROGRAM",
    "SEARCH_BUDGET_SECONDS",
    "SOURCE_SAVED",
    "SOURCE_SEARCHED",
    "LocatedCore",
    "candidate_checkouts",
    "default_pixi_roots",
    "documents_folders",
    "find_python",
    "is_checkout",
    "locate_core",
    "named_environments",
    "probe_python",
    "python_candidates",
    "remember_core",
    "workspace_name",
]
