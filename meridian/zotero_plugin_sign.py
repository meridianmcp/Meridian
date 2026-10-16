"""Mozilla unlisted signing for the Meridian Zotero bridge plugin.

Firefox and Zotero load an add-on that Mozilla has signed. This module signs the bridge once per plugin
version, outside the install path:

* ``stage_for_signing`` copies the files ``build_xpi`` packs into a scratch folder, with the same excludes
  and LF line endings, and adds ``browser_specific_settings.gecko.id`` to the STAGED manifest only. The
  shipped ``extensions/zotero-bridge/manifest.json`` is never changed.
* ``sign_plugin`` reads the bundle once and captures its version and source fingerprint. It writes a record
  with status ``submitted`` for that version into ``<state_dir>/signed.json`` BEFORE it runs
  ``web-ext sign --channel unlisted`` (pinned to ``WEB_EXT_PACKAGE``). The API key and secret go into the
  environment of that one call. They are never on the command line, never written to a file, and never
  returned. Afterwards it recomputes the fingerprint: a source that changed during the wait stores nothing.
  A zero exit with exactly one zip .xpi that has a signature under META-INF/ is stored as
  ``<state_dir>/signed/<addon-id>-<version>.xpi``, and that version's record becomes ``signed``.
* One submission per version, ever. Any record for a version, in any status and for any source, refuses a
  new submission of that version. A stored signed file with no record for its version refuses the same way. A
  failed, timed-out or source-changed run keeps its ``submitted`` record, because Mozilla may already hold the
  version. Bump the version in ``manifest.json`` to sign new source.
* ``stored_signed_artifact`` says whether the stored file is signed, missing, tampered, used up (submitted
  but not signed), or unreadable. The install path uses it, and it never calls ``sign_plugin``.
* A record file that is unreadable or has the wrong shape is refused. It is never treated as empty.
* ``sign_plugin(fetch=True)`` (CLI ``sign-plugin --fetch``) reads the file Mozilla has already approved and signed for
  this version, through ``zotero_amo``. It checks that file against this source, and stores it as ``signed`` with a
  record that says it was fetched. A fetch never writes a ``submitted`` record and never uploads anything.

Signing is the CLI step ``python -m meridian zotero sign-plugin``. It never closes Zotero and it never runs
during an install. The state folder is per user, so a build signed from a source checkout is also the one
the packaged tray app installs. The staged copy and the artifacts live in a folder under the system
temporary folder. npm and npx write their own files to the npm cache.

The only forced kill in this path is the one that ends the signing child's own process tree when web-ext
does not finish in time. It never touches Zotero or any other process.

UNVERIFIED on a real machine: that Mozilla's unlisted signing accepts this bridge, and that Zotero loads the
signed .xpi from the profile's extensions folder. Nothing here has been run against Mozilla. The META-INF
check looks at the file's shape only. It does not verify Mozilla's signature.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from meridian import zotero_amo as amo
from meridian import zotero_plugin_install as plugin

PLUGIN_ADDON_ID = "meridian-zotero@usemeridian.us"
# web-ext 8.10.0: `sign --channel unlisted` with the API key and secret read from WEB_EXT_API_KEY and
# WEB_EXT_API_SECRET (its sign command uses yargs' WEB_EXT env prefix). Its --timeout is in milliseconds.
WEB_EXT_PACKAGE = "web-ext@8.10.0"
API_KEY_ENV = amo.API_KEY_ENV
API_SECRET_ENV = amo.API_SECRET_ENV
DEFAULT_TIMEOUT_S = 3600.0
MAX_TIMEOUT_MINUTES = 1440.0
SIGNED_DIRNAME = "signed"
RECORD_FILENAME = "signed.json"
# Record statuses. A record with no status field is an older signed record, so it reads as signed.
RECORD_SUBMITTED = "submitted"
RECORD_SIGNED = "signed"
# Stored-artifact statuses (StoredSigned.status).
SIGNED_STATUS = "signed"
MISSING_STATUS = "missing"
TAMPERED_STATUS = "tampered"
USED_STATUS = "used"
UNREADABLE_STATUS = "unreadable"
_WALL_CLOCK_GRACE_S = 60.0
_KILL_WAIT_S = 10.0
_TASKKILL_TIMEOUT_S = 30.0
# web-ext reports its own validation or approval timeout with this text. Used only to classify the exit,
# never echoed.
_TIMEOUT_MARKER = b"timeout exceeded"
_VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
_SIGNATURE_ENTRY = re.compile(r"META-INF/[^/]+\.(rsa|sf)", re.IGNORECASE)

NO_CREDENTIALS_MESSAGE = (
    f"Mozilla signing needs two environment variables: {API_KEY_ENV} (the API key, the JWT issuer) and "
    f"{API_SECRET_ENV} (the API secret, the JWT secret). Create the key pair on the API Keys page of the "
    "addons.mozilla.org developer hub, set both as user environment variables, then run this command again. "
    "Nothing was signed or uploaded."
)
RANGE_MESSAGE = (
    "--timeout-minutes must be a number of minutes greater than 0 and at most 1440. Nothing was signed or uploaded."
)
TIMEOUT_MESSAGE = (
    "Mozilla did not return a signed file in time. Check this version's page in the developer hub. "
    "Mozilla may still have received this version, so bump the version in manifest.json to sign a new build. "
    "Nothing was stored."
)
SOURCE_CHANGED_MESSAGE = (
    "The plugin source changed while Mozilla was signing it, so nothing was stored. Mozilla may have received "
    "this version, so bump the version in manifest.json to sign a new build."
)
REUSED_VERSION_MESSAGE = (
    "This version has already been submitted to Mozilla. Bump the version in manifest.json to sign a new build."
)
# The same sentence the install shows for a version that is used up, so both paths give one instruction.
RECORD_UNREADABLE_MESSAGE = plugin.RECORD_UNREADABLE_MESSAGE
UNSIGNED_ZIP_MESSAGE = (
    "Mozilla signing finished, but the file it returned has no signature in META-INF/ (.rsa or .sf). "
    "Nothing was stored. Mozilla may have received this version, so bump the version in manifest.json to sign a "
    "new build."
)
_FAILED_TEMPLATE = (
    "Mozilla signing did not complete (web-ext exit code: {code}). Nothing was stored. Mozilla may have received "
    "this version, so bump the version in manifest.json to sign a new build."
)
_NOT_ONE_XPI_MESSAGE = (
    "Mozilla signing finished, but it did not produce exactly one signed .xpi. Nothing was stored. Mozilla may have "
    "received this version, so bump the version in manifest.json to sign a new build."
)
_STAGING_FAILED_MESSAGE = "The plugin bundle could not be prepared for signing. Nothing was signed or uploaded."
_BUNDLE_UNREADABLE_MESSAGE = "The plugin bundle's manifest could not be read. Nothing was signed or uploaded."
_STORE_FAILED_MESSAGE = (
    "The signed file could not be stored, so nothing was recorded. Mozilla may have signed this version, so bump "
    "the version in manifest.json to sign a new build."
)
_SCRATCH_FAILED_MESSAGE = "The temporary folder for signing could not be created. Nothing was signed or uploaded."
_SUBMIT_RECORD_FAILED_MESSAGE = "The signing record could not be written, so nothing was submitted to Mozilla."
_NPX_MISSING_MESSAGE = (
    "npx was not found on PATH, so the signing tool cannot run. Install Node.js, which includes npx, then try again. "
    "Nothing was signed or uploaded."
)
_UNSAFE_PATH_MESSAGE = (
    "The temporary folder path contains characters that the signing tool cannot take safely. Set TEMP to a folder "
    "path without those characters, then try again."
)
_NPX_UNSAFE_PATH_MESSAGE = (
    "The npx program path contains characters the signing tool cannot take safely. Move Node.js to a folder path "
    "without those characters, then try again."
)
# The fetch path: the approved file Mozilla already holds. Its record says it was fetched, not signed here.
FETCHED_ORIGIN = "fetched"
FETCH_NOT_APPROVED_MESSAGE = (
    "Mozilla has not approved and signed this version of the Zotero bridge plugin yet. Nothing was fetched or stored. "
    "Try again later, after Mozilla approves this version."
)
FETCH_MISMATCH_MESSAGE = (
    "The approved version does not match this source, so nothing was stored. If the source changed after Mozilla "
    "approved this version, bump the version in manifest.json to sign a new build."
)
FETCH_UNSIGNED_MESSAGE = (
    "The file Mozilla returned has no signature in META-INF/ (.rsa or .sf), so nothing was stored."
)
FETCH_NOT_ZIP_MESSAGE = "The file Mozilla returned is not a zip archive, so nothing was stored."
FETCH_SOURCE_CHANGED_MESSAGE = (
    "The plugin source changed while the approved file was being fetched, so nothing was stored. Run the command again."
)
FETCH_STORE_FAILED_MESSAGE = "The fetched file could not be stored, so nothing was recorded. Nothing was uploaded."
FETCH_FAILED_MESSAGE = "Mozilla's approved file could not be fetched. Nothing was stored."
# Characters that cmd.exe reads specially. The web-ext .cmd shim runs through it, so none may reach the argv.
_UNSAFE_PATH_CHARS = frozenset('&^%!"<>|')


@dataclass(frozen=True)
class SignRunResult:
    """What the runner reports: web-ext's exit code (None when it did not run to an exit), and whether it timed out."""

    returncode: "int | None"
    timed_out: bool = False


@dataclass(frozen=True)
class StoredSigned:
    """What is stored for one plugin version.

    * ``signed``: a signed record for this source matches the file's sha256.
    * ``missing``: no record and no stored file exist for this version, so it can be signed.
    * ``used``: a record exists for this version, but no signed file matches this source, or a stored file exists
      with no record for it. Mozilla may hold the version, so it is never signed again.
    * ``tampered``: a signed record for this source matches, but its file is changed or cannot be read.
    * ``unreadable``: the record file cannot be read or has the wrong shape.

    ``path`` and ``sha256`` are set only when the status is ``signed``. ``sha256`` is the recorded hash, which the
    copy into a profile checks the bytes against.
    """

    status: str
    path: "Path | None" = None
    sha256: "str | None" = None

    @property
    def signed(self) -> bool:
        return self.status == SIGNED_STATUS

    @property
    def tampered(self) -> bool:
        return self.status == TAMPERED_STATUS


# ---------------------------------------------------------------------------
# The bundle: its version, its fingerprint, and the staged copy that web-ext signs
# ---------------------------------------------------------------------------


def _packed_snapshot(source_dir: "Path | str") -> list[tuple[str, bytes]]:
    """Every runtime file's packed bytes, each read once. The fingerprint and the staged copy both come from this."""
    return [(name, plugin._packed_bytes(path)) for name, path in plugin._runtime_files(Path(source_dir))]


def _manifest_bytes(snapshot: "list[tuple[str, bytes]]") -> bytes:
    for name, data in snapshot:
        if name == "manifest.json":
            return data
    raise ValueError("the bundle has no manifest.json")


def _version_from_manifest(data: bytes) -> str:
    manifest = json.loads(data.decode("utf-8"))
    version = manifest.get("version") if isinstance(manifest, dict) else None
    if not isinstance(version, str) or not _VERSION_PATTERN.fullmatch(version):
        raise ValueError("the bundle manifest has no usable version")
    return version


def _fingerprint_of(snapshot: "list[tuple[str, bytes]]") -> str:
    digest = hashlib.sha256()
    for name, data in snapshot:
        packed = hashlib.sha256(data).hexdigest()
        digest.update(f"{name}\t{packed}\n".encode("utf-8"))
    return digest.hexdigest()


def bundle_version(source_dir: "Path | str") -> str:
    """The bundle manifest's version. Raises ValueError when it is missing or unsafe to use in a file name."""
    return _version_from_manifest((Path(source_dir) / "manifest.json").read_bytes())


def source_fingerprint(source_dir: "Path | str") -> str:
    """A sha256 over the packed runtime files (name and content, LF-normalised), so the same source always matches.

    It is recorded at signing time. An install compares it with the current source, so an edit that does not bump
    the version is never installed under a signature for the old code.
    """
    return _fingerprint_of(_packed_snapshot(source_dir))


def _staged_manifest(data: bytes) -> bytes:
    """The manifest for signing: the shipped one plus browser_specific_settings.gecko.id. Refuses an id mismatch."""
    manifest = json.loads(data.decode("utf-8"))
    applications = manifest.get("applications") if isinstance(manifest, dict) else None
    zotero = applications.get("zotero") if isinstance(applications, dict) else None
    if not isinstance(zotero, dict) or zotero.get("id") != PLUGIN_ADDON_ID:
        raise ValueError("the bundle manifest's applications.zotero.id is not the plugin's add-on id")
    settings = manifest.get("browser_specific_settings")
    settings = dict(settings) if isinstance(settings, dict) else {}
    gecko = settings.get("gecko")
    gecko = gecko if isinstance(gecko, dict) else {}
    # AMO requires a data-collection declaration on new add-ons. The plugin collects no data, so it declares none.
    settings["gecko"] = {
        **gecko,
        "id": PLUGIN_ADDON_ID,
        "data_collection_permissions": {"required": ["none"]},
    }
    manifest["browser_specific_settings"] = settings
    return (json.dumps(manifest, indent=2) + "\n").encode("utf-8")


def _stage_snapshot(snapshot: "list[tuple[str, bytes]]", dest_dir: "Path | str") -> Path:
    """Write a snapshot's files into ``dest_dir``, adding the gecko id to the staged manifest only."""
    dest = Path(dest_dir)
    for name, data in snapshot:
        if name == "manifest.json":
            data = _staged_manifest(data)
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return dest


def stage_for_signing(bundle_dir: "Path | str", dest_dir: "Path | str") -> Path:
    """Copy the files ``build_xpi`` packs into ``dest_dir``, adding the gecko id to the staged manifest only."""
    return _stage_snapshot(_packed_snapshot(bundle_dir), dest_dir)


def parse_timeout_minutes(raw: object) -> float:
    """The --timeout-minutes value as minutes: a finite number greater than 0 and at most 1440.

    Anything else raises ValueError with the one user-facing message, so the CLI can print it with no traceback.
    """
    try:
        minutes = float(raw.strip() if isinstance(raw, str) else raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(RANGE_MESSAGE) from None
    if not math.isfinite(minutes) or not 0 < minutes <= MAX_TIMEOUT_MINUTES:
        raise ValueError(RANGE_MESSAGE)
    return minutes


def _valid_timeout_seconds(timeout_s: object) -> bool:
    try:
        seconds = float(timeout_s)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return math.isfinite(seconds) and 0 < seconds <= MAX_TIMEOUT_MINUTES * 60


def sign_argv(
    staged_dir: "Path | str", artifacts_dir: "Path | str", timeout_s: float, npx: str = "npx",
) -> list[str]:
    """The web-ext argv. It carries no credential: those are passed in the call's environment.

    ``npx`` is the program that starts the run: ``sign_plugin`` passes the npx path it found and checked.
    ``--no-config-discovery`` stops a config file in the home folder from changing the target or the options.
    """
    timeout_ms = max(1, int(round(float(timeout_s) * 1000)))
    return [
        npx, "--yes", WEB_EXT_PACKAGE, "sign",
        "--source-dir", str(staged_dir),
        "--artifacts-dir", str(artifacts_dir),
        "--channel", "unlisted",
        "--timeout", str(timeout_ms),
        "--no-config-discovery",
    ]


def _unsafe_for_argv(path: "Path | str") -> bool:
    """True when a path holds a character cmd.exe reads specially (see ``_UNSAFE_PATH_CHARS``). Never raises."""
    return any(char in _UNSAFE_PATH_CHARS for char in str(path))


def _npx_folders() -> list[str]:
    """The PATH folders that may hold npx: absolute entries only, never an empty entry, never the current folder.

    A relative entry, an empty one, or one that resolves to the current folder is skipped, so npx is never taken from
    the folder the command runs in. Never raises.
    """
    try:
        here: "str | None" = os.path.normcase(os.path.realpath(os.getcwd()))
    except (OSError, ValueError):
        here = None
    folders: list[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        try:
            resolved = os.path.normcase(os.path.realpath(entry))
        except (OSError, ValueError):
            continue
        if resolved != here:
            folders.append(entry)
    return folders


def _find_npx() -> "str | None":
    """The npx program in the first PATH folder that holds one, or None. Never raises.

    Each folder is checked as ``<folder>/npx``, a name with a folder part. ``shutil.which("npx", path=...)`` is
    not used: on Windows it searches the current folder first, whatever ``path`` says, unless the
    NoDefaultCurrentDirectoryInExePath variable is set.
    """
    for folder in _npx_folders():
        try:
            found = shutil.which(os.path.join(folder, "npx"))
        except (OSError, ValueError, TypeError):
            continue
        if found:
            return found
    return None


# ---------------------------------------------------------------------------
# The stored signed artifact and its record
# ---------------------------------------------------------------------------


def _signed_path(state_dir: "Path | str", version: str) -> Path:
    return Path(state_dir) / SIGNED_DIRNAME / f"{PLUGIN_ADDON_ID}-{version}.xpi"


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _timestamp(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_for(record: "dict[str, Any]", version: str) -> bool:
    return record.get("addon_id") == PLUGIN_ADDON_ID and record.get("version") == version


def _record_status(record: "dict[str, Any]") -> str:
    return str(record.get("status", RECORD_SIGNED))


def _valid_record(record: Any) -> bool:
    """True when one record has a shape this module writes. Never raises."""
    if not isinstance(record, dict):
        return False
    if not isinstance(record.get("addon_id"), str) or not isinstance(record.get("version"), str):
        return False
    if not isinstance(record.get("source_sha256"), str):
        return False
    status = record.get("status", RECORD_SIGNED)
    if status == RECORD_SUBMITTED:
        return isinstance(record.get("submitted_at"), str)
    if status == RECORD_SIGNED:
        return isinstance(record.get("sha256"), str) and isinstance(record.get("signed_at"), str)
    return False


def _load_records(state_dir: "Path | str") -> "list[dict[str, Any]] | None":
    """The records in ``signed.json``: ``[]`` when there is no file, or None when it is unreadable or wrong-shaped.

    None is never treated as empty: every caller refuses on it. Never raises.
    """
    try:
        text = (Path(state_dir) / RECORD_FILENAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return None
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        return None
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list) or not all(_valid_record(record) for record in records):
        return None
    return records


def stored_signed_artifact(
    state_dir: "Path | str", version: str, *, source_sha256: "str | None" = None,
) -> StoredSigned:
    """What is stored for ``version``, and for ``source_sha256`` when given. Never raises.

    The statuses are described on :class:`StoredSigned`. A version that has any record, or a stored signed file, is
    never ``missing``, so a version Mozilla may already hold can never be treated as a fresh one. A stored file
    whose record was deleted is therefore ``used``: deleting ``signed.json`` alone does not make a version new.
    """
    try:
        if not isinstance(version, str) or not _VERSION_PATTERN.fullmatch(version):
            return StoredSigned(status=MISSING_STATUS)
        records = _load_records(state_dir)
        if records is None:
            return StoredSigned(status=UNREADABLE_STATUS)
        target = _signed_path(state_dir, version)
        mine = [record for record in records if _is_for(record, version)]
        if not mine:
            return StoredSigned(status=USED_STATUS if target.exists() else MISSING_STATUS)
        signed = [
            record for record in mine
            if _record_status(record) == RECORD_SIGNED
            and (source_sha256 is None or record.get("source_sha256") == source_sha256)
        ]
        if not signed or not target.is_file():
            return StoredSigned(status=USED_STATUS)
        recorded = signed[-1].get("sha256")
        try:
            actual = _sha256_of(target)
        except OSError:
            return StoredSigned(status=TAMPERED_STATUS)
        if not isinstance(recorded, str) or actual != recorded:
            return StoredSigned(status=TAMPERED_STATUS)
        return StoredSigned(status=SIGNED_STATUS, path=target, sha256=recorded)
    except (OSError, ValueError, TypeError):
        return StoredSigned(status=UNREADABLE_STATUS)


def signed_artifact_for_version(
    state_dir: "Path | str", version: str, *, source_sha256: "str | None" = None,
) -> "Path | None":
    """The stored signed .xpi for ``version`` when it is signed, else None. Never raises."""
    stored = stored_signed_artifact(state_dir, version, source_sha256=source_sha256)
    return stored.path if stored.signed else None


def _replace_record(state: Path, version: str, entry: "dict[str, Any]") -> None:
    """Write ``entry`` in place of any record for ``version``.

    Raises ValueError rather than overwrite a record file it cannot read.
    """
    records = _load_records(state)
    if records is None:
        raise ValueError(RECORD_UNREADABLE_MESSAGE)
    kept = [record for record in records if not _is_for(record, version)]
    kept.append(entry)
    text = json.dumps({"records": kept}, indent=2, sort_keys=True) + "\n"
    plugin._atomic_write_bytes(state / RECORD_FILENAME, text.encode("utf-8"))


def record_submission(
    state_dir: "Path | str", version: str, source_sha256: str, *, clock: "Callable[[], float] | None" = None,
) -> None:
    """Write the ``submitted`` record for ``version``.

    ``sign_plugin`` calls it before web-ext runs, so every run leaves one.
    """
    now = (clock if clock is not None else time.time)()
    _replace_record(Path(state_dir), version, {
        "addon_id": PLUGIN_ADDON_ID,
        "version": version,
        "status": RECORD_SUBMITTED,
        "source_sha256": source_sha256,
        "submitted_at": _timestamp(now),
    })


def record_signed_artifact(
    state_dir: "Path | str", signed_file: "Path | str", *, source_dir: "Path | str | None" = None,
    version: "str | None" = None, source_sha256: "str | None" = None,
    clock: "Callable[[], float] | None" = None,
) -> Path:
    """Store ``signed_file`` for ``version``, and mark that version ``signed``. Returns the stored path.

    ``version`` and ``source_sha256`` are the values captured before signing. When they are not given, they are
    read from ``source_dir``. The record file is checked first, then the signed file is written, and then the
    record replaces the version's ``submitted`` record. So a record never points at a file that was not written,
    and an unreadable record file is never overwritten. The key and the secret are never stored.
    """
    if version is None:
        version = bundle_version(source_dir)
    if source_sha256 is None:
        source_sha256 = source_fingerprint(source_dir)
    payload = Path(signed_file).read_bytes()
    return record_signed_bytes(state_dir, payload, version=version, source_sha256=source_sha256, clock=clock)


def record_signed_bytes(
    state_dir: "Path | str", payload: bytes, *, version: str, source_sha256: str,
    origin: "str | None" = None, clock: "Callable[[], float] | None" = None,
) -> Path:
    """Store ``payload`` as the signed file for ``version``, and mark that version ``signed``. Returns the stored path.

    ``origin`` says how the file was obtained: ``fetched`` for a file fetched from Mozilla, and left out for a signing
    run. The record file is checked first, then the file is written, and then the record replaces the version's record.
    So a record never points at a file that was not written, and an unreadable record file is never overwritten.
    """
    state = Path(state_dir)
    if _load_records(state) is None:
        raise ValueError(RECORD_UNREADABLE_MESSAGE)
    target = _signed_path(state, version)
    plugin._atomic_write_bytes(target, payload)
    now = (clock if clock is not None else time.time)()
    entry: dict[str, Any] = {
        "addon_id": PLUGIN_ADDON_ID,
        "version": version,
        "status": RECORD_SIGNED,
        "signed_at": _timestamp(now),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "source_sha256": source_sha256,
    }
    if origin is not None:
        entry["origin"] = origin
    _replace_record(state, version, entry)
    return target


def _has_signature_entries(path: Path) -> bool:
    """True when the zip has a META-INF/ folder with a signature file (.rsa or .sf) directly inside it. Never raises.

    This checks the file's shape only. It does not verify Mozilla's signature.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except (OSError, ValueError, zipfile.BadZipFile):
        return False
    has_folder = any(name.startswith("META-INF/") for name in names)
    has_signature = any(_SIGNATURE_ENTRY.fullmatch(name) for name in names)
    return has_folder and has_signature


# ---------------------------------------------------------------------------
# Running web-ext
# ---------------------------------------------------------------------------


def _credentials(env: "Mapping[str, str] | None") -> "tuple[str, str] | None":
    """The API key and secret, stripped, or None when either is missing or empty (the rule lives in ``zotero_amo``)."""
    return amo.read_credentials(env)


def _child_env(env: "Mapping[str, str] | None", key: str, secret: str) -> dict[str, str]:
    """The environment of the signing call. Other WEB_EXT_* settings are dropped, so none can redirect the credentials."""
    source = os.environ if env is None else env
    child = {str(name): str(value) for name, value in source.items() if not str(name).upper().startswith("WEB_EXT_")}
    child[API_KEY_ENV] = key
    child[API_SECRET_ENV] = secret
    return child


def _kill_signing_tree(process: Any) -> None:
    """Force-stop the signing child and the processes it started. Never another process, never Zotero. Never raises.

    On Windows this is ``taskkill /PID <child> /T /F``. Elsewhere the child runs in its own session, and that
    session's process group is killed. This is the only forced kill in the signing path.
    """
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=_TASKKILL_TIMEOUT_S,
                check=False,
                creationflags=plugin._CREATE_NO_WINDOW,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001 -- a failed kill still ends as a timeout
        pass
    try:
        process.wait(timeout=_KILL_WAIT_S)
    except Exception:  # noqa: BLE001 -- the outcome is already a timeout
        pass


def _run_web_ext(argv: list[str], env: Mapping[str, str], *, timeout_s: float, cwd: "Path | str") -> SignRunResult:
    """Run the sign argv, whose first item is the npx path ``sign_plugin`` checked, with its output in two files and
    never in pipes. Never raises.

    Pipes are avoided on purpose: a grandchild that inherits a pipe keeps it open, and that blocks the read. The
    wall clock is ``timeout_s`` plus a grace period, so web-ext's own timeout normally fires first. When the wait runs
    out, the child's process tree is killed (see :func:`_kill_signing_tree`). The same kill runs when the wait is
    interrupted by anything, KeyboardInterrupt included, and then the exception is raised again. If the program
    cannot start, the result has no exit code.
    """
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = plugin._CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        with tempfile.TemporaryDirectory(prefix="meridian-web-ext-", ignore_cleanup_errors=True) as logs:
            stdout_path = Path(logs) / "stdout.txt"
            stderr_path = Path(logs) / "stderr.txt"
            with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
                try:
                    process = subprocess.Popen(
                        list(argv),
                        env=dict(env),
                        cwd=str(cwd),
                        stdin=subprocess.DEVNULL,
                        stdout=out,
                        stderr=err,
                        **kwargs,
                    )
                except Exception:  # noqa: BLE001 -- a start that raises is a failed run; its detail is never kept
                    return SignRunResult(returncode=None)
                try:
                    returncode = process.wait(timeout=float(timeout_s) + _WALL_CLOCK_GRACE_S)
                except subprocess.TimeoutExpired:
                    _kill_signing_tree(process)
                    return SignRunResult(returncode=None, timed_out=True)
                except BaseException:  # noqa: BLE001 -- KeyboardInterrupt too: the signing child must not outlive us
                    _kill_signing_tree(process)
                    raise
            output = stdout_path.read_bytes() + stderr_path.read_bytes()
    except OSError:
        return SignRunResult(returncode=None)
    timed_out = returncode != 0 and _TIMEOUT_MARKER in output.lower()
    return SignRunResult(returncode=returncode, timed_out=timed_out)


def _outcome(status: str, *, version: "str | None" = None, path: "Path | None" = None, message: "str | None" = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": status, "version": version, "path": None if path is None else str(path)}
    if message:
        payload["message"] = message
    return payload


def _source_changed(source: Path, fingerprint: str) -> bool:
    """True when the source is no longer the one captured before signing. An unreadable source counts as changed."""
    try:
        return _fingerprint_of(_packed_snapshot(source)) != fingerprint
    except (OSError, ValueError, TypeError):
        return True


def sign_plugin(
    *,
    env: "Mapping[str, str] | None" = None,
    runner: "Callable[..., SignRunResult] | None" = None,
    state_dir: "Path | str",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    clock: "Callable[[], float] | None" = None,
    source_dir: "Path | str | None" = None,
    fetch: bool = False,
    transport: "Callable[[str, Mapping[str, str]], tuple[int, bytes]] | None" = None,
) -> dict[str, Any]:
    """Sign the bundle's current version and store the result. Never raises.

    Returns ``status`` as one of ``already_signed`` (a matching signed file is stored, so no runner call),
    ``refused_version_reused`` (this version was already submitted to Mozilla, for any source, so no runner call),
    ``refused_unreadable_record`` (the record file cannot be read or has the wrong shape, so no runner call),
    ``no_credentials`` (either variable is missing or empty, so no runner call), ``signed``, ``timeout`` or
    ``failed``. Before web-ext runs, the version's ``submitted`` record is written, and it stays whatever the
    outcome. Only a verified signed file replaces it with a ``signed`` record. ``runner`` is called as
    ``runner(argv, env, timeout_s=..., cwd=...)`` and defaults to running ``npx``.

    With ``fetch=True`` nothing is submitted and nothing is uploaded. The approved file for this version is read through
    ``transport`` (``zotero_amo``'s default when None), checked against this source, and returned as ``fetched``, or
    ``not_approved_yet`` when Mozilla has not approved and signed it. ``runner`` is not used.
    """
    state = Path(state_dir)
    if not _valid_timeout_seconds(timeout_s):
        return _outcome("failed", message=RANGE_MESSAGE)
    source = plugin._resolve_source(source_dir)
    try:
        snapshot = _packed_snapshot(source)
        version = _version_from_manifest(_manifest_bytes(snapshot))
        fingerprint = _fingerprint_of(snapshot)
    except (OSError, ValueError, TypeError, RecursionError):
        return _outcome("failed", message=_BUNDLE_UNREADABLE_MESSAGE)
    stored = stored_signed_artifact(state, version, source_sha256=fingerprint)
    if stored.status == UNREADABLE_STATUS:
        return _outcome("refused_unreadable_record", version=version, message=RECORD_UNREADABLE_MESSAGE)
    if stored.signed:
        return _outcome("already_signed", version=version, path=stored.path)
    if fetch:
        return _fetch_and_store(
            state, source, snapshot, version, fingerprint, env=env, transport=transport, clock=clock,
        )
    if stored.status != MISSING_STATUS:
        return _outcome("refused_version_reused", version=version, message=REUSED_VERSION_MESSAGE)
    creds = _credentials(env)
    if creds is None:
        return _outcome("no_credentials", version=version, message=NO_CREDENTIALS_MESSAGE)
    run = runner if runner is not None else _run_web_ext
    try:
        scratch_root = tempfile.TemporaryDirectory(prefix="meridian-zotero-sign-", ignore_cleanup_errors=True)
    except OSError:
        return _outcome("failed", version=version, message=_SCRATCH_FAILED_MESSAGE)
    with scratch_root as scratch:
        scratch_dir = Path(scratch)
        artifacts = scratch_dir / "artifacts"
        staged_dir = scratch_dir / "staged"
        if any(_unsafe_for_argv(path) for path in (scratch_dir, staged_dir, artifacts)):
            return _outcome("failed", version=version, message=_UNSAFE_PATH_MESSAGE)
        try:
            staged = _stage_snapshot(snapshot, staged_dir)
            artifacts.mkdir()
        except (OSError, ValueError, TypeError, OverflowError, RecursionError):
            return _outcome("failed", version=version, message=_STAGING_FAILED_MESSAGE)
        # Both checks run before the submission record, so a run that cannot start never uses up the version.
        npx = _find_npx()
        if npx is None:
            return _outcome("failed", version=version, message=_NPX_MISSING_MESSAGE)
        if _unsafe_for_argv(npx):
            return _outcome("failed", version=version, message=_NPX_UNSAFE_PATH_MESSAGE)
        try:
            argv = sign_argv(staged, artifacts, timeout_s, npx=npx)
        except (OSError, ValueError, TypeError, OverflowError):
            return _outcome("failed", version=version, message=_STAGING_FAILED_MESSAGE)
        try:
            record_submission(state, version, fingerprint, clock=clock)
        except (OSError, ValueError, TypeError):
            return _outcome("failed", version=version, message=_SUBMIT_RECORD_FAILED_MESSAGE)
        try:
            result = run(argv, _child_env(env, *creds), timeout_s=timeout_s, cwd=scratch_dir)
        except Exception:  # noqa: BLE001 -- a runner that raises is a failed signing, never a crash
            return _outcome("failed", version=version, message=_FAILED_TEMPLATE.format(code="none"))
        if _source_changed(source, fingerprint):
            return _outcome("failed", version=version, message=SOURCE_CHANGED_MESSAGE)
        if result.timed_out:
            return _outcome("timeout", version=version, message=TIMEOUT_MESSAGE)
        if result.returncode != 0:
            code = "none" if result.returncode is None else result.returncode
            return _outcome("failed", version=version, message=_FAILED_TEMPLATE.format(code=code))
        produced = sorted(artifacts.glob("*.xpi"))
        if len(produced) != 1 or not zipfile.is_zipfile(produced[0]):
            return _outcome("failed", version=version, message=_NOT_ONE_XPI_MESSAGE)
        if not _has_signature_entries(produced[0]):
            return _outcome("failed", version=version, message=UNSIGNED_ZIP_MESSAGE)
        try:
            stored_path = record_signed_artifact(
                state, produced[0], version=version, source_sha256=fingerprint, clock=clock,
            )
        except (OSError, ValueError, TypeError):
            return _outcome("failed", version=version, message=_STORE_FAILED_MESSAGE)
    return _outcome("signed", version=version, path=stored_path)


# ---------------------------------------------------------------------------
# Fetching the approved version: read-only, checked against this source, stored as signed, never a submission
# ---------------------------------------------------------------------------

_WITHOUT_SETTINGS = "browser_specific_settings"


def _without_settings(manifest: "dict[str, Any]") -> "dict[str, Any]":
    return {key: value for key, value in manifest.items() if key != _WITHOUT_SETTINGS}


def _zotero_id(manifest: "dict[str, Any]") -> object:
    applications = manifest.get("applications")
    zotero = applications.get("zotero") if isinstance(applications, dict) else None
    return zotero.get("id") if isinstance(zotero, dict) else None


def verify_fetched_signed_file(
    data: bytes, *, expected_id: str, expected_version: str, current_source_packed: "Mapping[str, bytes]",
) -> "str | None":
    """Check a file that Mozilla returned against this source. None when it passes, else the refusal text. Never raises.

    The file must be a zip with a signature entry directly under META-INF/. That checks the file's shape only, not
    Mozilla's signature. Its manifest.json must name ``expected_id`` and ``expected_version``, and must equal the
    current manifest once browser_specific_settings is removed, because signing adds that key to the staged copy only.
    Its runtime files must be exactly the source's runtime files, each with the sha256 of the source's packed bytes.
    ``current_source_packed`` maps each runtime file's archive name to its packed bytes: the same snapshot that the
    fingerprint is taken from.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError, TypeError):
        return FETCH_NOT_ZIP_MESSAGE
    try:
        with archive:
            names = archive.namelist()
            if len(set(names)) != len(names):
                return FETCH_MISMATCH_MESSAGE
            if not any(_SIGNATURE_ENTRY.fullmatch(name) for name in names):
                return FETCH_UNSIGNED_MESSAGE
            expected = set(current_source_packed) - {"manifest.json"}
            runtime = {name for name in names if not name.startswith("META-INF/") and not name.endswith("/")}
            if "manifest.json" not in names or runtime - {"manifest.json"} != expected:
                return FETCH_MISMATCH_MESSAGE
            signed_manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            current_bytes = current_source_packed.get("manifest.json")
            if current_bytes is None or not isinstance(signed_manifest, dict):
                return FETCH_MISMATCH_MESSAGE
            current_manifest = json.loads(current_bytes.decode("utf-8"))
            if not isinstance(current_manifest, dict):
                return FETCH_MISMATCH_MESSAGE
            if signed_manifest.get("version") != expected_version or _zotero_id(signed_manifest) != expected_id:
                return FETCH_MISMATCH_MESSAGE
            if _without_settings(signed_manifest) != _without_settings(current_manifest):
                return FETCH_MISMATCH_MESSAGE
            for name in expected:
                if hashlib.sha256(archive.read(name)).digest() != hashlib.sha256(current_source_packed[name]).digest():
                    return FETCH_MISMATCH_MESSAGE
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError, KeyError, TypeError, RecursionError, RuntimeError, NotImplementedError):
        return FETCH_MISMATCH_MESSAGE
    return None


def _fetch_and_store(
    state: Path, source: Path, snapshot: "list[tuple[str, bytes]]", version: str, fingerprint: str, *,
    env: "Mapping[str, str] | None",
    transport: "Callable[[str, Mapping[str, str]], tuple[int, bytes]] | None",
    clock: "Callable[[], float] | None",
) -> dict[str, Any]:
    """Fetch Mozilla's approved file for ``version``, check it against ``snapshot``, and store it. Never raises.

    Nothing is submitted and nothing is uploaded. The file is read and checked, and only then stored as ``signed``,
    with a record that names this source and says the file was fetched.
    """
    try:
        fetched = amo.fetch_approved_version(env=env, transport=transport, addon_id=PLUGIN_ADDON_ID, version=version)
    except Exception:  # noqa: BLE001 -- a failed fetch is a failed result, never a crash
        return _outcome("failed", version=version, message=FETCH_FAILED_MESSAGE)
    if fetched.status == amo.NO_CREDENTIALS:
        return _outcome("no_credentials", version=version, message=NO_CREDENTIALS_MESSAGE)
    if fetched.status == amo.NOT_APPROVED:
        return _outcome("not_approved_yet", version=version, message=FETCH_NOT_APPROVED_MESSAGE)
    if fetched.status != amo.FETCHED or fetched.data is None:
        return _outcome("failed", version=version, message=fetched.message or FETCH_FAILED_MESSAGE)
    refusal = verify_fetched_signed_file(
        fetched.data, expected_id=PLUGIN_ADDON_ID, expected_version=version, current_source_packed=dict(snapshot),
    )
    if refusal is not None:
        return _outcome("failed", version=version, message=refusal)
    if _source_changed(source, fingerprint):
        return _outcome("failed", version=version, message=FETCH_SOURCE_CHANGED_MESSAGE)
    try:
        stored_path = record_signed_bytes(
            state, fetched.data, version=version, source_sha256=fingerprint, origin=FETCHED_ORIGIN, clock=clock,
        )
    except (OSError, ValueError, TypeError):
        return _outcome("failed", version=version, message=FETCH_STORE_FAILED_MESSAGE)
    return _outcome("fetched", version=version, path=stored_path)


# ---------------------------------------------------------------------------
# CLI: python -m meridian zotero sign-plugin [--timeout-minutes N] [--dry-run] [--fetch]
# ---------------------------------------------------------------------------


def _status_text(result: Mapping[str, Any]) -> str:
    status = result.get("status")
    version = result.get("version")
    if status == "signed":
        return (
            f"Signed the Zotero bridge plugin {version} and stored it at {result.get('path')}. Next, run: "
            "python -m meridian zotero install-plugin (it closes Zotero only for the install, and restarts "
            "it if it closed it)."
        )
    if status == "fetched":
        return (
            f"Fetched the approved Zotero bridge plugin {version} from Mozilla and stored it at {result.get('path')}. "
            "Nothing was uploaded. Next, run: python -m meridian zotero install-plugin (it closes Zotero only for the "
            "install, and restarts it if it closed it)."
        )
    if status == "already_signed":
        return f"The Zotero bridge plugin {version} is already signed at {result.get('path')}. Nothing was uploaded."
    return str(result.get("message") or "Signing did not complete. Nothing was stored.")


def _dry_run() -> int:
    print("Dry run: nothing will be signed or uploaded.")
    try:
        source = plugin._resolve_source(None)
        version = bundle_version(source)
        fingerprint = source_fingerprint(source)
    except (OSError, ValueError, TypeError, RecursionError):
        print("Plugin version: cannot be read from the bundle manifest.")
        return 0
    print(f"Plugin version: {version}")
    creds = _credentials(None)
    print(f"Mozilla credentials set: {'yes' if creds is not None else 'no'}")
    stored = stored_signed_artifact(plugin.signing_state_dir(), version, source_sha256=fingerprint)
    if stored.signed:
        stored_text = f"yes ({stored.path})"
    elif stored.tampered:
        stored_text = "no (the stored file failed its integrity check)"
    elif stored.status == USED_STATUS:
        stored_text = "no (this version was already submitted to Mozilla, so sign-plugin will refuse it)"
    elif stored.status == UNREADABLE_STATUS:
        stored_text = "no (the signing record is unreadable)"
    else:
        stored_text = "no"
    print(f"Signed artifact stored: {stored_text}")
    if creds is None:
        print(f"To sign, set {API_KEY_ENV} and {API_SECRET_ENV} as user environment variables. Values are never printed.")
    return 0


def cli_main(argv: "list[str] | None" = None) -> int:
    """``python -m meridian zotero sign-plugin``. Returns 0 when the plugin is signed, 1 otherwise."""
    parser = argparse.ArgumentParser(
        prog="python -m meridian zotero sign-plugin",
        description=(
            "Sign the Zotero bridge plugin with Mozilla as an unlisted add-on, once per plugin version. "
            f"Needs the {API_KEY_ENV} and {API_SECRET_ENV} environment variables. Does not close Zotero."
        ),
    )
    parser.add_argument(
        "--timeout-minutes", default=DEFAULT_TIMEOUT_S / 60,
        help=(
            "How long to wait for Mozilla, in minutes: more than 0 and at most "
            f"{MAX_TIMEOUT_MINUTES:g} (default: 60)."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the plan. Signs nothing and uploads nothing.")
    parser.add_argument(
        "--fetch", action="store_true",
        help=(
            "Fetch the file Mozilla has already approved and signed for this version, check it against this source, "
            "and store it. It downloads only and never uploads anything."
        ),
    )
    args = parser.parse_args(argv)
    try:
        minutes = parse_timeout_minutes(args.timeout_minutes)
    except ValueError as exc:
        print(str(exc))
        return 1
    if args.dry_run:
        return _dry_run()
    result = sign_plugin(state_dir=plugin.signing_state_dir(), timeout_s=minutes * 60, fetch=args.fetch)
    print(_status_text(result))
    return 0 if result["status"] in ("signed", "already_signed", "fetched") else 1
