"""Zotero client: install and process checks, the local API probe, library search and the start action.

No test reaches the real Zotero, the real Program Files, the real %LOCALAPPDATA% or the real
network. Requests go through a fake transport. The only real HTTP in this file goes to a loopback
stub server on a free port, to check the default transport's limits. Paths come from tmp_path.
"""

import datetime as dt
import json
import socket
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from _fakes import QuietServer
from meridian_desktop import local_client, zotero_client
from meridian_desktop.zotero_client import (
    API_DISABLED,
    API_ERROR,
    API_NOT_RUNNING,
    API_ON,
    ITEMS_PATH,
    ZOTERO_BASE_URL,
    Reply,
    ZoteroError,
    ZoteroItem,
    ZoteroStatus,
    candidate_exes,
    command_line,
    default_log_path,
    find_zotero_exe,
    http_get,
    is_zotero_running,
    item_from_entry,
    probe_api,
    search_items,
    start_zotero,
    status,
)

NOW = dt.datetime(2026, 10, 19, 12, 0, 0, tzinfo=dt.timezone.utc)
ZOTERO_EXE = r"C:\Program Files\Zotero\zotero.exe"
ITEMS_JSON = [
    {
        "key": "ABCD1234",
        "version": 7,
        "library": {"type": "user", "id": 0},
        "data": {
            "key": "ABCD1234",
            "itemType": "journalArticle",
            "title": "Deep   Learning\nfor maps",
            "creators": [
                {"creatorType": "author", "firstName": "Yann", "lastName": "LeCun"},
                {"creatorType": "author", "firstName": "Yoshua", "lastName": "Bengio"},
            ],
            "date": "2015-05-28",
        },
    },
    {
        "key": "WXYZ9876",
        "data": {
            "itemType": "book",
            "title": "Corporate report",
            "creators": [{"creatorType": "editor", "name": "Ministry of Works"}],
            "date": "Spring 1999",
        },
    },
]


class FakeTransport:
    """Records each request and answers with a fixed reply, or raises a fixed error."""

    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.requests = []

    def __call__(self, base_url, path, headers, timeout):
        self.requests.append({"base_url": base_url, "path": path, "headers": dict(headers), "timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.reply


def _json_reply(payload, status=200):
    return Reply(status, json.dumps(payload).encode("utf-8"))


def _entry_path(tmp_path, root_name):
    """Create a fake zotero.exe under ``root_name`` in tmp_path and return the path string."""
    path = tmp_path / root_name / "Zotero" / "zotero.exe"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return str(path)


# ----------------------------------------------------------------- install


def test_candidates_use_the_usual_install_folders_in_order():
    env = {
        "ProgramFiles": r"D:\PF",
        "ProgramFiles(x86)": r"D:\PFX",
        "ProgramW6432": r"D:\PF",
        "LOCALAPPDATA": r"D:\Local",
    }
    assert candidate_exes(env) == [
        str(Path(r"D:\PF") / "Zotero" / "zotero.exe"),
        str(Path(r"D:\PFX") / "Zotero" / "zotero.exe"),
        str(Path(r"D:\Local") / "Programs" / "Zotero" / "zotero.exe"),
    ]


def test_candidates_fall_back_to_the_default_program_folders_when_env_is_empty():
    assert candidate_exes({}) == [
        str(Path(r"C:\Program Files") / "Zotero" / "zotero.exe"),
        str(Path(r"C:\Program Files (x86)") / "Zotero" / "zotero.exe"),
    ]


def test_find_returns_the_first_usual_path_that_exists(tmp_path):
    pf86 = _entry_path(tmp_path, "PFX")
    env = {"ProgramFiles": str(tmp_path / "PF-missing"), "ProgramFiles(x86)": str(tmp_path / "PFX")}
    assert find_zotero_exe(env) == pf86


def test_find_returns_none_when_no_usual_folder_has_zotero(tmp_path):
    env = {"ProgramFiles": str(tmp_path / "PF"), "LOCALAPPDATA": str(tmp_path / "Local")}
    assert find_zotero_exe(env) is None


def test_find_checks_each_candidate_in_order_and_stops_at_the_first_hit(tmp_path):
    checked = []

    def fake_is_file(path):
        checked.append(path)
        return False

    env = {"ProgramFiles": str(tmp_path)}
    assert find_zotero_exe(env, is_file=fake_is_file) is None
    assert checked == candidate_exes(env)


def test_command_line_quotes_a_path_with_spaces_and_passes_no_arguments():
    assert command_line(ZOTERO_EXE) == '"C:\\Program Files\\Zotero\\zotero.exe"'
    assert command_line(r"C:\Zotero\zotero.exe") == r"C:\Zotero\zotero.exe"


# ----------------------------------------------------------------- process


def test_running_when_tasklist_lists_zotero():
    output = '"zotero.exe","1234","Console","1","250,000 K"\n'
    assert is_zotero_running(lambda: output) is True


def test_not_running_when_tasklist_says_no_tasks_match():
    output = "INFO: No tasks are running which match the specified criteria.\n"
    assert is_zotero_running(lambda: output) is False


def test_a_similar_process_name_is_not_zotero():
    output = '"zotero-helper.exe","99","Console","1","10,000 K"\n'
    assert is_zotero_running(lambda: output) is False


def test_unreadable_process_list_is_unknown_not_an_error():
    def broken():
        raise OSError("tasklist missing")

    assert is_zotero_running(broken) is None


# ----------------------------------------------------------------- local API probe


def test_probe_reports_on_for_a_json_list_answer():
    transport = FakeTransport(reply=_json_reply([]))
    check = probe_api(transport=transport)
    assert check.state == API_ON
    request = transport.requests[0]
    assert request["base_url"] == ZOTERO_BASE_URL
    assert request["path"].startswith(f"{ITEMS_PATH}?")
    assert request["headers"]["Zotero-API-Version"] == "3"
    assert request["timeout"] <= 2.0


def test_probe_reports_disabled_on_403_and_says_where_to_turn_it_on():
    check = probe_api(transport=FakeTransport(reply=Reply(403, b"Forbidden")))
    assert check.state == API_DISABLED
    assert "Allow other applications on this computer to communicate with Zotero" in check.message
    assert "Advanced" in check.message


def test_probe_reports_not_running_when_nothing_listens():
    error = ZoteroError("not_running", "not running text")
    check = probe_api(transport=FakeTransport(error=error))
    assert check.state == API_NOT_RUNNING
    assert "not running" in check.message.lower()


def test_probe_reports_error_on_timeout_and_other_status_codes():
    timeout = probe_api(transport=FakeTransport(error=ZoteroError("timeout", "Zotero did not answer on 127.0.0.1:23119 in time.")))
    assert timeout.state == API_ERROR
    assert "in time" in timeout.message

    server_error = probe_api(transport=FakeTransport(reply=Reply(500, b"oops")))
    assert server_error.state == API_ERROR
    assert "HTTP 500" in server_error.message


def test_probe_rejects_a_200_that_is_not_zotero():
    check = probe_api(transport=FakeTransport(reply=Reply(200, b'{"not": "a list"}')))
    assert check.state == API_ERROR
    assert "not like Zotero" in check.message


def test_probe_never_raises_on_an_unexpected_transport_failure():
    check = probe_api(transport=FakeTransport(error=RuntimeError("boom")))
    assert check.state == API_ERROR
    assert "boom" not in check.message


# ----------------------------------------------------------------- status


def test_status_combines_install_process_and_api(tmp_path):
    exe = _entry_path(tmp_path, "PF")
    result = status(
        env={"ProgramFiles": str(tmp_path / "PF")},
        process_list=lambda: '"zotero.exe","1","Console","1","1 K"\n',
        transport=FakeTransport(reply=_json_reply([])),
    )
    assert result.installed is True
    assert result.exe == exe
    assert result.process_running is True
    assert result.api.state == API_ON


def test_status_reports_missing_install_and_unknown_process_without_raising(tmp_path):
    def broken():
        raise OSError("no tasklist")

    result = status(
        env={"ProgramFiles": str(tmp_path / "PF-missing")},
        process_list=broken,
        transport=FakeTransport(error=ZoteroError("not_running", "not running")),
    )
    assert result.installed is False
    assert result.exe is None
    assert result.process_running is None
    assert result.api.state == API_NOT_RUNNING


def test_status_never_raises_even_when_the_transport_breaks(tmp_path):
    result = status(
        env={"ProgramFiles": str(tmp_path)},
        process_list=lambda: "",
        transport=FakeTransport(error=RuntimeError("socket exploded")),
    )
    assert isinstance(result, ZoteroStatus)
    assert result.api.state == API_ERROR
    assert "socket exploded" not in result.api.message


# ----------------------------------------------------------------- search


def test_search_sends_the_encoded_quick_search_with_the_version_header():
    transport = FakeTransport(reply=_json_reply([]))
    search_items("smith & jones", transport=transport)
    request = transport.requests[0]
    assert request["path"] == f"{ITEMS_PATH}?q=smith%20%26%20jones&format=json&limit=25"
    assert request["headers"]["Zotero-API-Version"] == "3"
    assert request["base_url"] == ZOTERO_BASE_URL


def test_search_returns_title_authors_year_and_key_from_v3_items():
    items = search_items("learning", transport=FakeTransport(reply=_json_reply(ITEMS_JSON)))
    assert items == [
        ZoteroItem(
            key="ABCD1234",
            title="Deep Learning for maps",
            authors="LeCun, Yann; Bengio, Yoshua",
            year="2015",
            item_type="journalArticle",
        ),
        ZoteroItem(
            key="WXYZ9876",
            title="Corporate report",
            authors="Ministry of Works",
            year="1999",
            item_type="book",
        ),
    ]


def test_item_without_a_year_or_nested_data_is_still_read():
    item = item_from_entry({"key": "FLAT0001", "title": "Flat entry", "date": "n.d.", "itemType": "note"})
    assert item == ZoteroItem(key="FLAT0001", title="Flat entry", authors="", year="", item_type="note")


def test_blank_query_raises_before_any_request_is_sent():
    transport = FakeTransport(reply=_json_reply([]))
    with pytest.raises(ZoteroError) as info:
        search_items("   ", transport=transport)
    assert info.value.code == "blank_query"
    assert transport.requests == []


def test_search_maps_403_to_the_disabled_message():
    with pytest.raises(ZoteroError) as info:
        search_items("x", transport=FakeTransport(reply=Reply(403, b"no")))
    assert info.value.code == "api_disabled"
    assert "Allow other applications" in info.value.message


def test_search_maps_non_json_and_non_list_answers_to_bad_response():
    with pytest.raises(ZoteroError) as info:
        search_items("x", transport=FakeTransport(reply=Reply(200, b"<html>")))
    assert info.value.code == "bad_response"
    with pytest.raises(ZoteroError) as info:
        search_items("x", transport=FakeTransport(reply=Reply(200, b'{"items": []}')))
    assert info.value.code == "bad_response"


def test_search_passes_through_a_refused_connection_as_not_running():
    with pytest.raises(ZoteroError) as info:
        search_items("x", transport=FakeTransport(error=ZoteroError("not_running", "not running")))
    assert info.value.code == "not_running"


def test_search_truncates_an_overlong_query_before_sending():
    transport = FakeTransport(reply=_json_reply([]))
    search_items("a" * 500, transport=transport)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(transport.requests[0]["path"]).query)["q"][0]
    assert query == "a" * zotero_client.MAX_QUERY_CHARS


# ----------------------------------------------------------------- start


def test_start_launches_only_the_exe_with_no_shell_and_logs_the_attempt(tmp_path):
    launched = []

    def launcher(argv, **kwargs):
        launched.append((argv, kwargs))
        return object()

    log = tmp_path / "logs" / "zotero-launch.log"
    result = start_zotero(ZOTERO_EXE, launcher=launcher, log_path=log, now=lambda: NOW)

    argv, kwargs = launched[0]
    assert argv == [ZOTERO_EXE]
    assert kwargs["cwd"] == str(Path(ZOTERO_EXE).parent)
    assert "shell" not in kwargs
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert "zotero.exe" in result
    text = log.read_text(encoding="utf-8")
    assert text.count("\n") == 1
    assert "2026-10-19T12:00:00+00:00" in text
    assert "-> launched" in text


def test_start_failure_is_a_zotero_error_and_is_logged_by_class_name(tmp_path):
    def launcher(argv, **kwargs):
        raise FileNotFoundError("C:\\secret\\path was not found")

    log = tmp_path / "zotero-launch.log"
    with pytest.raises(ZoteroError) as info:
        start_zotero(ZOTERO_EXE, launcher=launcher, log_path=log, now=lambda: NOW)
    assert info.value.code == "start_failed"
    assert "FileNotFoundError" in info.value.message
    text = log.read_text(encoding="utf-8")
    assert "-> failed (FileNotFoundError)" in text
    assert "secret" not in text


def test_a_log_that_cannot_be_written_does_not_stop_the_start(tmp_path):
    blocker = tmp_path / "file-not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    launched = []
    result = start_zotero(
        ZOTERO_EXE,
        launcher=lambda argv, **kwargs: launched.append(argv),
        log_path=blocker / "zotero-launch.log",
        now=lambda: NOW,
    )
    assert launched == [[ZOTERO_EXE]]
    assert "zotero.exe" in result


def test_default_log_path_sits_beside_the_settings_file(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path)}
    assert default_log_path(env) == tmp_path / "meridian-desktop" / "zotero-launch.log"


# ----------------------------------------------------------------- default transport


class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output quiet
        pass

    def _send(self, status, body, headers=()):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - http.server naming
        if self.path.startswith("/echo"):
            seen = self.headers.get("Zotero-API-Version", "")
            self._send(200, json.dumps([{"key": seen}]).encode("utf-8"))
        elif self.path.startswith("/forbidden"):
            self._send(403, b"off")
        elif self.path.startswith("/redirect"):
            self._send(302, b"", headers=[("Location", "/echo")])
        elif self.path.startswith("/big"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                self.wfile.write(b"x" * (local_client.MAX_RESPONSE_BYTES + 10))
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self._send(404, b"")


@pytest.fixture
def stub_base():
    server = QuietServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_default_transport_sends_the_header_and_returns_the_reply(stub_base):
    reply = http_get(stub_base, "/echo", {"Zotero-API-Version": "3"}, 2.0)
    assert reply.status == 200
    assert json.loads(reply.body) == [{"key": "3"}]


def test_default_transport_returns_403_as_a_reply(stub_base):
    reply = http_get(stub_base, "/forbidden", {}, 2.0)
    assert reply.status == 403


def test_default_transport_does_not_follow_a_redirect(stub_base):
    reply = http_get(stub_base, "/redirect", {}, 2.0)
    assert reply.status == 302


def test_default_transport_refuses_a_body_over_the_cap(stub_base):
    with pytest.raises(ZoteroError) as info:
        http_get(stub_base, "/big", {}, 5.0)
    assert info.value.code == "too_large"


def test_default_transport_maps_a_refused_port_to_not_running():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    with pytest.raises(ZoteroError) as info:
        http_get(f"http://127.0.0.1:{port}", "/x", {}, 1.0)
    assert info.value.code == "not_running"


def test_default_transport_refuses_a_non_loopback_host_before_any_request():
    with pytest.raises(ZoteroError) as info:
        http_get("http://example.com", "/x", {}, 1.0)
    assert info.value.code == "not_loopback"
