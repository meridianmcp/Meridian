"""Mozilla unlisted signing for the Meridian Zotero bridge plugin.

Firefox and Zotero load an add-on that Mozilla has signed. This module signs the bridge once per plugin
version, outside the install path:

* ``stage_for_signing`` copies the files ``build_xpi`` packs into a scratch folder, with the same excludes
  and LF line endings, and adds ``browser_specific_settings.gecko.id`` to the STAGED manifest only. The
  shipped ``extensions/zotero-bridge/manifest.json`` is never changed.
* ``sign_plugin`` reads the bundle once and captures its version and source fingerprint BEFORE it runs
  ``web-ext sign --channel unlisted`` (pinned to ``WEB_EXT_PACKAGE``). The API key and secret go into the
  environment of that one call. They are never on the command line, never written to a file, and never
  returned. Afterwards it recomputes the fingerprint: a source that changed during the wait stores nothing.
  A zero exit with exactly one zip .xpi that has a signature under META-INF/ is stored as
  ``<state_dir>/signed/<addon-id>-<version>.xpi``, with the captured version and fingerprint recorded in
  ``<state_dir>/signed.json``.
* A version that already has a record for a different source is refused before any upload. Bump the
  version in ``manifest.json`` to sign new source.
* ``stored_signed_artifact`` says whether the stored file is signed, missing, or tampered. The install path
  uses it, and it never calls ``sign_plugin``.

Signing is the CLI step ``python -m meridian zotero sign-plugin``. It never closes Zotero and it never runs
during an install. The state folder is per user, so a build signed from a source checkout is also the one
the packaged tray app installs.

The only forced kill in this path is the one that ends the signing child's own process tree when web-ext
does not finish in time. It never touches Zotero or any other process.

UNVERIFIED on a real machine: that Mozilla's unlisted signing accepts this bridge, and that Zotero loads the
signed .xpi from the profile's extensions folder. Nothing here has been run against Mozilla. The META-INF
check looks at the file's shape only. It does not verify Mozilla's signature.
"""

from __future__ import annotations

import argparse
import hashlib
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

from meridian import zotero_plugin_install as plugin

PLUGIN_ADDON_ID = "meridian-zotero@usemeridian.us"
# web-ext 8.10.0: `sign --channel unlisted` with the API key and secret read from WEB_EXT_API_KEY and
# WEB_EXT_API_SECRET (its sign command uses yargs' WEB_EXT env prefix). Its --timeout is in milliseconds.
WEB_EXT_PACKAGE = "web-ext@8.10.0"
API_KEY_ENV = "WEB_EXT_API_KEY"
API_SECRET_ENV = "WEB_EXT_API_SECRET"
DEFAULT_TIMEOUT_S = 3600.0
MAX_TIMEOUT_MINUTES = 1440.0
SIGNED_DIRNAME = "signed"
RECORD_FILENAME = "signed.json"
SIGNED_STATUS = "signed"
MISSING_STATUS = "missing"
TAMPERED_STATUS = "tampered"
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
    "Nothing was stored."
)
SOURCE_CHANGED_MESSAGE = "The plugin source changed while Mozilla was signing it. Run sign-plugin again."
REUSED_VERSION_MESSAGE = (
    "This version was already submitted with different source. Bump the version in manifest.json, then sign again."
)
UNSIGNED_ZIP_MESSAGE = (
    "Mozilla signing finished, but the file it returned has no signature in META-INF/ (.rsa or .sf). "
    "Nothing was stored."
)
_FAILED_TEMPLATE = (
    "Mozilla signing did not complete (web-ext exit code: {code}). Nothing was stored. "
    "Check your network connection and the API key pair, then run this command again."
)
_NOT_ONE_XPI_MESSAGE = "Mozilla signing finished, but it did not produce exactly one signed .xpi. Nothing was stored."
_STAGING_FAILED_MESSAGE = "The plugin bundle could not be prepared for signing. Nothing was signed or uploaded."
_BUNDLE_UNREADABLE_MESSAGE = "The plugin bundle's manifest could not be read. Nothing was signed or uploaded."
_STORE_FAILED_MESSAGE = "The signed file could not be stored, so nothing was recorded."


@dataclass(frozen=True)
class SignRunResult:
    """What the runner reports: web-ext's exit code (None when it did not run to an exit), and whether it timed out."""

    returncode: "int | None"
    timed_out: bool = False


@dataclass(frozen=True)
class StoredSigned:
    """What is stored for one plugin version: ``signed`` (record and file match), ``missing``, or ``tampered``.

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
    settings["gecko"] = {**(gecko if isinstance(gecko, dict) else {}), "id": PLUGIN_ADDON_ID}
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


def sign_argv(staged_dir: "Path | str", artifacts_dir: "Path | str", timeout_s: float) -> list[str]:
    """The web-ext argv. It carries no credential: those are passed in the call's environment.

    ``--no-config-discovery`` stops a config file in the home folder from changing the target or the options.
    """
    timeout_ms = max(1, int(round(float(timeout_s) * 1000)))
    return [
        "npx", "--yes", WEB_EXT_PACKAGE, "sign",
        "--source-dir", str(staged_dir),
        "--artifacts-dir", str(artifacts_dir),
        "--channel", "unlisted",
        "--timeout", str(timeout_ms),
        "--no-config-discovery",
    ]


# ---------------------------------------------------------------------------
# The stored signed artifact and its record
# ---------------------------------------------------------------------------


def _signed_path(state_dir: "Path | str", version: str) -> Path:
    return Path(state_dir) / SIGNED_DIRNAME / f"{PLUGIN_ADDON_ID}-{version}.xpi"


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_records(state_dir: "Path | str") -> list[dict[str, Any]]:
    """The records in ``signed.json``. A missing, unreadable or wrong-shape file has none. Never raises."""
    try:
        payload = json.loads((Path(state_dir) / RECORD_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


def stored_signed_artifact(
    state_dir: "Path | str", version: str, *, source_sha256: "str | None" = None,
) -> StoredSigned:
    """What is stored for ``version``, and for ``source_sha256`` when given. Never raises.

    ``missing``: no record matches (or the record's file is gone). ``tampered``: a record matches and the file is
    there, but its sha256 is not the recorded one, or it cannot be read. ``signed``: the file's sha256 matches.
    """
    try:
        if not isinstance(version, str) or not _VERSION_PATTERN.fullmatch(version):
            return StoredSigned(status=MISSING_STATUS)
        matches = [
            record for record in _read_records(state_dir)
            if record.get("addon_id") == PLUGIN_ADDON_ID
            and record.get("version") == version
            and (source_sha256 is None or record.get("source_sha256") == source_sha256)
        ]
        if not matches:
            return StoredSigned(status=MISSING_STATUS)
        target = _signed_path(state_dir, version)
        if not target.is_file():
            return StoredSigned(status=MISSING_STATUS)
        recorded = matches[-1].get("sha256")
        try:
            actual = _sha256_of(target)
        except OSError:
            return StoredSigned(status=TAMPERED_STATUS)
        if not isinstance(recorded, str) or actual != recorded:
            return StoredSigned(status=TAMPERED_STATUS)
        return StoredSigned(status=SIGNED_STATUS, path=target, sha256=recorded)
    except (OSError, ValueError, TypeError):
        return StoredSigned(status=MISSING_STATUS)


def signed_artifact_for_version(
    state_dir: "Path | str", version: str, *, source_sha256: "str | None" = None,
) -> "Path | None":
    """The stored signed .xpi for ``version`` when it is signed, else None. Never raises."""
    stored = stored_signed_artifact(state_dir, version, source_sha256=source_sha256)
    return stored.path if stored.signed else None


def _conflicting_record(state_dir: "Path | str", version: str, source_sha256: str) -> bool:
    """True when a record for ``version`` holds a different source fingerprint. Never raises."""
    return any(
        record.get("addon_id") == PLUGIN_ADDON_ID
        and record.get("version") == version
        and record.get("source_sha256") != source_sha256
        for record in _read_records(state_dir)
    )


def record_signed_artifact(
    state_dir: "Path | str", signed_file: "Path | str", *, source_dir: "Path | str | None" = None,
    version: "str | None" = None, source_sha256: "str | None" = None,
    clock: "Callable[[], float] | None" = None,
) -> Path:
    """Store ``signed_file`` for ``version``, and record it. Returns the stored path.

    ``version`` and ``source_sha256`` are the values captured before signing. When they are not given, they are
    read from ``source_dir``. The file is written first and the record second, so a record never points at a
    file that was not written. A record replaces any earlier record for the same version. The key and the
    secret are never stored.
    """
    state = Path(state_dir)
    if version is None:
        version = bundle_version(source_dir)
    if source_sha256 is None:
        source_sha256 = source_fingerprint(source_dir)
    payload = Path(signed_file).read_bytes()
    target = _signed_path(state, version)
    plugin._atomic_write_bytes(target, payload)
    now = (clock if clock is not None else time.time)()
    entry = {
        "addon_id": PLUGIN_ADDON_ID,
        "version": version,
        "signed_at": datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "source_sha256": source_sha256,
    }
    kept = [
        record for record in _read_records(state)
        if not (record.get("addon_id") == PLUGIN_ADDON_ID and record.get("version") == version)
    ]
    kept.append(entry)
    text = json.dumps({"records": kept}, indent=2, sort_keys=True) + "\n"
    plugin._atomic_write_bytes(state / RECORD_FILENAME, text.encode("utf-8"))
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
    """The API key and secret, stripped, or None when either is missing or empty. ``env`` None means os.environ."""
    source = os.environ if env is None else env
    key = str(source.get(API_KEY_ENV) or "").strip()
    secret = str(source.get(API_SECRET_ENV) or "").strip()
    return (key, secret) if key and secret else None


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
    """Run the sign argv through npx, with its output in two files and never in pipes. Never raises.

    Pipes are avoided on purpose: a grandchild that inherits a pipe keeps it open, and that blocks the read. The
    wall clock is ``timeout_s`` plus a grace period, so web-ext's own timeout normally fires first. When the wait runs
    out, the child's process tree is killed (see :func:`_kill_signing_tree`). If npx is missing or cannot
    start, the result has no exit code.
    """
    npx = shutil.which("npx")
    if npx is None:
        return SignRunResult(returncode=None)
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
                process = subprocess.Popen(
                    [npx, *argv[1:]],
                    env=dict(env),
                    cwd=str(cwd),
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    **kwargs,
                )
                try:
                    returncode = process.wait(timeout=float(timeout_s) + _WALL_CLOCK_GRACE_S)
                except subprocess.TimeoutExpired:
                    _kill_signing_tree(process)
                    return SignRunResult(returncode=None, timed_out=True)
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
) -> dict[str, Any]:
    """Sign the bundle's current version and store the result. Never raises.

    Returns ``status`` as one of ``already_signed`` (a matching stored file exists, so no runner call),
    ``refused_version_reused`` (a record for this version has a different source, so no runner call),
    ``no_credentials`` (either variable is missing or empty, so no runner call), ``signed``, ``timeout`` or
    ``failed``. A failed, timed-out, changed-source, or unusable result stores nothing. ``runner`` is called as
    ``runner(argv, env, timeout_s=..., cwd=...)`` and defaults to running ``npx``.
    """
    state = Path(state_dir)
    if not _valid_timeout_seconds(timeout_s):
        return _outcome("failed", message=RANGE_MESSAGE)
    source = plugin._resolve_source(source_dir)
    try:
        snapshot = _packed_snapshot(source)
        version = _version_from_manifest(_manifest_bytes(snapshot))
        fingerprint = _fingerprint_of(snapshot)
    except (OSError, ValueError, TypeError):
        return _outcome("failed", message=_BUNDLE_UNREADABLE_MESSAGE)
    existing = signed_artifact_for_version(state, version, source_sha256=fingerprint)
    if existing is not None:
        return _outcome("already_signed", version=version, path=existing)
    if _conflicting_record(state, version, fingerprint):
        return _outcome("refused_version_reused", version=version, message=REUSED_VERSION_MESSAGE)
    creds = _credentials(env)
    if creds is None:
        return _outcome("no_credentials", version=version, message=NO_CREDENTIALS_MESSAGE)
    run = runner if runner is not None else _run_web_ext
    with tempfile.TemporaryDirectory(prefix="meridian-zotero-sign-", ignore_cleanup_errors=True) as scratch:
        scratch_dir = Path(scratch)
        artifacts = scratch_dir / "artifacts"
        try:
            staged = _stage_snapshot(snapshot, scratch_dir / "staged")
            artifacts.mkdir()
            argv = sign_argv(staged, artifacts, timeout_s)
        except (OSError, ValueError, TypeError, OverflowError):
            return _outcome("failed", version=version, message=_STAGING_FAILED_MESSAGE)
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
            stored = record_signed_artifact(
                state, produced[0], version=version, source_sha256=fingerprint, clock=clock,
            )
        except (OSError, ValueError, TypeError):
            return _outcome("failed", version=version, message=_STORE_FAILED_MESSAGE)
    return _outcome("signed", version=version, path=stored)


# ---------------------------------------------------------------------------
# CLI: python -m meridian zotero sign-plugin [--timeout-minutes N] [--dry-run]
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
    if status == "already_signed":
        return f"The Zotero bridge plugin {version} is already signed at {result.get('path')}. Nothing was uploaded."
    return str(result.get("message") or "Signing did not complete. Nothing was stored.")


def _dry_run() -> int:
    print("Dry run: nothing will be signed or uploaded.")
    try:
        source = plugin._resolve_source(None)
        version = bundle_version(source)
        fingerprint = source_fingerprint(source)
    except (OSError, ValueError, TypeError):
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
    args = parser.parse_args(argv)
    try:
        minutes = parse_timeout_minutes(args.timeout_minutes)
    except ValueError as exc:
        print(str(exc))
        return 1
    if args.dry_run:
        return _dry_run()
    result = sign_plugin(state_dir=plugin.signing_state_dir(), timeout_s=minutes * 60)
    print(_status_text(result))
    return 0 if result["status"] in ("signed", "already_signed") else 1
