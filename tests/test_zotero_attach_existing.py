"""Tests for --attach-existing: PDFs go to items ALREADY in the target collection.

Every test injects fake transports. An autouse guard makes the real Zotero
transport, the collection selector and the save path fail loudly, so nothing
here can reach the real app or library. The authorization key is a dummy value
and must only ever travel in request headers.
"""

from __future__ import annotations

import json
import os

import pytest

from meridian import zotero_filer as zf

COLLECTIONS = zf.LOCAL_API + "/collections"
ITEMS = zf.LOCAL_API + "/collections/ABCD1234/items"
AUTHORIZE = "http://127.0.0.1:23119/api/local/authorize"
WRITE_ITEMS = zf.LOCAL_API + "/items"
SERVER_ID = "SRV-TEST-1"
KEY = "DUMMYKEY0123456789ABCDEF"  # fake key, never a real credential
EXIST_KEYS = ("EXIST001", "EXIST002")
APPROVED_WRITE = (200, {"success": {"0": "ATT00001", "1": "ATT00002"}, "successful": {}, "failed": {}}, {})
PDF_HEAD = b"%PDF-1.7\n%test\n"

# Items already sitting in the target collection. smith matches by DOI only (titles differ),
# lee matches by normalised title only (no DOI on the item).
EXIST_ITEMS = [
    {"key": "EXIST001", "data": {
        "key": "EXIST001", "itemType": "journalArticle", "DOI": "10.1000/xyz123",
        "title": "Deep learning for GPT-style models (preprint)",
    }},
    {"key": "EXIST002", "data": {
        "key": "EXIST002", "itemType": "conferencePaper", "DOI": "",
        "title": "attention  is all you need, revisited",
    }},
]

THREE_ENTRY_BIB = """
@article{smith2020deep,
  title = {Deep {Learning} for {GPT}-style {Models}},
  author = {Smith, Jane},
  year = {2020},
  doi = {https://doi.org/10.1000/XYZ123},
}

@inproceedings{lee2021attention,
  author = "Lee, Min and Park, Ho",
  title = "Attention {Is} All You Need, Revisited",
  booktitle = {Proceedings of Things},
  year = 2021,
}

@article{orphan2024,
  title = {A Paper Nobody Has Filed},
  doi = {10.9999/none},
  year = {2024},
}
"""

TWO_ENTRY_BIB = """
@article{smith2020deep,
  title = {Deep {Learning} for {GPT}-style {Models}},
  year = {2020},
  doi = {https://doi.org/10.1000/XYZ123},
}

@inproceedings{lee2021attention,
  title = "Attention {Is} All You Need, Revisited",
  year = 2021,
}
"""

REPEATED_DOI_BIB = """
@article{smith2020deep,
  title = {Deep {Learning} for {GPT}-style {Models}},
  year = {2020},
  doi = {10.1000/xyz123},
}

@article{smith2020deep_copy,
  title = {Deep Learning for GPT-style models},
  year = {2020},
  doi = {10.1000/xyz123},
}
"""


class FakeTransport:
    """Read-side fake for the 2-tuple transport. An unmapped request raises AssertionError."""

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


def _library(children=None, items=None, collections=None):
    """Read-side fake. The target collection already holds EXIST_ITEMS. There are no save routes."""
    children = children or {}
    routes = {
        ("GET", COLLECTIONS): (200, [_collection("ABCD1234", "Research")] if collections is None else collections),
        ("GET", ITEMS): (200, EXIST_ITEMS if items is None else items),
    }
    for key in EXIST_KEYS:
        routes[("GET", zf.LOCAL_API + f"/items/{key}/children")] = (200, children.get(key, []))
    return FakeTransport(routes)


def _header(auth=None, post=None, probe=None):
    """Header-side fake: the server-id probe, the one authorize call, and the attachment write."""
    routes = {
        ("GET", COLLECTIONS): probe or (200, [], {"Zotero-Server-ID": SERVER_ID}),
        ("POST", AUTHORIZE): auth or (200, {"key": KEY}, {}),
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


def _pdf_child(key="CHILDPDF", *, path=None, link_mode="linked_file"):
    """A PDF child as the desktop children endpoint lists it. path is set only when given."""
    data = {"key": key, "itemType": "attachment", "contentType": "application/pdf", "linkMode": link_mode}
    if path is not None:
        data["path"] = path
    return {"key": key, "data": data}


def _write_bib(directory, text, name="refs.bib"):
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _patch_desktop(monkeypatch, library, header):
    monkeypatch.setattr(zf, "_urllib_transport", library)
    monkeypatch.setattr(zf, "_urllib_transport_with_headers", header)


@pytest.fixture
def bib_file(tmp_path):
    return _write_bib(tmp_path, THREE_ENTRY_BIB)


@pytest.fixture
def pdf_dir(tmp_path):
    directory = tmp_path / "pdfs"
    directory.mkdir()
    return directory


@pytest.fixture(autouse=True)
def _no_real_zotero(monkeypatch):
    """Any test that forgets to inject a fake fails here instead of touching Zotero."""
    def refuse(*args, **kwargs):
        raise AssertionError("the real Zotero transport or selector must not be used in this test")

    monkeypatch.setattr(zf, "_urllib_transport", refuse)
    monkeypatch.setattr(zf, "_urllib_transport_with_headers", refuse)
    monkeypatch.setattr(zf, "open_collection_in_zotero", refuse)
    monkeypatch.setattr(zf, "wait_for_selection", refuse)


def _run(bib, library, header, pdf_dir, dry_run=False, messages=None):
    return zf.attach_existing_files(
        [bib],
        "Research",
        str(pdf_dir),
        transport=library,
        header_transport=header,
        dry_run=dry_run,
        log=(messages if messages is not None else []).append,
    )


# ---------------------------------------------------------------------------
# Off by default, and the flag is refused without --pdf-dir
# ---------------------------------------------------------------------------

def test_without_the_flag_items_already_in_the_collection_are_never_attached(tmp_path, pdf_dir, monkeypatch, capsys):
    bib = _write_bib(tmp_path, TWO_ENTRY_BIB)
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    library, header = _library(), _header()
    _patch_desktop(monkeypatch, library, header)
    code = zf.main(["file", "--collection", "Research", "--bib", bib, "--backend", "desktop", "--pdf-dir", str(pdf_dir)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["sent"] == 0
    assert out["skipped_duplicates"] == 2
    assert out["attachments"]["status"] == "not_run"
    assert "attached" not in out["attachments"]
    assert header.calls == []
    assert library.posts() == []


def test_attach_existing_without_pdf_dir_is_refused_before_any_call(bib_file, monkeypatch, capsys):
    library, header = _library(), _header()
    _patch_desktop(monkeypatch, library, header)
    with pytest.raises(SystemExit) as refusal:
        zf.main(["file", "--collection", "Research", "--bib", bib_file, "--backend", "desktop", "--attach-existing"])
    assert refusal.value.code == 2
    assert "requires --pdf-dir" in capsys.readouterr().err
    assert library.calls == []
    assert header.calls == []


def test_attach_existing_on_the_web_backend_without_credentials_is_refused_before_any_call(
    bib_file, pdf_dir, monkeypatch, capsys,
):
    for name in ("ZOTERO_USER_ID", "ZOTERO_API_KEY", "ZOTERO_SECOND_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    def refuse(*args, **kwargs):
        raise AssertionError("the Web API must not be used without credentials")

    monkeypatch.setattr(zf, "file_bib_files_web", refuse)
    monkeypatch.setattr(zf, "attach_existing_web", refuse)
    code = zf.main([
        "file", "--collection", "Research", "--bib", bib_file, "--backend", "web",
        "--attach-existing", "--pdf-dir", str(pdf_dir),
    ])
    out = json.loads(capsys.readouterr().out)
    assert code == 2
    assert out["status"] == "no_credentials"


# ---------------------------------------------------------------------------
# Matching and planning
# ---------------------------------------------------------------------------

def test_matches_by_doi_and_by_normalised_title(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    result = _run(bib_file, _library(), _header(), pdf_dir, dry_run=True)
    assert result["attachments"]["planned"] == ["smith2020deep", "lee2021attention"]


def test_matched_item_with_a_valid_pdf_and_no_pdf_child_is_planned(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    result = _run(bib_file, _library(), _header(), pdf_dir, dry_run=True)
    assert result["status"] == "dry_run"
    assert result["attachments"]["already_has_pdf"] == []
    assert result["attachments"]["no_pdf"] == []


def test_repeated_entry_in_the_bib_is_planned_once(tmp_path, pdf_dir):
    bib = _write_bib(tmp_path, REPEATED_DOI_BIB)
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "smith2020deep_copy")
    result = _run(bib, _library(), _header(), pdf_dir, dry_run=True)
    assert result["attachments"]["planned"] == ["smith2020deep"]
    assert result["attachments"]["duplicate_in_bib"] == ["smith2020deep_copy"]


def test_unmatched_entry_sharing_a_title_does_not_block_a_matched_entry(tmp_path, pdf_dir):
    bib = _write_bib(tmp_path, """
@article{smith_preprint,
  title = {Deep Learning for GPT-style Models},
  year = {2019},
  doi = {10.5555/other},
}

@article{smith2020deep,
  title = {Deep {Learning} for {GPT}-style {Models}},
  year = {2020},
  doi = {10.1000/xyz123},
}
""")
    _write_pdf(pdf_dir, "smith_preprint")
    _write_pdf(pdf_dir, "smith2020deep")
    result = _run(bib, _library(), _header(), pdf_dir, dry_run=True)
    assert result["attachments"]["unmatched"] == ["smith_preprint"]
    assert result["attachments"]["planned"] == ["smith2020deep"]
    assert result["attachments"]["duplicate_in_bib"] == []


def test_same_title_with_conflicting_dois_matches_neither_item(tmp_path, pdf_dir):
    """Two items share a title but carry different DOIs. An entry with a third DOI matches neither."""
    bib = _write_bib(tmp_path, """
@article{clash2022,
  title = {Shared Title For Two Papers},
  year = {2022},
  doi = {10.1000/ccc333},
}
""")
    items = [
        {"key": "EXIST001", "data": {
            "key": "EXIST001", "itemType": "journalArticle", "DOI": "10.1000/aaa111",
            "title": "Shared Title For Two Papers",
        }},
        {"key": "EXIST002", "data": {
            "key": "EXIST002", "itemType": "journalArticle", "DOI": "10.1000/bbb222",
            "title": "Shared Title For Two Papers",
        }},
    ]
    _write_pdf(pdf_dir, "clash2022")
    library, header = _library(items=items), _header()
    result = _run(bib, library, header, pdf_dir)
    attachments = result["attachments"]
    assert attachments["unmatched"] == ["clash2022"]
    assert attachments["planned"] == []
    assert attachments["attached"] == []
    assert header.calls == []
    assert library.posts() == []


@pytest.mark.parametrize("doi_line, item_doi", [
    ("  doi = {10.1000/ccc333},\n", ""),
    ("", "10.1000/aaa111"),
])
def test_same_title_matches_when_one_side_has_no_doi(tmp_path, pdf_dir, doi_line, item_doi):
    bib = _write_bib(
        tmp_path,
        "@article{nodoi2023,\n  title = {Shared Title For Two Papers},\n  year = {2023},\n" + doi_line + "}\n",
    )
    items = [{"key": "EXIST001", "data": {
        "key": "EXIST001", "itemType": "journalArticle", "DOI": item_doi,
        "title": "Shared Title For Two Papers",
    }}]
    _write_pdf(pdf_dir, "nodoi2023")
    result = _run(bib, _library(items=items), _header(), pdf_dir, dry_run=True)
    assert result["attachments"]["unmatched"] == []
    assert result["attachments"]["planned"] == ["nodoi2023"]


def test_dry_run_never_authorizes_or_posts(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    library, header = _library(), _header()
    result = _run(bib_file, library, header, pdf_dir, dry_run=True)
    assert result["status"] == "dry_run"
    assert header.calls == []
    assert library.posts() == []


# ---------------------------------------------------------------------------
# Skips: already has a PDF, missing or invalid file
# ---------------------------------------------------------------------------

def test_matched_item_that_already_has_a_pdf_child_is_skipped(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    linked = _write_pdf(pdf_dir.parent, "linked-copy")  # the file the existing child links to
    header = _header(post=(200, {"success": {"0": "ATT00002"}, "successful": {}, "failed": {}}, {}))
    library = _library(children={"EXIST001": [_pdf_child(path=_posix_path(linked))]})
    result = _run(bib_file, library, header, pdf_dir)
    attachments = result["attachments"]
    assert attachments["already_has_pdf"] == ["smith2020deep"]
    assert attachments["attached"] == ["lee2021attention"]
    body = json.loads(header.requests_to("POST", WRITE_ITEMS)[0]["body"])
    assert [entry["parentItem"] for entry in body] == ["EXIST002"]


def test_matched_item_whose_linked_pdf_is_missing_is_attached(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    library = _library(children={"EXIST001": [_pdf_child(path=_posix_path(pdf_dir / "moved-away.pdf"))]})
    result = _run(bib_file, library, _header(), pdf_dir)
    attachments = result["attachments"]
    assert attachments["already_has_pdf"] == []
    assert attachments["attached"] == ["smith2020deep", "lee2021attention"]


def test_missing_or_non_pdf_file_is_skipped_and_nothing_is_authorized(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "lee2021attention", body=b"<html>not a pdf</html>")
    header = _header()
    result = _run(bib_file, _library(), header, pdf_dir)
    attachments = result["attachments"]
    assert result["status"] == "ok"
    assert attachments["no_pdf"] == ["smith2020deep", "lee2021attention"]
    assert attachments["planned"] == []
    assert attachments["attached"] == []
    assert header.calls == []


def test_entry_without_a_pdf_does_not_block_a_later_entry_with_the_same_doi(tmp_path, pdf_dir):
    bib = _write_bib(tmp_path, """
@article{nopdf2020,
  title = {Deep Learning for GPT-style Models},
  year = {2020},
  doi = {10.1000/xyz123},
}

@article{haspdf2020,
  title = {Deep Learning for GPT-style Models},
  year = {2020},
  doi = {10.1000/xyz123},
}
""")
    _write_pdf(pdf_dir, "haspdf2020")
    header = _header()
    result = _run(bib, _library(), header, pdf_dir)
    attachments = result["attachments"]
    assert attachments["no_pdf"] == ["nopdf2020"]
    assert attachments["duplicate_in_bib"] == []
    assert attachments["attached"] == ["haspdf2020"]
    body = json.loads(header.requests_to("POST", WRITE_ITEMS)[0]["body"])
    assert [entry["parentItem"] for entry in body] == ["EXIST001"]


def test_an_entry_not_in_the_collection_is_never_touched(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    _write_pdf(pdf_dir, "orphan2024")
    library, header = _library(), _header()
    result = _run(bib_file, library, header, pdf_dir)
    attachments = result["attachments"]
    assert attachments["unmatched"] == ["orphan2024"]
    for bucket in ("attached", "planned", "already_has_pdf", "no_pdf", "failed"):
        assert "orphan2024" not in attachments[bucket]
    children_urls = [call["url"] for call in library.calls if "/children" in call["url"]]
    assert children_urls and all(any(key in url for key in EXIST_KEYS) for url in children_urls)
    body = json.loads(header.requests_to("POST", WRITE_ITEMS)[0]["body"])
    assert not any("orphan2024" in entry["path"] for entry in body)


# ---------------------------------------------------------------------------
# Live run: one authorize, then the linked-file write
# ---------------------------------------------------------------------------

def test_live_run_authorizes_once_then_writes_linked_files(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    _write_pdf(pdf_dir, "orphan2024")
    library, header = _library(), _header()
    messages = []
    result = _run(bib_file, library, header, pdf_dir, messages=messages)
    attachments = result["attachments"]
    assert result["status"] == "ok"
    assert attachments["attached"] == ["smith2020deep", "lee2021attention"]
    assert attachments["failed"] == []
    assert [call["url"] for call in header.calls] == [COLLECTIONS + "?limit=1", AUTHORIZE, WRITE_ITEMS]
    assert len(header.requests_to("POST", AUTHORIZE)) == 1
    write = header.requests_to("POST", WRITE_ITEMS)[0]
    assert write["headers"]["Zotero-API-Key"] == KEY
    body = json.loads(write["body"])
    assert [entry["parentItem"] for entry in body] == ["EXIST001", "EXIST002"]
    assert all(entry["itemType"] == "attachment" and entry["linkMode"] == "linked_file" for entry in body)
    assert body[0]["title"] == "smith2020deep.pdf"
    assert body[0]["path"].endswith("smith2020deep.pdf")
    assert library.posts() == []
    assert KEY not in json.dumps(result)
    assert KEY not in "\n".join(messages)


def test_live_run_with_nothing_planned_never_authorizes(bib_file, pdf_dir):
    header = _header()
    result = _run(bib_file, _library(), header, pdf_dir)
    assert result["status"] == "ok"
    assert result["attachments"]["planned"] == []
    assert header.calls == []


@pytest.mark.parametrize("auth", [
    (403, {"error": "denied"}, {}),
    (200, {"message": "no key"}, {}),
    (200, "not json", {}),
    (429, {"error": "slow down"}, {"Retry-After": "30"}),
])
def test_refused_or_unanswered_authorize_stops_with_no_write_and_no_retry(bib_file, pdf_dir, auth):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    header = _header(auth=auth)
    result = _run(bib_file, _library(), header, pdf_dir)
    attachments = result["attachments"]
    assert result["status"] in ("not_approved", "rate_limited")
    assert attachments["status"] == result["status"]
    assert attachments["attached"] == []
    assert sorted(attachments["failed"]) == ["lee2021attention", "smith2020deep"]
    assert len(header.requests_to("POST", AUTHORIZE)) == 1
    assert header.requests_to("POST", WRITE_ITEMS) == []
    assert KEY not in json.dumps(result)


def test_refused_write_after_approval_is_reported_and_not_retried(bib_file, pdf_dir):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    header = _header(post=(500, {"error": "boom"}, {}))
    result = _run(bib_file, _library(), header, pdf_dir)
    attachments = result["attachments"]
    assert result["status"] == "attach_failed"
    assert attachments["attached"] == []
    assert sorted(attachments["failed"]) == ["lee2021attention", "smith2020deep"]
    assert len(header.requests_to("POST", AUTHORIZE)) == 1
    assert len(header.requests_to("POST", WRITE_ITEMS)) == 1


# ---------------------------------------------------------------------------
# Stop conditions before any attach
# ---------------------------------------------------------------------------

def test_missing_collection_stops_before_any_attach(bib_file, pdf_dir):
    header = _header()
    result = _run(bib_file, _library(collections=[]), header, pdf_dir)
    assert result["status"] == "no_collection"
    assert header.calls == []


def test_unreadable_bib_input_stops_before_any_attach(tmp_path, pdf_dir):
    not_bib = tmp_path / "refs.txt"
    not_bib.write_text(THREE_ENTRY_BIB, encoding="utf-8")
    header = _header()
    result = _run(str(not_bib), _library(), header, pdf_dir)
    assert result["status"] == "input_error"
    assert header.calls == []


def test_zotero_error_while_listing_the_collection_stops_before_any_attach(bib_file, pdf_dir):
    header = _header()
    result = _run(bib_file, _library(items={"not": "a list"}), header, pdf_dir)
    assert result["status"] == "zotero_error"
    assert header.calls == []


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def test_cli_dry_run_reports_the_plan_and_exits_zero(bib_file, pdf_dir, monkeypatch, capsys):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    library, header = _library(), _header()
    _patch_desktop(monkeypatch, library, header)
    code = zf.main([
        "file", "--collection", "Research", "--bib", bib_file, "--backend", "desktop",
        "--pdf-dir", str(pdf_dir), "--attach-existing", "--dry-run",
    ])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["status"] == "dry_run"
    assert out["attachments"]["planned"] == ["smith2020deep", "lee2021attention"]
    assert header.calls == []
    assert library.posts() == []


def test_cli_live_run_attaches_existing_items(bib_file, pdf_dir, monkeypatch, capsys):
    _write_pdf(pdf_dir, "smith2020deep")
    _write_pdf(pdf_dir, "lee2021attention")
    library, header = _library(), _header()
    _patch_desktop(monkeypatch, library, header)
    code = zf.main([
        "file", "--collection", "Research", "--bib", bib_file, "--backend", "desktop",
        "--pdf-dir", str(pdf_dir), "--attach-existing",
    ])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["status"] == "ok"
    assert out["attachments"]["attached"] == ["smith2020deep", "lee2021attention"]
    assert len(header.requests_to("POST", AUTHORIZE)) == 1
    assert KEY not in json.dumps(out)
