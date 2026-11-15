"""Tests for the sessions client (meridian_desktop/sessions_client.py). Fake transports only: no socket, no keychain.

Each typed state is checked for both sources. The token must never appear in a result, and the local address and
the hosted URL are refused before any request when they are not allowed. Rows carry four fields only, so a session
summary placed in the server body never reaches a result.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from meridian_desktop import hosted_client, local_client, sessions_client as sc
from meridian_desktop.hosted_client import HostedError
from meridian_desktop.local_client import LocalClientError
from meridian_desktop.sessions_client import SessionRow

LOCAL = "http://127.0.0.1:7878"
HOSTED = "https://usemeridian.us"
PID = "5787cc92-ba7d-4788-b17c-28ab7938b839"
SENTINEL_TOKEN = "sk_meridian_SENTINELTOKEN0123456789"
SECRET_SUMMARY = "SECRET-SUMMARY-TEXT-do-not-show"


def session(**overrides):
    row = {
        "id": "sess-1",
        "project_id": PID,
        "name": "claude-code-auth",
        "human_id": "adam",
        "status": "active",
        "last_seen": "2026-10-22 12:00:00",
        "created_at": "2026-10-22 11:00:00",
        "session_summary": {"notes": SECRET_SUMMARY},
        "agent_framework": "claude_code",
        "client_type": "claude-code",
    }
    row.update(overrides)
    return row


def body_of(rows):
    return json.dumps(rows).encode("utf-8")


class FakeLocal:
    """A local transport. It records each call and returns a reply, or raises the error it was given."""

    def __init__(self, status=200, body=b"[]", error=None):
        self.status = status
        self.body = body
        self.error = error
        self.calls = []

    def __call__(self, base_url, path, timeout):
        self.calls.append((base_url, path, timeout))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(status=self.status, body=self.body)


class FakeHosted:
    """A hosted transport. It records each request, and returns a reply or raises the error it was given."""

    def __init__(self, status=200, body=b"[]", error=None):
        self.status = status
        self.body = body
        self.error = error
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return hosted_client.Reply(self.status, self.body)


def _texts(result):
    """Every string a result holds, joined. A token or a body fragment must not be among them."""
    parts = [result.state, result.message, result.source]
    for row in result.rows:
        parts.extend([row.name, row.status, row.last_seen, row.client])
    return "\n".join(parts)


# ----------------------------------------------------------------- local source


def test_local_lists_rows_with_four_fields_only():
    fake = FakeLocal(body=body_of([session()]))
    result = sc.local_sessions(LOCAL, PID, transport=fake)
    assert result.state == sc.SESSIONS
    assert result.source == sc.SOURCE_LOCAL
    assert result.rows == (
        SessionRow(name="claude-code-auth", status="active", last_seen="2026-10-22 12:00:00", client="claude-code"),
    )
    assert SECRET_SUMMARY not in repr(result)


def test_local_empty_list_is_sessions_with_no_rows():
    result = sc.local_sessions(LOCAL, PID, transport=FakeLocal(body=b"[]"))
    assert result.state == sc.SESSIONS
    assert result.rows == ()


def test_local_request_names_the_project_and_active_only_and_sends_no_credentials():
    fake = FakeLocal()
    sc.local_sessions(LOCAL, PID, transport=fake, timeout=3.0)
    base, path, timeout = fake.calls[0]
    assert base == LOCAL
    assert path == f"/projects/{PID}/sessions?active_only=true"
    assert timeout == 3.0


def test_client_falls_back_to_agent_framework_when_client_type_is_empty():
    result = sc.local_sessions(LOCAL, PID, transport=FakeLocal(body=body_of([session(client_type=None)])))
    assert result.rows[0].client == "claude_code"


@pytest.mark.parametrize(
    ("status", "state"),
    [(401, sc.UNAUTHORISED), (403, sc.UNAUTHORISED), (404, sc.MISSING_ROUTE), (500, sc.ERROR), (302, sc.ERROR)],
)
def test_local_status_codes_map_to_typed_states(status, state):
    result = sc.local_sessions(LOCAL, PID, transport=FakeLocal(status=status))
    assert result.state == state
    assert result.rows == ()
    assert result.message


def test_local_404_message_covers_a_missing_route_and_an_unknown_project():
    result = sc.local_sessions(LOCAL, PID, transport=FakeLocal(status=404))
    assert "no sessions route" in result.message
    assert "not on this server" in result.message


def test_local_http_500_message_names_only_the_status():
    result = sc.local_sessions(LOCAL, PID, transport=FakeLocal(status=500, body=b"secret-body-text"))
    assert result.message == "The server answered with HTTP 500, which the desktop does not expect."
    assert "secret-body-text" not in repr(result)


@pytest.mark.parametrize("body", [b"{}", b"not json", b"[1, 2]", b'"text"', b"\xff\xfe"])
def test_local_body_that_is_not_a_list_of_objects_is_an_error(body):
    result = sc.local_sessions(LOCAL, PID, transport=FakeLocal(body=body))
    assert result.state == sc.ERROR
    assert result.message == sc.BAD_RESPONSE_MESSAGE


def test_local_not_running_is_its_own_state():
    result = sc.local_sessions(LOCAL, PID, transport=FakeLocal(error=LocalClientError("not_running", "x")))
    assert result.state == sc.NOT_RUNNING
    assert "not running on this computer" in result.message


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("timeout", sc.LOCAL_TIMEOUT_MESSAGE),
        ("unreachable", sc.LOCAL_UNREACHABLE_MESSAGE),
        ("too_large", sc.TOO_LARGE_MESSAGE),
        ("bad_url", sc.BAD_LOCAL_URL_MESSAGE),
        ("something_unlisted", sc.UNCHECKED_MESSAGE),
    ],
)
def test_local_client_errors_map_to_error_with_fixed_text(code, message):
    result = sc.local_sessions(LOCAL, PID, transport=FakeLocal(error=LocalClientError(code, "raw text")))
    assert result.state == sc.ERROR
    assert result.message == message
    assert "raw text" not in repr(result)


def test_local_unexpected_transport_exception_is_not_shown():
    result = sc.local_sessions(LOCAL, PID, transport=FakeLocal(error=RuntimeError("boom with a secret")))
    assert result.state == sc.ERROR
    assert result.message == sc.UNCHECKED_MESSAGE
    assert "boom" not in repr(result)


@pytest.mark.parametrize(
    "base",
    [
        "http://192.168.1.20:7878",
        "https://127.0.0.1:7878",
        "http://127.0.0.1.example.com:7878",
        "http://user:pw@127.0.0.1:7878",
        "",
        None,
    ],
)
def test_local_non_loopback_or_bad_url_is_refused_before_any_request(base):
    fake = FakeLocal()
    result = sc.local_sessions(base, PID, transport=fake)
    assert result.state == sc.ERROR
    assert result.message in (sc.NOT_LOOPBACK_MESSAGE,)
    assert fake.calls == []


def test_local_default_transport_refuses_a_non_loopback_address_without_opening_a_socket(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("no request may be made")

    monkeypatch.setattr(local_client, "_get", fail)
    result = sc.local_sessions("http://10.0.0.5:7878", PID)
    assert result.state == sc.ERROR


@pytest.mark.parametrize("project_id", [None, "", "   "])
def test_no_project_gives_no_project_and_sends_nothing(project_id):
    fake = FakeLocal()
    result = sc.local_sessions(LOCAL, project_id, transport=fake)
    assert result.state == sc.NO_PROJECT
    assert result.message == sc.NO_PROJECT_MESSAGE
    assert fake.calls == []


@pytest.mark.parametrize("project_id", ["../admin", "not-a-uuid", f"{PID}/extra", PID + "?x=1"])
def test_project_id_that_is_not_a_uuid_is_never_put_in_a_path(project_id):
    fake = FakeLocal()
    result = sc.local_sessions(LOCAL, project_id, transport=fake)
    assert result.state == sc.ERROR
    assert result.message == sc.UNKNOWN_PROJECT_MESSAGE
    assert fake.calls == []


def test_project_id_is_trimmed_before_it_is_used():
    fake = FakeLocal()
    sc.local_sessions(LOCAL, f"  {PID}  ", transport=fake)
    assert fake.calls[0][1] == f"/projects/{PID}/sessions?active_only=true"


def test_rows_are_capped_at_the_maximum():
    rows = [session(name=f"s{i}") for i in range(sc.MAX_SESSIONS + 50)]
    result = sc.local_sessions(LOCAL, PID, transport=FakeLocal(body=body_of(rows)))
    assert len(result.rows) == sc.MAX_SESSIONS


def test_text_is_cleaned_and_capped():
    long_name = "x" * 300
    result = sc.local_sessions(
        LOCAL, PID, transport=FakeLocal(body=body_of([session(name="bad\x00name   with   gaps"), session(name=long_name)]))
    )
    assert result.rows[0].name == "bad name with gaps"
    assert result.rows[1].name.endswith("...")
    assert len(result.rows[1].name) == sc.MAX_TEXT_CHARS


def test_non_string_fields_read_as_empty_text():
    result = sc.local_sessions(LOCAL, PID, transport=FakeLocal(body=body_of([session(name=None, status=5)])))
    assert result.rows[0].name == ""
    assert result.rows[0].status == ""


# ----------------------------------------------------------------- hosted source


def test_hosted_sends_the_bearer_token_to_the_https_host_only():
    fake = FakeHosted(body=body_of([session()]))
    result = sc.hosted_sessions(HOSTED, PID, SENTINEL_TOKEN, transport=fake)
    assert result.state == sc.SESSIONS
    assert result.source == sc.SOURCE_HOSTED
    request, timeout = fake.calls[0]
    assert request.full_url == f"{HOSTED}/projects/{PID}/sessions?active_only=true"
    assert request.get_header("Authorization") == f"Bearer {SENTINEL_TOKEN}"
    assert request.get_method() == "GET"
    assert timeout == sc.SESSIONS_TIMEOUT


def test_hosted_token_never_appears_in_a_result():
    fake = FakeHosted(body=body_of([session()]))
    result = sc.hosted_sessions(HOSTED, PID, SENTINEL_TOKEN, transport=fake)
    assert SENTINEL_TOKEN not in repr(result)
    assert SENTINEL_TOKEN not in _texts(result)


@pytest.mark.parametrize("base", ["http://usemeridian.us", "http://127.0.0.1:8000", "http://localhost:8000"])
def test_hosted_refuses_plain_http_even_on_loopback(base):
    fake = FakeHosted()
    result = sc.hosted_sessions(base, PID, SENTINEL_TOKEN, transport=fake)
    assert result.state == sc.ERROR
    assert result.message == sc.BAD_HOSTED_URL_MESSAGE
    assert fake.calls == []


@pytest.mark.parametrize("base", ["https://user:pw@usemeridian.us", "https://usemeridian.us/?x=1", "", None, "ftp://x"])
def test_hosted_refuses_userinfo_query_empty_and_other_schemes(base):
    fake = FakeHosted()
    result = sc.hosted_sessions(base, PID, SENTINEL_TOKEN, transport=fake)
    assert result.state == sc.ERROR
    assert fake.calls == []


@pytest.mark.parametrize("token", [None, "", "   "])
def test_hosted_without_a_token_is_unauthorised_and_sends_nothing(token):
    fake = FakeHosted()
    result = sc.hosted_sessions(HOSTED, PID, token, transport=fake)
    assert result.state == sc.UNAUTHORISED
    assert result.message == sc.NO_TOKEN_MESSAGE
    assert fake.calls == []


def test_hosted_token_with_a_space_is_refused_and_not_echoed():
    fake = FakeHosted()
    bad = "sk_meridian has-a-space"
    result = sc.hosted_sessions(HOSTED, PID, bad, transport=fake)
    assert result.state == sc.UNAUTHORISED
    assert result.message == sc.BAD_TOKEN_MESSAGE
    assert fake.calls == []
    assert bad not in repr(result)


@pytest.mark.parametrize(
    ("status", "state", "message"),
    [
        (401, sc.UNAUTHORISED, sc.REJECTED_MESSAGE),
        (403, sc.UNAUTHORISED, sc.FORBIDDEN_MESSAGE),
        (404, sc.MISSING_ROUTE, sc.MISSING_ROUTE_MESSAGE),
        (500, sc.ERROR, "The server answered with HTTP 500, which the desktop does not expect."),
        (301, sc.ERROR, sc.REDIRECT_MESSAGE),
    ],
)
def test_hosted_status_codes_map_to_typed_states(status, state, message):
    result = sc.hosted_sessions(HOSTED, PID, SENTINEL_TOKEN, transport=FakeHosted(status=status))
    assert result.state == state
    assert result.message == message
    assert SENTINEL_TOKEN not in repr(result)


def test_hosted_bad_json_is_an_error():
    result = sc.hosted_sessions(HOSTED, PID, SENTINEL_TOKEN, transport=FakeHosted(body=b"<html>"))
    assert result.state == sc.ERROR
    assert result.message == sc.BAD_RESPONSE_MESSAGE


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (OSError(f"connect failed for {SENTINEL_TOKEN}"), sc.HOSTED_UNREACHABLE_MESSAGE),
        (TimeoutError(f"slow {SENTINEL_TOKEN}"), sc.HOSTED_TIMEOUT_MESSAGE),
        (HostedError("too_large", "far too big"), sc.TOO_LARGE_MESSAGE),
        (HostedError("redirect_refused", "moved"), sc.REDIRECT_MESSAGE),
        (RuntimeError(f"surprise {SENTINEL_TOKEN}"), sc.HOSTED_UNCHECKED_MESSAGE),
    ],
)
def test_hosted_transport_failures_never_carry_the_token(error, message):
    result = sc.hosted_sessions(HOSTED, PID, SENTINEL_TOKEN, transport=FakeHosted(error=error))
    assert result.state == sc.ERROR
    assert result.message == message
    assert SENTINEL_TOKEN not in repr(result)


def test_hosted_default_transport_failure_hides_the_token(monkeypatch):
    """Real path through hosted_client's send: a socket error that carries the token text is not shown."""

    def broken(request, timeout):
        raise OSError(f"no route with {SENTINEL_TOKEN}")

    monkeypatch.setattr(hosted_client, "_urllib_transport", broken)
    result = sc.hosted_sessions(HOSTED, PID, SENTINEL_TOKEN)
    assert result.state == sc.ERROR
    assert result.message == sc.HOSTED_UNREACHABLE_MESSAGE
    assert SENTINEL_TOKEN not in repr(result)


def test_hosted_summary_and_notes_never_reach_a_result():
    body = body_of([session(session_summary={"notes": SECRET_SUMMARY}, name="visible")])
    result = sc.hosted_sessions(HOSTED, PID, SENTINEL_TOKEN, transport=FakeHosted(body=body))
    assert result.rows[0].name == "visible"
    assert SECRET_SUMMARY not in repr(result)


# ----------------------------------------------------------------- last seen age


NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("last_seen", "expected"),
    [
        ("2026-10-22 11:59:30", "just now"),
        ("2026-10-22T12:00:30+00:00", "just now"),
        ("2026-10-22 13:00:00", "just now"),
        ("2026-10-22 11:55:00", "5 min ago"),
        ("2026-10-22T09:00:00+00:00", "3 h ago"),
        ("2026-10-20T12:00:00Z", "2 d ago"),
        ("2026-10-22T11:00:00.123456+00:00", "59 min ago"),
        ("not a time", "unknown"),
        ("", "unknown"),
    ],
)
def test_last_seen_age_reads_sqlite_and_iso_timestamps(last_seen, expected):
    assert sc.last_seen_age(last_seen, NOW) == expected


def test_naive_timestamp_is_read_as_utc():
    value = sc.parse_timestamp("2026-10-22 12:00:00")
    assert value is not None
    assert value.tzinfo == timezone.utc
    assert value == datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)


def test_last_seen_age_accepts_a_naive_now():
    naive_now = datetime(2026, 10, 22, 12, 0, 0)
    assert sc.last_seen_age("2026-10-22 11:55:00", naive_now) == "5 min ago"


def test_parse_timestamp_rejects_empty_and_non_text():
    assert sc.parse_timestamp("") is None
    assert sc.parse_timestamp(None) is None


def test_last_seen_age_is_relative_to_the_time_passed_in():
    seen = (NOW - timedelta(hours=5)).isoformat()
    assert sc.last_seen_age(seen, NOW) == "5 h ago"
