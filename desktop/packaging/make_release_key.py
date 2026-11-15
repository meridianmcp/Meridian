#!/usr/bin/env python3
"""Create the Meridian Desktop release signing seed once, and print its public key.

    python packaging/make_release_key.py                   the default seed path
    python packaging/make_release_key.py --seed-path FILE  another path (tests, rotation drills)

The seed is 32 random bytes, stored as base64 in %LOCALAPPDATA%\\meridian-release-keys\\release-ed25519.key.
The file is created only when it does not exist. An existing seed is never overwritten. The seed is never
printed or logged. The only line on stdout is the base64 public key. Paste it into RELEASE_PUBLIC_KEY_B64 in
meridian_desktop/release_key.py and commit that file. Keep the seed out of the repository and out of backups
that other people can read.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import importlib.util
import os
import secrets
import sys
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[1]
KEY_DIR_NAME = "meridian-release-keys"
SEED_FILE_NAME = "release-ed25519.key"
SEED_BYTES = 32
_BAD_SEED = "the release signing key is not base64 of 32 bytes. Its contents are not printed."


class SeedError(RuntimeError):
    """The seed cannot be used. The message never includes the seed."""


def _load_ed25519() -> ModuleType:
    """Load meridian_desktop/ed25519.py by file path, as the publisher loads its packaging siblings."""
    name = "_meridian_ed25519"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / "meridian_desktop" / "ed25519.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ed25519 = _load_ed25519()


def default_seed_path() -> Path:
    """%LOCALAPPDATA%\\meridian-release-keys\\release-ed25519.key, outside the repository."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / KEY_DIR_NAME / SEED_FILE_NAME


def encode_seed(seed: bytes) -> str:
    return base64.b64encode(seed).decode("ascii")


def decode_seed_text(text: str) -> bytes:
    """The seed in base64 text. Raises SeedError, without echoing the text, unless it is 32 bytes."""
    try:
        seed = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise SeedError(_BAD_SEED) from None
    if len(seed) != SEED_BYTES:
        raise SeedError(_BAD_SEED)
    return seed


def read_seed(path: Path) -> bytes:
    """The 32-byte seed stored at path. Raises SeedError when it is missing or unusable."""
    try:
        text = path.read_text(encoding="ascii")
    except FileNotFoundError:
        raise SeedError(
            "the release signing key is missing: %LOCALAPPDATA%\\meridian-release-keys\\release-ed25519.key. "
            "Create it once with python packaging/make_release_key.py, then run again."
        ) from None
    except (OSError, ValueError):
        raise SeedError(
            "the release signing key could not be read. Its contents are not printed. Check the file, then run again."
        ) from None
    return decode_seed_text(text)


def create_seed(path: Path) -> bool:
    """Write a new random seed to path only if nothing is there. True when the file was created."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(encode_seed(secrets.token_bytes(SEED_BYTES)) + "\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return True


def public_key_b64(seed: bytes) -> str:
    return base64.b64encode(ed25519.public_key(seed)).decode("ascii")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create the release signing seed once, then print its public key. The seed is never printed."
    )
    parser.add_argument(
        "--seed-path",
        type=Path,
        default=None,
        help="seed file to create or read (default: %%LOCALAPPDATA%%\\meridian-release-keys\\release-ed25519.key)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = args.seed_path or default_seed_path()
    try:
        created = create_seed(path)
        seed = read_seed(path)
    except SeedError as exc:
        print(f"release key: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("release key: the seed file could not be created or read. Its contents are not printed.", file=sys.stderr)
        return 1
    if created:
        print("release key: created a new seed. It is not printed; keep it outside the repository.", file=sys.stderr)
    else:
        print("release key: the seed already exists and was not changed.", file=sys.stderr)
    print(public_key_b64(seed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
