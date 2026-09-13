"""Repo-scope validation shared by local-runner entry points (ba31dedf).

Confirmed gaps this closes (launch-readiness discovery, 2026-08-31):

* ``tunnel_client.run_tunnel`` defaulted an unset ``--repo`` to ``Path.cwd()``
  with zero rejection of the home directory (or anything above it) -- running
  the tunnel from a bare shell session in ``$HOME`` silently scoped the
  filesystem/code-intel connectors to the user's ENTIRE home tree.
* ``scripts/meridian_connect.py``'s local self-hosted health-check fallback
  contained a literal ``cd "$HOME" && nohup pixi run start`` -- the exact
  class of home-directory-execution bug this module exists to prevent.
* ``meridian/mcp/handler.py``'s ``set_active_repo`` plain-``repo_path`` branch
  accepted any caller-supplied string with no validation at all, unlike its
  own ``worktree_id`` branch (explicitly commented as "the validated,
  fail-closed activation path").

Two validators, because the two call sites have different information:

* :func:`validate_repo_scope` -- for a process that can resolve paths against
  a REAL local filesystem (the local tunnel client, running on the same
  machine as the repo). Resolves symlinks/relative segments and compares
  against the actual :func:`pathlib.Path.home`.
* :func:`looks_like_bare_home_directory` -- for a process validating a path
  STRING reported by a remote caller over the network (e.g. a hosted MCP
  handler validating a tunnel client's ``repo_path`` argument) where
  ``Path.home()`` in this process is the SERVER's home directory, not the
  remote caller's -- filesystem resolution would silently fail to catch
  anything. This is a string-shape heuristic instead: it recognizes a BARE
  home-directory path (Windows ``C:\\Users\\<name>``, Linux ``/home/<name>``,
  macOS ``/Users/<name>``, or ``/root``) with no subdirectory beneath it --
  a real project folder living inside one of those (e.g.
  ``C:\\Users\\me\\project``) is never flagged.

Both fail closed: an ambiguous or missing scope is a validation error, never a
silent default.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path


class RepoScopeError(ValueError):
    """Raised when a repo path fails scope validation. Always fail closed --
    callers must surface this to the user/operator, never swallow it and fall
    back to guessing a path."""


# Bare home-directory shapes, no subdirectory beneath them. Anchored to the
# WHOLE string (after stripping a trailing slash) so `C:\Users\me\project`
# does not match -- only `C:\Users\me` itself does.
_HOME_DIR_SHAPE_RE = re.compile(
    r"^(?:[A-Za-z]:[\\/]Users[\\/][^\\/]+|/home/[^/]+|/Users/[^/]+|/root)$"
)


def looks_like_bare_home_directory(path_str: "str | None") -> bool:
    """String-shape heuristic: True if *path_str* IS a home directory itself.

    Used where the validating process has no way to resolve the path against
    a real filesystem (see module docstring). Deliberately conservative --
    only flags the home directory itself, never a subdirectory inside it, so
    it never rejects a legitimate project checkout that happens to live under
    a user's home (the overwhelmingly common case).
    """
    if not path_str:
        return False
    candidate = path_str.strip().rstrip("\\/")
    if not candidate:
        return False
    return bool(_HOME_DIR_SHAPE_RE.match(candidate))


def _is_home_or_ancestor_of_home(path: Path, home: Path) -> bool:
    """True if *path* IS *home*, or is an ANCESTOR of *home* (e.g. ``C:\\Users``,
    ``/home``, ``/`` -- a filesystem root that encompasses the whole home tree).

    Deliberately NOT true for a path that is merely a DESCENDANT of home (e.g.
    ``C:\\Users\\me\\project``) -- that is an ordinary, legitimate project
    location and must never be rejected.
    """
    return path == home or path in home.parents


def validate_repo_scope(
    repo_path: "str | Path | None",
    *,
    cwd: "str | Path | None" = None,
    registered_repo_path: "str | Path | None" = None,
    home: "Path | None" = None,
) -> Path:
    """Resolve and validate a local repo scope path. Raises :class:`RepoScopeError`
    rather than ever silently defaulting to an ambiguous scope.

    Rules (ba31dedf):

    1. Missing/empty ``repo_path`` AND missing/empty ``cwd`` is an ambiguous
       scope -- reject rather than guessing.
    2. The resolved path must not BE the home directory, nor an ancestor of
       it (a drive root, ``/home``, ``/``, etc.) -- never silently scope to
       the user's entire home tree. A subdirectory INSIDE home (an ordinary
       project checkout) is always fine.
    3. When *registered_repo_path* is given (the project's own known
       repo/worktree path), the resolved path must equal it or be a
       subdirectory of it -- a mismatch is a cross-project scope error,
       fail closed rather than silently re-scoping to an unrelated tree.

    Returns the resolved, validated :class:`Path`.
    """
    home_dir = Path(home) if home is not None else Path.home()

    candidate = repo_path if repo_path else cwd
    if not candidate:
        raise RepoScopeError(
            "no repo path or cwd provided -- refusing to default to an "
            "ambiguous scope"
        )

    resolved = Path(candidate).resolve()

    if _is_home_or_ancestor_of_home(resolved, home_dir.resolve()):
        raise RepoScopeError(
            f"refusing to scope to the home directory (or an ancestor of "
            f"it): {resolved}. Pass an explicit project repo path instead "
            "of relying on a default."
        )

    if registered_repo_path:
        registered = Path(registered_repo_path).resolve()
        if resolved != registered and registered not in resolved.parents:
            raise RepoScopeError(
                f"cross-project scope mismatch: {resolved} is not the "
                f"registered repo ({registered}) or a subdirectory of it"
            )

    return resolved


def compute_repo_identity(repo_path: "str | Path | None") -> "str | None":
    """W1-G (G1) — derive a project-shared, cross-machine 'repo identity'
    string from a local repository path.

    ``projects.repo_identity`` is project-shared, multi-machine state (it
    travels through the dashboard/API to every session/machine that opens
    the project) — the same non-negotiable rule ``capability_manifest.py``
    already enforces for manifest fields applies here: never persist a
    machine-local absolute path (or a secret) into shared state. A raw
    ``C:\\Users\\alice\\project`` or ``/home/bob/project`` string leaks a
    local username/directory layout to every other machine/session that can
    see the project.

    Instead this returns ``f"{basename}-{sha256(normalized_path)[:12]}"``:

    * ``basename`` — the final path component (the repo folder's own name,
      e.g. ``"paper"``) — a harmless, human-readable label, never a full path.
    * the 12-hex-char suffix — a stable, deterministic fingerprint of the
      FULL normalized path, so the SAME checkout on the SAME machine always
      recomputes the IDENTICAL identity (this is what lets G2's CWD-mismatch
      check compare "the repo this project is bound to" against "the repo
      this session is running from" without ever storing either raw path) —
      while remaining one-way: the stored value cannot be reversed back into
      the original path.

    Normalization lowercases and flips backslashes to forward slashes before
    hashing, so trivial spelling differences that are the SAME path on
    Windows (``C:\\Repo`` vs ``c:/Repo/``) hash identically. Returns ``None``
    for an empty/missing/whitespace-only path — never raises.
    """
    if not repo_path:
        return None
    candidate = str(repo_path).strip().rstrip("\\/")
    if not candidate:
        return None
    normalized = candidate.replace("\\", "/").lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    basename = normalized.rsplit("/", 1)[-1] or "repo"
    return f"{basename}-{digest}"
