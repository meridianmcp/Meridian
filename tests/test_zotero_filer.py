"""Tests for meridian.zotero_filer.

Every test injects a fake transport, so nothing here touches the real Zotero
app, the network, or any file outside pytest's tmp_path.
"""

from __future__ import annotations

import ast
import json
import os
import urllib.request
from pathlib import Path

import pytest

from meridian import zotero_connect as zc
from meridian import zotero_filer as zf

COLLECTIONS = zf.LOCAL_API + "/collections"
ITEMS = zf.LOCAL_API + "/collections/ABCD1234/items"
SELECTED = zf.CONNECTOR + "/getSelectedCollection"
SAVE = zf.CONNECTOR + "/saveItems"

SAMPLE_BIB = """
@article{smith2020deep,
  author = {Smith, Jane and {Google DeepMind}},
  title = {Deep {Learning} for {GPT}-style {Models}},
  journal = {Journal of Things},
  year = {2020},
  doi = {10.1000/xyz123},
  url = {https://example.org/paper},
  abstract = {We study {nested {braces}} in abstracts.},
}

@inproceedings{lee2021attention,
  author = "Lee, Min and Park, Ho",
  title = "Attention {Is} All You Need, Revisited",
  booktitle = {Proceedings of Things},
  year = 2021,
  eprint = {2101.00001},
}
"""


class FakeTransport:
    """Records every request and answers from a route table.

    routes maps (METHOD, path-without-query) to either a fixed (status, payload)
    tuple or a callable(url, body) returning one. Any unmapped request raises
    AssertionError, so an unexpected Zotero call fails the test loudly.
    """

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, body, headers):
        self.calls.append({"method": method, "url": url, "body": body, "headers": dict(headers)})
        handler = self.routes.get((method, url.split("?", 1)[0]))
        if handler is None:
            raise AssertionError(f"unexpected request: {method} {url}")
        status, payload = handler(url, body) if callable(handler) else handler
        return status, json.dumps(payload)

    def posts(self):
        return [call for call in self.calls if call["method"] == "POST"]


class FakeClock:
    """Fake clock: time only moves when sleep() is called."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _collection(key, name):
    return {"key": key, "version": 1, "library": {"type": "user", "id": 0}, "data": {"key": key, "name": name}}


def _item(key, doi="", title="", item_type="journalArticle"):
    return {"key": key, "data": {"key": key, "itemType": item_type, "DOI": doi, "title": title}}


def _selection(name):
    return (200, {"id": "C1", "name": name, "targets": []})


@pytest.fixture
def bib_file(tmp_path):
    path = tmp_path / "refs.bib"
    path.write_text(SAMPLE_BIB, encoding="utf-8")
    return str(path)


class _MemoryApprovalStore:
    """In-memory stand-in for the OS keyring, so live runs see a stored approval."""

    def __init__(self, key):
        self.key = key
        self.delete_calls = 0

    def get(self):
        return self.key

    def set(self, key):
        self.key = key

    def delete(self):
        self.delete_calls += 1
        had = self.key is not None
        self.key = None
        return had


@pytest.fixture(autouse=True)
def _stored_zotero_approval(monkeypatch):
    """Every live run in this file starts with a stored approval. The key is a fake test value."""
    store = _MemoryApprovalStore("fake-filer-test-approval")
    monkeypatch.setattr(zc, "KeyringApprovalStore", lambda: store)
    return store


def _research_routes(items_payload=None, selected="Research"):
    return {
        ("GET", COLLECTIONS): (200, [_collection("ABCD1234", "Research")]),
        ("GET", ITEMS): (200, [] if items_payload is None else items_payload),
        ("POST", SELECTED): _selection(selected),
    }


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

def test_find_collection_key_is_exact_and_never_fuzzy():
    fake = FakeTransport({
        ("GET", COLLECTIONS): (200, [_collection("ABCD1234", "Research"), _collection("EFGH5678", "Research 2")]),
    })
    assert zf.find_collection_key("Research", fake) == "ABCD1234"
    assert zf.find_collection_key("Research 2", fake) == "EFGH5678"
    assert zf.find_collection_key("research", fake) is None
    assert zf.find_collection_key("Reseach", fake) is None
    assert zf.find_collection_key("Missing", fake) is None


def test_find_collection_key_refuses_duplicate_names():
    fake = FakeTransport({
        ("GET", COLLECTIONS): (200, [_collection("ABCD1234", "Research"), _collection("EFGH5678", "Research")]),
    })
    with pytest.raises(zf.ZoteroError):
        zf.find_collection_key("Research", fake)


def test_list_collections_follows_pagination_when_a_page_is_full():
    first_page = [_collection(f"K{i:07d}", f"Coll {i}") for i in range(100)]
    second_page = [_collection("LAST0001", "Last")]
    fake = FakeTransport({
        ("GET", COLLECTIONS): lambda url, body: (200, second_page if "start=100" in url else first_page),
    })
    result = zf.list_collections(fake)
    assert len(result) == 101
    assert result[-1] == {"key": "LAST0001", "name": "Last"}
    assert [c["url"] for c in fake.calls] == [COLLECTIONS + "?limit=100", COLLECTIONS + "?limit=100&start=100"]


def test_local_api_disabled_raises_zotero_error():
    fake = FakeTransport({("GET", COLLECTIONS): (403, {"error": "Local API is not enabled"})})
    with pytest.raises(zf.ZoteroError):
        zf.list_collections(fake)


def test_selected_collection_posts_empty_json_body_with_allowed_header():
    fake = FakeTransport({("POST", SELECTED): (200, {"id": "C1", "name": "Research", "targets": []})})
    assert zf.selected_collection(fake) == {"id": "C1", "name": "Research", "targets": []}
    call = fake.calls[0]
    assert call["body"] == b"{}"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["headers"]["Zotero-Allowed-Request"] == "1"


def test_wait_for_selection_times_out_on_a_fake_clock():
    clock = FakeClock()
    fake = FakeTransport({("POST", SELECTED): _selection("Other")})
    assert zf.wait_for_selection("Research", fake, timeout_s=3, poll_s=1.0, sleep=clock.sleep, clock=clock) is False
    assert clock.now == 3


def test_wait_for_selection_returns_true_without_sleeping_when_already_selected():
    clock = FakeClock()
    fake = FakeTransport({("POST", SELECTED): _selection("Research")})
    assert zf.wait_for_selection("Research", fake, sleep=clock.sleep, clock=clock) is True
    assert clock.now == 0


def test_open_collection_only_runs_on_windows_and_checks_the_key(monkeypatch):
    opened = []
    monkeypatch.setattr(os, "startfile", lambda target: opened.append(target), raising=False)

    monkeypatch.setattr(zf, "_is_windows", lambda: False)
    assert zf.open_collection_in_zotero("ABCD1234") is False
    assert opened == []

    monkeypatch.setattr(zf, "_is_windows", lambda: True)
    assert zf.open_collection_in_zotero("ABCD1234") is True
    assert zf.open_collection_in_zotero("../../evil") is False
    assert opened == ["zotero://select/library/collections/ABCD1234"]


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def test_save_items_sends_fresh_session_and_returns_written_status():
    fake = FakeTransport({("POST", SAVE): (201, {})})
    items = [{"itemType": "document", "title": "T"}]
    assert zf.save_items(items, fake) == {"status": "written", "http_status": 201}
    payload = json.loads(fake.calls[0]["body"])
    assert payload["uri"] == "https://meridian.local/research"
    assert payload["items"] == items
    assert len(payload["sessionID"]) == 32


def test_save_items_reports_http_and_connection_errors_without_writing():
    fake = FakeTransport({("POST", SAVE): (500, {"error": "boom"})})
    assert zf.save_items([{"title": "T"}], fake)["status"] == "error"

    def refuse(method, url, body, headers):
        raise ConnectionRefusedError("refused")

    assert zf.save_items([{"title": "T"}], refuse)["status"] == "unreachable"


# ---------------------------------------------------------------------------
# BibTeX parsing and conversion
# ---------------------------------------------------------------------------

def test_parse_bibtex_two_entries_with_nested_braces_and_bare_numbers():
    entries = zf.parse_bibtex(SAMPLE_BIB)
    assert [e["citekey"] for e in entries] == ["smith2020deep", "lee2021attention"]
    first, second = entries
    assert first["entrytype"] == "article"
    assert first["title"] == "Deep Learning for GPT-style Models"
    assert first["author"] == "Smith, Jane and {Google DeepMind}"
    assert first["year"] == "2020"
    assert first["doi"] == "10.1000/xyz123"
    assert first["abstract"] == "We study nested braces in abstracts."
    assert second["entrytype"] == "inproceedings"
    assert second["title"] == "Attention Is All You Need, Revisited"
    assert second["year"] == "2021"
    assert second["eprint"] == "2101.00001"
    assert second["doi"] == ""
    assert set(first) == {
        "citekey", "entrytype", "title", "author", "year", "doi", "url",
        "journal", "booktitle", "eprint", "note", "abstract",
    }


def test_bib_to_connector_item_article_with_doi_and_arxiv_eprint():
    entry = {
        "citekey": "k2020", "entrytype": "article", "title": "A Title",
        "author": "Jane Smith and {Google DeepMind}", "year": "2020",
        "doi": "https://doi.org/10.1000/xyz123", "url": "", "journal": "J. Things",
        "booktitle": "", "eprint": "2101.00001", "note": "", "abstract": "Short.",
    }
    item = zf.bib_to_connector_item(entry)
    assert item["itemType"] == "journalArticle"
    assert item["title"] == "A Title"
    assert item["creators"] == [
        {"creatorType": "author", "firstName": "Jane", "lastName": "Smith"},
        {"creatorType": "author", "name": "Google DeepMind"},
    ]
    assert item["date"] == "2020"
    assert item["DOI"] == "10.1000/xyz123"
    assert item["url"] == "https://arxiv.org/abs/2101.00001"
    assert item["publicationTitle"] == "J. Things"
    assert item["abstractNote"] == "Short."
    assert item["extra"] == "BibTeX key: k2020"


@pytest.mark.parametrize(
    ("entrytype", "expected"),
    [("article", "journalArticle"), ("inproceedings", "conferencePaper"),
     ("conference", "conferencePaper"), ("misc", "document")],
)
def test_bib_to_connector_item_maps_entry_types(entrytype, expected):
    assert zf.bib_to_connector_item({"entrytype": entrytype, "title": "T"})["itemType"] == expected


# ---------------------------------------------------------------------------
# Dedupe and the main entry point
# ---------------------------------------------------------------------------

def test_existing_keys_skip_attachments_and_lowercase_doi_and_title():
    attachment = {"key": "ATT00001", "data": {"key": "ATT00001", "itemType": "attachment", "title": "Full Text PDF"}}
    fake = FakeTransport({
        ("GET", ITEMS): (200, [
            _item("OLD00001", doi="10.1000/XYZ123", title="Deep Learning for GPT-style Models"),
            attachment,
        ]),
    })
    assert zf.existing_keys("ABCD1234", fake) == {"10.1000/xyz123", "deep learning for gpt-style models"}


def test_dedupe_skips_a_doi_that_already_exists(bib_file):
    fake = FakeTransport(_research_routes(items_payload=[_item("OLD00001", doi="10.1000/xyz123")]))
    result = zf.file_bib_files([bib_file], "Research", transport=fake, dry_run=True, log=lambda m: None)
    assert result == {"status": "dry_run", "collection": "Research", "sent": 1, "added": 0, "skipped_duplicates": 1}
    assert fake.posts() == []


def test_missing_collection_returns_no_collection_and_makes_no_post(bib_file):
    fake = FakeTransport({("GET", COLLECTIONS): (200, [])})
    messages = []
    result = zf.file_bib_files([bib_file], "Research", transport=fake, log=messages.append)
    assert result["status"] == "no_collection"
    assert fake.posts() == []
    assert messages == [
        "Create the collection 'Research' in Zotero first (right-click My Library > New Collection)."
    ]


def test_selection_changed_before_save_sends_nothing(bib_file, monkeypatch):
    monkeypatch.setattr(zf, "open_collection_in_zotero", lambda key: True)
    selections = iter([
        {"id": "C1", "name": "Research", "targets": []},
        {"id": "C2", "name": "Other", "targets": []},
    ])
    fake = FakeTransport({
        ("GET", COLLECTIONS): (200, [_collection("ABCD1234", "Research")]),
        ("GET", ITEMS): (200, []),
        ("POST", SELECTED): lambda url, body: (200, next(selections, {"id": "C2", "name": "Other", "targets": []})),
        ("POST", SAVE): (201, {}),
    })
    result = zf.file_bib_files([bib_file], "Research", transport=fake, log=lambda m: None)
    assert result["status"] == "selection_changed"
    assert result["sent"] == 0
    assert all(call["url"] == SELECTED for call in fake.posts())
    assert not any(call["url"] == SAVE for call in fake.calls)


def test_dry_run_writes_nothing_and_never_opens_zotero(bib_file, monkeypatch):
    opened = []
    monkeypatch.setattr(zf, "open_collection_in_zotero", lambda key: opened.append(key) or True)
    fake = FakeTransport(_research_routes())
    result = zf.file_bib_files([bib_file], "Research", transport=fake, dry_run=True, log=lambda m: None)
    assert result == {"status": "dry_run", "collection": "Research", "sent": 2, "added": 0, "skipped_duplicates": 0}
    assert fake.posts() == []
    assert opened == []


@pytest.mark.parametrize(("appears", "expected_status", "expected_added"), [(True, "ok", 2), (False, "verify_mismatch", 0)])
def test_save_then_verify_counts_new_items(bib_file, monkeypatch, appears, expected_status, expected_added):
    monkeypatch.setattr(zf, "open_collection_in_zotero", lambda key: True)
    saved = {"done": False}
    visible = [
        _item("NEW00001", doi="10.1000/xyz123", title="Deep Learning for GPT-style Models"),
        _item("NEW00002", title="Attention Is All You Need, Revisited", item_type="conferencePaper"),
    ]

    def items_route(url, body):
        return 200, (visible if saved["done"] and appears else [])

    def save_route(url, body):
        saved["done"] = True
        return 201, {}

    fake = FakeTransport({
        ("GET", COLLECTIONS): (200, [_collection("ABCD1234", "Research")]),
        ("GET", ITEMS): items_route,
        ("POST", SELECTED): _selection("Research"),
        ("POST", SAVE): save_route,
    })
    result = zf.file_bib_files([bib_file], "Research", transport=fake, log=lambda m: None)
    assert result == {
        "status": expected_status, "collection": "Research",
        "sent": 2, "added": expected_added, "skipped_duplicates": 0,
    }
    save_calls = [call for call in fake.calls if call["url"] == SAVE]
    assert len(save_calls) == 1
    sent = json.loads(save_calls[0]["body"])["items"]
    assert [item["itemType"] for item in sent] == ["journalArticle", "conferencePaper"]
    assert all(call["headers"]["Zotero-Allowed-Request"] == "1" for call in fake.calls)


def test_zotero_error_mid_run_stops_and_makes_no_save(bib_file):
    fake = FakeTransport({("GET", COLLECTIONS): (403, {"error": "Local API is not enabled"})})
    result = zf.file_bib_files([bib_file], "Research", transport=fake, log=lambda m: None)
    assert result["status"] == "zotero_error"
    assert fake.posts() == []


def test_non_bib_input_is_refused_before_any_post(tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("not bibtex", encoding="utf-8")
    fake = FakeTransport(_research_routes())
    result = zf.file_bib_files([str(notes)], "Research", transport=fake, log=lambda m: None)
    assert result["status"] == "input_error"
    assert fake.posts() == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_dry_run_exits_zero_and_prints_json(bib_file, monkeypatch, capsys):
    fake = FakeTransport(_research_routes())
    monkeypatch.setattr(zf, "_urllib_transport", fake)
    # Auto picks desktop when the local API answers. Fake that so no real Zotero is touched.
    monkeypatch.setattr("meridian.zotero_launcher.zotero_api_alive", lambda *a, **k: True)
    code = zf.main(["file", "--collection", "Research", "--bib", bib_file, "--dry-run"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["status"] == "dry_run"
    assert fake.posts() == []


def test_cli_missing_collection_exits_two(bib_file, monkeypatch, capsys):
    fake = FakeTransport({("GET", COLLECTIONS): (200, [])})
    monkeypatch.setattr(zf, "_urllib_transport", fake)
    # Auto picks desktop when the local API answers. Fake that so no real Zotero is touched.
    monkeypatch.setattr("meridian.zotero_launcher.zotero_api_alive", lambda *a, **k: True)
    code = zf.main(["file", "--collection", "Research", "--bib", bib_file])
    out = json.loads(capsys.readouterr().out)
    assert code == 2
    assert out["status"] == "no_collection"


# ---------------------------------------------------------------------------
# PDF attach on the stored approval only: a filer run never authorizes and never
# opens Zotero's approval dialog. Every fake here fails the test on an unknown request.
# ---------------------------------------------------------------------------

AUTHORIZE = "http://127.0.0.1:23119/api/local/authorize"
WRITE_ITEMS = zf.LOCAL_API + "/items"
SERVER_ID = "SRV-FILER-TEST"
PDF_HEAD = b"%PDF-1.7\n%test\n"
APPROVED_WRITE = (200, {"success": {"0": "ATT00001", "1": "ATT00002"}, "successful": {}, "failed": {}}, {})
FILED_ITEMS = [
    _item("NEW00001", doi="10.1000/xyz123", title="Deep Learning for GPT-style Models"),
    _item("NEW00002", title="Attention Is All You Need, Revisited", item_type="conferencePaper"),
]


class FakeHeaderTransport:
    """Header-aware fake. Routes return (status, payload, response_headers). Unmapped requests fail the test."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, body, headers, timeout=None):
        self.calls.append({
            "method": method, "url": url, "body": body, "headers": dict(headers), "timeout": timeout,
        })
        handler = self.routes.get((method, url.split("?", 1)[0]))
        if handler is None:
            raise AssertionError(f"unexpected header request: {method} {url}")
        status, payload, response_headers = handler
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return status, text, dict(response_headers)

    def requests_to(self, method, url):
        return [call for call in self.calls if call["method"] == method and call["url"] == url]


def _header_routes(post=None, items_probe=None):
    """Server-id probe, the reachability probe that follows a 403, and the attachment write. No authorize route."""
    return {
        ("GET", COLLECTIONS): (200, [], {"Zotero-Server-ID": SERVER_ID}),
        ("GET", zf.LOCAL_API + "/items"): items_probe or (200, [], {}),
        ("POST", WRITE_ITEMS): post or APPROVED_WRITE,
    }


def _filing_routes():
    """The collection starts empty, takes the save, and then lists both filed items with no PDF children."""
    state = {"saved": False}

    def items(url, body):
        return 200, (FILED_ITEMS if state["saved"] else [])

    def save(url, body):
        state["saved"] = True
        return 201, {}

    routes = {
        ("GET", COLLECTIONS): (200, [_collection("ABCD1234", "Research")]),
        ("GET", ITEMS): items,
        ("POST", SELECTED): _selection("Research"),
        ("POST", SAVE): save,
    }
    for item in FILED_ITEMS:
        routes[("GET", zf.LOCAL_API + f"/items/{item['key']}/children")] = (200, [])
    return routes


def _existing_routes():
    """The collection already holds both items, so attach-existing has something to match."""
    routes = {
        ("GET", COLLECTIONS): (200, [_collection("ABCD1234", "Research")]),
        ("GET", ITEMS): (200, FILED_ITEMS),
    }
    for item in FILED_ITEMS:
        routes[("GET", zf.LOCAL_API + f"/items/{item['key']}/children")] = (200, [])
    return routes


@pytest.fixture
def pdf_dir(tmp_path):
    directory = tmp_path / "pdfs"
    directory.mkdir()
    for citekey in ("smith2020deep", "lee2021attention"):
        (directory / f"{citekey}.pdf").write_bytes(PDF_HEAD)
    return directory


@pytest.fixture
def no_zotero_ui(monkeypatch):
    """Keep the desktop selection step out of these runs. Nothing here opens Zotero."""
    monkeypatch.setattr(zf, "open_collection_in_zotero", lambda key: False)


def test_no_stored_approval_skips_the_attach_step_with_no_authorize_and_one_connect_line(
        bib_file, pdf_dir, _stored_zotero_approval, no_zotero_ui):
    _stored_zotero_approval.key = None
    library = FakeTransport(_existing_routes())
    header = FakeHeaderTransport({})  # any header request at all fails the test
    messages = []
    result = zf.attach_existing_files(
        [bib_file], "Research", pdf_dir, transport=library, header_transport=header, log=messages.append,
    )
    assert result["status"] == "needs_connection"
    assert result["attachments"]["status"] == "needs_connection"
    assert result["attachments"]["attached"] == []
    assert sorted(result["attachments"]["failed"]) == ["lee2021attention", "smith2020deep"]
    assert header.calls == []
    assert messages.count(zc.CONNECT_FIRST_LINE) == 1
    assert library.posts() == []


def test_stored_approval_attach_writes_with_the_approval_header_and_never_authorizes(
        bib_file, pdf_dir, _stored_zotero_approval, no_zotero_ui):
    approval = _stored_zotero_approval.key
    library = FakeTransport(_filing_routes())
    header = FakeHeaderTransport(_header_routes())
    result = zf.file_bib_files(
        [bib_file], "Research", transport=library, header_transport=header,
        pdf_dir=str(pdf_dir), log=lambda message: None,
    )
    attachments = result["attachments"]
    assert result["status"] == "ok"
    assert attachments["status"] == "ok"
    assert attachments["attached"] == ["smith2020deep", "lee2021attention"]
    assert all(call["url"].split("?", 1)[0] != AUTHORIZE for call in header.calls)
    assert header.requests_to("POST", AUTHORIZE) == []
    write = header.requests_to("POST", WRITE_ITEMS)[0]
    assert write["headers"]["Authorization"] == f"Bearer {approval}"
    assert write["headers"]["Zotero-Server-ID"] == SERVER_ID
    assert "Zotero-API-Key" not in write["headers"]
    assert approval not in json.dumps(result)


@pytest.mark.parametrize("answer", [
    (401, {"error": "not approved"}, {}),
    (403, {"error": "denied"}, {}),
], ids=["401", "403-probe-reachable"])
def test_stored_key_rejected_with_401_or_403_is_deleted_and_never_reauthorized(
        bib_file, pdf_dir, _stored_zotero_approval, no_zotero_ui, answer):
    library = FakeTransport(_filing_routes())
    header = FakeHeaderTransport(_header_routes(post=answer))
    messages = []
    result = zf.file_bib_files(
        [bib_file], "Research", transport=library, header_transport=header,
        pdf_dir=str(pdf_dir), log=messages.append,
    )
    attachments = result["attachments"]
    assert result["status"] == "ok"
    assert attachments["status"] == "needs_connection"
    assert attachments["attached"] == []
    assert _stored_zotero_approval.key is None
    assert _stored_zotero_approval.delete_calls == 1
    assert header.requests_to("POST", AUTHORIZE) == []
    assert len(header.requests_to("POST", WRITE_ITEMS)) == 1
    assert messages.count(zc.CONNECT_FIRST_LINE) == 1


def test_a_403_while_the_local_api_is_off_keeps_the_stored_key(
        bib_file, pdf_dir, _stored_zotero_approval, no_zotero_ui):
    approval = _stored_zotero_approval.key
    library = FakeTransport(_filing_routes())
    header = FakeHeaderTransport(_header_routes(
        post=(403, {"error": "denied"}, {}),
        items_probe=(403, "Local API is not enabled", {}),
    ))
    result = zf.file_bib_files(
        [bib_file], "Research", transport=library, header_transport=header,
        pdf_dir=str(pdf_dir), log=lambda message: None,
    )
    assert result["attachments"]["status"] == "zotero_error"
    assert result["attachments"]["attached"] == []
    assert _stored_zotero_approval.key == approval
    assert _stored_zotero_approval.delete_calls == 0
    assert header.requests_to("POST", AUTHORIZE) == []


def test_only_connect_references_the_authorize_endpoint():
    """Every reference to /local/authorize in meridian/ sits in connect(), or is its module-level path constant."""
    package = Path(zc.__file__).resolve().parent
    references = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        owner_of = {}
        for func in ast.walk(tree):
            if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for inner in ast.walk(func):
                    owner_of.setdefault(id(inner), func.name)
        for node in ast.walk(tree):
            hit = (
                (isinstance(node, ast.Constant) and isinstance(node.value, str) and "/local/authorize" in node.value)
                or (isinstance(node, ast.Name) and node.id == "_AUTHORIZE_PATH")
                or (isinstance(node, ast.Attribute) and node.attr == "_AUTHORIZE_PATH")
            )
            if hit:
                references.append((path.name, owner_of.get(id(node), "<module>"), node.lineno))
    assert references, "the authorize endpoint must still be defined in zotero_connect"
    offenders = [
        ref for ref in references
        if not (ref[0] == "zotero_connect.py" and ref[1] in ("connect", "<module>"))
    ]
    assert offenders == [], f"the authorize endpoint is referenced outside connect(): {offenders}"
    assert {ref[1] for ref in references if ref[0] == "zotero_connect.py" and ref[1] != "<module>"} == {"connect"}


def test_a_request_carrying_the_approval_is_never_redirected_off_this_machine():
    request = urllib.request.Request(
        WRITE_ITEMS, data=b"[]", headers={"Authorization": "Bearer x", "Content-Type": "application/json"},
        method="POST",
    )
    handler = zf._StripKeyOnRedirect()
    assert handler.redirect_request(request, None, 307, "Temporary Redirect", {}, "https://storage.example.test/x") is None
