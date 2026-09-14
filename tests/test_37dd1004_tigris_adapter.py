"""37dd1004 (W1-N) — tests for :mod:`meridian.tigris_adapter`: the thin
Tigris/S3 adapter with graceful degradation, and the oversized-payload
spill/fetch round trip.

Tigris itself is deliberately NOT exercised end-to-end here beyond
confirming it degrades cleanly -- ``object_store.TigrisObjectStoreBackend``
remains an inactive-by-default ``NotImplementedError`` stub by design (see
decision bad077b9), matching tests/test_object_store.py's own stated
policy ("TigrisObjectStoreBackend is deliberately NOT tested here beyond
confirming it refuses to do anything").
"""
from __future__ import annotations

import asyncio

import pytest

from meridian import tigris_adapter


# ---------------------------------------------------------------------------
# is_tigris_enabled
# ---------------------------------------------------------------------------


def test_is_tigris_enabled_reflects_env_var(monkeypatch):
    monkeypatch.delenv("TIGRIS_ENABLED", raising=False)
    assert tigris_adapter.is_tigris_enabled() is False
    for truthy in ("1", "true", "True", "YES", "on"):
        monkeypatch.setenv("TIGRIS_ENABLED", truthy)
        assert tigris_adapter.is_tigris_enabled() is True
    for falsy in ("0", "false", "", "no"):
        monkeypatch.setenv("TIGRIS_ENABLED", falsy)
        assert tigris_adapter.is_tigris_enabled() is False


# ---------------------------------------------------------------------------
# _try_construct_tigris_backend -- graceful degradation
# ---------------------------------------------------------------------------


def test_try_construct_tigris_backend_none_when_disabled(monkeypatch):
    monkeypatch.delenv("TIGRIS_ENABLED", raising=False)
    assert tigris_adapter._try_construct_tigris_backend() is None


def test_try_construct_tigris_backend_none_when_enabled_but_stub_raises(monkeypatch):
    """TigrisObjectStoreBackend() always raises NotImplementedError today
    (decision bad077b9) -- this must degrade to None, never propagate."""
    monkeypatch.setenv("TIGRIS_ENABLED", "true")
    assert tigris_adapter._try_construct_tigris_backend() is None


# ---------------------------------------------------------------------------
# spill_oversized_payload -- local fallback (the only reachable path today)
# ---------------------------------------------------------------------------


def test_spill_rejects_non_bytes_payload():
    async def _run():
        result = await tigris_adapter.spill_oversized_payload(
            "unused", "proj-1", "not-bytes",  # type: ignore[arg-type]
        )
        assert result == {"spilled": False, "error": "payload must be bytes"}
    asyncio.run(_run())


def test_spill_and_fetch_round_trip_via_local_backend(tmp_path, monkeypatch):
    monkeypatch.delenv("TIGRIS_ENABLED", raising=False)
    data_dir = str(tmp_path)
    payload = b'{"summary": "a large receipt", "blob": "x"}'

    async def _run():
        spill = await tigris_adapter.spill_oversized_payload(
            data_dir, "proj-1", payload, content_type="application/json",
        )
        assert spill["spilled"] is True
        assert spill["backend"] == "local"
        assert spill["content_hash"].startswith("sha256:")
        assert spill["size"] == len(payload)
        assert spill["content_type"] == "application/json"
        assert "key" not in spill  # local backend is addressed by content_hash only

        fetched = await tigris_adapter.fetch_spilled_payload(data_dir, spill)
        assert fetched == payload
    asyncio.run(_run())


def test_spill_with_tigris_enabled_still_degrades_to_local(tmp_path, monkeypatch):
    """The headline graceful-degradation contract: setting TIGRIS_ENABLED
    must never crash the caller, and must still durably store the payload
    (today, always via the local fallback -- see decision bad077b9)."""
    monkeypatch.setenv("TIGRIS_ENABLED", "true")
    data_dir = str(tmp_path)
    payload = b"oversized payload bytes" * 2000

    async def _run():
        spill = await tigris_adapter.spill_oversized_payload(data_dir, "proj-2", payload)
        assert spill["spilled"] is True
        assert spill["backend"] == "local"
        fetched = await tigris_adapter.fetch_spilled_payload(data_dir, spill)
        assert fetched == payload
    asyncio.run(_run())


def test_spill_records_never_embed_the_original_payload(tmp_path, monkeypatch):
    monkeypatch.delenv("TIGRIS_ENABLED", raising=False)
    data_dir = str(tmp_path)
    payload = b"secret-shaped-marker-should-not-appear-in-record" * 100

    async def _run():
        spill = await tigris_adapter.spill_oversized_payload(data_dir, "proj-3", payload)
        for value in spill.values():
            assert value != payload
            if isinstance(value, str):
                assert b"secret-shaped-marker-should-not-appear-in-record" not in value.encode()
    asyncio.run(_run())


def test_spill_local_failure_returns_spilled_false_not_raise(monkeypatch):
    monkeypatch.delenv("TIGRIS_ENABLED", raising=False)

    def _boom(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(tigris_adapter.artifact_store, "store_artifact", _boom)

    async def _run():
        result = await tigris_adapter.spill_oversized_payload("unused", "proj-4", b"data")
        assert result["spilled"] is False
        assert "OSError" in result["error"]
    asyncio.run(_run())


# ---------------------------------------------------------------------------
# fetch_spilled_payload -- malformed/unreachable records degrade to None
# ---------------------------------------------------------------------------


def test_fetch_returns_none_for_malformed_records():
    async def _run():
        assert await tigris_adapter.fetch_spilled_payload("unused", None) is None  # type: ignore[arg-type]
        assert await tigris_adapter.fetch_spilled_payload("unused", {}) is None
        assert await tigris_adapter.fetch_spilled_payload(
            "unused", {"spilled": False},
        ) is None
        assert await tigris_adapter.fetch_spilled_payload(
            "unused", {"spilled": True, "backend": "local"},  # missing project_id/content_hash
        ) is None
        assert await tigris_adapter.fetch_spilled_payload(
            "unused", {"spilled": True, "backend": "carrier-pigeon",
                       "project_id": "p", "content_hash": "sha256:" + "0" * 64},
        ) is None
    asyncio.run(_run())


def test_fetch_missing_local_content_returns_none(tmp_path):
    data_dir = str(tmp_path)
    fake_record = {
        "spilled": True, "backend": "local", "project_id": "proj-5",
        "content_hash": "sha256:" + "0" * 64,
    }

    async def _run():
        assert await tigris_adapter.fetch_spilled_payload(data_dir, fake_record) is None
    asyncio.run(_run())


def test_fetch_tigris_backed_record_returns_none_when_tigris_unreachable(tmp_path, monkeypatch):
    monkeypatch.delenv("TIGRIS_ENABLED", raising=False)
    data_dir = str(tmp_path)
    fake_record = {
        "spilled": True, "backend": "tigris", "key": "proj-6/experiment_receipts/ab/abc123",
        "project_id": "proj-6", "content_hash": "sha256:" + "1" * 64,
    }

    async def _run():
        assert await tigris_adapter.fetch_spilled_payload(data_dir, fake_record) is None
    asyncio.run(_run())


def test_local_fetch_failure_degrades_to_none(tmp_path, monkeypatch):
    """A local get_artifact failure (e.g. a permissions/disk error, not just
    a missing key) must also degrade gracefully, never raise."""
    monkeypatch.delenv("TIGRIS_ENABLED", raising=False)

    def _boom(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(tigris_adapter.artifact_store, "get_artifact", _boom)
    record = {
        "spilled": True, "backend": "local", "project_id": "proj-7",
        "content_hash": "sha256:" + "2" * 64,
    }

    async def _run():
        assert await tigris_adapter.fetch_spilled_payload("unused", record) is None
    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Forward-activation contract: a FAKE future Tigris backend (simulating what
# a real TigrisObjectStoreBackend implementation would look like) must be
# used transparently by spill/fetch the moment _try_construct_tigris_backend
# can return something real -- exercising the "tigris succeeds" branches
# that are unreachable today (TigrisObjectStoreBackend is a permanent
# NotImplementedError stub per decision bad077b9) but must already work
# correctly for the day that changes.
# ---------------------------------------------------------------------------


class _FakePutResult:
    def __init__(self, key: str, size: int):
        self.key = key
        self.size = size


class _FakeFutureTigrisBackend:
    """Stands in for a hypothetical real TigrisObjectStoreBackend -- an
    in-memory dict keyed by the object key, exposing only the two methods
    this adapter actually calls (put/get)."""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.put_calls: list[str] = []

    async def put(self, key: str, data: bytes, *, content_type=None):
        self.store[key] = data
        self.put_calls.append(key)
        return _FakePutResult(key=key, size=len(data))

    async def get(self, key: str) -> bytes:
        if key not in self.store:
            raise KeyError(key)
        return self.store[key]


def test_spill_uses_tigris_backend_when_available(monkeypatch):
    fake_backend = _FakeFutureTigrisBackend()
    monkeypatch.setattr(
        tigris_adapter, "_try_construct_tigris_backend", lambda: fake_backend,
    )
    payload = b"a receipt that would have spilled to a real Tigris bucket"

    async def _run():
        spill = await tigris_adapter.spill_oversized_payload(
            "unused-data-dir", "proj-8", payload, content_type="application/json",
        )
        assert spill["spilled"] is True
        assert spill["backend"] == "tigris"
        assert spill["key"].startswith("proj-8/experiment_receipts/")
        assert spill["content_hash"].startswith("sha256:")
        assert spill["size"] == len(payload)
        assert fake_backend.put_calls == [spill["key"]]

        fetched = await tigris_adapter.fetch_spilled_payload("unused-data-dir", spill)
        assert fetched == payload
    asyncio.run(_run())


def test_spill_falls_back_to_local_when_tigris_put_fails(tmp_path, monkeypatch):
    class _FailingPutBackend:
        async def put(self, key, data, *, content_type=None):
            raise ConnectionError("simulated Tigris outage")

    monkeypatch.setattr(
        tigris_adapter, "_try_construct_tigris_backend", lambda: _FailingPutBackend(),
    )
    data_dir = str(tmp_path)
    payload = b"falls back to local when the Tigris put itself fails"

    async def _run():
        spill = await tigris_adapter.spill_oversized_payload(data_dir, "proj-9", payload)
        assert spill["spilled"] is True
        assert spill["backend"] == "local"
        fetched = await tigris_adapter.fetch_spilled_payload(data_dir, spill)
        assert fetched == payload
    asyncio.run(_run())


def test_fetch_tigris_get_failure_degrades_to_none(monkeypatch):
    class _FailingGetBackend:
        async def get(self, key):
            raise ConnectionError("simulated Tigris outage")

    monkeypatch.setattr(
        tigris_adapter, "_try_construct_tigris_backend", lambda: _FailingGetBackend(),
    )
    record = {
        "spilled": True, "backend": "tigris", "key": "proj-10/experiment_receipts/ab/abc",
        "project_id": "proj-10", "content_hash": "sha256:" + "3" * 64,
    }

    async def _run():
        assert await tigris_adapter.fetch_spilled_payload("unused", record) is None
    asyncio.run(_run())


def test_fetch_tigris_record_missing_key_returns_none(monkeypatch):
    fake_backend = _FakeFutureTigrisBackend()
    monkeypatch.setattr(
        tigris_adapter, "_try_construct_tigris_backend", lambda: fake_backend,
    )
    record = {
        "spilled": True, "backend": "tigris",
        "project_id": "proj-11", "content_hash": "sha256:" + "4" * 64,
        # "key" deliberately omitted
    }

    async def _run():
        assert await tigris_adapter.fetch_spilled_payload("unused", record) is None
    asyncio.run(_run())


def test_tigris_key_shape():
    assert tigris_adapter._tigris_key("proj-1", "abcd1234") == (
        "proj-1/experiment_receipts/ab/abcd1234"
    )
