"""fd726a05 — vendor-neutral file-claim guard.

The `claim_file` mechanism only helps if agents actually call it before editing. An
*instruction* to do so (CLAUDE.md / AGENTS.md / .cursor/rules — e2ac066b) can be
forgotten; the durable form is STRUCTURAL enforcement, the same principle as the
code-intel protocol: a PreToolUse hook / launch wrapper that consults the claims for a
file before an Edit/Write and blocks (or warns) when another live session owns it.

Every vendor's hook (Claude Code PreToolUse, Codex/Cursor equivalents) does the same
two things: (1) fetch `get_file_claims(path)` from the Meridian server, (2) decide
allow/block for the current session. Step (2) is identical across vendors, so it lives
here as a **pure decision function** — no network, no vendor coupling — and each vendor
integration is a thin shim that fetches the claims and calls this.

Reference PreToolUse-hook shape (pseudocode, any vendor):

    claims = http_get(f"{MERIDIAN}/projects/{pid}/file-claims?file_path={path}")
    verdict = evaluate_claim_guard(claims, MY_SESSION_ID, mode="write")
    if not verdict["allow"]:
        block(f"{path} is {verdict['reason']} by session {verdict['holder']} — "
              f"claim it or coordinate first")

Fail-open by design: a malformed/None claims payload degrades to allow. The server-side
`claim_file` is the real gate; this guard is a fast, forgettable-proof pre-check whose
job is to surface conflicts early, never to wedge an editor on bad data.
"""
from __future__ import annotations

from typing import Any


def _session_of(row: Any) -> str:
    return str(row.get("session_id")) if isinstance(row, dict) and row.get("session_id") else ""


def _symbol_name_of(row: Any) -> str:
    """Name of the symbol a symbol-claim row targets.

    ``get_file_claims`` rows come from ``get_symbol_claims`` (``SELECT fsc.*``), so
    the canonical column is ``symbol_name``; accept a bare ``symbol`` alias too for
    payloads built by other vendors' shims. Stripped; ``""`` when absent.
    """
    if not isinstance(row, dict):
        return ""
    return str(row.get("symbol_name") or row.get("symbol") or "").strip()


def evaluate_claim_guard(
    claims: dict[str, Any] | None,
    session_id: str,
    *,
    mode: str = "write",
    symbol: str | None = None,
) -> dict[str, Any]:
    """Decide whether ``session_id`` may edit a file, from a ``get_file_claims`` payload.

    Rules mirror the server-side ``claim_file`` / ``claim_symbol`` semantics
    (ffa03655 / 63b030a6):

    * a whole-file WRITE lock held by ANOTHER session blocks everything
      (read + write + symbol-scoped write) — the file owner may touch any symbol;
    * for a **whole-file** WRITE claim (``symbol=None``), another session's live
      SYMBOL claim on the file blocks it (file ⊃ symbol hierarchy), and another
      session's READ claim blocks it (an exclusive write waits for readers);
    * for a **symbol-scoped** WRITE claim (``symbol="Foo"`` — badc7b34), only
      another session's claim on the *same* symbol blocks it. A claim on a
      *different* symbol does not (two sessions safely own different symbols in one
      file), and shared read claims do not block a narrow symbol write — matching
      ``claim_symbol``, which checks whole-file locks and symbol overlap only;
    * shared reads never block another read; your own claims never block you.

    ``symbol`` is ignored for ``mode="read"`` (a read claim is shared and file-wide).

    Returns ``{"allow": bool, "reason": str, "holder": str | None, "mode": str,
    "symbol": str | None}``. Never raises — a malformed/None payload degrades to
    allow (fail-open).
    """
    _mode = "read" if str(mode or "write").lower() == "read" else "write"
    _symbol = (symbol or "").strip() or None
    # A symbol scope only refines a WRITE claim; a read claim is inherently file-wide.
    if _mode != "write":
        _symbol = None

    def _verdict(allow: bool, reason: str, holder: str | None) -> dict[str, Any]:
        return {
            "allow": allow,
            "reason": reason,
            "holder": holder,
            "mode": _mode,
            "symbol": _symbol,
        }

    if not isinstance(claims, dict):
        return _verdict(True, "no-claim-data", None)

    def _rows(key: str) -> list[Any]:
        val = claims.get(key)
        return val if isinstance(val, list) else []

    # (1) A whole-file write lock by another session blocks EVERYTHING — read,
    # whole-file write, and symbol-scoped write (the lock owner owns every symbol).
    lock = claims.get("file_lock")
    lock_owner = _session_of(lock)
    if lock_owner and lock_owner != session_id:
        return _verdict(False, "write_locked", lock_owner)

    # A shared read claim is only blocked by the write lock above; nothing else.
    if _mode == "write":
        if _symbol is not None:
            # (2s) Symbol-scoped write: only another session's claim on the SAME
            # symbol blocks. Different symbols coexist; read claims don't block a
            # narrow symbol write (mirrors claim_symbol, which ignores read claims).
            for sc in _rows("symbol_claims"):
                owner = _session_of(sc)
                if owner and owner != session_id and _symbol_name_of(sc) == _symbol:
                    return _verdict(False, "symbol_locked", owner)
            return _verdict(True, "clear", None)
        # (2) Whole-file write: another session's live symbol claim (any symbol) blocks.
        for sc in _rows("symbol_claims"):
            owner = _session_of(sc)
            if owner and owner != session_id:
                return _verdict(False, "symbol_locked", owner)
        # (3) Another session's read claim blocks a write (exclusive write waits for readers).
        for rc in _rows("read_claims"):
            owner = _session_of(rc)
            if owner and owner != session_id:
                return _verdict(False, "read_locked", owner)

    return _verdict(True, "clear", None)
