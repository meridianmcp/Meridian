#!/usr/bin/env python3
"""Build dist/release/MeridianDesktop-<version>.zip from the onedir bundle and the two install scripts.

Run it after ``python packaging/build.py`` and ``python packaging/verify_bundle.py``:

    python packaging/make_release.py

The zip holds the bundle's files at its root (MeridianDesktop.exe, _internal/, the licence and
notice files). Beside them it holds install.cmd and uninstall.cmd from packaging/release/. The user
unzips the archive and runs install.cmd, which runs ``MeridianDesktop.exe --install --desktop``
from its own folder.

The version is ``version`` under [workspace] in pixi.toml, plain dotted integers such as 0.1.0. A file
or folder named .env, .env.*, *.env, meridian.toml, *.pem, *.key or .netrc is never added, at any depth
and in any letter case, and each one left out is listed. A bundle that holds a symbolic link or a
junction is refused before anything is zipped. The scripts are written with CRLF line endings, which
cmd.exe needs for labels and exit codes to behave.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP_NAME = "MeridianDesktop"
EXE_NAME = "MeridianDesktop.exe"
BUNDLE_DIR = REPO / "dist" / APP_NAME
RELEASE_DIR = REPO / "dist" / "release"
SCRIPTS_DIR = REPO / "packaging" / "release"
PIXI_TOML = REPO / "pixi.toml"
SCRIPT_NAMES = ("install.cmd", "uninstall.cmd")
SECRET_FILE_NAMES = frozenset({"meridian.toml"})
VERSION_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)*")  # the same plain dotted integers the updater accepts


class ReleaseError(RuntimeError):
    """The release cannot be built. The message says what to do."""


@dataclass(frozen=True)
class ReleaseResult:
    path: Path
    files: tuple[str, ...]  # every name written to the zip, sorted
    excluded: tuple[str, ...]  # bundle paths left out as secrets, sorted


def read_version(pixi_toml: Path = PIXI_TOML) -> str:
    """The [workspace] version in pixi.toml, which must be a plain number such as 0.1.0."""
    try:
        data = tomllib.loads(pixi_toml.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseError(f"could not read the version from {pixi_toml.name} ({exc.__class__.__name__})") from exc
    version = data.get("workspace", {}).get("version")
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ReleaseError(f"[workspace] version in {pixi_toml.name} must be a plain number such as 0.1.0")
    return version


def is_secret_name(name: str) -> bool:
    """True for a file or folder name that must never ship, in any letter case.

    Covers the same names as verify_bundle._secret_named: .env and .env.* (any name starting .env), *.env,
    meridian.toml, *.pem, *.key and .netrc.
    """
    lower = name.lower()
    return (
        lower in SECRET_FILE_NAMES
        or lower.startswith(".env")
        or lower.endswith(".env")
        or lower.endswith((".pem", ".key"))
        or lower == ".netrc"
    )


def _is_link(path: Path) -> bool:
    """True for a symbolic link or a Windows junction. Either one could point outside the bundle."""
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or (is_junction is not None and is_junction())


def _crlf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def build_release(bundle: Path, scripts: Path, out_dir: Path, version: str) -> ReleaseResult:
    """Write ``out_dir``/MeridianDesktop-<version>.zip. Replaces an older zip of the same version."""
    bundle = Path(bundle)
    scripts = Path(scripts)
    if not (bundle / EXE_NAME).is_file():
        raise ReleaseError(f"{bundle} has no {EXE_NAME}; build the bundle first with pixi run build")
    for name in SCRIPT_NAMES:
        if not (scripts / name).is_file():
            raise ReleaseError(f"missing {scripts / name}")

    included: list[tuple[str, Path]] = []
    excluded: list[str] = []
    links: list[str] = []
    for path in sorted(bundle.rglob("*")):
        if _is_link(path):
            links.append(path.relative_to(bundle).as_posix())
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(bundle)
        if any(is_secret_name(part) for part in relative.parts):
            excluded.append(relative.as_posix())
            continue
        included.append((relative.as_posix(), path))

    if links:
        raise ReleaseError(
            f"the bundle holds a link, {', '.join(sorted(links))}; remove it from the bundle before building a "
            "release. Nothing was zipped."
        )
    taken = sorted(name for name, _ in included if name in SCRIPT_NAMES)
    if taken:
        raise ReleaseError(f"the bundle already holds {', '.join(taken)}; move it out before building a release")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{APP_NAME}-{version}.zip"
    temp = out_dir / f"{target.name}.tmp"
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for arcname, path in included:
                archive.write(path, arcname)
            for name in SCRIPT_NAMES:
                archive.writestr(name, _crlf((scripts / name).read_bytes()))
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise

    files = tuple(sorted([name for name, _ in included] + list(SCRIPT_NAMES)))
    return ReleaseResult(path=target, files=files, excluded=tuple(sorted(excluded)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Meridian Desktop release zip.")
    parser.add_argument("--bundle", type=Path, default=BUNDLE_DIR, help="onedir bundle folder (default dist/MeridianDesktop)")
    parser.add_argument("--out", type=Path, default=RELEASE_DIR, help="folder for the zip (default dist/release)")
    parser.add_argument("--scripts", type=Path, default=SCRIPTS_DIR, help="folder with install.cmd and uninstall.cmd")
    parser.add_argument("--pixi", type=Path, default=PIXI_TOML, help="pixi.toml that holds the version")
    args = parser.parse_args(argv)
    try:
        version = read_version(args.pixi)
        result = build_release(args.bundle, args.scripts, args.out, version)
    except ReleaseError as exc:
        print(f"release failed: {exc}", file=sys.stderr)
        return 1
    for relative in result.excluded:
        print(f"left out (secret-named): {relative}")
    print(f"wrote {result.path} ({len(result.files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
