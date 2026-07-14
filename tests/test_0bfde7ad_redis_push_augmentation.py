"""0bfde7ad — Redis push augmentation for send_message/receive_messages.

Scope, per the item's own finalized notes: keep the existing DB write for
persistence (send_message/receive_messages, d3a3a01d, are unchanged in their
own contract), ADD a best-effort Redis publish so a listener gets pushed
instead of polling. Deployed-app path only -- MERIDIAN_REDIS_URL is a Fly
secret on meridian-hosted; local unit tests use a mocked/fake Redis client,
never a real connection (real network verification happens post-deploy,
against the deployed app specifically -- see the item's completion notes).

Architectural framing (pinned decision 5710635f): this is a genuine
capability Postgres lacks (push wake-up), not a duplication of something
Postgres already does well -- distinct from the 229441bc/2ad938a0 "no new
infra" precedents, which reject Redis for problems Postgres already solves.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from meridian import db as db_module
from meridian import redis_bridge


@pytest.fixture(autouse=True)
def _reset_redis_bridge_cache(monkeypatch):
    """Every test starts from a clean slate -- no leaked client/failure-flag
    state between tests, and MERIDIAN_REDIS_URL is unset by default so tests
    are hermetic regardless of the real environment's env vars."""
    redis_bridge.reset_redis_client_cache()
    monkeypatch.delenv("MERIDIAN_REDIS_URL", raising=False)
    yield
    redis_bridge.reset_redis_client_cache()


class _FakeRedisClient:
    """In-memory stand-in for redis.asyncio.Redis -- records publishes and
    can deliver them to a fake pubsub, without touching a real network."""

    def __init__(self):
        self.published: list[tuple[str, str]] = []
        self._subscribers: dict[str, list["_FakePubSub"]] = {}

    async def publish(self, channel: str, data: str) -> int:
        self.published.append((channel, data))
        for ps in self._subscribers.get(channel, []):
            ps._queue.append({"type": "message", "channel": channel, "data": data})
        return len(self._subscribers.get(channel, []))

    def pubsub(self) -> "_FakePubSub":
        return _FakePubSub(self)


class _FakePubSub:
    def __init__(self, client: _FakeRedisClient):
        self._client = client
        self._queue: list[dict] = []
        self._channel: str | None = None

    async def subscribe(self, channel: str) -> None:
        self._channel = channel
        self._client._subscribers.setdefault(channel, []).append(self)

    async def unsubscribe(self, channel: str) -> None:
        subs = self._client._subscribers.get(channel, [])
        if self in subs:
            subs.remove(self)

    async def aclose(self) -> None:
        pass

    async def listen(self):
        # Real pubsub.listen() blocks until a message arrives -- actively
        # wait/poll rather than draining a point-in-time snapshot, so a
        # subscriber started before the publish() call still sees it.
        idle_polls = 0
        max_idle_polls = 200  # ~2s at 0.01s/poll, generous for a local test
        while idle_polls < max_idle_polls:
            if self._queue:
                idle_polls = 0
                yield self._queue.pop(0)
            else:
                await asyncio.sleep(0.01)
                idle_polls += 1


class _FailingRedisClient:
    """Simulates a Redis outage -- every call raises."""

    async def publish(self, channel: str, data: str):
        raise ConnectionError("simulated Redis outage")


def test_get_redis_client_returns_none_when_unset():
    async def _run():
        client = await redis_bridge.get_redis_client()
        assert client is None
    asyncio.run(_run())


def test_publish_session_message_is_noop_and_returns_false_when_unconfigured():
    async def _run():
        ok = await redis_bridge.publish_session_message("s1", {"id": "m1", "payload": "hi"})
        assert ok is False
    asyncio.run(_run())


def test_publish_session_message_publishes_real_json_to_scoped_channel(monkeypatch):
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    async def _run():
        ok = await redis_bridge.publish_session_message(
            "session-xyz", {"id": "m1", "payload": "do the thing", "from_session_id": "s0"}
        )
        assert ok is True
        assert len(fake.published) == 1
        channel, data = fake.published[0]
        assert channel == "meridian:messages:session-xyz"
        decoded = json.loads(data)
        assert decoded["payload"] == "do the thing"
        assert decoded["id"] == "m1"
    asyncio.run(_run())


def test_publish_session_message_fails_open_never_raises(monkeypatch):
    async def _fake_get_client():
        return _FailingRedisClient()

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    async def _run():
        # Must not raise -- a Redis outage is swallowed, not propagated.
        ok = await redis_bridge.publish_session_message("s1", {"id": "m1"})
        assert ok is False
    asyncio.run(_run())


def test_send_message_still_persists_when_redis_unconfigured():
    """THE CRITICAL REGRESSION GUARD: every existing send_message caller/test
    in the whole suite runs with no Redis configured. This must behave
    identically to before this item -- DB write succeeds, same return shape."""
    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "redis-noop-proj")
            pid = proj["id"]
            s1 = await db_module.register_session(db, pid, "a")
            s2 = await db_module.register_session(db, pid, "b")
            row = await db_module.send_message(
                db, pid, s2["id"], "do Y", from_session_id=s1["id"]
            )
            assert row["payload"] == "do Y"
            msgs = await db_module.receive_messages(db, s2["id"])
            assert len(msgs) == 1 and msgs[0]["payload"] == "do Y"
        finally:
            await db.close()
    asyncio.run(_run())


def test_send_message_publishes_to_redis_when_configured(monkeypatch):
    """send_message calls the new publish augmentation with the real,
    persisted row -- not a synthetic/partial payload."""
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "redis-push-proj")
            pid = proj["id"]
            s1 = await db_module.register_session(db, pid, "planner")
            s2 = await db_module.register_session(db, pid, "executor")

            row = await db_module.send_message(
                db, pid, s2["id"], "run_tool payload here", from_session_id=s1["id"], kind="run_tool",
            )

            assert len(fake.published) == 1
            channel, data = fake.published[0]
            assert channel == f"meridian:messages:{s2['id']}"
            pushed = json.loads(data)
            # The pushed content IS the real persisted DB row, not a stub.
            assert pushed["id"] == row["id"]
            assert pushed["payload"] == "run_tool payload here"
            assert pushed["kind"] == "run_tool"

            # DB write is STILL the source of truth -- unaffected by the push.
            msgs = await db_module.receive_messages(db, s2["id"])
            assert len(msgs) == 1 and msgs[0]["id"] == row["id"]
        finally:
            await db.close()
    asyncio.run(_run())


def test_send_message_succeeds_even_when_redis_publish_fails(monkeypatch):
    """A Redis outage must never break the DB write -- augment, not replace."""
    async def _fake_get_client():
        return _FailingRedisClient()

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "redis-outage-proj")
            pid = proj["id"]
            s1 = await db_module.register_session(db, pid, "a")
            s2 = await db_module.register_session(db, pid, "b")

            row = await db_module.send_message(db, pid, s2["id"], "still works", from_session_id=s1["id"])
            assert row["payload"] == "still works"
            msgs = await db_module.receive_messages(db, s2["id"])
            assert len(msgs) == 1
        finally:
            await db.close()
    asyncio.run(_run())


def test_live_round_trip_through_fake_pubsub_send_message_to_subscriber(monkeypatch):
    """END-TO-END (mocked Redis, per this item's own explicit local-test
    scope): send_message -> real publish call -> subscribe_session_messages
    actually yields the pushed content, proving the publish/subscribe wiring
    is correct before it's ever pointed at a real Redis instance."""
    fake = _FakeRedisClient()

    async def _fake_get_client():
        return fake

    monkeypatch.setattr(redis_bridge, "get_redis_client", _fake_get_client)

    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "redis-e2e-proj")
            pid = proj["id"]
            planner = await db_module.register_session(db, pid, "planner")
            executor = await db_module.register_session(db, pid, "executor")

            # Subscriber "arrives" first, as a real listener would.
            received = []

            async def _listen():
                async for msg in redis_bridge.subscribe_session_messages(executor["id"]):
                    received.append(msg)

            listen_task = asyncio.ensure_future(_listen())
            await asyncio.sleep(0)  # let the subscribe() call register before we publish

            sent = await db_module.send_message(
                db, pid, executor["id"], "pushed, not polled",
                from_session_id=planner["id"], kind="run_tool",
            )

            # The fake pubsub's listen() actively polls (like a real one
            # blocks) rather than terminating after one item, so wait for
            # the message to actually arrive rather than for the task to
            # finish on its own, then cancel the still-running listener.
            for _ in range(200):
                if received:
                    break
                await asyncio.sleep(0.01)
            listen_task.cancel()
            try:
                await listen_task
            except asyncio.CancelledError:
                pass

            assert len(received) == 1
            assert received[0]["id"] == sent["id"]
            assert received[0]["payload"] == "pushed, not polled"
        finally:
            await db.close()
    asyncio.run(_run())


def test_subscribe_session_messages_yields_nothing_when_unconfigured():
    """No Redis configured -> the async generator is immediately exhausted,
    not an error -- callers fall back to polling receive_messages."""
    async def _run():
        results = []
        async for msg in redis_bridge.subscribe_session_messages("s1"):
            results.append(msg)
        assert results == []
    asyncio.run(_run())
