"""Mozilla unlisted signing for the Meridian Zotero bridge plugin.

Firefox and Zotero load an add-on that Mozilla has signed. This module signs the bridge once per plugin
version, outside the install path:

* ``stage_for_signing`` copies the files ``build_xpi`` packs into a scratch folder, with the same excludes
  and LF line endings, and adds ``browser_specific_settings.gecko.id`` to the STAGED manifest only. The
  shipped ``extensions/zotero-bridge/manifest.json`` is never changed.
* ``sign_plugin`` runs ``web-ext sign --channel unlisted`` (pinned to ``WEB_EXT_PACKAGE``) on the staged
  folder. The API key and secret go into the environment of that one call. They are never on the command
  line, never written to a file, and never returned. A zero exit with exactly one zip .xpi is stored as
  ``<state_dir>/signed/<addon-id>-<version>.xpi``, with a record in ``<state_dir>/signed.json``.
* ``signed_artifact_for_version`` returns that stored file only when its record matches and the file's
  sha256 matches the record. The install path uses it, and it never calls ``sign_plugin``.

Signing is the CLI step ``python -m meridian zotero sign-plugin``. It never closes Zotero and it never runs
during an install. The state folder is per user, so a build signed from a source checkout is also the one
the packaged tray app installs.

UNVERIFIED on a real machine: that Mozilla's unlisted signing accepts this bridge, and that Zotero loads the
signed .xpi from the profile's extensions folder. Nothing here has been run against Mozilla.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
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
SIGNED_DIRNAME = "signed"
RECORD_FILENAME = "signed.json"
_WALL_CLOCK_GRACE_S = 60.0
# web-ext reports its own validation or approval timeout with this text. Used only to classify the exit,
# never echoed.
_TIMEOUT_MARKER = b"timeout exceeded"
_VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")

NO_CREDENTIALS_MESSAGE = (
    f"Mozilla signing needs two environment variables: {API_KEY_ENV} (the API key, the JWT issuer) and "
    f"{API_SECRET_ENV} (the API secret, the JWT secret). Create the key pair on the API Keys page of the "
    "addons.mozilla.org developer hub, set both as user environment variables, then run this command again. "
    "Nothing was signed or uploaded."
)
_TIMEOUT_TEMPLATE = "Mozilla did not finish signing within {minutes} minutes. Nothing was stored. Run this command again later."
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


# ---------------------------------------------------------------------------
# The bundle: its version, its fingerprint, and the staged copy that web-ext signs
# ---------------------------------------------------------------------------


def bundle_version(source_dir: "Path | str") -> str:
    """The bundle manifest's version. Raises ValueError when it is missing or unsafe to use in a file name."""
    manifest = json.loads((Path(source_dir) / "manifest.json").read_text(encoding="utf-8"))
    version = manifest.get("version") if isinstance(manifest, dict) else None
    if not isinstance(version, str) or not _VERSION_PATTERN.fullmatch(version):
        raise ValueError("the bundle manifest has no usable version")
    return version


def source_fingerprint(source_dir: "Path | str") -> str:
    """A sha256 over the packed runtime files (name and content, LF-normalised), so the same source always matches.

    It is recorded at signing time. An install compares it with the current source, so an edit that does not bump
    the version is never installed under a signature for the old code.
    """
    digest = hashlib.sha256()
    for name, path in plugin._runtime_files(Path(source_dir)):
        packed = hashlib.sha256(plugin._packed_bytes(path)).hexdigest()
        digest.update(f"{name}\t{packed}\n".encode("utf-8"))
    return digest.hexdigest()


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


def stage_for_signing(bundle_dir: "Path | str", dest_dir: "Path | str") -> Path:
    """Copy the files ``build_xpi`` packs into ``dest_dir``, adding the gecko id to the staged manifest only."""
    source = Path(bundle_dir)
    dest = Path(dest_dir)
    for name, path in plugin._runtime_files(source):
        data = plugin._packed_bytes(path)
        if name == "manifest.json":
            data = _staged_manifest(data)
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return dest


def sign_argv(staged_dir: "Path | str", artifacts_dir: "Path | str", timeout_s: float) -> list[str]:
    """The web-ext argv. It carries no credential: those are passed in the call's environment.

    ``--no-config-discovery`` stops a config file in the home folder from changing the target or the options.
    """
    return [
        "npx", "--yes", WEB_EXT_PACKAGE, "sign",
        "--source-dir", str(staged_dir),
        "--artifacts-dir", str(artifacts_dir),
        "--channel", "unlisted",
        "--timeout", str(int(round(float(timeout_s) * 1000))),
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


def signed_artifact_for_version(
    state_dir: "Path | str", version: str, *, source_sha256: "str | None" = None,
) -> "Path | None":
    """The stored signed .xpi for ``version``, only when its record matches and its sha256 matches. Never raises.

    With ``source_sha256``, the record must also be for that exact source. A missing or mismatched record,
    a missing file, or a file whose hash differs from the record all give None.
    """
    try:
        if not isinstance(version, str) or not _VERSION_PATTERN.fullmatch(version):
            return None
        matches = [
            record for record in _read_records(state_dir)
            if record.get("addon_id") == PLUGIN_ADDON_ID
            and record.get("version") == version
            and (source_sha256 is None or record.get("source_sha256") == source_sha256)
        ]
        if not matches:
            return None
        target = _signed_path(state_dir, version)
        if not target.is_file() or _sha256_of(target) != matches[-1].get("sha256"):
            return None
        return target
    except (OSError, ValueError, TypeError):
        return None


def record_signed_artifact(
    state_dir: "Path | str", signed_file: "Path | str", *, source_dir: "Path | str",
    clock: "Callable[[], float] | None" = None,
) -> Path:
    """Store ``signed_file`` for the bundle's version, and record it. Returns the stored path.

    The file is written first and the record second, so a record never points at a file that was not written.
    A record replaces any earlier record for the same version. The key and the secret are never stored.
    """
    state = Path(state_dir)
    version = bundle_version(source_dir)
    fingerprint = source_fingerprint(source_dir)
    payload = Path(signed_file).read_bytes()
    target = _signed_path(state, version)
    plugin._atomic_write_bytes(target, payload)
    now = (clock if clock is not None else time.time)()
    entry = {
        "addon_id": PLUGIN_ADDON_ID,
        "version": version,
        "signed_at": datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "source_sha256": fingerprint,
    }
    kept = [
        record for record in _read_records(state)
        if not (record.get("addon_id") == PLUGIN_ADDON_ID and record.get("version") == version)
    ]
    kept.append(entry)
    text = json.dumps({"records": kept}, indent=2, sort_keys=True) + "\n"
    plugin._atomic_write_bytes(state / RECORD_FILENAME, text.encode("utf-8"))
    return target


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


def _run_web_ext(argv: list[str], env: Mapping[str, str], *, timeout_s: float, cwd: "Path | str") -> SignRunResult:
    """Run the sign argv through npx. Its output is read for the timeout marker and then discarded. Never raises.

    The wall clock is ``timeout_s`` plus a grace period, so web-ext's own timeout normally fires first. If
    npx is missing, the result has no exit code.
    """
    npx = shutil.which("npx")
    if npx is None:
        return SignRunResult(returncode=None)
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = plugin._CREATE_NO_WINDOW
    try:
        completed = subprocess.run(
            [npx, *argv[1:]],
            env=dict(env),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=float(timeout_s) + _WALL_CLOCK_GRACE_S,
            check=False,
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        return SignRunResult(returncode=None, timed_out=True)
    except OSError:
        return SignRunResult(returncode=None)
    output = (completed.stdout or b"") + (completed.stderr or b"")
    timed_out = completed.returncode != 0 and _TIMEOUT_MARKER in output.lower()
    return SignRunResult(returncode=completed.returncode, timed_out=timed_out)


def _outcome(status: str, *, version: "str | None" = None, path: "Path | None" = None, message: "str | None" = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": status, "version": version, "path": None if path is None else str(path)}
    if message:
        payload["message"] = message
    return payload


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

    Returns ``status`` as one of ``already_signed`` (a matching stored file exists, so no runner call), ``no_credentials``
    (either variable is missing or empty, so no runner call), ``signed``, ``timeout`` or ``failed``. A failed,
    timed-out, or unusable result stores nothing. ``runner`` is called as
    ``runner(argv, env, timeout_s=..., cwd=...)`` and defaults to running ``npx``.
    """
    state = Path(state_dir)
    source = plugin._resolve_source(source_dir)
    try:
        version = bundle_version(source)
        fingerprint = source_fingerprint(source)
    except (OSError, ValueError, TypeError):
        return _outcome("failed", message=_BUNDLE_UNREADABLE_MESSAGE)
    existing = signed_artifact_for_version(state, version, source_sha256=fingerprint)
    if existing is not None:
        return _outcome("already_signed", version=version, path=existing)
    creds = _credentials(env)
    if creds is None:
        return _outcome("no_credentials", version=version, message=NO_CREDENTIALS_MESSAGE)
    run = runner if runner is not None else _run_web_ext
    with tempfile.TemporaryDirectory(prefix="meridian-zotero-sign-") as scratch:
        scratch_dir = Path(scratch)
        try:
            staged = stage_for_signing(source, scratch_dir / "staged")
        except (OSError, ValueError, TypeError):
            return _outcome("failed", version=version, message=_STAGING_FAILED_MESSAGE)
        artifacts = scratch_dir / "artifacts"
        artifacts.mkdir()
        argv = sign_argv(staged, artifacts, timeout_s)
        try:
            result = run(argv, _child_env(env, *creds), timeout_s=timeout_s, cwd=scratch_dir)
        except Exception:  # noqa: BLE001 -- a runner that raises is a failed signing, never a crash
            return _outcome("failed", version=version, message=_FAILED_TEMPLATE.format(code="none"))
        if result.timed_out:
            return _outcome(
                "timeout", version=version,
                message=_TIMEOUT_TEMPLATE.format(minutes=f"{float(timeout_s) / 60:g}"),
            )
        if result.returncode != 0:
            code = "none" if result.returncode is None else result.returncode
            return _outcome("failed", version=version, message=_FAILED_TEMPLATE.format(code=code))
        produced = sorted(artifacts.glob("*.xpi"))
        if len(produced) != 1 or not zipfile.is_zipfile(produced[0]):
            return _outcome("failed", version=version, message=_NOT_ONE_XPI_MESSAGE)
        try:
            stored = record_signed_artifact(state, produced[0], source_dir=source, clock=clock)
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
    signed = signed_artifact_for_version(plugin.signing_state_dir(), version, source_sha256=fingerprint)
    print(f"Signed artifact stored: {'yes (' + str(signed) + ')' if signed is not None else 'no'}")
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
        "--timeout-minutes", type=float, default=DEFAULT_TIMEOUT_S / 60,
        help="How long to wait for Mozilla, in minutes (default: 60).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the plan. Signs nothing and uploads nothing.")
    args = parser.parse_args(argv)
    if args.dry_run:
        return _dry_run()
    if not args.timeout_minutes > 0:
        print("--timeout-minutes must be more than zero. Nothing was signed or uploaded.")
        return 1
    result = sign_plugin(state_dir=plugin.signing_state_dir(), timeout_s=args.timeout_minutes * 60)
    print(_status_text(result))
    return 0 if result["status"] in ("signed", "already_signed") else 1
