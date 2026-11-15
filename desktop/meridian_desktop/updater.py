"""Desktop update: a newer release is downloaded, verified and staged, then started on the owner's restart.

Pure Python, no Qt import. check_for_update is the notify-only check. It reads only the newest page of releases from
GitHub (RELEASES_LIST_URL) and keeps the newest desktop-v<version> release. It returns one of four results:

    UpdateNotice(version, release_page_url)   a newer desktop release is published.
    UpToDate                                  no newer desktop release is published, or none exists yet.
    Offline                                   GitHub could not be reached, or it is failing for now.
    Refused(reason)                           the reply could not be read, or it is not allowed.

check_and_stage is the background update. It is a job, not a button, and it runs at launch and every six hours
while the app is open (ui/update_notice.py). Nothing it downloads is trusted before it is verified:

1. fetch_latest reads the newest desktop-v<version> release from GitHub. It needs three assets: MeridianDesktop-<version>.zip,
   SHA256SUMS and SHA256SUMS.sig. A release with no SHA256SUMS.sig is refused as not signed.
2. SHA256SUMS.sig is the base64 of a 64-byte Ed25519 signature over the exact bytes of SHA256SUMS. It must verify
   under RELEASE_PUBLIC_KEY (meridian_desktop/release_key.py). Otherwise nothing else is downloaded.
3. The zip is downloaded into a scratch folder. Its SHA-256 must match its line in SHA256SUMS, and it must be
   under MAX_DOWNLOAD_BYTES. A failure installs nothing.
4. install_update extracts the zip into versions/<version>.partial. Entries that are absolute, carry a drive or
   a stream name, contain "..", are symbolic links, or are junctions and other Windows reparse points are
   refused. A corrupt or encrypted member is refused too. MeridianDesktop.exe must be in the result. The folder is
   then renamed to versions/<version>, and pending.json records the version.

Staging does not touch the shortcuts. They keep pointing at the running version until the owner restarts. Then
launch_after_update starts versions/<version>/MeridianDesktop.exe with --after-update <old>, and that process calls
finish_update_at_start, which points the shortcuts at itself, makes it current and clears pending.json. A start with
no pending update also points the shortcuts at the running version, when it is not older than the current one.

The Apps and Features entry follows the same rule. After each successful staging, and after each start that makes a
version current, record_apps_entry (when the caller gives one) points the entry at that version's exe. The updater
never writes the registry itself, and a failed write never fails the update.

Every request goes through an injectable transport, and urllib never follows a redirect. Each hop is checked
against the https host allowlist before it is requested. Tests pass a fake transport, so they make no network call.

Layout under install_root:

    versions/<version>/MeridianDesktop.exe   installed versions. The current and the previous one are kept.
    versions/<version>.partial               an extraction in progress. Removed on any failure.
    versions/rolled_back.json                the version rolled back from. Releases at or below it are held
                                             until a newer release is published. Cleared by that update.
    current.json                             {"version": v}: the version the shortcuts point at.
    pending.json                             {"version": v}: a staged update that waits for a restart.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import lzma
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal

from meridian_desktop import ed25519
from meridian_desktop.release_key import RELEASE_PUBLIC_KEY_B64
from meridian_desktop.version import APP_VERSION

# The shared repo. Core releases live here too, so the desktop cannot use the "latest release" endpoint (it returns the
# newest core release). It reads the newest page of releases and keeps only tags that start with DESKTOP_TAG_PREFIX.
REPO = "meridianmcp/Meridian"
RELEASES_PER_PAGE = 100
RELEASES_LIST_URL = f"https://api.github.com/repos/{REPO}/releases?per_page={RELEASES_PER_PAGE}"
DESKTOP_TAG_PREFIX = "desktop-v"
ALLOWED_HOSTS = frozenset(
    {"api.github.com", "github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
)
EXE_NAME = "MeridianDesktop.exe"
VERSIONS_DIR = "versions"
SUMS_NAME = "SHA256SUMS"
SIG_NAME = "SHA256SUMS.sig"
CURRENT_NAME = "current.json"
PENDING_NAME = "pending.json"
AFTER_UPDATE_FLAG = "--after-update"
MAX_DOWNLOAD_BYTES = 400 * 1024 * 1024  # the release zip
MAX_SMALL_BYTES = 1024 * 1024  # the release JSON, SHA256SUMS and SHA256SUMS.sig
MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 30.0
DEV_RUN_REASON = "dev run: updates are not applied"
USER_AGENT = f"MeridianDesktop/{APP_VERSION} (updater)"
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
SKIP_MARKER = "rolled_back.json"
HELD_REASON = (
    "Update {version} is held after rollback. It will not be installed again until a newer release is published."
)
UNSIGNED_REASON = "The update is not signed by the Meridian release key."
RELEASE_PUBLIC_KEY = base64.b64decode(RELEASE_PUBLIC_KEY_B64)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400  # Windows: a junction, a symbolic link or another reparse point
_CHUNK = 64 * 1024
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*")
_SUM_LINE = re.compile(r"([0-9a-fA-F]{64})[ \t]+\*?(\S.*)")
_VERSION_REFUSED = "A version must be plain dotted numbers such as 0.2.0."
_NO_DESKTOP_RELEASE = "No desktop release has been published yet."
_NO_DESKTOP_IN_PAGE = (
    "The newest releases in the repository are all core releases. The desktop release cannot be checked from them."
)
# Turned on now that release signing exists. Setting it False is the kill switch: check_and_stage then refuses before
# any request, and install_update raises InstallDisabled before it writes anything.
INSTALL_ENABLED = True
INSTALL_OFF_REASON = "Installing updates is turned off in this build."
# The caps on what a release zip may unpack to. Each is checked from the zip's own headers before anything is written,
# and again while the bytes are copied, so a header that lies about a size is still stopped.
MAX_EXTRACT_BYTES = 2 * 1024 * 1024 * 1024  # the total uncompressed size of the release zip
MAX_EXTRACT_ENTRIES = 20_000  # the number of entries (files and folders) in the release zip
# The names Windows reserves for devices. Such a name, with or without an extension, cannot be a file on Windows.
_WINDOWS_DEVICE = re.compile(
    r"CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]|CONIN\$|CONOUT\$", re.IGNORECASE
)
_DEVICE_NAME_REFUSED = "The release zip has an entry named like a Windows device. Nothing was installed."
_TOO_MANY_ENTRIES = "The release zip has too many entries. Nothing was installed."
_TOO_LARGE_UNPACKED = "The release zip unpacks to more than the size limit. Nothing was installed."
NOT_INSTALLED_REASON = (
    "This copy runs from outside the install folder, so it does not update that folder. "
    "Run the installed copy to update."
)


class UpdateError(Exception):
    """An update step failed. The message is plain text that can be shown to the user."""

    @property
    def reason(self) -> str:
        """The plain text message. check_for_update returns it as the reason in a Refused or an Offline."""
        return str(self)


class Refused(UpdateError):
    """The update is refused. The installed app is left as it was."""


class TooLarge(Refused):
    """A download is over its size cap."""


class Offline(UpdateError):
    """GitHub could not be reached, or it is failing for now. Try again later."""


class InstallDisabled(Refused):
    """Installing is off in this build. Nothing was downloaded or written."""


Status = Literal["up_to_date", "update_staged", "refused", "offline"]


@dataclass(frozen=True)
class UpdateResult:
    status: Status
    version: str | None = None  # the release version, for up_to_date and update_staged
    reason: str = ""  # plain text, for refused and offline


@dataclass(frozen=True)
class Release:
    version: str
    zip_name: str
    zip_url: str
    zip_size: int | None
    sums_url: str
    sig_url: str = ""  # the SHA256SUMS.sig asset. An empty address is refused as not signed.


@dataclass(frozen=True)
class Reply:
    """One HTTP reply. The body is empty when a download went to a file, and for every redirect or error."""

    status: int
    headers: Mapping[str, str]  # names in lower case
    body: bytes


# A transport takes a URL, the most bytes it may return, and an optional file for a 2xx body to stream into.
# It returns the reply without following redirects. It raises TooLarge when the body is over the cap, and
# OSError, http.client.HTTPException or ValueError for a network fault.
Transport = Callable[[str, int, Path | None], Reply]
# A shortcut writer repoints one shortcut at the exe it is given. The caller binds the shortcut's own .lnk path.
ShortcutWriter = Callable[[Path], None]
# Points the Apps and Features entry at one installed version: its exe, and the version it is. Best effort.
AppsEntryWriter = Callable[[Path, str], None]
# Called with the version just before its download starts, so the window can say what is downloading.
Announce = Callable[[str], None]


class _RepointFailed(Exception):
    def __init__(self, undone: bool) -> None:
        super().__init__("a shortcut could not be written")
        self.undone = undone


# --- versions -------------------------------------------------------------------------------------


def parse_version(text: object) -> tuple[int, ...]:
    """Return the integer parts of a plain dotted version such as 0.2.0. Anything else is refused."""
    if not isinstance(text, str) or not _VERSION.fullmatch(text):
        raise Refused(_VERSION_REFUSED)
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:  # a part over Python's int digit limit
        raise Refused(_VERSION_REFUSED) from None


def is_newer(candidate: str, current: str) -> bool:
    """True when candidate is a higher version than current. Parts compare as integers, so 0.10.0 > 0.9.0."""
    a = parse_version(candidate)
    b = parse_version(current)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


# --- URLs and the release ---------------------------------------------------------------------------


def require_allowed_url(url: str) -> None:
    """Refuse any URL that is not https on an allowed GitHub host, or that carries a user name or password."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise Refused("A download address is not valid.") from None
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or host not in ALLOWED_HOSTS
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
    ):
        raise Refused("A download address is not an allowed https GitHub address.")


def _fetch(transport: Transport, url: str, max_bytes: int, dest: Path | None = None) -> Reply:
    """Fetch url, following redirects one hop at a time. Each hop is checked before the transport is asked for it."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        require_allowed_url(current)
        try:
            reply = transport(current, max_bytes, dest)
        except (OSError, http.client.HTTPException, ValueError):
            # A fault from the network. The original exception is not attached, so no request detail leaks.
            raise Offline("GitHub could not be reached. Try again later.") from None
        if reply.status in REDIRECT_STATUSES:
            location = reply.headers.get("location", "")
            if not location:
                raise Refused("GitHub sent a redirect with no address.")
            current = urllib.parse.urljoin(current, location)
            continue
        return reply
    raise Refused("The download was redirected too many times.")


def _require_ok(reply: Reply, what: str) -> None:
    if 200 <= reply.status < 300:
        return
    if reply.status == 429 or reply.status >= 500:
        raise Offline("GitHub is not available right now. Try again later.")
    raise Refused(f"GitHub did not return {what} (HTTP {reply.status}).")


def _releases_page(transport: Transport, *, forbidden_is_offline: bool) -> list[dict[str, Any]]:
    """Read the newest page of releases from REPO. Only a JSON list of release objects is accepted.

    The notify check passes forbidden_is_offline=True: a 403 can be a rate limit as well as a refusal, so the user is
    told to try again later. The download path keeps a 403 as a refusal, as it was before.
    """
    reply = _fetch(transport, RELEASES_LIST_URL, MAX_SMALL_BYTES)
    if forbidden_is_offline and reply.status == 403:
        raise Offline("GitHub is limiting requests right now. Try again later.")
    _require_ok(reply, "the release list")
    try:
        data = json.loads(reply.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):  # RecursionError: a body nested too deeply to parse
        raise Refused("The release list could not be read.") from None
    if not isinstance(data, list):
        raise Refused("The release list could not be read.")
    return [entry for entry in data if isinstance(entry, dict)]


def _newest_desktop_release(transport: Transport, *, forbidden_is_offline: bool) -> tuple[str, dict[str, Any]] | None:
    """Return (version, release) for the newest published desktop release on the newest page, or None if there is none.

    Only tags that start with DESKTOP_TAG_PREFIX count, drafts are skipped, and a tag whose rest is not a plain dotted
    version is skipped. When the page is full, a desktop release may sit beyond it, so the check refuses rather than
    reporting that nothing is newer.
    """
    entries = _releases_page(transport, forbidden_is_offline=forbidden_is_offline)
    best: tuple[str, dict[str, Any]] | None = None
    for entry in entries:
        tag = entry.get("tag_name")
        if entry.get("draft") is True or not isinstance(tag, str) or not tag.startswith(DESKTOP_TAG_PREFIX):
            continue
        version = tag[len(DESKTOP_TAG_PREFIX) :]
        try:
            parse_version(version)
        except Refused:
            continue
        if best is None or is_newer(version, best[0]):
            best = (version, entry)
    if best is None and len(entries) >= RELEASES_PER_PAGE:
        raise Refused(_NO_DESKTOP_IN_PAGE)
    return best


def fetch_latest(transport: Transport) -> Release:
    """Read the newest published desktop release. Refuse it unless it has the zip, SHA256SUMS and SHA256SUMS.sig assets."""
    found = _newest_desktop_release(transport, forbidden_is_offline=False)
    if found is None:
        raise Refused(_NO_DESKTOP_RELEASE)
    version, data = found
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise Refused("The latest release has no asset list.")
    by_name: dict[str, dict[str, Any]] = {}
    for asset in assets:
        if isinstance(asset, dict) and isinstance(asset.get("name"), str):
            if asset["name"] in by_name:
                raise Refused("The latest release lists an asset twice.")
            by_name[asset["name"]] = asset
    zip_name = f"MeridianDesktop-{version}.zip"
    zip_asset = by_name.get(zip_name)
    sums_asset = by_name.get(SUMS_NAME)
    if zip_asset is None:
        raise Refused(f"The latest release has no {zip_name} asset.")
    if sums_asset is None:
        raise Refused("The latest release has no SHA256SUMS asset.")
    sig_asset = by_name.get(SIG_NAME)
    if sig_asset is None:
        raise Refused(UNSIGNED_REASON)
    zip_url = zip_asset.get("browser_download_url")
    sums_url = sums_asset.get("browser_download_url")
    sig_url = sig_asset.get("browser_download_url")
    if not isinstance(zip_url, str) or not isinstance(sums_url, str):
        raise Refused("The latest release has an asset with no download address.")
    if not isinstance(sig_url, str):
        raise Refused(UNSIGNED_REASON)
    size = zip_asset.get("size")
    if isinstance(size, bool) or not isinstance(size, int):
        size = None
    elif size > MAX_DOWNLOAD_BYTES:
        raise TooLarge("The release zip is over the size limit.")
    return Release(
        version=version,
        zip_name=zip_name,
        zip_url=zip_url,
        zip_size=size,
        sums_url=sums_url,
        sig_url=sig_url,
    )


# --- verification -----------------------------------------------------------------------------------


def expected_sha256(sums_text: str, file_name: str) -> str:
    """Return the SHA-256 that SHA256SUMS lists for file_name. The name must be listed exactly once."""
    found = []
    for raw in sums_text.splitlines():
        match = _SUM_LINE.fullmatch(raw.strip())
        if match is not None and match.group(2) == file_name:
            found.append(match.group(1).lower())
    if len(found) != 1:
        raise Refused("SHA256SUMS does not list the release zip exactly once.")
    return found[0]


def verify_sums_signature(sums: bytes, signature_text: bytes, public_key: bytes) -> None:
    """Refuse unless signature_text is the base64 of an Ed25519 signature of the exact bytes of sums under public_key."""
    try:
        signature = base64.b64decode(signature_text.strip(), validate=True)
    except (ValueError, TypeError):  # binascii.Error is a ValueError
        raise Refused(UNSIGNED_REASON) from None
    if len(signature) != 64 or not ed25519.verify(public_key, sums, signature):
        raise Refused(UNSIGNED_REASON)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text(body: bytes) -> str:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("SHA256SUMS could not be read.") from None


def _download_verified(
    release: Release,
    transport: Transport,
    scratch: Path,
    public_key: bytes,
    announce: Announce | None = None,
) -> Path:
    """Download SHA256SUMS and its signature, verify the signature, then download the zip and check its SHA-256.

    Nothing is downloaded from the zip address until the signature verifies, and announce is called only then, so the
    window says "Downloading" only for a release that is signed. A failure leaves scratch with no zip.
    """
    if not release.sig_url:
        raise Refused(UNSIGNED_REASON)
    sums_reply = _fetch(transport, release.sums_url, MAX_SMALL_BYTES)
    _require_ok(sums_reply, SUMS_NAME)
    sig_reply = _fetch(transport, release.sig_url, MAX_SMALL_BYTES)
    if sig_reply.status == 404:
        raise Refused(UNSIGNED_REASON)
    _require_ok(sig_reply, SIG_NAME)
    verify_sums_signature(sums_reply.body, sig_reply.body, public_key)
    if announce is not None:
        announce(release.version)
    expected = expected_sha256(_text(sums_reply.body), release.zip_name)
    zip_path = scratch / release.zip_name
    zip_reply = _fetch(transport, release.zip_url, MAX_DOWNLOAD_BYTES, zip_path)
    _require_ok(zip_reply, "the release zip")
    if not zip_path.is_file():
        raise Refused("The release zip did not arrive.")
    if zip_path.stat().st_size > MAX_DOWNLOAD_BYTES:
        raise TooLarge("The release zip is over the size limit.")
    if _sha256_file(zip_path) != expected:
        raise Refused("The download does not match SHA256SUMS. Nothing was installed.")
    return zip_path


# --- extraction and install -------------------------------------------------------------------------

_UNSAFE_ENTRY = "The release zip has an entry with an unsafe path. Nothing was installed."


def _member_parts(name: str) -> tuple[str, ...]:
    """The relative path parts a zip entry may take, after backslashes become slashes. () means skip it.

    Refuses an absolute path, a drive or stream name (a colon), a NUL, and any ".." part.
    """
    text = name.replace("\\", "/")
    if text.startswith("/") or "\x00" in text:
        raise Refused(_UNSAFE_ENTRY)
    parts = tuple(part for part in text.split("/") if part not in ("", "."))
    if any(part == ".." or ":" in part for part in parts):
        raise Refused(_UNSAFE_ENTRY)
    for part in parts:
        # Windows ignores the extension and the spaces around the name: NUL, nul.txt and "NUL .log" are all the device.
        stem = part.split(".", 1)[0].strip(" ")
        if _WINDOWS_DEVICE.fullmatch(stem):
            raise Refused(_DEVICE_NAME_REFUSED)
    return parts


def _remove_tree(path: Path, *, strict: bool = False) -> None:
    """Delete a folder tree. With strict, a folder that cannot be removed refuses the update."""
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError:
        if strict:
            raise Refused("An old copy of the update could not be removed. Close the app and try again.") from None


def _copy_within(source: BinaryIO, sink: BinaryIO, budget: int) -> int:
    """Copy source to sink and return the bytes copied. Raises TooLarge before a byte past budget is written.

    A read that fails is a corrupt member: bzip2 data raises OSError and LZMA data raises LZMAError. Both are refused as
    an unreadable zip. A write that fails is a disk fault and is left as it is.
    """
    copied = 0
    while True:
        try:
            chunk = source.read(_CHUNK)
        except (OSError, lzma.LZMAError):
            raise Refused(_EXTRACT_FAILED) from None
        if not chunk:
            return copied
        copied += len(chunk)
        if copied > budget:
            raise TooLarge(_TOO_LARGE_UNPACKED)
        sink.write(chunk)


_EXTRACT_FAILED = "The release zip could not be extracted. Nothing was installed."


def _extract_verified(zip_path: Path, staging: Path) -> None:
    """Extract the zip into staging after checking every entry. The result must hold MeridianDesktop.exe.

    A corrupt member (bad CRC or deflate data), an encrypted member, or any other unreadable member is refused.
    """
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise Refused("The release zip is not a valid zip file.") from None
    try:
        with archive:
            infos = archive.infolist()
            if len(infos) > MAX_EXTRACT_ENTRIES:
                raise Refused(_TOO_MANY_ENTRIES)
            if sum(info.file_size for info in infos) > MAX_EXTRACT_BYTES:
                raise TooLarge(_TOO_LARGE_UNPACKED)
            plan = []
            for info in infos:
                parts = _member_parts(info.filename)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise Refused(_UNSAFE_ENTRY)
                if (info.external_attr & 0xFFFF) & _FILE_ATTRIBUTE_REPARSE_POINT:
                    raise Refused(_UNSAFE_ENTRY)
                if parts:
                    plan.append((info, parts))
            staging.mkdir(parents=True)
            staging_root = staging.resolve()
            remaining = MAX_EXTRACT_BYTES
            for info, parts in plan:
                target = staging.joinpath(*parts)
                if not target.resolve().is_relative_to(staging_root):
                    raise Refused(_UNSAFE_ENTRY)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as sink:
                    remaining -= _copy_within(source, sink, remaining)
    except (zipfile.BadZipFile, RuntimeError, zlib.error, ValueError, EOFError):
        # RuntimeError is an encrypted member. EOFError is a member whose data runs past the end of the file. The
        # others are a bad CRC or a corrupt compressed stream.
        raise Refused(_EXTRACT_FAILED) from None
    if not (staging / EXE_NAME).is_file():
        raise Refused(f"The release zip does not contain {EXE_NAME}. Nothing was installed.")


def _point_shortcuts(writers: Sequence[ShortcutWriter], target: Path, fallback: Path) -> None:
    """Run every shortcut writer with target. On a failure, point the ones already done back at fallback.

    Raises _RepointFailed with undone=True when no shortcut is left pointing at target.
    """
    done: list[ShortcutWriter] = []
    try:
        for write in writers:
            write(target)
            done.append(write)
    except Exception:  # an injected writer may fail in any way; the failure is reported, never raised as is
        undone = True
        if done:
            if fallback.is_file():
                for write in done:
                    try:
                        write(fallback)
                    except Exception:
                        undone = False
            else:
                undone = False
        raise _RepointFailed(undone) from None


def _prune(versions: Path, keep: set[str]) -> None:
    """Delete every version folder not in keep. A folder that cannot be removed is left for the next update."""
    for entry in versions.iterdir():
        if entry.is_dir() and entry.name not in keep:
            _remove_tree(entry)


# --- the layout pointers: current.json and pending.json ---------------------------------------------------


def version_dir(install_root: Path | str, version: str) -> Path:
    """The folder that holds one installed version."""
    return Path(install_root) / VERSIONS_DIR / version


def exe_for(install_root: Path | str, version: str) -> Path:
    """The exe of one installed version. The shortcuts point at the one named by current.json."""
    return version_dir(install_root, version) / EXE_NAME


def _read_version_file(path: Path, key: str) -> str | None:
    """The plain version held under key in a small JSON file, or None when the file is missing or unusable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get(key) if isinstance(data, dict) else None
    try:
        parse_version(version)
    except Refused:
        return None
    return version


def _write_version_file(path: Path, key: str, version: str) -> None:
    """Write {key: version} through a temp file and os.replace, so a reader never sees a half-written file."""
    parse_version(version)
    temp = path.with_name(f"{path.name}.tmp")
    try:
        temp.write_text(json.dumps({key: version}), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        _remove_file(temp)
        raise


def read_current(install_root: Path | str) -> str | None:
    """The version the shortcuts point at, or None when current.json is missing or unusable."""
    return _read_version_file(Path(install_root) / CURRENT_NAME, "version")


def write_current(install_root: Path | str, version: str) -> None:
    """Record the version the shortcuts point at. Raises OSError when the file cannot be written."""
    _write_version_file(Path(install_root) / CURRENT_NAME, "version", version)


def read_pending(install_root: Path | str) -> str | None:
    """The staged version that waits for a restart, or None when there is none."""
    return _read_version_file(Path(install_root) / PENDING_NAME, "version")


def write_pending(install_root: Path | str, version: str) -> None:
    """Record a staged version. Raises OSError when the file cannot be written."""
    _write_version_file(Path(install_root) / PENDING_NAME, "version", version)


def clear_pending(install_root: Path | str) -> None:
    """Remove the staged-update record. Best effort: a record that cannot be removed is only ever a prompt."""
    _remove_file(Path(install_root) / PENDING_NAME)


def ready_version(install_root: Path | str, running_version: str) -> str | None:
    """The staged version that is newer than running_version and whose exe is on disk, or None."""
    pending = read_pending(install_root)
    if pending is None or not is_newer(pending, running_version):
        return None
    if not exe_for(install_root, pending).is_file():
        return None
    return pending


def install_update(
    release: Release,
    current_version: str,
    transport: Transport,
    install_root: Path,
    *,
    public_key: bytes | None = None,
    announce: Announce | None = None,
    record_apps_entry: AppsEntryWriter | None = None,
) -> Path:
    """Download, verify, extract and stage a release, then record it as pending. Return the version folder.

    The shortcuts are not touched. They point at the running version until the owner restarts. The Apps and Features
    entry is pointed at the new version once it is recorded as pending. Keeps the new version, the running one and the
    current one, and deletes the rest. Raises InstallDisabled while INSTALL_ENABLED is False, before anything is
    created or requested. Otherwise raises Refused, Offline or OSError. Does not check whether the app is frozen;
    check_and_stage does that. public_key defaults to RELEASE_PUBLIC_KEY.
    """
    if not INSTALL_ENABLED:
        raise InstallDisabled(INSTALL_OFF_REASON)
    key = RELEASE_PUBLIC_KEY if public_key is None else public_key
    install_root = Path(install_root)
    versions = install_root / VERSIONS_DIR
    final = versions / release.version
    staging = versions / f"{release.version}.partial"
    install_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=install_root, prefix="update-") as scratch:
        zip_path = _download_verified(release, transport, Path(scratch), key, announce)
        _remove_tree(staging, strict=True)
        try:
            _extract_verified(zip_path, staging)
            _remove_tree(final, strict=True)
            staging.rename(final)
        except BaseException:
            _remove_tree(staging)
            raise
    try:
        write_pending(install_root, release.version)
    except OSError:
        _remove_tree(final)
        raise Refused("The update could not be recorded. Nothing was changed.") from None
    # Recorded before pruning, so the entry never names a folder that the prune below is about to delete.
    _record_apps_entry(record_apps_entry, final / EXE_NAME, release.version)
    keep = {release.version, current_version, read_current(install_root)}
    _prune(versions, keep={name for name in keep if name})
    return final


def _remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass  # best effort: a marker that cannot be removed is only ever a hold, never a risk


def _read_skip_marker(versions: Path) -> str | None:
    """The version rolled back from, or None when there is no usable marker. A marker that does not parse is ignored."""
    try:
        data = json.loads((versions / SKIP_MARKER).read_text(encoding="utf-8"))
        version = data.get("rolled_back_from") if isinstance(data, dict) else None
        parse_version(version)
    except (OSError, ValueError, Refused):
        return None
    return version


def _write_skip_marker(versions: Path, rolled_back_from: str) -> None:
    """Record the version rolled back from. Keeps the higher of the new and any existing marker version."""
    held = rolled_back_from
    previous = _read_skip_marker(versions)
    if previous is not None and is_newer(previous, held):
        held = previous
    temp = versions / f"{SKIP_MARKER}.tmp"
    try:
        temp.write_text(json.dumps({"rolled_back_from": held}), encoding="utf-8")
        os.replace(temp, versions / SKIP_MARKER)
    except OSError:
        _remove_file(temp)
        raise Refused("The rollback could not be recorded. Nothing was changed.") from None


def _clear_skip_marker(versions: Path) -> None:
    _remove_file(versions / SKIP_MARKER)


def rollback(current_version: str, install_root: Path, shortcut_writers: Sequence[ShortcutWriter]) -> str:
    """Point the shortcuts at the newest installed version below current_version, and return that version.

    Records current_version in the skip marker first, so check_and_stage does not stage it again. The marker is
    removed again if the shortcuts could not be changed at all. Refuses when no earlier version folder with an exe
    is left, or when the marker cannot be written. current.json then names the version the shortcuts point at.
    """
    install_root = Path(install_root)
    versions = install_root / VERSIONS_DIR
    current = parse_version(current_version)
    best: tuple[tuple[int, ...], Path] | None = None
    if versions.is_dir():
        for entry in versions.iterdir():
            if not entry.is_dir() or not (entry / EXE_NAME).is_file():
                continue
            try:
                parsed = parse_version(entry.name)
            except Refused:
                continue
            if parsed < current and (best is None or parsed > best[0]):
                best = (parsed, entry)
    if best is None:
        raise Refused("There is no earlier version to roll back to.")
    _write_skip_marker(versions, current_version)
    try:
        _point_shortcuts(shortcut_writers, best[1] / EXE_NAME, versions / current_version / EXE_NAME)
    except _RepointFailed as failed:
        if failed.undone:
            _clear_skip_marker(versions)  # every shortcut is back on current_version, so nothing was rolled back
        raise Refused("The shortcuts could not be pointed back at the earlier version.") from None
    try:
        write_current(install_root, best[1].name)
    except OSError:
        pass  # the shortcuts already point at it; the next start that finds current.json stale sorts it out
    return best[1].name


# --- top level ----------------------------------------------------------------------------------


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def running_frozen() -> bool:
    """True when this process is the frozen bundle. The update is only applied to a frozen bundle."""
    return _is_frozen()


def _running_exe() -> Path:
    """The exe this process runs from. A function, so a test can place the bundle anywhere."""
    return Path(sys.executable)


def _running_inside(install_root: Path) -> bool:
    """True when this process runs from inside install_root. A bundle run from the build folder never updates it."""
    try:
        exe = os.path.normcase(str(_running_exe().resolve()))
        root = os.path.normcase(str(Path(install_root).resolve()))
    except OSError:
        return False
    return Path(exe).is_relative_to(Path(root))


def _record_apps_entry(record: AppsEntryWriter | None, exe: Path, version: str) -> None:
    """Point the Apps and Features entry at one installed version. Best effort: a registry fault never fails the update.

    The entry is a convenience for Apps and Features. A write that fails leaves the entry as it was, and the next
    staging or start tries again.
    """
    if record is None:
        return
    try:
        record(exe, version)
    except OSError:
        pass


def check_and_stage(
    current_version: str,
    transport: Transport,
    install_root: Path,
    *,
    public_key: bytes | None = None,
    announce: Announce | None = None,
    record_apps_entry: AppsEntryWriter | None = None,
) -> UpdateResult:
    """Check GitHub for a newer release and stage it. Never raises for a network or download fault.

    While INSTALL_ENABLED is False, it returns refused at once with INSTALL_OFF_REASON and makes no request. A source
    run (not frozen) returns refused at once and makes no request. A frozen copy that runs from outside install_root,
    such as a bundle in the build folder, is refused the same way, so it never writes into the real install. Otherwise
    the result is up_to_date, update_staged (the new version is downloaded, verified and recorded as pending, and the
    shortcuts keep pointing at the running version until the restart), refused (with the plain reason), or offline.
    A release whose version is already staged is not downloaded again. After a rollback, a release that is not newer
    than the rolled-back version is held: it is refused before anything is downloaded, and it stays held until a newer
    release is staged, which clears the marker. A successful staging points the Apps and Features entry at the new
    version through record_apps_entry, when one is given.
    """
    if not INSTALL_ENABLED:
        return UpdateResult("refused", reason=INSTALL_OFF_REASON)
    if not _is_frozen():
        return UpdateResult("refused", reason=DEV_RUN_REASON)
    root = Path(install_root)
    if not _running_inside(root):
        return UpdateResult("refused", reason=NOT_INSTALLED_REASON)
    versions = root / VERSIONS_DIR
    try:
        release = fetch_latest(transport)
        if not is_newer(release.version, current_version):
            return UpdateResult("up_to_date", version=release.version)
        held = _read_skip_marker(versions)
        if held is not None and not is_newer(release.version, held):
            reason = HELD_REASON.format(version=release.version)
            return UpdateResult("refused", version=release.version, reason=reason)
        if read_pending(root) == release.version and exe_for(root, release.version).is_file():
            return UpdateResult("update_staged", version=release.version)
        install_update(
            release,
            current_version,
            transport,
            root,
            public_key=public_key,
            announce=announce,
            record_apps_entry=record_apps_entry,
        )
        _clear_skip_marker(versions)
    except Offline as exc:
        return UpdateResult("offline", reason=str(exc))
    except Refused as exc:
        return UpdateResult("refused", reason=str(exc))
    except OSError:
        # A disk fault while staging. The message says nothing about the path, and the installed app is untouched.
        return UpdateResult("refused", reason="The update could not be written. The installed app was not changed.")
    return UpdateResult("update_staged", version=release.version)


def launch_after_update(exe: Path, old_version: str, *, popen: Callable[..., object] = subprocess.Popen) -> None:
    """Start a staged exe with --after-update <old_version>, detached and with no shell. Raises Refused when it cannot.

    The caller then quits this app. The new process finishes the update at its start (finish_update_at_start).
    """
    exe = Path(exe)
    parse_version(old_version)
    if not exe.is_file():
        raise Refused("The new version is missing. It will be staged again at the next check.")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        popen(
            [str(exe), AFTER_UPDATE_FLAG, old_version],
            cwd=str(exe.parent),
            creationflags=flags,
            close_fds=True,
            shell=False,
        )
    except OSError:
        raise Refused("The new version could not be started. The update is still ready; try again.") from None


def _is_version_exe(install_root: Path, exe: Path, version: str) -> bool:
    try:
        return os.path.normcase(str(exe.resolve())) == os.path.normcase(str(exe_for(install_root, version).resolve()))
    except OSError:
        return False


def finish_update_at_start(
    install_root: Path | str,
    running_exe: Path | str,
    running_version: str,
    shortcut_writers: Sequence[ShortcutWriter],
    *,
    record_apps_entry: AppsEntryWriter | None = None,
) -> bool:
    """Run at each real start of an installed exe. Return True when it finished a staged update.

    A start of the staged version (after Restart to update) points the shortcuts at it, makes it current and clears
    pending.json. A start with no pending update points the shortcuts at the running version, when that version is not
    older than the current one. A start of any other version changes nothing. A shortcut that cannot be written keeps
    the pending update for the next start, and nothing is raised. Whenever the running version is current after the
    start, record_apps_entry points the Apps and Features entry at it, so an entry left on a pruned folder is repaired.
    """
    root = Path(install_root)
    exe = Path(running_exe)
    try:
        parse_version(running_version)
    except Refused:
        return False
    if not _is_version_exe(root, exe, running_version):
        return False
    pending = read_pending(root)
    current = read_current(root)
    if pending is not None and pending != running_version:
        return False  # another version is staged; the window offers the restart
    if pending is None and (current == running_version or (current is not None and is_newer(current, running_version))):
        if current == running_version:
            _record_apps_entry(record_apps_entry, exe_for(root, running_version), running_version)
        return False  # the shortcuts already follow this version, or they point at a newer one that must not be undone
    fallback = exe_for(root, current) if current is not None else exe
    try:
        _point_shortcuts(shortcut_writers, exe, fallback)
        write_current(root, running_version)
    except (_RepointFailed, OSError):
        return False
    _record_apps_entry(record_apps_entry, exe_for(root, running_version), running_version)
    if pending is None:
        return False
    clear_pending(root)
    return True


# --- the notify-only check ------------------------------------------------------------------------------

# check_for_update is the notify-only check. Its four results are below. Offline and Refused are the exception classes
# above, returned rather than raised, so a caller can match them by the same names.


@dataclass(frozen=True)
class UpdateNotice:
    """A newer release is published. Nothing has been downloaded."""

    version: str
    release_page_url: str


@dataclass(frozen=True)
class UpToDate:
    """The latest published release is not newer than the running app."""


UpdateCheck = UpdateNotice | UpToDate | Offline | Refused


def release_page_url_for(version: str) -> str:
    """The GitHub release page for a version. It is built only from a version that parse_version accepts."""
    parse_version(version)
    return f"https://github.com/{REPO}/releases/tag/{DESKTOP_TAG_PREFIX}{version}"


def _newest_desktop_version(transport: Transport) -> str | None:
    """Return the version of the newest published desktop release, or None when there is none. The assets are never read.

    The tag must be DESKTOP_TAG_PREFIX followed by a plain dotted version, so the release page built from it is the real one.
    """
    found = _newest_desktop_release(transport, forbidden_is_offline=True)
    return None if found is None else found[0]


def check_for_update(current_version: str, transport: Transport) -> UpdateCheck:
    """Say whether GitHub publishes a release newer than current_version. Downloads nothing and installs nothing.

    The one request is RELEASES_LIST_URL, the newest page of releases as JSON, with no file and at most MAX_SMALL_BYTES.
    A redirect is followed only to an allowed host. The result is one of:

    - UpdateNotice(version, release_page_url) when the newest desktop release is newer than current_version,
    - UpToDate() when it is not, or when no desktop release has been published,
    - Offline(reason) when GitHub cannot be reached, is failing for now, or is rate-limiting (403 or 429),
    - Refused(reason) when current_version or the reply is not acceptable.

    Each failure is returned, not raised. Nothing on this path reaches check_and_stage, install_update, or any other
    code that downloads or extracts a zip.
    """
    try:
        parse_version(current_version)
        version = _newest_desktop_version(transport)
    except Offline as exc:
        return Offline(exc.reason)
    except Refused as exc:
        return Refused(exc.reason)
    if version is None or not is_newer(version, current_version):
        return UpToDate()
    return UpdateNotice(version=version, release_page_url=release_page_url_for(version))


# --- the real transport ---------------------------------------------------------------------------------


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Return no follow-up request, so urllib raises HTTPError for a 3xx reply and nothing is followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _lower_headers(headers: Any) -> dict[str, str]:
    return {str(name).lower(): str(value) for name, value in (headers or {}).items()}


def _copy_capped(source: BinaryIO, sink: BinaryIO, max_bytes: int) -> None:
    """Copy source to sink, and raise TooLarge before writing any byte past max_bytes."""
    total = 0
    while True:
        chunk = source.read(_CHUNK)
        if not chunk:
            return
        total += len(chunk)
        if total > max_bytes:
            raise TooLarge("The download is over the size limit.")
        sink.write(chunk)


def urllib_transport(url: str, max_bytes: int, dest: Path | None) -> Reply:
    """The real transport: urllib, with redirects off and a size cap. A redirect or error status is returned, not raised.

    A 2xx body goes to dest when it is given, otherwise into Reply.body. A failed or oversized download removes dest.
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/octet-stream"}
    )
    opener = urllib.request.build_opener(_NoRedirects())
    try:
        response = opener.open(request, timeout=TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        exc.close()
        return Reply(exc.code, _lower_headers(exc.headers), b"")
    with response:
        status = response.status
        headers = _lower_headers(response.headers)
        if not 200 <= status < 300:
            return Reply(status, headers, b"")
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            raise TooLarge("The download is over the size limit.")
        if dest is None:
            buffer = io.BytesIO()
            _copy_capped(response, buffer, max_bytes)
            return Reply(status, headers, buffer.getvalue())
        try:
            with dest.open("wb") as sink:
                _copy_capped(response, sink, max_bytes)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        return Reply(status, headers, b"")
