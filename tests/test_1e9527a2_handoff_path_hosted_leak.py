"""Regression test for 1e9527a2 / 29fb9c8f.

SECURITY: hosted Meridian must never leak the server's raw absolute
filesystem path (MERIDIAN_DATA_DIR / an ``output_dir``) to a client.

Confirmed leak: ``meridian.handoff.regenerate_handoff_correction`` (and its
public wrapper ``amend_handoff``, and the REST/MCP transports that relay
their result dicts verbatim -- ``routes/handoff.py``'s
``record_handoff_correction_endpoint`` and ``mcp/handler.py``'s
``record_handoff_correction`` dispatch) built an absolute path with
``str(out_path.resolve())`` (see ``meridian.handoff.generate_handoff`` and
its per-mode ``_generate_*_handoff`` helpers) and echoed it back verbatim as
``result["new_handoff_path"]``. On the hosted tier (``MERIDIAN_HOSTED=1``),
one process serves many tenants from a shared, process-global data
directory (e.g. ``/app/data`` on the Fly.io hosted tier) -- returning that
path discloses server-internal filesystem layout to every connected tenant.
The exact same bug class exists in ``server.py``'s ``start_session`` response
(``payload["handoff_path"]``) -- tracked separately as item 29fb9c8f, NOT
covered by this test file (see this item's completion notes for why: a
concurrent live-session file lock on ``meridian/server.py`` at the time this
fix landed prevented editing it safely under this repo's own "server.py:
always sequential, never parallel" rule).

Self-hosted (``MERIDIAN_HOSTED`` unset/false) must stay byte-for-byte
unchanged -- caller and server share a trust boundary there, and existing
tests (``tests/test_cov_handoff.py``) already assert the real path is
returned in that mode.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


async def _seed_handoff(db, name: str, tmp_path):
    """Create a project with a goal + one FRESH generated handoff row.

    Mirrors ``tests/test_cov_handoff.py``'s own ``_seed_handoff`` helper so
    this file's tests read the same way as the rest of the handoff-correction
    coverage.
    """
    p = await db_module.create_project(db, name)
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True,
    )
    rows = await db_module.get_handoffs(db, p["id"], limit=1)
    return p, rows[0]


@pytest.mark.asyncio
async def test_regenerate_handoff_correction_omits_path_when_hosted(
    monkeypatch, db, tmp_path
):
    """29fb9c8f/1e9527a2: in hosted mode, new_handoff_path must be None, and
    the raw data_dir must not appear anywhere in the response payload."""
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    data_dir = tmp_path / "app_data_like_dir"
    data_dir.mkdir()

    p, h = await _seed_handoff(db, "corr-hosted-leak", data_dir)
    corr = await handoff_module.record_handoff_correction(
        db, p["id"], source_handoff_id=h["id"], blocker_classification="scope_stale",
    )
    result = await handoff_module.regenerate_handoff_correction(
        db, p["id"], corr["id"], str(data_dir), mode="full",
    )

    assert result["regenerated"] is True
    assert result["new_handoff_path"] is None, (
        "hosted mode must not echo the server's absolute handoff path back "
        f"to the client (got {result['new_handoff_path']!r})"
    )
    # Belt-and-suspenders: the raw data_dir string must not leak through ANY
    # other field either (e.g. an accidental string-interpolation elsewhere).
    serialized = repr(result)
    assert str(data_dir) not in serialized
    assert "app_data_like_dir" not in serialized


@pytest.mark.asyncio
async def test_amend_handoff_omits_path_when_hosted(monkeypatch, db, tmp_path):
    """Same leak, reached through the public ``amend_handoff`` entry point --
    the function ``routes/handoff.py`` and the MCP ``record_handoff_correction``
    dispatch both call under the hood when ``regenerate=true``."""
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    data_dir = tmp_path / "app_data_like_dir"
    data_dir.mkdir()

    p, h = await _seed_handoff(db, "amend-hosted-leak", data_dir)
    result = await handoff_module.amend_handoff(
        db, p["id"], h["id"], str(data_dir), force_regenerate=True,
    )

    assert result["amended"] is True
    assert result["new_handoff_path"] is None
    assert str(data_dir) not in repr(result)


@pytest.mark.asyncio
async def test_regenerate_handoff_correction_still_returns_real_path_when_self_hosted(
    monkeypatch, db, tmp_path
):
    """Self-hosted (MERIDIAN_HOSTED unset) must be unaffected: caller and
    server share a trust boundary there, and other tests
    (test_cov_handoff.py) already depend on the real path being returned."""
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)

    p, h = await _seed_handoff(db, "corr-self-hosted-path", tmp_path)
    corr = await handoff_module.record_handoff_correction(
        db, p["id"], source_handoff_id=h["id"], blocker_classification="scope_stale",
    )
    result = await handoff_module.regenerate_handoff_correction(
        db, p["id"], corr["id"], str(tmp_path), mode="full",
    )
    assert result["new_handoff_path"]
    assert result["new_handoff_path"].endswith(
        f"{handoff_module.handoff_file_stem(p['id'])}_handoff.md"
    )
