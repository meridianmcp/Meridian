"""37dd1004 (W1-N) — tests for the Tigris spill path wired into
:func:`meridian.db.experiments.complete_experiment_run` /
:func:`meridian.db.experiments.resolve_result_receipt`.

An oversized ``result_receipt`` (> model.MAX_RESULT_RECEIPT_BYTES, 32KB)
used to make ``complete_experiment_run`` raise ``ExperimentError``
unconditionally (see tests/test_experiments.py's
``test_validate_result_receipt_rejects_over_32kb_deterministically`` --
that test calls ``model.validate_result_receipt`` DIRECTLY and is
untouched/still-passing: this item only changes what
``complete_experiment_run`` does BEFORE calling that function). Now it
spills the receipt out-of-line and stores a small pointer instead, unless
the spill itself fails, in which case the original reject-on-oversize
behavior is preserved (fail closed).
"""
from __future__ import annotations

import json

import pytest

from meridian import db as db_module
from meridian import tigris_adapter
from meridian.db import experiments as exp_db


async def _session(db, prefix: str):
    project = await db_module.create_project(db, prefix)
    session = await db_module.register_session(db, project["id"], f"{prefix}-session")
    return project, session


def _oversized_receipt() -> dict:
    # Comfortably over the 32_768-byte cap once JSON-encoded.
    return {"blob": "x" * 40_000}


# ---------------------------------------------------------------------------
# Regression guard -- a receipt that fits inline is completely unaffected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_small_receipt_is_stored_unchanged_no_spill(db, tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    project, session = await _session(db, "exp-small-receipt")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Small receipt")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])

    completed = await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=run["id"], outcome_summary="Small receipt case.", disposition="keep",
        result_receipt={"summary": "all good", "score": 0.9},
    )
    assert completed["result_receipt"] == {"summary": "all good", "score": 0.9}

    resolved = await exp_db.resolve_result_receipt(
        db, project["id"], run_id=run["id"], data_dir=str(tmp_path),
    )
    assert resolved == {"summary": "all good", "score": 0.9}


@pytest.mark.asyncio
async def test_none_receipt_still_works(db):
    project, session = await _session(db, "exp-none-receipt")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="No receipt")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])

    completed = await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=run["id"], outcome_summary="No receipt supplied.", disposition="discard",
    )
    assert completed["result_receipt"] is None
    resolved = await exp_db.resolve_result_receipt(db, project["id"], run_id=run["id"])
    assert resolved is None


# ---------------------------------------------------------------------------
# The headline new behavior: oversized receipt spills instead of rejecting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_receipt_spills_instead_of_raising(db, tmp_path, monkeypatch):
    monkeypatch.delenv("TIGRIS_ENABLED", raising=False)
    project, session = await _session(db, "exp-oversized-receipt")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Oversized receipt")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])

    huge = _oversized_receipt()
    completed = await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=run["id"], outcome_summary="Produced a huge receipt.", disposition="keep",
        result_receipt=huge, data_dir=str(tmp_path),
    )

    stored = completed["result_receipt"]
    assert stored is not None
    assert stored["spilled"] is True
    assert stored["backend"] == "local"
    assert stored["content_hash"].startswith("sha256:")
    # The pointer itself must be small -- nowhere near the original 40KB blob.
    assert len(json.dumps(stored)) < 1000

    # HARD INVARIANT (this module's own): a terminal transition still wrote
    # its experiment_events row, unaffected by the spill.
    events = await exp_db.get_experiment_events(db, project["id"], experiment_id=experiment["id"])
    assert any(e["run_id"] == run["id"] for e in events)


@pytest.mark.asyncio
async def test_oversized_receipt_resolves_back_to_original_content(db, tmp_path, monkeypatch):
    monkeypatch.delenv("TIGRIS_ENABLED", raising=False)
    project, session = await _session(db, "exp-resolve-receipt")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Resolve receipt")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])

    huge = _oversized_receipt()
    await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=run["id"], outcome_summary="Produced a huge receipt.", disposition="keep",
        result_receipt=huge, data_dir=str(tmp_path),
    )

    resolved = await exp_db.resolve_result_receipt(
        db, project["id"], run_id=run["id"], data_dir=str(tmp_path),
    )
    assert resolved == huge


@pytest.mark.asyncio
async def test_oversized_receipt_still_rejected_when_spill_fails(db, tmp_path, monkeypatch):
    """Fail-closed guarantee: if the storage backend is ALSO unavailable,
    complete_experiment_run must still raise -- never silently drop the
    caller's receipt, and never silently accept an oversized blob inline."""
    async def _always_fail_spill(*args, **kwargs):
        return {"spilled": False, "error": "simulated total storage failure"}

    monkeypatch.setattr(exp_db.tigris_adapter, "spill_oversized_payload", _always_fail_spill)

    project, session = await _session(db, "exp-spill-fails")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Spill fails")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])

    with pytest.raises(Exception, match="32768-byte cap|32_768-byte cap"):
        await exp_db.complete_experiment_run(
            db, project["id"], session["id"],
            run_id=run["id"], outcome_summary="Should be rejected.", disposition="discard",
            result_receipt=_oversized_receipt(), data_dir=str(tmp_path),
        )


@pytest.mark.asyncio
async def test_secret_shaped_oversized_receipt_still_hard_rejected(db, tmp_path, monkeypatch):
    """An oversized receipt containing secret-shaped content must be
    rejected BEFORE ever reaching a storage backend -- being oversized must
    never downgrade the existing hard-reject-on-secret posture to silent
    redaction."""
    monkeypatch.delenv("TIGRIS_ENABLED", raising=False)
    project, session = await _session(db, "exp-secret-receipt")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Secret receipt")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])

    secretish = {
        "blob": "x" * 40_000,
        "leaked": "AKIAABCDEFGHIJKLMNOP",  # AWS-access-key-shaped
    }
    with pytest.raises(ValueError):
        await exp_db.complete_experiment_run(
            db, project["id"], session["id"],
            run_id=run["id"], outcome_summary="Contains a secret.", disposition="discard",
            result_receipt=secretish, data_dir=str(tmp_path),
        )


@pytest.mark.asyncio
async def test_non_serializable_receipt_raises_original_error_no_spill_attempted(db, tmp_path, monkeypatch):
    calls = []

    async def _tracking_spill(*args, **kwargs):
        calls.append(args)
        return {"spilled": True, "backend": "local", "content_hash": "sha256:" + "0" * 64, "size": 0}

    monkeypatch.setattr(exp_db.tigris_adapter, "spill_oversized_payload", _tracking_spill)

    project, session = await _session(db, "exp-non-serializable")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Non serializable")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])

    with pytest.raises(Exception, match="must be an object|JSON serializable"):
        await exp_db.complete_experiment_run(
            db, project["id"], session["id"],
            run_id=run["id"], outcome_summary="Bad receipt type.", disposition="discard",
            result_receipt="not-a-dict",  # type: ignore[arg-type]
            data_dir=str(tmp_path),
        )
    # A non-dict receipt never even reaches the size check / spill attempt.
    assert calls == []


@pytest.mark.asyncio
async def test_resolve_result_receipt_raises_for_missing_run(db):
    project, _session_row = await _session(db, "exp-resolve-missing-run")
    with pytest.raises(ValueError, match="not found in project"):
        await exp_db.resolve_result_receipt(db, project["id"], run_id="does-not-exist")


@pytest.mark.asyncio
async def test_resolve_result_receipt_returns_none_when_spilled_payload_unreachable(db, tmp_path, monkeypatch):
    monkeypatch.delenv("TIGRIS_ENABLED", raising=False)
    project, session = await _session(db, "exp-resolve-unreachable")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Resolve unreachable")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])
    await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=run["id"], outcome_summary="Spilled.", disposition="keep",
        result_receipt=_oversized_receipt(), data_dir=str(tmp_path),
    )

    async def _always_none(*args, **kwargs):
        return None

    monkeypatch.setattr(exp_db.tigris_adapter, "fetch_spilled_payload", _always_none)
    resolved = await exp_db.resolve_result_receipt(
        db, project["id"], run_id=run["id"], data_dir=str(tmp_path),
    )
    assert resolved is None


@pytest.mark.asyncio
async def test_resolve_result_receipt_returns_none_on_corrupt_payload(db, tmp_path, monkeypatch):
    monkeypatch.delenv("TIGRIS_ENABLED", raising=False)
    project, session = await _session(db, "exp-resolve-corrupt")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Resolve corrupt")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])
    await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=run["id"], outcome_summary="Spilled.", disposition="keep",
        result_receipt=_oversized_receipt(), data_dir=str(tmp_path),
    )

    async def _corrupt_bytes(*args, **kwargs):
        return b"not valid json {{{"

    monkeypatch.setattr(exp_db.tigris_adapter, "fetch_spilled_payload", _corrupt_bytes)
    resolved = await exp_db.resolve_result_receipt(
        db, project["id"], run_id=run["id"], data_dir=str(tmp_path),
    )
    assert resolved is None


@pytest.mark.asyncio
async def test_spill_pointer_includes_key_when_backend_supplies_one(db, tmp_path, monkeypatch):
    """Covers the tigris-backed pointer shape (the ``key`` field is only
    present for that backend -- see tigris_adapter.spill_oversized_payload's
    docstring) by simulating what a real Tigris spill result looks like."""
    async def _fake_tigris_spill(*args, **kwargs):
        return {
            "spilled": True, "backend": "tigris", "key": "proj/experiment_receipts/ab/abc123",
            "content_hash": "sha256:" + "5" * 64, "size": 99, "content_type": "application/json",
            "project_id": "irrelevant-overwritten-by-caller",
        }

    monkeypatch.setattr(exp_db.tigris_adapter, "spill_oversized_payload", _fake_tigris_spill)
    project, session = await _session(db, "exp-tigris-pointer-shape")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Tigris pointer shape")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])

    completed = await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=run["id"], outcome_summary="Spilled to tigris (simulated).", disposition="keep",
        result_receipt=_oversized_receipt(), data_dir=str(tmp_path),
    )
    stored = completed["result_receipt"]
    assert stored["backend"] == "tigris"
    assert stored["key"] == "proj/experiment_receipts/ab/abc123"
    assert stored["project_id"] == project["id"]  # caller's project_id wins, not the fake's


@pytest.mark.asyncio
async def test_dict_receipt_with_non_json_serializable_value_falls_through_no_spill_attempted(db, tmp_path, monkeypatch):
    """A dict that fails to encode at all (e.g. contains a set) can never be
    size-checked, so the spill path must never even be attempted -- falls
    straight through to model.validate_result_receipt's own
    "must be JSON serializable" rejection, exactly as before this item."""
    calls = []

    async def _tracking_spill(*args, **kwargs):
        calls.append(args)
        return {"spilled": True, "backend": "local", "content_hash": "sha256:" + "0" * 64, "size": 0}

    monkeypatch.setattr(exp_db.tigris_adapter, "spill_oversized_payload", _tracking_spill)

    project, session = await _session(db, "exp-dict-non-serializable")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Dict non serializable")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])

    with pytest.raises(Exception, match="JSON serializable"):
        await exp_db.complete_experiment_run(
            db, project["id"], session["id"],
            run_id=run["id"], outcome_summary="Set is not JSON serializable.", disposition="discard",
            result_receipt={"blob": {1, 2, 3}}, data_dir=str(tmp_path),
        )
    assert calls == []


@pytest.mark.asyncio
async def test_data_dir_defaults_to_meridian_data_dir_env(db, tmp_path, monkeypatch):
    """Omitting data_dir must still resolve consistently via
    MERIDIAN_DATA_DIR, matching every other local-artifact caller in this
    repo (see meridian.routes.oauth's identical lookup)."""
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TIGRIS_ENABLED", raising=False)
    project, session = await _session(db, "exp-default-data-dir")
    experiment = await exp_db.create_experiment(db, project["id"], session["id"], name="Default data dir")
    run = await exp_db.start_experiment_run(db, project["id"], session["id"], experiment_id=experiment["id"])

    huge = _oversized_receipt()
    completed = await exp_db.complete_experiment_run(
        db, project["id"], session["id"],
        run_id=run["id"], outcome_summary="Uses default data_dir.", disposition="keep",
        result_receipt=huge,
        # data_dir intentionally omitted -- must fall back to MERIDIAN_DATA_DIR.
    )
    assert completed["result_receipt"]["spilled"] is True

    resolved = await exp_db.resolve_result_receipt(db, project["id"], run_id=run["id"])
    assert resolved == huge
