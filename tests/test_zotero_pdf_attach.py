"""Tests for the PDF attach step of meridian.zotero_filer (desktop backend only).

Every test injects fake transports and a fake approval store, so nothing here
touches the real Zotero app, the network, the OS keyring or a real library. The
approval used below is a dummy value. The attach step uses only the stored
approval: no test here may reach the authorize endpoint, and the fake header
transport has no route for it, so any authorize call fails the test loudly.
The approval only ever travels in the Authorization header.
"""

from __future__ import annotations

import json
import os

import pytest

from meridian import zotero_connect as zc
from meridian import zotero_filer as zf

COLLECTIONS = zf.LOCAL_API + "/collections"
ITEMS = zf.LOCAL_API + "/collections/ABCD1234/items"
SELECTED = zf.CONNECTOR + "/getSelectedCollection"
SAVE = zf.CONNECTOR + "/saveItems"
AUTHORIZE = "http://127.0.0.1:23119/api/local/authorize"
WRITE_ITEMS = zf.LOCAL_API + "/items"
SERVER_ID = "SRV-TEST-1"
KEY = "DUMMYKEY0123456789ABCDEF"  # fake approval, never a real credential
FILED_KEYS = ("NEW00001", "NEW00002")
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
PDF_HEAD = b"%PDF-1.7\n%test\n"


class FakeTransport:
    """Read-side fake for the 2-tuple transport. Routes map (METHOD, path) to a response.

    An unmapped request raises AssertionError, so an unexpected Zotero call fails loudly.
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


class FakeHeaderTransport:
    """Fake for the header-aware transport. Routes return (status, payload, response_headers)."""

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
        status, payload, response_headers = handler(url, body) if callable(handler) else handler
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return status, text, dict(response_headers)

    def requests_to(self, method, url):
        return [call for call in self.calls if call["method"] == method and call["url"] == url]


def _collection(key, name):
    return {"key": key, "version": 1, "library": {"type": "user", "id": 0}, "data": {"key": key, "name": name}}


def _item(key, doi="", title="", item_type="journalArticle"):
    return {"key": key, "data": {"key": key, "itemType": item_type, "DOI": doi, "title": title}}


def _selection(name):
    return (200, {"id": "C1", "name": name, "targets": []})


def _filed_items():
    return [
        _item("NEW00001", doi="10.1000/xyz123", title="Deep Learning for GPT-style Models"),
        _item("NEW00002", title="Attention Is All You Need, Revisited", item_type="conferencePaper"),
    ]


def _library(children=None):
    """Read-side fake for a run that files SAMPLE_BIB into 'Research'.

    The collection is empty until the save call, then it lists the two filed items.
    children maps an item key to the child objects its children endpoint returns.
    """
    state = {"saved": False}
    children = children or {}

    def items_route(url, body):
        return 200, (_filed_items() if state["saved"] else [])

    def save_route(url, body):
        state["saved"] = True
        return 201, {}

    routes = {
        ("GET", COLLECTIONS): (200, [_collection("ABCD1234", "Research")]),
        ("GET", ITEMS): items_route,
        ("POST", SELECTED): _selection("Research"),
        ("POST", SAVE): save_route,
    }
    for key in FILED_KEYS:
        routes[("GET", zf.LOCAL_API + f"/items/{key}/children")] = (200, children.get(key, []))
    return FakeTransport(routes)


APPROVED_WRITE = (200, {"success": {"0": "ATT00001", "1": "ATT00002"}, "successful": {}, "failed": {}}, {})


class MemoryApprovalStore:
    """In-memory stand-in for the OS keyring approval store. Counts deletes."""

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
def approval(monkeypatch):
    """The stored approval every test here sees. A test that needs none sets key to None.

    KeyringApprovalStore is replaced for the whole file, so the OS keyring is never read.
    """
    store = MemoryApprovalStore(KEY)
    monkeypatch.setattr(zc, "KeyringApprovalStore", lambda: store)
    return store


def _attach(post=None, probe=None, items_probe=None):
    """Header-side fake: the server-id probe, the reachability probe used after a 403, and the attachment write.

    There is deliberately no route for the authorize endpoint.
    """
    routes = {
        ("GET", COLLECTIONS): probe or (200, [], {"Zotero-Server-ID": SERVER_ID}),
        ("GET", zf.LOCAL_API + "/items"): items_probe or (200, [], {}),
        ("POST", WRITE_ITEMS): post or APPROVED_WRITE,
    }
    return FakeHeaderTransport(routes)


def _write_pdf(directory, citekey, body=PDF_HEAD):
    path = directory / f"{citekey}.pdf"
    path.write_bytes(body)
    return path


def _posix_path(path):
    """Absolute path with forward slashes, the form the desktop API uses for linked files."""
    return os.path.abspath(path).replace("\\", "/")


def _pdf_child(key="CHILDPDF1", *, path=None, link_mode="linked_file"):
    """A PDF child as the desktop children endpoint lists it. path is set only when given."""
    data = {"key": key, "itemType": "attachment", "contentType": "application/pdf", "linkMode": link_mode}
    if path is not None:
        data["path"] = path
    return {"key": key, "data": data}


@pytest.fixture
def bib_file(tmp_path):
    path = tmp_path / "refs.bib"
    path.write_text(SAMPLE_BIB, encoding="utf-8")
    return str(path)


@pytest.fixture
def pdf_dir(tmp_path):
    directory = tmp_path / "pdfs"
    directory.mkdir()
    return directory


@pytest.fixture(autouse=True)
def _never_open_zotero(monkeypatch):
    monkeypatch.setattr(zf, "open_collection_in_zotero", lambda key: True)


def _run(bib_file, library, header, pdf=None, dry_run=False, messages=None):
    return zf.file_bib_files(
        [bib_file],
        "Research",
        transport=library,
        header_transport=header,
        dry_run=dry_run,
        pdf_dir=None if pdf is None else str(pdf),
        log=(messages if messages is not None else []).append,
    )


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------

def test_without_pdf_dir_the_attach_step_never_runs(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    header = _attach()
    result = _run(bib_file, _library(), header, pdf=None)
    assert result == {"status": "ok", "collection": "Research", "sent": 2, "added": 2, "skipped_duplicates": 0}
    assert header.calls == []


def test_cli_passes_pdf_dir_to_the_desktop_backend(bib_file, pdf_dir, monkeypatch, capsys):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    monkeypatch.setattr(zf, "_urllib_transport", _library())
    monkeypatch.setattr(zf, "_urllib_transport_with_headers", _attach())
    code = zf.main([
        "file", "--collection", "Research", "--bib", bib_file, "--backend", "desktop", "--pdf-dir", str(pdf_dir),
    ])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["attachments"]["attached"] == ["smith2020deep", "lee2021attention"]


def test_cli_web_backend_reports_that_pdf_attach_did_not_run(bib_file, pdf_dir, monkeypatch, capsys):
    monkeypatch.setattr(zf, "web_credentials", lambda env=None: ("1234567", "dummy"))
    monkeypatch.setattr(
        zf,
        "file_bib_files_web",
        lambda *a, **k: {"status": "ok", "collection": "Research", "sent": 2, "added": 2, "skipped_duplicates": 0},
    )
    code = zf.main(["file", "--collection", "Research", "--bib", bib_file, "--backend", "web", "--pdf-dir", str(pdf_dir)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["attachments"]["status"] == "not_run"


def test_cli_reports_not_run_when_the_filing_itself_did_not_succeed(bib_file, pdf_dir, monkeypatch, capsys):
    monkeypatch.setattr(zf, "_urllib_transport", FakeTransport({("GET", COLLECTIONS): (200, [])}))
    monkeypatch.setattr(zf, "_urllib_transport_with_headers", _attach())
    code = zf.main([
        "file", "--collection", "Research", "--bib", bib_file, "--backend", "desktop", "--pdf-dir", str(pdf_dir),
    ])
    out = json.loads(capsys.readouterr().out)
    assert code == 2
    assert out["status"] == "no_collection"
    assert out["attachments"]["status"] == "not_run"


# ---------------------------------------------------------------------------
# Dry run and planning
# ---------------------------------------------------------------------------

def test_dry_run_plans_attachments_but_never_authorizes_or_posts(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    library = _library()
    header = _attach()
    result = _run(bib_file, library, header, pdf=pdf_dir, dry_run=True)
    assert result["status"] == "dry_run"
    assert result["attachments"] == {"status": "dry_run", "planned": ["smith2020deep", "lee2021attention"], "no_pdf": []}
    assert header.calls == []
    assert library.posts() == []


def test_dry_run_lists_entries_without_a_usable_pdf_as_no_pdf(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "lee2021attention", body=b"<html>not a pdf</html>")
    result = _run(bib_file, _library(), _attach(), pdf=pdf_dir, dry_run=True)
    assert result["attachments"] == {"status": "dry_run", "planned": [], "no_pdf": ["smith2020deep", "lee2021attention"]}


@pytest.mark.parametrize("citekey", ["", "../escape", "sub/dir", ".."])
def test_citekeys_that_are_not_plain_file_names_never_resolve_to_a_pdf(pdf_dir, citekey):
    _write_pdf(pdf_dir.parent, "escape")
    assert zf._pdf_for_entry({"citekey": citekey}, pdf_dir) is None


# ---------------------------------------------------------------------------
# Skips: missing or invalid files, and items that already have a PDF
# ---------------------------------------------------------------------------

def test_missing_and_non_pdf_files_are_skipped_without_authorizing(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "lee2021attention", body=b"<html>not a pdf</html>")
    header = _attach()
    result = _run(bib_file, _library(), header, pdf=pdf_dir)
    assert result["status"] == "ok"
    assert result["attachments"]["no_pdf"] == ["smith2020deep", "lee2021attention"]
    assert result["attachments"]["attached"] == []
    assert header.calls == []


def test_item_that_already_has_a_pdf_child_is_skipped(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    linked = _write_pdf(pdf_dir.parent, "linked-copy")  # the file the existing child links to
    header = _attach(post=(200, {"success": {"0": "ATT00002"}, "successful": {}, "failed": {}}, {}))
    children = {"NEW00001": [_pdf_child(path=_posix_path(linked))]}
    result = _run(bib_file, _library(children=children), header, pdf=pdf_dir)
    assert result["attachments"]["already_has_pdf"] == ["smith2020deep"]
    assert result["attachments"]["attached"] == ["lee2021attention"]
    assert header.requests_to("POST", AUTHORIZE) == []
    body = json.loads(header.requests_to("POST", WRITE_ITEMS)[0]["body"])
    assert [entry["parentItem"] for entry in body] == ["NEW00002"]


def test_non_pdf_child_does_not_count_as_a_pdf(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    snapshot = {"key": "CHILDSNAP1", "data": {"itemType": "attachment", "contentType": "text/html"}}
    result = _run(bib_file, _library(children={"NEW00001": [snapshot]}), _attach(), pdf=pdf_dir)
    assert result["attachments"]["already_has_pdf"] == []
    assert result["attachments"]["attached"] == ["smith2020deep", "lee2021attention"]


# ---------------------------------------------------------------------------
# A PDF child counts only when its file is present on this machine
# ---------------------------------------------------------------------------

def _children_transport(item_key, children):
    return FakeTransport({("GET", zf.LOCAL_API + f"/items/{item_key}/children"): (200, children)})


@pytest.mark.parametrize("child", [
    _pdf_child(),
    _pdf_child(link_mode="linked_url"),
    _pdf_child(link_mode="imported_file"),
], ids=["linked_file_without_path", "linked_url", "stored_without_storage_path"])
def test_pdf_child_with_no_file_does_not_count(child):
    assert zf._has_pdf_child("NEW00001", _children_transport("NEW00001", [child])) is False


def test_linked_pdf_child_whose_file_is_missing_does_not_count(pdf_dir):
    child = _pdf_child(path=_posix_path(pdf_dir / "moved-away.pdf"))
    assert zf._has_pdf_child("NEW00001", _children_transport("NEW00001", [child])) is False


def test_linked_pdf_child_whose_file_exists_counts(pdf_dir):
    present = _write_pdf(pdf_dir, "linked-copy")
    child = _pdf_child(path=_posix_path(present))
    assert zf._has_pdf_child("NEW00001", _children_transport("NEW00001", [child])) is True


def test_stored_pdf_child_counts_when_zotero_reports_its_storage_path():
    child = _pdf_child(link_mode="imported_file", path="storage:smith2020deep.pdf")
    assert zf._has_pdf_child("NEW00001", _children_transport("NEW00001", [child])) is True


def test_item_whose_linked_pdf_is_missing_gets_a_new_attachment(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    children = {"NEW00001": [_pdf_child(path=_posix_path(pdf_dir / "moved-away.pdf"))]}
    result = _run(bib_file, _library(children=children), _attach(), pdf=pdf_dir)
    assert result["attachments"]["already_has_pdf"] == []
    assert result["attachments"]["attached"] == ["smith2020deep", "lee2021attention"]


# ---------------------------------------------------------------------------
# Stored approval only: one write, stop on refusal, never authorize
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("answer", [
    (401, {"error": "not approved"}, {}),
    (403, {"error": "denied"}, {}),
], ids=["401", "403-probe-reachable"])
def test_stored_approval_rejected_by_zotero_is_deleted_and_the_step_stops(bib_file, pdf_dir, approval, answer):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    header = _attach(post=answer)
    result = _run(bib_file, _library(), header, pdf=pdf_dir)
    attachments = result["attachments"]
    assert attachments["status"] == "needs_connection"
    assert attachments["attached"] == []
    assert sorted(attachments["failed"]) == ["lee2021attention", "smith2020deep"]
    assert approval.key is None
    assert approval.delete_calls == 1
    assert header.requests_to("POST", AUTHORIZE) == []
    assert len(header.requests_to("POST", WRITE_ITEMS)) == 1
    assert result["status"] == "ok"


def test_rate_limited_write_with_stored_approval_is_reported_and_keeps_the_approval(bib_file, pdf_dir, approval):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    header = _attach(post=(429, {"error": "slow down"}, {"Retry-After": "60"}))
    result = _run(bib_file, _library(), header, pdf=pdf_dir)
    attachments = result["attachments"]
    assert attachments["status"] == "rate_limited"
    assert attachments["retry_after"] == "60"
    assert attachments["attached"] == []
    assert approval.key == KEY
    assert approval.delete_calls == 0
    assert header.requests_to("POST", AUTHORIZE) == []


def test_missing_server_id_means_no_write_and_no_authorize(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    header = _attach(probe=(200, [], {}))
    result = _run(bib_file, _library(), header, pdf=pdf_dir)
    assert result["attachments"]["status"] == "no_server_id"
    assert header.requests_to("POST", AUTHORIZE) == []
    assert header.requests_to("POST", WRITE_ITEMS) == []


# ---------------------------------------------------------------------------
# Approved attach: request shape, key handling, per-item reporting
# ---------------------------------------------------------------------------

def test_stored_approval_attach_posts_linked_file_items_with_forward_slash_paths(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    library = _library()
    header = _attach()
    messages = []
    result = _run(bib_file, library, header, pdf=pdf_dir, messages=messages)

    attachments = result["attachments"]
    assert attachments["status"] == "ok"
    assert attachments["attached"] == ["smith2020deep", "lee2021attention"]
    assert attachments["failed"] == []

    assert header.requests_to("POST", AUTHORIZE) == []
    write_call = header.requests_to("POST", WRITE_ITEMS)[0]
    assert write_call["headers"]["Zotero-Server-ID"] == SERVER_ID
    assert write_call["headers"]["Authorization"] == f"Bearer {KEY}"
    assert "Zotero-API-Key" not in write_call["headers"]
    body = json.loads(write_call["body"])
    expected_path = os.path.abspath(pdf_dir / "smith2020deep.pdf").replace("\\", "/")
    assert body[0] == {
        "itemType": "attachment",
        "linkMode": "linked_file",
        "parentItem": "NEW00001",
        "path": expected_path,
        "title": "smith2020deep.pdf",
        "contentType": "application/pdf",
    }
    assert "\\" not in body[0]["path"]
    assert [entry["parentItem"] for entry in body] == ["NEW00001", "NEW00002"]


def test_authorization_key_only_ever_travels_in_request_headers(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    messages = []
    header = _attach()
    result = _run(bib_file, _library(), header, pdf=pdf_dir, messages=messages)
    assert result["attachments"]["status"] == "ok"
    for message in messages:
        assert KEY not in message
    assert KEY not in json.dumps(result)
    for call in header.calls:
        assert KEY not in (call["body"] or b"").decode("utf-8")
        assert call["timeout"] is not None


def test_per_item_write_failures_are_reported_separately(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    post = (200, {"success": {"0": "ATT00001"}, "successful": {}, "failed": {"1": {"code": 400, "message": "bad"}}}, {})
    result = _run(bib_file, _library(), _attach(post=post), pdf=pdf_dir)
    attachments = result["attachments"]
    assert attachments["status"] == "attach_failed"
    assert attachments["attached"] == ["smith2020deep"]
    assert attachments["failed"] == ["lee2021attention"]


def test_a_rejected_write_batch_is_reported_as_failed(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    header = _attach(post=(500, {"error": "boom"}, {}))
    result = _run(bib_file, _library(), header, pdf=pdf_dir)
    attachments = result["attachments"]
    assert attachments["status"] == "attach_failed"
    assert attachments["attached"] == []
    assert sorted(attachments["failed"]) == ["lee2021attention", "smith2020deep"]
    assert len(header.requests_to("POST", WRITE_ITEMS)) == 1


def test_a_write_response_without_a_result_map_is_treated_as_failed(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    result = _run(bib_file, _library(), _attach(post=(200, "ok", {})), pdf=pdf_dir)
    assert result["attachments"]["attached"] == []
    assert sorted(result["attachments"]["failed"]) == ["lee2021attention", "smith2020deep"]


def test_rate_limited_write_batch_stops_and_is_not_retried(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    header = _attach(post=(429, {"error": "slow down"}, {"Retry-After": "30"}))
    result = _run(bib_file, _library(), header, pdf=pdf_dir)
    attachments = result["attachments"]
    assert attachments["status"] == "rate_limited"
    assert attachments["retry_after"] == "30"
    assert attachments["attached"] == []
    assert sorted(attachments["failed"]) == ["lee2021attention", "smith2020deep"]
    assert len(header.requests_to("POST", WRITE_ITEMS)) == 1


def test_a_zotero_error_during_the_attach_step_fails_every_pending_item(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    library = _library()
    library.routes[("GET", zf.LOCAL_API + "/items/NEW00002/children")] = (500, {"error": "boom"})
    header = _attach()
    messages = []
    result = _run(bib_file, library, header, pdf=pdf_dir, messages=messages)
    attachments = result["attachments"]
    assert attachments["status"] == "zotero_error"
    assert attachments["attached"] == []
    assert sorted(attachments["failed"]) == ["lee2021attention", "smith2020deep"]
    assert header.calls == []  # no probe and no authorize once the read failed
    assert any("Zotero error during the PDF attach" in message for message in messages)


@pytest.mark.parametrize("citekey", ["a:stream", "name with space/..", "tab\name"])
def test_more_unsafe_citekeys_never_resolve(pdf_dir, citekey):
    assert zf._pdf_for_entry({"citekey": citekey}, pdf_dir) is None


class _FakeResponse:
    def __init__(self, status, body, headers):
        self.status = status
        self._body = body
        self.headers = headers

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_real_header_transport_returns_status_body_and_headers(monkeypatch):
    import email.message
    import io
    import urllib.error

    message = email.message.Message()
    message["Zotero-Server-ID"] = SERVER_ID

    class OkOpener:
        def open(self, request, timeout):
            assert timeout == 7.0
            assert request.get_method() == "GET"
            return _FakeResponse(200, b"[]", message)

    monkeypatch.setattr(zf, "_OPENER", OkOpener())
    status, text, headers = zf._urllib_transport_with_headers("GET", COLLECTIONS, None, {}, timeout=7.0)
    assert (status, text) == (200, "[]")
    assert headers["Zotero-Server-ID"] == SERVER_ID

    error_headers = email.message.Message()
    error_headers["Retry-After"] = "5"

    class ErrorOpener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many", error_headers, io.BytesIO(b"slow"))

    monkeypatch.setattr(zf, "_OPENER", ErrorOpener())
    status, text, headers = zf._urllib_transport_with_headers("POST", WRITE_ITEMS, b"{}", {})
    assert (status, text) == (429, "slow")
    assert headers["Retry-After"] == "5"
