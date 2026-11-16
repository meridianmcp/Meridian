"""Zotero local API: libraries, collections and search inside one collection. Read-only, offline.

Every test passes a fake transport, so no request reaches a real Zotero. The fake records each path it is asked for, so a
test can prove that a bad id is refused before anything is sent.
"""

import json

import pytest

from meridian_desktop import zotero_client as zc
from meridian_desktop.zotero_client import (
    LIBRARY_GROUP,
    LIBRARY_USER,
    Reply,
    ZoteroCollection,
    ZoteroError,
    ZoteroLibrary,
)

USER = ZoteroLibrary(LIBRARY_USER, "0", "My Library")
GROUP = ZoteroLibrary(LIBRARY_GROUP, "123", "Lab Group")
COLLECTION_KEY = "AAAABBBB"


class Recorder:
    """A transport that records every path and answers with the handler's reply or raised error."""

    def __init__(self, handler):
        self.handler = handler
        self.paths = []

    def __call__(self, base_url, path, headers, timeout):
        self.paths.append(path)
        return self.handler(path)


def _json(value, status=200):
    return Reply(status, json.dumps(value).encode("utf-8"))


def _never(path):
    pytest.fail(f"no request expected, but one was sent: {path}")


def _collection_entry(number, parent=False, name=None):
    return {
        "key": f"C{number:07d}",
        "data": {"name": name or f"Col {number}", "parentCollection": parent},
    }


# ----------------------------------------------------------------- library prefix


def test_the_user_library_and_a_group_have_their_own_fixed_prefixes():
    assert zc.library_prefix(USER) == "/api/users/0"
    assert zc.library_prefix(GROUP) == "/api/groups/123"


@pytest.mark.parametrize(
    "library",
    [
        ZoteroLibrary(LIBRARY_USER, "5", "Someone else"),
        ZoteroLibrary(LIBRARY_GROUP, "abc", "Letters"),
        ZoteroLibrary(LIBRARY_GROUP, "", "Empty"),
        ZoteroLibrary(LIBRARY_GROUP, "1/../../x", "Path tricks"),
        ZoteroLibrary("other", "0", "Unknown type"),
    ],
)
def test_a_library_id_that_is_not_plain_is_refused_before_any_request(library):
    transport = Recorder(_never)
    with pytest.raises(ZoteroError) as caught:
        zc.list_collections(library, transport=transport)
    assert caught.value.code == "bad_scope"
    assert transport.paths == []


# ----------------------------------------------------------------- libraries


def test_my_library_comes_first_and_the_groups_follow_by_name():
    groups = [
        {"id": 9, "data": {"name": "Zed group"}},
        {"id": "123", "data": {"name": "lab group"}},
        {"id": "x", "data": {"name": "not a number"}},
        "not an object",
    ]
    transport = Recorder(lambda path: _json(groups))
    libraries, note = zc.list_libraries(transport=transport)
    assert note == ""
    assert libraries == [
        USER,
        ZoteroLibrary(LIBRARY_GROUP, "123", "lab group"),
        ZoteroLibrary(LIBRARY_GROUP, "9", "Zed group"),
    ]
    assert transport.paths == ["/api/users/0/groups?format=json"]


def test_a_group_without_a_name_is_named_by_its_id():
    libraries, _note = zc.list_libraries(transport=Recorder(lambda path: _json([{"id": 7, "data": {}}])))
    assert libraries[1] == ZoteroLibrary(LIBRARY_GROUP, "7", "Group 7")


@pytest.mark.parametrize("reply", [Reply(404, b"no"), Reply(500, b"boom"), Reply(200, b"not json")])
def test_a_failed_groups_listing_keeps_my_library_and_gives_the_reason(reply):
    libraries, note = zc.list_libraries(transport=Recorder(lambda path: reply))
    assert libraries == [USER]
    assert note == zc.GROUPS_UNAVAILABLE_NOTE


def test_a_switched_off_local_api_raises_for_the_libraries_listing():
    with pytest.raises(ZoteroError) as caught:
        zc.list_libraries(transport=Recorder(lambda path: Reply(403, b"")))
    assert caught.value.code == "api_disabled"


def test_an_unreachable_api_is_raised_and_not_hidden_as_no_groups():
    def gone(path):
        raise ZoteroError("not_running", zc.NOT_RUNNING_MESSAGE)

    with pytest.raises(ZoteroError) as caught:
        zc.list_libraries(transport=Recorder(gone))
    assert caught.value.code == "not_running"


# ----------------------------------------------------------------- collections


def test_collections_are_read_page_by_page_until_a_short_page():
    first = [_collection_entry(number) for number in range(zc.COLLECTION_PAGE)]
    second = [_collection_entry(number) for number in range(zc.COLLECTION_PAGE, zc.COLLECTION_PAGE + 3)]

    def handler(path):
        return _json(first if "start=0" in path else second)

    transport = Recorder(handler)
    result = zc.list_collections(USER, transport=transport)
    assert len(result) == zc.COLLECTION_PAGE + 3
    assert transport.paths == [
        "/api/users/0/collections?format=json&limit=100&start=0",
        "/api/users/0/collections?format=json&limit=100&start=100",
    ]


def test_parent_keys_are_read_and_an_entry_without_a_valid_key_is_skipped():
    entries = [
        {"key": "AAAA1111", "data": {"name": "Top", "parentCollection": False}},
        {"key": "BBBB2222", "data": {"name": "Child", "parentCollection": "AAAA1111"}},
        {"key": "bad", "data": {"name": "Bad key"}},
        {"key": "CCCC3333", "data": {"name": "Odd parent", "parentCollection": "../etc"}},
        {"key": "DDDD4444", "data": {"name": "   spaced   "}},
        {"key": "EEEE5555", "name": "Flat entry"},
    ]
    result = zc.list_collections(USER, transport=Recorder(lambda path: _json(entries)))
    by_key = {collection.key: collection for collection in result}
    assert set(by_key) == {"AAAA1111", "BBBB2222", "CCCC3333", "DDDD4444", "EEEE5555"}
    assert by_key["AAAA1111"].parent_key == ""
    assert by_key["BBBB2222"].parent_key == "AAAA1111"
    assert by_key["CCCC3333"].parent_key == ""
    assert by_key["DDDD4444"].name == "spaced"
    assert by_key["EEEE5555"].name == "Flat entry"


def test_a_library_too_big_to_read_in_full_is_refused_and_not_cut_short():
    full_page = [_collection_entry(number) for number in range(zc.COLLECTION_PAGE)]
    transport = Recorder(lambda path: _json(full_page))
    with pytest.raises(ZoteroError) as caught:
        zc.list_collections(USER, transport=transport)
    assert caught.value.code == "too_many"
    assert len(transport.paths) == zc.COLLECTION_PAGE_LIMIT


def test_a_switched_off_api_raises_for_the_collections_listing():
    with pytest.raises(ZoteroError) as caught:
        zc.list_collections(USER, transport=Recorder(lambda path: Reply(403, b"")))
    assert caught.value.code == "api_disabled"


def test_a_failed_collections_reply_raises_with_its_status_code():
    with pytest.raises(ZoteroError) as caught:
        zc.list_collections(USER, transport=Recorder(lambda path: Reply(500, b"boom")))
    assert caught.value.code == "http_500"


# ----------------------------------------------------------------- tree order


def test_collection_rows_are_in_tree_order_with_depth():
    collections = [
        ZoteroCollection("BBBBBBBB", "Beta", ""),
        ZoteroCollection("AAAAAAAA", "Alpha", ""),
        ZoteroCollection("CCCCCCCC", "Alpha child", "AAAAAAAA"),
        ZoteroCollection("DDDDDDDD", "Grandchild", "CCCCCCCC"),
        ZoteroCollection("EEEEEEEE", "Orphan", "ZZZZZZZZ"),
    ]
    rows = [(collection.name, depth) for collection, depth in zc.collection_rows(collections)]
    assert rows == [("Alpha", 0), ("Alpha child", 1), ("Grandchild", 2), ("Beta", 0), ("Orphan", 0)]


def test_a_parent_cycle_lists_every_collection_once():
    collections = [
        ZoteroCollection("AAAAAAAA", "A", "BBBBBBBB"),
        ZoteroCollection("BBBBBBBB", "B", "AAAAAAAA"),
    ]
    rows = zc.collection_rows(collections)
    assert sorted(collection.key for collection, _depth in rows) == ["AAAAAAAA", "BBBBBBBB"]


# ----------------------------------------------------------------- search in one collection


def test_a_collection_search_reads_top_level_items_inside_that_collection_only():
    entries = [{"key": "ABCD1234", "data": {"title": "Deep", "itemType": "journalArticle"}}]
    transport = Recorder(lambda path: _json(entries))
    items = zc.search_collection_items("deep learning", USER, COLLECTION_KEY, transport=transport)
    assert transport.paths == [
        f"/api/users/0/collections/{COLLECTION_KEY}/items/top?q=deep%20learning&format=json&limit=25",
    ]
    assert [item.key for item in items] == ["ABCD1234"]


def test_a_collection_search_in_a_group_uses_the_group_prefix():
    transport = Recorder(lambda path: _json([]))
    zc.search_collection_items("x", GROUP, COLLECTION_KEY, transport=transport)
    assert transport.paths == [f"/api/groups/123/collections/{COLLECTION_KEY}/items/top?q=x&format=json&limit=25"]


def test_a_blank_query_is_refused_before_any_request():
    with pytest.raises(ZoteroError) as caught:
        zc.search_collection_items("   ", USER, COLLECTION_KEY, transport=Recorder(_never))
    assert caught.value.code == "blank_query"


@pytest.mark.parametrize("key", ["", "SHORT", "AAAABBBBC", "aaaabbbb", "AAAA/../"])
def test_a_bad_collection_key_is_refused_before_any_request(key):
    with pytest.raises(ZoteroError) as caught:
        zc.search_collection_items("deep", USER, key, transport=Recorder(_never))
    assert caught.value.code == "bad_scope"
