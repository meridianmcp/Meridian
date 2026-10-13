"""Tests for the Web API attach path (--attach-existing --backend web).

Every test injects a fake header transport. An autouse guard makes the real
transports, the desktop selector and the desktop launcher fail loudly, so
nothing here reaches api.zotero.org, the local Zotero app or the library.
The API key is a dummy value. Each test clears the Zotero environment
variables first, so the host's real keys are never read.
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse

import pytest

from meridian import zotero_filer as zf

USER = "1234567"
KEY = "DUMMYWEBKEY0123456789ABCDEFGH"  # fake key, never a real credential
COLL_NAME = "Research"
COLL_KEY = "5K9YPMD8"
API = zf.WEB_API
KEYS_URL = f"{API}/keys/current"
COLLECTIONS_URL = f"{API}/users/{USER}/collections?format=json&limit=100&start=0"
TOP_URL = f"{API}/users/{USER}/collections/{COLL_KEY}/items/top?format=json&limit=100&start=0"
WRITE_URL = f"{API}/users/{USER}/items"
UPLOAD_URL = "https://storage.example.test/upload/abc"  # https, a test host
UPLOAD_CONTENT_TYPE = "multipart/form-data; boundary=zzz"
PREFIX = "--prefix-part--"
SUFFIX = "--suffix-part--"
PDF_BYTES = b"%PDF-1.7\n%web attach test\n"
PDF_MD5 = hashlib.md5(PDF_BYTES).hexdigest()

KEY_RESPONSE = {
    "key": KEY,  # the real endpoint echoes the key, so the code must never return or log this body
    "userID": int(USER),
    "username": "tester",
    "access": {
        "user": {"library": True, "files": True, "notes": True, "write": True},
        "groups": {"all": {"library": True, "write": True}},
    },
}
COLLECTION_ROW = {"key": COLL_KEY, "data": {"key": COLL_KEY, "name": COLL_NAME}}

# Items already in the target collection. smith matches by DOI. lee matches by
# normalised title, because the item has no DOI.
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
  year = 2024,
}
"""


def children_url(item_key):
    return f"{API}/users/{USER}/items/{item_key}/children?format=json&limit=100&start=0"


def file_url(att_key):
    return f"{API}/users/{USER}/items/{att_key}/file"


def ok(payload, status=200, headers=None):
    """A fake response: (status, body text, response headers)."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return status, text, dict(headers or {})


def created(key, shape="success"):
    """A successful item-create response in either Web API shape."""
    if shape == "success":
        return ok({"success": {"0": key}, "successful": {}, "failed": {}})
    return ok({"success": {}, "successful": {"0": {"key": key, "data": {"key": key}}}, "failed": {}})


def authorize(upload_key="UPKEY001", url=UPLOAD_URL):
    return ok({
        "url": url, "contentType": UPLOAD_CONTENT_TYPE,
        "prefix": PREFIX, "suffix": SUFFIX, "uploadKey": upload_key,
    })


class FakeWeb:
    """Scripted header transport. Routes by (method, url) and records every call.

    A route maps to one response or a list of responses, used in order. The last
    response in a list repeats. A call with no route fails the test.
    """

    def __init__(self, routes):
        self.routes = {key: (list(value) if isinstance(value, list) else [value]) for key, value in routes.items()}
        self.calls = []

    def __call__(self, method, url, body, headers, timeout=None):
        self.calls.append({"method": method, "url": url, "body": body, "headers": dict(headers), "timeout": timeout})
        queue = self.routes.get((method, url))
        if not queue:
            raise AssertionError(f"unexpected request: {method} {url}")
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def posts(self, url=None):
        return [c for c in self.calls if c["method"] == "POST" and (url is None or c["url"] == url)]


def standard_routes(top_items=None, key_response=None):
    routes = {
        ("GET", KEYS_URL): ok(key_response if key_response is not None else KEY_RESPONSE),
        ("GET", COLLECTIONS_URL): ok([COLLECTION_ROW]),
        ("GET", TOP_URL): ok(top_items if top_items is not None else EXIST_ITEMS),
        ("GET", children_url("EXIST001")): ok([]),
        ("GET", children_url("EXIST002")): ok([]),
    }
    return routes


def upload_routes(att_keys=("ATTKEY01", "ATTKEY02"), upload_keys=("UPKEY001", "UPKEY002")):
    """Routes for a full successful upload of the two standard entries."""
    routes = {
        ("POST", WRITE_URL): [created(att_keys[0]), created(att_keys[1], shape="successful")],
        ("POST", UPLOAD_URL): ok("", status=201),
    }
    for att_key, upload_key in zip(att_keys, upload_keys):
        routes[("POST", file_url(att_key))] = [authorize(upload_key), ok("", status=204)]
    return routes


def merged(*parts):
    out = {}
    for part in parts:
        out.update(part)
    return out


def write_inputs(tmp_path, bib_text=THREE_ENTRY_BIB, pdf_keys=("smith2020deep", "lee2021attention")):
    bib = tmp_path / "refs.bib"
    bib.write_text(bib_text, encoding="utf-8")
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    for citekey in pdf_keys:
        (pdfs / f"{citekey}.pdf").write_bytes(PDF_BYTES)
    return bib, pdfs


def attach(bib, pdfs, transport, dry_run=False, messages=None, sleep=None):
    log = messages.append if messages is not None else (lambda message: None)
    waits = sleep if sleep is not None else []
    return zf.attach_existing_web(
        [str(bib)], COLL_NAME, str(pdfs), USER, KEY,
        transport=transport, dry_run=dry_run, log=log,
        sleep=(lambda seconds: waits.append(seconds)),
    )


@pytest.fixture(autouse=True)
def _no_real_zotero(monkeypatch):
    """Any test that forgets to inject a fake fails here instead of touching Zotero or the network."""
    def refuse(*args, **kwargs):
        raise AssertionError("a real transport, launcher or selector must not be used in this test")

    monkeypatch.setattr(zf, "_urllib_transport", refuse)
    monkeypatch.setattr(zf, "_urllib_transport_with_headers", refuse)
    monkeypatch.setattr(zf, "open_collection_in_zotero", refuse)
    monkeypatch.setattr(zf, "wait_for_selection", refuse)
    monkeypatch.setattr(zf.zotero_launcher, "start_zotero", refuse)
    monkeypatch.setattr(zf.zotero_launcher, "zotero_api_alive", refuse)


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("ZOTERO_USER_ID", "ZOTERO_API_KEY", "ZOTERO_SECOND_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def test_web_attach_credentials_prefer_the_second_key_then_fall_back(clean_env):
    both = {"ZOTERO_USER_ID": USER, "ZOTERO_API_KEY": "FIRSTKEY", "ZOTERO_SECOND_API_KEY": "SECONDKEY"}
    assert zf.web_attach_credentials(both) == (USER, "SECONDKEY", "ZOTERO_SECOND_API_KEY")
    only_first = {"ZOTERO_USER_ID": USER, "ZOTERO_API_KEY": "FIRSTKEY"}
    assert zf.web_attach_credentials(only_first) == (USER, "FIRSTKEY", "ZOTERO_API_KEY")


def test_web_attach_credentials_need_a_numeric_user_id_and_a_key(clean_env):
    assert zf.web_attach_credentials({"ZOTERO_USER_ID": "not-a-number", "ZOTERO_API_KEY": KEY}) is None
    assert zf.web_attach_credentials({"ZOTERO_USER_ID": USER}) is None


# ---------------------------------------------------------------------------
# Key checks (step 1)
# ---------------------------------------------------------------------------

def test_key_for_another_user_is_refused_before_any_library_call(tmp_path):
    bib, pdfs = write_inputs(tmp_path)
    other_user = dict(KEY_RESPONSE, userID=7654321)
    fake = FakeWeb(standard_routes(key_response=other_user))
    result = attach(bib, pdfs, fake)
    assert result["status"] == "key_user_mismatch"
    assert [c["url"] for c in fake.calls] == [KEYS_URL]
    assert result["attachments"]["planned"] == []


@pytest.mark.parametrize("mutate", [
    lambda body: body["access"]["user"].update(write=False),
    lambda body: body["access"]["user"].pop("write"),
    lambda body: body["access"]["user"].update(files=False),
    lambda body: body["access"]["user"].pop("files"),
])
def test_key_without_write_or_files_access_is_refused(tmp_path, mutate):
    bib, pdfs = write_inputs(tmp_path)
    body = json.loads(json.dumps(KEY_RESPONSE))
    mutate(body)
    fake = FakeWeb(standard_routes(key_response=body))
    result = attach(bib, pdfs, fake)
    assert result["status"] == "key_insufficient"
    assert [c["url"] for c in fake.calls] == [KEYS_URL]


def test_refused_key_stops_at_once_and_never_echoes_the_key(tmp_path):
    bib, pdfs = write_inputs(tmp_path)
    fake = FakeWeb({("GET", KEYS_URL): ok("Invalid key " + KEY, status=403)})
    messages = []
    result = attach(bib, pdfs, fake, messages=messages)
    assert result["status"] == "key_refused"
    assert len(fake.calls) == 1
    assert KEY not in json.dumps(result)
    assert all(KEY not in message for message in messages)


# ---------------------------------------------------------------------------
# Collection lookup and planning (steps 2 to 4)
# ---------------------------------------------------------------------------

def test_no_collection_with_that_name_stops_before_listing_items(tmp_path):
    bib, pdfs = write_inputs(tmp_path)
    other = {"key": "OTHERKEY", "data": {"key": "OTHERKEY", "name": "Something else"}}
    fake = FakeWeb({("GET", KEYS_URL): ok(KEY_RESPONSE), ("GET", COLLECTIONS_URL): ok([other])})
    result = attach(bib, pdfs, fake)
    assert result["status"] == "no_collection"
    assert all(c["url"] != TOP_URL for c in fake.calls)


def test_two_collections_with_the_same_name_stop_the_run(tmp_path):
    bib, pdfs = write_inputs(tmp_path)
    twin = {"key": "TWINKEY1", "data": {"key": "TWINKEY1", "name": COLL_NAME}}
    fake = FakeWeb({
        ("GET", KEYS_URL): ok(KEY_RESPONSE),
        ("GET", COLLECTIONS_URL): ok([COLLECTION_ROW, twin]),
    })
    result = attach(bib, pdfs, fake)
    assert result["status"] == "ambiguous_collection"
    assert fake.posts() == []


def test_dry_run_is_get_only_and_plans_without_writing(tmp_path):
    bib, pdfs = write_inputs(tmp_path)
    fake = FakeWeb(standard_routes())
    result = attach(bib, pdfs, fake, dry_run=True)
    assert result["status"] == "dry_run"
    assert all(c["method"] == "GET" for c in fake.calls)
    assert result["attachments"]["planned"] == ["smith2020deep", "lee2021attention"]
    assert result["attachments"]["unmatched"] == ["orphan2024"]


def test_doi_and_title_matching_with_the_conflicting_doi_rule(tmp_path):
    items = [
        {"key": "ITEMA001", "data": {"key": "ITEMA001", "itemType": "journalArticle",
                                     "DOI": "10.1/aaa", "title": "Alpha Study"}},
        {"key": "ITEMB002", "data": {"key": "ITEMB002", "itemType": "journalArticle",
                                     "DOI": "", "title": "Lone Title"}},
        {"key": "ITEMC003", "data": {"key": "ITEMC003", "itemType": "journalArticle",
                                     "DOI": "10.2/ccc", "title": "Same Name"}},
    ]
    bib_text = """
@article{alpha2020, title = {Alpha Study}, doi = {https://doi.org/10.1/AAA}}
@article{beta2021, title = {Lone Title}, doi = {10.4/bbb}}
@article{gamma2022, title = {Same Name}, doi = {10.3/zzz}}
"""
    bib, pdfs = write_inputs(tmp_path, bib_text=bib_text, pdf_keys=("alpha2020", "beta2021", "gamma2022"))
    routes = standard_routes(top_items=items)
    routes[("GET", children_url("ITEMA001"))] = ok([])
    routes[("GET", children_url("ITEMB002"))] = ok([])
    fake = FakeWeb(routes)
    result = attach(bib, pdfs, fake, dry_run=True)
    summary = result["attachments"]
    # alpha matches by DOI, beta matches by title because the item has no DOI,
    # and gamma does not match because both sides have conflicting DOIs.
    assert summary["planned"] == ["alpha2020", "beta2021"]
    assert summary["unmatched"] == ["gamma2022"]
    # Only matched parents are checked for PDF children.
    assert children_url("ITEMC003") not in [c["url"] for c in fake.calls]


def test_entry_with_a_pdf_child_is_skipped_and_nothing_is_written(tmp_path):
    bib, pdfs = write_inputs(tmp_path)
    routes = standard_routes()
    routes[("GET", children_url("EXIST001"))] = ok([
        {"key": "CHILDPDF", "data": {"key": "CHILDPDF", "itemType": "attachment",
                                     "contentType": "application/pdf", "linkMode": "imported_file"}},
    ])
    routes.update(upload_routes(att_keys=("ATTKEY02", "ATTKEY03")))
    fake = FakeWeb(routes)
    messages = []
    result = attach(bib, pdfs, fake, messages=messages)
    summary = result["attachments"]
    assert summary["already_has_pdf"] == ["smith2020deep"]
    assert summary["attached"] == ["lee2021attention"]
    assert result["status"] == "ok"
    assert len(fake.posts(WRITE_URL)) == 1  # only lee gets an attachment item


# ---------------------------------------------------------------------------
# Upload flow (step 5)
# ---------------------------------------------------------------------------

def test_full_upload_flow_runs_in_order_with_prefix_and_suffix(tmp_path):
    bib, pdfs = write_inputs(tmp_path)
    fake = FakeWeb(merged(standard_routes(), upload_routes()))
    result = attach(bib, pdfs, fake)
    assert result["status"] == "ok"
    assert result["attachments"]["attached"] == ["smith2020deep", "lee2021attention"]
    assert result["attachments"]["failed"] == []

    order = [(c["method"], c["url"]) for c in fake.calls if c["method"] == "POST"]
    assert order == [
        ("POST", WRITE_URL), ("POST", file_url("ATTKEY01")), ("POST", UPLOAD_URL), ("POST", file_url("ATTKEY01")),
        ("POST", WRITE_URL), ("POST", file_url("ATTKEY02")), ("POST", UPLOAD_URL), ("POST", file_url("ATTKEY02")),
    ]

    create_body = json.loads(fake.posts(WRITE_URL)[0]["body"].decode("utf-8"))
    assert create_body == [{
        "itemType": "attachment", "parentItem": "EXIST001", "linkMode": "imported_file",
        "title": "smith2020deep.pdf", "filename": "smith2020deep.pdf", "contentType": "application/pdf",
        "md5": PDF_MD5, "mtime": create_body[0]["mtime"], "tags": [], "relations": {},
    }]
    assert isinstance(create_body[0]["mtime"], int)

    authorize_form = urllib.parse.parse_qs(fake.posts(file_url("ATTKEY01"))[0]["body"].decode("ascii"))
    assert authorize_form == {
        "md5": [PDF_MD5], "filename": ["smith2020deep.pdf"],
        "filesize": [str(len(PDF_BYTES))], "mtime": [str(create_body[0]["mtime"])],
    }

    upload = fake.posts(UPLOAD_URL)[0]
    assert upload["body"] == PREFIX.encode("utf-8") + PDF_BYTES + SUFFIX.encode("utf-8")
    assert upload["headers"]["Content-Type"] == UPLOAD_CONTENT_TYPE

    register = fake.posts(file_url("ATTKEY01"))[1]
    assert urllib.parse.parse_qs(register["body"].decode("ascii")) == {"upload": ["UPKEY001"]}

    file_posts = [c for c in fake.posts() if c["url"].endswith("/file")]
    assert file_posts and all(c["headers"]["If-None-Match"] == "*" for c in file_posts)
    assert all(c["headers"]["Content-Type"] == "application/x-www-form-urlencoded" for c in file_posts)


def test_zotero_calls_send_the_version_and_key_headers_and_the_storage_host_gets_neither(tmp_path):
    bib, pdfs = write_inputs(tmp_path)
    fake = FakeWeb(merged(standard_routes(), upload_routes()))
    attach(bib, pdfs, fake)
    upload = fake.posts(UPLOAD_URL)[0]
    assert "Zotero-API-Key" not in upload["headers"]
    assert "Zotero-API-Version" not in upload["headers"]
    for call in fake.calls:
        if call["url"].startswith(API):
            assert call["headers"]["Zotero-API-Version"] == "3"
            assert call["headers"]["Zotero-API-Key"] == KEY
            assert KEY not in call["url"]
            assert call["body"] is None or KEY.encode("utf-8") not in call["body"]
    assert all(call["url"].startswith("https://") for call in fake.calls)
    assert all("127.0.0.1" not in call["url"] and "/api/local/" not in call["url"] for call in fake.calls)


def test_exists_response_skips_the_upload_for_that_entry(tmp_path):
    bib, pdfs = write_inputs(tmp_path)
    routes = merged(standard_routes(), upload_routes())
    routes[("POST", file_url("ATTKEY01"))] = [ok({"exists": 1}), ok("", status=204)]
    fake = FakeWeb(routes)
    result = attach(bib, pdfs, fake)
    assert result["attachments"]["attached"] == ["smith2020deep", "lee2021attention"]
    # Only lee's bytes are uploaded, and smith's file is never registered a second time.
    assert len(fake.posts(UPLOAD_URL)) == 1
    assert [c["body"] for c in fake.posts(file_url("ATTKEY01"))] == [
        urllib.parse.urlencode({"md5": PDF_MD5, "filename": "smith2020deep.pdf",
                                "filesize": len(PDF_BYTES),
                                "mtime": json.loads(fake.posts(WRITE_URL)[0]["body"])[0]["mtime"]}).encode("ascii"),
    ]


def test_upload_url_that_is_not_https_is_never_sent_to(tmp_path):
    bib, pdfs = write_inputs(tmp_path, pdf_keys=("smith2020deep",))
    routes = standard_routes()
    routes[("POST", WRITE_URL)] = created("ATTKEY01")
    routes[("POST", file_url("ATTKEY01"))] = authorize(url="http://insecure.example.test/up")
    fake = FakeWeb(routes)
    result = attach(bib, pdfs, fake)
    assert result["attachments"]["failed"] == ["smith2020deep"]
    assert result["attachments"]["failed_steps"] == {"smith2020deep": "authorize"}
    assert all(c["url"] != "http://insecure.example.test/up" for c in fake.calls)


def test_a_failed_upload_names_its_step_and_the_rest_still_run(tmp_path):
    bib, pdfs = write_inputs(tmp_path)
    routes = merged(standard_routes(), upload_routes())
    routes[("POST", UPLOAD_URL)] = [ok("rejected", status=500), ok("", status=201)]
    fake = FakeWeb(routes)
    result = attach(bib, pdfs, fake)
    summary = result["attachments"]
    assert summary["failed"] == ["smith2020deep"]
    assert summary["failed_steps"] == {"smith2020deep": "upload"}
    assert summary["attached"] == ["lee2021attention"]
    assert result["status"] == "attach_failed"


# ---------------------------------------------------------------------------
# Rate limits: one honoured Retry-After, then stop
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("limited_status", [429, 503])
def test_a_limited_request_is_retried_once_after_retry_after(tmp_path, limited_status):
    bib, pdfs = write_inputs(tmp_path)
    routes = merged(standard_routes(), upload_routes())
    routes[("POST", WRITE_URL)] = [
        ok("", status=limited_status, headers={"Retry-After": "7"}),
        created("ATTKEY01"),
        created("ATTKEY02", shape="successful"),
    ]
    fake = FakeWeb(routes)
    waits = []
    result = attach(bib, pdfs, fake, sleep=waits)
    assert waits == [7.0]
    assert len(fake.posts(WRITE_URL)) == 3
    assert result["attachments"]["attached"] == ["smith2020deep", "lee2021attention"]


def test_a_second_limited_answer_stops_the_run_with_no_further_requests(tmp_path):
    bib, pdfs = write_inputs(tmp_path)
    routes = merged(standard_routes(), upload_routes())
    routes[("POST", WRITE_URL)] = [
        ok("", status=429, headers={"Retry-After": "3"}),
        ok("", status=429, headers={"Retry-After": "3"}),
        created("ATTKEY01"),
    ]
    fake = FakeWeb(routes)
    waits = []
    result = attach(bib, pdfs, fake, sleep=waits)
    summary = result["attachments"]
    assert result["status"] == "rate_limited"
    assert summary["retry_after"] == "3"
    assert waits == [3.0]
    assert len(fake.posts(WRITE_URL)) == 2
    assert fake.posts(file_url("ATTKEY01")) == []
    assert summary["attached"] == []
    assert summary["failed"] == ["smith2020deep", "lee2021attention"]
    assert summary["failed_steps"] == {"smith2020deep": "create_item", "lee2021attention": "not_run"}


def test_a_retry_after_too_long_to_wait_stops_without_sleeping(tmp_path):
    bib, pdfs = write_inputs(tmp_path)
    routes = merged(standard_routes(), upload_routes())
    routes[("POST", WRITE_URL)] = ok("", status=429, headers={"Retry-After": "86400"})
    fake = FakeWeb(routes)
    waits = []
    result = attach(bib, pdfs, fake, sleep=waits)
    assert result["status"] == "rate_limited"
    assert waits == []
    assert len(fake.posts(WRITE_URL)) == 1


# ---------------------------------------------------------------------------
# The key stays in headers
# ---------------------------------------------------------------------------

def test_key_never_appears_in_results_logs_or_errors(tmp_path):
    bib, pdfs = write_inputs(tmp_path)
    routes = merged(standard_routes(), upload_routes())
    # The storage host and the register step echo the key back in their bodies.
    routes[("POST", UPLOAD_URL)] = ok("rejected " + KEY, status=500)
    fake = FakeWeb(routes)
    messages = []
    result = attach(bib, pdfs, fake, messages=messages)
    rendered = json.dumps(result)
    assert KEY not in rendered
    assert all(KEY not in message for message in messages)
    assert result["attachments"]["failed_steps"] == {
        "smith2020deep": "upload", "lee2021attention": "upload",
    }
    for call in fake.calls:
        assert KEY not in call["url"]
        if call["body"] is not None:
            assert KEY.encode("utf-8") not in call["body"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_dry_run_uses_the_web_backend_and_names_only_the_key_variable(tmp_path, clean_env, capsys):
    bib, pdfs = write_inputs(tmp_path)
    clean_env.setenv("ZOTERO_USER_ID", USER)
    clean_env.setenv("ZOTERO_SECOND_API_KEY", KEY)
    fake = FakeWeb(standard_routes())
    clean_env.setattr(zf, "_urllib_transport_with_headers", fake)
    code = zf.main([
        "file", "--collection", COLL_NAME, "--bib", str(bib), "--backend", "web",
        "--attach-existing", "--pdf-dir", str(pdfs), "--dry-run",
    ])
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert code == 0
    assert out["status"] == "dry_run"
    assert out["mode"] == "attach_existing_web"
    assert out["attachments"]["planned"] == ["smith2020deep", "lee2021attention"]
    assert "ZOTERO_SECOND_API_KEY" in captured.err
    assert KEY not in captured.out and KEY not in captured.err
    assert all(c["method"] == "GET" for c in fake.calls)


def test_cli_without_credentials_refuses_before_any_web_call(tmp_path, clean_env, capsys):
    bib, pdfs = write_inputs(tmp_path)
    clean_env.setenv("ZOTERO_USER_ID", USER)  # no key in the environment
    fake = FakeWeb(standard_routes())
    clean_env.setattr(zf, "_urllib_transport_with_headers", fake)
    code = zf.main([
        "file", "--collection", COLL_NAME, "--bib", str(bib), "--backend", "web",
        "--attach-existing", "--pdf-dir", str(pdfs),
    ])
    out = json.loads(capsys.readouterr().out)
    assert code == 2
    assert out["status"] == "no_credentials"
    assert fake.calls == []
