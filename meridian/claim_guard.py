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


def evaluate_claim_guard(
    claims: dict[str, Any] | None,
    session_id: str,
    *,
    mode: str = "write",
) -> dict[str, Any]:
    """Decide whether ``session_id`` may edit a file, from a ``get_file_claims`` payload.

    Rules mirror the server-side ``claim_file`` semantics (ffa03655 / 63b030a6):

    * a whole-file WRITE lock held by ANOTHER session blocks everything (read + write);
    * for a WRITE claim, another session's live SYMBOL claim on the file blocks it
      (file ⊃ symbol hierarchy), and another session's READ claim blocks it
      (an exclusive write waits for readers);
    * shared reads never block another read; your own claims never block you.

    Returns ``{"allow": bool, "reason": str, "holder": str | None, "mode": str}``.
    Never raises — a malformed/None payload degrades to allow (fail-open).
    """
    _mode = "read" if str(mode or "write").lower() == "read" else "write"
    if not isinstance(claims, dict):
        return {"allow": True, "reason": "no-claim-data", "holder": None, "mode": _mode}

    def _rows(key: str) -> list[Any]:
        val = claims.get(key)
        return val if isinstance(val, list) else []

    # (1) A whole-file write lock by another session blocks both read and write claims.
    lock = claims.get("file_lock")
    lock_owner = _session_of(lock)
    if lock_owner and lock_owner != session_id:
        return {"allow": False, "reason": "write_locked", "holder": lock_owner, "mode": _mode}

    # A shared read claim is only blocked by the write lock above; nothing else.
    if _mode == "write":
        # (2) Another session's live symbol claim blocks a whole-file write.
        for sc in _rows("symbol_claims"):
            owner = _session_of(sc)
            if owner and owner != session_id:
                return {"allow": False, "reason": "symbol_locked", "holder": owner, "mode": _mode}
        # (3) Another session's read claim blocks a write (exclusive write waits for readers).
        for rc in _rows("read_claims"):
            owner = _session_of(rc)
            if owner and owner != session_id:
                return {"allow": False, "reason": "read_locked", "holder": owner, "mode": _mode}

    return {"allow": True, "reason": "clear", "holder": None, "mode": _mode}
