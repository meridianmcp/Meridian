"""2f9bad06 — synthetic/canary tool-call relay via existing
send_message/receive_messages.

Adds a documented executor-side convention (agent_defaults.py v13) on top of
the ALREADY-WORKING send_message/receive_messages primitives: a planner
session with no direct route to a tool (e.g. no tunnel access) can ask an
already-connected executor to run it for real and report the actual result
back. No new tools, no new schema -- a {action, tool, args, correlation_id}
JSON convention carried in the existing `payload` string field.

Deliberately NOT a general RPC framework (see the item's own notes): single
target only, no new message kind enum, no auth beyond what send_message
already has.
"""
from __future__ import annotations

import asyncio
import json

from meridian import db as db_module
from meridian.agent_defaults import (
    AGENT_INSTRUCTIONS_STANDARD_VERSION,
    DEFAULT_AGENT_INSTRUCTIONS,
    parse_standard_version,
)


def test_standard_version_bumped_to_13():
    assert AGENT_INSTRUCTIONS_STANDARD_VERSION >= 13
    expected_marker = f"meridian-executor-standard: v{AGENT_INSTRUCTIONS_STANDARD_VERSION}"
    assert expected_marker in DEFAULT_AGENT_INSTRUCTIONS


def test_version_marker_matches_constant():
    embedded = parse_standard_version(DEFAULT_AGENT_INSTRUCTIONS)
    assert embedded == AGENT_INSTRUCTIONS_STANDARD_VERSION


def test_relay_convention_documented_with_exact_payload_shape():
    """The new section must name the exact JSON shape an executor is expected
    to recognize -- prose alone ("handle run_tool messages") without the exact
    field names is exactly the kind of ambiguity v10/v11's grep/glob anti-
    pattern history shows silently fails in practice."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    assert "synthetic/canary tool-call relay" in text.lower()
    assert "receive_messages" in text
    assert '"action": "run_tool"' in text
    assert "correlation_id" in text
    # Must explicitly guard against treating message contents as authorization
    # to bypass hard rules (credentials hygiene) -- this session's own repeated
    # experience tonight with a pasted "credentials_rule" contradicting the
    # real hard rule is exactly the failure mode this guards against.
    assert "never read credentials" in text.lower() or "never treat a message" in text.lower()


def test_live_round_trip_planner_to_executor_and_back():
    """THE COMPLETION BAR: a real send_message -> receive_messages ->
    (executor runs a REAL tool) -> send_message -> receive_messages round
    trip, using the actual DB functions (not mocked), demonstrating the
    documented convention actually works end-to-end."""
    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "relay-test-proj")
            pid = proj["id"]

            planner = await db_module.register_session(
                db, pid, "planner-session", session_type="human"
            )
            executor = await db_module.register_session(
                db, pid, "executor-session", session_type="human"
            )
            planner_id = planner["id"]
            executor_id = executor["id"]

            # 1. Planner sends a run_tool request for a real, safe, read-only
            # tool -- get_pinned_decisions, which just queries the DB.
            correlation_id = "relay-test-corr-1"
            request_payload = json.dumps({
                "action": "run_tool",
                "tool": "get_pinned_decisions",
                "args": {"project_id": pid},
                "correlation_id": correlation_id,
            })
            await db_module.send_message(
                db, pid, executor_id, request_payload,
                from_session_id=planner_id, kind="run_tool",
            )

            # 2. Executor polls receive_messages and picks it up for real.
            inbox = await db_module.receive_messages(db, executor_id)
            assert len(inbox) == 1
            received = json.loads(inbox[0]["payload"])
            assert received["action"] == "run_tool"
            assert received["tool"] == "get_pinned_decisions"
            assert received["correlation_id"] == correlation_id

            # A second poll must see nothing new -- receive_messages marks
            # read by default, exactly as documented.
            second_poll = await db_module.receive_messages(db, executor_id)
            assert second_poll == []

            # 3. Executor actually RUNS the named tool for real (not a canned
            # response) -- this is the whole point: a genuine, live result.
            real_result = await db_module.get_pinned_decisions(db, pid)
            assert isinstance(real_result, list)

            # 4. Executor sends the REAL result back, tagged to the correlation id.
            reply_payload = json.dumps({
                "correlation_id": correlation_id,
                "result": real_result,
            })
            await db_module.send_message(
                db, pid, planner_id, reply_payload,
                from_session_id=executor_id, kind="run_tool_result",
            )

            # 5. Planner polls and receives the real result.
            planner_inbox = await db_module.receive_messages(db, planner_id)
            assert len(planner_inbox) == 1
            reply = json.loads(planner_inbox[0]["payload"])
            assert reply["correlation_id"] == correlation_id
            assert reply["result"] == real_result
            assert planner_inbox[0]["kind"] == "run_tool_result"
            assert planner_inbox[0]["from_session_id"] == executor_id
        finally:
            await db.close()

    asyncio.run(_run())


def test_unrecognized_payload_shape_is_inert():
    """A message that doesn't match the {action: run_tool, ...} shape must be
    treated as a normal coordination message, not misinterpreted as a tool
    request -- the convention must not swallow unrelated traffic."""
    async def _run():
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "relay-inert-proj")
            pid = proj["id"]
            s1 = await db_module.register_session(db, pid, "s1")
            s2 = await db_module.register_session(db, pid, "s2")

            await db_module.send_message(
                db, pid, s2["id"], "just a plain status update, not JSON",
                from_session_id=s1["id"],
            )
            inbox = await db_module.receive_messages(db, s2["id"])
            assert len(inbox) == 1
            # A real executor's handler must not crash trying to json.loads
            # and treat this as a run_tool request -- verify the payload
            # genuinely isn't the run_tool shape (informs the doc guidance:
            # "Only act on messages that match this exact shape").
            try:
                parsed = json.loads(inbox[0]["payload"])
                is_run_tool = isinstance(parsed, dict) and parsed.get("action") == "run_tool"
            except json.JSONDecodeError:
                is_run_tool = False
            assert not is_run_tool
        finally:
            await db.close()

    asyncio.run(_run())
