"""Tests for the Zotero Web API backend in meridian.zotero_filer.

Every test uses a fake Web API that records each request. Nothing touches the
real Zotero, the network, or any path outside pytest's tmp_path.
"""

from __future__ import annotations

import json

import pytest

from meridian import zotero_filer as zf

USER_ID = "1234567"
API_KEY = "SECRET-TEST-KEY-do-not-leak"
CS2 = "CS2 CSDS Round Win Probability"
NEW_KEY = "NEWKEY01"

BIB = """\
@article{smith2020,
  title={A Study of Things},
  author={Smith, Jane and Doe, John},
  year={2020},
  doi={10.1000/xyz123},
  journal={Journal of Things}
}
@misc{lee2024,
  title={Another Paper},
  author={Lee, Kim},
  year={2024},
  eprint={2401.00001}
}
"""


class FakeWebApi:
    """Stateful stand-in for api.zotero.org. Records (method, url, payload, headers)."""

    def __init__(self, collections=None, items=None, fail_item_post=None):
        self.collections = list(collections or [])  # [{"key", "name"}]
        self.items = list(items or [])  # [{"key", "data"}]
        self.fail_item_post = fail_item_post  # (status, text) returned for POST /items
        self.calls = []

    def __call__(self, method, url, body, headers):
        payload = json.loads(body) if body else None
        self.calls.append((method, url, payload, dict(headers)))
        if method == "GET" and "/collections?" in url:
            return 200, json.dumps(
                [{"key": c["key"], "data": {"key": c["key"], "name": c["name"]}} for c in self.collections]
            )
        if method == "GET" and "/collections/" in url and "/items?" in url:
            return 200, json.dumps(self.items)
        if method == "POST" and url.endswith("/collections"):
            self.collections.append({"key": NEW_KEY, "name": payload[0]["name"]})
            return 200, json.dumps({"success": {"0": NEW_KEY}, "successful": {}, "failed": {}})
        if method == "POST" and url.endswith("/items"):
            if self.fail_item_post is not None:
                return self.fail_item_post
            ok = {}
            for index, obj in enumerate(payload):
                key = f"ITEM{len(self.items) + index:04d}"
                self.items.append({"key": key, "data": obj})
                ok[str(index)] = key
            return 200, json.dumps({"success": ok, "successful": {}, "failed": {}})
        return 404, ""

    def posts_to(self, suffix):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith(suffix)]


@pytest.fixture
def bib_path(tmp_path):
    path = tmp_path / "sample.bib"
    path.write_text(BIB, encoding="utf-8")
    return str(path)


def _run(bib_path, api, dry_run=False):
    logs: list[str] = []
    result = zf.file_bib_files_web(
        [bib_path], CS2, USER_ID, API_KEY, transport=api, dry_run=dry_run, log=logs.append
    )
    return result, logs


def test_credentials_need_numeric_user_id_and_key():
    assert zf.web_credentials({}) is None
    assert zf.web_credentials({"ZOTERO_USER_ID": "abc", "ZOTERO_API_KEY": "k"}) is None
    assert zf.web_credentials({"ZOTERO_USER_ID": "123", "ZOTERO_API_KEY": ""}) is None
    assert zf.web_credentials({"ZOTERO_USER_ID": " 123 ", "ZOTERO_API_KEY": " k "}) == ("123", "k")


def test_schema_fix_moves_venue_and_doi_for_documents():
    fixed = zf._schema_fix(
        {"itemType": "document", "title": "t", "publicationTitle": "V", "DOI": "10.1/x", "extra": "BibTeX key: a"}
    )
    assert "publicationTitle" not in fixed and "DOI" not in fixed
    assert "Venue: V" in fixed["extra"] and "DOI: 10.1/x" in fixed["extra"]
    assert "BibTeX key: a" in fixed["extra"]


def test_schema_fix_maps_conference_venue_to_proceedings_title():
    fixed = zf._schema_fix({"itemType": "conferencePaper", "title": "t", "publicationTitle": "Proc"})
    assert fixed["proceedingsTitle"] == "Proc"
    assert "publicationTitle" not in fixed


def test_dry_run_writes_nothing(bib_path):
    api = FakeWebApi()
    result, _ = _run(bib_path, api, dry_run=True)
    assert result["status"] == "dry_run"
    assert result["sent"] == 2
    assert api.posts_to("/collections") == []
    assert api.posts_to("/items") == []


def test_creates_missing_collection_and_writes_items(bib_path):
    api = FakeWebApi()
    result, logs = _run(bib_path, api)
    assert result["status"] == "ok", logs
    assert result["sent"] == 2 and result["added"] == 2
    created = api.posts_to("/collections")
    assert len(created) == 1 and created[0][2] == [{"name": CS2}]
    written = api.posts_to("/items")
    assert written, "items must be written"
    for _, _, payload, _ in written:
        assert all(obj["collections"] == [NEW_KEY] for obj in payload)


def test_existing_item_is_skipped_by_doi(bib_path):
    api = FakeWebApi(
        collections=[{"key": "CSKEY001", "name": CS2}],
        items=[{"key": "OLD0001", "data": {"itemType": "journalArticle", "DOI": "10.1000/xyz123", "title": "A Study of Things"}}],
    )
    result, _ = _run(bib_path, api)
    assert result["status"] == "ok"
    assert result["sent"] == 1
    assert result["skipped_duplicates"] == 1
    assert api.posts_to("/collections") == []


def test_api_key_never_appears_in_logs_or_result(bib_path):
    api = FakeWebApi()
    result, logs = _run(bib_path, api)
    assert API_KEY not in json.dumps(result)
    assert all(API_KEY not in line for line in logs)


def test_key_travels_in_header_only(bib_path):
    api = FakeWebApi()
    _run(bib_path, api)
    for method, url, payload, headers in api.calls:
        assert headers.get("Zotero-API-Key") == API_KEY
        assert API_KEY not in url
        assert payload is None or API_KEY not in json.dumps(payload)


def test_forbidden_write_is_reported_not_ok(bib_path):
    api = FakeWebApi(fail_item_post=(403, "forbidden"))
    result, logs = _run(bib_path, api)
    assert result["status"] == "zotero_error"
    assert any("write access" in line for line in logs)


def test_cli_without_credentials_exits_2(monkeypatch, bib_path, capsys):
    monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    code = zf.main(["file", "--collection", CS2, "--bib", bib_path, "--backend", "web"])
    assert code == 2
    assert "no_credentials" in capsys.readouterr().out
