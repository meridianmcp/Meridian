"""Path validation and portable path references for the local Outputs package."""
from __future__ import annotations

import os


def _require_within(outputs_dir: str, path: str) -> str:
    """Return a real path below ``outputs_dir`` or raise ``ValueError``.

    Relative paths are interpreted from the outputs root. ``realpath`` makes
    the check symlink-aware, and ``commonpath`` avoids unsafe string-prefix
    comparisons (for example, ``outputs-old`` is not inside ``outputs``).
    The exception deliberately omits the resolved path so it cannot leak a
    machine-local absolute path into logs or API errors.
    """
    if (
        not isinstance(outputs_dir, (str, os.PathLike))
        or not os.fspath(outputs_dir).strip()
    ):
        raise ValueError("outputs_dir must be a non-empty path")
    if (
        not isinstance(path, (str, os.PathLike))
        or not os.fspath(path).strip()
    ):
        raise ValueError("path must be a non-empty path")

    try:
        root = os.path.realpath(os.path.abspath(os.fspath(outputs_dir)))
        supplied = os.fspath(path)
        candidate = supplied if os.path.isabs(supplied) else os.path.join(root, supplied)
        resolved = os.path.realpath(os.path.abspath(candidate))
        common = os.path.commonpath((root, resolved))
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError("path could not be resolved under outputs_dir") from exc

    if os.path.normcase(common) != os.path.normcase(root):
        raise ValueError("path must resolve within outputs_dir")
    return resolved


def relative_output_path(outputs_dir: str, path: str) -> str:
    """Return a stable slash-separated path relative to ``outputs_dir``."""
    resolved = _require_within(outputs_dir, path)
    root = os.path.realpath(os.path.abspath(os.fspath(outputs_dir)))
    relative = os.path.relpath(resolved, root)
    return relative.replace("\\", "/")


def relative_reference(outputs_dir: str, path: str) -> str | None:
    """Relativize an absolute source-script path without constraining its root.

    Generating scripts often live beside, rather than inside, an outputs tree.
    Preserve same-volume resolvability while keeping drive letters and
    usernames out of persistent ledgers. ``None`` means they are on a
    different volume and cannot be represented as a root-relative reference.
    """
    raw = os.fspath(path)
    if not os.path.isabs(raw):
        return raw.replace("\\", "/")
    root = os.path.realpath(os.path.abspath(os.fspath(outputs_dir)))
    resolved = os.path.realpath(os.path.abspath(raw))
    try:
        return os.path.relpath(resolved, root).replace("\\", "/")
    except ValueError:
        # Different Windows volumes cannot be represented by a root-relative
        # reference without persisting the forbidden drive-qualified path.
        return None


def resolve_reference(outputs_dir: str, path: str) -> str:
    """Resolve a ledger reference, supporting legacy absolute paths."""
    raw = os.fspath(path)
    if os.path.isabs(raw):
        return os.path.realpath(os.path.abspath(raw))
    root = os.path.realpath(os.path.abspath(os.fspath(outputs_dir)))
    return os.path.realpath(os.path.abspath(os.path.join(root, raw)))
