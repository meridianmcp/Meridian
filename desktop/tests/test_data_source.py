"""Tests for meridian_desktop/data_source.py.

Every outside call is a fake that records its name. Nothing here opens a real database, reads the
real home directory or touches the OS keychain. The one real probe runs against a loopback stub on
127.0.0.1, which is allowed. FAKE_TOKEN is an obviously fake value, so a leak check can search for it.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from _fakes import MemoryKeyring, QuietServer
from meridian_desktop import data_source, hosted_client, local_client
from meridian_desktop.data_source import (
    NO_LOCAL_DATA_MESSAGE,
    NOT_SIGNED_IN_MESSAGE,
    SOURCE_FILE,
    SOURCE_HOSTED,
    SOURCE_SERVER,
    STATE_NO_LOCAL_DATA,
    STATE_NOT_SIGNED_IN,
    STATE_OK,
    STATE_READ_FAILED,
    DataSources,
    resolve,
)
from meridian_desktop.settings_store import Settings

LOCAL_URL = "http://127.0.0.1:7878"
HOSTED_URL = "https://hosted.test"
FAKE_TOKEN = "sk_meridian_fake-resolver-0004"

SERVER_ROWS = [{"id": "srv-1", "name": "From server"}]
FILE_ROWS = [{"id": "file-1", "name": "From file"}]
HOSTED_ROWS = [{"id": "hosted-1", "name": "From hosted"}]

QT_ROOTS = ("PySide6", "PySide2", "PyQt5", "PyQt6", "PyQt4", "shiboken6", "shiboken2")


class FakeSources:
    """Canned callables for DataSources. Each one appends its name to ``calls`` when it runs.

    An ``*_error`` argument is raised instead of returning, so a failed read can be tested.
    """

    def __init__(
        self,
        *,
        probe_state: str = local_client.HEALTH_NOT_RUNNING,
        token: str | None = None,
        server_error: BaseException | None = None,
        file_error: BaseException | None = None,
        hosted_error: BaseException | None = None,
    ) -> None:
        self.probe_state = probe_state
        self.token = token
        self.server_error = server_error
        self.file_error = file_error
        self.hosted_error = hosted_error
        self.calls: list[str] = []
        self.probed: list[str] = []
        self.read_paths: list[Path] = []
        self.hosted_args: list[tuple[str, str]] = []

    def sources(self) -> DataSources:
        return DataSources(
            probe=self._probe,
            read_server=self._read_server,
            read_file=self._read_file,
            get_token=self._get_token,
            list_hosted=self._list_hosted,
        )

    def _probe(self, url: str) -> str:
        self.calls.append("probe")
        self.probed.append(url)
        return self.probe_state

    def _read_server(self, url: str) -> list:
        self.calls.append("read_server")
        if self.server_error is not None:
            raise self.server_error
        return list(SERVER_ROWS)

    def _read_file(self, path: Path) -> list:
        self.calls.append("read_file")
        self.read_paths.append(path)
        if self.file_error is not None:
            raise self.file_error
        return list(FILE_ROWS)

    def _get_token(self) -> str | None:
        self.calls.append("get_token")
        return self.token

    def _list_hosted(self, base_url: str, token: str) -> list:
        self.calls.append("list_hosted")
        self.hosted_args.append((base_url, token))
        if self.hosted_error is not None:
            raise self.hosted_error
        return list(HOSTED_ROWS)


@pytest.fixture
def db_file(tmp_path) -> Path:
    """A file at a database path. The fake reader never opens it, so its bytes do not matter."""
    path = tmp_path / "meridian.db"
    path.write_bytes(b"")
    return path


def local_settings(db_path: Path | None) -> Settings:
    return Settings(
        mode="local",
        local_url=LOCAL_URL,
        hosted_url=HOSTED_URL,
        db_path=None if db_path is None else str(db_path),
    )


def hosted_settings(db_path: Path | None) -> Settings:
    return dataclasses.replace(local_settings(db_path), mode="hosted")


def test_local_mode_prefers_running_server_over_file(db_file):
    fakes = FakeSources(probe_state=local_client.HEALTH_RUNNING)

    result = resolve(local_settings(db_file), fakes.sources())

    assert result.source == SOURCE_SERVER
    assert result.state == STATE_OK
    # Only the server rows come back. The file rows are not added to them.
    assert result.rows == SERVER_ROWS
    assert fakes.probed == [LOCAL_URL]
    # The file exists and could be read, but the running server wins, so the file is never read.
    assert "read_file" not in fakes.calls


@pytest.mark.parametrize(
    "probe_state",
    [local_client.HEALTH_NOT_RUNNING, local_client.HEALTH_NOT_MERIDIAN, local_client.HEALTH_ERROR],
)
def test_local_mode_uses_file_when_the_server_is_not_running(db_file, probe_state):
    fakes = FakeSources(probe_state=probe_state)

    result = resolve(local_settings(db_file), fakes.sources())

    assert result.source == SOURCE_FILE
    assert result.state == STATE_OK
    assert result.rows == FILE_ROWS
    assert fakes.read_paths == [db_file]
    assert "read_server" not in fakes.calls


@pytest.mark.parametrize("db_choice", ["unset", "missing", "directory"])
def test_local_mode_without_server_or_file_has_no_local_data(tmp_path, db_choice):
    if db_choice == "unset":
        db_path = None
    elif db_choice == "missing":
        db_path = tmp_path / "absent.db"
    else:
        db_path = tmp_path / "a-folder"
        db_path.mkdir()
    fakes = FakeSources(probe_state=local_client.HEALTH_NOT_RUNNING)

    result = resolve(local_settings(db_path), fakes.sources())

    assert result.state == STATE_NO_LOCAL_DATA
    assert result.source is None
    assert result.rows == []
    assert result.message == NO_LOCAL_DATA_MESSAGE
    # Nothing is read, because there is no server and no file to read.
    assert fakes.calls == ["probe"]


def test_hosted_mode_without_token_is_not_signed_in_and_makes_no_network_call(db_file, monkeypatch):
    def no_socket(*args, **kwargs):
        raise AssertionError("the resolver opened a socket")

    monkeypatch.setattr(socket, "socket", no_socket)
    # The server is running and the file exists. Hosted mode must still ignore both.
    fakes = FakeSources(probe_state=local_client.HEALTH_RUNNING, token=None)

    result = resolve(hosted_settings(db_file), fakes.sources())

    assert result.state == STATE_NOT_SIGNED_IN
    assert result.source is None
    assert result.rows == []
    assert result.message == NOT_SIGNED_IN_MESSAGE
    assert fakes.calls == ["get_token"]


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_token_counts_as_not_signed_in(db_file, blank):
    fakes = FakeSources(token=blank)

    result = resolve(hosted_settings(db_file), fakes.sources())

    assert result.state == STATE_NOT_SIGNED_IN
    assert fakes.calls == ["get_token"]


def test_hosted_mode_with_token_returns_the_hosted_source(db_file):
    fakes = FakeSources(probe_state=local_client.HEALTH_RUNNING, token=FAKE_TOKEN)

    result = resolve(hosted_settings(db_file), fakes.sources())

    assert result.source == SOURCE_HOSTED
    assert result.state == STATE_OK
    assert result.rows == HOSTED_ROWS
    assert fakes.hosted_args == [(HOSTED_URL, FAKE_TOKEN)]
    # The local probe and the file are not used in hosted mode.
    assert fakes.calls == ["get_token", "list_hosted"]


def test_hosted_failure_leaves_no_token_in_the_result(db_file):
    fakes = FakeSources(token=FAKE_TOKEN, hosted_error=RuntimeError(f"rejected {FAKE_TOKEN}"))

    result = resolve(hosted_settings(db_file), fakes.sources())

    assert result.state == STATE_READ_FAILED
    assert result.source == SOURCE_HOSTED
    assert result.rows == []
    assert result.message == "Hosted Meridian projects could not be loaded (RuntimeError)."
    assert FAKE_TOKEN not in repr(result)
    assert FAKE_TOKEN not in result.message


def test_file_read_failure_names_the_class_only(db_file):
    fakes = FakeSources(file_error=OSError(f"cannot open {db_file}"))

    result = resolve(local_settings(db_file), fakes.sources())

    assert result.state == STATE_READ_FAILED
    assert result.source == SOURCE_FILE
    assert result.rows == []
    assert result.message == "The database file could not be read (OSError)."
    assert str(db_file) not in result.message


def test_running_server_that_cannot_be_read_does_not_fall_back_to_the_file(db_file):
    fakes = FakeSources(probe_state=local_client.HEALTH_RUNNING, server_error=TimeoutError("slow"))

    result = resolve(local_settings(db_file), fakes.sources())

    assert result.state == STATE_READ_FAILED
    assert result.source == SOURCE_SERVER
    assert result.rows == []
    assert result.message == "The local Meridian server could not be read (TimeoutError)."
    assert "read_file" not in fakes.calls


def test_unknown_mode_is_refused(db_file):
    settings = dataclasses.replace(local_settings(db_file), mode="cloud")

    with pytest.raises(ValueError):
        resolve(settings, FakeSources().sources())


@pytest.fixture
def health_url():
    """A loopback stub that answers GET /health with a Meridian health body."""
    body = json.dumps({"service": "meridian", "status": "ok"}).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server naming
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep test output quiet
            pass

    server = QuietServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_probe_of_a_meridian_server_resolves_to_server(db_file, health_url):
    sources = dataclasses.replace(FakeSources().sources(), probe=local_client.probe_health)
    settings = dataclasses.replace(local_settings(db_file), local_url=health_url)

    result = resolve(settings, sources)

    assert result.source == SOURCE_SERVER
    assert result.rows == SERVER_ROWS


def test_real_probe_on_a_closed_loopback_port_falls_back_to_the_file(db_file):
    # Bind a port, then close it. Nothing listens there, so the probe reports not_running.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    sources = dataclasses.replace(FakeSources().sources(), probe=local_client.probe_health)
    settings = dataclasses.replace(local_settings(db_file), local_url=f"http://127.0.0.1:{port}")

    result = resolve(settings, sources)

    assert result.source == SOURCE_FILE
    assert result.rows == FILE_ROWS


def test_resolver_module_imports_no_qt():
    tree = ast.parse(Path(data_source.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.append(node.module)
    assert [name for name in imported if name.split(".")[0] in QT_ROOTS] == []


def test_hosted_failure_gives_the_client_text_and_no_token(db_file):
    fakes = FakeSources(token=FAKE_TOKEN, hosted_error=hosted_client.HostedUnreachable())

    result = resolve(hosted_settings(db_file), fakes.sources())

    assert result.state == STATE_READ_FAILED
    assert result.source == SOURCE_HOSTED
    assert result.rows == []
    assert result.message == f"Hosted Meridian projects could not be loaded. {hosted_client.UNREACHABLE_TEXT}"
    assert FAKE_TOKEN not in result.message


def test_hosted_url_refusal_is_shown_in_plain_words(db_file):
    refusal = hosted_client.BadBaseUrl(hosted_client.BAD_SCHEME_TEXT)
    fakes = FakeSources(token=FAKE_TOKEN, hosted_error=refusal)

    result = resolve(hosted_settings(db_file), fakes.sources())

    assert result.state == STATE_READ_FAILED
    assert result.message == f"Hosted Meridian projects could not be loaded. {hosted_client.BAD_SCHEME_TEXT}"


def test_not_signed_in_message_says_to_sign_in_with_the_browser_in_settings():
    assert "Settings" in NOT_SIGNED_IN_MESSAGE
    assert "Sign in with your browser" in NOT_SIGNED_IN_MESSAGE
    assert "paste" not in NOT_SIGNED_IN_MESSAGE.lower()
    assert "Save token" not in NOT_SIGNED_IN_MESSAGE


def test_default_hosted_reader_is_the_hosted_client(db_file, monkeypatch):
    # The real wiring: default_sources reads the hosted client at call time. The client is replaced
    # here, so no request is made, and the call shows the URL and token it was given.
    from meridian_desktop.secret_store import KeyringSecretStore
    from meridian_desktop.token_store import set_token
    from meridian_desktop.ui.projects_tab import default_sources

    calls: list[tuple[str, str]] = []

    def fake_list(base_url, token, **_options):
        calls.append((base_url, token))
        return list(HOSTED_ROWS)

    monkeypatch.setattr(hosted_client, "list_projects", fake_list)
    backend = MemoryKeyring()
    secrets = KeyringSecretStore(backend=backend)
    set_token(FAKE_TOKEN, store=secrets)

    result = resolve(hosted_settings(db_file), default_sources(secrets))

    assert result.state == STATE_OK
    assert result.source == SOURCE_HOSTED
    assert result.rows == HOSTED_ROWS
    assert calls == [(HOSTED_URL, FAKE_TOKEN)]


def test_default_hosted_reader_without_a_token_sends_nothing(db_file, monkeypatch):
    from meridian_desktop.secret_store import KeyringSecretStore
    from meridian_desktop.ui.projects_tab import default_sources

    def no_call(*args, **kwargs):
        raise AssertionError("the hosted client was called with no token")

    monkeypatch.setattr(hosted_client, "list_projects", no_call)
    secrets = KeyringSecretStore(backend=MemoryKeyring())

    result = resolve(hosted_settings(db_file), default_sources(secrets))

    assert result.state == STATE_NOT_SIGNED_IN
    assert result.message == NOT_SIGNED_IN_MESSAGE
