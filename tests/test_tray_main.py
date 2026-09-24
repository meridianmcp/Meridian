"""4e4c3817 -- focused tests for meridian/tray_main.py.

Covers the pure-logic surface that doesn't require an actual Windows GUI
session: server-command resolution (frozen vs. unfrozen self-relaunch),
environment merging (the real subprocess.Popen env=-replaces-not-merges
hazard this module explicitly guards against), the health probe, and the
--run-server flag dispatch. Does NOT attempt to drive a real pystray tray
icon or tkinter window -- those need an actual display/Windows session, not
a CI test runner; the dialog functions are exercised only for their
LocalRunner-facing logic (via monkeypatched tkinter), never a real GUI loop.

507e55de -- also carries the tray/GUI installer's PRE-SHIP VALIDATION
CHECKLIST (see ``TestTraySpecPreShipConsistency`` below and the standalone
subprocess regression test at the bottom of this file). Those are a
deliberately different kind of test from the unit tests above: instead of
mocking tray_main.py's collaborators, they check the REAL, as-shipped
``meridian-tray.spec`` / ``meridian/static/meridian-tray.ico`` files on disk
for the specific properties a broken pre-ship build has historically failed
on (see the spec file's own comments: a missing ``meridian/static`` datas
entry 500'd the bundled server, a missing ``meridian/templates`` entry
500'd `GET /`). A drift here -- e.g. tray_main.py growing a new third-party
import that the spec's ``hiddenimports`` doesn't know about -- fails a fast
pytest run instead of surfacing as a broken exe after a real PyInstaller
build.
"""
from __future__ import annotations

import ast
import io
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import meridian
from meridian import tray_main

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TRAY_SPEC_PATH = _REPO_ROOT / "meridian-tray.spec"
_TRAY_MAIN_PATH = _REPO_ROOT / "meridian" / "tray_main.py"
_ICO_PATH = _REPO_ROOT / "meridian" / "static" / "meridian-tray.ico"


# ---------------------------------------------------------------------------
# _server_command -- frozen self-relaunch vs. unfrozen direct entry point
# ---------------------------------------------------------------------------


def test_server_command_unfrozen_uses_python_dash_m_meridian(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert tray_main._server_command() == [sys.executable, "-m", "meridian"]


def test_server_command_frozen_self_relaunches_with_flag(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\fake\meridian-tray.exe", raising=False)
    assert tray_main._server_command() == [r"C:\fake\meridian-tray.exe", "--run-server"]


# ---------------------------------------------------------------------------
# _server_env -- must be the FULL environment plus one addition, never a
# bare replacement (subprocess.Popen(env=...) replaces, doesn't merge).
# ---------------------------------------------------------------------------


def test_server_env_preserves_existing_vars_and_adds_frozen_mode(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SOME_OTHER_VAR", "keep-me")
    env = tray_main._server_env()
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["SOME_OTHER_VAR"] == "keep-me"
    assert env["MERIDIAN_FROZEN_MODE"] == "server"


def test_server_env_returns_a_copy_not_the_real_os_environ(monkeypatch):
    monkeypatch.setenv("SHOULD_NOT_LEAK", "1")
    env = tray_main._server_env()
    env["SHOULD_NOT_LEAK"] = "mutated"
    assert os.environ["SHOULD_NOT_LEAK"] == "1"


# ---------------------------------------------------------------------------
# _default_port / _dashboard_url
# ---------------------------------------------------------------------------


def test_default_port_falls_back_to_7878(monkeypatch):
    monkeypatch.delenv("MERIDIAN_PORT", raising=False)
    assert tray_main._default_port() == 7878


def test_default_port_respects_env_override(monkeypatch):
    monkeypatch.setenv("MERIDIAN_PORT", "9999")
    assert tray_main._default_port() == 9999


def test_dashboard_url_uses_the_resolved_port(monkeypatch):
    monkeypatch.setenv("MERIDIAN_PORT", "8123")
    assert tray_main._dashboard_url() == "http://127.0.0.1:8123/"


# ---------------------------------------------------------------------------
# _health_probe -- a real HTTP GET against /health, never a bare port check.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_health_probe_true_on_2xx(monkeypatch):
    monkeypatch.setattr(
        tray_main.urllib.request, "urlopen", lambda url, timeout: _FakeResponse(200)
    )
    assert tray_main._health_probe() is True


def test_health_probe_false_on_non_2xx(monkeypatch):
    monkeypatch.setattr(
        tray_main.urllib.request, "urlopen", lambda url, timeout: _FakeResponse(503)
    )
    assert tray_main._health_probe() is False


def test_health_probe_false_on_connection_error(monkeypatch):
    def _raise(url, timeout):
        raise tray_main.urllib.error.URLError("connection refused")

    monkeypatch.setattr(tray_main.urllib.request, "urlopen", _raise)
    assert tray_main._health_probe() is False


def test_health_probe_false_on_os_error_never_raises(monkeypatch):
    def _raise(url, timeout):
        raise OSError("network unreachable")

    monkeypatch.setattr(tray_main.urllib.request, "urlopen", _raise)
    assert tray_main._health_probe() is False


# ---------------------------------------------------------------------------
# _icon_image_path -- frozen (bundled under _MEIPASS) vs. source (static/)
# ---------------------------------------------------------------------------


def test_icon_image_path_unfrozen_resolves_under_static(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    path = tray_main._icon_image_path()
    assert path.name == "meridian-tray.ico"
    assert path.parent.name == "static"


def test_icon_image_path_frozen_resolves_under_meipass(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    path = tray_main._icon_image_path()
    assert path == tmp_path / "meridian-tray.ico"


# ---------------------------------------------------------------------------
# main() -- --run-server dispatch vs. normal tray launch
# ---------------------------------------------------------------------------


def test_main_run_server_flag_dispatches_to_meridian_entry_and_sets_frozen_mode(monkeypatch):
    monkeypatch.delenv("MERIDIAN_FROZEN_MODE", raising=False)
    called_with = {}

    fake_entry = mock.MagicMock()

    def _fake_main(argv):
        called_with["argv"] = argv
        called_with["frozen_mode"] = os.environ.get("MERIDIAN_FROZEN_MODE")
        return 0

    fake_entry.main = _fake_main
    # Patch BOTH sys.modules and the real `meridian` package's own
    # `__main__` attribute. `from . import __main__` (tray_main.main's
    # dispatch) resolves via attribute lookup on the already-imported
    # `meridian` package object FIRST (CPython's `_handle_fromlist`) and
    # only falls back to sys.modules if that attribute is not yet set --
    # so if any earlier-running test in this process (or this xdist worker)
    # already did a real `import meridian.__main__`, patching sys.modules
    # alone silently does nothing and this test's dispatch call falls
    # through to the REAL entry point, which starts a real, indefinitely
    # -running Uvicorn server inside the test process (confirmed live: this
    # is exactly what caused CI's tray-installer test runs to hang at ~99%
    # instead of finishing -- reproduced locally by importing
    # meridian.__main__ for real before running this test with only the
    # sys.modules patch). Same class of hazard the tkinter dialog tests
    # below already guard against via `fake_tkinter.messagebox = ...`.
    monkeypatch.setitem(sys.modules, "meridian.__main__", fake_entry)
    monkeypatch.setattr(meridian, "__main__", fake_entry, raising=False)

    rc = tray_main.main(["--run-server"])
    assert rc == 0
    assert called_with["argv"] == []
    assert called_with["frozen_mode"] == "server"


def test_main_without_flag_runs_the_tray(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(tray_main, "_run_tray", lambda: sentinel)
    assert tray_main.main([]) is sentinel


def test_run_server_flag_is_hidden_from_help(capsys):
    with pytest.raises(SystemExit):
        tray_main.main(["--help"])
    out = capsys.readouterr().out
    assert "--run-server" not in out


# ---------------------------------------------------------------------------
# Dialog helpers -- exercised against a real (offscreen) LocalRunner status,
# with tkinter itself monkeypatched out so this never needs a real display.
# ---------------------------------------------------------------------------


def test_show_status_dialog_reads_runner_status_without_raising(monkeypatch):
    fake_tkinter = mock.MagicMock()
    fake_messagebox = mock.MagicMock()
    fake_tkinter.Tk.return_value = mock.MagicMock()
    # `from tkinter import messagebox` resolves via attribute access on the
    # already-imported `tkinter` module object -- must be wired explicitly,
    # or Mock auto-attribute creation silently hands back a DIFFERENT mock
    # than the one this test asserts against.
    fake_tkinter.messagebox = fake_messagebox
    monkeypatch.setitem(sys.modules, "tkinter", fake_tkinter)
    monkeypatch.setitem(sys.modules, "tkinter.messagebox", fake_messagebox)

    runner = mock.MagicMock()
    status = mock.MagicMock()
    status.child.state.value = "running"
    status.child.pid = 1234
    status.child.uptime_seconds = 42.0
    status.local_mcp.state.value = "ready"
    status.local_mcp.detail = "health probe reported ready"
    status.warnings = ()
    runner.status.return_value = status

    tray_main._show_status_dialog(runner)
    assert fake_messagebox.showinfo.called
    title, message = fake_messagebox.showinfo.call_args[0][:2]
    assert "running" in message
    assert "1234" in message


# ---------------------------------------------------------------------------
# Pre-ship validation checklist (507e55de) -- real-artifact consistency
# checks between meridian-tray.spec, meridian/static/meridian-tray.ico, and
# tray_main.py itself. These read the ACTUAL files on disk (never mocks),
# so they catch the class of bug a unit test mocking LocalRunner/pystray/PIL
# cannot: a PyInstaller build succeeding but shipping a broken exe because
# the spec drifted from what the module actually needs at runtime.
# ---------------------------------------------------------------------------


def _spec_call_kwargs(spec_path: Path, call_name: str, occurrence: int = 1) -> dict:
    """Statically extract literal keyword arguments from a named call (e.g.
    ``Analysis(...)`` or ``EXE(...)``) inside a PyInstaller .spec file.

    .spec files are real Python but reference names (``Analysis``, ``EXE``,
    ``PYZ``, and the pipeline variables they're chained through) that only
    exist inside PyInstaller's own exec environment -- they cannot be safely
    ``exec``'d here just to validate their shape. Parsing the AST and pulling
    only the literal (list/str/bool/None) keyword values out of the call we
    care about validates the spec's real, checked-in content without needing
    PyInstaller itself (or a full build) to do it.

    73257801 -- the spec now has TWO ``EXE(...)`` calls (macOS's onedir
    build inside ``if _IS_MACOS:``, Windows' onefile build inside the
    ``else:``). ``occurrence`` (1-based) picks which match to return when a
    call name appears more than once -- default 1 keeps every pre-existing,
    unambiguous call site (``Analysis``/``PYZ``/``COLLECT``/``BUNDLE`` each
    appear exactly once) working unchanged; the macOS branch is textually
    first, so the Windows-only ``EXE(...)`` call is occurrence=2.
    """
    tree = ast.parse(spec_path.read_text(encoding="utf-8"))
    seen = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == call_name:
            seen += 1
            if seen != occurrence:
                continue
            kwargs: dict = {}
            for kw in node.keywords:
                if kw.arg is None:
                    continue
                try:
                    kwargs[kw.arg] = ast.literal_eval(kw.value)
                except (ValueError, TypeError):
                    # 73257801 -- a list kwarg may contain a platform-
                    # conditional ternary element (e.g.
                    # `'pystray._darwin' if _IS_MACOS else 'pystray._win32'`),
                    # which isn't a literal, so a whole-list literal_eval
                    # fails even though every OTHER element is a plain
                    # literal. Fall back to evaluating element-by-element,
                    # resolving such a ternary to BOTH of its literal
                    # branches -- a static pre-ship check just needs to know
                    # a given literal is reachable somewhere in the list, not
                    # which platform's branch actually picks it at build
                    # time. A genuinely non-literal, non-list value (e.g.
                    # `cipher=block_cipher`, a bare Name reference) is still
                    # skipped entirely, unchanged from before.
                    if isinstance(kw.value, ast.List):
                        elts: list = []
                        ok = True
                        for elt in kw.value.elts:
                            try:
                                if isinstance(elt, ast.IfExp):
                                    elts.append(ast.literal_eval(elt.body))
                                    elts.append(ast.literal_eval(elt.orelse))
                                else:
                                    elts.append(ast.literal_eval(elt))
                            except (ValueError, TypeError):
                                ok = False
                                break
                        if ok:
                            kwargs[kw.arg] = elts
                    continue
            return kwargs
    raise AssertionError(f"no {call_name}(...) call found in {spec_path}")


def _tray_main_top_level_imports() -> set[str]:
    """Top-level module names tray_main.py imports anywhere in its source
    (module scope or inside a function, e.g. the lazily-imported ``pystray``/
    ``PIL`` inside ``_run_tray``) -- excludes relative imports (``from .
    local_runner import ...``, ``from . import __main__``), which are
    covered by their own explicit hiddenimports checks below instead.
    """
    tree = ast.parse(_TRAY_MAIN_PATH.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                modules.add(node.module.split(".")[0])
    return modules


class TestTraySpecPreShipConsistency:
    """Cross-checks between the real, committed meridian-tray.spec,
    meridian/static/meridian-tray.ico, and tray_main.py."""

    def test_ico_asset_exists_and_has_valid_ico_magic(self):
        assert _ICO_PATH.is_file(), f"missing tray icon asset: {_ICO_PATH}"
        header = _ICO_PATH.read_bytes()[:4]
        # ICO file format header: reserved(2)=0x0000, type(2)=0x0001.
        assert header == b"\x00\x00\x01\x00", (
            f"{_ICO_PATH} does not have a valid .ico header (got {header!r})"
        )

    def test_spec_datas_paths_all_exist_relative_to_repo_root(self):
        datas = _spec_call_kwargs(_TRAY_SPEC_PATH, "Analysis")["datas"]
        assert datas, "meridian-tray.spec Analysis(datas=...) is empty"
        for src, _dest in datas:
            path = _REPO_ROOT / src
            assert path.exists(), (
                f"meridian-tray.spec datas=... references {src!r}, which "
                f"does not exist at {path}"
            )

    def test_spec_bundles_the_real_static_and_templates_directories(self):
        # Regression check for the two documented live-build failures in the
        # spec file's own comments: a missing 'meridian/static' entry 500'd
        # the bundled server's StaticFiles mount, and a missing
        # 'meridian/templates' entry 500'd `GET /` (Jinja2Templates).
        datas_sources = {src for src, _dest in _spec_call_kwargs(_TRAY_SPEC_PATH, "Analysis")["datas"]}
        assert "meridian/static/meridian-tray.ico" in datas_sources
        assert "meridian/static" in datas_sources
        assert "meridian/templates" in datas_sources

    def test_spec_exe_icon_kwarg_points_at_the_real_ico_file(self):
        # occurrence=2: the Windows-only EXE() call (see 73257801) -- the
        # macOS EXE() sets no icon at all (BUNDLE() carries it there instead).
        icon = _spec_call_kwargs(_TRAY_SPEC_PATH, "EXE", occurrence=2).get("icon")
        assert icon == "meridian/static/meridian-tray.ico"
        assert (_REPO_ROOT / icon).is_file()

    def test_spec_exe_is_a_windowed_gui_app_not_a_console_tool(self):
        exe_kwargs = _spec_call_kwargs(_TRAY_SPEC_PATH, "EXE")
        assert exe_kwargs.get("console") is False, (
            "meridian-tray.exe is a tray/GUI app -- console=True would pop "
            "a terminal window behind the tray icon on every launch"
        )

    def test_spec_exe_name_matches_the_documented_binary_name(self):
        assert _spec_call_kwargs(_TRAY_SPEC_PATH, "EXE").get("name") == "meridian-tray"

    def test_spec_exe_is_a_single_onefile_binary(self):
        # The module docstring promises "a single binary that is BOTH the
        # tray icon and ... the real Meridian HTTP server" -- onefile=False
        # would ship a directory instead, breaking that contract silently.
        # This is the Windows shape specifically (occurrence=2, see
        # 73257801) -- macOS deliberately uses onedir+BUNDLE instead, since
        # pystray's menu-bar icon needs a real .app bundle to show at all.
        assert _spec_call_kwargs(_TRAY_SPEC_PATH, "EXE", occurrence=2).get("onefile") is True

    def test_spec_hiddenimports_has_no_duplicate_entries(self):
        hidden = _spec_call_kwargs(_TRAY_SPEC_PATH, "Analysis")["hiddenimports"]
        assert len(hidden) == len(set(hidden)), (
            f"duplicate entries in meridian-tray.spec hiddenimports: {hidden}"
        )

    @pytest.mark.parametrize(
        "required_entry",
        [
            "meridian.tray_main",  # PyInstaller's own Analysis(['meridian/tray_main.py']) entry
            "meridian.__main__",  # main()'s `from . import __main__` (--run-server dispatch)
            "meridian.server",  # the real HTTP server --run-server ultimately serves
            "meridian.local_runner",  # module-level `from .local_runner import (...)`
            "pystray._win32",  # _run_tray()'s lazily-imported `import pystray`
            "PIL",  # _run_tray()'s lazily-imported `from PIL import Image`
            "PIL.Image",
        ],
    )
    def test_spec_hiddenimports_covers_tray_mains_real_dependencies(self, required_entry):
        hidden = _spec_call_kwargs(_TRAY_SPEC_PATH, "Analysis")["hiddenimports"]
        assert required_entry in hidden, (
            f"meridian-tray.spec hiddenimports is missing {required_entry!r}, "
            "which tray_main.py needs at runtime (directly, or via "
            "local_runner/server/--run-server dispatch)"
        )

    def test_spec_hiddenimports_covers_every_third_party_import_tray_main_uses(self):
        """Statically derives tray_main.py's own third-party imports (stdlib
        modules and the relative `meridian` package excluded) and asserts
        each has a matching hiddenimports entry -- so this test itself
        breaks, rather than silently drifting, the next time tray_main.py
        grows a new third-party import the spec hasn't been updated for.
        """
        third_party = _tray_main_top_level_imports() - set(sys.stdlib_module_names) - {"meridian"}
        assert third_party, "sanity check: expected at least pystray/PIL here"
        hidden = _spec_call_kwargs(_TRAY_SPEC_PATH, "Analysis")["hiddenimports"]
        for module in sorted(third_party):
            assert any(entry == module or entry.startswith(module + ".") for entry in hidden), (
                f"tray_main.py imports {module!r} but meridian-tray.spec's "
                f"hiddenimports does not declare it (or any submodule of it): {hidden}"
            )


@pytest.mark.subprocess_isolated
def test_running_tray_main_as_a_direct_script_does_not_hit_relative_import_error():
    """Regression check for the exact frozen-crash class tray_main.py's own
    module docstring documents: PyInstaller's Analysis(['meridian/tray_main.py'])
    runs this file as __main__ with __package__ unset, which historically
    crashed the frozen exe with "attempted relative import with no known
    parent package" before the module's top-of-file __package__ fixup was
    added. A real PyInstaller build isn't available in a plain pytest run,
    but invoking the script directly (not via `python -m`, which would
    already set __package__ correctly and mask the bug) reproduces the same
    "no parent package" starting condition the frozen exe hits, over a real
    subprocess -- not a mock.
    """
    result = subprocess.run(
        [sys.executable, str(_TRAY_MAIN_PATH), "--help"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"direct-script invocation failed (rc={result.returncode}):\n{result.stderr}"
    )
    assert "attempted relative import" not in result.stderr
    assert "--run-server" not in result.stdout, (
        "the internal --run-server flag must stay hidden from --help output"
    )
