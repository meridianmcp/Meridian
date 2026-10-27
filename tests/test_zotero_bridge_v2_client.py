"""Meridian-side project mapping and idempotent source sync for the Zotero bridge (proposal 9c0a4767).

Every test runs against FakeLibrary, an in-memory stand-in for one Zotero library. Its reader serves the collections,
collection items and children that the client reads, and its bridge changes the library when a write arrives, so a
rerun sees what the first run wrote. Nothing here opens a socket, reads a token or raises a grant prompt. The fake
follows Zotero's JSON shape only as far as the client reads it. It is not Zotero, and it proves nothing about the live
bridge.
"""

from __future__ import annotations

from typing import Any

import pytest

from meridian import zotero_bridge_client as zbc
from meridian.zotero_bridge_client import (
    ALREADY_ATTACHED,
    ALREADY_EXISTS,
    ALREADY_PRESENT,
    CREATED,
    CollectionChain,
    ZoteroBridgeError,
)
from zotero_bridge_fakes import URL, FakeLibrary, LibraryReader, make


# ---------------------------------------------------------------------------------------------
# Reading collections
# ---------------------------------------------------------------------------------------------


def test_list_collections_reads_every_page_and_maps_parents():
    library = FakeLibrary()
    top = library.seed_collection("Top")
    for index in range(249):
        library.seed_collection(f"Child {index}", parent_key=top)
    client, _, reader = make(library)

    rows = client.list_collections()

    assert len(rows) == 250
    assert [row["parent_key"] for row in rows if row["key"] == top] == [None]
    assert all(row["parent_key"] == top for row in rows if row["key"] != top)
    assert [path.partition("start=")[2] for path in reader.paths] == ["0", "100", "200"]


def test_list_collections_refuses_a_partial_list_past_the_page_limit(monkeypatch):
    monkeypatch.setattr(zbc, "COLLECTION_PAGE_LIMIT", 2)
    library = FakeLibrary()
    for index in range(201):
        library.seed_collection(f"Name {index}")
    client, bridge, _ = make(library)

    with pytest.raises(ZoteroBridgeError) as info:
        client.list_collections()

    assert info.value.code == "reader_failed"
    with pytest.raises(ZoteroBridgeError) as ensure_info:
        client.ensure_collection("Fresh name")
    assert ensure_info.value.code == "reader_failed"
    assert library.created == [] and bridge.calls == []


def test_list_collections_reads_a_list_of_exactly_the_page_limit_whole(monkeypatch):
    monkeypatch.setattr(zbc, "COLLECTION_PAGE_LIMIT", 2)
    library = FakeLibrary()
    for index in range(200):
        library.seed_collection(f"Name {index}")
    client, _, reader = make(library)

    rows = client.list_collections()

    assert len(rows) == 200
    assert [path.partition("start=")[2] for path in reader.paths] == ["0", "100", "200"]


@pytest.mark.parametrize(
    "bad_row",
    [
        {"key": "BADKEY01", "data": {"key": "BADKEY01", "name": 5, "parentCollection": False}},
        {"key": "BADKEY02", "data": {"key": "BADKEY02", "name": "No parent field"}},
        {"key": "BADKEY03", "data": {"key": "BADKEY03", "name": "Bad parent", "parentCollection": "not a key"}},
        {"key": "BADKEY04"},
        # Zotero reports a top-level collection as false. Null and an empty string are not top-level, and not keys.
        {"key": "BADKEY05", "data": {"key": "BADKEY05", "name": "Null parent", "parentCollection": None}},
        {"key": "BADKEY06", "data": {"key": "BADKEY06", "name": "Empty parent", "parentCollection": ""}},
    ],
)
def test_an_unreadable_collection_row_fails_the_check_and_nothing_is_written(bad_row):
    library = FakeLibrary()
    library.seed_collection("Keep")
    real = LibraryReader(library)

    def reader_with_a_bad_row(path: str) -> tuple[int, Any]:
        status, body = real(path)
        if path.startswith("/api/users/0/collections?"):
            body = [*body, bad_row]
        return status, body

    client, bridge, _ = make(library, reader=reader_with_a_bad_row)

    with pytest.raises(ZoteroBridgeError) as info:
        client.ensure_collection("Anything")

    assert info.value.code == "reader_failed"
    assert library.created == [] and bridge.calls == []


# ---------------------------------------------------------------------------------------------
# ensure_collection: exact name, parent-scoped, refused when ambiguous
# ---------------------------------------------------------------------------------------------


def test_ensure_collection_creates_once_then_reuses_the_same_key():
    library = FakeLibrary()
    client, _, _ = make(library)

    first = client.ensure_collection("Project A")
    second = client.ensure_collection("Project A")

    assert first[0] == CREATED
    assert second == (ALREADY_EXISTS, first[1])
    assert library.count("collection") == 1


def test_ensure_collection_matches_on_name_and_parent_not_on_name_alone():
    library = FakeLibrary()
    project_a = library.seed_collection("Project A")
    project_b = library.seed_collection("Project B")
    methods_a = library.seed_collection("Methods", parent_key=project_a)
    client, _, _ = make(library)

    assert client.ensure_collection("Methods", project_a) == (ALREADY_EXISTS, methods_a)
    status, key = client.ensure_collection("Methods", project_b)

    assert status == CREATED
    assert key != methods_a
    assert library.collections[key]["parent_key"] == project_b


def test_names_match_exactly_and_case_sensitively_with_no_trimming():
    library = FakeLibrary()
    existing = library.seed_collection("Methods")
    client, _, _ = make(library)

    assert client.ensure_collection("Methods") == (ALREADY_EXISTS, existing)
    for other in ("methods", "METHODS", "Methods ", " Methods"):
        status, key = client.ensure_collection(other)
        assert status == CREATED and key != existing
    assert library.count("collection") == 4


def test_a_renamed_collection_is_not_found_so_the_new_name_creates_a_second_one():
    library = FakeLibrary()
    library.seed_collection("Old name")
    client, _, _ = make(library)

    status, _ = client.ensure_collection("New name")

    assert status == CREATED


def test_two_collections_with_one_name_under_the_same_parent_are_refused_with_no_write():
    library = FakeLibrary()
    project = library.seed_collection("Project A")
    library.seed_collection("Methods", parent_key=project)
    library.seed_collection("Methods", parent_key=project)
    client, bridge, _ = make(library)

    with pytest.raises(ZoteroBridgeError) as info:
        client.ensure_collection("Methods", project)

    assert info.value.code == "ambiguous_collection"
    assert library.count("collection") == 0 and bridge.calls == []


def test_invalid_names_and_parent_keys_are_refused_before_any_read():
    library = FakeLibrary()
    client, bridge, reader = make(library)

    with pytest.raises(ZoteroBridgeError) as blank:
        client.ensure_collection("   ")
    with pytest.raises(ZoteroBridgeError) as bad_parent:
        client.ensure_collection("Methods", "not-a-key!")

    assert blank.value.code == "invalid_input"
    assert bad_parent.value.code == "invalid_input"
    assert reader.paths == [] and bridge.calls == []


def test_create_collection_keeps_its_default_of_always_creating():
    library = FakeLibrary()
    client, _, _ = make(library)

    first = client.create_collection("Project A")
    second = client.create_collection("Project A")

    assert first != second
    assert library.count("collection") == 2


# ---------------------------------------------------------------------------------------------
# ensure_collection_chain: a project and its one-level subproject
# ---------------------------------------------------------------------------------------------


def test_chain_maps_a_project_and_its_subproject_with_the_parent_chain():
    library = FakeLibrary()
    client, _, _ = make(library)

    chain = client.ensure_collection_chain("Project A", "Methods")

    assert chain.project_status == CREATED and chain.subproject_status == CREATED
    assert library.collections[chain.project_key]["parent_key"] is None
    assert library.collections[chain.subproject_key] == {"name": "Methods", "parent_key": chain.project_key}


def test_chain_rerun_creates_nothing_new():
    library = FakeLibrary()
    client, _, _ = make(library)
    first = client.ensure_collection_chain("Project A", "Methods")

    again = client.ensure_collection_chain("Project A", "Methods")

    assert again == CollectionChain(first.project_key, ALREADY_EXISTS, first.subproject_key, ALREADY_EXISTS)
    assert library.count("collection") == 2


def test_chain_without_a_subproject_ensures_only_the_project():
    library = FakeLibrary()
    client, _, _ = make(library)

    chain = client.ensure_collection_chain("Project A")

    assert chain == CollectionChain(chain.project_key, CREATED, None, None)
    assert library.count("collection") == 1


def test_an_existing_project_with_a_new_subproject_creates_only_the_subproject():
    library = FakeLibrary()
    project = library.seed_collection("Project A")
    client, _, _ = make(library)

    chain = client.ensure_collection_chain("Project A", "Methods")

    assert chain == CollectionChain(project, ALREADY_EXISTS, chain.subproject_key, CREATED)
    assert library.count("collection") == 1


def test_one_subproject_name_under_two_projects_gives_two_collections():
    library = FakeLibrary()
    client, _, _ = make(library)

    first = client.ensure_collection_chain("Project A", "Methods")
    second = client.ensure_collection_chain("Project B", "Methods")

    assert first.subproject_key != second.subproject_key
    assert library.collections[second.subproject_key]["parent_key"] == second.project_key
    assert client.ensure_collection_chain("Project A", "Methods").subproject_key == first.subproject_key


def test_two_top_level_collections_named_as_the_project_refuse_the_chain_before_any_write():
    library = FakeLibrary()
    library.seed_collection("Project A")
    library.seed_collection("Project A")
    client, bridge, _ = make(library)

    with pytest.raises(ZoteroBridgeError) as info:
        client.ensure_collection_chain("Project A", "Methods")

    assert info.value.code == "ambiguous_collection"
    assert library.count("collection") == 0 and bridge.calls == []


def test_an_ambiguous_subproject_refuses_the_chain_before_anything_is_written():
    library = FakeLibrary()
    project = library.seed_collection("Project A")
    library.seed_collection("Methods", parent_key=project)
    library.seed_collection("Methods", parent_key=project)
    client, bridge, _ = make(library)

    with pytest.raises(ZoteroBridgeError) as info:
        client.ensure_collection_chain("Project A", "Methods")

    assert info.value.code == "ambiguous_collection"
    assert library.count("collection") == 0 and bridge.calls == []


def test_chain_refuses_blank_names_before_any_read():
    library = FakeLibrary()
    client, bridge, reader = make(library)

    with pytest.raises(ZoteroBridgeError) as info:
        client.ensure_collection_chain("  ", "Methods")

    assert info.value.code == "invalid_input"
    assert reader.paths == [] and bridge.calls == []


# ---------------------------------------------------------------------------------------------
# create_webpage: the same exact URL in the named collections is not written twice
# ---------------------------------------------------------------------------------------------


def test_webpage_rerun_with_a_collection_returns_the_existing_key_and_writes_nothing():
    library = FakeLibrary()
    collection = library.seed_collection("Methods")
    client, _, _ = make(library)

    status, key = client.create_webpage("Pricing", URL, [collection])
    assert (status, library.count("item")) == (CREATED, 1)

    again = client.create_webpage("Pricing", URL, [collection])

    assert again == (ALREADY_PRESENT, key)
    assert library.count("item") == 1


def test_webpage_without_collections_is_not_checked_so_each_call_creates_an_item():
    library = FakeLibrary()
    client, _, reader = make(library)

    first = client.create_webpage("Pricing", URL)
    second = client.create_webpage("Pricing", URL)

    assert first[0] == second[0] == CREATED
    assert first[1] != second[1]
    assert library.count("item") == 2
    assert reader.paths == []


def test_webpage_url_match_is_exact_and_limited_to_webpage_items():
    library = FakeLibrary()
    collection = library.seed_collection("Methods")
    library.seed_item("journalArticle", URL, [collection])
    library.seed_item("webpage", URL + "/", [collection])
    library.seed_item("webpage", "https://example.org/Pricing", [collection])
    client, _, _ = make(library)

    status, _ = client.create_webpage("Pricing", URL, [collection])

    assert status == CREATED
    assert library.count("item") == 1


def test_webpage_held_by_only_some_collections_is_refused_with_no_write():
    library = FakeLibrary()
    first = library.seed_collection("A")
    second = library.seed_collection("B")
    library.seed_item("webpage", URL, [first])
    client, bridge, _ = make(library)

    with pytest.raises(ZoteroBridgeError) as info:
        client.create_webpage("Pricing", URL, [first, second])

    assert info.value.code == "partial_match"
    assert library.count("item") == 0 and bridge.calls == []


def test_webpage_held_by_every_collection_returns_the_existing_key_even_with_a_repeated_collection():
    library = FakeLibrary()
    first = library.seed_collection("A")
    second = library.seed_collection("B")
    existing = library.seed_item("webpage", URL, [first, second])
    client, _, _ = make(library)

    assert client.create_webpage("Pricing", URL, [first, second]) == (ALREADY_PRESENT, existing)
    assert client.create_webpage("Pricing", URL, [first, first]) == (ALREADY_PRESENT, existing)
    assert library.count("item") == 0


def test_webpage_check_that_cannot_read_the_collection_writes_nothing():
    library = FakeLibrary()
    collection = library.seed_collection("Methods")
    client, bridge, _ = make(library, reader=LibraryReader(library, fail_status=500))

    with pytest.raises(ZoteroBridgeError) as info:
        client.create_webpage("Pricing", URL, [collection])

    assert info.value.code == "reader_failed"
    assert library.count("item") == 0 and bridge.calls == []


def test_webpage_check_refuses_a_collection_past_the_item_page_limit_with_no_write(monkeypatch):
    monkeypatch.setattr(zbc, "ITEM_PAGE_LIMIT", 2)
    library = FakeLibrary()
    collection = library.seed_collection("Methods")
    for index in range(201):
        library.seed_item("webpage", f"https://example.org/page/{index}", [collection])
    client, bridge, _ = make(library)

    with pytest.raises(ZoteroBridgeError) as info:
        client.create_webpage("Pricing", URL, [collection])

    assert info.value.code == "reader_failed"
    assert library.count("item") == 0 and bridge.calls == []


def test_webpage_check_reads_a_collection_of_exactly_the_item_page_limit_whole(monkeypatch):
    monkeypatch.setattr(zbc, "ITEM_PAGE_LIMIT", 2)
    library = FakeLibrary()
    collection = library.seed_collection("Methods")
    for index in range(199):
        library.seed_item("webpage", f"https://example.org/page/{index}", [collection])
    existing = library.seed_item("webpage", URL, [collection])  # the 200th row, found only if every row is read
    client, bridge, _ = make(library)

    assert client.create_webpage("Pricing", URL, [collection]) == (ALREADY_PRESENT, existing)
    assert library.count("item") == 0 and bridge.calls == []


@pytest.mark.parametrize(
    "bad_row",
    [
        {"key": "BADWEB01", "data": "not an object"},
        {"key": "BADWEB02", "data": {"key": "BADWEB02", "url": URL}},
        {"key": "BADWEB03", "data": {"key": "BADWEB03", "itemType": 7, "url": URL}},
        {"key": "BADWEB04", "data": {"key": "BADWEB04", "itemType": "webpage"}},
        {"key": "bad key", "data": {"key": "bad key", "itemType": "webpage", "url": URL}},
    ],
)
def test_a_malformed_webpage_row_fails_the_check_so_a_rerun_cannot_duplicate(bad_row):
    library = FakeLibrary()
    collection = library.seed_collection("Methods")
    real = LibraryReader(library)

    def reader_with_a_bad_row(path: str) -> tuple[int, Any]:
        status, body = real(path)
        if path.startswith(f"/api/users/0/collections/{collection}/items?"):
            body = [*body, bad_row]
        return status, body

    client, bridge, _ = make(library, reader=reader_with_a_bad_row)

    with pytest.raises(ZoteroBridgeError) as info:
        client.create_webpage("Pricing", URL, [collection])

    assert info.value.code == "reader_failed"
    assert library.count("item") == 0 and bridge.calls == []


def test_a_readable_row_of_another_kind_is_skipped_even_without_a_url():
    library = FakeLibrary()
    collection = library.seed_collection("Methods")
    real = LibraryReader(library)
    note_row = {"key": "NOTE0001", "data": {"key": "NOTE0001", "itemType": "note"}}

    def reader_with_a_note(path: str) -> tuple[int, Any]:
        status, body = real(path)
        if path.startswith(f"/api/users/0/collections/{collection}/items?"):
            body = [*body, note_row]
        return status, body

    client, _, _ = make(library, reader=reader_with_a_note)

    status, _ = client.create_webpage("Pricing", URL, [collection])

    assert status == CREATED
    assert library.count("item") == 1


# ---------------------------------------------------------------------------------------------
# Idempotence: a rerun of the whole sync creates nothing new
# ---------------------------------------------------------------------------------------------


def test_a_rerun_of_the_whole_sync_creates_nothing_new(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    library = FakeLibrary()
    client, _, _ = make(library)

    def sync():
        chain = client.ensure_collection_chain("Project A", "Methods")
        status, item = client.create_webpage("Pricing", URL, [chain.subproject_key])
        attach = client.attach_pdf(item, str(pdf), title="Full text")
        note = client.add_child_note(item, "<p>Summary</p>", "meridian-summary-v1")
        return chain, status, item, attach, note

    _, first_status, item, first_attach, first_note = sync()

    assert first_status == CREATED
    assert library.created == ["collection", "collection", "item", "attachment", "note"]
    assert first_attach != ALREADY_ATTACHED and first_note != ALREADY_PRESENT

    second_chain, second_status, second_item, second_attach, second_note = sync()

    assert library.created == ["collection", "collection", "item", "attachment", "note"]
    assert second_chain.project_status == ALREADY_EXISTS
    assert second_chain.subproject_status == ALREADY_EXISTS
    assert (second_status, second_item) == (ALREADY_PRESENT, item)
    assert second_attach == ALREADY_ATTACHED
    assert second_note == ALREADY_PRESENT


def test_a_rerun_that_finds_everything_asks_for_no_grant_and_writes_nothing():
    library = FakeLibrary()
    client, bridge, _ = make(library)
    client.ensure_collection_chain("Project A", "Methods")
    grants_before = [c for c in bridge.calls if c[1] == "/v1/grants"]

    again = client.ensure_collection_chain("Project A", "Methods")

    assert again.project_status == ALREADY_EXISTS and again.subproject_status == ALREADY_EXISTS
    assert [c for c in bridge.calls if c[1] == "/v1/grants"] == grants_before
    assert library.count("collection") == 2
