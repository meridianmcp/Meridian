"""Tests for meridian_desktop/updater.py. Each test uses tmp_path and a fake transport: no network, no GitHub,
no real %LOCALAPPDATA%. The urllib transport test swaps the opener for a fake, so it makes no request either."""

from __future__ import annotations

import base64
import hashlib
import http.client
import importlib.util
import io
import json
import lzma
import stat
import struct
import sys
import urllib.error
import zipfile
from pathlib import Path

import pytest

import meridian_desktop
from meridian_desktop import ed25519, install, release_key
from meridian_desktop import updater as u
from meridian_desktop.version import APP_VERSION

ZIP_NAME = "MeridianDesktop-0.2.0.zip"
SIG_NAME = "SHA256SUMS.sig"
CDN_URL = "https://objects.githubusercontent.com/github-production-release-asset/1/MeridianDesktop-0.2.0.zip"
# RFC 8032 section 7.1, TEST 1. A test key only: it stands in for the release key, which these tests never touch.
TEST_SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
TEST_PUBLIC = ed25519.public_key(TEST_SEED)
OTHER_SEED = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")  # RFC 8032 TEST 2


def sign_sums(sums: bytes, seed: bytes = TEST_SEED) -> bytes:
    """The SHA256SUMS.sig text for sums: the base64 of the 64-byte signature, as the release publisher writes it."""
    return base64.b64encode(ed25519.sign(seed, sums)) + b"\n"


@pytest.fixture(autouse=True)
def test_release_key(monkeypatch):
    """Every test verifies against the test key, so no test depends on the release key on this machine."""
    monkeypatch.setattr(u, "RELEASE_PUBLIC_KEY", TEST_PUBLIC)


def asset_url(version: str, name: str) -> str:
    return f"https://github.com/{u.REPO}/releases/download/v{version}/{name}"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_zip(entries: dict[str, bytes], symlinks: dict[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, target)
    return buffer.getvalue()


def truncated_member_zip(name: str, data: bytes, declared: int) -> bytes:
    """A stored zip whose one member declares `declared` bytes, more than the file holds after its data.

    zipfile refuses a member whose data runs into the central directory. A second record with the same header
    offset gets past that check (it only warns), so reading the member runs off the end of the file: EOFError.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(name, data)
    raw = buffer.getvalue()
    cd = raw.index(b"PK\x01\x02")  # the central directory record
    eocd = raw.index(b"PK\x05\x06")  # the end of central directory record
    bad = bytearray(raw[cd:eocd])
    struct.pack_into("<II", bad, 20, declared, declared)  # compressed size, then size: the first record only
    end = bytearray(raw[eocd:])
    struct.pack_into("<HHHHII", end, 4, 0, 0, 2, 2, 2 * len(bad), cd)  # two records, same start, same disk
    return raw[:cd] + bytes(bad) + raw[cd:eocd] + bytes(end)


def bundle_zip(version: str = "0.2.0") -> bytes:
    return make_zip(
        {u.EXE_NAME: f"MZ-{version}".encode(), "_internal/base.dll": b"dll", "install.cmd": b"@echo off\r\n"}
    )


def release_body(version="0.2.0", *, names=None, tag=None, draft=False, zip_size=1000) -> bytes:
    names = names if names is not None else [f"MeridianDesktop-{version}.zip", "SHA256SUMS", SIG_NAME]
    assets = [
        {
            "name": name,
            "browser_download_url": asset_url(version, name),
            "size": zip_size if name.endswith(".zip") else 64,
        }
        for name in names
    ]
    # The releases endpoint returns a JSON list. Core releases share the repo, so this list is what the check reads.
    release = {"tag_name": tag or f"{u.DESKTOP_TAG_PREFIX}{version}", "draft": draft, "assets": assets}
    return json.dumps([release]).encode()


class FakeGitHub:
    """A transport. A route maps a URL to a (status, headers, body) tuple, to an exception to raise, or to a
    function (max_bytes, dest) -> Reply. Every URL asked for is recorded. An unrouted URL fails the test."""

    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls: list[str] = []
        self.requests: list[tuple[str, int, Path | None]] = []  # each URL with the size cap and file it was given

    def __call__(self, url, max_bytes, dest):
        self.calls.append(url)
        self.requests.append((url, max_bytes, dest))
        if url not in self.routes:
            raise AssertionError(f"unexpected request to {url}")
        route = self.routes[url]
        if isinstance(route, BaseException):
            raise route
        if callable(route):
            return route(max_bytes, dest)
        status, headers, body = route
        if not 200 <= status < 300:
            return u.Reply(status, headers, b"")
        if len(body) > max_bytes:
            raise u.TooLarge("The download is over the size limit.")
        if dest is None:
            return u.Reply(status, headers, body)
        dest.write_bytes(body)
        return u.Reply(status, headers, b"")


def github(version="0.2.0", *, zip_bytes=None, sums=None, signed_by=TEST_SEED, sig=None, **release_kwargs) -> FakeGitHub:
    """A fake GitHub with one release. SHA256SUMS is signed by signed_by, unless sig gives the .sig text itself."""
    zip_bytes = bundle_zip(version) if zip_bytes is None else zip_bytes
    name = f"MeridianDesktop-{version}.zip"
    sums_text = f"{sha256(zip_bytes)}  {name}\n" if sums is None else sums
    sums_bytes = sums_text.encode()
    sig_bytes = sign_sums(sums_bytes, signed_by) if sig is None else sig
    return FakeGitHub(
        {
            u.RELEASES_LIST_URL: (200, {}, release_body(version, **release_kwargs)),
            asset_url(version, name): (302, {"location": CDN_URL}, b""),
            CDN_URL: (200, {}, zip_bytes),
            asset_url(version, "SHA256SUMS"): (200, {}, sums_bytes),
            asset_url(version, SIG_NAME): (200, {}, sig_bytes),
        }
    )


class Recorder:
    """A shortcut writer that records the exe it was pointed at."""

    def __init__(self):
        self.targets: list[Path] = []

    def __call__(self, exe: Path) -> None:
        self.targets.append(exe)


class Failing:
    """A shortcut writer that always fails."""

    def __call__(self, exe: Path) -> None:
        raise OSError("shortcut write failed")


@pytest.fixture
def frozen(monkeypatch, tmp_path):
    """A frozen bundle that runs from inside the test's install folder, as an installed copy does."""
    monkeypatch.setattr(u, "_is_frozen", lambda: True)
    monkeypatch.setattr(u, "_running_exe", lambda: tmp_path / "install" / "versions" / "0.1.0" / u.EXE_NAME)


@pytest.fixture
def root(tmp_path):
    """An install root with 0.1.0 installed, as the running app would be."""
    install = tmp_path / "install"
    exe = install / "versions" / "0.1.0" / u.EXE_NAME
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ-0.1.0")
    return install


def version_names(install: Path) -> list[str]:
    return sorted(p.name for p in (install / "versions").iterdir())


def tree_state(root: Path) -> dict[str, bytes | None]:
    """Every path under root with its bytes (None for a folder), so a test can show that nothing changed."""
    paths = sorted(root.rglob("*"))
    return {str(path.relative_to(root)): (path.read_bytes() if path.is_file() else None) for path in paths}


def release_0_2_0() -> u.Release:
    return u.Release(
        version="0.2.0",
        zip_name=ZIP_NAME,
        zip_url=asset_url("0.2.0", ZIP_NAME),
        zip_size=1000,
        sums_url=asset_url("0.2.0", "SHA256SUMS"),
        sig_url=asset_url("0.2.0", SIG_NAME),
    )


# --- versions and URLs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("latest", "current", "expected"),
    [
        ("0.2.0", "0.1.0", True),
        ("0.1.0", "0.1.0", False),
        ("0.1.0", "0.2.0", False),
        ("0.10.0", "0.9.0", True),
        ("0.2", "0.2.0", False),
        ("1.0.0", "0.99.99", True),
    ],
)
def test_is_newer_compares_integer_parts(latest, current, expected):
    assert u.is_newer(latest, current) is expected


@pytest.mark.parametrize(
    "text", ["v0.2.0", "0.2.0-beta", "0.2.0 ", "", "1.x", "1..2", ".1", "0.2.0.rc1", None, "9" * 4301]
)
def test_versions_that_are_not_plain_dotted_integers_are_refused(text):
    with pytest.raises(u.Refused):
        u.parse_version(text)


def test_app_version_is_a_plain_version():
    assert u.parse_version(APP_VERSION) == tuple(int(part) for part in APP_VERSION.split("."))


def test_the_package_version_is_the_app_version():
    assert meridian_desktop.__version__ == APP_VERSION


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/ajc3xc/meridian-desktop/x.zip",
        "https://evil.example/x.zip",
        "https://github.com.evil.example/x.zip",
        "https://user:pw@github.com/x.zip",
        "https://github.com:8443/x.zip",
        "ftp://github.com/x.zip",
    ],
)
def test_urls_that_are_not_allowed_https_github_addresses_are_refused(url):
    with pytest.raises(u.Refused):
        u.require_allowed_url(url)


def test_every_listed_host_is_allowed():
    for host in sorted(u.ALLOWED_HOSTS):
        u.require_allowed_url(f"https://{host}/x")


# --- release and checksum ------------------------------------------------------------------------


def test_fetch_latest_reads_the_zip_sums_and_signature_assets():
    release = u.fetch_latest(github())
    assert release == u.Release(
        version="0.2.0",
        zip_name=ZIP_NAME,
        zip_url=asset_url("0.2.0", ZIP_NAME),
        zip_size=1000,
        sums_url=asset_url("0.2.0", "SHA256SUMS"),
        sig_url=asset_url("0.2.0", SIG_NAME),
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"names": ["SHA256SUMS", SIG_NAME]},  # the zip is missing
        {"names": [ZIP_NAME, SIG_NAME]},  # SHA256SUMS is missing
        {"names": [ZIP_NAME, "SHA256SUMS"]},  # SHA256SUMS.sig is missing
        {"tag": "v0.2.0-beta"},  # not plain dotted integers
        {"draft": True},
    ],
)
def test_a_release_missing_an_asset_or_with_a_bad_version_is_refused(kwargs):
    with pytest.raises(u.Refused):
        u.fetch_latest(github(**kwargs))


def test_a_zip_asset_over_the_cap_is_refused_before_download(monkeypatch):
    gh = github(zip_size=u.MAX_DOWNLOAD_BYTES + 1)
    with pytest.raises(u.TooLarge):
        u.fetch_latest(gh)
    assert CDN_URL not in gh.calls


def test_expected_sha256_accepts_both_common_line_forms():
    assert u.expected_sha256(f"{'AB' * 32}  {ZIP_NAME}\n", ZIP_NAME) == "ab" * 32
    assert u.expected_sha256(f"{'cd' * 32} *{ZIP_NAME}\n", ZIP_NAME) == "cd" * 32


def test_expected_sha256_refuses_a_missing_or_duplicate_zip_line():
    line = f"{'ab' * 32}  {ZIP_NAME}\n"
    with pytest.raises(u.Refused):
        u.expected_sha256("", ZIP_NAME)
    with pytest.raises(u.Refused):
        u.expected_sha256(line * 2, ZIP_NAME)
    with pytest.raises(u.Refused):
        u.expected_sha256(f"{'ab' * 32}  other.zip\n", ZIP_NAME)


def _with_every_member_marked_encrypted(data: bytes) -> bytes:
    """Set general purpose bit 0 (encrypted) on each local and central header, as a password-protected zip has."""
    raw = bytearray(data)
    for signature, flags_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        position = raw.find(signature)
        while position != -1:
            raw[position + flags_offset] |= 0x01
            position = raw.find(signature, position + 4)
    return bytes(raw)


# --- check_and_stage: the signature, the checksum, and the staging -------------------------------------


def test_a_newer_release_is_staged_as_pending_and_the_older_copy_is_pruned(root, frozen):
    old = root / "versions" / "0.0.9"
    old.mkdir()
    (old / u.EXE_NAME).write_bytes(b"MZ-0.0.9")

    result = u.check_and_stage("0.1.0", github(), root)

    new_exe = root / "versions" / "0.2.0" / u.EXE_NAME
    assert result == u.UpdateResult("update_staged", version="0.2.0")
    assert new_exe.read_bytes() == b"MZ-0.2.0"
    assert (root / "versions" / "0.2.0" / "_internal" / "base.dll").read_bytes() == b"dll"
    assert u.read_pending(root) == "0.2.0"
    assert version_names(root) == ["0.1.0", "0.2.0"]
    assert not list(root.glob("update-*"))


def test_staging_leaves_the_shortcuts_alone_until_the_restart(root, frozen):
    assert u.check_and_stage("0.1.0", github(), root).status == "update_staged"
    assert u.read_current(root) is None  # current.json is written only when the new version starts
    assert (root / "versions" / "0.1.0" / u.EXE_NAME).read_bytes() == b"MZ-0.1.0"


def test_the_download_announces_its_version_before_it_starts(root, frozen):
    announced = []
    result = u.check_and_stage("0.1.0", github(), root, announce=announced.append)
    assert result.status == "update_staged"
    assert announced == ["0.2.0"]


def test_a_staged_update_repoints_the_shortcut_only_when_its_version_starts(root, frozen):
    assert u.check_and_stage("0.1.0", github(), root).status == "update_staged"
    shortcut = Recorder()
    new_exe = root / "versions" / "0.2.0" / u.EXE_NAME
    previous_exe = root / "versions" / "0.1.0" / u.EXE_NAME

    assert u.finish_update_at_start(root, previous_exe, "0.1.0", [shortcut]) is False
    assert shortcut.targets == []  # the running version still owns the shortcut
    assert u.read_pending(root) == "0.2.0"

    assert u.finish_update_at_start(root, new_exe, "0.2.0", [shortcut]) is True
    assert shortcut.targets == [new_exe]
    assert u.read_current(root) == "0.2.0"
    assert u.read_pending(root) is None


def test_a_start_with_no_pending_update_points_the_shortcuts_at_the_running_version(root):
    shortcut = Recorder()
    exe = root / "versions" / "0.1.0" / u.EXE_NAME
    assert u.finish_update_at_start(root, exe, "0.1.0", [shortcut]) is False
    assert shortcut.targets == [exe]
    assert u.read_current(root) == "0.1.0"
    assert u.finish_update_at_start(root, exe, "0.1.0", [shortcut]) is False
    assert shortcut.targets == [exe]  # already current, so no second repoint


def test_a_start_of_an_older_copy_does_not_move_the_shortcuts_back(root):
    u.write_current(root, "0.2.0")
    shortcut = Recorder()
    older = root / "versions" / "0.1.0" / u.EXE_NAME
    assert u.finish_update_at_start(root, older, "0.1.0", [shortcut]) is False
    assert shortcut.targets == []
    assert u.read_current(root) == "0.2.0"


def test_a_pending_update_for_another_version_is_left_alone_at_start(root):
    u.write_pending(root, "0.2.0")
    shortcut = Recorder()
    exe = root / "versions" / "0.1.0" / u.EXE_NAME
    assert u.finish_update_at_start(root, exe, "0.1.0", [shortcut]) is False
    assert shortcut.targets == []
    assert u.read_pending(root) == "0.2.0"


def test_a_failed_shortcut_at_the_restart_keeps_the_update_pending(root, frozen):
    assert u.check_and_stage("0.1.0", github(), root).status == "update_staged"
    new_exe = root / "versions" / "0.2.0" / u.EXE_NAME
    assert u.finish_update_at_start(root, new_exe, "0.2.0", [Failing()]) is False
    assert u.read_pending(root) == "0.2.0"
    assert u.read_current(root) is None


def test_a_failing_shortcut_at_the_restart_undoes_the_ones_already_done(root):
    u.write_current(root, "0.1.0")
    u.write_pending(root, "0.2.0")
    new_exe = root / "versions" / "0.2.0" / u.EXE_NAME
    new_exe.parent.mkdir(parents=True)
    new_exe.write_bytes(b"MZ-0.2.0")
    previous = root / "versions" / "0.1.0" / u.EXE_NAME
    shortcut = Recorder()
    assert u.finish_update_at_start(root, new_exe, "0.2.0", [shortcut, Failing()]) is False
    assert shortcut.targets == [new_exe, previous]
    assert u.read_pending(root) == "0.2.0"
    assert u.read_current(root) == "0.1.0"


def test_a_checksum_mismatch_installs_nothing(root, frozen):
    result = u.check_and_stage("0.1.0", github(sums=f"{'0' * 64}  {ZIP_NAME}\n"), root)
    assert result.status == "refused"
    assert "does not match SHA256SUMS" in result.reason
    assert version_names(root) == ["0.1.0"]
    assert u.read_pending(root) is None


def test_a_fresh_updater_verifies_against_the_committed_release_key(monkeypatch):
    """Every other test patches the key to a test key. This loads the updater as it ships and checks its own key."""
    spec = importlib.util.spec_from_file_location("updater_as_shipped", u.__file__)
    shipped = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "updater_as_shipped", shipped)  # dataclasses look the module up while it loads
    spec.loader.exec_module(shipped)
    assert shipped.RELEASE_PUBLIC_KEY == base64.b64decode(release_key.RELEASE_PUBLIC_KEY_B64, validate=True)
    assert len(shipped.RELEASE_PUBLIC_KEY) == 32
    sums = b"not a real release\n"
    # A signature by the test key is refused under the committed key, which is the point of the check.
    with pytest.raises(shipped.Refused, match="not signed by the Meridian release key"):
        shipped.verify_sums_signature(sums, sign_sums(sums), shipped.RELEASE_PUBLIC_KEY)


def test_a_release_not_signed_by_the_release_key_is_refused_before_the_zip_is_fetched(root, frozen):
    gh = github(signed_by=OTHER_SEED)
    before = tree_state(root)
    result = u.check_and_stage("0.1.0", gh, root)
    assert result == u.UpdateResult("refused", reason=u.UNSIGNED_REASON)
    assert CDN_URL not in gh.calls
    assert tree_state(root) == before


def test_the_reason_for_an_unsigned_release_is_the_plain_sentence():
    assert u.UNSIGNED_REASON == "The update is not signed by the Meridian release key."


def test_a_sums_file_changed_after_signing_fails_its_signature(root, frozen):
    gh = github()
    sums_url = asset_url("0.2.0", "SHA256SUMS")
    gh.routes[sums_url] = (200, {}, gh.routes[sums_url][2] + b"\n")  # one byte more than was signed
    result = u.check_and_stage("0.1.0", gh, root)
    assert result == u.UpdateResult("refused", reason=u.UNSIGNED_REASON)
    assert CDN_URL not in gh.calls
    assert version_names(root) == ["0.1.0"]


@pytest.mark.parametrize(
    "sig",
    [b"", b"not base64 !!", base64.b64encode(b"x" * 63) + b"\n", base64.b64encode(b"\x00" * 64)],
)
def test_a_malformed_signature_is_refused_as_unsigned(root, frozen, sig):
    gh = github(sig=sig)
    result = u.check_and_stage("0.1.0", gh, root)
    assert result == u.UpdateResult("refused", reason=u.UNSIGNED_REASON)
    assert CDN_URL not in gh.calls
    assert version_names(root) == ["0.1.0"]


def test_a_release_with_no_signature_asset_is_refused_before_any_download(root, frozen):
    gh = github()
    gh.routes[u.RELEASES_LIST_URL] = (200, {}, release_body("0.2.0", names=[ZIP_NAME, "SHA256SUMS"]))
    result = u.check_and_stage("0.1.0", gh, root)
    assert result == u.UpdateResult("refused", reason=u.UNSIGNED_REASON)
    assert gh.calls == [u.RELEASES_LIST_URL]


def test_a_signature_that_is_not_found_is_refused_as_unsigned(root, frozen):
    gh = github()
    gh.routes[asset_url("0.2.0", SIG_NAME)] = (404, {}, b"")
    assert u.check_and_stage("0.1.0", gh, root) == u.UpdateResult("refused", reason=u.UNSIGNED_REASON)
    assert CDN_URL not in gh.calls


def test_missing_sums_line_stops_before_the_zip_is_downloaded(root, frozen):
    gh = github(sums="")
    result = u.check_and_stage("0.1.0", gh, root)
    assert result.status == "refused"
    assert CDN_URL not in gh.calls
    assert version_names(root) == ["0.1.0"]


def test_download_over_the_size_cap_installs_nothing(root, frozen, monkeypatch):
    monkeypatch.setattr(u, "MAX_DOWNLOAD_BYTES", 64)
    result = u.check_and_stage("0.1.0", github(zip_size=10), root)
    assert result.status == "refused"
    assert "size limit" in result.reason
    assert version_names(root) == ["0.1.0"]
    assert not list(root.glob("update-*"))


def test_zip_without_the_exe_is_refused_and_leaves_nothing_behind(root, frozen):
    zip_bytes = make_zip({"_internal/base.dll": b"dll"})
    result = u.check_and_stage("0.1.0", github(zip_bytes=zip_bytes), root)
    assert result.status == "refused"
    assert u.EXE_NAME in result.reason
    assert version_names(root) == ["0.1.0"]


def test_a_file_that_is_not_a_zip_is_refused(root, frozen):
    result = u.check_and_stage("0.1.0", github(zip_bytes=b"not a zip"), root)
    assert result.status == "refused"
    assert version_names(root) == ["0.1.0"]


@pytest.mark.parametrize(
    "entry",
    [
        "../evil.txt",
        "/abs.txt",
        "C:/evil.txt",
        "C:evil.txt",
        "..\\evil.txt",
        "a/../../evil.txt",
        "\\\\server\\share\\evil.txt",
        "a/b:stream.txt",
    ],
)
def test_zip_slip_entries_are_refused_and_write_nothing(root, frozen, entry, tmp_path):
    zip_bytes = make_zip({u.EXE_NAME: b"MZ", entry: b"evil"})
    result = u.check_and_stage("0.1.0", github(zip_bytes=zip_bytes), root)
    assert result.status == "refused"
    assert not list(tmp_path.rglob("evil.txt"))
    assert version_names(root) == ["0.1.0"]


def test_a_symbolic_link_entry_is_refused(root, frozen):
    zip_bytes = make_zip({u.EXE_NAME: b"MZ"}, symlinks={"link": "/etc/passwd"})
    result = u.check_and_stage("0.1.0", github(zip_bytes=zip_bytes), root)
    assert result.status == "refused"
    assert version_names(root) == ["0.1.0"]


@pytest.mark.parametrize(("name", "attributes"), [("linked.txt", 0x0400), ("junction/", 0x0410)])
def test_a_windows_reparse_point_entry_is_refused_and_writes_nothing(root, frozen, name, attributes):
    # The Windows attributes sit in the low 16 bits of external_attr. No Unix mode is set, so only the
    # reparse-point bit can refuse this entry.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(u.EXE_NAME, b"MZ")
        info = zipfile.ZipInfo(name)
        info.external_attr = attributes
        archive.writestr(info, b"C:/Windows")
    result = u.check_and_stage("0.1.0", github(zip_bytes=buffer.getvalue()), root)
    assert result.status == "refused"
    assert version_names(root) == ["0.1.0"]
    assert not list(root.glob("update-*"))


def test_a_member_with_a_bad_crc_is_refused_and_installs_nothing(root, frozen):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(u.EXE_NAME, b"MZ-0.2.0-payload")
    raw = bytearray(buffer.getvalue())
    raw[raw.index(b"MZ-0.2.0-payload")] ^= 0xFF  # the stored bytes no longer match the CRC in the archive
    result = u.check_and_stage("0.1.0", github(zip_bytes=bytes(raw)), root)
    assert result.status == "refused"
    assert "could not be extracted" in result.reason
    assert version_names(root) == ["0.1.0"]
    assert (root / "versions" / "0.1.0" / u.EXE_NAME).read_bytes() == b"MZ-0.1.0"
    assert not list(root.glob("update-*"))


def test_an_encrypted_member_is_refused_and_installs_nothing(root, frozen):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(u.EXE_NAME, b"MZ-0.2.0")
    zip_bytes = _with_every_member_marked_encrypted(buffer.getvalue())
    result = u.check_and_stage("0.1.0", github(zip_bytes=zip_bytes), root)
    assert result.status == "refused"
    assert "could not be extracted" in result.reason
    assert version_names(root) == ["0.1.0"]
    assert not list(root.glob("update-*"))


@pytest.mark.filterwarnings("ignore:Overlapped entries")
def test_a_truncated_member_is_refused_and_leaves_the_install_unchanged(root, frozen):
    zip_bytes = truncated_member_zip(u.EXE_NAME, b"MZ-0.2.0-payload", declared=4096 + 17)
    # Precondition: plain zipfile reads this member to the end of the file and raises EOFError. Without that the
    # test would prove nothing about the EOFError path.
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        with pytest.warns(UserWarning, match="Overlapped entries"):
            member = archive.open(archive.infolist()[0])
        with member, pytest.raises(EOFError):
            member.read()
    before = tree_state(root)
    result = u.check_and_stage("0.1.0", github(zip_bytes=zip_bytes), root)
    assert result.status == "refused"
    assert "could not be extracted" in result.reason
    assert tree_state(root) == before
    assert not list(root.glob("update-*"))


def test_an_oversized_desktop_version_tag_is_skipped_so_no_release_is_refused_and_never_raises(root, frozen):
    gh = github(tag=u.DESKTOP_TAG_PREFIX + "9" * 4301)
    result = u.check_and_stage("0.1.0", gh, root)
    assert result.status == "refused"
    assert gh.calls == [u.RELEASES_LIST_URL]
    assert version_names(root) == ["0.1.0"]


def test_a_staged_version_is_not_downloaded_again(root, frozen):
    assert u.check_and_stage("0.1.0", github(), root).status == "update_staged"
    gh = github()
    assert u.check_and_stage("0.1.0", gh, root) == u.UpdateResult("update_staged", version="0.2.0")
    assert CDN_URL not in gh.calls


def test_a_failure_while_recording_the_pending_update_removes_the_new_version(root, frozen, monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(u, "write_pending", refuse)
    result = u.check_and_stage("0.1.0", github(), root)
    assert result.status == "refused"
    assert "could not be recorded" in result.reason
    assert version_names(root) == ["0.1.0"]


def test_a_source_run_returns_refused_and_makes_no_request(root, monkeypatch):
    monkeypatch.setattr(u, "_is_frozen", lambda: False)
    gh = github()
    result = u.check_and_stage("0.1.0", gh, root)
    assert result == u.UpdateResult("refused", reason=u.DEV_RUN_REASON)
    assert gh.calls == []
    assert version_names(root) == ["0.1.0"]


def test_the_same_version_is_up_to_date(root, frozen):
    gh = github("0.1.0")
    result = u.check_and_stage("0.1.0", gh, root)
    assert result == u.UpdateResult("up_to_date", version="0.1.0")
    assert gh.calls == [u.RELEASES_LIST_URL]


def test_github_unreachable_is_offline_and_never_raises(root, frozen):
    gh = github()
    gh.routes[u.RELEASES_LIST_URL] = OSError("no route")
    result = u.check_and_stage("0.1.0", gh, root)
    assert result.status == "offline"


def test_a_timeout_during_download_is_offline_and_leaves_no_partial_files(root, frozen):
    gh = github()
    gh.routes[CDN_URL] = TimeoutError("slow")
    result = u.check_and_stage("0.1.0", gh, root)
    assert result.status == "offline"
    assert version_names(root) == ["0.1.0"]
    assert not list(root.glob("update-*"))
    assert u.read_pending(root) is None


@pytest.mark.parametrize(
    ("status", "expected"), [(404, "refused"), (403, "refused"), (429, "offline"), (503, "offline")]
)
def test_github_error_statuses_map_to_refused_or_offline(root, frozen, status, expected):
    gh = github()
    gh.routes[u.RELEASES_LIST_URL] = (status, {}, b"")
    assert u.check_and_stage("0.1.0", gh, root).status == expected


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/MeridianDesktop-0.2.0.zip",
        "http://objects.githubusercontent.com/MeridianDesktop-0.2.0.zip",
        "https://objects.githubusercontent.com.evil.example/x.zip",
    ],
)
def test_a_redirect_to_a_disallowed_address_is_refused_before_it_is_requested(root, frozen, location):
    gh = github()
    gh.routes[asset_url("0.2.0", ZIP_NAME)] = (302, {"location": location}, b"")
    result = u.check_and_stage("0.1.0", gh, root)
    assert result.status == "refused"
    assert location not in gh.calls
    assert version_names(root) == ["0.1.0"]


def test_a_redirect_loop_is_refused(root, frozen):
    gh = github()
    gh.routes[asset_url("0.2.0", ZIP_NAME)] = (302, {"location": asset_url("0.2.0", ZIP_NAME)}, b"")
    result = u.check_and_stage("0.1.0", gh, root)
    assert result.status == "refused"
    assert "too many times" in result.reason


# --- the layout pointers and the restart --------------------------------------------------------------


def test_the_pointer_files_round_trip_and_ignore_bad_content(root):
    assert u.read_current(root) is None
    u.write_current(root, "0.1.0")
    assert u.read_current(root) == "0.1.0"
    u.write_pending(root, "0.2.0")
    assert u.read_pending(root) == "0.2.0"
    u.clear_pending(root)
    assert u.read_pending(root) is None
    (root / u.CURRENT_NAME).write_text('{"version": "not a version"}', encoding="utf-8")
    assert u.read_current(root) is None
    with pytest.raises(u.Refused):
        u.write_current(root, "v0.1.0")


def test_a_ready_version_needs_a_newer_pending_version_whose_exe_is_on_disk(root):
    u.write_pending(root, "0.2.0")
    assert u.ready_version(root, "0.1.0") is None  # its exe is not there yet
    exe = u.exe_for(root, "0.2.0")
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ-0.2.0")
    assert u.ready_version(root, "0.1.0") == "0.2.0"
    assert u.ready_version(root, "0.2.0") is None  # not newer than the running version


def test_restart_starts_the_staged_exe_with_the_old_version_detached_and_without_a_shell(root):
    import subprocess

    exe = u.exe_for(root, "0.1.0")
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))

    u.launch_after_update(exe, "0.0.9", popen=popen)
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == [str(exe), "--after-update", "0.0.9"]
    assert kwargs.get("shell", False) is False
    assert kwargs["close_fds"] is True
    assert kwargs["cwd"] == str(exe.parent)
    expected_flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    assert kwargs["creationflags"] == expected_flags


def test_a_restart_that_cannot_start_the_exe_is_refused_and_says_so(root):
    def popen(argv, **kwargs):
        raise OSError("no such file")

    with pytest.raises(u.Refused, match="could not be started"):
        u.launch_after_update(u.exe_for(root, "0.1.0"), "0.0.9", popen=popen)


def test_a_restart_into_a_missing_exe_starts_nothing(root):
    calls = []
    with pytest.raises(u.Refused, match="missing"):
        u.launch_after_update(u.exe_for(root, "0.9.0"), "0.1.0", popen=lambda argv, **kw: calls.append(argv))
    assert calls == []


# --- rollback ------------------------------------------------------------------------------------------


def test_rollback_points_back_at_the_previous_version(root, frozen):
    assert u.check_and_stage("0.1.0", github(), root).status == "update_staged"
    rec = Recorder()
    assert u.rollback("0.2.0", root, [rec]) == "0.1.0"
    assert rec.targets == [root / "versions" / "0.1.0" / u.EXE_NAME]
    assert u.read_current(root) == "0.1.0"


def test_rollback_picks_the_newest_version_below_current(root):
    for version in ("0.1.5", "0.0.9"):
        exe = root / "versions" / version / u.EXE_NAME
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")
    rec = Recorder()
    assert u.rollback("0.2.0", root, [rec]) == "0.1.5"
    assert rec.targets == [root / "versions" / "0.1.5" / u.EXE_NAME]


def test_rollback_refuses_when_no_earlier_version_is_left(root):
    rec = Recorder()
    with pytest.raises(u.Refused, match="no earlier version"):
        u.rollback("0.1.0", root, [rec])
    assert rec.targets == []


def test_a_rollback_holds_the_rolled_back_release_on_every_later_check(root, frozen):
    shortcut = Recorder()
    assert u.check_and_stage("0.1.0", github(), root).status == "update_staged"
    assert u.rollback("0.2.0", root, [shortcut]) == "0.1.0"
    targets_after_rollback = list(shortcut.targets)
    assert (root / "versions" / u.SKIP_MARKER).is_file()
    for _ in range(2):
        gh = github()
        result = u.check_and_stage("0.1.0", gh, root)
        assert result.status == "refused"
        assert result.version == "0.2.0"
        assert "held after rollback" in result.reason
        assert gh.calls == [u.RELEASES_LIST_URL]  # the release is held before any download
        assert shortcut.targets == targets_after_rollback  # the shortcut stays on 0.1.0
    assert shortcut.targets[-1] == root / "versions" / "0.1.0" / u.EXE_NAME


@pytest.mark.parametrize("latest", ["0.2.0", "0.3.0"])
def test_a_release_not_newer_than_the_rolled_back_version_is_held(root, frozen, latest):
    shortcut = Recorder()
    for version in ("0.2.0", "0.3.0"):
        exe = root / "versions" / version / u.EXE_NAME
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")
    assert u.rollback("0.3.0", root, [shortcut]) == "0.2.0"
    assert u.rollback("0.2.0", root, [shortcut]) == "0.1.0"  # the marker keeps 0.3.0, not 0.2.0
    gh = github(latest)
    result = u.check_and_stage("0.1.0", gh, root)
    assert result.status == "refused"
    assert "held after rollback" in result.reason
    assert gh.calls == [u.RELEASES_LIST_URL]


def test_a_newer_release_after_a_rollback_stages_and_clears_the_marker(root, frozen):
    shortcut = Recorder()
    assert u.check_and_stage("0.1.0", github(), root).status == "update_staged"
    assert u.rollback("0.2.0", root, [shortcut]) == "0.1.0"
    result = u.check_and_stage("0.1.0", github("0.3.0"), root)
    assert result == u.UpdateResult("update_staged", version="0.3.0")
    assert not (root / "versions" / u.SKIP_MARKER).exists()
    assert u.read_pending(root) == "0.3.0"
    assert version_names(root) == ["0.1.0", "0.3.0"]


def test_an_unreadable_marker_is_ignored_and_holds_nothing(root, frozen):
    (root / "versions" / u.SKIP_MARKER).write_text("{not json", encoding="utf-8")
    result = u.check_and_stage("0.1.0", github(), root)
    assert result == u.UpdateResult("update_staged", version="0.2.0")


def test_a_rollback_whose_shortcuts_all_fail_leaves_no_marker(root):
    with pytest.raises(u.Refused, match="could not be pointed back"):
        u.rollback("0.2.0", root, [Failing()])
    assert not (root / "versions" / u.SKIP_MARKER).exists()


# --- the kill switch: the value this build ships with is on -------------------------------------------


def test_installing_is_on_in_the_shipped_build():
    assert u.INSTALL_ENABLED is True


@pytest.mark.parametrize("frozen_build", [True, False])
def test_the_kill_switch_refuses_before_any_request_and_changes_nothing(root, monkeypatch, frozen_build):
    monkeypatch.setattr(u, "INSTALL_ENABLED", False)
    monkeypatch.setattr(u, "_is_frozen", lambda: frozen_build)
    gh = github()
    result = u.check_and_stage("0.1.0", gh, root)
    assert result == u.UpdateResult("refused", reason=u.INSTALL_OFF_REASON)
    assert gh.requests == []
    assert version_names(root) == ["0.1.0"]


def test_install_update_with_the_kill_switch_raises_and_changes_nothing(root, monkeypatch):
    monkeypatch.setattr(u, "INSTALL_ENABLED", False)
    before = tree_state(root)
    gh = github()
    with pytest.raises(u.InstallDisabled, match="turned off"):
        u.install_update(release_0_2_0(), "0.1.0", gh, root)
    assert gh.calls == []
    assert tree_state(root) == before


def test_install_update_with_the_kill_switch_does_not_create_the_install_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "INSTALL_ENABLED", False)
    install = tmp_path / "never-created"
    with pytest.raises(u.InstallDisabled):
        u.install_update(release_0_2_0(), "0.1.0", github(), install)
    assert not install.exists()


# --- the copy cap and the urllib transport ---------------------------------------------------------


def test_copy_capped_allows_exactly_the_cap_and_refuses_one_byte_more():
    out = io.BytesIO()
    u._copy_capped(io.BytesIO(b"x" * 10), out, 10)
    assert out.getvalue() == b"x" * 10
    with pytest.raises(u.TooLarge):
        u._copy_capped(io.BytesIO(b"x" * 11), io.BytesIO(), 10)


class _FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, headers: dict[str, str] | None = None, status: int = 200):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


class _FakeOpener:
    def __init__(self, outcome):
        self.outcome = outcome

    def open(self, request, timeout=None):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _patch_opener(monkeypatch, outcome) -> None:
    monkeypatch.setattr(u.urllib.request, "build_opener", lambda *handlers: _FakeOpener(outcome))


def test_urllib_transport_streams_a_2xx_body_to_the_file(monkeypatch, tmp_path):
    _patch_opener(monkeypatch, _FakeResponse(b"zipbytes", {"Content-Type": "application/zip"}))
    dest = tmp_path / "x.zip"
    reply = u.urllib_transport("https://github.com/x", 100, dest)
    assert reply.status == 200
    assert reply.body == b""
    assert dest.read_bytes() == b"zipbytes"
    assert reply.headers["content-type"] == "application/zip"


def test_urllib_transport_returns_a_small_body_in_memory(monkeypatch):
    _patch_opener(monkeypatch, _FakeResponse(b"{}"))
    assert u.urllib_transport("https://api.github.com/x", 100, None).body == b"{}"


def test_urllib_transport_removes_a_file_that_goes_over_the_cap(monkeypatch, tmp_path):
    _patch_opener(monkeypatch, _FakeResponse(b"x" * 11))
    dest = tmp_path / "x.zip"
    with pytest.raises(u.TooLarge):
        u.urllib_transport("https://github.com/x", 10, dest)
    assert not dest.exists()


def test_urllib_transport_refuses_a_declared_size_over_the_cap_before_reading(monkeypatch, tmp_path):
    _patch_opener(monkeypatch, _FakeResponse(b"x", {"Content-Length": "999"}))
    dest = tmp_path / "x.zip"
    with pytest.raises(u.TooLarge):
        u.urllib_transport("https://github.com/x", 10, dest)
    assert not dest.exists()


def test_urllib_transport_returns_a_redirect_without_following_it(monkeypatch, tmp_path):
    location = "https://objects.githubusercontent.com/x"
    headers = http.client.HTTPMessage()
    headers["Location"] = location
    _patch_opener(monkeypatch, urllib.error.HTTPError("https://github.com/x", 302, "Found", headers, io.BytesIO()))
    dest = tmp_path / "x.zip"
    reply = u.urllib_transport("https://github.com/x", 100, dest)
    assert reply.status == 302
    assert reply.headers["location"] == location
    assert not dest.exists()


# --- check_for_update: a notice only, never a download ----------------------------------------------


def page(version: str) -> str:
    return f"https://github.com/{u.REPO}/releases/tag/{u.DESKTOP_TAG_PREFIX}{version}"


def test_a_newer_release_gives_a_notice_and_only_the_api_is_requested():
    gh = github("0.2.0")
    result = u.check_for_update("0.1.0", gh)
    assert result == u.UpdateNotice(version="0.2.0", release_page_url=page("0.2.0"))
    assert gh.requests == [(u.RELEASES_LIST_URL, u.MAX_SMALL_BYTES, None)]


@pytest.mark.parametrize(("latest", "current"), [("0.1.0", "0.1.0"), ("0.0.9", "0.1.0"), ("0.9.0", "0.10.0")])
def test_the_same_or_an_older_release_is_up_to_date(latest, current):
    gh = github(latest)
    assert u.check_for_update(current, gh) == u.UpToDate()
    assert gh.calls == [u.RELEASES_LIST_URL]


def test_a_newer_release_compares_its_parts_as_integers():
    assert u.check_for_update("0.9.0", github("0.10.0")) == u.UpdateNotice("0.10.0", page("0.10.0"))


@pytest.mark.parametrize("failure", [OSError("no route"), TimeoutError("slow"), http.client.HTTPException("reset")])
def test_an_offline_transport_gives_offline(failure):
    def offline(url, max_bytes, dest):
        raise failure

    result = u.check_for_update("0.1.0", offline)
    assert isinstance(result, u.Offline)
    assert result.reason


@pytest.mark.parametrize(("status", "kind"), [(429, u.Offline), (503, u.Offline), (404, u.Refused), (403, u.Offline)])
def test_github_error_statuses_on_the_check_are_offline_or_refused(status, kind):
    gh = github()
    gh.routes[u.RELEASES_LIST_URL] = (status, {}, b"")
    assert isinstance(u.check_for_update("0.1.0", gh), kind)


@pytest.mark.parametrize("status", [403, 429])
def test_a_rate_limited_check_is_offline_and_says_to_try_again_later(status):
    # GitHub answers 403 for an unauthenticated rate limit as well as for a refusal, so the user is told to retry.
    gh = github()
    gh.routes[u.RELEASES_LIST_URL] = (status, {}, b"")
    result = u.check_for_update("0.1.0", gh)
    assert isinstance(result, u.Offline)
    assert result.reason.endswith("Try again later.")


@pytest.mark.parametrize(
    "tag",
    [
        "v0.2.0",  # a core-style tag in the shared repo
        "0.2.0",
        "desktop-v0.2.0-beta",
        "desktop-0.2.0",
        "desktop-V0.2.0",
        "desktop-vv0.2.0",
        "desktop-v",
        "desktop-v" + "9" * 4301,
        "desktop-latest",
    ],
)
def test_a_tag_that_is_not_a_plain_desktop_version_is_ignored(tag):
    # Only desktop-v<plain version> is a desktop release. Other tags are skipped, so nothing is offered.
    gh = github(tag=tag)
    assert u.check_for_update("0.1.0", gh) == u.UpToDate()
    assert gh.calls == [u.RELEASES_LIST_URL]


def test_a_release_with_no_tag_is_not_a_desktop_release():
    gh = FakeGitHub({u.RELEASES_LIST_URL: (200, {}, json.dumps([{"draft": False, "assets": []}]).encode())})
    assert u.check_for_update("0.1.0", gh) == u.UpToDate()


def test_a_reply_that_is_not_json_gives_refused():
    gh = FakeGitHub({u.RELEASES_LIST_URL: (200, {}, b"<html>not json</html>")})
    assert isinstance(u.check_for_update("0.1.0", gh), u.Refused)


def test_a_reply_nested_too_deeply_to_parse_gives_refused_and_never_raises():
    # 400 KB of open brackets is under the small cap, and far past the parser's recursion limit.
    deep = b"[" * 200_000 + b"]" * 200_000
    gh = FakeGitHub({u.RELEASES_LIST_URL: (200, {}, deep)})
    result = u.check_for_update("0.1.0", gh)
    assert isinstance(result, u.Refused)
    assert result.reason == "The release list could not be read."


def test_a_draft_desktop_release_is_not_offered():
    assert u.check_for_update("0.1.0", github("0.2.0", draft=True)) == u.UpToDate()


def test_a_reply_over_the_small_cap_gives_refused():
    def too_large(url, max_bytes, dest):
        raise u.TooLarge("The download is over the size limit.")

    assert isinstance(u.check_for_update("0.1.0", too_large), u.Refused)


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/releases/latest",
        "http://api.github.com/repos/ajc3xc/meridian-desktop/releases/latest",
        "https://api.github.com.evil.example/releases/latest",
    ],
)
def test_a_redirect_to_an_off_list_host_gives_refused(location):
    gh = github()
    gh.routes[u.RELEASES_LIST_URL] = (302, {"location": location}, b"")
    result = u.check_for_update("0.1.0", gh)
    assert isinstance(result, u.Refused)
    assert gh.calls == [u.RELEASES_LIST_URL]  # the off-list address is never requested


@pytest.mark.parametrize("current", ["0.1.0-beta", "v0.1.0", "", "1..2"])
def test_a_current_version_that_is_not_plain_is_refused_before_any_request(current):
    gh = github("0.2.0")
    assert isinstance(u.check_for_update(current, gh), u.Refused)
    assert gh.calls == []


@pytest.mark.parametrize("version", ["0.2.0", "10.0.1", "1.2"])
def test_the_release_page_is_built_from_the_validated_version(version):
    assert u.release_page_url_for(version) == page(version)


@pytest.mark.parametrize("version", ["v0.2.0", "0.2.0/../x", "0.2.0?x=1", "0.2.0-beta"])
def test_a_release_page_is_never_built_from_an_unvalidated_version(version):
    with pytest.raises(u.Refused):
        u.release_page_url_for(version)


def test_check_for_update_cannot_reach_the_install_path(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the install path was reached from check_for_update")

    for name in (
        "check_and_stage",
        "install_update",
        "rollback",
        "_download_verified",
        "_extract_verified",
        "_point_shortcuts",
    ):
        monkeypatch.setattr(u, name, forbidden)
    assert isinstance(u.check_for_update("0.1.0", github("0.2.0")), u.UpdateNotice)
    assert isinstance(u.check_for_update("0.2.0", github("0.2.0")), u.UpToDate)
    assert isinstance(u.check_for_update("0.1.0", github("0.1.0")), u.UpToDate)


# --- the Apps and Features entry follows the version that is staged or running ----------------------------


class FakeRegistry:
    """The installer's registry backend, kept in memory. Values are read back the way the installer reads them."""

    def __init__(self) -> None:
        self.values: dict[str, dict[str, str]] = {}

    def write_strings(self, key, values) -> None:
        self.values.setdefault(key, {}).update(values)

    def read_string(self, key, name):
        return self.values.get(key, {}).get(name)

    def delete_value(self, key, name) -> bool:
        return self.values.get(key, {}).pop(name, None) is not None

    def delete_key(self, key) -> bool:
        return self.values.pop(key, None) is not None


def _recorder(registry: FakeRegistry):
    return install.apps_entry_recorder({}, registry=registry)


def _entry(registry: FakeRegistry) -> tuple[Path, str]:
    """The exe and the version the Apps and Features entry names. Its three paths must agree, or this fails."""
    values = registry.values[install.UNINSTALL_KEY]
    exe_text, _icon_index = values["DisplayIcon"].rsplit(",", 1)
    assert values["UninstallString"] == f'"{exe_text}" --uninstall'
    return Path(exe_text), values["DisplayVersion"]


def test_a_staged_update_points_the_apps_entry_at_the_new_version(root, frozen):
    registry = FakeRegistry()
    result = u.check_and_stage("0.1.0", github(), root, record_apps_entry=_recorder(registry))
    assert result.status == "update_staged"
    assert _entry(registry) == (root / "versions" / "0.2.0" / u.EXE_NAME, "0.2.0")


def test_a_refused_update_leaves_the_apps_entry_alone(root, frozen):
    registry = FakeRegistry()
    result = u.check_and_stage(
        "0.1.0", github(sums=f"{'0' * 64}  {ZIP_NAME}\n"), root, record_apps_entry=_recorder(registry)
    )
    assert result.status == "refused"
    assert registry.values == {}


def test_the_apps_entry_follows_the_running_version_through_an_update(root, frozen):
    registry = FakeRegistry()
    record = _recorder(registry)
    running = root / "versions" / "0.1.0" / u.EXE_NAME
    assert u.finish_update_at_start(root, running, "0.1.0", [], record_apps_entry=record) is False
    assert _entry(registry) == (running, "0.1.0")

    assert u.check_and_stage("0.1.0", github(), root, record_apps_entry=record).status == "update_staged"
    new_exe = root / "versions" / "0.2.0" / u.EXE_NAME
    assert _entry(registry) == (new_exe, "0.2.0")

    assert u.finish_update_at_start(root, new_exe, "0.2.0", [Recorder()], record_apps_entry=record) is True
    assert _entry(registry) == (new_exe, "0.2.0")


def test_after_pruning_the_apps_entry_still_names_an_existing_exe(root, frozen):
    """The entry starts on 0.1.0, which the second update prunes. After the prune it must name a folder that exists."""
    registry = FakeRegistry()
    record = _recorder(registry)
    record(root / "versions" / "0.1.0" / u.EXE_NAME, "0.1.0")  # what install() wrote when 0.1.0 was installed

    assert u.check_and_stage("0.1.0", github("0.2.0"), root, record_apps_entry=record).status == "update_staged"
    new_exe = root / "versions" / "0.2.0" / u.EXE_NAME
    assert u.finish_update_at_start(root, new_exe, "0.2.0", [Recorder()], record_apps_entry=record) is True
    assert u.check_and_stage("0.2.0", github("0.3.0"), root, record_apps_entry=record).status == "update_staged"

    assert not (root / "versions" / "0.1.0").exists()  # the prune removed the folder the entry named first
    exe, version = _entry(registry)
    assert version == "0.3.0"
    assert exe.is_file()


def test_a_start_with_no_update_repoints_a_stale_apps_entry(root):
    registry = FakeRegistry()
    record = _recorder(registry)
    record(root / "versions" / "0.0.9" / u.EXE_NAME, "0.0.9")
    running = root / "versions" / "0.1.0" / u.EXE_NAME
    assert u.finish_update_at_start(root, running, "0.1.0", [], record_apps_entry=record) is False
    assert _entry(registry) == (running, "0.1.0")


def test_a_start_of_an_older_copy_leaves_the_apps_entry_alone(root):
    u.write_current(root, "0.2.0")
    registry = FakeRegistry()
    older = root / "versions" / "0.1.0" / u.EXE_NAME
    assert u.finish_update_at_start(root, older, "0.1.0", [], record_apps_entry=_recorder(registry)) is False
    assert registry.values == {}


def test_a_registry_fault_never_fails_the_update(root, frozen):
    def broken(exe, version):
        raise PermissionError("the registry key is locked")

    assert u.check_and_stage("0.1.0", github(), root, record_apps_entry=broken).status == "update_staged"
    assert u.read_pending(root) == "0.2.0"
    new_exe = root / "versions" / "0.2.0" / u.EXE_NAME
    assert u.finish_update_at_start(root, new_exe, "0.2.0", [Recorder()], record_apps_entry=broken) is True


# --- the download notice, the extraction caps, Windows device names and the install-folder rule -----------


def test_the_download_is_announced_only_after_its_signature_checks(root, frozen):
    gh = github()
    seen = []
    result = u.check_and_stage("0.1.0", gh, root, announce=lambda version: seen.append((version, list(gh.calls))))
    assert result.status == "update_staged"
    assert len(seen) == 1
    version, calls_when_announced = seen[0]
    assert version == "0.2.0"
    assert asset_url("0.2.0", SIG_NAME) in calls_when_announced  # the signature was fetched and checked
    assert CDN_URL not in calls_when_announced  # the zip had not been asked for yet


def test_an_unsigned_release_is_never_announced(root, frozen):
    announced = []
    result = u.check_and_stage("0.1.0", github(signed_by=OTHER_SEED), root, announce=announced.append)
    assert result.status == "refused"
    assert announced == []


def test_a_zip_that_unpacks_past_the_size_cap_is_refused_and_installs_nothing(root, frozen, monkeypatch):
    monkeypatch.setattr(u, "MAX_EXTRACT_BYTES", 10)  # bundle_zip unpacks to 22 bytes
    result = u.check_and_stage("0.1.0", github(), root)
    assert result.status == "refused"
    assert "unpacks to more than the size limit" in result.reason
    assert version_names(root) == ["0.1.0"]
    assert not list(root.glob("update-*"))


def test_a_zip_with_more_entries_than_the_cap_is_refused(root, frozen, monkeypatch):
    monkeypatch.setattr(u, "MAX_EXTRACT_ENTRIES", 2)  # bundle_zip has three entries
    result = u.check_and_stage("0.1.0", github(), root)
    assert result.status == "refused"
    assert "too many entries" in result.reason
    assert version_names(root) == ["0.1.0"]


def test_a_zip_exactly_at_both_caps_is_staged(root, frozen, monkeypatch):
    monkeypatch.setattr(u, "MAX_EXTRACT_BYTES", 22)  # 8 + 3 + 11 bytes
    monkeypatch.setattr(u, "MAX_EXTRACT_ENTRIES", 3)
    assert u.check_and_stage("0.1.0", github(), root).status == "update_staged"


def _zip_with_corrupt_member(method: int, data: bytes) -> bytes:
    """A zip whose one member is compressed with method, with eight bytes of its compressed data flipped in the middle.

    The damage is in the compressed stream itself, so the decompressor fails before the CRC is ever compared.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", method) as archive:
        archive.writestr(u.EXE_NAME, data)
    raw = bytearray(buffer.getvalue())
    name_len, extra_len = struct.unpack_from("<HH", raw, 26)  # the local file header's name and extra lengths
    compressed_len = struct.unpack_from("<I", raw, 18)[0]  # the local file header's compressed size
    middle = 30 + name_len + extra_len + compressed_len // 2
    for index in range(middle, middle + 8):
        raw[index] ^= 0xFF
    return bytes(raw)


_INCOMPRESSIBLE = bytes(range(256)) * 64


@pytest.mark.parametrize(("method", "error"), [(zipfile.ZIP_BZIP2, OSError), (zipfile.ZIP_LZMA, lzma.LZMAError)])
def test_a_corrupt_bzip2_or_lzma_member_is_refused_and_installs_nothing(root, frozen, method, error):
    zip_bytes = _zip_with_corrupt_member(method, _INCOMPRESSIBLE)
    # Precondition: plain zipfile fails inside the decompressor with the error this case is about.
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        with pytest.raises(error):
            archive.read(u.EXE_NAME)
    before = tree_state(root)
    result = u.check_and_stage("0.1.0", github(zip_bytes=zip_bytes), root)
    assert result.status == "refused"
    assert result.reason == u._EXTRACT_FAILED  # the plain refusal, not the disk-fault message
    assert tree_state(root) == before
    assert not list(root.glob("update-*"))


class _UnreadableSource(io.BytesIO):
    """A member whose data cannot be decompressed: reading it raises, as a corrupt bzip2 or LZMA stream does."""

    def __init__(self, error: Exception) -> None:
        super().__init__(b"")
        self._error = error

    def read(self, *args):
        raise self._error


@pytest.mark.parametrize("error", [OSError("Invalid data stream"), lzma.LZMAError("Corrupt input data")])
def test_copy_within_refuses_a_member_whose_data_cannot_be_read(error):
    with pytest.raises(u.Refused) as caught:
        u._copy_within(_UnreadableSource(error), io.BytesIO(), 10)
    assert str(caught.value) == u._EXTRACT_FAILED


def test_copy_within_leaves_a_disk_error_while_writing_as_an_oserror():
    class FullDisk(io.BytesIO):
        def write(self, data):
            raise OSError(28, "No space left on device")

    with pytest.raises(OSError) as caught:
        u._copy_within(io.BytesIO(b"abcd"), FullDisk(), 10)
    assert not isinstance(caught.value, u.Refused)


def test_copy_within_allows_exactly_its_budget_and_refuses_one_byte_more():
    sink = io.BytesIO()
    assert u._copy_within(io.BytesIO(b"abcd"), sink, 4) == 4
    assert sink.getvalue() == b"abcd"
    refused = io.BytesIO()
    with pytest.raises(u.TooLarge):
        u._copy_within(io.BytesIO(b"abcde"), refused, 4)
    assert refused.getvalue() == b""  # not one byte past the budget is written


@pytest.mark.parametrize(
    "entry",
    ["CON", "nul.txt", "dir/COM1", "lpt9.log", "aux/x.txt", "Com1 .txt", "CONIN$", "COM¹.txt"],
)
def test_a_windows_device_name_entry_is_refused_and_writes_nothing(root, frozen, entry):
    zip_bytes = make_zip({u.EXE_NAME: b"MZ", entry: b"x"})
    result = u.check_and_stage("0.1.0", github(zip_bytes=zip_bytes), root)
    assert result.status == "refused"
    assert "Windows device" in result.reason
    assert version_names(root) == ["0.1.0"]
    assert not list(root.glob("update-*"))


@pytest.mark.parametrize("name", ["CONFIG.txt", "COM10.txt", "console/readme.md", "nulls.txt", "lptx/file", "a/com1x"])
def test_names_that_only_begin_like_device_names_are_allowed(name):
    assert u._member_parts(name)


@pytest.mark.parametrize("outside", ["dist", "sibling"])
def test_a_bundle_run_outside_the_install_folder_stages_nothing(tmp_path, root, monkeypatch, outside):
    running = (tmp_path / "dist" / "MeridianDesktop" if outside == "dist" else tmp_path / "install-old") / u.EXE_NAME
    monkeypatch.setattr(u, "_is_frozen", lambda: True)
    monkeypatch.setattr(u, "_running_exe", lambda: running)
    gh = github()
    before = tree_state(root)
    result = u.check_and_stage("0.1.0", gh, root)
    assert result == u.UpdateResult("refused", reason=u.NOT_INSTALLED_REASON)
    assert gh.calls == []
    assert tree_state(root) == before


@pytest.mark.parametrize("digits", [4300, 4301, 20_000])
def test_a_desktop_tag_of_any_length_never_raises_from_check_for_update(digits):
    # 4300 nines is still a number Python can read, so it is a notice. Longer ones are skipped as malformed.
    result = u.check_for_update("0.1.0", github(tag=u.DESKTOP_TAG_PREFIX + "9" * digits))
    assert isinstance(result, (u.UpdateNotice, u.UpToDate))


# --- the shared repo: core releases are ignored, desktop releases are picked by version -------------


def _release_entry(tag: str, *, draft: bool = False, prerelease: bool = False) -> dict:
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease, "assets": []}


def test_the_newest_desktop_release_by_version_wins_and_core_releases_are_ignored():
    entries = [
        _release_entry("v2.3.0"),  # a core release, first in the list
        _release_entry("desktop-v0.2.0"),
        _release_entry("desktop-v0.10.0"),
        _release_entry("desktop-v0.9.0"),
    ]
    gh = FakeGitHub({u.RELEASES_LIST_URL: (200, {}, json.dumps(entries).encode())})
    assert u.check_for_update("0.1.0", gh) == u.UpdateNotice("0.10.0", page("0.10.0"))


def test_a_desktop_release_marked_prerelease_is_still_offered():
    # Desktop releases are published as prereleases so the repo's "latest" stays a core release.
    entries = [_release_entry("desktop-v0.2.0", prerelease=True)]
    gh = FakeGitHub({u.RELEASES_LIST_URL: (200, {}, json.dumps(entries).encode())})
    assert u.check_for_update("0.1.0", gh) == u.UpdateNotice("0.2.0", page("0.2.0"))


def test_a_full_page_of_core_releases_with_no_desktop_release_is_refused_not_up_to_date():
    entries = [_release_entry(f"v1.0.{i}") for i in range(u.RELEASES_PER_PAGE)]
    gh = FakeGitHub({u.RELEASES_LIST_URL: (200, {}, json.dumps(entries).encode())})
    result = u.check_for_update("0.1.0", gh)
    assert isinstance(result, u.Refused)
    assert result.reason == u._NO_DESKTOP_IN_PAGE


def test_a_short_page_of_core_releases_with_no_desktop_release_is_up_to_date():
    gh = FakeGitHub({u.RELEASES_LIST_URL: (200, {}, json.dumps([_release_entry("v2.3.0")]).encode())})
    assert u.check_for_update("0.1.0", gh) == u.UpToDate()


def test_fetch_latest_takes_the_newest_desktop_release_not_the_first_one():
    gh = github("0.3.0")
    newer = json.loads(gh.routes[u.RELEASES_LIST_URL][2])
    older = json.loads(release_body("0.2.0"))
    gh.routes[u.RELEASES_LIST_URL] = (200, {}, json.dumps(older + newer).encode())
    assert u.fetch_latest(gh).version == "0.3.0"


def test_fetch_latest_with_no_desktop_release_is_refused():
    gh = FakeGitHub({u.RELEASES_LIST_URL: (200, {}, json.dumps([_release_entry("v2.3.0")]).encode())})
    with pytest.raises(u.Refused) as caught:
        u.fetch_latest(gh)
    assert caught.value.reason == u._NO_DESKTOP_RELEASE
