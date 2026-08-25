"""Durable, structural receipt for code-intel prospecting (a8c0f3b7).

The gap this closes: ``meridian/mcp/handler.py::_prospect_code_context`` (the
``code_context``/``hint`` field on a freshly-claimed sprint item) and
``routes/tunnel.py``'s ``_CODE_INTEL_FIRST_GUIDANCE`` are both PROSE nudges --
"prospect before editing", "call search_graph first". A live executor
transcript still used broad Read/grep/``git show``/PowerShell ``Get-Content``,
or spawned a sub-agent that never touched code-intel at all, and there was no
way to tell after the fact whether real semantic prospecting happened before
the edit. The existing ``.claude/hooks/code_intel_guard.sh`` PreToolUse hook
(see ``tests/test_code_intel_guard.py``) blocks raw Grep/Glob at the CLI layer
for ONE surface (Claude Code), but a Read tool call, a shell ``git show`` /
``Get-Content``, or a sub-agent that calls Serena/codebase-memory-mcp through
a connection Meridian never sees are all structurally invisible to it.

This module does NOT try to block those paths -- that would overclaim (see
the module docstring warning below). Instead it builds an audit RECEIPT: a
durable, machine-checkable row proving "a genuine code-intel prospecting call
was made for this project" -- and a verification gate
(:func:`verify_code_intel_prospecting`) that ``complete_sprint_item`` consults
before marking an item done. Reuses the existing, already-migrated
``action_audit_log`` table (5dfe34b2/cd495afa -- same append-only audit
pattern as ``sprint_evidence_guard``'s strict-evidence overrides and the
manual-issue-screening toggle log) rather than inventing a new table: no new
migration, no SQLite/Postgres parity work needed.

**Harden, do not overclaim.** This is explicitly NOT a hard requirement for
every project or every sprint item:

* A sprint item that never declared ``touches_resources`` was never a real
  prospecting candidate in the first place (mirrors ``claim_sprint_item``'s
  own UNPROSPECTED gate scope guard, :func:`meridian.db.sprint_items.
  _item_declares_resources`) -- not gated here either.
* A human-set ``prospect_bypass`` on the item is honoured, same as the claim
  gate.
* **Opt-in via the project's capability manifest**, not a global switch: the
  gate is a no-op (``applicable=False``, zero behavior change) unless the
  project has declared a capability with id
  :data:`CODE_INTEL_CAPABILITY_ID` (``"code_intel_prospecting"``) via
  ``set_capability_manifest`` -- "old projects are not broken by this feature
  existing" (AGENTS.md's capability-manifest contract, 649e095f).
* When that capability IS declared, its ``availability_policy`` (``required``
  / ``optional`` / ``degraded_ok``) governs what happens when code-intel
  itself is unavailable (fail closed only for ``required``) or when it WAS
  available but no receipt was recorded (fail closed for ``required``, warn
  and degrade for ``optional``/``degraded_ok``) -- reusing
  :mod:`meridian.capability_availability` / :func:`meridian.mcp.handlers.
  project_tools.check_capability_availability` rather than re-implementing
  the required/optional/degraded_ok posture from scratch.

**Structural, not self-report.** The receipt is written by the SERVER's own
tool-dispatch code (see the two call sites in ``meridian/mcp/handler.py``:
the native ``prospect_symbol`` branch of ``_handle_code_index_tools``, and
the tunnel-forward chokepoint inside ``_handle_mcp_request``'s
``tools/call`` handling -- the ONE place every tool call over a Meridian MCP
connection passes through, tunneled or native) -- never by the calling agent
declaring "yes, I searched". A bare Read/grep/``git show``/``Get-Content``
call, or a sub-agent that never routes a code-intel call through this
connection, simply never reaches either receipt-writing call site, so no row
is ever written for that work -- :func:`verify_code_intel_prospecting` then
correctly reports no receipt, exactly the "cannot silently evade" property
the item asks for.

**Known, documented limitation** (do not overclaim this either): a
tunnel-forwarded code-intel tool call (``codebase__search_graph``,
``extractor__find_symbol``, ...) is a THIRD-PARTY tool schema that does not
carry Meridian's own project UUID, so :func:`resolve_receipt_project_id`
falls back to the self-hosted default-project convention
(``toml_config.get_default_project_id()`` / ``MERIDIAN_PROJECT_ID`` --
AGENTS.md's own "Auto-scoping to a single project" feature) to attribute the
receipt. A hosted, multi-project tenant with no default project configured
and a caller that never passes a UUID-shaped ``project_id`` on the call gets
no receipt attribution at all for THAT call (``resolve_receipt_project_id``
returns ``None`` and the write is skipped) -- a real, acknowledged gap, not a
silent false-positive: the completion-time gate then correctly reports "no
receipt found" rather than fabricating one.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from . import db as db_module

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

# MDE-2 — identity-binding markers, so a receipt can be REJECTED (not just
# recorded), instead of treated as evidence for a project it never actually
# touched. See ``is_contaminated_repo_path`` / ``resolve_receipt_repo_root``.
#
# ``.codex/worktrees/...`` is a DIFFERENT agent tool's own isolated scratch
# checkout convention (Codex, running directly in a shared main checkout per
# this session's own launch context) -- distinct from Meridian's own
# ``.claude/worktrees/...`` convention (see worktree_cleanup.py's
# ``looks_like_worktree_path``, which a genuine Meridian-registered worktree
# always matches instead). A prospecting call resolved against a sibling
# tool's temp checkout says nothing about whether prospecting happened
# against THIS session's actual code -- excluded outright, never silently
# accepted as if it were an ordinary worktree.
_CONTAMINATION_MARKERS = ("/.codex/worktrees/",)

#: event_type recorded in action_audit_log for a genuine prospecting receipt.
RECEIPT_EVENT_TYPE = "code_intel_prospect_receipt"

#: event_type recorded in action_audit_log for an audited override of a
#: blocked (missing-receipt / unavailable) completion.
OVERRIDE_EVENT_TYPE = "code_intel_receipt_override"

#: Well-known capability id a project's manifest opts in with (see
#: set_capability_manifest / meridian.capability_manifest). Absent from a
#: project's manifest -> this whole module is a no-op for that project.
CODE_INTEL_CAPABILITY_ID = "code_intel_prospecting"

#: Bare (unprefixed) tool names that count as a genuine code-intel
#: prospecting call. ``prospect_symbol`` is the promoted single entry point
#: (agent_defaults.py v12 -- "call prospect_symbol FIRST"); the rest mirror
#: the codebase-memory-mcp / Serena tools code_intel_guard.sh's stderr
#: already names as the correct alternative to grep/glob, plus the local
#: BM25 fallback (search_code_semantic).
CODE_INTEL_RECEIPT_TOOLS = frozenset({
    "prospect_symbol",
    "search_graph", "query_graph", "trace_path", "get_architecture",
    "search_code", "get_code_snippet",
    "find_symbol", "find_declaration", "find_implementations",
    "find_referencing_symbols", "get_symbols_overview",
    "search_code_semantic",
})

# Keys, in priority order, that plausibly carry the symbol/query text across
# the different (Meridian-native + third-party) code-intel tool schemas.
_QUERY_HINT_KEYS = ("symbol", "query", "name_path", "symbol_name", "name")


def bare_tool_name(name: str) -> str:
    """Strip a tunnel slot prefix (``codebase__search_graph`` -> ``search_graph``)."""
    return name.split("__", 1)[1] if isinstance(name, str) and "__" in name else (name or "")


def is_code_intel_receipt_tool(name: str) -> bool:
    """True when *name* (prefixed or bare) is a recognized prospecting call."""
    return bare_tool_name(name) in CODE_INTEL_RECEIPT_TOOLS


def extract_query_hint(args: "dict[str, Any] | None") -> str:
    """Best-effort short text describing what a prospecting call searched for."""
    if not isinstance(args, dict):
        return ""
    for key in _QUERY_HINT_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:200]
    return ""


def extract_query_hint_full(args: "dict[str, Any] | None") -> str:
    """MDE-2 rework — same field lookup as :func:`extract_query_hint`, but
    WITHOUT the 200-char truncation. ``extract_query_hint``'s truncation is
    fine for the receipt's ``query`` diagnostic field, but truncating BEFORE
    comparing against a hit's own reported identity would make an exact-name
    match structurally impossible for any symbol whose qualified name is
    itself >200 chars (a real, if uncommon, shape for a deeply-nested
    qualified name) — this is the untruncated string exact-match selection
    (:func:`meridian.prospect.select_exact_hit`) must be compared against.
    """
    if not isinstance(args, dict):
        return ""
    for key in _QUERY_HINT_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def resolve_receipt_project_id(args: "dict[str, Any] | None") -> "str | None":
    """Best-effort resolution of the MERIDIAN project id a receipt belongs to.

    Code-intel graph tools identify a project by a LOCAL REPO-PATH SLUG (see
    ``routes/tunnel.py``'s ``_CODE_INTEL_PROJECT_TOOLS`` error enrichment) --
    a different identifier from Meridian's own project UUID, so a tool call's
    own ``project_id``/``project`` argument cannot be trusted as Meridian's
    id. Prefers the self-hosted default-project convention
    (``toml_config.get_default_project_id()`` -- the same resolution
    ``start_session`` already falls back to, AGENTS.md's "Auto-scoping to a
    single project"), since that genuinely IS Meridian's id; falls back to a
    UUID-shaped ``project_id`` passed directly on the call (covers a caller
    that happens to pass the real Meridian id) only when no default is
    configured. Returns ``None`` when neither resolves -- callers must treat
    that as "cannot attribute this receipt", not silently guess.
    """
    from . import toml_config as _toml_config  # noqa: PLC0415

    default_pid = _toml_config.get_default_project_id()
    if default_pid:
        return default_pid
    if isinstance(args, dict):
        pid = str(args.get("project_id") or "").strip()
        if _UUID_RE.match(pid):
            return pid
    return None


def normalize_repo_root(path: "str | None") -> "str | None":
    """Canonicalize a filesystem path for stable repo-identity comparison.

    Falls back to a stripped string for anything that isn't resolvable on
    this machine (matches ``worktree_code_intel_context.normalize_context_
    path``'s own fallback posture) rather than raising.
    """
    if not path:
        return None
    try:
        resolved = str(Path(str(path)).expanduser().resolve())
        return resolved or None
    except Exception:  # noqa: BLE001
        text = str(path).strip()
        return text or None


def is_contaminated_repo_path(path: "str | None") -> bool:
    """True when *path* passes through another agent tool's own isolated
    scratch-checkout convention (``.codex/worktrees/...``) rather than this
    project's own repo, or Meridian's own ``.claude/worktrees/...``
    convention. See the module-level ``_CONTAMINATION_MARKERS`` docstring.
    """
    if not path:
        return False
    normalized = "/" + str(path).replace("\\", "/").strip("/").lower() + "/"
    return any(marker in normalized for marker in _CONTAMINATION_MARKERS)


def _git_head_sync(root_dir: str) -> "str | None":
    """Best-effort, synchronous ``git rev-parse HEAD`` -- MUST be called from
    a worker thread (see ``_git_revision``), never awaited directly: asyncio
    subprocess creation is unsupported on the Windows SelectorEventLoop this
    project forces (see ``__main__.py`` / ``git_md.py``'s identical rationale).
    Never raises; returns ``None`` for anything that isn't a clean git repo.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root_dir, capture_output=True, text=True, timeout=3,
        )
        if proc.returncode != 0:
            return None
        sha = (proc.stdout or "").strip()
        return sha or None
    except Exception:  # noqa: BLE001 -- identity probe must never raise
        return None


async def _git_revision(root_dir: "str | None") -> "str | None":
    if not root_dir:
        return None
    try:
        return await asyncio.to_thread(_git_head_sync, root_dir)
    except Exception:  # noqa: BLE001
        return None


def _hash_file_sync(root_dir: str, resolved_file: str) -> "str | None":
    try:
        p = Path(resolved_file)
        if not p.is_absolute():
            p = Path(root_dir) / resolved_file
        if not p.is_file():
            return None
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except Exception:  # noqa: BLE001
        return None


async def compute_source_hash(
    root_dir: "str | None", resolved_file: "str | None"
) -> "str | None":
    """sha256 of *resolved_file*'s current bytes under *root_dir*, or ``None``
    when either is missing or the file can't be read. Off the event loop
    (worker thread), same rationale as ``_git_head_sync``."""
    if not root_dir or not resolved_file:
        return None
    try:
        return await asyncio.to_thread(_hash_file_sync, root_dir, resolved_file)
    except Exception:  # noqa: BLE001
        return None


def compute_graph_hash(hit: "dict[str, Any] | None") -> "str | None":
    """MDE-2 rework — sha256 of a hit's OWN inline body/snippet text (see
    :func:`meridian.prospect.hit_content`), when it carries one. ``None``
    when the hit has no inline text at all (most graph/serena hits only
    report a location, not a body) — never fabricated. Paired with
    :func:`compute_live_range_hash` for the "graph and live-file hashes
    agree" check.
    """
    if not isinstance(hit, dict):
        return None
    try:
        from .prospect import hit_content as _hit_content  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    text = _hit_content(hit)
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_line_range_sync(
    root_dir: str, resolved_file: str, start_line: "int | None", end_line: "int | None",
) -> "str | None":
    try:
        if start_line is None:
            return None
        p = Path(resolved_file)
        if not p.is_absolute():
            p = Path(root_dir) / resolved_file
        if not p.is_file():
            return None
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        start = max(1, int(start_line))
        end = max(start, int(end_line) if end_line is not None else start)
        chunk = "".join(lines[start - 1:end])
        if not chunk:
            return None
        return hashlib.sha256(chunk.encode("utf-8")).hexdigest()
    except Exception:  # noqa: BLE001
        return None


async def compute_live_range_hash(
    root_dir: "str | None", resolved_file: "str | None",
    start_line: "int | None", end_line: "int | None",
) -> "str | None":
    """MDE-2 rework — sha256 of the LIVE file's bytes for exactly
    ``[start_line, end_line]`` (1-indexed, inclusive) under *root_dir* — the
    counterpart to :func:`compute_graph_hash`'s hash of the graph/index's
    OWN reported body for the same location.

    Comparing the two is the "graph and live-file hashes agree" mechanism
    the acceptance bar asks for: equal hashes mean the indexed body still
    matches what's really on disk right now (fresh); different hashes are a
    concrete, positive staleness signal — the graph/index captured a body
    that no longer matches this exact range (a subsequent edit, or the
    project reverting to a different revision since the hit was captured) —
    distinct from (and finer-grained than) :func:`compute_source_hash`'s
    WHOLE-FILE hash, which this function complements rather than replaces.

    ``None`` (never computed, never a false "disagreement") when any input
    is missing or the range/file can't be read — off the event loop (worker
    thread), same rationale as ``compute_source_hash``.
    """
    if not root_dir or not resolved_file or start_line is None:
        return None
    try:
        return await asyncio.to_thread(
            _hash_line_range_sync, root_dir, resolved_file, start_line, end_line,
        )
    except Exception:  # noqa: BLE001
        return None


async def resolve_receipt_repo_root(
    db: Any,
    *,
    session_id: "str | None",
    root_dir: "str | None" = None,
    default_repo_root: "str | None" = None,
) -> "dict[str, Any]":
    """Resolve the repo identity a prospecting receipt should bind to.

    Preference order, mirroring ``test_run_receipt.resolve_repo_root_for_
    session``'s already-established "session's own registered worktree wins
    over the server's default checkout" technique (same root cause: a
    parallel-worktree executor session's real work lives in ITS OWN
    checkout, not the server process's ambient one):

    1. An explicit ``root_dir`` the caller already resolved for this exact
       tool call (e.g. the tunnel's per-tenant active-repo cache, or
       ``prospect_symbol``'s own ``root_dir`` argument).
    2. The session's registered ``active_worktrees`` row, resolved to a real
       disk path via ``worktree_cleanup.resolve_worktree_disk_path``.
    3. ``default_repo_root`` (typically the server's own main checkout).

    Returns ``{"repo_root", "source", "contaminated"}``. ``repo_root`` is
    ``None`` (``source="unresolved"``) when nothing above resolves --
    callers must treat that as "identity unknown", never as a mismatch.
    Never raises.
    """
    explicit = normalize_repo_root(root_dir)
    if explicit:
        return {
            "repo_root": explicit, "source": "explicit_root_dir",
            "contaminated": is_contaminated_repo_path(explicit),
        }
    if session_id:
        try:
            from . import worktree_cleanup as _worktree_cleanup  # noqa: PLC0415

            wt = await db_module.get_active_worktree_for_session(db, session_id)
            if wt and wt.get("path"):
                base = normalize_repo_root(default_repo_root) or "."
                wt_abs = _worktree_cleanup.resolve_worktree_disk_path(
                    Path(base), wt["path"],
                )
                resolved = normalize_repo_root(str(wt_abs))
                if resolved:
                    return {
                        "repo_root": resolved, "source": "session_worktree",
                        "contaminated": is_contaminated_repo_path(resolved),
                    }
        except Exception:  # noqa: BLE001 -- unresolvable worktree isn't itself a failure
            pass
    default = normalize_repo_root(default_repo_root)
    if default:
        return {
            "repo_root": default, "source": "default_repo_root",
            "contaminated": is_contaminated_repo_path(default),
        }
    return {"repo_root": None, "source": "unresolved", "contaminated": False}


async def record_prospect_receipt(
    db: Any,
    *,
    tenant_id: "str | None",
    project_id: "str | None",
    session_id: "str | None",
    tool_name: str,
    query: "str | None" = None,
    root_dir: "str | None" = None,
    resolved_file: "str | None" = None,
    rung: "str | None" = None,
    default_repo_root: "str | None" = None,
    resolved_range: "dict[str, Any] | None" = None,
    resolved_symbol: "str | None" = None,
    hit: "dict[str, Any] | None" = None,
) -> "dict[str, Any] | None":
    """Write ONE durable prospecting receipt to ``action_audit_log``.

    MDE-2 — the receipt now binds to the active repo identity: resolved
    ``repo_root`` (explicit ``root_dir``, else the session's registered
    worktree, else ``default_repo_root`` — see ``resolve_receipt_repo_
    root``), best-effort git ``revision`` (HEAD sha) for that root, and,
    when the caller supplied one, the ``resolved_file``'s current content
    hash. A resolved identity that lands inside another agent tool's
    isolated scratch checkout (``.codex/worktrees/...``) is EXCLUDED
    outright — the receipt is not written at all — so contamination from a
    concurrent, differently-tooled session can never be produced as
    evidence for this (or any other) project's completion gate.

    MDE-2 rework — three additional, OPTIONAL fields, all backward
    compatible (every existing caller that omits them gets byte-identical
    detail to before, modulo the new keys being present and null/absent):

    * ``resolved_range`` — ``{"start_line", "end_line"}`` (see
      :func:`meridian.prospect.hit_range`) — stored as ``range`` in the
      detail. Never recorded before this rework.
    * ``resolved_symbol`` — the REAL resolved symbol identity (see
      :func:`meridian.prospect.hit_identity`), as opposed to the raw,
      caller-supplied query string ``query`` was always a (200-char
      truncated) proxy for. Stored as ``symbol``, falling back to the
      truncated ``query`` proxy (documented via ``symbol_source``) only
      when no exact hit was resolved.
    * ``hit`` — the raw, EXACT-matched hit (caller's responsibility to have
      selected it via :func:`meridian.prospect.select_exact_hit` — this
      function does not itself re-verify exactness) — used, together with
      ``resolved_range``, to compute ``graph_hash``/``live_range_hash``/
      ``hash_agreement``: whether the graph/index's own reported body for
      this exact range still matches what's really on disk right now. See
      :func:`compute_graph_hash` / :func:`compute_live_range_hash`.

    Best-effort and fully guarded: a receipt-write failure must NEVER break
    the underlying tool call that already succeeded. Returns the stored row,
    or ``None`` when nothing could be written (no ``project_id`` to attribute
    it to, a contaminated resolved identity, or an unexpected DB error).
    """
    if not project_id:
        return None
    try:
        repo_ctx = await resolve_receipt_repo_root(
            db, session_id=session_id, root_dir=root_dir,
            default_repo_root=default_repo_root,
        )
        if repo_ctx.get("contaminated"):
            return None
        repo_root = repo_ctx.get("repo_root")
        source_hash = await compute_source_hash(repo_root, resolved_file)
        revision = await _git_revision(repo_root)

        range_start = (
            resolved_range.get("start_line") if isinstance(resolved_range, dict) else None
        )
        range_end = (
            resolved_range.get("end_line") if isinstance(resolved_range, dict) else None
        )
        graph_hash = compute_graph_hash(hit)
        live_range_hash = await compute_live_range_hash(
            repo_root, resolved_file, range_start, range_end,
        )
        hash_agreement = (
            (graph_hash == live_range_hash)
            if (graph_hash is not None and live_range_hash is not None)
            else None
        )

        truncated_query = (query or "")[:200]
        detail = json.dumps({
            "tool": tool_name,
            "query": truncated_query,
            "symbol": resolved_symbol or (truncated_query or None),
            "symbol_source": "resolved_hit" if resolved_symbol else "query_hint_proxy",
            "repo_root": repo_root,
            "repo_source": repo_ctx.get("source"),
            "revision": revision,
            "resolved_file": resolved_file,
            "range": resolved_range if isinstance(resolved_range, dict) else None,
            "source_hash": source_hash,
            "graph_hash": graph_hash,
            "live_range_hash": live_range_hash,
            "hash_agreement": hash_agreement,
            "rung": rung,
        })
        return await db_module.record_action_audit_event(
            db, RECEIPT_EVENT_TYPE,
            tenant_id=tenant_id, project_id=project_id,
            actor=session_id or None, detail=detail,
        )
    except Exception:  # noqa: BLE001 -- logging must never break the caller's tool call
        return None


def _receipt_detail(row: "dict[str, Any]") -> "dict[str, Any]":
    try:
        parsed = json.loads(row.get("detail") or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _receipt_repo_root(row: "dict[str, Any]") -> "str | None":
    val = _receipt_detail(row).get("repo_root")
    return val if isinstance(val, str) and val.strip() else None


def _receipt_resolved_file(row: "dict[str, Any]") -> "str | None":
    val = _receipt_detail(row).get("resolved_file")
    return val if isinstance(val, str) and val.strip() else None


def _file_exists_under_root(root: "str | None", resolved_file: "str | None") -> bool:
    """True unless *root*/*resolved_file* is a concrete, checkable pair that
    fails to exist. Nothing to check (either side missing) or an
    unverifiable filesystem error both return True -- this function only
    ever REJECTS a receipt on positive evidence the file isn't there, never
    manufactures a rejection out of an unresolved/unverifiable case (same
    "don't overclaim" posture as the rest of this module)."""
    if not root or not resolved_file:
        return True
    try:
        p = Path(resolved_file)
        if not p.is_absolute():
            p = Path(root) / resolved_file
        return p.is_file()
    except Exception:  # noqa: BLE001
        return True


async def find_recent_prospect_receipt_with_context(
    db: Any,
    *,
    project_id: str,
    tenant_id: "str | None" = None,
    since: "str | None" = None,
    expected_repo_root: "str | None" = None,
) -> "dict[str, Any]":
    """MDE-2 — identity-aware receipt lookup.

    Returns ``{"receipt": row|None, "wrong_repo_only": bool, "candidates":
    [rows]}``. When *expected_repo_root* is given, a candidate whose OWN
    resolved ``repo_root`` is recorded and disagrees is skipped (never
    treated as evidence for a different checkout); one whose declared
    ``resolved_file`` doesn't actually exist under its own ``repo_root`` is
    ALSO skipped ("wrong-body" rejection — a receipt claiming to have
    resolved a file that isn't really there is never silently trusted). A
    candidate with NO resolved repo identity of its own is accepted (can't
    prove a mismatch against an unresolved receipt — same "don't overclaim"
    posture the whole module uses elsewhere), matching pre-MDE-2 behavior
    for receipts written before this identity binding existed.

    ``wrong_repo_only=True`` means: real receipts exist for this project
    and time window, every one of them carries a RESOLVED identity, and
    NONE of them matches *expected_repo_root* -- the explicit "stale/wrong
    repo" signal distinct from "no receipt was ever recorded at all".
    """
    fetch_limit = 8 if expected_repo_root else 1
    try:
        rows = await db_module.get_action_audit_log(
            db, project_id=project_id, tenant_id=tenant_id,
            event_type=RECEIPT_EVENT_TYPE, since=since, limit=fetch_limit,
        )
    except Exception:  # noqa: BLE001 -- an unverifiable check must never wedge completion
        return {"receipt": None, "wrong_repo_only": False, "candidates": []}
    if not rows:
        return {"receipt": None, "wrong_repo_only": False, "candidates": []}
    if not expected_repo_root:
        return {"receipt": rows[0], "wrong_repo_only": False, "candidates": rows}

    expected_norm = normalize_repo_root(expected_repo_root)
    saw_resolved_mismatch = False
    for row in rows:
        stored_root = _receipt_repo_root(row)
        if stored_root is None:
            # This receipt never resolved an identity of its own (legacy
            # row, or a tunnel-forwarded call with no active-repo cache) --
            # cannot prove it's wrong, so it's accepted rather than
            # penalized for a gap this receipt predates or can't see.
            return {"receipt": row, "wrong_repo_only": False, "candidates": rows}
        if is_contaminated_repo_path(stored_root):
            continue
        if normalize_repo_root(stored_root) != expected_norm:
            saw_resolved_mismatch = True
            continue
        resolved_file = _receipt_resolved_file(row)
        if resolved_file and not _file_exists_under_root(stored_root, resolved_file):
            # "Wrong-body" signal: the receipt's OWN declared repo_root
            # doesn't actually contain the file it claims to have resolved.
            # Never trusted as valid prospecting evidence -- keep looking.
            saw_resolved_mismatch = True
            continue
        return {"receipt": row, "wrong_repo_only": False, "candidates": rows}
    return {"receipt": None, "wrong_repo_only": saw_resolved_mismatch, "candidates": rows}


async def find_recent_prospect_receipt(
    db: Any,
    *,
    project_id: str,
    tenant_id: "str | None" = None,
    since: "str | None" = None,
    expected_repo_root: "str | None" = None,
) -> "dict[str, Any] | None":
    """Return the newest MATCHING prospecting receipt for *project_id*, or
    ``None``. Back-compat entry point (unchanged 0-arg behavior when
    *expected_repo_root* is omitted, same as before MDE-2): every existing
    caller that doesn't pass *expected_repo_root* gets byte-identical
    behavior to the pre-MDE-2 implementation.

    ``since`` (inclusive lower bound on ``created_at``, same TEXT-comparable
    ``YYYY-MM-DD HH:MM:SS`` form the rest of this codebase's timestamps use)
    scopes the search to receipts recorded no earlier than the item's own
    ``claimed_at`` -- a receipt from a stale, earlier pass at the item does
    not count as evidence for the CURRENT claim, mirroring
    ``sprint_evidence_guard``'s ``EVIDENCE_STALE`` freshness check.

    Callers that need to distinguish "no receipt at all" from "receipts
    exist but none match this repo identity" should call
    :func:`find_recent_prospect_receipt_with_context` directly instead.
    """
    result = await find_recent_prospect_receipt_with_context(
        db, project_id=project_id, tenant_id=tenant_id, since=since,
        expected_repo_root=expected_repo_root,
    )
    return result["receipt"]


def _claimed_at_since(item: "dict[str, Any]") -> "str | None":
    """``item['claimed_at']`` normalized to the DB's comparable timestamp form."""
    try:
        from .db.sprint_items import _parse_deferral_ts  # noqa: PLC0415

        dt = _parse_deferral_ts(item.get("claimed_at"))
    except Exception:  # noqa: BLE001
        dt = None
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt is not None else None


async def verify_code_intel_prospecting(
    db: Any,
    tenant: "dict[str, Any] | None",
    project_id: str,
    item: "dict[str, Any]",
    *,
    session_id: "str | None" = None,
    live_inventory: "dict[str, Any] | None" = None,
    root_dir: "str | None" = None,
    default_repo_root: "str | None" = None,
) -> "dict[str, Any]":
    """The completion-time prospecting-receipt gate.

    Never raises for an expected condition -- every rejection comes back as
    a structured ``{"applicable", "ok", "code", "message", ...}`` dict, same
    contract style as ``sprint_evidence_guard.verify_strict_completion_
    evidence``. Only a genuinely unexpected error inside the availability
    lookup degrades that check to "not applicable" (fail-open on
    infrastructure trouble -- a structural defect here must never
    permanently wedge the board).

    Returns keys:
      ``applicable`` -- False means "this gate does not apply" (no declared
        touches_resources, ``prospect_bypass`` set, or the project's manifest
        never declared the :data:`CODE_INTEL_CAPABILITY_ID` capability) --
        zero behavior change from before this module existed.
      ``ok`` -- False means BLOCKED (fail-closed, ``required`` policy only).
      ``code`` -- ``CODE_INTEL_UNAVAILABLE`` | ``CODE_INTEL_RECEIPT_MISSING``
        | ``None``.
      ``degraded`` / ``warning`` -- set when proceeding on a documented
        degrade (``optional``/``degraded_ok`` policy) rather than a hard
        block.
      ``capability`` -- the ``evaluate_capability_availability`` verdict, for
        callers that want to surface it.
      ``receipt`` -- the matched receipt row, when found.
    """
    base: "dict[str, Any]" = {
        "applicable": False, "ok": True, "code": None, "message": None,
        "capability": None, "receipt": None, "degraded": False, "warning": None,
    }
    if not isinstance(item, dict):
        return base
    if not db_module._item_declares_resources(item) or bool(item.get("prospect_bypass")):
        return base

    try:
        from .mcp.handlers.project_tools import check_capability_availability  # noqa: PLC0415
        from . import capability_availability as _capability_availability  # noqa: PLC0415

        availability = await check_capability_availability(
            db, project_id, tenant,
            capability_id=CODE_INTEL_CAPABILITY_ID,
            live_inventory=live_inventory,
        )
    except Exception:  # noqa: BLE001 -- infra trouble must never wedge completion
        return base
    if not availability:
        # Project never opted in: no capability manifest entry declared.
        return base

    cap_result = availability[0]
    policy = cap_result.get("availability_policy") or "required"
    status = cap_result.get("status")
    unresolved = status in (
        _capability_availability.STATUS_MISSING, _capability_availability.STATUS_UNKNOWN,
    )

    if unresolved:
        if policy == "required":
            return {
                **base, "applicable": True, "ok": False,
                "code": "CODE_INTEL_UNAVAILABLE", "capability": cap_result,
                "message": (
                    "capability 'code_intel_prospecting' is declared REQUIRED for "
                    "this project but no required tool (and no working fallback) "
                    "is available right now -- failing closed rather than "
                    "silently skipping the prospecting-receipt requirement."
                ),
            }
        return {
            **base, "applicable": True, "ok": True, "degraded": True,
            "capability": cap_result,
            "warning": (
                f"code-intel is unavailable (policy={policy}) -- proceeding "
                "without a prospecting receipt; documented degrade, not a "
                "silent bypass."
            ),
        }

    # Code-intel IS usable (available, or degraded via a working fallback) --
    # the executor genuinely had the means to prospect. A durable receipt is
    # required to prove it actually happened for the CURRENT claim, AND
    # (MDE-2) that receipt must bind to THIS session's own repo identity --
    # a receipt is not free-floating proof "prospecting happened somewhere",
    # it's evidence prospecting happened against the code this completion is
    # actually about.
    repo_ctx = await resolve_receipt_repo_root(
        db, session_id=session_id, root_dir=root_dir,
        default_repo_root=default_repo_root,
    )
    expected_repo_root = (
        None if repo_ctx.get("contaminated") else repo_ctx.get("repo_root")
    )
    lookup = await find_recent_prospect_receipt_with_context(
        db, project_id=project_id, tenant_id=(tenant or {}).get("id") if tenant else None,
        since=_claimed_at_since(item), expected_repo_root=expected_repo_root,
    )
    receipt = lookup.get("receipt")
    if receipt is not None:
        return {**base, "applicable": True, "ok": True, "capability": cap_result, "receipt": receipt}

    if lookup.get("wrong_repo_only"):
        # Explicit, distinct signal from CODE_INTEL_RECEIPT_MISSING: real
        # receipt(s) exist for this project/time-window, but every one of
        # them resolved to a DIFFERENT repo identity than the one this
        # completion is actually running against -- a stale/wrong-repo (or
        # wrong-body) receipt must never silently satisfy the gate.
        if policy == "required":
            return {
                **base, "applicable": True, "ok": False,
                "code": "CODE_INTEL_RECEIPT_WRONG_REPO", "capability": cap_result,
                "message": (
                    "code-intel prospecting receipt(s) were recorded for this "
                    "project since the item was claimed, but none resolve to "
                    f"the current repo identity ({expected_repo_root!r}) -- "
                    "they were recorded against a different checkout/worktree "
                    "(or claim a resolved file that isn't actually there) and "
                    "cannot attest to prospecting against THIS session's "
                    "actual code. Prospect again from the current worktree, "
                    "or pass override_code_intel_receipt=true with a "
                    "non-empty override_reason to explicitly acknowledge and "
                    "complete anyway (audited)."
                ),
            }
        return {
            **base, "applicable": True, "ok": True, "degraded": True,
            "capability": cap_result,
            "warning": (
                "code-intel prospecting receipt(s) exist but none match the "
                f"current repo identity (policy={policy}) -- proceeding, "
                "skip noted."
            ),
        }

    if policy == "required":
        return {
            **base, "applicable": True, "ok": False,
            "code": "CODE_INTEL_RECEIPT_MISSING", "capability": cap_result,
            "message": (
                "code-intel is available for this project but no durable "
                "search_graph/find_symbol/prospect_symbol receipt was recorded "
                "since this item was claimed. Run a code-intel prospecting call "
                "(prospect_symbol, search_graph, find_symbol, ...) before "
                "completing, or pass override_code_intel_receipt=true with a "
                "non-empty override_reason to explicitly acknowledge and "
                "complete anyway (audited)."
            ),
        }
    return {
        **base, "applicable": True, "ok": True, "degraded": True,
        "capability": cap_result,
        "warning": (
            f"code-intel was available but no prospecting receipt was found "
            f"(policy={policy}) -- proceeding, skip noted."
        ),
    }


async def record_prospect_receipt_override(
    db: Any,
    project_id: str,
    item_id: str,
    *,
    actor: "str | None",
    reason: "str | None",
    check: "dict[str, Any]",
    tenant_id: "str | None" = None,
) -> "dict[str, Any]":
    """Audit-log an explicit override of a blocked prospecting-receipt gate.

    ``reason`` is REQUIRED and non-empty -- mirrors ``sprint_evidence_guard.
    record_strict_evidence_override`` exactly: an override with no stated
    reason is refused outright (``ValueError``), never silently accepted.
    """
    _reason = (reason or "").strip()
    if not _reason:
        raise ValueError(
            "override_reason is required and must be non-empty to override a "
            "blocked code-intel prospecting-receipt gate -- an override with "
            "no stated reason is not auditable and is refused."
        )
    detail = json.dumps({
        "item_id": item_id,
        "reason": _reason,
        "code": check.get("code"),
        "capability": check.get("capability"),
    })
    return await db_module.record_action_audit_event(
        db, OVERRIDE_EVENT_TYPE,
        tenant_id=tenant_id, project_id=project_id,
        actor=actor, detail=detail,
    )
