"""Embedded-copy-vs-source drift detection for figures and tables (432fcfcb).

A figure or table gets EMBEDDED into a .docx at some point in time (e.g. a plot
image pasted in, or a results table copied from a CSV). The embedded copy is a
frozen snapshot. The SOURCE (the script that generated the plot, the
``results.csv`` that fed the table) can be re-run/regenerated later, and the
docx has no way of knowing its embedded copy is now stale relative to that
source.

This module wires up an explicit "embedded-at-time vs current-source" comparison
that covers BOTH figures and tables identically via one shared entry point:
``check_embedded_staleness(kind, ...)``.

The comparison uses:
* file **mtime** — cheap first-pass changed-signal.
* content **fingerprint / SHA-256** — the same SHA-256 that
  ``OutputsFtsIndex`` already records in ``outputs_index.sha256`` for every
  indexed output file (``outputs_indexer._sha256_file``). When the embed-time
  sha256 (stored when the figure/table was originally indexed) differs from the
  current sha256 of the live source file, the embedded copy is stale.

Three reportable states (consistent with ``check_staleness`` in docs_intel.py):
* ``stale=False``, ``reason="current"`` — source unchanged.
* ``stale=True``,  ``reason="content-changed"`` — sha256 or mtime differs.
* ``stale=None``,  ``reason="source-missing"`` — source file gone (distinct
  from stale; the file might be intentionally deleted or renamed, not just
  regenerated).
* ``stale=None``,  ``reason="no-source-provenance"`` — no source path or
  outputs_dir provenance available at all (e.g. a manually pasted figure with
  no meridian-outputs record).

Deliberately a pure, synchronous, dependency-free function so it is trivially
unit-testable without an async runtime or a live doc store.
"""
from __future__ import annotations

import os
from typing import Any


def _sha256_file(path: str) -> str | None:
    """SHA-256 of a file's bytes, streamed in 1 MiB chunks. None if unreadable."""
    import hashlib  # noqa: PLC0415 — stdlib, lazy to keep import-time cheap

    try:
        h = hashlib.sha256()
        with open(path, "rb", buffering=0) as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def check_embedded_staleness(
    kind: str,
    *,
    source_path: str | None,
    embed_sha256: str | None,
    embed_mtime: float | None = None,
) -> dict[str, Any]:
    """Compare an embedded copy's recorded fingerprint against the live source.

    Parameters
    ----------
    kind:
        ``"figure"`` or ``"table"`` — cosmetic, surfaced in the result.
    source_path:
        Absolute (or resolvable) path to the LIVE source file on disk right now.
        ``None`` / blank means no source provenance exists for this embed.
    embed_sha256:
        The SHA-256 hash of the source file recorded at embed time (typically
        the ``sha256`` field of the ``outputs_index`` row captured when the
        figure/table was first indexed). ``None`` means the hash was never
        recorded (provenance exists but is incomplete — still checkable via
        mtime alone when ``embed_mtime`` is given).
    embed_mtime:
        Optional: the file mtime recorded at embed time. Used as a secondary
        changed-signal when sha256 is absent, and surface in the result always.

    Returns
    -------
    dict with keys:
        * ``kind`` — the value of ``kind``.
        * ``stale`` — ``False`` (current), ``True`` (drifted), or ``None``
          (cannot determine: source missing or no provenance at all).
        * ``reason`` — one of ``"current"``, ``"content-changed"``,
          ``"mtime-changed"`` (mtime differs but no sha256 to confirm),
          ``"source-missing"``, ``"no-source-provenance"``.
        * ``source_path`` — the ``source_path`` argument (may be ``None``).
        * ``embed_sha256`` — the hash recorded at embed time (may be ``None``).
        * ``current_sha256`` — the hash of the LIVE source right now (may be
          ``None`` when the source is missing or unreadable).
        * ``embed_mtime`` — the mtime recorded at embed time (may be ``None``).
        * ``current_mtime`` — the current mtime of the live source (may be
          ``None``).
    """
    _result_base: dict[str, Any] = {
        "kind": kind,
        "source_path": source_path,
        "embed_sha256": embed_sha256,
        "embed_mtime": embed_mtime,
        "current_sha256": None,
        "current_mtime": None,
    }

    if not source_path or not str(source_path).strip():
        return {**_result_base, "stale": None, "reason": "no-source-provenance"}

    # Try to stat the file for current mtime.
    try:
        st = os.stat(source_path)
        current_mtime: float | None = st.st_mtime
    except OSError:
        current_mtime = None

    if current_mtime is None:
        # Source file is gone / unreadable.
        return {**_result_base, "stale": None, "reason": "source-missing"}

    _result_base["current_mtime"] = current_mtime

    # Fast path: sha256 comparison.
    if embed_sha256 is not None:
        current_sha256 = _sha256_file(source_path)
        _result_base["current_sha256"] = current_sha256
        if current_sha256 is None:
            # File exists (stat succeeded) but is now unreadable — treat as
            # source-missing rather than a false staleness positive.
            return {**_result_base, "stale": None, "reason": "source-missing"}
        if current_sha256 == embed_sha256:
            return {**_result_base, "stale": False, "reason": "current"}
        return {**_result_base, "stale": True, "reason": "content-changed"}

    # Fallback: mtime-only when no sha256 was recorded at embed time.
    if embed_mtime is not None:
        if current_mtime != embed_mtime:
            return {**_result_base, "stale": True, "reason": "mtime-changed"}
        return {**_result_base, "stale": False, "reason": "current"}

    # Neither sha256 nor mtime was recorded — provenance exists (source_path
    # given) but no fingerprint to compare against.
    return {**_result_base, "stale": None, "reason": "no-source-provenance"}
