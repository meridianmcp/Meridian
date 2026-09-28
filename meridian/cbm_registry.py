"""55d48d69 -- codebase-memory index registry: snapshot builder + resolver.

codebase-memory-mcp keeps no central registry. Its "registry" is the set of
``<project>.db`` SQLite files in its cache directory (``$CBM_CACHE_DIR``,
otherwise ``~/.cache/codebase-memory-mcp``), one file per indexed project,
each carrying exactly one ``projects(name, indexed_at, root_path)`` row. The
CLI/MCP ``list_projects`` call opens every DB and probes git per root, which
took ~113 s on the owner's machine -- far too slow for a PreToolUse hook.

This module splits the work in two, exactly as the guard design
(``final_design.index_detection``) specifies:

**Builder** (:func:`build_snapshot`, CLI ``python -m meridian.cbm_registry
--refresh``). Runs only at SessionStart (detached). It scans the cache dir,
computes a stat signature per DB from the size + mtime of the ``.db`` and its
``.db-wal`` (never ``-shm``: every reader touches it), and re-reads ONLY the
DBs whose signature changed, through read-only ``file:...?mode=ro`` URIs with
a 50 ms busy timeout (never ``immutable=1``, which ignores the WAL; never
``count(*)``, which cost up to 204 ms on a 176k-node DB -- the node count
comes from ``sqlite_sequence``). A locked/unreadable DB keeps its last good
row. Rows whose root directory no longer exists are dropped. The snapshot is
written atomically (temp file + ``os.replace``); on any catastrophic error the
previous snapshot is left untouched.

**Resolver** (:func:`resolve`). Pure given a snapshot and a filesystem probe
(:class:`RealFS` in production, :class:`DictFS` for the parity fixture). It
walks up from the target to the first ``.git`` using file reads only (a
``.git`` FILE means a linked worktree: ``gitdir:`` -> ``commondir`` -> the
canonical checkout), then selects:

  (a) a pin (``pins.json`` in the guard dir, baked into the snapshot, or the
      ``MERIDIAN_CBM_PROJECT`` env naming a row rooted there). A pin keyed on
      the worktree root is enforced like an own index; one reached only via the
      canonical root of a LINKED worktree resolves to ``canonical`` mode
      (advisory), because that graph is the canonical checkout, not the branch;
  (b) otherwise an OWN index (``root_path == worktree root``);
  (c) otherwise, for a linked worktree, the canonical checkout's index
      (``canonical`` mode -- advisory only, the graph is not your branch);
  (d) otherwise the longest existing ancestor root (``ancestor`` mode --
      advisory only);
  (e) otherwise nothing (the caller allows silently).

Same-root duplicates are broken, in order, by: explicit pin; slug-name match
(``name == slug(on-disk root_path)``, the name ``index_repository``/the
watcher converge on); newest ``indexed_at``; most nodes; lexically smallest
name. Losing rows are reported as ``shadowed``. "Most nodes" is deliberately
NOT first: on the owner's machine the largest Meridian index was ~86% stale
``.codex`` worktree copies.

Freshness is ``max(indexed_at, .db mtime, .db-wal mtime)`` within 7 days --
one stat of the winning DB at decision time. The mtime is a lower bound on
the watcher's last activity, so a snapshot that is hours old but whose DB is
still being written stays "fresh"; there is no wall-clock TTL on the snapshot
itself (a signature mismatch never forces an allow).

Everything here is pure string handling for paths (no ``os.path.abspath``,
which maps ``"C:"`` to the current directory on drive C) so the same code and
the same fixture behave identically on Windows and on Linux CI.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import sqlite3
import stat as _stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Protocol

SNAPSHOT_SCHEMA = "meridian-guard-snapshot/1"
STALE_DAYS = 7
STALE_SECONDS = STALE_DAYS * 86400
SQLITE_TIMEOUT_S = 0.05
MAX_COVERED_DIRS = 2000
# Upper bound on file_hashes rows scanned for covered_dirs (a 9k-file index is ~10 ms).
MAX_COVERAGE_ROWS = 200000
# An index is PARTIAL (unfinished or broken build, e.g. 90 nodes / 2 File nodes for
# 1146 hashed files next to a *.db.corrupt) when it has fewer graph nodes than hashed
# files -- every healthy index on the owner's machine has >= 1 File node per hashed
# file plus symbol nodes -- or when it is tiny (< PARTIAL_TINY_NODES) and a
# same-root sibling is more than PARTIAL_SIBLING_RATIO times larger. pick() never
# lets a partial index beat a whole one, and the guard only advises (never
# denies) for a partial winner.
PARTIAL_SIBLING_RATIO = 100
PARTIAL_TINY_NODES = 1000
# Extensions that make a top-level directory count as COVERED code (G1/G3 deny only
# inside covered dirs). guard_core.CODE_EXTS is this same set.
CODE_EXTS = frozenset({
    "py", "pyi", "pyx", "ts", "tsx", "js", "jsx", "mjs", "cjs", "mts", "cts", "go", "rs",
    "java", "kt", "kts", "scala", "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "cs", "fs",
    "rb", "php", "swift", "m", "mm", "sh", "bash", "zsh", "ps1", "psm1", "psd1", "sql",
    "vue", "svelte", "lua", "r", "jl", "dart", "ex", "exs", "erl", "hrl", "hs", "ml",
    "mli", "clj", "cljs", "groovy", "pl", "pm", "css", "scss", "sass", "less", "html",
    "htm", "tex", "ipynb", "proto", "tf", "nim", "zig", "sol",
})
_MAX_WALK = 128
# Default MCP tool prefix when no local codebase-memory server is detected.
DEFAULT_SERVER_PREFIX = "mcp__codebase-memory-mcp__"

_DRIVE_RE = re.compile(r"^([A-Za-z]):(?:/|$)")
_DRIVE_ANY_RE = re.compile(r"^([A-Za-z]):")
_MSYS_RE = re.compile(r"^/([A-Za-z])(?:/|$)")
_WSL_RE = re.compile(r"^/mnt/([A-Za-z])(?:/|$)")
_INDEXED_AT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})")
_UUIDISH_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")


# ---------------------------------------------------------------------------
# Path normalization (pure string handling -- identical on every OS)
# ---------------------------------------------------------------------------


def is_windows_path(p: str | None) -> bool:
    """True when ``p`` starts with a drive letter (``C:``, ``c:/``...)."""
    return bool(p) and bool(_DRIVE_ANY_RE.match(p))  # type: ignore[arg-type]


def norm_path(p: Any, cwd: str | None = None, *, msys: bool = False) -> str | None:
    """Normalize a path string for comparison. Never touches the filesystem.

    - backslashes become forward slashes, duplicate slashes collapse;
    - the drive letter is uppercased and a drive root stays ``C:/``
      (``"C:"`` alone is the drive root, never "the cwd on C:");
    - with ``msys=True`` the Git-Bash spellings ``/c/...`` and ``/mnt/c/...``
      become ``C:/...``;
    - a relative path is joined to ``cwd`` (returns None without a cwd);
    - ``.`` and ``..`` segments are resolved textually;
    - the trailing slash is stripped (except for a root).

    Returns None for a non-string / empty input.
    """
    if not isinstance(p, str):
        return None
    s = p.strip()
    if not s:
        return None
    s = s.replace("\\", "/")
    if msys:
        m = _WSL_RE.match(s) or _MSYS_RE.match(s)
        if m:
            rest = s[m.end():]
            s = m.group(1).upper() + ":/" + rest
    prefix: str
    rest: str
    if _DRIVE_ANY_RE.match(s):
        prefix = s[0].upper() + ":"
        rest = s[2:]
    elif s.startswith("//"):
        # UNC: //server/share/...
        parts = [x for x in s[2:].split("/") if x]
        if len(parts) < 2:
            return "//" + "/".join(parts)
        prefix = "//" + parts[0] + "/" + parts[1]
        rest = "/".join(parts[2:])
    elif s.startswith("/"):
        prefix = ""
        rest = s
    else:
        if cwd is None:
            return None
        base = norm_path(cwd, None, msys=msys)
        if base is None:
            return None
        return norm_path(base.rstrip("/") + "/" + s, None, msys=False)
    segs: list[str] = []
    for seg in rest.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if segs:
                segs.pop()
            continue
        segs.append(seg)
    if prefix.startswith("//"):
        return prefix + ("/" + "/".join(segs) if segs else "")
    return prefix + "/" + "/".join(segs)


def path_key(p: str | None) -> str | None:
    """Case-folded comparison key (Windows paths are case-insensitive)."""
    return p.lower() if isinstance(p, str) else None


def parent_path(p: str) -> str:
    """Textual parent of a normalized path; a root is its own parent."""
    if p == "/" or re.fullmatch(r"[A-Za-z]:/", p):
        return p
    head = p.rsplit("/", 1)[0]
    if head == "":
        return "/"
    if re.fullmatch(r"[A-Za-z]:", head):
        return head + "/"
    return head


def is_under(key: str | None, root_key: str | None) -> bool:
    """True when ``key`` equals ``root_key`` or lies below it (keys, not paths)."""
    if not key or not root_key:
        return False
    if key == root_key:
        return True
    r = root_key if root_key.endswith("/") else root_key + "/"
    return key.startswith(r)


def rel_path(key_or_path: str, root: str) -> str:
    """Path of ``key_or_path`` relative to ``root`` ("" when equal). Case-insensitive."""
    k = key_or_path.lower()
    r = root.lower()
    if k == r:
        return ""
    r2 = r if r.endswith("/") else r + "/"
    if k.startswith(r2):
        return key_or_path[len(r2):]
    return key_or_path


def slug(path: str | None) -> str:
    """Mirror of codebase-memory's ``cbm_project_name_from_path`` (src/pipeline/fqn.c).

    Every byte outside ``[A-Za-z0-9._-]`` becomes ``-`` (non-ASCII bytes become
    two lowercase hex digits), runs of ``-`` and of ``.`` collapse, leading
    ``-``/``.`` and trailing ``-`` are trimmed, and names over 200 bytes keep 191
    bytes plus ``-`` and an 8-hex FNV-1a hash of the full name. Apply it to the
    registry row's ON-DISK-cased ``root_path``, never to user input casing.
    """
    if not path:
        return "root"
    if all(c in "/\\:" for c in path):
        return "root"
    raw = path.replace("\\", "/").encode("utf-8", "surrogatepass")
    out: list[str] = []
    for b in raw:
        if (0x61 <= b <= 0x7A) or (0x41 <= b <= 0x5A) or (0x30 <= b <= 0x39) or b in (0x2E, 0x5F, 0x2D):
            out.append(chr(b))
        elif b >= 0x80:
            out.append("%02x" % b)
        else:
            out.append("-")
    collapsed: list[str] = []
    prev = ""
    for ch in "".join(out):
        if (ch == "-" and prev == "-") or (ch == "." and prev == "."):
            continue
        collapsed.append(ch)
        prev = ch
    s = "".join(collapsed).lstrip("-.").rstrip("-")
    if not s:
        return "root"
    if len(s) > 200:
        h = 2166136261
        for b in s.encode("ascii"):
            h ^= b
            h = (h * 16777619) & 0xFFFFFFFF
        s = s[:191] + "-%08x" % h
    return s


def parse_indexed_at(value: Any) -> int:
    """``YYYY-MM-DDTHH:MM:SSZ`` -> epoch seconds (UTC); 0 when unparseable."""
    if not isinstance(value, str):
        return 0
    m = _INDEXED_AT_RE.match(value.strip())
    if not m:
        return 0
    try:
        y, mo, d, hh, mm, ss = (int(x) for x in m.groups())
        return int(calendar.timegm((y, mo, d, hh, mm, ss, 0, 0, 0)))
    except (ValueError, OverflowError):
        return 0


def home_dir(env: dict[str, str]) -> str | None:
    """Home directory from ``USERPROFILE`` then ``HOME`` (upper-cased env keys)."""
    for k in ("USERPROFILE", "HOME"):
        v = env.get(k)
        if v:
            n = norm_path(v, None, msys=True)
            if n:
                return n
    return None


def upper_env(env: dict[str, Any] | None) -> dict[str, str]:
    """Case-insensitive view of an env mapping (Windows env names are case-insensitive)."""
    out: dict[str, str] = {}
    if not isinstance(env, dict):
        return out
    for k, v in env.items():
        if isinstance(k, str) and isinstance(v, str):
            out[k.upper()] = v
    return out


def guard_dir(env: dict[str, Any] | None) -> str | None:
    """The guard's state directory: ``%LOCALAPPDATA%/meridian/guard``.

    ``MERIDIAN_GUARD_DIR`` overrides everything else when set (tests,
    relocation) -- this is the single resolver ``guard_core`` uses for both
    PreToolUse/PostToolUse and the briefs, so honouring it here is what keeps
    every caller (not just ``session_brief``'s own wrapper) consistent.
    Without LOCALAPPDATA: ``<home>/AppData/Local/meridian/guard`` for a Windows
    home, else ``${XDG_STATE_HOME:-<home>/.local/state}/meridian/guard``.
    """
    e = upper_env(env)
    override = norm_path(e.get("MERIDIAN_GUARD_DIR"), None, msys=True) if e.get("MERIDIAN_GUARD_DIR") else None
    if override:
        return override
    lad = norm_path(e.get("LOCALAPPDATA"), None, msys=True) if e.get("LOCALAPPDATA") else None
    if lad:
        return lad.rstrip("/") + "/meridian/guard"
    home = home_dir(e)
    if not home:
        return None
    if is_windows_path(home):
        return home.rstrip("/") + "/AppData/Local/meridian/guard"
    xdg = norm_path(e.get("XDG_STATE_HOME"), None) if e.get("XDG_STATE_HOME") else None
    return (xdg or home.rstrip("/") + "/.local/state") + "/meridian/guard"


def default_cache_dir(env: dict[str, Any] | None) -> str | None:
    """``$CBM_CACHE_DIR``, otherwise ``<home>/.cache/codebase-memory-mcp``."""
    e = upper_env(env)
    if e.get("CBM_CACHE_DIR"):
        return norm_path(e["CBM_CACHE_DIR"], None, msys=True)
    home = home_dir(e)
    return home.rstrip("/") + "/.cache/codebase-memory-mcp" if home else None


# ---------------------------------------------------------------------------
# Filesystem probes
# ---------------------------------------------------------------------------


class FsProbe(Protocol):
    """The three filesystem facts the hot path needs. Every method fails soft."""

    def kind(self, path: str) -> str | None:  # "file" | "dir" | None
        ...

    def read_text(self, path: str, limit: int = 65536) -> str | None:
        ...

    def mtime(self, path: str) -> float | None:
        ...


class RealFS:
    """Production probe over the real filesystem. Never raises."""

    def kind(self, path: str) -> str | None:
        try:
            st = os.stat(path)
        except (OSError, ValueError, TypeError):
            return None
        if _stat.S_ISDIR(st.st_mode):
            return "dir"
        if _stat.S_ISREG(st.st_mode):
            return "file"
        return None

    def read_text(self, path: str, limit: int = 65536) -> str | None:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read(limit)
        except (OSError, ValueError, TypeError):
            return None

    def mtime(self, path: str) -> float | None:
        try:
            return float(os.stat(path).st_mtime)
        except (OSError, ValueError, TypeError):
            return None


class DictFS:
    """A virtual filesystem described by plain JSON (the parity fixture's ``filesystems``).

    ``spec = {"dirs": [path, ...], "files": {path: content_or_null},
    "mtimes": {path: epoch_seconds}}``. Every ancestor of a listed dir/file
    is implicitly a directory. Lookups are case-insensitive, like Windows.
    """

    def __init__(self, spec: dict[str, Any] | None = None, *, overlay: dict[str, Any] | None = None):
        self._files: dict[str, str | None] = {}
        self._dirs: set[str] = set()
        self._mtimes: dict[str, float] = {}
        for s in (spec or {}, overlay or {}):
            self._load(s)

    def _add_dir_chain(self, p: str) -> None:
        cur = p
        for _ in range(_MAX_WALK):
            self._dirs.add(cur.lower())
            nxt = parent_path(cur)
            if nxt == cur:
                break
            cur = nxt

    def _load(self, spec: dict[str, Any]) -> None:
        if not isinstance(spec, dict):
            return
        for d in spec.get("dirs") or []:
            n = norm_path(d)
            if n:
                self._add_dir_chain(n)
        files = spec.get("files") or {}
        if isinstance(files, dict):
            for f, content in files.items():
                n = norm_path(f)
                if not n:
                    continue
                self._files[n.lower()] = content if isinstance(content, str) else None
                self._add_dir_chain(parent_path(n))
        mt = spec.get("mtimes") or {}
        if isinstance(mt, dict):
            for f, v in mt.items():
                n = norm_path(f)
                if n and isinstance(v, (int, float)):
                    self._mtimes[n.lower()] = float(v)

    def kind(self, path: str) -> str | None:
        n = norm_path(path)
        if not n:
            return None
        k = n.lower()
        if k in self._files:
            return "file"
        if k in self._dirs:
            return "dir"
        return None

    def read_text(self, path: str, limit: int = 65536) -> str | None:
        n = norm_path(path)
        if not n:
            return None
        v = self._files.get(n.lower())
        return v[:limit] if isinstance(v, str) else None

    def mtime(self, path: str) -> float | None:
        n = norm_path(path)
        if not n:
            return None
        return self._mtimes.get(n.lower())


# ---------------------------------------------------------------------------
# Git layout (file reads only -- no git subprocess)
# ---------------------------------------------------------------------------


def git_roots(target: str, fs: FsProbe) -> tuple[str | None, str | None, bool]:
    """Walk up from ``target`` to the first ``.git``.

    Returns ``(worktree_root, canonical_root, linked)``:
    - ``.git`` directory -> ``(d, d, False)``;
    - ``.git`` file -> read ``gitdir:``; if that gitdir has a ``commondir``
      the target is in a LINKED worktree and canonical_root is the parent of
      the common ``.git`` dir; without ``commondir`` (a submodule) the
      checkout is its own repo;
    - no ``.git`` up to the root -> ``(None, None, False)``.
    """
    d = norm_path(target)
    if not d:
        return None, None, False
    if fs.kind(d) == "file":
        d = parent_path(d)
    for _ in range(_MAX_WALK):
        g = d.rstrip("/") + "/.git"
        k = fs.kind(g)
        if k == "dir":
            return d, d, False
        if k == "file":
            txt = fs.read_text(g, 4096) or ""
            gitdir = None
            for line in txt.splitlines():
                if line.strip().lower().startswith("gitdir:"):
                    gitdir = norm_path(line.split(":", 1)[1].strip(), d)
                    break
            if not gitdir:
                return d, d, False
            common_txt = fs.read_text(gitdir.rstrip("/") + "/commondir", 4096)
            if common_txt is None:
                return d, d, False  # submodule-style gitfile: its own repo
            first = (common_txt.strip().splitlines() or [""])[0].strip()
            common = norm_path(first, gitdir) if first else None
            if not common:
                return d, d, True
            return d, parent_path(common), True
        nxt = parent_path(d)
        if nxt == d:
            break
        d = nxt
    return None, None, False


# ---------------------------------------------------------------------------
# Snapshot validation + resolution
# ---------------------------------------------------------------------------

_ROW_REQUIRED = {"name": str, "root": str, "root_key": str, "db": str}


def _valid_row(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    for k, t in _ROW_REQUIRED.items():
        if not isinstance(row.get(k), t) or not row.get(k):
            return False
    if not isinstance(row.get("indexed_epoch", 0), (int, float)):
        return False
    if not isinstance(row.get("nodes", 0), (int, float)):
        return False
    return True


def valid_snapshot(snapshot: Any) -> dict[str, Any] | None:
    """Return a sanitized snapshot, or None when missing/corrupt (= unknown = allow)."""
    if not isinstance(snapshot, dict) or snapshot.get("schema") != SNAPSHOT_SCHEMA:
        return None
    rows = snapshot.get("rows")
    if not isinstance(rows, list):
        return None
    good = [r for r in rows if _valid_row(r)]
    out = dict(snapshot)
    out["rows"] = good
    if not isinstance(out.get("pins"), dict):
        out["pins"] = {}
    if not isinstance(out.get("servers"), dict):
        out["servers"] = {}
    return out


def row_by_name(snapshot: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Look a project up by name: exact first, then case-insensitive."""
    if not isinstance(name, str) or not name:
        return None
    rows = snapshot.get("rows") or []
    for r in rows:
        if r.get("name") == name:
            return r
    low = name.lower()
    for r in rows:
        if str(r.get("name", "")).lower() == low:
            return r
    return None


def rows_for_root(snapshot: dict[str, Any], root_key: str) -> list[dict[str, Any]]:
    return [r for r in snapshot.get("rows") or [] if r.get("root_key") == root_key]


def pick(cands: list[dict[str, Any]], *, pin_name: str | None = None) -> tuple[dict[str, Any], str, list[str]]:
    """Break a same-root tie. Returns ``(winner, why, shadowed_names)``.

    Order: explicit pin > slug-name match > newest indexed_at > most nodes >
    lexically smallest name (deterministic). A ``partial`` row (unfinished or
    broken build, see :func:`mark_partial`) only wins when it is pinned or every
    candidate is partial.
    """
    winner = None
    why = ""
    if pin_name:
        for c in cands:
            if c["name"] == pin_name or c["name"].lower() == pin_name.lower():
                winner, why = c, "pin"
                break
    pool = [c for c in cands if c.get("partial") is not True] or cands
    if winner is None:
        slugged = sorted((c for c in pool if c.get("slug_match")), key=lambda c: c["name"])
        if slugged:
            winner, why = slugged[0], "slug-name match"
    if winner is None:
        ordered = sorted(
            pool,
            key=lambda c: (-float(c.get("indexed_epoch") or 0), -int(c.get("nodes") or 0), c["name"]),
        )
        winner = ordered[0]
        why = "newest indexed_at" if len(cands) > 1 else "only index"
    shadowed = sorted(c["name"] for c in cands if c is not winner)
    return winner, why, shadowed


def pin_for(snapshot: dict[str, Any], env: dict[str, str], roots: Iterable[str | None]) -> str | None:
    """The pinned project for any of ``roots`` (snapshot pins, then env pin)."""
    pins = snapshot.get("pins") or {}
    for r in roots:
        if r and isinstance(pins.get(r.lower()), str):
            return pins[r.lower()]
    env_pin = env.get("MERIDIAN_CBM_PROJECT")
    if env_pin and _UUIDISH_NAME_RE.match(env_pin):
        row = row_by_name(snapshot, env_pin)
        if row and any(r and row["root_key"] == r.lower() for r in roots):
            return row["name"]
    return None


def resolve(
    target: str, snapshot: dict[str, Any] | None, fs: FsProbe, env: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Resolve the codebase-memory index covering ``target`` (see module docstring).

    Returns a dict with ``mode`` in ``pin|own|canonical|ancestor|none`` plus
    ``winner`` (row or None), ``shadowed`` names, ``index_root``,
    ``worktree_root``, ``canonical_root``, ``linked``, ``rel`` (target
    relative to the worktree root, or to the index root outside git) and
    ``why``. Never raises; an invalid snapshot yields ``mode='none'``.
    """
    e = upper_env(env)
    base: dict[str, Any] = {
        "mode": "none", "winner": None, "shadowed": [], "index_root": None,
        "worktree_root": None, "canonical_root": None, "linked": False,
        "rel": None, "why": "no index covers this path", "target": None,
    }
    try:
        snap = valid_snapshot(snapshot)
        t = norm_path(target)
        if not t:
            base["why"] = "no target"
            return base
        base["target"] = t
        if snap is None:
            base["why"] = "snapshot missing or corrupt"
            return base
        wt, canon, linked = git_roots(t, fs)
        base.update(worktree_root=wt, canonical_root=canon, linked=linked)
        tk = t.lower()

        def _done(mode: str, winner: dict[str, Any], why: str, shadowed: list[str], rel_root: str) -> dict[str, Any]:
            out = dict(base)
            out.update(mode=mode, winner=winner, why=why, shadowed=shadowed,
                       index_root=winner["root"], rel=rel_path(t, rel_root))
            return out

        # A pin keyed on the worktree root is an explicit owner choice for THIS
        # checkout (mode "pin", enforced like an own index). A pin reached only
        # through the canonical root of a LINKED worktree names a graph of the
        # canonical checkout, not this branch, so it stays advisory ("canonical").
        via_canon = False
        pin_name = pin_for(snap, e, [wt])
        if not pin_name and linked and canon and canon.lower() != (wt or "").lower():
            pin_name = pin_for(snap, e, [canon])
            via_canon = bool(pin_name)
        if pin_name:
            row = row_by_name(snap, pin_name)
            if row is None:
                out = dict(base)
                out["why"] = f"pinned project {pin_name!r} is not in the snapshot"
                return out
            same = rows_for_root(snap, row["root_key"])
            winner, _why, shadowed = pick(same or [row], pin_name=row["name"])
            rel_root = wt if wt and is_under(tk, wt.lower()) else winner["root"]
            if via_canon and winner["root_key"] != (wt or "").lower():
                return _done("canonical", winner, "pin (canonical checkout, not this branch)", shadowed, rel_root)
            return _done("pin", winner, "pin", shadowed, rel_root)
        if wt:
            own = rows_for_root(snap, wt.lower())
            if own:
                winner, why, shadowed = pick(own)
                return _done("own", winner, why, shadowed, wt)
            if linked and canon:
                canon_rows = rows_for_root(snap, canon.lower())
                if canon_rows:
                    winner, why, shadowed = pick(canon_rows)
                    why += " (worktree has no index -> canonical checkout)"
                    return _done("canonical", winner, why, shadowed, wt)
        anc = [r for r in snap["rows"] if is_under(tk, r["root_key"])]
        if anc:
            longest = max(len(r["root_key"]) for r in anc)
            cands = [r for r in anc if len(r["root_key"]) == longest]
            winner, why, shadowed = pick(cands)
            return _done("ancestor", winner, why + " (longest ancestor root)", shadowed, winner["root"])
        return base
    except Exception as exc:  # pragma: no cover - defensive fail-open
        out = dict(base)
        out["mode"] = "none"
        out["why"] = f"resolver error: {type(exc).__name__}"
        return out


def freshness(row: dict[str, Any], fs: FsProbe, now: float) -> dict[str, Any]:
    """Freshness of an index row from ONE stat of its ``.db`` (+ ``.db-wal``).

    ``fresh`` is ``max(indexed_at, db mtime, wal mtime) >= now - 7 days``.
    ``present`` is False when the ``.db`` itself cannot be stat'ed (the index
    is gone -> the caller treats it as unknown and allows).
    """
    db_m = fs.mtime(row.get("db") or "")
    wal = row.get("wal") or ((row.get("db") or "") + "-wal")
    wal_m = fs.mtime(wal) if wal else None
    stamps = [float(row.get("indexed_epoch") or 0)]
    if db_m is not None:
        stamps.append(db_m)
    if wal_m is not None:
        stamps.append(wal_m)
    last = max(stamps)
    age_days = (now - last) / 86400.0 if last else None
    return {
        "present": db_m is not None,
        "fresh": bool(last) and (now - last) <= STALE_SECONDS,
        "last_activity": last,
        "age_days": round(age_days, 1) if age_days is not None else None,
    }


def server_prefix(snapshot: dict[str, Any] | None, project_roots: Iterable[str | None]) -> str:
    """The ``mcp__<server>__`` prefix of the local codebase-memory server to name.

    A project-scoped ``.mcp.json`` server for the session's project (or its
    canonical checkout) wins; otherwise the user-level server; otherwise the
    default ``mcp__codebase-memory-mcp__``.
    """
    snap = snapshot if isinstance(snapshot, dict) else {}
    servers = snap.get("servers") if isinstance(snap.get("servers"), dict) else {}
    projects = servers.get("projects") if isinstance(servers.get("projects"), dict) else {}
    for r in project_roots:
        if not r:
            continue
        names = projects.get(r.lower())
        if isinstance(names, list) and names and isinstance(names[0], str):
            return f"mcp__{names[0]}__"
    user = servers.get("user")
    if isinstance(user, list) and user and isinstance(user[0], str):
        return f"mcp__{user[0]}__"
    return DEFAULT_SERVER_PREFIX


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def _sig(db_path: str) -> list[int]:
    """Stat signature: [db size, db mtime_ns, wal size, wal mtime_ns] (-1 = no wal)."""
    st = os.stat(db_path)
    try:
        wst = os.stat(db_path + "-wal")
        wal = [int(wst.st_size), int(wst.st_mtime_ns)]
    except OSError:
        wal = [-1, -1]
    return [int(st.st_size), int(st.st_mtime_ns)] + wal


def _ro_uri(db_path: str) -> str:
    return Path(os.path.abspath(db_path)).as_uri() + "?mode=ro"


def read_db_row(db_path: str, *, timeout: float = SQLITE_TIMEOUT_S) -> dict[str, Any]:
    """Read one project DB read-only: projects row, node count, covered dirs.

    Opens ``file:<db>?mode=ro`` (never ``immutable=1``) with a short busy
    timeout. Raises on any SQLite error so the caller can keep the last good row.
    """
    conn = sqlite3.connect(_ro_uri(db_path), uri=True, timeout=timeout)
    try:
        stem = os.path.basename(db_path)[: -len(".db")]
        rows = conn.execute("SELECT name, indexed_at, root_path FROM projects").fetchall()
        if not rows:
            raise ValueError("projects table is empty")
        chosen = next((r for r in rows if r[0] == stem), rows[0])
        _name, indexed_at, root = chosen
        seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='nodes'").fetchone()
        covered: list[str] | None
        files: int | None
        try:
            # A directory is covered only when it holds at least one CODE file, at
            # ANY depth -- not just its top-level ancestor: data/output dirs
            # (results/ of json+npz, paper/ of png/pdf/log) are hashed by the
            # indexer but hold nothing search_code can answer better than Grep, so
            # G1/G3 must not deny there, even when they nest under a covered
            # top-level dir (e.g. src/data/ inside an otherwise code-covered src/).
            # Every ANCESTOR of a code file's directory is recorded (including the
            # repo root as "" and the immediate parent), so a G1/G3 check against
            # the SPECIFIC target directory (not just its top segment) finds a hit
            # exactly when the index has code at or under that directory.
            cur = conn.execute("SELECT rel_path FROM file_hashes LIMIT ?", (MAX_COVERAGE_ROWS,))
            dirs: set[str] = set()
            files = 0
            for (rp,) in cur:
                files += 1
                if not isinstance(rp, str) or len(dirs) >= MAX_COVERED_DIRS:
                    continue
                p = rp.replace("\\", "/")
                base = p.rsplit("/", 1)[-1]
                ext = base.rsplit(".", 1)[-1].lower() if "." in base.lstrip(".") else ""
                if ext in CODE_EXTS:
                    parent = p.rsplit("/", 1)[0] if "/" in p else ""
                    segs = parent.split("/") if parent else []
                    for depth in range(len(segs) + 1):
                        dirs.add("/".join(segs[:depth]))
                        if len(dirs) >= MAX_COVERED_DIRS:
                            break
            covered = sorted(dirs)
        except sqlite3.Error:
            covered = None
            files = None
        return {
            "name": stem,
            "indexed_at": indexed_at if isinstance(indexed_at, str) else None,
            "root_path": root if isinstance(root, str) else None,
            "nodes": int(seq[0]) if seq and isinstance(seq[0], int) else 0,
            "covered_dirs": covered,
            "files": files,
        }
    finally:
        conn.close()


def _row_from_read(db_path: str, info: dict[str, Any], sig: list[int]) -> dict[str, Any] | None:
    root = norm_path(info.get("root_path"), None, msys=True)
    if not root:
        return None
    db = norm_path(db_path)
    if not db:
        return None
    covered = info.get("covered_dirs")
    files = info.get("files")
    return {
        "name": info["name"],
        "root": root,
        "root_key": root.lower(),
        "indexed_at": info.get("indexed_at"),
        "indexed_epoch": parse_indexed_at(info.get("indexed_at")),
        "nodes": int(info.get("nodes") or 0),
        "files": int(files) if isinstance(files, int) and not isinstance(files, bool) else None,
        "slug_match": info["name"] == slug(root),
        "covered_dirs": covered if isinstance(covered, list) else None,
        "db": db,
        "wal": db + "-wal",
        "sig": sig,
    }


def mark_partial(rows: list[dict[str, Any]]) -> None:
    """Set ``row['partial']`` on every row in place (see PARTIAL_SIBLING_RATIO)."""
    biggest: dict[str, int] = {}
    for r in rows:
        k = r.get("root_key") or ""
        biggest[k] = max(biggest.get(k, 0), int(r.get("nodes") or 0))
    for r in rows:
        nodes = int(r.get("nodes") or 0)
        files = r.get("files")
        few = isinstance(files, int) and not isinstance(files, bool) and files > 0 and nodes < files
        dwarfed = nodes < PARTIAL_TINY_NODES and nodes * PARTIAL_SIBLING_RATIO < biggest.get(r.get("root_key") or "", 0)
        r["partial"] = bool(few or dwarfed)


def _mcp_server_names(mcp_servers: Any) -> list[str]:
    """Local (stdio) codebase-memory server KEYS from an ``mcpServers`` mapping.

    Reads only keys and the ``command`` basename -- never ``env`` values.
    """
    names: list[str] = []
    if not isinstance(mcp_servers, dict):
        return names
    for key, spec in mcp_servers.items():
        if not isinstance(key, str) or not isinstance(spec, dict):
            continue
        cmd = spec.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            continue  # remote (url/http) servers use a different index store
        base = re.split(r"[\\/]", cmd.strip())[-1].lower()
        if base.endswith(".exe"):
            base = base[:-4]
        if "codebase-memory" in key.lower() or base == "codebase-memory-mcp":
            names.append(key)
    return names


def _read_json(path: str, limit: int = 64 * 1024 * 1024) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.loads(fh.read(limit))
    except (OSError, ValueError, TypeError):
        return None


def detect_servers(home: str | None, roots: Iterable[str]) -> dict[str, Any]:
    """Detect local codebase-memory MCP server keys (user-level + per project root)."""
    user: list[str] = []
    projects: dict[str, list[str]] = {}
    if home:
        cfg = _read_json(home.rstrip("/") + "/.claude.json")
        if isinstance(cfg, dict):
            user = _mcp_server_names(cfg.get("mcpServers"))
            per = cfg.get("projects")
            if isinstance(per, dict):
                for proj_path, pcfg in per.items():
                    if isinstance(pcfg, dict):
                        names = _mcp_server_names(pcfg.get("mcpServers"))
                        n = norm_path(proj_path, None, msys=True) if isinstance(proj_path, str) else None
                        if names and n:
                            projects.setdefault(n.lower(), names)
    for r in roots:
        n = norm_path(r, None, msys=True)
        if not n:
            continue
        cfg = _read_json(n.rstrip("/") + "/.mcp.json", 1024 * 1024)
        if isinstance(cfg, dict):
            names = _mcp_server_names(cfg.get("mcpServers"))
            if names:
                projects[n.lower()] = names
    return {"user": user, "projects": projects}


def _automem_dirs(home: str | None, project_dirs: Iterable[str]) -> list[str]:
    """Configured ``autoMemoryDirectory`` values (user + project settings)."""
    out: list[str] = []
    files: list[str] = []
    if home:
        files.append(home.rstrip("/") + "/.claude/settings.json")
    for p in project_dirs:
        n = norm_path(p, None, msys=True)
        if n:
            files.append(n.rstrip("/") + "/.claude/settings.json")
            files.append(n.rstrip("/") + "/.claude/settings.local.json")
    for f in files:
        cfg = _read_json(f, 4 * 1024 * 1024)
        if isinstance(cfg, dict) and isinstance(cfg.get("autoMemoryDirectory"), str):
            v = cfg["autoMemoryDirectory"]
            if v.startswith("~") and home:
                v = home.rstrip("/") + v[1:]
            n = norm_path(v, None, msys=True)
            if n and n not in out:
                out.append(n)
    return out


def _load_pins(gdir: str | None) -> dict[str, str]:
    if not gdir:
        return {}
    data = _read_json(gdir.rstrip("/") + "/pins.json", 1024 * 1024)
    pins: dict[str, str] = {}
    if isinstance(data, dict):
        for root, name in data.items():
            n = norm_path(root, None, msys=True) if isinstance(root, str) else None
            if n and isinstance(name, str) and _UUIDISH_NAME_RE.match(name):
                pins[n.lower()] = name
    return pins


def build_snapshot(
    cache_dir: str,
    previous: dict[str, Any] | None = None,
    *,
    env: dict[str, Any] | None = None,
    now: float | None = None,
    project_dirs: Iterable[str] = (),
    reader=read_db_row,
) -> dict[str, Any]:
    """Build a registry snapshot from ``cache_dir`` (see module docstring).

    ``previous`` (a prior snapshot) supplies rows to reuse when a DB's stat
    signature is unchanged, and last-good rows when a DB cannot be read.
    Raises ``OSError`` only when ``cache_dir`` itself cannot be listed.
    """
    e = upper_env(env if env is not None else dict(os.environ))
    now = time.time() if now is None else now
    prev = valid_snapshot(previous) if previous is not None else None
    prev_by_db = {r["db"].lower(): r for r in (prev or {}).get("rows", [])}
    rows: list[dict[str, Any]] = []
    stats = {"reused": 0, "read": 0, "kept_last_good": 0, "skipped": 0, "dropped_missing_root": 0}
    entries = sorted(os.scandir(cache_dir), key=lambda d: d.name)
    for ent in entries:
        name = ent.name
        if not name.endswith(".db") or name.startswith("_"):
            continue
        try:
            if not ent.is_file():
                continue
        except OSError:
            continue
        path = ent.path
        db_norm = norm_path(path)
        if not db_norm:
            continue
        prev_row = prev_by_db.get(db_norm.lower())
        try:
            sig = _sig(path)
        except OSError:
            stats["skipped"] += 1
            continue
        row: dict[str, Any] | None
        if prev_row is not None and prev_row.get("sig") == sig:
            row = dict(prev_row)
            stats["reused"] += 1
        else:
            try:
                info = reader(path)
                row = _row_from_read(path, info, sig)
                stats["read"] += 1
            except Exception:
                row = dict(prev_row) if prev_row is not None else None
                if row is not None:
                    stats["kept_last_good"] += 1
                else:
                    stats["skipped"] += 1
        if row is None:
            continue
        if not os.path.isdir(row["root"]):
            stats["dropped_missing_root"] += 1
            continue
        rows.append(row)
    mark_partial(rows)
    home = home_dir(e)
    roots = sorted({r["root"] for r in rows})
    extra = [p for p in project_dirs if p]
    return {
        "schema": SNAPSHOT_SCHEMA,
        "built_at": int(now),
        "cache_dir": norm_path(cache_dir),
        "rows": rows,
        "pins": _load_pins(guard_dir(e)),
        "servers": detect_servers(home, list(roots) + list(extra)),
        "automem_dirs": _automem_dirs(home, extra),
        "stats": stats,
    }


def write_snapshot_atomic(path: str, snapshot: dict[str, Any]) -> None:
    """Write ``snapshot`` as JSON to ``path`` via a temp file + ``os.replace``."""
    target = os.path.abspath(path)
    d = os.path.dirname(target)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_snapshot(path: str) -> dict[str, Any] | None:
    """Load and validate a snapshot file; None when missing or corrupt."""
    return valid_snapshot(_read_json(path, 64 * 1024 * 1024))


def default_snapshot_path(env: dict[str, Any] | None = None) -> str | None:
    g = guard_dir(env if env is not None else dict(os.environ))
    return g + "/snapshot.json" if g else None


def refresh(
    out_path: str | None = None,
    *,
    cache_dir: str | None = None,
    env: dict[str, Any] | None = None,
    now: float | None = None,
    project_dirs: Iterable[str] = (),
) -> tuple[int, dict[str, Any] | None]:
    """Rebuild the snapshot at ``out_path``; keep the last good one on failure.

    Returns ``(exit_code, snapshot_or_None)``. Exit code 0 = written,
    1 = kept the previous snapshot (cache unreadable / write failed),
    2 = no output path could be determined.
    """
    env_d = dict(os.environ) if env is None else env
    out = out_path or default_snapshot_path(env_d)
    if not out:
        return 2, None
    cdir = cache_dir or default_cache_dir(env_d)
    previous = _read_json(out, 64 * 1024 * 1024)
    if not cdir:
        return 1, valid_snapshot(previous)
    try:
        snap = build_snapshot(cdir, previous, env=env_d, now=now, project_dirs=project_dirs)
    except Exception:
        return 1, valid_snapshot(previous)
    try:
        write_snapshot_atomic(out, snap)
    except Exception:
        return 1, valid_snapshot(previous)
    return 0, snap


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m meridian.cbm_registry", description=__doc__.splitlines()[0])
    ap.add_argument("--refresh", action="store_true", help="rebuild the snapshot")
    ap.add_argument("--out", help="snapshot path (default: %%LOCALAPPDATA%%/meridian/guard/snapshot.json)")
    ap.add_argument("--cache-dir",
                    help="codebase-memory cache dir (default: $CBM_CACHE_DIR or ~/.cache/codebase-memory-mcp)")
    ap.add_argument("--project-dir", action="append", default=[],
                    help="extra project dir for .mcp.json / settings detection")
    ap.add_argument("--resolve", metavar="PATH", help="print the index resolution for PATH")
    args = ap.parse_args(argv)
    env = dict(os.environ)
    project_dirs = list(args.project_dir)
    if env.get("CLAUDE_PROJECT_DIR"):
        project_dirs.append(env["CLAUDE_PROJECT_DIR"])
    code = 0
    snap: dict[str, Any] | None = None
    if args.refresh:
        code, snap = refresh(args.out, cache_dir=args.cache_dir, env=env, project_dirs=project_dirs)
        if snap is not None:
            print(json.dumps({"rows": len(snap.get("rows", [])), "stats": snap.get("stats"), "exit": code}))
        else:
            print(json.dumps({"rows": 0, "exit": code}))
    if args.resolve:
        if snap is None:
            p = args.out or default_snapshot_path(env)
            snap = load_snapshot(p) if p else None
        fs = RealFS()
        res = resolve(args.resolve, snap, fs, env)
        winner = res.get("winner")
        fr = freshness(winner, fs, time.time()) if winner else None
        print(json.dumps({
            "mode": res["mode"], "project": winner["name"] if winner else None,
            "shadowed": res["shadowed"], "index_root": res["index_root"],
            "worktree_root": res["worktree_root"], "canonical_root": res["canonical_root"],
            "rel": res["rel"], "why": res["why"], "freshness": fr,
        }, indent=1))
    if not args.refresh and not args.resolve:
        ap.print_help()
    return code


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
