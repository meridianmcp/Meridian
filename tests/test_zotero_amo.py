"""Tests for the Mozilla AMO API client of the Zotero bridge signing (meridian/zotero_amo.py).

Nothing here reaches Mozilla or any other host. urllib is blocked for the whole file, and every transport is a
fake that records its calls. The credentials are fake values, and they are checked to never appear in any output.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import urllib.error
import urllib.request

import pytest

from meridian import zotero_amo as amo

ADDON_ID = "meridian-zotero@usemeridian.us"
KEY_VALUE = "KEY-VALUE-DO-NOT-PRINT-123"
SECRET_VALUE = "SECRET-VALUE-DO-NOT-PRINT-456"
CREDS = {amo.API_KEY_ENV: KEY_VALUE, amo.API_SECRET_ENV: SECRET_VALUE}
NOW = 1_800_000_000.0
DOWNLOAD_URL = "https://addons.mozilla.org/firefox/downloads/file/99/meridian-zotero-0.1.0.xpi"
VERSION_URL = "https://addons.mozilla.org/api/v5/addons/addon/meridian-zotero%40usemeridian.us/versions/0.1.0/"
JWT_SHAPE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _blocked(*args, **kwargs):
        raise RuntimeError("network calls are blocked in tests")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", _blocked)
    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
    monkeypatch.delenv(amo.API_KEY_ENV, raising=False)
    monkeypatch.delenv(amo.API_SECRET_ENV, raising=False)


def _b64decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _verified_payload(token: str, secret: str) -> dict:
    """The payload of ``token``, after checking its HS256 signature with ``secret``. Fails the test when it is wrong."""
    header_part, payload_part, signature_part = token.split(".")
    signing_input = f"{header_part}.{payload_part}".encode("ascii")
    expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    assert hmac.compare_digest(_b64decode(signature_part), expected)
    return json.loads(_b64decode(payload_part))


def _version_record(
    *, version="0.1.0", status="public", signed=True, url=DOWNLOAD_URL, size=None, hash_=None,
) -> dict:
    return {
        "id": 7,
        "version": version,
        "channel": "unlisted",
        "file": {
            "id": 99,
            "status": status,
            "is_mozilla_signed_extension": signed,
            "url": url,
            "size": size,
            "hash": hash_,
        },
    }


class FakeAmo:
    """A stand-in for the AMO transport. It answers the version read and the download, and records every call."""

    def __init__(
        self, record=None, *, version_status=200, body=None, download=b"", download_status=200,
        raise_on_call=None, raise_on_download=None,
    ):
        self.record = record
        self.version_status = version_status
        self.body = body
        self.download = download
        self.download_status = download_status
        self.raise_on_call = raise_on_call
        self.raise_on_download = raise_on_download
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if self.raise_on_call is not None:
            raise self.raise_on_call
        if url == DOWNLOAD_URL:
            if self.raise_on_download is not None:
                raise self.raise_on_download
            return self.download_status, self.download
        if self.body is not None:
            return self.version_status, self.body
        return self.version_status, json.dumps(self.record).encode("utf-8")


def _fetch(fake, env=CREDS):
    return amo.fetch_approved_version(env=env, transport=fake, addon_id=ADDON_ID, version="0.1.0", clock=lambda: NOW)


# ---------------------------------------------------------------------------
# The JWT: HS256, standard library only, short-lived, never the secret
# ---------------------------------------------------------------------------


def test_make_jwt_is_three_base64url_parts_with_a_valid_hs256_signature():
    token = amo.make_jwt(KEY_VALUE, SECRET_VALUE, NOW)
    assert JWT_SHAPE.fullmatch(token)
    header_part = token.split(".")[0]
    assert json.loads(_b64decode(header_part)) == {"alg": "HS256", "typ": "JWT"}
    _verified_payload(token, SECRET_VALUE)


def test_make_jwt_claims_the_api_key_as_issuer_and_a_five_minute_window():
    payload = _verified_payload(amo.make_jwt(KEY_VALUE, SECRET_VALUE, NOW), SECRET_VALUE)
    assert payload["iss"] == KEY_VALUE
    assert payload["iat"] == int(NOW)
    assert 0 < payload["exp"] - payload["iat"] <= 300
    assert isinstance(payload["jti"], str) and len(payload["jti"]) >= 16


def test_make_jwt_has_a_new_unique_id_every_time():
    ids = {_verified_payload(amo.make_jwt(KEY_VALUE, SECRET_VALUE, NOW), SECRET_VALUE)["jti"] for _ in range(5)}
    assert len(ids) == 5


def test_make_jwt_never_puts_the_secret_in_the_token():
    token = amo.make_jwt(KEY_VALUE, SECRET_VALUE, NOW)
    assert SECRET_VALUE not in token
    for part in token.split("."):
        assert SECRET_VALUE not in _b64decode(part).decode("utf-8", "replace")


def test_make_jwt_refuses_an_empty_key_or_secret():
    with pytest.raises(ValueError):
        amo.make_jwt("", SECRET_VALUE, NOW)
    with pytest.raises(ValueError):
        amo.make_jwt(KEY_VALUE, "", NOW)


# ---------------------------------------------------------------------------
# Credentials and the one place the API host is named
# ---------------------------------------------------------------------------


def test_the_credential_variables_are_the_two_web_ext_names():
    assert amo.API_KEY_ENV == "WEB_EXT_API_KEY"
    assert amo.API_SECRET_ENV == "WEB_EXT_API_SECRET"


@pytest.mark.parametrize(
    "env",
    [
        {},
        {amo.API_KEY_ENV: KEY_VALUE},
        {amo.API_SECRET_ENV: SECRET_VALUE},
        {amo.API_KEY_ENV: " ", amo.API_SECRET_ENV: SECRET_VALUE},
    ],
)
def test_read_credentials_needs_both_variables_to_be_non_empty(env):
    assert amo.read_credentials(env) is None


def test_read_credentials_returns_the_stripped_pair():
    env = {amo.API_KEY_ENV: f" {KEY_VALUE} ", amo.API_SECRET_ENV: SECRET_VALUE}
    assert amo.read_credentials(env) == (KEY_VALUE, SECRET_VALUE)


def test_the_api_base_is_the_fixed_production_host_and_is_not_read_from_the_environment():
    assert amo.API_BASE == "https://addons.mozilla.org/api/v5/"


def test_version_detail_url_reads_the_version_by_number_under_the_add_on_guid():
    assert amo.version_detail_url(ADDON_ID, "0.1.0") == VERSION_URL


def test_a_version_with_no_dot_gets_the_v_prefix_so_mozilla_reads_it_as_a_number():
    url = amo.version_detail_url(ADDON_ID, "2")
    assert url == amo.API_BASE + "addons/addon/meridian-zotero%40usemeridian.us/versions/v2/"


# ---------------------------------------------------------------------------
# fetch_approved_version: the headers, the outcomes, and the download
# ---------------------------------------------------------------------------


def test_the_authorization_header_is_the_jwt_scheme_then_a_token_that_verifies():
    fake = FakeAmo(record=_version_record(), download=b"PK-approved")
    assert _fetch(fake).status == amo.FETCHED
    header = fake.calls[0][1]["Authorization"]
    scheme, token = header.split(" ")
    assert scheme == "JWT"
    assert JWT_SHAPE.fullmatch(token)
    payload = _verified_payload(token, SECRET_VALUE)
    assert payload["iss"] == KEY_VALUE
    assert payload["exp"] - payload["iat"] <= 300


def test_the_download_carries_the_same_authorization_header_and_only_the_api_host_is_called():
    fake = FakeAmo(record=_version_record(), download=b"PK-approved")
    _fetch(fake)
    assert [url for url, _ in fake.calls] == [VERSION_URL, DOWNLOAD_URL]
    assert fake.calls[1][1]["Authorization"] == fake.calls[0][1]["Authorization"]
    assert "Accept" not in fake.calls[1][1]
    for url, _ in fake.calls:
        assert url.startswith("https://addons.mozilla.org/")
        assert "upload" not in url


def test_an_approved_signed_file_is_returned_as_its_bytes():
    fake = FakeAmo(record=_version_record(), download=b"PK-approved")
    result = _fetch(fake)
    assert result.status == amo.FETCHED
    assert result.data == b"PK-approved"
    assert result.message is None


@pytest.mark.parametrize(
    "record_kwargs",
    [
        {"status": "unreviewed", "signed": False},
        {"status": "public", "signed": False},
        {"status": "disabled", "signed": True},
        {"url": None},
    ],
    ids=["awaiting_review_unsigned", "approved_but_unsigned", "disabled", "no_download_url"],
)
def test_a_version_that_is_not_approved_signed_and_available_is_not_approved_yet(record_kwargs):
    fake = FakeAmo(record=_version_record(**record_kwargs), download=b"never")
    result = _fetch(fake)
    assert result.status == amo.NOT_APPROVED
    assert result.data is None
    assert len(fake.calls) == 1


def test_a_version_mozilla_does_not_list_yet_is_not_approved_yet():
    fake = FakeAmo(version_status=404)
    result = _fetch(fake)
    assert result.status == amo.NOT_APPROVED
    assert len(fake.calls) == 1


def test_a_response_for_another_version_number_is_a_failed_fetch():
    fake = FakeAmo(record=_version_record(version="0.2.0"), download=b"x")
    result = _fetch(fake)
    assert result.status == amo.FAILED
    assert len(fake.calls) == 1


def test_a_response_that_is_not_json_is_a_failed_fetch():
    fake = FakeAmo(body=b"<html>not json</html>")
    result = _fetch(fake)
    assert result.status == amo.FAILED


@pytest.mark.parametrize(
    "url",
    ["https://evil.example/file.xpi", "http://addons.mozilla.org/firefox/downloads/file/99/x.xpi"],
    ids=["other_host", "plain_http_same_host"],
)
def test_a_download_address_that_is_not_the_api_host_over_https_is_refused_and_the_token_is_not_sent(url):
    fake = FakeAmo(record=_version_record(url=url), download=b"x")
    result = _fetch(fake)
    assert result.status == amo.FAILED
    assert [called for called, _ in fake.calls] == [VERSION_URL]
    assert "evil.example" not in json.dumps({"m": result.message})


def test_a_download_of_the_wrong_size_is_refused():
    fake = FakeAmo(record=_version_record(size=99), download=b"0123456789")
    assert _fetch(fake).status == amo.FAILED


def test_a_download_whose_sha256_is_not_the_declared_one_is_refused():
    fake = FakeAmo(record=_version_record(hash_="sha256:" + "0" * 64), download=b"abc")
    assert _fetch(fake).status == amo.FAILED


def test_a_download_that_matches_its_declared_size_and_sha256_is_returned():
    data = b"abc"
    fake = FakeAmo(
        record=_version_record(size=len(data), hash_="sha256:" + hashlib.sha256(data).hexdigest()), download=data,
    )
    result = _fetch(fake)
    assert result.status == amo.FETCHED
    assert result.data == data


@pytest.mark.parametrize("status", [401, 403, 500, 302], ids=["unauthorised", "forbidden", "server", "redirect"])
def test_an_http_error_on_the_version_read_names_only_the_code(status):
    fake = FakeAmo(version_status=status)
    result = _fetch(fake)
    assert result.status == amo.FAILED
    assert str(status) in result.message
    assert KEY_VALUE not in result.message and SECRET_VALUE not in result.message


def test_an_http_error_on_the_download_is_a_failed_fetch():
    fake = FakeAmo(record=_version_record(), download=b"", download_status=503)
    result = _fetch(fake)
    assert result.status == amo.FAILED
    assert "503" in result.message


def test_no_credentials_means_no_transport_call():
    fake = FakeAmo(record=_version_record(), download=b"x")
    result = _fetch(fake, env={})
    assert result.status == amo.NO_CREDENTIALS
    assert fake.calls == []


def test_no_credentials_with_the_default_transport_never_opens_a_connection(monkeypatch):
    monkeypatch.setattr(amo, "urllib_transport", lambda *a, **k: pytest.fail("no connection without credentials"))
    result = amo.fetch_approved_version(env={}, transport=None, addon_id=ADDON_ID, version="0.1.0")
    assert result.status == amo.NO_CREDENTIALS


def test_a_transport_error_on_the_version_read_is_a_failed_result_with_no_secret_in_it():
    fake = FakeAmo(raise_on_call=OSError(f"connection reset while sending {KEY_VALUE} and {SECRET_VALUE}"))
    result = _fetch(fake)
    text = json.dumps({"status": result.status, "message": result.message})
    assert result.status == amo.FAILED
    assert result.data is None
    assert KEY_VALUE not in text and SECRET_VALUE not in text
    assert "connection reset" not in text


def test_a_transport_error_on_the_download_is_a_failed_result_with_no_secret_in_it():
    fake = FakeAmo(
        record=_version_record(),
        raise_on_download=OSError(f"reset during download with {SECRET_VALUE}"),
    )
    result = _fetch(fake)
    text = json.dumps({"status": result.status, "message": result.message})
    assert result.status == amo.FAILED
    assert SECRET_VALUE not in text


# ---------------------------------------------------------------------------
# The default transport: no cross-host redirect, bounded timeout, GET only
# ---------------------------------------------------------------------------


def test_the_redirect_guard_refuses_a_redirect_to_another_host():
    handler = amo._NoCrossHostRedirect()
    request = urllib.request.Request(VERSION_URL, headers={"Authorization": "JWT a.b.c"})
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/x") is None


def test_the_redirect_guard_follows_a_redirect_that_stays_on_the_api_host():
    handler = amo._NoCrossHostRedirect()
    request = urllib.request.Request(VERSION_URL, headers={"Authorization": "JWT a.b.c"})
    followed = handler.redirect_request(request, None, 302, "Found", {}, "https://addons.mozilla.org/other/")
    assert followed is not None
    assert followed.full_url == "https://addons.mozilla.org/other/"


def test_the_default_transport_sends_the_headers_as_a_get_with_a_timeout_and_the_redirect_guard(monkeypatch):
    seen: dict = {}

    class _Response:
        status = 200

        def read(self, size=None):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Opener:
        def open(self, request, timeout=None):
            seen["method"] = request.get_method()
            seen["timeout"] = timeout
            seen["headers"] = dict(request.header_items())
            return _Response()

    def _build(*handlers):
        seen["handlers"] = handlers
        return _Opener()

    monkeypatch.setattr(urllib.request, "build_opener", _build)
    assert amo.urllib_transport(VERSION_URL, {"Authorization": "JWT a.b.c"}) == (200, b"ok")
    assert seen["method"] == "GET"
    assert seen["timeout"] == amo.REQUEST_TIMEOUT_S
    assert seen["headers"]["Authorization"] == "JWT a.b.c"
    assert amo._NoCrossHostRedirect in seen["handlers"]


def test_the_default_transport_returns_an_http_error_code_without_raising(monkeypatch):
    class _Opener:
        def open(self, request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: _Opener())
    assert amo.urllib_transport(VERSION_URL, {}) == (404, b"")
