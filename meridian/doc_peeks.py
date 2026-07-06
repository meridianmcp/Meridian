"""In-memory 'recently viewed, not saved' document-peek log (79ee73e8).

``get_document_structure`` is a STATELESS peek — it parses a .docx and returns its
outline without persisting anything, so repeated peeks never appeared in the
Documents tab (which lists only INGESTED docs). That silently conflated two
different things and made the tab feel empty even after heavy use.

This records recent peeks (path + when + ok) in a small, capped, TENANT-SCOPED
in-memory ring so the Documents tab can show a "Recently viewed (not saved)"
section and offer to ingest them. Ephemeral by design ("not saved") — cleared on
restart, per-instance on multi-instance hosting. Tenant-scoped so a peek never
leaks across tenants (``scope`` = tenant id on hosted, "local" self-hosted).
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, List

_MAX_PER_SCOPE = 10
_lock = threading.Lock()
_peeks: Dict[str, List[Dict[str, Any]]] = {}


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def record_peek(scope: str | None, file_path: str, *, ok: bool = True) -> None:
    """Record a get_document_structure peek: newest-first, deduped by path, capped."""
    if not file_path:
        return
    key = scope or "local"
    entry = {"file_path": file_path, "viewed_at": _now_iso(), "ok": bool(ok)}
    with _lock:
        kept = [e for e in _peeks.get(key, []) if e.get("file_path") != file_path]
        _peeks[key] = ([entry] + kept)[:_MAX_PER_SCOPE]  # newest first, capped


def get_peeks(scope: str | None) -> List[Dict[str, Any]]:
    """Return recent peeks for a scope (newest-first)."""
    with _lock:
        return list(_peeks.get(scope or "local", []))


def clear(scope: str | None = None) -> None:
    """Test helper: clear one scope, or (scope=None) everything."""
    with _lock:
        if scope is None:
            _peeks.clear()
        else:
            _peeks.pop(scope or "local", None)
