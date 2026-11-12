"""Tests for meridian_inventree.read_tools. Every request goes to a fake transport."""

from __future__ import annotations

import json
import socket
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from meridian_inventree import read_tools
from meridian_inventree.client import MAX_PAGE_SIZE, InvenTreeClient, InvenTreeError

BASE = "https://inventree.example.com"
TOKEN = "tok_read_tools_fixture"


@dataclass
class Call:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None


Responder = Callable[[Call], tuple[int, bytes]]


class FakeTransport:
    """Records every request and answers from a responder."""

    def __init__(self, responder: Responder) -> None:
        self._responder = responder
        self.calls: list[Call] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, bytes]:
        call = Call(method, url, dict(headers), body)
        self.calls.append(call)
        return self._responder(call)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backstop: any real socket or urllib open in this module fails the test."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("read tool tests must not open network connections")

    monkeypatch.setattr(urllib.request, "build_opener", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def reply(status: int, payload: Any) -> tuple[int, bytes]:
    return status, json.dumps(payload).encode("utf-8")


def fixed(payload: Any, status: int = 200) -> FakeTransport:
    return FakeTransport(lambda _call: reply(status, payload))


def paged(total: int, server_cap: int | None = None) -> FakeTransport:
    """Serve `total` rows with limit/offset paging, optionally capping the page size."""

    def respond(call: Call) -> tuple[int, bytes]:
        query = _query(call.url)
        requested = int(query.get("limit", ["25"])[0])
        offset = int(query.get("offset", ["0"])[0])
        page = requested if server_cap is None else min(requested, server_cap)
        end = min(total, offset + page)
        rows = [{"pk": index} for index in range(offset + 1, end + 1)]
        link = f"{BASE}/api/part/?limit={page}&offset={end}" if end < total else None
        return reply(200, {"count": total, "next": link, "previous": None, "results": rows})

    return FakeTransport(respond)


def _answer_by_path(call: Call) -> tuple[int, bytes]:
    """Answer every read tool with a shape it accepts, based on the path alone."""
    path = urllib.parse.urlsplit(call.url).path
    if path.endswith("/user/roles/"):
        return reply(200, [{"name": "viewer"}])
    if path.rstrip("/").rsplit("/", 1)[-1].isdigit():
        return reply(200, {"pk": 42})
    return reply(200, {"count": 0, "next": None, "results": []})


def _client(transport: FakeTransport) -> InvenTreeClient:
    return InvenTreeClient(BASE, TOKEN, transport=transport)


def _query(url: str) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


READ_CALLS: dict[str, Callable[[InvenTreeClient], Any]] = {
    "search_parts": lambda c: read_tools.search_parts(c, query="resistor"),
    "get_part": lambda c: read_tools.get_part(c, 42),
    "list_categories": lambda c: read_tools.list_categories(c),
    "search_stock": lambda c: read_tools.search_stock(c, query="bolt"),
    "get_stock_item": lambda c: read_tools.get_stock_item(c, 42),
    "list_locations": lambda c: read_tools.list_locations(c),
    "roles": lambda c: read_tools.roles(c),
}


# ---------------------------------------------------------------- query parameters


def test_search_parts_sends_search_category_and_limit_as_query_params() -> None:
    transport = fixed({"count": 1, "next": None, "results": [{"pk": 9, "name": "Resistor 10k"}]})

    result = read_tools.search_parts(_client(transport), query="resistor", category=12, limit=20)

    call = transport.calls[0]
    assert call.method == "GET"
    assert call.url == f"{BASE}/api/part/?search=resistor&category=12&limit=20"
    assert call.headers["Authorization"] == f"Token {TOKEN}"
    assert result == {
        "items": [{"pk": 9, "name": "Resistor 10k"}],
        "returned": 1,
        "truncated": False,
        "limit": 20,
        "notes": [],
    }


def test_search_parts_defaults_send_only_the_default_limit() -> None:
    transport = fixed({"count": 0, "next": None, "results": []})

    result = read_tools.search_parts(_client(transport))

    assert transport.calls[0].url == f"{BASE}/api/part/?limit=50"
    assert result["items"] == []
    assert result["truncated"] is False
    assert result["limit"] == 50


def test_search_stock_sends_search_location_cascade_and_limit() -> None:
    transport = fixed({"count": 0, "next": None, "results": []})

    read_tools.search_stock(_client(transport), query="M3 bolt", location=7, cascade=False, limit=25)

    call = transport.calls[0]
    assert call.method == "GET"
    assert call.url.startswith(f"{BASE}/api/stock/?")
    assert _query(call.url) == {"search": ["M3 bolt"], "location": ["7"], "cascade": ["false"], "limit": ["25"]}


def test_search_stock_cascades_by_default_and_omits_empty_filters() -> None:
    transport = fixed({"count": 0, "next": None, "results": []})

    read_tools.search_stock(_client(transport))

    assert _query(transport.calls[0].url) == {"cascade": ["true"], "limit": ["50"]}


# The client asks for at most MAX_PAGE_SIZE rows per request and follows `next` links,
# so a 200-row limit goes out as a MAX_PAGE_SIZE page size on the first request.


def test_list_categories_sends_parent_and_defaults_to_200() -> None:
    transport = fixed({"count": 0, "next": None, "results": []})

    result = read_tools.list_categories(_client(transport), parent=3)

    assert transport.calls[0].method == "GET"
    assert transport.calls[0].url == f"{BASE}/api/part/category/?parent=3&limit={MAX_PAGE_SIZE}"
    assert result["limit"] == 200


def test_list_categories_without_parent_sends_only_limit() -> None:
    transport = fixed({"count": 0, "next": None, "results": []})

    read_tools.list_categories(_client(transport))

    assert transport.calls[0].url == f"{BASE}/api/part/category/?limit={MAX_PAGE_SIZE}"


def test_list_locations_sends_parent_and_defaults_to_200() -> None:
    transport = fixed({"count": 0, "next": None, "results": []})

    result = read_tools.list_locations(_client(transport), parent=4)

    assert transport.calls[0].method == "GET"
    assert transport.calls[0].url == f"{BASE}/api/stock/location/?parent=4&limit={MAX_PAGE_SIZE}"
    assert result["limit"] == 200


def test_list_locations_without_parent_sends_only_limit() -> None:
    transport = fixed({"count": 0, "next": None, "results": []})

    read_tools.list_locations(_client(transport))

    assert transport.calls[0].url == f"{BASE}/api/stock/location/?limit={MAX_PAGE_SIZE}"


def test_empty_query_is_left_out_of_the_request() -> None:
    transport = fixed({"count": 0, "next": None, "results": []})

    read_tools.search_parts(_client(transport), query="", category=None)

    assert "search" not in _query(transport.calls[0].url)
    assert "category" not in _query(transport.calls[0].url)


# ---------------------------------------------------------------- detail endpoints


@pytest.mark.parametrize(("given", "expected"), [(42, "42"), ("42", "42"), ("007", "7")])
def test_get_part_builds_the_detail_path(given: int | str, expected: str) -> None:
    transport = fixed({"pk": 42, "name": "Widget"})

    part = read_tools.get_part(_client(transport), given)

    assert transport.calls[0].method == "GET"
    assert transport.calls[0].url == f"{BASE}/api/part/{expected}/"
    assert part == {"pk": 42, "name": "Widget"}


def test_get_stock_item_builds_the_detail_path() -> None:
    transport = fixed({"pk": 5, "quantity": 3})

    item = read_tools.get_stock_item(_client(transport), 5)

    assert transport.calls[0].method == "GET"
    assert transport.calls[0].url == f"{BASE}/api/stock/5/"
    assert item == {"pk": 5, "quantity": 3}


@pytest.mark.parametrize("bad", [0, -3, True, "", "12/../x", "1?x=2", "4.5", "٣", None])
def test_get_part_rejects_bad_pk_before_any_request(bad: object) -> None:
    transport = fixed({"pk": 1})

    with pytest.raises(ValueError):
        read_tools.get_part(_client(transport), bad)  # type: ignore[arg-type]
    assert transport.calls == []


@pytest.mark.parametrize("bad", ["../secret", "7/x", 0, True])
def test_search_filters_reject_bad_pk_before_any_request(bad: object) -> None:
    transport = fixed({"count": 0, "next": None, "results": []})

    with pytest.raises(ValueError):
        read_tools.search_parts(_client(transport), category=bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        read_tools.search_stock(_client(transport), location=bad)  # type: ignore[arg-type]
    assert transport.calls == []


@pytest.mark.parametrize("name", ["get_part", "get_stock_item"])
def test_detail_response_that_is_not_an_object_is_bad_response(name: str) -> None:
    client = _client(fixed([{"pk": 1}]))
    call = read_tools.get_part if name == "get_part" else read_tools.get_stock_item

    with pytest.raises(InvenTreeError) as info:
        call(client, 1)

    assert info.value.code == "bad_response"


# ---------------------------------------------------------------- pagination and limits


def test_search_parts_reports_truncation_with_a_note() -> None:
    transport = paged(total=5)

    result = read_tools.search_parts(_client(transport), limit=3)

    assert [row["pk"] for row in result["items"]] == [1, 2, 3]
    assert result["returned"] == 3
    assert result["truncated"] is True
    assert len(result["notes"]) == 1
    assert "first 3" in result["notes"][0]


def test_search_parts_full_result_is_not_truncated_and_has_no_notes() -> None:
    result = read_tools.search_parts(_client(paged(total=2)), limit=10)

    assert result["truncated"] is False
    assert result["notes"] == []


def test_search_parts_follows_next_links_up_to_the_limit() -> None:
    transport = paged(total=5, server_cap=2)

    result = read_tools.search_parts(_client(transport), limit=10)

    assert [row["pk"] for row in result["items"]] == [1, 2, 3, 4, 5]
    assert result["truncated"] is False
    assert len(transport.calls) == 3


def test_limit_above_one_page_collects_across_pages_and_flags_truncation() -> None:
    complete = paged(total=150)
    result = read_tools.search_parts(_client(complete), limit=200)
    assert result["returned"] == 150
    assert result["truncated"] is False
    assert len(complete.calls) == 2

    cut = paged(total=150)
    result = read_tools.search_parts(_client(cut), limit=120)
    assert result["returned"] == 120
    assert result["truncated"] is True
    assert any("first 120" in note for note in result["notes"])


def test_list_locations_reports_truncation_with_a_note() -> None:
    result = read_tools.list_locations(_client(paged(total=4)), limit=2)

    assert result["truncated"] is True
    assert result["returned"] == 2
    assert any("first 2" in note for note in result["notes"])


@pytest.mark.parametrize(
    ("requested", "applied"),
    [(500, 200), (201, 200), (200, 200), (1, 1), (0, 1), (-7, 1)],
)
def test_limit_is_clamped_and_the_clamp_is_reported(requested: int, applied: int) -> None:
    transport = paged(total=3)

    result = read_tools.search_parts(_client(transport), limit=requested)

    assert result["limit"] == applied
    assert _query(transport.calls[0].url)["limit"] == [str(min(applied, MAX_PAGE_SIZE))]
    clamp_notes = [note for note in result["notes"] if "clamped" in note]
    if requested == applied:
        assert clamp_notes == []
    else:
        assert len(clamp_notes) == 1
        assert str(requested) in clamp_notes[0]
        assert str(applied) in clamp_notes[0]


@pytest.mark.parametrize("bad", ["5", 5.0, True, None])
def test_limit_must_be_an_integer(bad: object) -> None:
    transport = fixed({"count": 0, "next": None, "results": []})

    with pytest.raises(TypeError):
        read_tools.search_parts(_client(transport), limit=bad)  # type: ignore[arg-type]
    assert transport.calls == []


def test_query_must_be_a_string() -> None:
    transport = fixed({"count": 0, "next": None, "results": []})

    with pytest.raises(TypeError):
        read_tools.search_stock(_client(transport), query=5)  # type: ignore[arg-type]
    assert transport.calls == []


# ---------------------------------------------------------------- roles


def test_roles_returns_the_role_list() -> None:
    transport = fixed([{"name": "part_manager"}])

    result = read_tools.roles(_client(transport))

    assert result == {"roles": [{"name": "part_manager"}]}
    assert transport.calls[0].method == "GET"
    assert transport.calls[0].url == f"{BASE}/api/user/roles/"


def test_roles_non_list_response_is_bad_response() -> None:
    with pytest.raises(InvenTreeError) as info:
        read_tools.roles(_client(fixed({"roles": []})))

    assert info.value.code == "bad_response"


# ---------------------------------------------------------------- errors


@pytest.mark.parametrize("name", sorted(READ_CALLS))
def test_404_surfaces_as_not_found(name: str) -> None:
    client = _client(fixed({"detail": "Not found."}, status=404))

    with pytest.raises(InvenTreeError) as info:
        READ_CALLS[name](client)

    assert info.value.code == "not_found"
    assert info.value.status == 404
    assert TOKEN not in info.value.message


@pytest.mark.parametrize("name", sorted(READ_CALLS))
def test_403_surfaces_as_unauthorized(name: str) -> None:
    with pytest.raises(InvenTreeError) as info:
        READ_CALLS[name](_client(fixed({"detail": "denied"}, status=403)))

    assert info.value.code == "unauthorized"
    assert info.value.status == 403


# ---------------------------------------------------------------- read only


def test_read_tools_never_send_a_write(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse_write(self: InvenTreeClient, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("read tools must not call post or patch")

    monkeypatch.setattr(InvenTreeClient, "post", refuse_write)
    monkeypatch.setattr(InvenTreeClient, "patch", refuse_write)

    for name, call in READ_CALLS.items():
        transport = FakeTransport(_answer_by_path)
        result = call(_client(transport))

        assert transport.calls, f"{name} sent no request"
        assert {request.method for request in transport.calls} == {"GET"}, name
        json.dumps(result)  # every result is JSON-serialisable
