"""Tests for meridian_desktop/desktop_log.py and for the desktop's start-up order in ui/app.py.

Every file here lives under pytest's temp directory. The environment mapping and the home directory
are passed in, so no test reads the real LOCALAPPDATA or home directory. The app-level tests use
fakes for Qt, the window and the tray, so no window is built except in the smoke test that already
exists. Fake secrets carry a fake marker and match no live token.
"""

import ast
import logging
from pathlib import Path

import pytest

from meridian_desktop.desktop_log import (
    HANDLER_NAME,
    LOG_NAME,
    LOGGER_NAME,
    READ_MAX_BYTES,
    ROTATE_MAX_BYTES,
    STARTED_MESSAGE,
    desktop_log_path,
    detach_desktop_log,
    read_desktop_log_tail,
    redact_text,
    start_desktop_log,
)
from meridian_desktop.log_tail import (
    DEFAULT_MAX_BYTES,
    STATUS_NO_LOG_YET,
    STATUS_OK,
    STATUS_UNREADABLE,
)
from meridian_desktop.ui import app as app_module

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "meridian_desktop" / "desktop_log.py"

FAKE_QUERY = "fake-query-0001"
FAKE_BEARER = "sk_meridian_fake-bearer-0002"
FAKE_BARE = "sk_meridian_fake-bare-0003"
FAKE_PASSWORD = "fake-password-0004"
FAKE_API_KEY = "fake-api-key-0005"
FAKE_TRACEBACK = "fake-traceback-0006"
SECRETS = (FAKE_QUERY, FAKE_BEARER, FAKE_BARE, FAKE_PASSWORD, FAKE_API_KEY, FAKE_TRACEBACK)

ALLOWED_IMPORT_ROOTS = {"__future__", "collections", "logging", "os", "pathlib", "re", "meridian_desktop"}


@pytest.fixture(autouse=True)
def _detach_the_desktop_handler():
    """Each test starts and ends with no desktop file handler and the logger at its default level."""
    detach_desktop_log()
    yield
    detach_desktop_log()
    logging.getLogger(LOGGER_NAME).setLevel(logging.NOTSET)


def _env(tmp_path: Path) -> dict[str, str]:
    return {"LOCALAPPDATA": str(tmp_path / "local")}


def _log_file(tmp_path: Path) -> Path:
    return desktop_log_path(env=_env(tmp_path), home=tmp_path / "home")


def _has_desktop_handler() -> bool:
    return any(handler.get_name() == HANDLER_NAME for handler in logging.getLogger(LOGGER_NAME).handlers)


# ----------------------------------------------------------- location


def test_log_path_is_under_localappdata_when_it_is_set(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path / "local")}

    assert desktop_log_path(env=env, home=tmp_path / "home") == (
        tmp_path / "local" / "meridian-desktop" / "logs" / "desktop.log"
    )
    assert LOG_NAME == "desktop.log"


@pytest.mark.parametrize("env", [{}, {"LOCALAPPDATA": ""}, {"LOCALAPPDATA": "   "}])
def test_log_path_falls_back_under_home_when_localappdata_is_unset_or_blank(tmp_path, env):
    home = tmp_path / "home"

    assert desktop_log_path(env=env, home=home) == home / ".meridian-desktop" / "logs" / "desktop.log"


# ----------------------------------------------------------- start and write


def test_start_writes_the_start_line_into_the_injected_folder(tmp_path):
    path = start_desktop_log(env=_env(tmp_path), home=tmp_path / "home")

    assert path == _log_file(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert STARTED_MESSAGE in text
    assert "INFO meridian_desktop:" in text
    assert _has_desktop_handler()


def test_secret_shaped_text_is_redacted_in_the_file(tmp_path):
    path = start_desktop_log(env=_env(tmp_path), home=tmp_path / "home")
    log = logging.getLogger(LOGGER_NAME)

    log.info("connect https://example.test/mcp?token=%s&slot=fs", FAKE_QUERY)
    log.info("Authorization: Bearer %s", FAKE_BEARER)
    log.info("key %s rejected", FAKE_BARE)
    log.info("password=%s", FAKE_PASSWORD)
    log.info("api_key: '%s'", FAKE_API_KEY)

    text = path.read_text(encoding="utf-8")
    for secret in SECRETS[:5]:
        assert secret not in text
    assert "token=[redacted]&slot=fs" in text
    assert "Bearer [redacted]" in text
    assert "key [redacted] rejected" in text
    assert "password=[redacted]" in text
    assert "api_key: [redacted]" in text


def test_a_traceback_is_redacted_too(tmp_path):
    path = start_desktop_log(env=_env(tmp_path), home=tmp_path / "home")
    log = logging.getLogger(LOGGER_NAME)

    try:
        raise ValueError(f"token={FAKE_TRACEBACK}")
    except ValueError:
        log.exception("request failed")

    text = path.read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in text
    assert "ValueError: token=[redacted]" in text
    assert FAKE_TRACEBACK not in text


def test_redact_text_covers_the_shapes_and_leaves_ordinary_text_alone():
    assert redact_text(f"GET /ws?token={FAKE_QUERY}") == "GET /ws?token=[redacted]"
    assert redact_text(f"Authorization: Bearer {FAKE_BEARER}") == "Authorization: Bearer [redacted]"
    assert redact_text(f"key {FAKE_BARE} rejected") == "key [redacted] rejected"
    assert redact_text(f"sk_meridian_{FAKE_BARE}") == "[redacted]"
    assert redact_text(f"DB_PASSWORD={FAKE_PASSWORD}") == "DB_PASSWORD=[redacted]"
    assert redact_text(f"api-key: {FAKE_API_KEY}") == "api-key: [redacted]"
    assert redact_text(f'secret: "{FAKE_API_KEY} x" end') == "secret: [redacted] end"
    assert redact_text(f"token={FAKE_QUERY}&slot=fs") == "token=[redacted]&slot=fs"
    for line in (
        "tokens per second: 5",
        "secret_store: ok",
        "passwordless: yes",
        "token: ",
        "connect ok on slot fs",
    ):
        assert redact_text(line) == line


# ----------------------------------------------------------- bounded rotation


def test_rotation_keeps_the_folder_bounded(tmp_path):
    path = start_desktop_log(env=_env(tmp_path), home=tmp_path / "home", max_bytes=2000, backup_count=2)
    log = logging.getLogger(LOGGER_NAME)

    for index in range(400):
        log.info("row %04d %s", index, "x" * 60)

    names = sorted(entry.name for entry in path.parent.iterdir())
    assert names == ["desktop.log", "desktop.log.1", "desktop.log.2"]
    assert all(entry.stat().st_size <= 2000 for entry in path.parent.iterdir())


def test_the_newest_lines_are_read_back_by_the_bounded_reader(tmp_path):
    path = start_desktop_log(env=_env(tmp_path), home=tmp_path / "home", max_bytes=2000, backup_count=2)
    log = logging.getLogger(LOGGER_NAME)
    for index in range(400):
        log.info("row %04d %s", index, "x" * 60)

    tail = read_desktop_log_tail(env=_env(tmp_path), home=tmp_path / "home", max_bytes=512)

    assert tail.status == STATUS_OK
    assert tail.path == path
    assert "row 0399" in tail.lines[-1]  # the newest row is the last line read back
    assert sum(len(line.encode("utf-8")) + 1 for line in tail.lines) <= 512


# ----------------------------------------------------------- idempotence and failure


def test_starting_twice_keeps_one_handler_and_writes_each_line_once(tmp_path):
    path = start_desktop_log(env=_env(tmp_path), home=tmp_path / "home")
    start_desktop_log(env=_env(tmp_path), home=tmp_path / "home")
    logging.getLogger(LOGGER_NAME).info("once-marker")

    handlers = [h for h in logging.getLogger(LOGGER_NAME).handlers if h.get_name() == HANDLER_NAME]
    assert len(handlers) == 1
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if "once-marker" in line]
    assert len(lines) == 1


def test_detach_removes_and_closes_the_handler(tmp_path):
    start_desktop_log(env=_env(tmp_path), home=tmp_path / "home")
    assert _has_desktop_handler()

    detach_desktop_log()

    assert not _has_desktop_handler()
    detach_desktop_log()  # safe when nothing is attached


def test_an_unwritable_folder_returns_none_and_attaches_nothing(tmp_path):
    blocker = tmp_path / "local"
    blocker.write_text("a file where a folder should be", encoding="utf-8")

    result = start_desktop_log(env={"LOCALAPPDATA": str(blocker)}, home=tmp_path / "home")

    assert result is None
    assert not _has_desktop_handler()


@pytest.mark.parametrize(
    "kwargs",
    [{"max_bytes": 0}, {"max_bytes": True}, {"max_bytes": "100"}, {"backup_count": -1}],
)
def test_a_bad_budget_is_refused_before_any_folder_is_made(tmp_path, kwargs):
    with pytest.raises(ValueError):
        start_desktop_log(env=_env(tmp_path), home=tmp_path / "home", **kwargs)

    assert not (tmp_path / "local").exists()
    assert not _has_desktop_handler()


# ----------------------------------------------------------- reading


def test_the_default_read_is_the_bounded_tail_not_the_rotation_size(tmp_path):
    assert READ_MAX_BYTES == DEFAULT_MAX_BYTES == 65536
    assert ROTATE_MAX_BYTES == 1024 * 1024
    path = _log_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"".join(b"row %06d %s\n" % (index, b"x" * 40) for index in range(40000)))  # about 2 MB

    tail = read_desktop_log_tail(env=_env(tmp_path), home=tmp_path / "home")

    assert tail.status == STATUS_OK
    assert sum(len(line.encode("utf-8")) + 1 for line in tail.lines) <= DEFAULT_MAX_BYTES
    assert tail.lines[-1].startswith("row 039999")
    assert "row 000000" not in tail.lines


def test_a_missing_desktop_log_reads_no_log_yet_and_creates_nothing(tmp_path):
    tail = read_desktop_log_tail(env=_env(tmp_path), home=tmp_path / "home")

    assert tail.status == STATUS_NO_LOG_YET
    assert tail.lines == ()
    assert not (tmp_path / "local").exists()


def test_a_desktop_log_that_cannot_be_read_is_a_status(tmp_path):
    path = _log_file(tmp_path)
    path.mkdir(parents=True)  # a folder cannot be read as a log

    tail = read_desktop_log_tail(env=_env(tmp_path), home=tmp_path / "home")

    assert tail.status == STATUS_UNREADABLE


# ----------------------------------------------------------- module rules


def test_desktop_log_module_imports_no_qt_and_opens_no_file_itself():
    tree = ast.parse(MODULE_PATH.read_bytes(), filename=str(MODULE_PATH))
    imported: set[str] = set()
    opens = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            opens.append(node)
    assert imported <= ALLOWED_IMPORT_ROOTS
    assert "PySide6" not in imported
    assert opens == [], "the desktop log writes through logging only, and reads through log_tail"


# ----------------------------------------------------------- start-up order in the app


class _FakeQApp:
    """Stands in for QApplication. Records the order of start-up and returns 0 from exec."""

    events: list[str] = []

    def __init__(self, argv):
        _FakeQApp.events.append("qapp")

    @classmethod
    def instance(cls):
        return None

    def setApplicationName(self, name):
        pass

    def setQuitOnLastWindowClosed(self, flag):
        pass

    def exec(self):
        return 0


class _FakeWindow:
    events: list[str] = []

    def __init__(
        self,
        store,
        update_transport=None,
        update_root=None,
        record_apps_entry=None,
        runner_actions=None,
        connection_transport=None,
        restart_watch=None,
    ):
        # The app passes the update folder, the Apps entry writer, the shared runner controls, the connection bar's
        # transport and the restart watch as well. The fake takes them and ignores them, so the start-order
        # assertions are unchanged.
        _FakeWindow.events.append("window")

    def show(self):
        pass

    def isVisible(self):
        return True

    def show_launch_notes(self, text):
        # The real window shows the launch-time notes on its status bar. The fake has no status bar to show them on.
        pass


class _FakeTray:
    def __init__(self, window, app):
        pass

    def show(self):
        pass


@pytest.fixture
def start_order(monkeypatch):
    """Replace the app's Qt pieces and the desktop log starter with recording fakes."""
    events: list[str] = []
    _FakeQApp.events = events
    _FakeWindow.events = events
    calls: list[tuple] = []

    def fake_start(*args, **kwargs):
        calls.append((args, kwargs))
        events.append("log")
        return None

    monkeypatch.setattr(app_module, "QApplication", _FakeQApp)
    monkeypatch.setattr(app_module, "MainWindow", _FakeWindow)
    monkeypatch.setattr(app_module, "MeridianTray", _FakeTray)
    monkeypatch.setattr(app_module, "start_desktop_log", fake_start)
    return events, calls


def test_main_starts_the_desktop_log_before_the_window_is_built(start_order, tmp_path, capsys, monkeypatch):
    events, calls = start_order
    # The real event loop runs until the window closes, and the first-run dialog waits for a person.
    # The test needs main to reach the loop, so the loop is stubbed to return at once.
    monkeypatch.setattr(app_module.QApplication, "exec", lambda self: 0)

    code = app_module.main([], settings_path=tmp_path / "settings.json", first_run=False)

    assert code == 0
    assert events == ["log", "qapp", "window"]
    assert len(calls) == 1


def test_smoke_run_writes_no_desktop_log(start_order, tmp_path, capsys):
    events, calls = start_order

    code = app_module.main(["--smoke"], settings_path=tmp_path / "settings.json")

    assert code == 0
    assert calls == []
    assert "log" not in events
    assert "smoke ok" in capsys.readouterr().out
