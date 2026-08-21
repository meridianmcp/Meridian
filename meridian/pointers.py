"""GENERIC POINTER PRIMITIVE — 2976e168.

A single, composable way to point at *a thing in a source* from a Meridian sprint
item, grounded in two established models (NOT invented fresh):

* **LSP Location / WorkspaceEdit.** A pointer's ``targets`` is an ARRAY of
  ``{uri, selector, subSelector?}`` objects — native multi-file, exactly like an
  LSP ``WorkspaceEdit`` maps one edit across many files. A ``range`` selector is
  an LSP ``Range`` (``{start_line, start_char, end_line, end_char}``).
* **W3C Web Annotation Selector composition.** A ``selector`` names *how* to point
  inside the ``uri`` (``range`` | ``symbol`` | ``node_id`` | ``zotero_key``), and
  an optional ``subSelector`` (W3C ``hasSubSelector``) nests finer granularity —
  e.g. ``symbol`` + a ``range`` subSelector = "these lines, within this function".
  A ``subSelector`` is itself a FULL selector: it carries its OWN explicit
  ``"type"`` (it does not inherit the parent's).

Shape (stored as JSON on ``sprint_item_pointers.targets`` — NOT per-domain
columns, the core requirement)::

    pointer = {
        "source_type": "code" | "docs" | "citation" | ...,
        "targets": [
            {"uri": "...", "selector": {"type": ..., ...},
             "subSelector": {"type": ..., ...}?,
             "target_kind": "existing" | "planned_new"?},
            ...
        ],
        "label": "..."?,
    }

``target_kind`` (300a063d) — distinguishes a pointer at REAL, already-present code/
docs from a pointer at a file a sprint item plans to CREATE. Without this, a
new-file item could point at a path that doesn't exist yet and pass the claim
gate's "has a pointer" check exactly like real, verified prospecting — the two
were indistinguishable. Per TARGET (not per-pointer): a single multi-file pointer
may modify an existing file AND introduce a new one in the same targets array.

* ``"existing"`` (the default when omitted, for backward compatibility with
  pointers written before this field existed) — the target is asserted to
  already be real. Verification is OPT-IN, not retroactive: the filesystem
  check below only runs when the caller EXPLICITLY writes
  ``"target_kind": "existing"`` on the target. A target that omits
  ``target_kind`` entirely is normalized to ``"existing"`` in the returned
  shape (so downstream readers always see a concrete value) but is NOT
  filesystem-checked — this keeps every pointer written before 300a063d
  (bare ``uri`` strings, no ``target_kind`` key, often placeholder paths in
  tests) validating exactly as before.
* ``"planned_new"`` — the caller is explicitly asserting the path does not
  exist YET (a planned new file). No filesystem check is performed; the
  target is exempt, not "verified" — the distinction is preserved through to
  the stored shape.

When ``target_kind`` is explicitly ``"existing"`` and the ``uri`` looks like a
local filesystem path (not a URL / ``zotero:`` / ``doc:`` / ``finding:``
reference — those have their own existence semantics, checked at RESOLVE time
by :func:`resolve_pointer`, not here), :func:`validate_pointer` verifies the
path is actually present on disk (via an injectable ``path_exists`` checker,
defaulting to :func:`os.path.exists`) and raises :class:`PointerValidationError`
if it is not. This same existence check — and its completion-time sibling in
:func:`verify_target_readiness` — go through :func:`normalize_local_uri_candidates`
(ba539706) first: ONE canonical normalization contract for ``file://`` URIs,
Windows drive letters, ``/``-vs-``\\`` separators, UNC hosts, and WSL
``/mnt/<drive>`` spellings, so a path recorded under one valid spelling isn't
reported "missing" purely because a later check ran under a different one.
See that function's docstring for the full contract.

Selector variants — every selector carries an explicit ``"type"``; the field
below is the type-specific key it also needs (443d9453):

* ``range``     — a line span: ``{"type":"range", start_line, start_char?,
                  end_line, end_char?}`` (LSP Range; pointer IS the location).
* ``symbol``    — ``{"type":"symbol", qualified_name}`` (reuses codebase-memory-
                  mcp's field; resolved via ``db.search_graph_entities``).
* ``node_id``   — ``{"type":"node_id", id}`` — an ``id`` (NOT ``value``) of a
                  ``meridian.doc_store`` element (9ee6d2ec).
* ``zotero_key``— ``{"type":"zotero_key", key}`` (resolved via
                  ``zotero_client.resolve_citation_ref``).
* ``text_quote``— ``{exact, prefix?, suffix?, archived_url?, archived_at?,
                  canonical_url?, retrieval_hash?}`` (W3C TextQuoteSelector for
                  source_type "web", 1d3f6e71; resolving it re-fetches the URL and
                  flags content drift). 06df6ab3 — the SAME selector also anchors
                  against docx paragraph text: a ``uri`` that is a local ``.docx``
                  path resolves via ``web_archive.default_web_fetcher``'s docx
                  branch instead of an HTTP GET, so one mechanism covers web AND
                  docs. 62640241 — ``canonical_url`` (the source's stable/canonical
                  address, when it differs from the fetch ``uri``, e.g. a mirror or
                  redirect) and ``retrieval_hash`` (a content hash of the fetched
                  body at capture time) are additive, optional fields completing
                  the "web target" freshness story alongside the pre-existing
                  ``archived_url``/``archived_at``.
* ``finding_id``— ``{id}`` (source_type "experiment", 1f1cd4d9; a ``save_finding``
                  artifact note resolved via ``db.get_project_note``).
* ``directory``  — ``{root, include?, exclude?, manifest_id?, snapshot_id?}``
                  (62640241) — a directory ROOT identity plus an include/exclude
                  glob selector and an optional snapshot/manifest identity.
                  Resolving it (best-effort, local-path-only by default) walks
                  ``root`` and returns a deterministic manifest (sorted relative
                  paths + a ``manifest_hash``) usable as a freshness proof.
* ``git``        — ``{repository, ref?, commit?, path?}`` (62640241) — a Git
                  REPOSITORY identity plus at least one of ``ref``/``commit``; an
                  optional ``path`` within the repo. A file/line range within
                  ``path`` is expressed via the EXISTING ``subSelector`` mechanism
                  (a nested ``{"type":"range", ...}``) — never duplicated as a new
                  field here. Resolving it (best-effort, local clones only by
                  default) shells out to ``git rev-parse`` to report the repo's
                  current HEAD and whether the requested ref/commit is reachable.
* ``remote_fs``  — ``{host_id, filesystem_slot, path, lease_id?, session_id?,
                  snapshot_id?}`` (62640241) — an opaque tunnel-connector HOST
                  identity, the filesystem SLOT it was exposed under (mirrors the
                  ``Filesystem:``-prefixed tool-slot naming in AGENTS.md), a remote
                  PATH, and an optional lease/session identity binding the pointer
                  to the connector session that captured it. No core-local default
                  resolver exists (mirrors ``verify_target_readiness``'s
                  ``provenance_getter`` seam) — resolving one requires an injected,
                  tunnel-backed resolver; without one it is reported unresolved,
                  never silently dropped.
* ``artifact``   — ``{manifest_uri, fingerprint?, run_id?, item_id?,
                  provenance_id?}`` (62640241) — a build/output ARTIFACT's manifest
                  URI, an optional file fingerprint, and an optional link to the
                  producing run/sprint-item/provenance record. Resolving it
                  (best-effort, local files only by default) hashes the manifest
                  file to report its current fingerprint.

62640241 — every target MAY also carry an optional ``freshness`` object —
``{content_hash?, source_revision?, resolver_version?, captured_at?, state?}`` —
the pointer author's proof of what the source looked like when the pointer was
captured. ``state`` is one of ``current`` | ``stale`` | ``unknown`` |
``unavailable`` | ``ambiguous`` (defaults to ``unknown`` when the object is
present but ``state`` is omitted). Purely additive/opt-in exactly like
``target_kind`` — a target with no ``freshness`` key behaves byte-for-byte as
before. :func:`resolve_pointer` RECOMPUTES a live freshness state for
``directory``/``git``/``artifact``/``remote_fs``/``text_quote`` targets by
comparing this declared proof against what resolution finds right now (see
``_freshness_state_for_target``); :func:`strict_freshness_gate` is the
fail-closed check a strict handoff consults to block on ``stale``/``unknown``/
``unavailable``/``ambiguous`` targets with an actionable reason.

This module owns:

* :func:`validate_pointer` — validates the composite shape, rejecting malformed
  input (bad selector.type, missing selector fields, malformed subSelector).
* :func:`serialize_targets` / :func:`deserialize_targets` — JSON round-trip for
  the ``targets`` column.
* :func:`resolve_pointer` — the ONE resolver, dispatching by ``selector.type``.
  Every dispatch is best-effort and guarded: an unresolvable target yields
  ``{"resolved": False, "reason": ...}``; the resolver **never raises**.

The resolver's external dependencies (code-graph search, doc_store lookup, Zotero
resolver) are all injectable so tests can stub them without touching a network or
a live Zotero.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import subprocess
from typing import Any, Awaitable, Callable
from urllib.parse import unquote, urlsplit

_log = logging.getLogger(__name__)


# The selector types the primitive dispatches on:
#   range / symbol / node_id / zotero_key — the original four.
#   text_quote  — W3C TextQuoteSelector for source_type "web" (1d3f6e71): the exact
#                 cited passage on a URL, carrying the Internet-Archive snapshot
#                 captured at citation time. Resolving it re-fetches live and flags
#                 content drift (the passage silently changed/vanished). 06df6ab3
#                 extends the SAME selector to anchor a docx paragraph (uri = a
#                 local .docx path) — one selector mechanism across code/docs/web.
#   finding_id  — addresses a save_finding artifact (a kind='finding' note) for
#                 source_type "experiment" (1f1cd4d9): a zero-ceremony run log
#                 (input / output / params / timestamp), no stages, no YAML.
_SELECTOR_TYPES = frozenset(
    {
        "range", "symbol", "node_id", "zotero_key", "text_quote", "finding_id",
        # 62640241 — typed external pointer targets: directory / git /
        # remote_fs / artifact. See the module docstring for the full shape
        # of each.
        "directory", "git", "remote_fs", "artifact",
    }
)

# 62640241 — the five universal freshness states a target's (optional)
# ``freshness`` proof, or a resolver's live re-check of it, can report.
_FRESHNESS_STATES = frozenset({"current", "stale", "unknown", "unavailable", "ambiguous"})
_DEFAULT_FRESHNESS_STATE = "unknown"

# Selector types :func:`resolve_pointer` recomputes a LIVE freshness state
# for (see ``_freshness_state_for_target``). The original six selector types
# (range/symbol/node_id/zotero_key/finding_id, plus text_quote's own
# pre-existing drift check being the exception) are left untouched — this
# module's docstring's "preserve existing behavior" guarantee.
_FRESHNESS_APPLICABLE_TYPES = frozenset({"directory", "git", "remote_fs", "artifact", "text_quote"})

# 300a063d — target_kind distinguishes a pointer at real, already-present code
# from a pointer at a planned-but-not-yet-created file. See the module
# docstring for the full rationale and the backward-compat/opt-in rules.
_TARGET_KINDS = frozenset({"existing", "planned_new"})
_DEFAULT_TARGET_KIND = "existing"

# uri prefixes that are NOT local filesystem paths — the target_kind='existing'
# filesystem check is skipped for these (they have their own existence
# semantics, checked elsewhere: zotero_key/finding_id/node_id at resolve time,
# text_quote via a live fetch). A local .docx path used by text_quote (06df6ab3)
# still looks like a plain path and IS checked, which is correct.
_NON_LOCAL_URI_PREFIXES = ("zotero:", "finding:", "doc:", "mailto:")


def _looks_like_local_path(uri: str) -> bool:
    """True when ``uri`` looks like a filesystem path rather than a URL or a
    scheme reference (``zotero:``, ``doc:``, ``finding:``, ``http(s)://``, …).

    The ``target_kind='existing'`` filesystem check only makes sense for local
    paths; other uri schemes address things that don't live on this machine's
    disk, or that are already verified by their own resolver.

    ``file://`` is the one exception to the generic ``"://" -> not local``
    rule below (ba539706): a ``file://`` URI IS a local filesystem reference,
    just spelled as a URI — treating it as non-local would silently SKIP the
    existence check entirely (see :func:`verify_target_readiness`'s
    ``"skipped"`` status) instead of actually verifying it via
    :func:`normalize_local_uri_candidates`.
    """
    if not uri:
        return False
    if uri.lower().startswith("file://"):
        return True
    if "://" in uri:
        return False
    return not uri.startswith(_NON_LOCAL_URI_PREFIXES)


# ---------------------------------------------------------------------------
# ba539706 — ONE canonical URI/path normalization contract, shared by the
# write-time ``target_kind='existing'`` check (:func:`_validate_target`) and
# the completion-time fail-closed readiness gate
# (:func:`verify_target_readiness`), so the two can never independently
# drift on what counts as "the same local file".
#
# Bug this fixes: a pointer's ``uri`` can be recorded in one path spelling
# (e.g. by a tunnel-connected agent running under WSL, or as a ``file://``
# URI, or with forward slashes) and later checked for existence by a
# DIFFERENT process on a DIFFERENT OS/shell — a plain ``os.path.exists(uri)``
# on the literal stored string then reports "missing" even though the SAME
# file is right there under a different valid spelling, and even though
# meridian-docs (which resolves paths more permissively before opening them)
# can open it fine. This is purely about WHICH SPELLING of an already-local
# uri is accepted; it never widens WHAT counts as present, and non-local
# uris (http(s)://, zotero:, doc:, finding:, mailto:) are never touched —
# fail-closed for genuinely unavailable remote targets is unchanged.
# ---------------------------------------------------------------------------

_WINDOWS_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")
_WSL_MOUNT_RE = re.compile(r"^/mnt/([A-Za-z])(?:/(.*))?$")


def _strip_file_uri(uri: str) -> "str | None":
    """Convert a ``file://`` URI to a bare local filesystem path spelling.

    Returns ``None`` when *uri* is not a ``file://`` URI (case-insensitive
    scheme match). Handles the three shapes a ``file://`` uri can take:

    * POSIX:    ``file:///home/alice/doc.docx``     -> ``/home/alice/doc.docx``
    * Windows:  ``file:///C:/Users/alice/doc.docx``  -> ``C:/Users/alice/doc.docx``
      (the leading ``/`` before a drive letter is a URI-path artifact, not
      part of the Windows path — stripped here)
    * UNC host: ``file://server/share/doc.docx``    -> ``\\\\server\\share\\doc.docx``
      (a non-empty, non-``localhost`` netloc names a UNC host/share per the
      ``file://host/path`` form of the URI)

    Percent-encoded characters (``%20`` etc.) are decoded. Never raises.
    """
    if not uri or not uri.lower().startswith("file://"):
        return None
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return None
    path = unquote(parsed.path)
    netloc = parsed.netloc
    if netloc and netloc.lower() != "localhost":
        return f"\\\\{netloc}{path.replace('/', chr(92))}"
    if len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path or "/"


def _windows_path_to_wsl(path: str) -> "str | None":
    """``C:\\Users\\alice`` / ``C:/Users/alice`` -> ``/mnt/c/Users/alice``
    (the WSL spelling of a Windows drive). ``None`` when *path* doesn't start
    with a drive letter."""
    m = _WINDOWS_DRIVE_RE.match(path)
    if not m:
        return None
    drive = m.group(1).lower()
    rest = m.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"


def _wsl_path_to_windows(path: str) -> "str | None":
    """``/mnt/c/Users/alice`` -> ``C:\\Users\\alice`` (the inverse of
    :func:`_windows_path_to_wsl`). ``None`` when *path* isn't a WSL
    ``/mnt/<drive>`` mount spelling."""
    m = _WSL_MOUNT_RE.match(path)
    if not m:
        return None
    drive = m.group(1).upper()
    rest = (m.group(2) or "").replace("/", "\\")
    return f"{drive}:\\{rest}" if rest else f"{drive}:\\"


def normalize_local_uri_candidates(uri: str) -> list[str]:
    """Return ordered, deduplicated candidate local-filesystem path spellings
    for *uri* — the canonical normalization contract described above.

    Pure string transforms only — no filesystem I/O, never raises. Tries, in
    order: the uri as given; its ``file://``-stripped form (if any); that
    same path with separators normalized both ways (``/`` <-> ``\\``); and,
    for each of those, the Windows-drive <-> WSL ``/mnt/<drive>`` conversion
    in whichever direction applies. A relative path (no drive letter, no
    ``/mnt/`` prefix) or a uri that matches none of these shapes degrades to
    just ``[uri]`` — no normalization is invented for it, matching prior
    behavior exactly.
    """
    if not uri:
        return []
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(cand: "str | None") -> None:
        if cand and cand not in seen:
            seen.add(cand)
            candidates.append(cand)

    raw = uri.strip()
    _add(raw)

    file_path = _strip_file_uri(raw)
    _add(file_path)
    base = file_path if file_path is not None else raw

    _add(base)
    _add(base.replace("/", "\\"))
    _add(base.replace("\\", "/"))

    for spelling in (base, base.replace("/", "\\"), base.replace("\\", "/")):
        _add(_windows_path_to_wsl(spelling))
        _add(_wsl_path_to_windows(spelling))

    return candidates


def _resolve_local_existence(
    uri: str, exists_checker: Callable[[str], bool]
) -> "tuple[bool, str]":
    """Try *uri* and its normalized candidate spellings (see
    :func:`normalize_local_uri_candidates`) against *exists_checker*, in
    order; return ``(found, path_used)``.

    ``path_used`` is *uri* itself when nothing matched — a caller reporting
    "missing" reports it against the ORIGINAL uri, never a guessed variant.
    Fail-closed: this only widens which SPELLING of *uri* is accepted, never
    what counts as present — a genuinely absent file still fails on every
    candidate. Never raises: a checker that raises ``OSError`` on one
    candidate (e.g. an unrepresentable path on this OS) is treated as "not
    found for that candidate" and the next one is tried.
    """
    for candidate in normalize_local_uri_candidates(uri):
        try:
            if exists_checker(candidate):
                return True, candidate
        except OSError:
            continue
    return False, uri


# Integer fields an LSP Range carries. start_line/end_line are required; the char
# offsets are optional (a whole-line span is valid) but must be ints when present.
_RANGE_REQUIRED_INTS = ("start_line", "end_line")
_RANGE_OPTIONAL_INTS = ("start_char", "end_char")


class PointerValidationError(ValueError):
    """Raised by :func:`validate_pointer` when a pointer's shape is malformed."""


# ---------------------------------------------------------------------------
# Validation (pure — no DB, unit-testable)
# ---------------------------------------------------------------------------

# Common WRONG keys a caller reaches for instead of the real string field, mapped
# to the correct field name — so the validation error can say "you wrote 'value',
# use 'id'" instead of a bare "requires a non-empty id". These are the mistakes the
# tool schema was underspecified about (443d9453): a node_id selector needs 'id'
# (not the generic 'value'), etc.
_WRONG_KEY_ALIASES = ("value", "val", "node_id", "nodeId")


def _require_str_field(
    selector: dict[str, Any], field: str, *, what: str, stype: str
) -> str:
    """Require a non-empty string ``field`` on ``selector``; return it stripped.

    Raises :class:`PointerValidationError` with a MESSAGE THAT NAMES THE FIELD (and,
    when the caller used a common wrong key like ``value`` instead of ``id``, points
    at the actual key they should have used) rather than silently mis-parsing.
    """
    val = selector.get(field)
    if isinstance(val, str) and val.strip():
        return val.strip()
    # Point at the likely typo: a present wrong-key that should have been `field`.
    for alias in _WRONG_KEY_ALIASES:
        if alias != field and alias in selector and selector.get(alias) is not None:
            raise PointerValidationError(
                f"{what} {stype} requires a non-empty {field!r} "
                f"(found {alias!r} — rename it to {field!r})"
            )
    raise PointerValidationError(
        f"{what} {stype} requires a non-empty {field!r}"
    )


def _validate_selector(selector: Any, *, is_sub: bool = False) -> dict[str, Any]:
    """Validate one selector (or subSelector) dict; return a normalized copy.

    A selector is ``{"type": <one of _SELECTOR_TYPES>, ...type-specific fields}``.
    ``is_sub`` only affects error wording (a subSelector is itself a full
    selector — W3C hasSubSelector — validated by the SAME rules, recursively, so
    a subSelector may itself carry a subSelector).
    """
    what = "subSelector" if is_sub else "selector"
    if not isinstance(selector, dict):
        raise PointerValidationError(f"{what} must be an object")
    stype = selector.get("type")
    if stype not in _SELECTOR_TYPES:
        # A subSelector is itself a FULL selector (W3C hasSubSelector) — it must
        # carry its OWN explicit "type"; it does not inherit the parent's. Missing
        # type is the common mistake (443d9453), so say so explicitly instead of
        # silently mis-parsing.
        hint = (
            " (each subSelector must carry its own explicit 'type')"
            if is_sub and stype is None
            else ""
        )
        raise PointerValidationError(
            f"{what}.type must be one of {sorted(_SELECTOR_TYPES)}, "
            f"got {stype!r}{hint}"
        )
    out: dict[str, Any] = {"type": stype}

    if stype == "range":
        for field in _RANGE_REQUIRED_INTS:
            val = selector.get(field)
            if not isinstance(val, int) or isinstance(val, bool):
                raise PointerValidationError(
                    f"{what} range requires integer {field!r}"
                )
            out[field] = val
        for field in _RANGE_OPTIONAL_INTS:
            if field in selector and selector[field] is not None:
                val = selector[field]
                if not isinstance(val, int) or isinstance(val, bool):
                    raise PointerValidationError(
                        f"{what} range {field!r} must be an integer"
                    )
                out[field] = val
    elif stype == "symbol":
        out["qualified_name"] = _require_str_field(
            selector, "qualified_name", what=what, stype="symbol"
        )
    elif stype == "node_id":
        out["id"] = _require_str_field(
            selector, "id", what=what, stype="node_id"
        )
    elif stype == "zotero_key":
        key = selector.get("key")
        if not isinstance(key, str) or not key.strip():
            raise PointerValidationError(
                f"{what} zotero_key requires a non-empty key"
            )
        out["key"] = key.strip()
    elif stype == "text_quote":
        # 1d3f6e71 — W3C TextQuoteSelector: the exact cited passage, optionally
        # bracketed by prefix/suffix for disambiguation. `exact` is stored VERBATIM
        # (not stripped) — leading/trailing whitespace is part of an exact match.
        # archived_url / archived_at carry the Internet-Archive snapshot captured at
        # citation time (set by add_sprint_item_pointer for source_type "web").
        exact = selector.get("exact")
        if not isinstance(exact, str) or not exact.strip():
            raise PointerValidationError(
                f"{what} text_quote requires a non-empty exact"
            )
        out["exact"] = exact
        for opt in (
            "prefix", "suffix", "archived_url", "archived_at",
            # 62640241 — additive "web target" freshness fields.
            "canonical_url", "retrieval_hash",
        ):
            if opt in selector and selector[opt] is not None:
                val = selector[opt]
                if not isinstance(val, str):
                    raise PointerValidationError(
                        f"{what} text_quote {opt!r} must be a string"
                    )
                out[opt] = val
    elif stype == "finding_id":
        # 1f1cd4d9 — addresses a save_finding artifact (a kind='finding' note) by id.
        out["id"] = _require_str_field(
            selector, "id", what=what, stype="finding_id"
        )
    elif stype == "directory":
        # 62640241 — a directory ROOT identity + include/exclude glob selector
        # + optional snapshot/manifest identity. See module docstring.
        out["root"] = _require_str_field(selector, "root", what=what, stype="directory")
        for field in ("include", "exclude"):
            if field in selector and selector[field] is not None:
                val = selector[field]
                if not isinstance(val, list) or not val or not all(
                    isinstance(v, str) and v.strip() for v in val
                ):
                    raise PointerValidationError(
                        f"{what} directory {field!r} must be a non-empty list of "
                        "non-empty strings (glob patterns)"
                    )
                out[field] = [v.strip() for v in val]
        for field in ("manifest_id", "snapshot_id"):
            if field in selector and selector[field] is not None:
                val = selector[field]
                if not isinstance(val, str) or not val.strip():
                    raise PointerValidationError(
                        f"{what} directory {field!r} must be a non-empty string"
                    )
                out[field] = val.strip()
    elif stype == "git":
        # 62640241 — a Git REPOSITORY identity + ref/commit (at least one
        # required) + optional path within the repo. A line range within
        # that path reuses the EXISTING subSelector mechanism, never a new
        # field here.
        out["repository"] = _require_str_field(
            selector, "repository", what=what, stype="git"
        )
        ref, commit = selector.get("ref"), selector.get("commit")
        has_ref = isinstance(ref, str) and ref.strip()
        has_commit = isinstance(commit, str) and commit.strip()
        if ref is not None and not has_ref:
            raise PointerValidationError(f"{what} git 'ref' must be a non-empty string")
        if commit is not None and not has_commit:
            raise PointerValidationError(f"{what} git 'commit' must be a non-empty string")
        if not has_ref and not has_commit:
            raise PointerValidationError(
                f"{what} git requires at least one of a non-empty 'ref' or 'commit'"
            )
        if has_ref:
            out["ref"] = ref.strip()
        if has_commit:
            out["commit"] = commit.strip()
        if "path" in selector and selector["path"] is not None:
            path = selector["path"]
            if not isinstance(path, str) or not path.strip():
                raise PointerValidationError(f"{what} git 'path' must be a non-empty string")
            out["path"] = path.strip()
    elif stype == "remote_fs":
        # 62640241 — an opaque tunnel-connector HOST + filesystem SLOT + remote
        # PATH, plus an optional lease/session identity binding the pointer to
        # the connector session that captured it.
        out["host_id"] = _require_str_field(selector, "host_id", what=what, stype="remote_fs")
        out["filesystem_slot"] = _require_str_field(
            selector, "filesystem_slot", what=what, stype="remote_fs"
        )
        out["path"] = _require_str_field(selector, "path", what=what, stype="remote_fs")
        for field in ("lease_id", "session_id", "snapshot_id"):
            if field in selector and selector[field] is not None:
                val = selector[field]
                if not isinstance(val, str) or not val.strip():
                    raise PointerValidationError(
                        f"{what} remote_fs {field!r} must be a non-empty string"
                    )
                out[field] = val.strip()
    elif stype == "artifact":
        # 62640241 — a build/output ARTIFACT's manifest URI + optional
        # fingerprint + optional link to the producing run/item/provenance
        # record (any subset — at least the manifest_uri is required).
        out["manifest_uri"] = _require_str_field(
            selector, "manifest_uri", what=what, stype="artifact"
        )
        for field in ("fingerprint", "run_id", "item_id", "provenance_id"):
            if field in selector and selector[field] is not None:
                val = selector[field]
                if not isinstance(val, str) or not val.strip():
                    raise PointerValidationError(
                        f"{what} artifact {field!r} must be a non-empty string"
                    )
                out[field] = val.strip()

    # W3C hasSubSelector — optional, recursive, validated by the same rules.
    sub = selector.get("subSelector")
    if sub is not None:
        out["subSelector"] = _validate_selector(sub, is_sub=True)
    return out


_FRESHNESS_STR_FIELDS = ("content_hash", "source_revision", "resolver_version", "captured_at")


def _validate_freshness(freshness: Any) -> dict[str, Any]:
    """Validate one target's optional ``freshness`` proof object; return a
    normalized copy. See the module docstring for the field shape.

    Every string field is optional (a caller may supply only the ones it
    actually has evidence for — e.g. just ``captured_at`` with no hash yet).
    ``state`` defaults to :data:`_DEFAULT_FRESHNESS_STATE` ("unknown") when
    the object is present but omits it — presence of a ``freshness`` object
    with no explicit state is itself meaningful ("I know I captured this,
    I just don't have a computed state for it"), distinct from omitting the
    whole object (no freshness claim at all).
    """
    if not isinstance(freshness, dict):
        raise PointerValidationError("freshness must be an object")
    out: dict[str, Any] = {}
    for field in _FRESHNESS_STR_FIELDS:
        if field in freshness and freshness[field] is not None:
            val = freshness[field]
            if not isinstance(val, str) or not val.strip():
                raise PointerValidationError(
                    f"freshness {field!r} must be a non-empty string"
                )
            out[field] = val.strip()
    state = freshness.get("state")
    if state is not None:
        if state not in _FRESHNESS_STATES:
            raise PointerValidationError(
                f"freshness state must be one of {sorted(_FRESHNESS_STATES)}, got {state!r}"
            )
        out["state"] = state
    else:
        out["state"] = _DEFAULT_FRESHNESS_STATE
    return out


def _validate_target(
    target: Any, *, path_exists: Callable[[str], bool] | None = None
) -> dict[str, Any]:
    """Validate one ``{uri, selector, subSelector?, target_kind?}`` target; return
    a normalized copy.

    A ``subSelector`` at the TARGET level (a peer of ``selector``) is accepted as
    an alias for nesting it under the selector — some callers put it there per the
    W3C shape. Either placement is normalized into ``selector.subSelector``.

    ``target_kind`` (300a063d) is ``"existing"`` | ``"planned_new"``, defaulting
    to ``"existing"`` when omitted (backward compat — see module docstring).
    Verification is opt-in: the on-disk existence check only runs when the
    caller EXPLICITLY wrote ``target_kind: "existing"`` on this target, so
    pointers written before this field existed (no key at all) are never
    retroactively checked. ``planned_new`` never triggers a check.
    """
    if not isinstance(target, dict):
        raise PointerValidationError("each target must be an object")
    uri = target.get("uri")
    if not isinstance(uri, str) or not uri.strip():
        raise PointerValidationError("each target requires a non-empty uri")
    uri = uri.strip()
    if "selector" not in target:
        raise PointerValidationError("each target requires a selector")
    selector_src = dict(target["selector"]) if isinstance(target["selector"], dict) else target["selector"]
    # A target-level subSelector is folded into the selector (unless the selector
    # already carries its own, which wins).
    if isinstance(selector_src, dict) and target.get("subSelector") is not None:
        selector_src.setdefault("subSelector", target["subSelector"])
    selector = _validate_selector(selector_src)

    # 300a063d — target_kind: existing (default, backward-compat) | planned_new.
    kind_explicit = "target_kind" in target
    raw_kind = target.get("target_kind")
    if kind_explicit:
        if raw_kind not in _TARGET_KINDS:
            raise PointerValidationError(
                f"target_kind must be one of {sorted(_TARGET_KINDS)}, got {raw_kind!r}"
            )
        kind = raw_kind
    else:
        kind = _DEFAULT_TARGET_KIND

    if kind_explicit and kind == "existing" and _looks_like_local_path(uri):
        checker = path_exists or os.path.exists
        # ba539706 — try uri under every normalized candidate spelling
        # (file:// stripped, separators flipped, Windows-drive <-> WSL
        # /mnt/<drive>) before concluding the target is genuinely missing.
        exists, _matched_path = _resolve_local_existence(uri, checker)
        if not exists:
            raise PointerValidationError(
                f"target_kind='existing' but no file exists at uri {uri!r} "
                "(use target_kind='planned_new' for a file that does not exist yet)"
            )

    out_target: dict[str, Any] = {"uri": uri, "selector": selector, "target_kind": kind}
    # 62640241 — optional, additive universal freshness proof. Omitted
    # entirely when the caller didn't supply one — matches target_kind's own
    # backward-compat contract of never inventing a value that wasn't there.
    if "freshness" in target and target["freshness"] is not None:
        out_target["freshness"] = _validate_freshness(target["freshness"])
    return out_target


def validate_pointer(
    pointer: Any, *, path_exists: Callable[[str], bool] | None = None
) -> dict[str, Any]:
    """Validate a full pointer and return a normalized copy.

    Enforces ``{source_type: non-empty str, targets: non-empty list of
    {uri, selector, subSelector?, target_kind?}, label?: str}``. Raises
    :class:`PointerValidationError` on any malformed field. The returned dict is a
    fresh, normalized structure safe to serialize.

    ``path_exists`` (300a063d) is an injectable ``uri -> bool`` filesystem
    checker (defaults to :func:`os.path.exists`), used ONLY for targets that
    explicitly declare ``target_kind: "existing"`` on a local-path-looking uri
    — see :func:`_validate_target` and the module docstring. Tests inject a
    stub so the check never touches the real filesystem.
    """
    if not isinstance(pointer, dict):
        raise PointerValidationError("pointer must be an object")
    source_type = pointer.get("source_type")
    if not isinstance(source_type, str) or not source_type.strip():
        raise PointerValidationError("pointer requires a non-empty source_type")
    targets = pointer.get("targets")
    if not isinstance(targets, list) or not targets:
        raise PointerValidationError(
            "pointer requires a non-empty targets array"
        )
    normalized_targets = [
        _validate_target(t, path_exists=path_exists) for t in targets
    ]
    out: dict[str, Any] = {
        "source_type": source_type.strip(),
        "targets": normalized_targets,
    }
    label = pointer.get("label")
    if label is not None:
        if not isinstance(label, str):
            raise PointerValidationError("label must be a string when provided")
        out["label"] = label
    return out


# ---------------------------------------------------------------------------
# Serialize / deserialize the JSON ``targets`` column
# ---------------------------------------------------------------------------

def serialize_targets(targets: list[dict[str, Any]]) -> str:
    """Serialize a validated ``targets`` list to the JSON stored in the column."""
    return json.dumps(targets, ensure_ascii=False, sort_keys=True)


def deserialize_targets(raw: Any) -> list[dict[str, Any]]:
    """Deserialize the JSON ``targets`` column back into a list.

    Tolerant: a None/blank column, or an already-decoded list, or malformed JSON
    all degrade to ``[]`` rather than raising — a stored pointer must always be
    readable even if its column was somehow corrupted.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str):
        return []
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return decoded if isinstance(decoded, list) else []


def row_to_pointer(row: dict[str, Any]) -> dict[str, Any]:
    """Materialize a ``sprint_item_pointers`` DB row into a pointer dict.

    Deserializes the JSON ``targets`` column and echoes the id / sprint_item_id /
    source_type / label / created_at, so callers get the full pointer shape plus
    its persistence metadata.
    """
    return {
        "id": row.get("id"),
        "project_id": row.get("project_id"),
        "sprint_item_id": row.get("sprint_item_id"),
        "source_type": row.get("source_type"),
        "targets": deserialize_targets(row.get("targets")),
        "label": row.get("label"),
        "created_at": row.get("created_at"),
    }


# ---------------------------------------------------------------------------
# The ONE resolver — dispatch by selector.type. Best-effort; NEVER raises.
# ---------------------------------------------------------------------------

# Injectable resolver seams (tests stub these; production wires the real ones).
SymbolResolver = Callable[..., Awaitable[list[dict[str, Any]]]]
NodeResolver = Callable[..., Awaitable[dict[str, Any] | None]]
CitationResolver = Callable[..., Awaitable[dict[str, Any] | None]]
WebFetcher = Callable[..., Awaitable[str | None]]
FindingResolver = Callable[..., Awaitable[dict[str, Any] | None]]
# 62640241 — resolver seams for the four new typed target contracts. Each is
# ``async (selector: dict) -> dict|None``, matching the shape of the seams
# above: the FULL normalized selector goes in, an implementation-defined
# "what did resolution find" dict (or ``None`` for "not found") comes out.
# Guarded by their dispatch wrapper exactly like every other selector type —
# a resolver that raises degrades to an unresolved result, never propagates.
DirectoryResolver = Callable[..., Awaitable[dict[str, Any] | None]]
GitResolver = Callable[..., Awaitable[dict[str, Any] | None]]
RemoteFsResolver = Callable[..., Awaitable[dict[str, Any] | None]]
ArtifactResolver = Callable[..., Awaitable[dict[str, Any] | None]]

# ---------------------------------------------------------------------------
# e9d72d17 — pluggable reference-manager backend registry.
#
# The citation resolver was an injectable seam (tests could stub it) but, for real
# use, hardcoded to Zotero — there was no way to SELECT a different backend. This
# registry makes the reference-manager backend genuinely configurable: register a
# resolver factory per backend name; the configured backend (explicit arg, else the
# MERIDIAN_CITATION_BACKEND env var, else the default) picks which resolver
# resolve_pointer uses for zotero_key/citation targets. Zotero ships registered;
# Mendeley/EndNote/etc. register their own factory when their client is built — no
# edit to resolve_pointer needed. The seam shape mirrors symbol_resolver/
# node_resolver, so this is the same swappable-resolver pattern, extended to a
# selectable *named* backend.
# ---------------------------------------------------------------------------
CitationResolverFactory = Callable[[], CitationResolver]

DEFAULT_CITATION_BACKEND = "zotero"
_CITATION_BACKENDS: dict[str, CitationResolverFactory] = {}


def register_citation_backend(name: str, factory: CitationResolverFactory) -> None:
    """Register (or replace) a reference-manager backend by name (case-insensitive).

    ``factory`` is a zero-arg callable returning a :data:`CitationResolver`
    (``async (ref: str) -> item|None``). Registering is cheap + idempotent; the
    factory is only invoked when that backend is actually selected.
    """
    key = (name or "").strip().lower()
    if not key:
        raise ValueError("citation backend name must be non-empty")
    _CITATION_BACKENDS[key] = factory


def available_citation_backends() -> list[str]:
    """Sorted names of the registered reference-manager backends (for a UI picker)."""
    return sorted(_CITATION_BACKENDS)


def resolve_citation_backend(name: str | None = None) -> CitationResolver:
    """Return the citation resolver for backend ``name``.

    Selection order: explicit ``name`` → ``MERIDIAN_CITATION_BACKEND`` env var →
    :data:`DEFAULT_CITATION_BACKEND`. An unknown/absent backend falls back to the
    default (zotero) so citation resolution never hard-fails on misconfiguration.
    """
    key = (name or "").strip().lower()
    if not key:
        import os  # noqa: PLC0415
        key = (os.environ.get("MERIDIAN_CITATION_BACKEND") or "").strip().lower()
    factory = _CITATION_BACKENDS.get(key) or _CITATION_BACKENDS.get(DEFAULT_CITATION_BACKEND)
    if factory is None:  # pragma: no cover — default is always registered at import
        raise LookupError("no citation backend registered")
    return factory()


def _zotero_citation_backend() -> CitationResolver:
    """Default backend: Zotero, via zotero_client.resolve_citation_ref (lazy import
    to avoid an import cycle / keep zotero optional)."""
    from .zotero_client import resolve_citation_ref as _rc  # noqa: PLC0415

    async def _resolver(ref: str) -> dict[str, Any] | None:
        return await _rc(ref)

    return _resolver


register_citation_backend(DEFAULT_CITATION_BACKEND, _zotero_citation_backend)


def _unresolved(reason: str, **extra: Any) -> dict[str, Any]:
    """Build a guarded unresolved result: ``{resolved: False, reason, ...}``."""
    return {"resolved": False, "reason": reason, **extra}


async def _resolve_range(selector: dict[str, Any], uri: str) -> dict[str, Any]:
    """``range`` — the pointer IS the location. Return ``{uri, range}`` as-is."""
    rng = {k: selector[k] for k in ("start_line", "start_char", "end_line", "end_char")
           if k in selector}
    return {"resolved": True, "selector_type": "range", "uri": uri, "range": rng}


async def _resolve_symbol(
    db: Any,
    project_id: str,
    selector: dict[str, Any],
    uri: str,
    symbol_resolver: SymbolResolver,
) -> dict[str, Any]:
    """``symbol`` — best-match the qualified_name in the cached code graph."""
    qn = selector.get("qualified_name")
    try:
        matches = await symbol_resolver(db, project_id, qn, 5)
    except Exception:  # noqa: BLE001 — a search failure is just "unresolvable"
        _log.debug("symbol resolve failed for %r", qn, exc_info=True)
        return _unresolved("symbol lookup failed", selector_type="symbol",
                           uri=uri, qualified_name=qn)
    if not matches:
        return _unresolved("no matching symbol in graph snapshot",
                           selector_type="symbol", uri=uri, qualified_name=qn)
    # Prefer an exact qualified_name match; else the first (best-ranked) row.
    best = None
    for m in matches:
        if isinstance(m, dict) and str(m.get("qualified_name") or "") == str(qn):
            best = m
            break
    best = best or matches[0]
    out: dict[str, Any] = {
        "resolved": True,
        "selector_type": "symbol",
        "uri": uri,
        "qualified_name": qn,
        "file": (best.get("file") if isinstance(best, dict) else None),
        "match": best,
    }
    # eb8b6894 — surface WHICH resolution path produced this match: "live"
    # tunnel-connected graph search (build_symbol_resolver's prospect_symbol_impl
    # rung) vs. a fallback (local semantic search, or the cached-snapshot
    # search_graph_entities table — production-empty, see
    # test_mcp_resolve_pointers_reaches_live_graph_via_tenant). Reuses the
    # EXISTING distinction (prospect_symbol_impl's own "rung" field / the
    # snapshot-fallback tag applied above and in build_symbol_resolver) rather
    # than reinventing one. Always set (never omitted) so a symbol match is
    # never mistaken for a selector type this concept doesn't apply to;
    # "unknown" is the honest default when the symbol_resolver in use doesn't
    # self-report (a bare caller-supplied test stub, for instance) — NEVER
    # presumed to be "live_graph".
    out["resolution_source"] = (
        (best.get("resolution_source") if isinstance(best, dict) else None) or "unknown"
    )
    return out


async def _resolve_node_id(
    selector: dict[str, Any],
    uri: str,
    node_resolver: NodeResolver | None,
) -> dict[str, Any]:
    """``node_id`` — look an element up in the doc_store by its id (9ee6d2ec)."""
    nid = selector.get("id")
    if node_resolver is None:
        return _unresolved("no doc_store available", selector_type="node_id",
                           uri=uri, id=nid)
    try:
        found = await node_resolver(nid)
    except Exception:  # noqa: BLE001 — store lookup failure → unresolvable
        _log.debug("node_id resolve failed for %r", nid, exc_info=True)
        return _unresolved("doc_store lookup failed", selector_type="node_id",
                           uri=uri, id=nid)
    if not found:
        return _unresolved("no element with that id", selector_type="node_id",
                           uri=uri, id=nid)
    return {
        "resolved": True,
        "selector_type": "node_id",
        "uri": uri,
        "id": nid,
        "element": (found.get("element") if isinstance(found, dict) else found),
        "document": (found.get("document") if isinstance(found, dict) else None),
    }


async def _resolve_zotero_key(
    selector: dict[str, Any],
    uri: str,
    citation_resolver: CitationResolver,
) -> dict[str, Any]:
    """``zotero_key`` — resolve via ``resolve_citation_ref('zotero:'+key)``."""
    key = selector.get("key")
    try:
        item = await citation_resolver(f"zotero:{key}")
    except Exception:  # noqa: BLE001 — the citation resolver never raises, but be safe
        _log.debug("zotero resolve failed for %r", key, exc_info=True)
        return _unresolved("zotero resolve failed", selector_type="zotero_key",
                           uri=uri, key=key)
    if not isinstance(item, dict) or not item.get("zotero_key"):
        return _unresolved("zotero item not found (or Zotero unavailable)",
                           selector_type="zotero_key", uri=uri, key=key)
    return {
        "resolved": True,
        "selector_type": "zotero_key",
        "uri": uri,
        "key": key,
        "item": item,
    }


async def _resolve_text_quote(
    selector: dict[str, Any],
    uri: str,
    web_fetcher: WebFetcher | None,
) -> dict[str, Any]:
    """``text_quote`` — is the exact cited passage still present at ``uri``? (1d3f6e71)

    Fetches the live page (injectable ``web_fetcher``) and checks whether ``exact``
    (bracketed by ``prefix``/``suffix`` when given) is still present. A missing
    passage is a RESOLVED result with ``drift: True`` — the URL's content changed
    since it was cited — not an error. Echoes the ``archived_url`` snapshot captured
    at citation time so a caller can fall back to the archived copy.
    """
    exact = selector.get("exact") or ""
    base: dict[str, Any] = {"selector_type": "text_quote", "uri": uri, "exact": exact}
    if selector.get("archived_url"):
        base["archived_url"] = selector["archived_url"]
    if web_fetcher is None:
        return _unresolved("no web fetcher available", **base)
    try:
        text = await web_fetcher(uri)
    except Exception:  # noqa: BLE001 — a fetch failure is just "unresolvable"
        _log.debug("web fetch failed for %r", uri, exc_info=True)
        return _unresolved("web fetch failed", **base)
    if text is None:
        return _unresolved("web fetch returned nothing", **base)
    prefix = selector.get("prefix") or ""
    suffix = selector.get("suffix") or ""
    needle = f"{prefix}{exact}{suffix}" if (prefix or suffix) else exact
    present = needle in text
    return {"resolved": True, **base, "found": present, "drift": not present}


async def _resolve_finding_id(
    selector: dict[str, Any],
    uri: str,
    finding_resolver: FindingResolver | None,
) -> dict[str, Any]:
    """``finding_id`` — resolve a save_finding artifact (a note) by id (1f1cd4d9)."""
    fid = selector.get("id")
    if finding_resolver is None:
        return _unresolved("no finding resolver available",
                           selector_type="finding_id", uri=uri, id=fid)
    try:
        found = await finding_resolver(fid)
    except Exception:  # noqa: BLE001 — lookup failure → unresolvable
        _log.debug("finding_id resolve failed for %r", fid, exc_info=True)
        return _unresolved("finding lookup failed",
                           selector_type="finding_id", uri=uri, id=fid)
    if not found:
        return _unresolved("no finding artifact with that id",
                           selector_type="finding_id", uri=uri, id=fid)
    return {
        "resolved": True, "selector_type": "finding_id", "uri": uri,
        "id": fid, "artifact": found,
    }


# ---------------------------------------------------------------------------
# 62640241 — resolvers for the four new typed target contracts. Each default
# is core-local and LOCAL-FILESYSTEM-ONLY (no network, no tunnel) — a real,
# working implementation for the common self-hosted case, not a stub. A
# richer caller (a tunnel-backed remote_fs resolver, a network-aware git
# resolver for a remote repository URL) injects its own; there is
# deliberately NO core-local default for remote_fs (mirrors
# ``verify_target_readiness``'s ``provenance_getter`` seam — the tunnel
# infrastructure it needs lives outside this module and is not importable
# here without an import cycle / a hard runtime dependency this module must
# not take on).
# ---------------------------------------------------------------------------


def _default_directory_resolver() -> DirectoryResolver:
    """Core-local default: verify ``root`` is a local directory and build a
    deterministic manifest (sorted relative paths, filtered by ``include``/
    ``exclude`` glob patterns, plus a ``manifest_hash`` over that list) — no
    network. ``None`` when ``root`` is missing, not a local path, or not a
    directory; every filesystem error is guarded (never raises)."""

    async def _resolver(selector: dict[str, Any]) -> dict[str, Any] | None:
        root = selector.get("root") or ""
        if not _looks_like_local_path(root):
            return None
        exists, matched = _resolve_local_existence(root, os.path.isdir)
        if not exists:
            return None
        include = selector.get("include") or ["*"]
        exclude = selector.get("exclude") or []
        entries: list[str] = []
        try:
            for dirpath, _dirnames, filenames in os.walk(matched):
                for fname in filenames:
                    rel = os.path.relpath(os.path.join(dirpath, fname), matched)
                    rel_posix = rel.replace(os.sep, "/")
                    if not any(fnmatch.fnmatch(rel_posix, pat) for pat in include):
                        continue
                    if any(fnmatch.fnmatch(rel_posix, pat) for pat in exclude):
                        continue
                    entries.append(rel_posix)
        except OSError:
            return None
        entries.sort()
        manifest_hash = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
        _cap = 500
        return {
            "root": matched,
            "entry_count": len(entries),
            "entries": entries[:_cap],
            "truncated": len(entries) > _cap,
            "manifest_hash": manifest_hash,
        }

    return _resolver


def _default_git_resolver() -> GitResolver:
    """Core-local default: for a LOCAL repository path only (no network
    fetch of a remote URL — that requires an injected, network-aware
    resolver), shell out to ``git rev-parse`` to report the repo's current
    HEAD and whether the requested ``ref``/``commit`` is currently
    reachable. ``None`` when ``repository`` isn't a local directory, ``git``
    isn't on PATH, or the subprocess call fails/times out — never raises."""

    async def _resolver(selector: dict[str, Any]) -> dict[str, Any] | None:
        repo = selector.get("repository") or ""
        if not _looks_like_local_path(repo):
            return None
        exists, matched = _resolve_local_existence(repo, os.path.isdir)
        if not exists:
            return None
        want = selector.get("commit") or selector.get("ref")
        if not want:
            return None
        try:
            head = subprocess.run(
                ["git", "-C", matched, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            wanted = subprocess.run(
                ["git", "-C", matched, "rev-parse", want],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        if head.returncode != 0:
            return None
        reachable = wanted.returncode == 0
        return {
            "repository": matched,
            "head": head.stdout.strip(),
            "requested": want,
            "requested_sha": wanted.stdout.strip() if reachable else None,
            "reachable": reachable,
        }

    return _resolver


def _default_artifact_resolver() -> ArtifactResolver:
    """Core-local default: for a LOCAL ``manifest_uri`` file only, hash its
    current bytes to report a fresh fingerprint. ``None`` when the uri isn't
    a local path or the file can't be read — never raises."""

    async def _resolver(selector: dict[str, Any]) -> dict[str, Any] | None:
        manifest_uri = selector.get("manifest_uri") or ""
        if not _looks_like_local_path(manifest_uri):
            return None
        exists, matched = _resolve_local_existence(manifest_uri, os.path.isfile)
        if not exists:
            return None
        current_hash = _local_sha256_file(matched)
        if current_hash is None:
            return None
        return {"manifest_uri": matched, "content_hash": current_hash}

    return _resolver


async def _resolve_directory(
    selector: dict[str, Any], uri: str, directory_resolver: DirectoryResolver | None,
) -> dict[str, Any]:
    """``directory`` — resolve a directory root + include/exclude selector
    to a deterministic manifest (62640241)."""
    root = selector.get("root")
    base = {"selector_type": "directory", "uri": uri, "root": root}
    if directory_resolver is None:
        return _unresolved("no directory resolver available", **base)
    try:
        found = await directory_resolver(selector)
    except Exception:  # noqa: BLE001 — a resolver failure is just "unresolvable"
        _log.debug("directory resolve failed for %r", root, exc_info=True)
        return _unresolved("directory resolve failed", **base)
    if not found:
        return _unresolved("directory root not found", **base)
    return {"resolved": True, **base, "manifest": found}


async def _resolve_git(
    selector: dict[str, Any], uri: str, git_resolver: GitResolver | None,
) -> dict[str, Any]:
    """``git`` — resolve a repository + ref/commit to its current
    reachability against HEAD (62640241)."""
    repository = selector.get("repository")
    requested = selector.get("commit") or selector.get("ref")
    base = {
        "selector_type": "git", "uri": uri, "repository": repository,
        "requested": requested,
    }
    if git_resolver is None:
        return _unresolved("no git resolver available", **base)
    try:
        found = await git_resolver(selector)
    except Exception:  # noqa: BLE001
        _log.debug("git resolve failed for %r", repository, exc_info=True)
        return _unresolved("git resolve failed", **base)
    if not found:
        return _unresolved("git repository or ref/commit not found", **base)
    out = {"resolved": True, **base, **found}
    if not found.get("reachable", True):
        out["resolved"] = False
        out["reason"] = (
            f"{requested!r} is not reachable in repository {repository!r} "
            f"(current HEAD is {found.get('head')!r})"
        )
    return out


async def _resolve_remote_fs(
    selector: dict[str, Any], uri: str, remote_fs_resolver: RemoteFsResolver | None,
) -> dict[str, Any]:
    """``remote_fs`` — resolve an opaque host_id/filesystem_slot/path
    identity via an injected, tunnel-backed resolver (62640241). No
    core-local default — see the section docstring above."""
    host_id = selector.get("host_id")
    path = selector.get("path")
    base = {
        "selector_type": "remote_fs", "uri": uri, "host_id": host_id,
        "filesystem_slot": selector.get("filesystem_slot"), "path": path,
    }
    if remote_fs_resolver is None:
        return _unresolved(
            "no remote filesystem resolver available — remote_fs targets "
            "require a tunnel-backed resolver to be injected; this is an "
            "auditable unavailable result, not a silent drop", **base,
        )
    try:
        found = await remote_fs_resolver(selector)
    except Exception:  # noqa: BLE001
        _log.debug("remote_fs resolve failed for %r/%r", host_id, path, exc_info=True)
        return _unresolved("remote filesystem resolve failed", **base)
    if not found:
        return _unresolved("remote filesystem path not found", **base)
    return {"resolved": True, **base, "manifest": found}


async def _resolve_artifact(
    selector: dict[str, Any], uri: str, artifact_resolver: ArtifactResolver | None,
) -> dict[str, Any]:
    """``artifact`` — resolve a manifest_uri (+ optional fingerprint/
    run/item/provenance link) to its current fingerprint (62640241)."""
    manifest_uri = selector.get("manifest_uri")
    base = {"selector_type": "artifact", "uri": uri, "manifest_uri": manifest_uri}
    if artifact_resolver is None:
        return _unresolved("no artifact resolver available", **base)
    try:
        found = await artifact_resolver(selector)
    except Exception:  # noqa: BLE001
        _log.debug("artifact resolve failed for %r", manifest_uri, exc_info=True)
        return _unresolved("artifact resolve failed", **base)
    if not found:
        return _unresolved("artifact manifest not found", **base)
    return {"resolved": True, **base, "manifest": found}


async def _resolve_selector(
    db: Any,
    project_id: str,
    selector: dict[str, Any],
    uri: str,
    *,
    symbol_resolver: SymbolResolver,
    node_resolver: NodeResolver | None,
    citation_resolver: CitationResolver,
    web_fetcher: WebFetcher | None = None,
    finding_resolver: FindingResolver | None = None,
    directory_resolver: DirectoryResolver | None = None,
    git_resolver: GitResolver | None = None,
    remote_fs_resolver: RemoteFsResolver | None = None,
    artifact_resolver: ArtifactResolver | None = None,
) -> dict[str, Any]:
    """Dispatch ONE selector to its type-specific resolver (guarded)."""
    stype = selector.get("type")
    if stype == "range":
        return await _resolve_range(selector, uri)
    if stype == "symbol":
        return await _resolve_symbol(db, project_id, selector, uri, symbol_resolver)
    if stype == "node_id":
        return await _resolve_node_id(selector, uri, node_resolver)
    if stype == "zotero_key":
        return await _resolve_zotero_key(selector, uri, citation_resolver)
    if stype == "text_quote":
        return await _resolve_text_quote(selector, uri, web_fetcher)
    if stype == "finding_id":
        return await _resolve_finding_id(selector, uri, finding_resolver)
    if stype == "directory":
        return await _resolve_directory(selector, uri, directory_resolver)
    if stype == "git":
        return await _resolve_git(selector, uri, git_resolver)
    if stype == "remote_fs":
        return await _resolve_remote_fs(selector, uri, remote_fs_resolver)
    if stype == "artifact":
        return await _resolve_artifact(selector, uri, artifact_resolver)
    return _unresolved(f"unknown selector.type {stype!r}", uri=uri)


def _freshness_state_for_target(
    stype: str, declared: "dict[str, Any] | None", outer: dict[str, Any],
) -> "str | None":
    """Compute the LIVE freshness state for one resolved target (62640241).

    ``None`` when this selector type has no freshness concept at all (not in
    :data:`_FRESHNESS_APPLICABLE_TYPES` — the pre-existing range/symbol/
    node_id/zotero_key/finding_id types are left untouched, matching the
    module's "preserve existing behavior" guarantee). Otherwise one of the
    five universal states:

    * ``"unavailable"`` — the target didn't resolve at all (no resolver
      wired, resolve failed, or — for ``git`` — the requested ref/commit
      isn't reachable from the repo's current HEAD).
    * ``"current"`` / ``"stale"`` — ``text_quote`` reuses its own pre-existing
      ``drift`` flag directly (no live-hash comparison needed — drift
      already IS the freshness answer for a web/docx quote).
    * ``"unknown"`` — resolved live, but the target carries no declared
      ``freshness`` proof (or the proof has neither ``content_hash`` nor
      ``source_revision``) to compare against — there's nothing to confirm
      currency AGAINST, so it can't be called "current".
    * ``"current"`` / ``"stale"`` (directory/git/remote_fs/artifact) — the
      declared ``content_hash``/``source_revision`` matches (or doesn't)
      what was JUST resolved live (the directory's ``manifest_hash``, the
      git ``requested_sha``/``head``, the artifact's ``content_hash``, or
      whatever a remote_fs resolver's manifest reports under either key).

    Never raises: every lookup is a plain ``dict.get`` with static keys.
    """
    if stype not in _FRESHNESS_APPLICABLE_TYPES:
        return None
    if not isinstance(outer, dict) or not outer.get("resolved"):
        return "unavailable"
    if stype == "text_quote":
        return "stale" if outer.get("drift") else "current"

    if stype == "git" and not outer.get("reachable", True):
        return "unavailable"

    manifest = outer.get("manifest") if isinstance(outer.get("manifest"), dict) else {}
    if stype == "git":
        live_ref = outer.get("requested_sha") or outer.get("head")
    else:
        live_ref = manifest.get("content_hash") or manifest.get("manifest_hash") or manifest.get("source_revision")

    declared_ref = None
    if isinstance(declared, dict):
        declared_ref = declared.get("content_hash") or declared.get("source_revision")
    if not declared_ref:
        return "unknown"
    if live_ref is None:
        return "unavailable"
    return "current" if declared_ref == live_ref else "stale"


async def resolve_pointer(
    db: Any,
    pointer: dict[str, Any],
    *,
    project_id: str | None = None,
    symbol_resolver: SymbolResolver | None = None,
    node_resolver: NodeResolver | None = None,
    citation_resolver: CitationResolver | None = None,
    citation_backend: str | None = None,
    web_fetcher: WebFetcher | None = None,
    finding_resolver: FindingResolver | None = None,
    directory_resolver: DirectoryResolver | None = None,
    git_resolver: GitResolver | None = None,
    remote_fs_resolver: RemoteFsResolver | None = None,
    artifact_resolver: ArtifactResolver | None = None,
) -> dict[str, Any]:
    """Resolve every target of a pointer, dispatching by ``selector.type``.

    Returns ``{source_type, label?, targets: [<resolved-target>, ...]}`` where each
    resolved-target carries ``{uri, selector_type, resolved, ...}``. A ``range``
    target returns its location as-is; ``symbol`` / ``node_id`` / ``zotero_key``
    each call their (best-effort, injectable) resolver. An unresolvable target
    yields ``{resolved: False, reason}``. A ``subSelector`` is handled by resolving
    the OUTER selector first, then narrowing: the subSelector's resolution is
    attached under ``subResolved`` (and, when the subSelector is a ``range``, its
    ``range`` is echoed as ``narrowed_range`` — "these lines, within this
    function"). **NEVER raises** — malformed targets degrade to unresolved.

    Resolver seams default to the real implementations
    (``db.search_graph_entities`` / doc_store / ``zotero_client``); tests inject
    stubs so no network / live Zotero is touched. 62640241 —
    ``directory_resolver``/``git_resolver``/``artifact_resolver`` default to
    core-local, local-filesystem-only implementations (no network, no
    tunnel); ``remote_fs_resolver`` has NO core-local default (mirrors
    ``verify_target_readiness``'s ``provenance_getter`` seam) and stays
    ``None`` — a ``remote_fs`` target is reported unresolved, explicitly,
    unless a caller injects a tunnel-backed resolver. Because this dispatch
    is the ONE resolver every caller (hosted MCP, stdio MCP, or any other
    connector surface) invokes — there is no per-connector branch anywhere
    in this function — the SAME typed status shape comes back regardless of
    which connector path called it.

    Each resolved target also carries an additive ``freshness_state`` key
    (one of ``current``/``stale``/``unknown``/``unavailable``/``ambiguous``,
    or omitted entirely for selector types with no freshness concept) — see
    :func:`_freshness_state_for_target`.
    """
    # Default resolver seams (lazy imports to avoid import cycles / optional deps).
    if symbol_resolver is None:
        from .db import search_graph_entities as _sg  # noqa: PLC0415

        async def symbol_resolver(_db: Any, _pid: str, _q: str, _lim: int):  # type: ignore[misc]
            # eb8b6894 — this default resolver ONLY ever queries the cached
            # codebase_graph_entities snapshot (no production writers — see
            # prospect.build_symbol_resolver's docstring), never the live
            # tunnel-connected graph. Tag every hit explicitly so a caller
            # (e.g. handoff._annotate_resolved_pointers, which never injects
            # a tenant-aware symbol_resolver) can tell "resolved" apart from
            # "resolved against a possibly-stale cached snapshot" instead of
            # the two looking identical.
            _hits = await _sg(_db, _pid, _q, _lim)
            return [
                {**h, "resolution_source": h.get("resolution_source") or "stale_snapshot"}
                if isinstance(h, dict) else h
                for h in (_hits or [])
            ]

    if citation_resolver is None:
        # e9d72d17 — pick the reference-manager backend by name (arg / env / default)
        # instead of hardcoding Zotero. An explicit citation_resolver still wins
        # (tests inject one); citation_backend selects among registered backends.
        citation_resolver = resolve_citation_backend(citation_backend)

    if web_fetcher is None:
        # 1d3f6e71 — default live fetch for text_quote drift checks (httpx, guarded).
        from .web_archive import default_web_fetcher as _wf  # noqa: PLC0415

        async def web_fetcher(_uri: str):  # type: ignore[misc]
            return await _wf(_uri)

    if finding_resolver is None:
        # 1f1cd4d9 — a save_finding artifact IS a kind='finding' project note.
        from .db import get_project_note as _gn  # noqa: PLC0415

        async def finding_resolver(_id: str):  # type: ignore[misc]
            return await _gn(db, _id)

    # 62640241 — core-local, local-filesystem-only defaults. remote_fs has
    # deliberately NO default (see the docstring above).
    if directory_resolver is None:
        directory_resolver = _default_directory_resolver()
    if git_resolver is None:
        git_resolver = _default_git_resolver()
    if artifact_resolver is None:
        artifact_resolver = _default_artifact_resolver()

    pid = project_id or pointer.get("project_id") or ""
    source_type = pointer.get("source_type")
    targets = pointer.get("targets") or []

    resolved_targets: list[dict[str, Any]] = []
    for target in targets:
        if not isinstance(target, dict):
            resolved_targets.append(_unresolved("malformed target"))
            continue
        uri = target.get("uri") or ""
        selector = target.get("selector")
        if not isinstance(selector, dict):
            resolved_targets.append(
                _unresolved("malformed selector", uri=uri)
            )
            continue
        try:
            outer = await _resolve_selector(
                db, pid, selector, uri,
                symbol_resolver=symbol_resolver,
                node_resolver=node_resolver,
                citation_resolver=citation_resolver,
                web_fetcher=web_fetcher,
                finding_resolver=finding_resolver,
                directory_resolver=directory_resolver,
                git_resolver=git_resolver,
                remote_fs_resolver=remote_fs_resolver,
                artifact_resolver=artifact_resolver,
            )
        except Exception:  # noqa: BLE001 — belt-and-suspenders: a target never crashes the pass
            _log.debug("resolve_pointer target failed", exc_info=True)
            resolved_targets.append(_unresolved("resolve error", uri=uri))
            continue

        # 62640241 — live freshness state, compared against the target's own
        # declared proof (if any). Additive; omitted for selector types with
        # no freshness concept (see _freshness_state_for_target).
        stype = selector.get("type")
        fstate = _freshness_state_for_target(stype, target.get("freshness"), outer)
        if fstate is not None:
            outer["freshness_state"] = fstate

        # subSelector — resolve the outer, then narrow. The subSelector is itself a
        # full selector (W3C hasSubSelector); resolve it against the SAME uri.
        sub = selector.get("subSelector")
        if isinstance(sub, dict):
            try:
                sub_resolved = await _resolve_selector(
                    db, pid, sub, uri,
                    symbol_resolver=symbol_resolver,
                    node_resolver=node_resolver,
                    citation_resolver=citation_resolver,
                    web_fetcher=web_fetcher,
                    finding_resolver=finding_resolver,
                    directory_resolver=directory_resolver,
                    git_resolver=git_resolver,
                    remote_fs_resolver=remote_fs_resolver,
                    artifact_resolver=artifact_resolver,
                )
            except Exception:  # noqa: BLE001
                sub_resolved = _unresolved("subSelector resolve error", uri=uri)
            outer["subResolved"] = sub_resolved
            # "these lines, within this function": echo a range subSelector's span.
            if sub_resolved.get("resolved") and sub_resolved.get("selector_type") == "range":
                outer["narrowed_range"] = sub_resolved.get("range")
        resolved_targets.append(outer)

    result: dict[str, Any] = {
        "source_type": source_type,
        "targets": resolved_targets,
    }
    if pointer.get("label") is not None:
        result["label"] = pointer.get("label")
    if pointer.get("id") is not None:
        result["id"] = pointer.get("id")
    return result


# ---------------------------------------------------------------------------
# 665 follow-up — typed, machine-readable pointer records for XML/JSON
# handoff serialization.
#
# ``handoff._format_resolved_pointer`` (above this module, in handoff.py)
# already turns a ``resolve_pointer()`` result into a COMPACT human-readable
# ``{source_type, label?, targets: [str, ...]}`` for markdown/goal-line
# rendering — that stays byte-for-byte unchanged (backward compatibility for
# every existing legacy compact pointer line).
#
# This section builds the FULL typed record instead: source_type, target
# uri, selector/anchor (the whole selector object, including any nested
# subSelector), target_kind ("existing"/"planned_new"), label, an EXPLICIT
# per-target resolution ``status`` ("resolved" | "unresolved" | "planned" |
# "stale" | "archival" — see ``_typed_target_status``), canonical metadata
# (the resolved zotero item / doc_store element+document / graph match /
# finding artifact, when the selector type produces one) and archival
# metadata (a text_quote's Internet-Archive ``archived_url``/``archived_at``
# snapshot plus live ``drift``), when available. Everything a receiving
# executor needs to see a pointer's full typed state without re-resolving
# it, and without weakening ``validate_pointer``/the prospecting gate — this
# is a READ-ONLY, best-effort rendering over already-validated, already-
# persisted data.
#
# ``build_item_pointer_records`` is the ONE shared extraction both
# ``handoff.py``'s ``<sprint_item_pointers>`` XML clause and
# ``capability_contract.py``'s ``item_sprint_item_pointers`` JSON section
# call, so the two representations can never independently drift (mirrors
# the ``tool_requirements`` 76dde31f precedent).
# ---------------------------------------------------------------------------

# Selector-type-specific fields a RESOLVED target carries that count as
# "canonical metadata" for that selector type (the zotero bibliographic item,
# the doc_store element/document, the best-match graph symbol, the
# save_finding artifact). range/text_quote have none — text_quote's extra
# fields are archival metadata instead (see _ARCHIVAL_RESOLVED_FIELDS).
_CANONICAL_RESOLVED_FIELDS: dict[str, tuple[str, ...]] = {
    "symbol": ("file", "match"),
    "node_id": ("element", "document"),
    "zotero_key": ("item",),
    "finding_id": ("artifact",),
}
# text_quote's Internet-Archive snapshot metadata, captured at citation time.
_ARCHIVAL_RESOLVED_FIELDS = ("archived_url", "archived_at")


def _typed_target_status(target_kind: str, resolved_target: dict[str, Any]) -> str:
    """One explicit status word per target so a receiving executor never has
    to re-derive "is this planned / stale / archival / unresolved" from
    separate booleans scattered across the record:

    * ``"planned"``    — ``target_kind == "planned_new"`` (the file/target is
      declared but not expected to exist yet; never filesystem-checked).
    * ``"unresolved"`` — the resolver could not locate the target (see the
      sibling ``reason`` field on the record).
    * ``"stale"``      — resolved, but a ``text_quote`` target's cited
      passage has drifted (the live content no longer contains it).
    * ``"archival"``   — resolved, backed by an archived snapshot
      (``archived_url``) rather than (or in addition to) the live source.
    * ``"resolved"``   — resolved with none of the above special states.
    """
    if target_kind == "planned_new":
        return "planned"
    if not resolved_target.get("resolved"):
        return "unresolved"
    if resolved_target.get("drift"):
        return "stale"
    if resolved_target.get("archived_url"):
        return "archival"
    return "resolved"


def _extract_canonical_metadata(
    selector: Any, resolved_target: dict[str, Any]
) -> "dict[str, Any] | None":
    """Selector-type-specific canonical metadata the resolver produced, or
    ``None`` when this selector type has none (``range``/``text_quote``)."""
    stype = selector.get("type") if isinstance(selector, dict) else None
    fields = _CANONICAL_RESOLVED_FIELDS.get(stype or "")
    if not fields:
        return None
    out = {
        k: resolved_target[k] for k in fields if resolved_target.get(k) is not None
    }
    return out or None


def _extract_archival_metadata(resolved_target: dict[str, Any]) -> "dict[str, Any] | None":
    """A ``text_quote`` target's archived-snapshot metadata (``archived_url``/
    ``archived_at``) plus the live ``drift`` flag, when present. ``None``
    when the resolved target carries neither."""
    out: dict[str, Any] = {
        k: resolved_target[k]
        for k in _ARCHIVAL_RESOLVED_FIELDS
        if resolved_target.get(k) is not None
    }
    if "drift" in resolved_target:
        out["drift"] = bool(resolved_target["drift"])
    return out or None


# ---------------------------------------------------------------------------
# eb8b6894 — distinguish pointer PRESENCE ("a durable row exists") from
# successful TARGET RESOLUTION ("resolve_pointer actually found it") in
# readiness/handoff projections.
#
# Confirmed bug this fixes: a checkpoint/handoff projection could show a
# durable ``sprint_item_pointers`` row and mark provenance "satisfied" purely
# because the row existed (``get_pointer_evidence_item_ids`` /
# ``is_item_claim_prospected`` are PRESENCE-ONLY by design — see
# ``db.sprint_items.get_pointer_evidence_item_ids``'s own docstring) even when
# :func:`resolve_pointer` reported every target unresolved in the SAME
# annotation pass (``handoff._annotate_resolved_pointers`` already resolves
# every stored pointer — it just never surfaced the result at the
# provenance-decision layer). The three functions below give every pointer
# THREE separate, explicit signals instead of one conflated one:
#
# * :func:`check_structural_validity` — ``validate_pointer`` passed: the
#   pointer is well-formed (shape/schema only, no resolution attempted).
# * ``target_resolved`` (computed inline in :func:`build_typed_pointer_record`
#   from the already-resolved ``resolved`` argument) — :func:`resolve_pointer`
#   actually found/resolved EVERY target, not merely "a row exists".
# * ``provenance_verified`` (also computed inline, from an OPTIONAL
#   pre-computed :func:`verify_pointer_readiness` result) — a provenance
#   record backs this target, WHERE APPLICABLE (reuses 3196ba0e's readiness
#   primitives verbatim; ``None`` when not applicable, e.g. a non-local uri).
#
# All three are purely ADDITIVE fields on the existing typed record / item
# provenance dicts — no existing field's value or type changes, so a
# non-strict caller sees zero functional regression (see
# ``db.sprint_items.is_item_claim_prospected``'s new ``strict``/
# ``target_resolved`` kwargs, both opt-in and default-False/None).
# ---------------------------------------------------------------------------


def check_structural_validity(pointer: dict[str, Any]) -> "tuple[bool, str | None]":
    """Pure shape/schema check for an ALREADY-STORED pointer — no resolution,
    no filesystem I/O.

    Re-runs :func:`validate_pointer` with ``path_exists`` stubbed to always
    return ``True``. This is deliberate, not a shortcut: a pointer fetched
    from ``sprint_item_pointers`` (``row_to_pointer``) always carries an
    EXPLICIT ``target_kind`` on every target (normalization at write time
    fills it in even when the caller omitted it — see
    :func:`_validate_target`'s own docstring). Re-validating that already-
    normalized shape with the REAL filesystem checker would silently turn
    every implicit-``"existing"`` pointer written before 300a063d into an
    explicit, retroactive disk check — exactly the "opt-in, never
    retroactive" contract :func:`validate_pointer`'s module docstring
    promises NOT to do. Stubbing ``path_exists`` keeps this check to pure
    shape/schema correctness, matching the module docstring's own framing
    of ``validate_pointer`` as "structural validation... no resolution".

    Returns ``(True, None)`` when valid, ``(False, <message>)`` otherwise.
    Never raises.
    """
    try:
        validate_pointer(pointer, path_exists=lambda _uri: True)
        return True, None
    except PointerValidationError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 — never let this break annotation
        return False, f"{type(exc).__name__}: {exc}"


def _summarize_target_resolution(
    resolved_targets: "list[dict[str, Any]]",
) -> "tuple[bool, str | None]":
    """Aggregate :func:`resolve_pointer` per-target ``resolved`` flags for
    ONE pointer into a single bool: ``True`` iff there is at least one
    target AND every one of them resolved (mirrors
    :func:`verify_pointer_readiness`'s own "all targets must pass" policy —
    an empty/all-missing resolve pass is never vacuously "resolved").
    """
    if not resolved_targets:
        return False, "pointer has not been resolved (no resolve_pointer output supplied)"
    unresolved = [
        t for t in resolved_targets if not (isinstance(t, dict) and t.get("resolved"))
    ]
    if unresolved:
        reasons = sorted({
            str(t.get("reason")) for t in unresolved
            if isinstance(t, dict) and t.get("reason")
        })
        detail = "; ".join(reasons) if reasons else "target did not resolve"
        return False, f"{len(unresolved)}/{len(resolved_targets)} target(s) unresolved — {detail}"
    return True, None


def _summarize_resolution_source(resolved_targets: "list[dict[str, Any]]") -> str:
    """Aggregate each resolved target's ``resolution_source`` (set by
    :func:`_resolve_symbol` — "live_graph" / "local_fallback" /
    "stale_snapshot" / "unknown") into one value for the whole pointer:

    * ``"not_applicable"`` — no target reports a resolution_source at all
      (every target is a selector type this concept doesn't apply to, e.g.
      ``range``/``zotero_key``/``node_id``/``text_quote``/``finding_id``).
    * ``"mixed"`` — more than one distinct source across this pointer's targets.
    * the single shared value otherwise (e.g. ``"live_graph"``).
    """
    sources = {
        t.get("resolution_source") for t in resolved_targets
        if isinstance(t, dict) and t.get("resolution_source")
    }
    if not sources:
        return "not_applicable"
    if len(sources) > 1:
        return "mixed"
    return next(iter(sources))


def _summarize_target_freshness(
    resolved_targets: "list[dict[str, Any]]",
) -> "tuple[bool | None, str | None]":
    """Aggregate each resolved target's ``freshness_state`` (62640241) for
    ONE pointer into a tri-state ``(fresh, reason)``, mirroring
    :func:`_summarize_provenance_verified`'s own shape:

    * ``(None, reason)`` — not applicable: no target in this pointer reports
      a ``freshness_state`` at all (every target is a selector type with no
      freshness concept — see :data:`_FRESHNESS_APPLICABLE_TYPES`).
    * ``(False, reason)`` — applicable, and at least one target's state is
      ``stale`` / ``unknown`` / ``unavailable`` / ``ambiguous``.
    * ``(True, None)`` — applicable, every reporting target is ``current``.
    """
    applicable = [
        t for t in resolved_targets
        if isinstance(t, dict) and t.get("freshness_state") is not None
    ]
    if not applicable:
        return None, "no targets report a freshness state"
    not_current = [t for t in applicable if t.get("freshness_state") != "current"]
    if not_current:
        states = sorted({str(t.get("freshness_state")) for t in not_current})
        return False, f"{len(not_current)}/{len(applicable)} target(s) not current — {', '.join(states)}"
    return True, None


def _summarize_provenance_verified(
    readiness: "dict[str, Any] | None",
) -> "tuple[bool | None, str | None]":
    """Aggregate an OPTIONAL, pre-computed :func:`verify_pointer_readiness`
    result for ONE pointer into a tri-state ``(verified, reason)``:

    * ``(None, reason)`` — not computed (``readiness`` is ``None`` — the
      caller didn't run the I/O-backed check) or not applicable (every
      target is a non-local uri — see :func:`verify_target_readiness`'s own
      ``"skipped"`` status).
    * ``(False, reason)`` — computed, applicable, and at least one target's
      ``ready`` came back ``False`` (missing file, undischarged
      planned_new provenance, etc).
    * ``(True, None)`` — computed, applicable, every target ready.

    Deliberately tri-state (never coerces "not computed" to ``False``): a
    caller that never ran the readiness check must not have that silence
    misread as "verification failed".
    """
    if not isinstance(readiness, dict):
        return None, "readiness not computed for this pointer"
    results = readiness.get("targets")
    if not isinstance(results, list) or not results:
        return None, "readiness not computed for this pointer"
    applicable = [
        r for r in results if isinstance(r, dict) and r.get("status") != "skipped"
    ]
    if not applicable:
        return None, "no local filesystem targets to verify"
    not_ready = [r for r in applicable if not r.get("ready")]
    if not_ready:
        reasons = sorted({str(r.get("reason")) for r in not_ready if r.get("reason")})
        detail = "; ".join(reasons) if reasons else "one or more targets not provenance-verified"
        return False, detail
    return True, None


def aggregate_pointer_evidence(
    typed_records: "list[dict[str, Any]]",
) -> "dict[str, Any]":
    """Item-level rollup of the PER-POINTER ``structural_valid`` /
    ``target_resolved`` / ``provenance_verified`` / ``resolution_source``
    fields :func:`build_typed_pointer_record` attaches to each entry of
    ``typed_records`` (one item's full ``pointer_records`` list — see
    ``handoff._annotate_resolved_pointers``).

    Pure, synchronous rollup — mirrors :func:`verify_pointer_readiness`'s own
    "ready iff non-empty AND every entry passes" aggregation:

    * ``structural_valid`` — ``None`` when ``typed_records`` is empty (no
      pointer to check — not "valid", not "invalid"); else ``True`` iff
      EVERY pointer's own ``structural_valid`` is ``True``.
    * ``target_resolved`` — ``False`` when empty (nothing has been
      resolved); else ``True`` iff EVERY pointer's own ``target_resolved``
      is ``True``. Deliberately never vacuously ``True`` on an empty list —
      this is the field the eb8b6894 STRICT pointer gate consults (see
      ``db.sprint_items.is_item_claim_prospected``'s ``strict`` kwarg): a
      row that exists but never actually resolved must not read as
      resolved just because nothing failed.
    * ``provenance_verified`` — tri-state: ``False`` if any pointer's own
      value is explicitly ``False``; else ``None`` if every pointer's own
      value is ``None`` (nothing applicable/computed); else ``True``.
    * ``resolution_source`` — ``"not_applicable"`` when empty or no pointer
      reports one; the single shared value when every reporting pointer
      agrees; ``"mixed"`` otherwise.
    * ``freshness_verified`` (62640241) — tri-state, same rule as
      ``provenance_verified``: ``False`` if any pointer's own
      ``freshness_verified`` is explicitly ``False``; else ``None`` if every
      pointer's own value is ``None`` (nothing applicable/computed); else
      ``True``.

    Never raises: a malformed (non-dict) entry in ``typed_records`` is
    skipped rather than breaking the rollup.
    """
    records = [r for r in typed_records if isinstance(r, dict)]
    if not records:
        return {
            "structural_valid": None,
            "target_resolved": False,
            "provenance_verified": None,
            "resolution_source": "not_applicable",
            "freshness_verified": None,
        }
    structural = [bool(r.get("structural_valid")) for r in records]
    resolved = [bool(r.get("target_resolved")) for r in records]
    prov_values = [r.get("provenance_verified") for r in records]
    prov_verified: "bool | None"
    if any(v is False for v in prov_values):
        prov_verified = False
    else:
        _non_none = [v for v in prov_values if v is not None]
        prov_verified = True if _non_none else None
    fresh_values = [r.get("freshness_verified") for r in records]
    fresh_verified: "bool | None"
    if any(v is False for v in fresh_values):
        fresh_verified = False
    else:
        _non_none_fresh = [v for v in fresh_values if v is not None]
        fresh_verified = True if _non_none_fresh else None
    sources = {
        r.get("resolution_source") for r in records
        if r.get("resolution_source") not in (None, "not_applicable")
    }
    if not sources:
        resolution_source = "not_applicable"
    elif len(sources) > 1:
        resolution_source = "mixed"
    else:
        resolution_source = next(iter(sources))
    return {
        "structural_valid": all(structural),
        "target_resolved": all(resolved),
        "provenance_verified": prov_verified,
        "resolution_source": resolution_source,
        "freshness_verified": fresh_verified,
    }


def build_typed_pointer_record(
    pointer: dict[str, Any],
    resolved: "dict[str, Any] | None" = None,
    *,
    readiness: "dict[str, Any] | None" = None,
) -> "dict[str, Any] | None":
    """Build ONE typed, machine-readable pointer record.

    ``pointer`` is a STORED pointer (as returned by ``row_to_pointer`` /
    ``db.get_sprint_item_pointers`` — normalized targets carrying
    ``target_kind``); ``resolved`` is that SAME pointer's
    :func:`resolve_pointer` output (targets in the identical order/count —
    ``resolve_pointer`` always appends exactly one resolved entry per input
    target, even a malformed one, so a positional zip is safe).

    Returns ``None`` when the pointer has no renderable target — mirrors
    ``handoff._format_resolved_pointer``'s "no lines -> None" contract for
    its compact sibling. ``resolved`` may be omitted (or ``None``): every
    target is then treated as unresolved with no reason, which is still a
    fully valid, explicit typed record.

    ``readiness`` (eb8b6894) — an OPTIONAL, pre-computed
    :func:`verify_pointer_readiness` result for this SAME pointer (the
    caller runs the I/O-backed check itself, e.g. via
    :func:`compute_pointer_readiness_for_record`, and passes the result in —
    this function stays pure/synchronous). When omitted, ``provenance_verified``
    on the returned record is ``None`` ("not computed"), never coerced to
    ``False``.

    eb8b6894 — beyond the per-TARGET fields above, the returned record also
    carries four POINTER-level fields distinguishing "a durable row exists"
    from "the target actually resolves to something real":
    ``structural_valid`` (:func:`check_structural_validity` — shape/schema
    only), ``target_resolved`` (:func:`_summarize_target_resolution` — every
    target actually resolved via ``resolve_pointer``, not just present),
    ``resolution_source`` (:func:`_summarize_resolution_source` — "live_graph"
    vs. a fallback/cache, when the selector type reports it), and
    ``provenance_verified`` (:func:`_summarize_provenance_verified` — tri-state,
    ``None`` when not computed/not applicable). These are purely ADDITIVE
    keys; no existing key's value or type changes. 62640241 adds a fifth,
    same-shape field: ``freshness_verified`` (:func:`_summarize_target_freshness`
    — tri-state, ``None`` when no target in this pointer has a freshness
    concept at all). Each per-target entry also carries the declared
    ``freshness`` proof (when the stored target had one) and the LIVE
    ``freshness_state`` :func:`resolve_pointer` computed for it.

    Never raises: a malformed stored target is skipped rather than blowing
    up the whole record — this must be safe to call from a mandatory
    handoff path.
    """
    if not isinstance(pointer, dict):
        return None
    stored_targets = pointer.get("targets")
    if not isinstance(stored_targets, list) or not stored_targets:
        return None
    resolved = resolved if isinstance(resolved, dict) else {}
    resolved_targets = resolved.get("targets")
    if not isinstance(resolved_targets, list):
        resolved_targets = []

    typed_targets: list[dict[str, Any]] = []
    for idx, raw_target in enumerate(stored_targets):
        if not isinstance(raw_target, dict):
            continue
        uri = raw_target.get("uri")
        selector = raw_target.get("selector")
        target_kind = raw_target.get("target_kind") or _DEFAULT_TARGET_KIND
        rtarget = (
            resolved_targets[idx]
            if idx < len(resolved_targets) and isinstance(resolved_targets[idx], dict)
            else {}
        )
        entry: dict[str, Any] = {
            "uri": uri,
            "selector": selector,
            "target_kind": target_kind,
            "resolved": bool(rtarget.get("resolved")),
            "status": _typed_target_status(target_kind, rtarget),
        }
        if not rtarget.get("resolved") and rtarget.get("reason"):
            entry["reason"] = rtarget["reason"]
        # 62640241 — echo the declared freshness proof (if the target had
        # one) and the LIVE freshness state resolve_pointer computed for it.
        declared_freshness = raw_target.get("freshness")
        if isinstance(declared_freshness, dict):
            entry["freshness"] = declared_freshness
        if rtarget.get("freshness_state") is not None:
            entry["freshness_state"] = rtarget["freshness_state"]
        canonical = _extract_canonical_metadata(selector, rtarget)
        if canonical:
            entry["canonical"] = canonical
        archival = _extract_archival_metadata(rtarget)
        if archival:
            entry["archival"] = archival
        # A subSelector narrows the outer target ("these lines, within this
        # function") — surface its resolution too, so "resolution status …
        # explicit" also holds for the nested anchor, not just the outer one.
        sub_resolved = rtarget.get("subResolved")
        if isinstance(sub_resolved, dict):
            sub_entry: dict[str, Any] = {"resolved": bool(sub_resolved.get("resolved"))}
            if not sub_resolved.get("resolved") and sub_resolved.get("reason"):
                sub_entry["reason"] = sub_resolved["reason"]
            entry["sub_resolved"] = sub_entry
        if rtarget.get("narrowed_range"):
            entry["narrowed_range"] = rtarget["narrowed_range"]
        typed_targets.append(entry)

    if not typed_targets:
        return None

    record: dict[str, Any] = {
        "source_type": pointer.get("source_type") or resolved.get("source_type"),
        "targets": typed_targets,
    }
    if pointer.get("id"):
        record["id"] = pointer["id"]
    if pointer.get("label"):
        record["label"] = pointer["label"]

    # eb8b6894 — pointer-level presence-vs-resolution distinction (see the
    # module section docstring above check_structural_validity).
    _struct_valid, _struct_error = check_structural_validity(pointer)
    record["structural_valid"] = _struct_valid
    if not _struct_valid:
        record["structural_error"] = _struct_error
    _target_resolved, _target_resolved_reason = _summarize_target_resolution(resolved_targets)
    record["target_resolved"] = _target_resolved
    if not _target_resolved:
        record["target_resolved_reason"] = _target_resolved_reason
    record["resolution_source"] = _summarize_resolution_source(resolved_targets)
    _prov_verified, _prov_reason = _summarize_provenance_verified(readiness)
    record["provenance_verified"] = _prov_verified
    if _prov_verified is not True:
        record["provenance_reason"] = _prov_reason
    # 62640241 — pointer-level freshness rollup, same tri-state shape as
    # provenance_verified above.
    _fresh_verified, _fresh_reason = _summarize_target_freshness(resolved_targets)
    record["freshness_verified"] = _fresh_verified
    if _fresh_verified is not True:
        record["freshness_reason"] = _fresh_reason
    return record


async def build_item_pointer_records(
    db: Any,
    project_id: str,
    stored_pointers: list[dict[str, Any]],
    *,
    node_resolver: "NodeResolver | None" = None,
) -> list[dict[str, Any]]:
    """Resolve + type EVERY stored pointer for one item in a single pass.

    The canonical, shared extraction behind BOTH ``handoff.py``'s
    ``<sprint_item_pointers>`` XML clause and ``capability_contract.py``'s
    ``item_sprint_item_pointers`` JSON section (665 follow-up) — one
    :func:`resolve_pointer` call per stored pointer, never two independent
    resolve passes over the same data.

    Guarded per-pointer: a resolve failure degrades that ONE pointer to an
    unresolved typed record (via ``build_typed_pointer_record(ptr, None)``)
    rather than dropping the item's other evidence; NEVER raises.

    eb8b6894 — ALSO runs :func:`compute_pointer_readiness_for_record` per
    pointer (best-effort, core-local default figure_resolver, no
    provenance_getter — see that function's docstring) and threads the
    result into :func:`build_typed_pointer_record`'s ``readiness`` kwarg, so
    every typed record's ``provenance_verified`` field is populated here the
    SAME way ``handoff._annotate_resolved_pointers`` populates it — the two
    call sites that build a pointer's typed record can never independently
    drift on what "provenance_verified" means.
    """
    records: list[dict[str, Any]] = []
    for ptr in stored_pointers:
        if not isinstance(ptr, dict):
            continue
        resolved: "dict[str, Any] | None"
        try:
            resolved = await resolve_pointer(
                db, ptr, project_id=project_id, node_resolver=node_resolver
            )
        except Exception:  # noqa: BLE001 — resolve_pointer never raises, but be safe
            resolved = None
        readiness = await compute_pointer_readiness_for_record(ptr)
        try:
            record = build_typed_pointer_record(ptr, resolved, readiness=readiness)
        except Exception:  # noqa: BLE001 — a malformed pointer must not break the batch
            record = None
        if record:
            records.append(record)
    return records


def assemble_pointer_entries_from_annotated_items(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pure, synchronous assembly of the canonical per-item pointer entries
    from items ALREADY annotated with ``pointer_records``/
    ``pointer_provenance`` (see ``handoff._annotate_resolved_pointers``,
    which sets both in the SAME resolve pass this module's
    :func:`build_item_pointer_records` powers).

    Each entry is ``{item_id, provenance: {required, bypassed, satisfied}?,
    resolution_status: {structural_valid, target_resolved,
    provenance_verified, resolution_source, strict_satisfied}?,
    pointers: [<typed pointer record>, ...]?}``, sorted by ``item_id`` for
    deterministic byte-for-byte output. An item contributes an entry only
    when it has >=1 typed pointer record OR its provenance state is
    ``required`` — an ordinary item with neither is silently skipped, so
    this never adds noise for the common case.

    ``resolution_status`` (eb8b6894) — the item's ``pointer_resolution_status``
    field, when present (set by ``handoff._annotate_resolved_pointers``
    alongside ``pointer_provenance``/``pointer_records`` in the SAME resolve
    pass — see :func:`aggregate_pointer_evidence`). Purely additive: an item
    with no such field (e.g. annotated by a caller predating eb8b6894)
    simply omits the key, byte-for-byte identical to before this field
    existed.

    Shared by ``handoff._build_pointer_records_clause`` (the /goal block's
    ``<sprint_item_pointers>`` XML clause) and
    ``capability_contract.extract_sprint_item_pointers`` (the JSON
    ``item_sprint_item_pointers`` section, when the caller already supplied
    pre-annotated items) — 665 follow-up — so neither maintains its own
    independent derivation of "which items make the cut."
    """
    entries: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        iid = it.get("id")
        if not iid:
            continue
        records = it.get("pointer_records") or []
        provenance = it.get("pointer_provenance")
        resolution_status = it.get("pointer_resolution_status")
        if not records and not (isinstance(provenance, dict) and provenance.get("required")):
            continue
        entry: dict[str, Any] = {"item_id": iid}
        if provenance:
            entry["provenance"] = provenance
        if resolution_status:
            entry["resolution_status"] = resolution_status
        if records:
            entry["pointers"] = records
        entries.append(entry)
    return sorted(entries, key=lambda e: e["item_id"])


# ---------------------------------------------------------------------------
# 62640241 — strict-handoff freshness gate.
#
# A STRICT handoff (``require_strict_evidence``) must not let a receiving
# executor act on evidence the source has silently drifted out from under —
# acceptance criterion 4 of 62640241: "strict handoff readiness blocks
# stale/unknown targets with an actionable reason." This is the ONE gate
# every strict-evidence caller consults, mirroring the "one shared
# extraction, many renderers" precedent of :func:`build_typed_pointer_record`
# / :func:`assemble_pointer_entries_from_annotated_items` above — it never
# re-derives freshness itself, only reads the ``freshness_state`` values
# those functions already computed and attached.
# ---------------------------------------------------------------------------

# States that block a STRICT handoff. "ambiguous" is included even though no
# resolver in this module currently emits it for the new selector types
# (directory/git/artifact resolution is always deterministic here) — it is
# part of the universal five-state vocabulary (module docstring) and a
# richer injected resolver (e.g. a remote_fs resolver reporting multiple
# candidate snapshots) may legitimately report it.
_BLOCKING_FRESHNESS_STATES = frozenset({"stale", "unknown", "unavailable", "ambiguous"})


def strict_freshness_gate(
    typed_records: "list[dict[str, Any]]",
) -> "tuple[bool, list[str]]":
    """Fail-closed freshness readiness verdict for a STRICT handoff.

    ``typed_records`` is one item's ``pointer_records`` list — the SAME
    already-built :func:`build_typed_pointer_record` output
    :func:`assemble_pointer_entries_from_annotated_items` consumes. Scans
    every target of every pointer for a ``freshness_state`` in
    :data:`_BLOCKING_FRESHNESS_STATES`; a target with no freshness concept at
    all (``freshness_state`` omitted — range/symbol/node_id/zotero_key/
    finding_id) is never blocking, matching this module's "preserve existing
    behavior" guarantee for the six original selector types.

    Returns ``(True, [])`` when nothing blocks (including the case where
    ``typed_records`` is empty or carries no freshness-applicable targets at
    all — freshness is opt-in; a pointer that never declared or resolved to
    a freshness concept cannot fail a check that doesn't apply to it).
    Otherwise ``(False, reasons)`` where ``reasons`` is a sorted, actionable,
    human-readable list — one entry per blocking target, naming the pointer
    (its id, else its source_type), the target's uri, and its state — so a
    receiving executor knows EXACTLY which evidence to re-verify or refresh,
    not just that "something" is stale.

    Never raises: every field access is a guarded ``dict.get``.
    """
    reasons: list[str] = []
    for rec in typed_records or []:
        if not isinstance(rec, dict):
            continue
        pointer_ref = rec.get("id") or rec.get("source_type") or "pointer"
        for t in rec.get("targets") or []:
            if not isinstance(t, dict):
                continue
            state = t.get("freshness_state")
            if state in _BLOCKING_FRESHNESS_STATES:
                reasons.append(
                    f"{pointer_ref}: target {t.get('uri')!r} is freshness_state="
                    f"{state!r} — re-capture or refresh this pointer's evidence "
                    "before trusting it in a strict handoff"
                )
    return (not reasons), sorted(reasons)


# ---------------------------------------------------------------------------
# Fail-closed artifact readiness verification (3196ba0e — b730 follow-up)
# ---------------------------------------------------------------------------
#
# ``validate_pointer``'s target_kind check (300a063d, above) answers a narrow
# WRITE-time question: "does an explicit target_kind='existing' point at
# something that exists RIGHT NOW?" It is opt-in and never touches
# meridian-outputs — a symlink/directory mixup, an archival/stale copy, or a
# planned_new target that was declared but never actually produced all pass
# it silently (planned_new is exempt from the check entirely, by design).
#
# :func:`verify_target_readiness` / :func:`verify_pointer_readiness` answer
# the FAIL-CLOSED, COMPLETION-time question instead: "is this target
# genuinely ready to stand behind a sprint item being marked done?" Per
# ``target_kind``:
#
#   * ``"existing"`` — the uri must resolve to a real FILE (not a directory)
#     on disk; that is the one hard gate (``ready=False`` on ``"missing"`` /
#     ``"is_directory"``). Beyond it, when a ``figure_resolver`` is
#     available the target is resolved THROUGH meridian-outputs — reusing
#     ``outputs_indexer.resolve_output`` / ``resolve_figure_output`` (the
#     SAME two-stage canonical-vs-archival classification
#     ``classify_canonical_archival`` already computes; never re-derived
#     here) — and classified as ``"canonical"``, ``"archival"`` (stale),
#     ``"unresolved"``, or ``"ambiguous"`` (multiple same-basename
#     candidates — the meridian-outputs extension's relocation-tolerant
#     basename-fallback tier, see ``provenance.resolve_figure_output``).
#     That classification is recorded EVIDENCE, not a second gate — it never
#     flips ``ready`` back to False on its own, mirroring
#     ``OutputsFtsIndex.search``'s own policy that archival rows are
#     DEPRIORITIZED, never hard-excluded.
#
#   * ``"planned_new"`` — a much harder bar, straight from the sprint spec:
#     "planned_new must NOT be allowed to satisfy readiness merely by naming
#     a future path." A planned_new target is only ``ready`` when (1) the
#     file now actually EXISTS (the plan was executed, not merely declared)
#     AND (2) a provenance record for it is on file, via an injected
#     ``provenance_getter`` that reuses
#     ``extensions/meridian-outputs``'s ``record_provenance`` /
#     ``get_provenance`` ledger — never re-implemented here. Either missing
#     piece fails closed (``"not_created"`` / ``"provenance_missing"``).
#
# meridian-outputs lives in a SEPARATE package (``extensions/meridian-
# outputs``) that is not installed into core's own env and is not
# importable from here (see pixi.toml's 52cbe5d8 note and
# ``tunnel_plugins.py``) — a hosted tenant's outputs tree may not even be on
# this machine, reachable only over the tunnel. Both seams are therefore
# guarded, injectable, ASYNC callables, matching :func:`resolve_pointer`'s
# own symbol_resolver/node_resolver/citation_resolver seam shape exactly:
#
#   * ``figure_resolver`` DOES have a real core-local default — a lazy-
#     imported async wrapper around ``outputs_indexer.resolve_figure_output``
#     (plain, synchronous, no tunnel needed for an outputs tree that IS on
#     this machine, e.g. self-hosted). A caller with a richer resolver (the
#     meridian-outputs extension's own ``provenance.resolve_figure_output``,
#     with its basename-fallback/``match_type``/``candidate_count`` fields,
#     or a tunnel-backed MCP call for a hosted tenant) injects it instead.
#   * ``provenance_getter`` has NO core-local default — the provenance
#     ledger lives ONLY in the extension — and stays ``None`` unless a
#     caller (the MCP handler layer, once wired) injects one.
#
# An unavailable seam (``None``) or one that raises is ALWAYS surfaced as an
# EXPLICIT degraded/unavailable status (``"unresolved"`` / ``"degraded"`` for
# existing targets; ``"provenance_unavailable"`` / ``"provenance_check_failed"``
# for planned_new, both ``ready=False``) — NEVER silently treated as
# ``"canonical"`` / ``"ready"``. That is the whole point of a fail-closed
# gate: an unreachable check must never be indistinguishable from a passed
# one.
# ---------------------------------------------------------------------------

FigureResolver = Callable[[str, str], Awaitable[dict[str, Any] | None]]
ProvenanceGetter = Callable[[str, str], Awaitable[dict[str, Any] | None]]


def _default_figure_resolver() -> FigureResolver:
    """Lazy-imported default seam: core's own local, synchronous
    ``outputs_indexer.resolve_figure_output`` (exact-path match against the
    ``outputs_index`` — no tunnel needed for an outputs tree reachable on
    this machine). Wrapped as ``async`` purely to match the injectable
    seam's shape (mirrors ``resolve_pointer``'s own lazy-import wrappers).
    """
    from . import outputs_indexer as _oi  # noqa: PLC0415

    async def _resolver(outputs_dir: str, file_path: str) -> dict[str, Any] | None:
        return _oi.resolve_figure_output(outputs_dir, file_path)

    return _resolver


async def verify_target_readiness(
    target: dict[str, Any],
    *,
    outputs_dir: str | None = None,
    path_exists: Callable[[str], bool] | None = None,
    is_dir: Callable[[str], bool] | None = None,
    figure_resolver: FigureResolver | None = None,
    provenance_getter: ProvenanceGetter | None = None,
) -> dict[str, Any]:
    """Fail-closed, completion-time readiness check for ONE pointer target.

    Distinct from (and complementary to) :func:`validate_pointer`'s opt-in,
    write-time ``target_kind='existing'`` filesystem check — see the module
    section docstring above for the full rationale.

    ``target`` is one normalized ``{uri, target_kind, ...}`` target dict
    (e.g. one entry of a :func:`validate_pointer`-normalized pointer's
    ``targets`` list). A missing/invalid ``target_kind`` defaults to
    ``"existing"`` (matches :func:`validate_pointer`'s own default). A
    non-local ``uri`` (a URL, or a ``zotero:``/``doc:``/``finding:``
    reference — see :func:`_looks_like_local_path`) is out of scope for this
    filesystem-oriented check and is reported ``ready=True, status="skipped"``
    — those schemes have their own existence semantics, already checked by
    :func:`resolve_pointer`.

    ``path_exists`` / ``is_dir`` are injectable filesystem checkers
    (default :func:`os.path.exists` / :func:`os.path.isdir`), the same
    pattern :func:`validate_pointer` already uses. ``figure_resolver`` /
    ``provenance_getter`` are the meridian-outputs seams described above.

    Returns a dict always carrying ``{uri, target_kind, ready, status,
    reason}`` plus, when available, ``resolved`` (the meridian-outputs hit)
    or ``provenance`` (the provenance ledger record). ``status`` values:

    * ``"skipped"`` — non-local uri, not applicable (``ready=True``).
    * ``"missing_uri"`` — the target carries no uri at all (``ready=False``).
    * ``"missing"`` — ``existing``: no file at ``uri`` (``ready=False``).
    * ``"is_directory"`` — the uri names a directory, not a file
      (``ready=False``, either kind).
    * ``"canonical"`` — ``existing``: verified on disk AND meridian-outputs
      resolves it to a non-archival, unambiguous output (``ready=True``).
    * ``"archival"`` — ``existing``: verified on disk but meridian-outputs
      flags the resolved output archival/stale (``ready=True`` — recorded
      evidence, not a second gate; see rationale above).
    * ``"ambiguous"`` — ``existing``: verified on disk but meridian-outputs'
      basename-fallback tier found multiple same-named candidates
      (``ready=True``).
    * ``"unresolved"`` — ``existing``: verified on disk, but meridian-outputs
      is unavailable (``figure_resolver`` is ``None``) or has no matching
      record (``ready=True`` — file presence alone is the hard gate).
    * ``"degraded"`` — ``existing``: verified on disk, but the injected
      ``figure_resolver`` raised (``ready=True`` — same reasoning: disk
      presence is the hard gate, the enrichment step merely failed).
    * ``"not_created"`` — ``planned_new``: the planned path does not exist
      yet — naming a future path never satisfies readiness (``ready=False``).
    * ``"provenance_unavailable"`` — ``planned_new``: the file was created
      but no ``provenance_getter`` is wired to confirm registration
      (``ready=False`` — fail closed, never assume success).
    * ``"provenance_check_failed"`` — ``planned_new``: the file was created
      but the injected ``provenance_getter`` raised (``ready=False``).
    * ``"provenance_missing"`` — ``planned_new``: the file was created but no
      provenance record is on file for it (``ready=False``).
    * ``"ready"`` — ``planned_new``: created AND provenance-registered
      (``ready=True``).

    Never raises: every resolver/checker call is guarded; a failure degrades
    to an explicit unavailable/degraded status, never to a silent pass.
    """
    uri = ""
    kind: Any = None
    if isinstance(target, dict):
        raw_uri = target.get("uri")
        uri = raw_uri.strip() if isinstance(raw_uri, str) else ""
        kind = target.get("target_kind")
    if kind not in _TARGET_KINDS:
        kind = _DEFAULT_TARGET_KIND
    base: dict[str, Any] = {"uri": uri, "target_kind": kind}

    if not uri:
        return {**base, "ready": False, "status": "missing_uri",
                "reason": "target has no uri to verify"}

    if not _looks_like_local_path(uri):
        return {
            **base, "ready": True, "status": "skipped",
            "reason": (
                "not a local filesystem path — readiness verification only "
                "applies to local artifacts; other uri schemes "
                "(zotero:/doc:/finding:/URLs) have their own existence "
                "semantics, checked by resolve_pointer instead"
            ),
        }

    exists_checker = path_exists or os.path.exists
    dir_checker = is_dir or os.path.isdir
    # ba539706 — try uri under every normalized candidate spelling (file://
    # stripped, separators flipped, Windows-drive <-> WSL /mnt/<drive>)
    # before concluding "missing". matched_path is the spelling that
    # actually resolved (== uri when nothing needed normalizing, or when
    # nothing matched at all); it's what subsequent is_dir/figure_resolver/
    # provenance_getter calls use, while `base["uri"]` keeps reporting the
    # ORIGINAL stored uri.
    exists, matched_path = _resolve_local_existence(uri, exists_checker)

    if kind == "planned_new":
        if not exists:
            return {
                **base, "ready": False, "status": "not_created",
                "reason": (
                    f"planned_new target {uri!r} has not been created yet — "
                    "naming a future path does not satisfy readiness; create "
                    "the file, then record its provenance"
                ),
            }
        try:
            is_directory = bool(dir_checker(matched_path))
        except OSError:
            is_directory = False
        if is_directory:
            return {**base, "ready": False, "status": "is_directory",
                    "reason": f"{uri!r} is a directory, not a file"}
        if provenance_getter is None:
            return {
                **base, "ready": False, "status": "provenance_unavailable",
                "reason": (
                    "no provenance checker is wired (meridian-outputs is "
                    "unavailable) — cannot confirm record_provenance was "
                    "ever called for this path; degrading explicitly rather "
                    "than assuming success"
                ),
            }
        try:
            record = await provenance_getter(outputs_dir or "", matched_path)
        except Exception as exc:  # noqa: BLE001 — degrade, never fake success
            _log.debug(
                "verify_target_readiness: provenance_getter failed for %r",
                uri, exc_info=True,
            )
            return {**base, "ready": False, "status": "provenance_check_failed",
                    "reason": f"provenance lookup raised: {exc}"}
        if not record:
            return {
                **base, "ready": False, "status": "provenance_missing",
                "reason": (
                    f"{uri!r} exists but has no provenance record on file — "
                    "call record_provenance after creating a planned_new "
                    "output before it can satisfy readiness"
                ),
            }
        return {**base, "ready": True, "status": "ready", "provenance": record}

    # kind == "existing"
    if not exists:
        return {**base, "ready": False, "status": "missing",
                "reason": f"no file exists at {uri!r}"}
    try:
        is_directory = bool(dir_checker(matched_path))
    except OSError:
        is_directory = False
    if is_directory:
        return {**base, "ready": False, "status": "is_directory",
                "reason": f"{uri!r} is a directory, not a file"}

    if figure_resolver is None:
        return {
            **base, "ready": True, "status": "unresolved",
            "reason": (
                "meridian-outputs is unavailable — existence verified on "
                "disk, but canonical/archival status could not be determined"
            ),
        }
    try:
        resolved = await figure_resolver(outputs_dir or "", matched_path)
    except Exception as exc:  # noqa: BLE001 — degrade, never fake success
        _log.debug(
            "verify_target_readiness: figure_resolver failed for %r",
            uri, exc_info=True,
        )
        return {**base, "ready": True, "status": "degraded",
                "reason": f"meridian-outputs resolve failed: {exc}"}
    if not resolved:
        return {
            **base, "ready": True, "status": "unresolved",
            "reason": (
                "existence verified on disk, but not found in the "
                "meridian-outputs index"
            ),
        }
    if resolved.get("is_archival"):
        return {
            **base, "ready": True, "status": "archival",
            "reason": "resolved output is flagged archival/stale by meridian-outputs",
            "resolved": resolved,
        }
    if resolved.get("match_type") == "basename" and int(resolved.get("candidate_count") or 0) <= 1:
        return {
            **base, "ready": True, "status": "unresolved",
            "reason": (
                "basename/label fallback found a candidate, but it is only "
                "diagnostic evidence and cannot prove exact output provenance"
            ),
            "resolved": resolved,
        }
    if resolved.get("match_type") == "basename" and int(resolved.get("candidate_count") or 0) > 1:
        return {
            **base, "ready": True, "status": "ambiguous",
            "reason": (
                f"{resolved.get('candidate_count')} same-basename candidates "
                "found in the meridian-outputs index — treat as a best "
                "guess, not a certain match"
            ),
            "resolved": resolved,
        }
    return {**base, "ready": True, "status": "canonical", "resolved": resolved}


async def verify_pointer_readiness(
    pointer: dict[str, Any],
    *,
    outputs_dir: str | None = None,
    path_exists: Callable[[str], bool] | None = None,
    is_dir: Callable[[str], bool] | None = None,
    figure_resolver: FigureResolver | None = None,
    provenance_getter: ProvenanceGetter | None = None,
) -> dict[str, Any]:
    """Fail-closed readiness verdict for EVERY target of a pointer.

    The pointer-level counterpart of :func:`verify_target_readiness`, this is
    the intended completion-time gate: run it over each of a sprint item's
    durable pointers (``db.get_sprint_item_pointers``) before letting
    ``complete_sprint_item`` pass, per the capability-manifest-style design
    contract in AGENTS.md ("Capability manifests & fallback contracts") —
    the primitive is built and fully tested here; wiring it into the
    ``complete_sprint_item`` DB gate itself is a follow-up, not part of this
    change (mirrors how 649e095f's capability manifest shipped ahead of its
    own enforcement wiring).

    Returns ``{source_type, label?, ready, targets: [<target-verdict>, ...]}``
    where ``ready`` is ``True`` iff the pointer has at least one target AND
    every target's own ``ready`` is ``True`` (an empty/malformed
    ``targets`` list is never vacuously ready). Never raises — a malformed
    (non-dict) target entry yields a ``ready=False`` "malformed_target"
    verdict for that slot rather than crashing the whole pass, matching
    :func:`resolve_pointer`'s own belt-and-suspenders guarding.
    """
    targets_raw = pointer.get("targets") if isinstance(pointer, dict) else None
    targets = targets_raw if isinstance(targets_raw, list) else []

    results: list[dict[str, Any]] = []
    for raw_target in targets:
        if not isinstance(raw_target, dict):
            results.append({
                "ready": False, "status": "malformed_target",
                "reason": "target is not an object",
            })
            continue
        results.append(await verify_target_readiness(
            raw_target,
            outputs_dir=outputs_dir,
            path_exists=path_exists,
            is_dir=is_dir,
            figure_resolver=figure_resolver,
            provenance_getter=provenance_getter,
        ))

    out: dict[str, Any] = {
        "source_type": pointer.get("source_type") if isinstance(pointer, dict) else None,
        "ready": bool(results) and all(r.get("ready") for r in results),
        "targets": results,
    }
    if isinstance(pointer, dict) and pointer.get("label") is not None:
        out["label"] = pointer.get("label")
    return out


def default_figure_resolver() -> FigureResolver:
    """Public accessor for the core-local default :data:`FigureResolver`
    (see :func:`_default_figure_resolver`) so callers OUTSIDE this module
    (e.g. ``handoff._annotate_resolved_pointers``'s per-pointer
    ``provenance_verified`` check, eb8b6894) can reuse the exact same
    default without reaching into a private name. Identical behaviour to
    ``_default_figure_resolver()`` — this is purely a public alias.
    """
    return _default_figure_resolver()


async def compute_pointer_readiness_for_record(
    pointer: dict[str, Any],
) -> "dict[str, Any] | None":
    """Best-effort :func:`verify_pointer_readiness` call for ONE stored
    pointer, using the core-local default figure_resolver
    (:func:`default_figure_resolver`) and no ``provenance_getter``
    (meridian-outputs' provenance ledger is extension-only — see the
    3196ba0e module section docstring above; a ``planned_new`` target will
    therefore consistently report ``provenance_unavailable`` here unless a
    richer caller injects its own ledger, which correctly fails CLOSED
    rather than assuming success).

    eb8b6894 — the ONE shared readiness-for-a-record step both
    :func:`build_item_pointer_records` (capability_contract's self-fetch
    path) and ``handoff._annotate_resolved_pointers`` (its own inline
    resolve loop) call, so ``provenance_verified`` can never independently
    drift between the XML /goal clause and the JSON capability contract.

    Guarded: returns ``None`` on any failure (never raises) — a caller
    passes that straight through to :func:`build_typed_pointer_record`,
    which already treats ``readiness=None`` as "not computed" rather than
    "verification failed".
    """
    try:
        return await verify_pointer_readiness(
            pointer, figure_resolver=default_figure_resolver(),
        )
    except Exception:  # noqa: BLE001 — best-effort, never breaks a mandatory pass
        _log.debug("compute_pointer_readiness_for_record failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# 3af86d28 — pointer REPAIR: re-resolution before a corrective handoff
# regeneration.
#
# A corrective handoff (meridian.handoff.record_handoff_correction /
# regenerate_handoff_correction) carries an ``added_pointers`` list — the
# evidence pointers the blocked executor found during its investigation.
# Before that evidence is trusted enough to invalidate the source handoff and
# generate a new revision, every added pointer must be independently
# RE-resolved (not merely re-validated for shape) against the live project —
# the whole point of the correction is that the world moved since the
# original handoff was rendered, so a pointer that looked fine when written
# may no longer resolve. :func:`repair_pointer_set` is that re-resolution
# pass: pure composition of the existing primitives above
# (:func:`validate_pointer` for shape, :func:`resolve_pointer` for live
# resolution) — it invents no new resolution logic of its own, so it can
# never drift from what a normal pointer-resolution call would find.
# ---------------------------------------------------------------------------


async def repair_pointer_set(
    db: Any,
    project_id: str,
    pointers: "list[dict[str, Any]] | None",
    **resolver_kwargs: Any,
) -> dict[str, Any]:
    """Re-resolve every pointer in ``pointers`` against the LIVE project.

    Used by a corrective handoff's regeneration step to repair the evidence
    pointers a blocked executor is asserting (``added_pointers``) before that
    evidence is trusted enough to invalidate the source handoff. Each pointer
    is first shape-validated (:func:`validate_pointer`) and then, if
    structurally valid, resolved live (:func:`resolve_pointer`) — the SAME
    resolver dispatch every other caller in this module uses, so a pointer
    that "repairs" here is resolved exactly as strictly as it would be
    anywhere else.

    ``resolver_kwargs`` are forwarded to :func:`resolve_pointer` verbatim
    (``symbol_resolver``, ``citation_resolver``, ``web_fetcher``, etc.) so
    callers/tests can inject stubs — no network or live Zotero touched by
    default, matching every other resolver call in this module.

    Never raises: a malformed or unresolvable pointer is sorted into
    ``unresolved`` with an explicit ``reason`` rather than aborting the whole
    repair pass (mirrors :func:`resolve_pointer`'s own never-raise contract).

    Returns::

        {
          "repaired": [<pointer dict, with a "resolution" key attached>, ...],
          "unresolved": [{"pointer": <original>, "reason": <str>}, ...],
          "repaired_count": int,
          "unresolved_count": int,
        }

    A pointer is "repaired" iff it is structurally valid AND every one of its
    targets resolved (``resolved: True``) — a pointer with even one
    unresolved target is NOT considered repaired (fail-closed: partial
    resolution is not evidence a receiving executor should trust silently).
    """
    repaired: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    for raw in pointers or []:
        if not isinstance(raw, dict):
            unresolved.append(
                {"pointer": raw, "reason": "malformed pointer entry (not an object)"}
            )
            continue
        try:
            validate_pointer(raw)
        except PointerValidationError as exc:
            unresolved.append({"pointer": raw, "reason": f"validation failed: {exc}"})
            continue
        try:
            resolution = await resolve_pointer(
                db, raw, project_id=project_id, **resolver_kwargs
            )
        except Exception as exc:  # noqa: BLE001 — never abort the whole pass
            _log.debug("repair_pointer_set: resolve_pointer failed", exc_info=True)
            unresolved.append({"pointer": raw, "reason": f"resolve error: {exc}"})
            continue
        targets = resolution.get("targets") or []
        fully_resolved = bool(targets) and all(
            isinstance(t, dict) and t.get("resolved") for t in targets
        )
        entry = {**raw, "resolution": resolution}
        if fully_resolved:
            repaired.append(entry)
        else:
            unresolved.append({
                "pointer": entry,
                "reason": "one or more targets did not resolve" if targets
                else "pointer has no targets",
            })

    return {
        "repaired": repaired,
        "unresolved": unresolved,
        "repaired_count": len(repaired),
        "unresolved_count": len(unresolved),
    }


# ---------------------------------------------------------------------------
# Warn/strict artifact-pointer POLICY evaluator (88f82c15 — b730 follow-up)
# ---------------------------------------------------------------------------
#
# :func:`verify_target_readiness` / :func:`verify_pointer_readiness` (3196ba0e,
# above) answer a COMPLETION-time, per-target, fail-closed question: "is this
# ALREADY-DECLARED target genuinely ready?" They never ask whether a pointer
# was declared in the first place, and they run on-demand, per pointer, with
# real filesystem/meridian-outputs I/O.
#
# :func:`evaluate_artifact_pointer_policy` answers a different, EARLIER
# question, at handoff-ANNOTATION time (``handoff._annotate_resolved_pointers``
# — see that function's own docstring for the wiring): "does this pending
# item even HAVE an exact enough output pointer for the kind of work it is,
# and what should happen if it does not?" It is pure/synchronous (no
# filesystem or meridian-outputs I/O — item state only), and it never
# duplicates classification or pointer-sufficiency logic: the figure/table
# verdict comes straight from :func:`meridian.artifact_classification.classify_artifact_work`
# (5fd9d2fd), and the "is this uri actually exact enough" judgment comes
# straight from that SAME module's ``_pointer_evidence``/
# ``_classify_uri_insufficiency``/``artifact_pointer_insufficiency_evidence``
# — lazily imported below (never at module scope: artifact_classification ->
# artifact_declaration -> pointers is already a cycle back to this module).
#
# Policy (``artifact_declaration.effective_artifact_policy``'s
# ``artifact_pointer_check``, default ``"warn"``) controls what happens with
# an insufficiency finding:
#
# * ``"strict"`` — the finding becomes an ACTIVE warning AND ``ready=False``
#   (non-executable) — a receiving executor must not call
#   ``complete_sprint_item`` on this item until the pointer is fixed or the
#   policy is relaxed.
# * ``"warn"`` (default) — the finding becomes an ACTIVE warning, but
#   ``ready=True`` — surfaced, never blocking.
# * ``"off"`` — the finding is SUPPRESSED (``warning_code``/
#   ``required_remediation``/``affected_pointer_ids`` all come back empty)
#   and ``ready=True``. The item's own ``classification``/``policy`` are
#   still returned in full either way — "off" withholds the ACTIVE warning
#   surface, it never discards the item's own declared/classified state.
#
# A figure/table item can NEVER self-declare its way out of this check:
# whether an item is "genuinely" document_only/caption_only/equation_only/
# embedded_docx_drawing/code_only is decided ENTIRELY by
# ``classify_artifact_work``'s own ``is_artifact_sensitive`` verdict (which
# already treats a human's declared ``artifact_kind`` as authoritative, per
# that module's own docstring) — this evaluator never consults
# ``policy.allow_document_only_override`` or any other flag to flip a
# sensitive verdict to non-sensitive. Only the classifier's own reasoning
# (declared kind, or conservative title/notes/pointer fallback evidence) can
# make an item non-sensitive; policy only controls what happens AFTER that.
# ---------------------------------------------------------------------------

def evaluate_artifact_pointer_policy(item: dict[str, Any]) -> dict[str, Any]:
    """Warn/strict policy evaluator for ONE pending sprint item's artifact
    pointer, run at handoff-annotation time (see
    ``handoff._annotate_resolved_pointers``).

    Returns a dict always carrying exactly:

    * ``item_id`` — the item's id (``None`` when absent/malformed).
    * ``classification`` — the FULL :func:`meridian.artifact_classification.classify_artifact_work`
      result (classification/is_artifact_sensitive/confidence/ambiguous/
      rule/evidence) — never re-derived independently.
    * ``policy`` — the FULL :func:`meridian.artifact_declaration.effective_artifact_policy`
      result (the item's own declared policy merged over the project
      default) — an executor reasoning about enforcement needs the resolved
      answer, not "whatever this one item happened to set".
    * ``warning_code`` — ``None`` when not applicable (not artifact-
      sensitive, or a concrete figure/table pointer is already on file, or
      policy is ``"off"``); otherwise one of
      :data:`meridian.artifact_classification.INSUFFICIENT_MISSING_POINTER` /
      ``INSUFFICIENT_BARE_DOCX`` / ``INSUFFICIENT_DIRECTORY`` /
      ``INSUFFICIENT_GENERIC_REFERENCE`` / ``INSUFFICIENT_UNSUPPORTED_TYPE``.
    * ``required_remediation`` — a human-readable fix instruction for
      ``warning_code`` (``None`` iff ``warning_code`` is ``None``).
    * ``affected_pointer_ids`` — durable ``sprint_item_pointers`` row ids
      implicated by the dominant insufficiency reason (``[]`` when there is
      nothing to name, e.g. ``missing_pointer`` or no active warning).
    * ``ready`` — ``True`` unless policy is ``"strict"`` AND
      ``warning_code`` is active (non-``None``) — the handoff-readiness/
      non-executable signal a receiving executor checks before doing any
      work, mirroring ``capability_contract``/``executor_contract``'s own
      ``executable`` convention.

    Never raises: every sub-step (classification, policy resolution,
    pointer-evidence scan) is individually guarded and degrades to the
    least-informative branch rather than breaking the mandatory handoff this
    feeds — a malformed ``item`` (not a dict, missing fields) returns a
    clean, non-warning, ``ready=True`` result.
    """
    from . import artifact_classification as _artifact_classification  # noqa: PLC0415 — avoid import cycle
    from . import artifact_declaration as _artifact_declaration  # noqa: PLC0415 — avoid import cycle

    if not isinstance(item, dict):
        item = {}
    item_id = item.get("id")

    try:
        classification = _artifact_classification.classify_artifact_work(item)
    except Exception:  # noqa: BLE001 — never let a bad item break annotation
        classification = {
            "classification": _artifact_classification.AMBIGUOUS,
            "is_artifact_sensitive": False,
            "confidence": "low",
            "ambiguous": True,
            "rule": "fallback_error",
            "evidence": ["classification raised — treated as unknown"],
        }

    try:
        policy = _artifact_declaration.effective_artifact_policy(item)
    except Exception:  # noqa: BLE001
        policy = _artifact_declaration.default_artifact_policy()
    mode = policy.get("artifact_pointer_check")
    if mode not in _artifact_declaration.ARTIFACT_POINTER_CHECK_LEVELS:
        mode = _artifact_declaration.DEFAULT_ARTIFACT_POINTER_CHECK

    result: dict[str, Any] = {
        "item_id": item_id,
        "classification": classification,
        "policy": policy,
        "warning_code": None,
        "required_remediation": None,
        "affected_pointer_ids": [],
        "ready": True,
    }

    # Not artifact-sensitive (per the classifier's OWN verdict — declared
    # artifact_kind, or conservative fallback evidence): genuinely safe,
    # never warns. No policy flag can reach this branch for a sensitive item.
    if not classification.get("is_artifact_sensitive"):
        return result

    try:
        pointer_kind, _hits, _mixed = _artifact_classification._pointer_evidence(item)
    except Exception:  # noqa: BLE001
        pointer_kind = None
    if pointer_kind is not None:
        return result  # a concrete figure/table pointer is already on file

    try:
        reason_code, affected_ids = (
            _artifact_classification.artifact_pointer_insufficiency_evidence(item)
        )
    except Exception:  # noqa: BLE001
        reason_code, affected_ids = None, []
    if reason_code is None:
        # Zero candidate uris at all — "there is no pointer", distinct from
        # "there is a pointer but it's the wrong shape".
        reason_code = _artifact_classification.INSUFFICIENT_MISSING_POINTER
        affected_ids = []

    if mode == "off":
        # Suppressed: classification/policy above are still fully populated
        # ("raw declarations... preserved") — only the ACTIVE warning
        # surface is withheld.
        return result

    result["warning_code"] = reason_code
    result["required_remediation"] = _artifact_classification._INSUFFICIENCY_REMEDIATION.get(
        reason_code,
        _artifact_classification._INSUFFICIENCY_REMEDIATION[
            _artifact_classification.INSUFFICIENT_MISSING_POINTER
        ],
    )
    result["affected_pointer_ids"] = affected_ids
    result["ready"] = mode != "strict"
    return result


# ---------------------------------------------------------------------------
# Machine-readable artifact-pointer FINDING projection (70c10ca3 — b730
# follow-up: consumes 3196ba0e, extends 88f82c15)
# ---------------------------------------------------------------------------
#
# :func:`evaluate_artifact_pointer_policy` (88f82c15, above) answers the
# warn/strict POLICY question at handoff-annotation time and stays
# deliberately pure (no filesystem/meridian-outputs I/O — see its own
# docstring). It never says whether the INSUFFICIENT pointer it flagged is
# itself missing on disk, stale/archival, or ambiguous — that is a separate,
# I/O-backed question :func:`verify_target_readiness` /
# :func:`verify_pointer_readiness` (3196ba0e, above) already answer, but
# neither primitive had ever been wired to the other.
#
# :func:`build_artifact_pointer_finding` is the seam that combines them into
# ONE canonical, machine-readable finding per item — reused, verbatim, by
# EVERY handoff representation (batch /goal XML, capability_contract JSON,
# generate_handoff response metadata) so a receiving executor never has to
# reconcile "the same warning" described independently in three places.
# Mirrors 665's own precedent of "one shared extraction, many renderers"
# (see :func:`build_typed_pointer_record` /
# :func:`assemble_pointer_entries_from_annotated_items` above).
# ---------------------------------------------------------------------------


async def build_artifact_pointer_finding(
    item: dict[str, Any],
    *,
    policy_result: "dict[str, Any] | None" = None,
    stored_pointers: "list[dict[str, Any]] | None" = None,
    outputs_dir: str | None = None,
    figure_resolver: "FigureResolver | None" = None,
    provenance_getter: "ProvenanceGetter | None" = None,
) -> "dict[str, Any] | None":
    """Combine :func:`evaluate_artifact_pointer_policy`'s warn/strict verdict
    with :func:`verify_pointer_readiness`'s fail-closed provenance/existence
    check for ONE item's implicated pointers.

    ``policy_result`` lets a caller pass an ALREADY-COMPUTED
    ``evaluate_artifact_pointer_policy(item)`` result (e.g.
    ``handoff._annotate_resolved_pointers``, which already ran it earlier in
    the same annotation pass) instead of recomputing it here; when omitted,
    this function computes it itself from ``item``.

    ``stored_pointers`` should be ``item``'s durable, RAW
    ``sprint_item_pointers`` rows (``db.get_sprint_item_pointers`` /
    :func:`row_to_pointer` shape — each carrying ``{id, targets:
    [{uri, selector, target_kind}, ...]}``), used to resolve each
    ``affected_pointer_ids`` entry to its actual target(s) for readiness
    verification. ``None``/``[]`` degrades to an empty ``target_readiness``
    (the policy verdict alone is still returned).

    ``outputs_dir`` / ``figure_resolver`` / ``provenance_getter`` are the
    SAME injectable meridian-outputs seams :func:`verify_target_readiness`
    takes. ``figure_resolver`` defaults to :func:`_default_figure_resolver`
    (core-local, always safe to call) when not explicitly given, so this
    actually CONSUMES the 3196ba0e primitive by default rather than always
    degrading to the "figure_resolver is None -> unavailable" branch;
    ``provenance_getter`` has no core-local default (meridian-outputs' own
    provenance ledger lives only in the extension) and stays ``None`` unless
    a caller injects one.

    Returns ``None`` when there is no ACTIVE warning (mirrors
    ``evaluate_artifact_pointer_policy``'s / the existing
    ``<artifact_pointer_policy>`` XML clause's own "nothing to say"
    restraint) — an item with sufficient/exact pointer evidence, or one
    that's simply not artifact-sensitive, contributes nothing here either.

    Otherwise returns the FULL ``evaluate_artifact_pointer_policy`` verdict
    (item_id/classification/policy/warning_code/required_remediation/
    affected_pointer_ids/ready) plus:

    * ``pointer_status`` — ``"missing"`` (warning_code is
      ``missing_pointer`` — no candidate pointer at all) or ``"weak"`` (a
      pointer exists but is not exact enough — every other insufficiency
      code). The "exact" case never reaches here at all: it is exactly the
      case with no active warning, handled by the early ``None`` return.
    * ``target_readiness`` — ``[{"pointer_id": ..., <verify_pointer_readiness
      result fields>}, ...]`` for each id in ``affected_pointer_ids`` that
      has a matching entry in ``stored_pointers`` (matched by id), sorted by
      ``pointer_id`` for deterministic output. Empty when
      ``affected_pointer_ids`` is empty (e.g. ``missing_pointer`` — there is
      no durable row to verify) or ``stored_pointers`` was not supplied.

    Never raises: every sub-step is individually guarded, matching
    ``evaluate_artifact_pointer_policy``'s own "must never break a mandatory
    handoff" contract. A ``verify_pointer_readiness`` failure for one
    affected pointer degrades that ONE entry to an explicit
    ``"verification_error"`` status rather than dropping the whole finding.
    """
    if isinstance(policy_result, dict):
        finding_src = policy_result
    else:
        try:
            finding_src = evaluate_artifact_pointer_policy(item)
        except Exception:  # noqa: BLE001 — never break a mandatory handoff
            return None

    warning_code = finding_src.get("warning_code")
    if not warning_code:
        return None

    from . import artifact_classification as _artifact_classification  # noqa: PLC0415 — avoid import cycle

    finding: dict[str, Any] = dict(finding_src)
    finding["pointer_status"] = (
        "missing"
        if warning_code == _artifact_classification.INSUFFICIENT_MISSING_POINTER
        else "weak"
    )

    resolver = (
        figure_resolver if figure_resolver is not None else _default_figure_resolver()
    )

    target_readiness: list[dict[str, Any]] = []
    affected_ids = finding.get("affected_pointer_ids") or []
    if affected_ids and stored_pointers:
        by_id: dict[str, dict[str, Any]] = {}
        for p in stored_pointers:
            if isinstance(p, dict) and p.get("id"):
                by_id[str(p["id"])] = p
        for pid in sorted({str(x) for x in affected_ids}):
            ptr = by_id.get(pid)
            if ptr is None:
                continue
            try:
                verdict = await verify_pointer_readiness(
                    ptr,
                    outputs_dir=outputs_dir,
                    figure_resolver=resolver,
                    provenance_getter=provenance_getter,
                )
            except Exception as exc:  # noqa: BLE001 — degrade, never break the batch
                _log.debug(
                    "build_artifact_pointer_finding: readiness check failed for %r",
                    pid, exc_info=True,
                )
                verdict = {
                    "ready": False, "status": "verification_error",
                    "reason": f"readiness verification raised: {exc}",
                }
            target_readiness.append({"pointer_id": pid, **verdict})
    finding["target_readiness"] = target_readiness
    return finding


def assemble_artifact_pointer_findings_from_annotated_items(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pure, synchronous assembly of the canonical per-item artifact-pointer
    findings from items ALREADY annotated with ``artifact_pointer_finding``
    (see ``handoff._annotate_resolved_pointers``, which sets it via
    :func:`build_artifact_pointer_finding` in the SAME resolve pass this
    module's :func:`build_item_pointer_records` powers).

    Only items carrying a non-``None`` ``artifact_pointer_finding`` (an
    ACTIVE warning) contribute — mirrors that function's own "nothing to
    say" restraint, so an ordinary /goal with no figure/table pointer
    problems produces zero entries, never noise. Sorted by ``item_id`` for
    deterministic byte-for-byte output; each entry's own ``target_readiness``
    sub-list is already sorted by ``pointer_id`` (set by
    :func:`build_artifact_pointer_finding`).

    Shared by ``handoff._build_artifact_pointer_findings_clause`` (the
    /goal block's ``<artifact_pointer_findings>`` XML clause) and
    ``capability_contract.extract_artifact_pointer_findings`` (the JSON
    ``item_artifact_pointer_findings`` section, pre-annotated fast path) —
    70c10ca3 (b730 follow-up) — so neither maintains its own independent
    derivation of "which items make the cut."
    """
    entries: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if not it.get("id"):
            continue
        finding = it.get("artifact_pointer_finding")
        if isinstance(finding, dict):
            entries.append(finding)
    return sorted(entries, key=lambda e: e.get("item_id") or "")


# ---------------------------------------------------------------------------
# 3b3020ac -- execution-manifest-backed readiness.
#
# meridian.executor_contract.aggregate_worker_completions() (a hash-pinned,
# fail-closed aggregation over a scientific fan-out run's per-worker
# completion records) is consumed here as a PLAIN DICT -- this module
# deliberately never imports meridian.executor_contract at module scope:
# executor_contract already imports meridian.capability_contract, which in
# turn imports THIS module for pointer extraction, so an eager import here
# would be a real cycle. Duck-typing the aggregation shape (``ok``,
# ``status``, ``worker_records: {worker_id: {output_hashes: {path: sha256}, ...}}``)
# keeps this a genuinely thin adapter: no re-derivation of aggregation
# logic, just one more fail-closed cross-check layered on top of
# :func:`verify_target_readiness`'s existing disk-presence gate.
#
# Completion/provenance gates must consume the manifest rather than trust
# narrative notes or directory presence (sprint spec) -- this is that
# consumption point for the pointers.py readiness primitive specifically.
# ---------------------------------------------------------------------------

def _local_sha256_file(path: str) -> "str | None":
    """Minimal, dependency-free sha256-of-a-file helper (mirrors
    ``meridian.executor_contract.hash_file_set``'s per-file semantics; not
    imported from there to avoid the cycle described above). ``None`` on any
    read failure -- never raises."""
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


async def verify_execution_manifest_target_readiness(
    target: dict[str, Any],
    aggregation: "dict[str, Any] | None",
    *,
    outputs_dir: str | None = None,
    path_exists: Callable[[str], bool] | None = None,
    is_dir: Callable[[str], bool] | None = None,
    hash_file: Callable[[str], "str | None"] | None = None,
    figure_resolver: FigureResolver | None = None,
    provenance_getter: ProvenanceGetter | None = None,
) -> dict[str, Any]:
    """Fail-closed completion-time readiness for a pointer target that is
    supposed to be backed by a hash-pinned execution-manifest aggregation
    (see the module section docstring above), rather than by directory
    presence alone.

    Reuses :func:`verify_target_readiness` VERBATIM for the disk-level half
    of the check (existence, is_directory, meridian-outputs
    canonical/archival classification, planned_new provenance) -- this
    function only ADDS a manifest cross-check on top, never duplicating any
    of that logic:

    1. If the base disk-level check is not ``ready``, that verdict is
       returned unchanged (with ``manifest_verified=False`` and an
       explanatory ``manifest_reason``) -- a manifest can never rescue a
       target that genuinely does not exist on disk.
    2. If ``aggregation`` is missing or not ``ok`` (per
       ``executor_contract.aggregate_worker_completions``'s own fail-closed
       verdict), the target is downgraded to ``ready=False`` even if the
       disk-level check passed -- directory presence alone is never
       sufficient evidence for a target that claims manifest backing.
    3. Otherwise, the target's ``uri`` (tried under every normalized local
       path spelling — :func:`normalize_local_uri_candidates`, the SAME
       normalization :func:`verify_target_readiness` itself already uses)
       must appear in ``aggregation["worker_records"]``'s recorded
       ``output_hashes``, and the file's CURRENT on-disk hash
       (:func:`_local_sha256_file`, or an injected ``hash_file``) must match
       the recorded one — a target whose bytes changed since the run
       completed, or that was never part of the run's own recorded outputs
       at all, is refused.

    Returns the SAME dict :func:`verify_target_readiness` returns, plus two
    additive keys: ``manifest_verified`` (bool) and ``manifest_reason``
    (``None`` iff ``manifest_verified`` is ``True``). Never raises.
    """
    base = await verify_target_readiness(
        target, outputs_dir=outputs_dir, path_exists=path_exists, is_dir=is_dir,
        figure_resolver=figure_resolver, provenance_getter=provenance_getter,
    )
    if not base.get("ready"):
        return {
            **base, "manifest_verified": False,
            "manifest_reason": "disk-level readiness already failed — see 'reason'",
        }

    if not isinstance(aggregation, dict) or not aggregation.get("ok"):
        status = aggregation.get("status") if isinstance(aggregation, dict) else None
        return {
            **base, "ready": False, "manifest_verified": False,
            "manifest_reason": (
                "no ok execution-manifest aggregation was supplied "
                f"(status={status!r}) — directory presence alone is not "
                "sufficient evidence; consume "
                "executor_contract.aggregate_worker_completions()'s result"
            ),
        }

    uri = ""
    if isinstance(target, dict):
        raw_uri = target.get("uri")
        uri = raw_uri.strip() if isinstance(raw_uri, str) else ""
    candidates = set(normalize_local_uri_candidates(uri))

    recorded_hash: "str | None" = None
    matched_path: "str | None" = None
    for rec in (aggregation.get("worker_records") or {}).values():
        if not isinstance(rec, dict):
            continue
        for out_path, out_hash in (rec.get("output_hashes") or {}).items():
            if out_path in candidates:
                recorded_hash, matched_path = out_hash, out_path
                break
        if recorded_hash is not None:
            break

    if recorded_hash is None:
        return {
            **base, "ready": False, "manifest_verified": False,
            "manifest_reason": (
                f"{uri!r} is not among the execution-manifest aggregation's "
                "recorded worker output hashes — no manifest-backed evidence "
                "this file is genuine run output"
            ),
        }

    hasher = hash_file or _local_sha256_file
    try:
        current_hash = hasher(matched_path or uri)
    except Exception as exc:  # noqa: BLE001 — degrade, never fake success
        return {
            **base, "ready": False, "manifest_verified": False,
            "manifest_reason": f"could not hash {uri!r} to verify against the manifest: {exc}",
        }

    if current_hash != recorded_hash:
        return {
            **base, "ready": False, "manifest_verified": False,
            "manifest_reason": (
                f"current on-disk hash of {uri!r} ({current_hash!r}) does not "
                f"match the manifest-recorded output hash ({recorded_hash!r}) "
                "— the file changed since the run completed"
            ),
        }

    return {**base, "manifest_verified": True, "manifest_reason": None}
